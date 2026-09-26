"""Desktop-organization transaction (port of DesktopOrganizationTransaction
and its Restore partial).

Execute moves files per an approved plan with a durable crash-recovery
journal; undo restores them by object identity (device + inode + size + mtime);
startup recovery reconciles interrupted runs. The commit discipline mirrors
the C#: settings save FIRST, history receipt LAST, and the journal is only
cleared once both are durable.
"""

from __future__ import annotations

import os
import shutil
import threading
from typing import Callable, Optional

from ..models.widget_config import CURRENT_BOUNDS_COORDINATE_VERSION, WidgetConfig
from .file_ops import get_available_path
from .organize_models import (
    DestinationIdentity,
    ExecutionResult,
    ExclusionReason,
    HistoryItem,
    IncompleteUndoError,
    OrganizationActionType,
    HistoryTarget,
    OrganizationHistoryEntry,
    PendingRecoveryError,
    Plan,
    RecoveryItem,
    RecoveryJournal,
    RetainedItem,
    RetentionReason,
    SourceChangedError,
    SourceScope,
    InsufficientSpaceError,
)
from .organize_store import (
    HistoryStore,
    RecoveryStore,
    apply_retention_policy,
    is_empty_directory,
    merge_retry_history,
)

FREE_SPACE_SAFETY_MARGIN_BYTES = 16 * 1024 * 1024

ProgressCallback = Callable[[int, int, str, str], None]


def _utcnow_iso_ns(stat) -> int:
    return stat.st_mtime_ns


def capture_identity(path: str) -> Optional[DestinationIdentity]:
    """Object identity at `path` (device + inode); None when unavailable."""
    try:
        return DestinationIdentity.of(os.stat(path))
    except OSError:
        return None


def matches_snapshot(path: str, item: RecoveryItem) -> bool:
    """Identity-only authority: the object at `path` must still carry the
    identity recorded when the move completed (plus size/mtime conjunction)."""
    identity = item.destinationIdentity
    if identity is None:
        return False
    try:
        stat = os.stat(path)
    except OSError:
        return False
    if not identity.matches(stat):
        return False
    if item.size is not None and stat.st_size != item.size:
        return False
    mtime_ns = item.mtimeNs
    if mtime_ns is not None and stat.st_mtime_ns != mtime_ns:
        return False
    return True


def _entry_exists(path: str) -> bool:
    return os.path.exists(path)


class _Canceled(Exception):
    pass


