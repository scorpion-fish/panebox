"""Desktop-organization models (port of Models/DesktopOrganizationModels.cs
and Models/OrganizationHistoryEntry.cs).

Categories, routing rules, scan snapshots, plans, the recovery journal and
undo receipts. Journal identity on Linux is device + inode (the C# uses
volume serial + 128-bit file id — same authority, different syscall).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from ..models.base import JsonModel
from ..models.settings_slices import DesktopOrganizationRule  # noqa: F401 (re-export; canonical home = models)


class StatLike(Protocol):
    """Structural type for os.stat results used by identity capture."""

    st_dev: int
    st_ino: int
    st_size: int
    st_mtime: float


class CategoryIds:
    SHORTCUTS = "Shortcuts"
    DOCUMENTS = "Documents"
    IMAGES = "Images"
    MEDIA = "Media"
    PACKAGES = "Packages"
    OTHER = "Other"

    DEFAULT_ORDER = (SHORTCUTS, DOCUMENTS, IMAGES, MEDIA, PACKAGES, OTHER)


class SubtypeIds:
    PDF = "Pdf"
    WORD = "Word"
    EXCEL = "Excel"
    POWERPOINT = "PowerPoint"
    TEXT = "Text"
    AUDIO = "Audio"
    VIDEO = "Video"


class SourceScope:
    PERSONAL = "Personal"
    PUBLIC = "Public"


class ExclusionReason:
    NONE = "None"
    FOLDER = "Folder"
    HIDDEN_OR_SYSTEM = "HiddenOrSystem"
    REPARSE_POINT = "ReparsePoint"
    OFFLINE_PLACEHOLDER = "OfflinePlaceholder"
    TEMPORARY_OR_DOWNLOADING = "TemporaryOrDownloading"
    PUBLIC_DESKTOP_ITEM = "PublicDesktopItem"
    UNAVAILABLE = "Unavailable"
    SLOW_ITEM = "SlowItem"
    BATCH_LIMIT = "BatchLimit"
    USER_CHOICE = "UserChoice"
    SOURCE_NOT_SELECTED = "SourceNotSelected"

    OPT_INABLE = (FOLDER, SLOW_ITEM, BATCH_LIMIT)


class DestinationMode:
    DYNAMIC = "Dynamic"
    EXISTING_WIDGET = "ExistingWidget"


class RetentionReason:
    SOURCE_CHANGED = "SourceChanged"
    IN_USE = "InUse"
    ACCESS_DENIED = "AccessDenied"
    UNAVAILABLE = "Unavailable"
    TRANSFER_FAILED = "TransferFailed"
    CANCELED = "Canceled"


class OrganizationActionType:
    MANAGED_DROP = "ManagedDrop"
    MOVE_BACK_TO_DESKTOP = "MoveBackToDesktop"
    DESKTOP_ORGANIZATION = "DesktopOrganization"


@dataclass
class Classification:
    category_id: str
    subtype_id: Optional[str]
    extension: str


@dataclass
class FileSnapshot:
    source_path: str
    name: str
    extension: str
    size: int
    last_write_time_utc: datetime
    category_id: str
    subtype_id: Optional[str]
    exclusion_reason: str = ExclusionReason.NONE
    is_directory: bool = False
    source_scope: str = SourceScope.PERSONAL
    mtime_ns: int = 0  # exact revalidation anchor (not serialized; scan is same-session)

    @property
    def is_eligible(self) -> bool:
        return self.exclusion_reason == ExclusionReason.NONE

    @property
    def can_opt_in(self) -> bool:
        return self.exclusion_reason in ExclusionReason.OPT_INABLE


@dataclass
class ScanResult:
    desktop_path: str = ""
    public_desktop_path: str = ""
    public_desktop_unavailable: bool = False
    items: list[FileSnapshot] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def eligible_count(self) -> int:
        return sum(1 for item in self.items if item.is_eligible)

    @property
    def eligible_size(self) -> int:
        return sum(item.size for item in self.items if item.is_eligible)

    @property
    def excluded_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            if not item.is_eligible:
                counts[item.exclusion_reason] = counts.get(item.exclusion_reason, 0) + 1
        return counts


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def intersects(self, other: "Rect") -> bool:
        return self.x < other.right and self.right > other.x and self.y < other.bottom and self.bottom > other.y


@dataclass
class TargetPlan:
    source_bucket_id: str
    category_id: str = CategoryIds.OTHER
    target_widget_id: str = ""
    suggested_display_name: str = ""
    target_directory_path: str = ""
    creates_widget: bool = False
    items: list[FileSnapshot] = field(default_factory=list)
    planned_bounds: Optional[Rect] = None

    def clone_with(
        self,
        target_widget_id: str,
        display_name: str,
        target_directory_path: str,
        creates_widget: bool,
        items: list[FileSnapshot],
    ) -> "TargetPlan":
        return TargetPlan(
            source_bucket_id=self.source_bucket_id,
            category_id=self.category_id,
            target_widget_id=target_widget_id,
            suggested_display_name=display_name,
            target_directory_path=target_directory_path,
            creates_widget=creates_widget,
            items=list(items),
            planned_bounds=self.planned_bounds,
        )


@dataclass
class TargetSelection:
    source_bucket_id: str
    is_selected: bool = True
    destination_mode: str = DestinationMode.DYNAMIC
    existing_widget_id: Optional[str] = None


@dataclass
class Plan:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    desktop_path: str = ""
    public_desktop_path: str = ""
    public_desktop_unavailable: bool = False
    include_personal_desktop: bool = True
    include_public_desktop: bool = False
    source_items: list[FileSnapshot] = field(default_factory=list)
    storage_root_path: str = ""
    targets: list[TargetPlan] = field(default_factory=list)
    excluded_items: list[FileSnapshot] = field(default_factory=list)

    @property
    def eligible_item_count(self) -> int:
        return sum(len(target.items) for target in self.targets)

    @property
    def new_widget_count(self) -> int:
        return sum(1 for target in self.targets if target.creates_widget)

    @property
    def total_transfer_size(self) -> int:
        return sum(item.size for target in self.targets for item in target.items)


@dataclass
class DestinationIdentity(JsonModel):
    """Durable object identity (device + inode) recorded at move completion."""

    volumeSerialNumber: int = 0  # st_dev
    fileIdHigh: int = 0  # st_ino >> 32 (kept as two halves for wire parity)
    fileIdLow: int = 0  # st_ino & 0xFFFFFFFF

    @classmethod
    def of(cls, stat: StatLike) -> "DestinationIdentity":
        ino = stat.st_ino
        return cls(
            volumeSerialNumber=stat.st_dev,
            fileIdHigh=ino >> 32,
            fileIdLow=ino & 0xFFFFFFFF,
        )

    @property
    def inode(self) -> int:
        return (self.fileIdHigh << 32) | (self.fileIdLow & 0xFFFFFFFF)

    def matches(self, stat: StatLike) -> bool:
        return stat.st_dev == self.volumeSerialNumber and stat.st_ino == self.inode


@dataclass
class RecoveryItem(JsonModel):
    restorePath: Optional[str] = None
    sourceScope: str = SourceScope.PERSONAL
    size: Optional[int] = None
    lastWriteTimeUtc: Optional[datetime] = None
    sourcePath: str = ""
    destinationPath: str = ""
    targetWidgetId: str = ""
    completed: bool = False
    destinationIdentity: Optional[DestinationIdentity] = None
    mtimeNs: Optional[int] = None  # Linux port: exact mtime for identity conjunct

    NESTED = {"destinationIdentity": (DestinationIdentity, "one")}


@dataclass
class RecoveryJournal(JsonModel):
    isUndo: bool = False
    transactionId: str = ""
    isAbandoned: bool = False
    startedAt: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    items: list[RecoveryItem] = field(default_factory=list)
    createdWidgetIds: list[str] = field(default_factory=list)

    NESTED = {"items": (RecoveryItem, "list")}


@dataclass
class RetainedItem:
    source_path: str
    name: str
    reason: str
    detail: str
    source_scope: str = SourceScope.PERSONAL


@dataclass
class HistoryItem(JsonModel):
    sourceScope: str = SourceScope.PERSONAL
    isRestored: bool = False
    restoredPath: Optional[str] = None
    size: Optional[int] = None
    lastWriteTimeUtc: Optional[datetime] = None
    destinationIdentity: Optional[DestinationIdentity] = None
    name: str = ""
    sourcePath: str = ""
    destinationPath: str = ""
    targetWidgetId: str = ""
    targetWidgetName: str = ""
    mtimeNs: Optional[int] = None  # Linux port: exact mtime for identity conjunct

    NESTED = {"destinationIdentity": (DestinationIdentity, "one")}


@dataclass
class HistoryTarget(JsonModel):
    widgetId: str = ""
    widgetName: str = ""
    directoryPath: str = ""
    wasCreated: bool = False


@dataclass
class OrganizationHistoryEntry(JsonModel):
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestampUtc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    widgetId: str = ""
    widgetName: str = ""
    actionType: str = OrganizationActionType.MANAGED_DROP
    transferMode: str = "Move"
    canUndo: bool = False
    isUndone: bool = False
    undoStarted: bool = False
    totalItemCount: int = 0
    undoReceiptsDiscarded: bool = False
    errorMessage: Optional[str] = None
    items: list[HistoryItem] = field(default_factory=list)
    targets: list[HistoryTarget] = field(default_factory=list)

    NESTED = {
        "items": (HistoryItem, "list"),
        "targets": (HistoryTarget, "list"),
    }

    @property
    def is_failed(self) -> bool:
        return bool(self.errorMessage)

    @property
    def item_count(self) -> int:
        return self.totalItemCount if self.totalItemCount > 0 else len(self.items)


@dataclass
class ExecutionResult:
    history: OrganizationHistoryEntry = field(default_factory=OrganizationHistoryEntry)
    completed_items: list[HistoryItem] = field(default_factory=list)
    created_widgets: list[object] = field(default_factory=list)  # WidgetConfig
    retained_items: list[RetainedItem] = field(default_factory=list)


class OrganizeError(Exception):
    """Base class surfaced verbatim in the organize window."""


class PendingRecoveryError(OrganizeError):
    def __init__(self):
        super().__init__("A pending desktop operation must be recovered first.")


class InsufficientSpaceError(OrganizeError):
    def __init__(self, drive_name: str):
        super().__init__(f"There is not enough free space on {drive_name}.")
        self.drive_name = drive_name


class SourceChangedError(OrganizeError):
    def __init__(self, item_name: str):
        super().__init__(f"The desktop item changed after it was scanned: {item_name}")
        self.item_name = item_name


class IncompleteUndoError(OrganizeError):
    def __init__(self, restored_count: int, remaining_count: int):
        super().__init__(f"Restored {restored_count} item(s); {remaining_count} item(s) still need restoration.")
        self.restored_count = restored_count
        self.remaining_count = remaining_count


def entries_to_dict(entries: list[OrganizationHistoryEntry]) -> dict:
    return {"items": [entry.to_dict() for entry in entries]}


def entries_from_dict(data: dict) -> list[OrganizationHistoryEntry]:
    raw = data.get("items") if isinstance(data, dict) else None
    return [OrganizationHistoryEntry.from_dict(item) for item in raw] if isinstance(raw, list) else []
