"""Todo domain models (port of Models/TodoItem.cs and friends).

Datetimes are timezone-aware ISO strings on the wire (DateTimeOffset
equivalent). Color markers carry the PaneBox hex + localization mapping.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from typing import Optional

from .base import JsonModel
from .widget_config import omit_none


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _truncate_to_seconds(value: Optional[datetime]) -> Optional[datetime]:
    """DateTimeOffset(Y,M,D,H,M,S) drops sub-second ticks; mirror that."""
    if value is None:
        return None
    return value.replace(microsecond=0)


class TodoRecurrenceMode:
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    WEEKDAYS = "weekdays"

    SUPPORTED = (NONE, DAILY, WEEKLY, MONTHLY, WEEKDAYS)

    @classmethod
    def normalize(cls, mode: Optional[str]) -> str:
        if not mode or not mode.strip():
            return cls.NONE
        normalized = mode.strip().lower()
        return normalized if normalized in cls.SUPPORTED else cls.NONE


class TodoReminderOptions:
    REMINDER_OFF = -1
    SUPPORTED_OFFSET_MINUTES = (0, 5, 10, 15, 30, 60, 1440)

    @classmethod
    def normalize_offset_minutes(cls, minutes: Optional[int]) -> Optional[int]:
        if minutes is None:
            return None
        return minutes if minutes == cls.REMINDER_OFF or minutes in cls.SUPPORTED_OFFSET_MINUTES else None

    @classmethod
    def is_reminder_off(cls, minutes: Optional[int]) -> bool:
        return minutes == cls.REMINDER_OFF


class TodoAttachmentStorage:
    LINKED = "linked"
    MANAGED = "managed"

    @staticmethod
    def normalize(storage_mode: Optional[str]) -> str:
        return (
            TodoAttachmentStorage.MANAGED
            if (storage_mode or "").strip().lower() == TodoAttachmentStorage.MANAGED
            else TodoAttachmentStorage.LINKED
        )


@dataclasses.dataclass
class TodoRecurrence(JsonModel):
    mode: str = TodoRecurrenceMode.NONE
    anchorDueDate: Optional[datetime] = None

    @classmethod
    def normalize(cls, recurrence: Optional["TodoRecurrence"], due_date: Optional[datetime]):
        """Drop recurrences without a due date or with mode none (C# Normalize)."""
        if due_date is None or recurrence is None:
            return None
        mode = TodoRecurrenceMode.normalize(recurrence.mode)
        if mode == TodoRecurrenceMode.NONE:
            return None
        recurrence.mode = mode
        recurrence.anchorDueDate = _truncate_to_seconds(recurrence.anchorDueDate) or _truncate_to_seconds(due_date)
        return recurrence

    def clone(self) -> "TodoRecurrence":
        return TodoRecurrence(
            mode=TodoRecurrenceMode.normalize(self.mode),
            anchorDueDate=_truncate_to_seconds(self.anchorDueDate),
        )


@dataclasses.dataclass
class TodoStep(JsonModel):
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    text: str = ""
    isCompleted: bool = False
    sortOrder: int = 0


@dataclasses.dataclass
class TodoAttachment(JsonModel):
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    filePath: str = ""
    displayName: str = ""
    type: str = "file"
    storageMode: str = TodoAttachmentStorage.LINKED
    addedAt: datetime = dataclasses.field(default_factory=_now_utc)

    @property
    def is_managed_copy(self) -> bool:
        return TodoAttachmentStorage.normalize(self.storageMode) == TodoAttachmentStorage.MANAGED


@dataclasses.dataclass
class TodoItem(JsonModel):
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    text: str = ""
    isCompleted: bool = False
    isImportant: bool = False
    colorMarker: Optional[str] = omit_none()
    dueDate: Optional[datetime] = None
    recurrence: Optional[TodoRecurrence] = None
    steps: list[TodoStep] = dataclasses.field(default_factory=list)
    notes: Optional[str] = omit_none()
    attachments: list[TodoAttachment] = dataclasses.field(default_factory=list)
    completedAt: Optional[datetime] = None
    reminderLastNotifiedAt: Optional[datetime] = None
    reminderDismissedForDueDate: Optional[datetime] = None
    reminderOffsetMinutes: Optional[int] = None
    snoozedUntil: Optional[datetime] = None
    snoozeLastNotifiedAt: Optional[datetime] = None
    recurrenceSeriesId: Optional[str] = omit_none()
    generatedNextItemId: Optional[str] = omit_none()
    sortOrder: int = 0
    createdAt: datetime = dataclasses.field(default_factory=_now_utc)
    updatedAt: datetime = dataclasses.field(default_factory=_now_utc)
    deviceId: Optional[str] = omit_none()
    isDeleted: bool = False

    NESTED = {
        "recurrence": (TodoRecurrence, "one"),
        "steps": (TodoStep, "list"),
        "attachments": (TodoAttachment, "list"),
    }

    # ---- color markers (TodoItem.cs statics) --------------------------------

    RED, ORANGE, YELLOW, GREEN, BLUE = "red", "orange", "yellow", "green", "blue"
    PURPLE, TEAL, PINK = "purple", "teal", "pink"
    SUPPORTED_COLOR_MARKERS = (RED, ORANGE, YELLOW, GREEN, BLUE, PURPLE, TEAL, PINK)

    COLOR_HEX = {
        RED: "#E34D4D",
        ORANGE: "#F08A3C",
        YELLOW: "#F2C94C",
        GREEN: "#4CAF6D",
        BLUE: "#4D8FE3",
        PURPLE: "#9B6BE8",
        TEAL: "#2DB7A3",
        PINK: "#E66AA2",
    }

    @staticmethod
    def normalize_color_marker(color_marker: Optional[str]) -> Optional[str]:
        if not color_marker or not color_marker.strip():
            return None
        normalized = color_marker.strip().lower()
        return normalized if normalized in TodoItem.SUPPORTED_COLOR_MARKERS else None

    @classmethod
    def color_marker_hex(cls, color_marker: Optional[str]) -> str:
        return cls.COLOR_HEX.get(cls.normalize_color_marker(color_marker), "#8A8F98")

    @staticmethod
    def color_marker_localization_key(color_marker: Optional[str]) -> str:
        normalized = TodoItem.normalize_color_marker(color_marker)
        return f"Todo.Color.{normalized.capitalize()}" if normalized else "Todo.Color.None"


@dataclasses.dataclass
class TodoWidgetData(JsonModel):
    version: int = 3
    items: list[TodoItem] = dataclasses.field(default_factory=list)

    NESTED = {"items": (TodoItem, "list")}