class OrganizeTransaction:
    """Serialized execute/undo/recover over settings + journal + history."""

    def __init__(
        self,
        settings_service,
        history_store: HistoryStore,
        recovery_store: RecoveryStore,
        transfer=None,
    ):
        self.settings = settings_service
        self.history = history_store
        self.recovery = recovery_store
        self._transfer = transfer or _OsTransfer()
        self._gate = threading.Lock()

    @property
    def has_pending_recovery(self) -> bool:
        return self.recovery.has_pending_journal

    # ---- execute ------------------------------------------------------------

    def execute(
        self,
        plan: Plan,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> ExecutionResult:
        with self._gate:
            if self.has_pending_recovery:
                raise PendingRecoveryError()
            self._validate_plan(plan)
            self._validate_available_space(plan)

            layout = self.settings.layout
            original_widgets = list(layout.widgets)
            original_rules = list(self.rules())
            original_history = list(self.history.entries)
            created_directories: list[str] = []
            retained: list[RetainedItem] = []
            created_widgets = self._create_candidate_widgets(plan)
            journal = self._build_journal(plan)
            committed = False

            try:
                for target in plan.targets:
                    try:
                        if not os.path.isdir(target.target_directory_path):
                            os.makedirs(target.target_directory_path, exist_ok=True)
                            created_directories.append(target.target_directory_path)
                    except OSError as exc:
                        retained.extend(self._retained_for(item, exc) for item in target.items)
                        journal.items = [
                            item for item in journal.items if item.targetWidgetId != target.target_widget_id
                        ]

                self._reserve_destinations(journal)
                if journal.items:
                    self.recovery.save(journal)
                total = len(journal.items)
                completed = 0
                snapshots_by_path = {item.source_path.lower(): item for target in plan.targets for item in target.items}

                def record_completed(source: str, destination: str) -> None:
                    item = next(
                        (candidate for candidate in journal.items if candidate.sourcePath.lower() == source.lower()),
                        None,
                    )
                    if item is None or item.completed:
                        return
                    item.destinationPath = destination
                    item.completed = True
                    item.destinationIdentity = capture_identity(destination)
                    item.mtimeNs = _stat_mtime_ns(destination)
                    self.recovery.save(journal)

                for item in list(journal.items):
                    snapshot = snapshots_by_path.get(item.sourcePath.lower())
                    if snapshot is None:
                        continue
                    try:
                        if cancel is not None and cancel():
                            raise _Canceled()
                        self._revalidate_source(snapshot)
                    except Exception as exc:
                        retained.append(self._retained_for(snapshot, exc))
                        continue
                    try:
                        _destination = self._transfer.move(item.sourcePath, item.destinationPath)
                        record_completed(item.sourcePath, _destination)
                    except _Canceled:
                        retained.append(self._retained_for(snapshot, _Canceled("canceled")))
                    except Exception as exc:
                        retained.append(self._retained_for(snapshot, exc))
                    completed += 1
                    if progress is not None:
                        target_name = next(
                            (
                                t.suggested_display_name
                                for t in plan.targets
                                if t.target_widget_id == item.targetWidgetId
                            ),
                            "",
                        )
                        progress(completed, total, item.targetWidgetId, target_name)

                committed_plan = self._committed_plan(plan, journal)
                committed_ids = {t.target_widget_id for t in committed_plan.targets}
                created_widgets = [w for w in created_widgets if w.id in committed_ids]
                layout.widgets = [
                    w
                    for w in layout.widgets
                    if not (
                        any(t.creates_widget and t.target_widget_id == w.id for t in plan.targets)
                        and w.id not in committed_ids
                    )
                ]

                self._add_rules_for_new_targets(committed_plan)
                history = self._create_history(committed_plan, journal)
                if history.items:
                    previous = next((e for e in self.history.entries if e.id == history.id), None)
                    if previous is not None:
                        merge_retry_history(history, previous)
                        for target in previous.targets:
                            history.targets = [t for t in history.targets if t.widgetId != target.widgetId] + [target]
                        self.history.entries.remove(previous)
                    self.history.entries.insert(0, history)
                if not history.items:
                    history = next((e for e in self.history.entries if e.id == plan.id), history)
                completed_items = list(history.items)

                # Commit point: full receipts durable while the journal exists.
                if not self._save_settings_checked() or not self.history.save_checked():
                    raise OSError(
                        "Persisting the desktop organization commit failed; "
                        "the recovery journal is kept for the next launch."
                    )

                self.recovery.clear()
                committed = True

                if apply_retention_policy(self.history.entries):
                    self.history.save_checked()
                    self._save_settings_checked()

                self._remove_empty_created_directories(created_directories)
                return ExecutionResult(
                    history=history,
                    completed_items=completed_items,
                    created_widgets=created_widgets,
                    retained_items=retained,
                )
            except Exception:
                if committed:
                    raise
                layout.widgets = original_widgets
                self._set_rules(original_rules)
                self.history.entries = original_history
                # Completed moves are never reversed here — the journal keeps
                # every receipt for the next launch's recovery.
                self._remove_empty_created_directories(created_directories)
                self.history.save_checked()
                self._save_settings_checked()
                raise

    # ---- undo -----------------------------------------------------------------

    def undo(self, history_id: str) -> None:
        with self._gate:
            history = next((e for e in self.history.entries if e.id == history_id), None)
            if history is None:
                raise OrganizeStateError("The organization history no longer exists.")
            if not history.canUndo or history.isUndone:
                raise OrganizeStateError("This operation cannot be undone.")
            pending = self.recovery.load()
            if pending is not None:
                if pending.isAbandoned or not pending.isUndo or pending.transactionId != history_id:
                    raise OrganizeStateError("Recover the pending desktop operation first.")
                self._apply_undo_receipts(history, pending)
            reserved: set[str] = set()
            journal = RecoveryJournal(
                transactionId=history_id,
                isUndo=True,
                items=[
                    RecoveryItem(
                        sourcePath=item.sourcePath,
                        destinationPath=item.destinationPath,
                        restorePath=get_available_path(item.sourcePath, reserved),
                        targetWidgetId=item.targetWidgetId,
                        sourceScope=item.sourceScope,
                        size=item.size,
                        lastWriteTimeUtc=item.lastWriteTimeUtc,
                        destinationIdentity=item.destinationIdentity,
                        mtimeNs=item.mtimeNs,
                    )
                    for item in history.items
                    if not item.isRestored
                ],
            )
            self.recovery.save(journal)
            self._restore_items(journal)
            self._apply_undo_receipts(history, journal)
            if not self._save_settings_checked() or not self.history.save_checked():
                raise OSError("Persisting the undo receipts failed; the recovery journal is kept.")
            self.recovery.clear()
            remaining = sum(1 for item in history.items if not item.isRestored)
            if remaining > 0:
                raise IncompleteUndoError(len(history.items) - remaining, remaining)

    # ---- startup recovery --------------------------------------------------------

    def recover_pending(self) -> int:
        with self._gate:
            journal = self.recovery.load()
            if journal is None:
                self._compact_history()
                return 0

            if journal.isAbandoned:
                self._finalize_abandoned_journal(journal)
                return 0

            history = next((e for e in self.history.entries if e.id == journal.transactionId), None)
            if journal.isUndo:
                if history is None:
                    self.recovery.clear()
                    self._compact_history()
                    return 0
                if not history.canUndo or history.isUndone:
                    if self._save_settings_checked() and self.history.save_checked():
                        self.recovery.clear()
                        self._compact_history()
                        return sum(1 for item in journal.items if item.completed)
                    return sum(1 for item in journal.items if item.completed)
                self._apply_undo_receipts(history, journal)
                if self._save_settings_checked() and self.history.save_checked():
                    self.recovery.clear()
                    self._compact_history()
                return sum(1 for item in journal.items if item.completed)

            committed_paths = {item.sourcePath.lower() for item in (history.items if history else [])}
            journal.items = [item for item in journal.items if item.sourcePath.lower() not in committed_paths]
            reserved: set[str] = set()
            candidates: list[RecoveryItem] = []
            for item in journal.items:
                if not _entry_exists(item.destinationPath):
                    continue
                if not item.completed and (
                    _entry_exists(item.sourcePath) or not matches_snapshot(item.destinationPath, item)
                ):
                    continue
                if item.restorePath is None:
                    item.restorePath = get_available_path(item.sourcePath, reserved)
                candidates.append(item)
            journal.items = candidates
            for item in journal.items:
                item.completed = False
            self.recovery.save(journal)
            self._restore_items(journal)
            restored = sum(1 for item in journal.items if item.completed)
            journal.items = [item for item in journal.items if not item.completed]
            if journal.items:
                self.recovery.save(journal)
                return restored

            if history is not None and not history.items:
                self.history.entries.remove(history)
            self._remove_uncommitted_widgets(journal, history)
            if not self._save_settings_checked() or not self.history.save_checked():
                return restored
            self.recovery.clear()
            self._compact_history()
            return restored

    # ---- abandon --------------------------------------------------------------------

    def abandon_undo(self, history_id: str) -> Optional[OrganizationHistoryEntry]:
        with self._gate:
            history = next((e for e in self.history.entries if e.id == history_id), None)
            journal = self.recovery.load()
            owns_journal = journal is None or (journal.isUndo and journal.transactionId == history_id)
            if journal is not None and owns_journal:
                journal.isAbandoned = True
                self.recovery.save(journal)

            if history is not None and history.canUndo and not history.isUndone:
                history.canUndo = False
                history.undoStarted = False
                if not self.history.save_checked() or not self._save_settings_checked():
                    raise OSError("Persisting the abandon state failed; the journal is kept.")
            if owns_journal:
                self.recovery.clear()
                self._compact_history()
            return history

    def abandon_pending_recovery(self) -> None:
        with self._gate:
            journal = self.recovery.load()
            if journal is None:
                return
            journal.isAbandoned = True
            self.recovery.save(journal)
            if not self._finalize_abandoned_journal(journal):
                raise OSError("Persisting the abandoned recovery failed; the journal is kept.")

    # ---- internals -------------------------------------------------------------------

    def rules(self) -> list:
        return self.settings.settings.desktopOrganization.desktopOrganizationRules

    def _set_rules(self, rules: list) -> None:
        self.settings.settings.desktopOrganization.desktopOrganizationRules = rules

    def _save_settings_checked(self) -> bool:
        try:
            self.settings.save_now()
            return True
        except OSError:
            return False

    def _finalize_abandoned_journal(self, journal: RecoveryJournal) -> bool:
        history = next((e for e in self.history.entries if e.id == journal.transactionId), None)
        if journal.isUndo:
            if history is not None and history.canUndo and not history.isUndone:
                history.canUndo = False
                history.undoStarted = False
        else:
            self._remove_uncommitted_widgets(journal, history)
        if not self._save_settings_checked() or not self.history.save_checked():
            return False
        self.recovery.clear()
        self._compact_history()
        return True

    def _compact_history(self) -> None:
        if self.recovery.has_pending_journal:
            return
        if apply_retention_policy(self.history.entries):
            self.history.save_checked()
            self._save_settings_checked()

    def _remove_uncommitted_widgets(
        self, journal: RecoveryJournal, history: Optional[OrganizationHistoryEntry]
    ) -> None:
        if history is not None and not history.items:
            if history in self.history.entries:
                self.history.entries.remove(history)
        created_ids = set(journal.createdWidgetIds)
        if history is not None:
            created_ids -= {t.widgetId for t in history.targets}
        removable = [
            w
            for w in self.settings.layout.widgets
            if w.id in created_ids and w.mappedFolderPath and is_empty_directory(w.mappedFolderPath)
        ]
        for widget in removable:
            self.settings.layout.widgets = [w for w in self.settings.layout.widgets if w.id != widget.id]
            self._set_rules([r for r in self.rules() if r.targetWidgetId != widget.id])
            self._remove_empty_created_directories([widget.mappedFolderPath])

    def _restore_items(self, journal: RecoveryJournal) -> None:
        ready = [
            item
            for item in journal.items
            if not item.completed and item.restorePath and matches_snapshot(item.destinationPath, item)
        ]
        for item in ready:
            try:
                restored_to = self._transfer.move(item.destinationPath, item.restorePath)
            except Exception:
                continue
            item.restorePath = restored_to
            item.completed = True
            item.destinationIdentity = capture_identity(restored_to)
            item.mtimeNs = _stat_mtime_ns(restored_to)
            self.recovery.save(journal)

    @staticmethod
    def _apply_undo_receipts(history: OrganizationHistoryEntry, journal: RecoveryJournal) -> None:
        history.undoStarted = True
        for receipt in journal.items:
            if not receipt.completed and (
                _entry_exists(receipt.destinationPath)
                or receipt.restorePath is None
                or not matches_snapshot(receipt.restorePath, receipt)
            ):
                continue
            item = next(
                (
                    candidate
                    for candidate in history.items
                    if candidate.destinationPath.lower() == receipt.destinationPath.lower()
                ),
                None,
            )
            if item is None:
                continue
            item.isRestored = True
            item.restoredPath = receipt.restorePath
        history.isUndone = all(item.isRestored for item in history.items)
        history.canUndo = not history.isUndone

    # ---- plan validation -----------------------------------------------------------

    def _validate_plan(self, plan: Plan) -> None:
        if not plan.targets or plan.eligible_item_count == 0:
            raise OrganizeStateError("The desktop organization plan is empty.")
        desktop = os.path.abspath(plan.desktop_path).rstrip(os.sep)
        storage = os.path.abspath(plan.storage_root_path).rstrip(os.sep)
        if os.path.dirname(storage) == "" or storage in ("/", ""):
            raise OrganizeStateError("A drive root cannot be used as the managed storage folder.")
        if _same(desktop, storage) or storage.lower().startswith(desktop.lower() + os.sep):
            raise OrganizeStateError("The managed storage root cannot be the desktop or one of its subfolders.")
        if plan.public_desktop_path and _paths_overlap(storage, plan.public_desktop_path):
            raise OrganizeStateError("Managed storage overlaps the public desktop.")
        for target in plan.targets:
            for item in target.items:
                root = plan.public_desktop_path if item.source_scope == SourceScope.PUBLIC else plan.desktop_path
                if (
                    not root
                    or not item.is_eligible
                    or (item.source_scope == SourceScope.PUBLIC and not plan.include_public_desktop)
                    or (item.source_scope == SourceScope.PERSONAL and not plan.include_personal_desktop)
                    or _same(os.path.dirname(item.source_path), root.rstrip(os.sep)) is False
                ):
                    raise OrganizeStateError("An item is outside the selected desktop scope.")
            directory = os.path.abspath(target.target_directory_path)
            if _paths_overlap(directory, desktop) or (
                plan.public_desktop_path and _paths_overlap(directory, plan.public_desktop_path)
            ):
                raise OrganizeStateError("An organization target cannot be the desktop or one of its subfolders.")
            if (
                not (directory.lower().startswith(storage.lower() + os.sep) or _same(directory, storage))
                and target.creates_widget
            ):
                raise OrganizeStateError("A new organization target is outside the managed storage root.")

    @staticmethod
    def _validate_available_space(plan: Plan) -> None:
        for directory, required in compute_required_space_by_device(plan).items():
            try:
                stat = os.statvfs(directory)
                available = stat.f_bavail * stat.f_frsize
            except OSError:
                continue
            if available < required + FREE_SPACE_SAFETY_MARGIN_BYTES:
                raise InsufficientSpaceError(directory)

    @staticmethod
    def _revalidate_source(item) -> None:
        stat = os.lstat(item.source_path)
        if os.path.islink(item.source_path) or stat.st_size != item.size or stat.st_mtime_ns != item.mtime_ns:
            raise SourceChangedError(item.name)

    def _create_candidate_widgets(self, plan: Plan) -> list[WidgetConfig]:
        created: list[WidgetConfig] = []
        shell = self.settings.settings.widgetShell
        for target in plan.targets:
            if not target.creates_widget:
                continue
            bounds = target.planned_bounds
            config = WidgetConfig(
                id=target.target_widget_id,
                name=target.suggested_display_name,
                isDefaultTitle=False,
                widgetKind="File",
                mappedFolderPath=target.target_directory_path,
                followsDefaultStoragePath=True,
                managedFolderName=os.path.relpath(target.target_directory_path, plan.storage_root_path),
                boundsCoordinateVersion=CURRENT_BOUNDS_COORDINATE_VERSION,
                width=shell.defaultWidgetWidth,
                height=shell.defaultWidgetHeight,
                x=bounds.x if bounds else 100.0,
                y=bounds.y if bounds else 100.0,
                isVisible=True,
            )
            self.settings.layout.widgets.append(config)
            created.append(config)
        return created

    @staticmethod
    def _build_journal(plan: Plan) -> RecoveryJournal:
        return RecoveryJournal(
            transactionId=plan.id,
            createdWidgetIds=[target.target_widget_id for target in plan.targets if target.creates_widget],
            items=[
                RecoveryItem(
                    sourceScope=item.source_scope,
                    size=None if item.is_directory else item.size,
                    lastWriteTimeUtc=item.last_write_time_utc,
                    sourcePath=item.source_path,
                    targetWidgetId=target.target_widget_id,
                    destinationPath=os.path.join(target.target_directory_path, item.name),
                )
                for target in plan.targets
                for item in target.items
            ],
        )

    @staticmethod
    def _reserve_destinations(journal: RecoveryJournal) -> None:
        reserved: set[str] = set()
        for item in journal.items:
            item.destinationPath = get_available_path(item.destinationPath, reserved)

    def _add_rules_for_new_targets(self, plan: Plan) -> None:
        for target in plan.targets:
            if not target.creates_widget:
                continue
            categories = [category for category in {item.category_id for item in target.items} if category]
            from .organize_models import DesktopOrganizationRule

            self.rules().append(
                DesktopOrganizationRule(
                    targetWidgetId=target.target_widget_id,
                    categoryIds=categories or [target.category_id],
                )
            )

    @staticmethod
    def _create_history(plan: Plan, journal: RecoveryJournal) -> OrganizationHistoryEntry:
        names_by_id = {target.target_widget_id: target.suggested_display_name for target in plan.targets}
        return OrganizationHistoryEntry(
            id=plan.id,
            actionType=OrganizationActionType.DESKTOP_ORGANIZATION,
            transferMode="Move",
            canUndo=any(item.completed for item in journal.items),
            widgetName="Desktop organization",
            targets=[
                HistoryTarget(
                    widgetId=target.target_widget_id,
                    widgetName=target.suggested_display_name,
                    directoryPath=target.target_directory_path,
                    wasCreated=target.creates_widget,
                )
                for target in plan.targets
            ],
            items=[
                HistoryItem(
                    name=os.path.basename(item.destinationPath),
                    sourcePath=item.sourcePath,
                    destinationPath=item.destinationPath,
                    targetWidgetId=item.targetWidgetId,
                    targetWidgetName=names_by_id.get(item.targetWidgetId, ""),
                    sourceScope=item.sourceScope,
                    size=item.size,
                    lastWriteTimeUtc=item.lastWriteTimeUtc,
                    destinationIdentity=item.destinationIdentity,
                    mtimeNs=item.mtimeNs,
                )
                for item in journal.items
                if item.completed
            ],
        )

    @staticmethod
    def _committed_plan(plan: Plan, journal: RecoveryJournal) -> Plan:
        completed_by_target: dict[str, set[str]] = {}
        for item in journal.items:
            if item.completed:
                completed_by_target.setdefault(item.targetWidgetId, set()).add(item.sourcePath.lower())
        targets = [
            target.clone_with(
                target.target_widget_id,
                target.suggested_display_name,
                target.target_directory_path,
                target.creates_widget,
                [
                    item
                    for item in target.items
                    if item.source_path.lower() in completed_by_target.get(target.target_widget_id, set())
                ],
            )
            for target in plan.targets
            if target.target_widget_id in completed_by_target
        ]
        return Plan(
            id=plan.id,
            desktop_path=plan.desktop_path,
            storage_root_path=plan.storage_root_path,
            excluded_items=list(plan.excluded_items),
            targets=[target for target in targets if target.items],
        )

    @staticmethod
    def _retained_for(item, exc: Exception) -> RetainedItem:
        if isinstance(exc, SourceChangedError):
            reason = RetentionReason.SOURCE_CHANGED
        elif isinstance(exc, _Canceled):
            reason = RetentionReason.CANCELED
        elif isinstance(exc, PermissionError):
            reason = RetentionReason.ACCESS_DENIED
        elif isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            reason = RetentionReason.UNAVAILABLE
        elif isinstance(exc, OSError):
            reason = RetentionReason.UNAVAILABLE
        else:
            reason = RetentionReason.TRANSFER_FAILED
        return RetainedItem(
            source_path=item.source_path,
            name=item.name,
            reason=reason,
            detail=str(exc) or type(exc).__name__,
            source_scope=getattr(item, "source_scope", SourceScope.PERSONAL),
        )

    @staticmethod
    def _remove_empty_created_directories(directories: list[str]) -> None:
        for directory in sorted(directories, key=len, reverse=True):
            try:
                if os.path.isdir(directory) and not os.listdir(directory):
                    os.rmdir(directory)
            except OSError:
                pass


class OrganizeStateError(Exception):
    """Invalid organize/undo state (surfaced verbatim in the window)."""


def compute_required_space_by_device(plan: Plan) -> dict[str, int]:
    """Same-device moves are renames; only cross-device items charge the target."""
    required: dict[str, int] = {}
    for target in plan.targets:
        target_dir = os.path.abspath(target.target_directory_path)
        for item in target.items:
            try:
                target_device = os.stat(target_dir).st_dev
                source_device = os.stat(item.source_path).st_dev
            except OSError:
                continue
            if target_device == source_device:
                continue
            required[target_dir] = required.get(target_dir, 0) + item.size
    return required


def _same(a: str, b: str) -> bool:
    return os.path.normpath(a).rstrip(os.sep).lower() == os.path.normpath(b).rstrip(os.sep).lower()


def _paths_overlap(a: str, b: str) -> bool:
    """Same path or one nested inside the other (FileService.PathsOverlap)."""
    left = os.path.abspath(a).rstrip(os.sep).lower()
    right = os.path.abspath(b).rstrip(os.sep).lower()
    return left == right or left.startswith(right + os.sep) or right.startswith(left + os.sep)


def _stat_mtime_ns(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


class _OsTransfer:
    """Per-item move to an explicit destination path (no shell elevation)."""

    @staticmethod
    def move(source: str, destination: str) -> str:
        result = shutil.move(source, destination)
        return destination if isinstance(result, str) else destination


_ = ExclusionReason  # re-exported for callers extending this module
