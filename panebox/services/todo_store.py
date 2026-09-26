"""Per-widget Todo store (port of Services/TodoWidgetStore.cs).

data/widgets/<sanitized-id>/todo.json via the resilient store; Normalize
mirrors the C# pass field-for-field (ids, reminders, recurrence series
linkage, sort orders, dedupe).
"""

from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..constants import WIDGETS_DATA_DIR
from ..models.todo import (
    TodoAttachment,
    TodoAttachmentStorage,
    TodoItem,
    TodoRecurrence,
    TodoStep,
    TodoWidgetData,
)
from . import todo_recurrence
from .resilient_json_store import ResilientJsonStore

_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_widget_id(widget_id: str) -> str:
    safe = _INVALID_FILENAME.sub("_", widget_id.strip())
    return safe or uuid.uuid4().hex


class TodoWidgetStore:
    def __init__(self, widget_id: str, widgets_root: Path = WIDGETS_DATA_DIR):
        if not widget_id or not widget_id.strip():
            raise ValueError("widget id cannot be empty")
        self.widget_id = widget_id
        self.data_dir = Path(widgets_root) / sanitize_widget_id(widget_id)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = ResilientJsonStore(self.data_dir / "todo.json")

    @property
    def store_path(self) -> Path:
        return self.store.path

    @property
    def attachment_directory(self) -> Path:
        return self.data_dir / "attachments"

    # ---- load / save -----------------------------------------------------------

    def load(self) -> TodoWidgetData:
        result = self.store.load()
        return normalize(TodoWidgetData.from_dict(result.data))

    def save(self, data: TodoWidgetData) -> None:
        self.store.save(normalize(data).to_dict())

    def clear(self) -> None:
        self.save(TodoWidgetData())

    @staticmethod
    def delete_for_widget(widget_id: str, widgets_root: Path = WIDGETS_DATA_DIR) -> None:
        data_dir = Path(widgets_root) / sanitize_widget_id(widget_id)
        store_path = data_dir / "todo.json"
        store_path.unlink(missing_ok=True)
        store_path.with_name("todo.json.bak").unlink(missing_ok=True)
        shutil.rmtree(data_dir / "attachments", ignore_errors=True)
        try:
            if data_dir.exists() and not any(data_dir.iterdir()):
                data_dir.rmdir()
        except OSError:
            pass


# ---- Normalize (TodoWidgetStore.cs) -------------------------------------------


def normalize(data: Optional[TodoWidgetData]) -> TodoWidgetData:
    data = data or TodoWidgetData()
    data.version = max(3, data.version)
    data.items = [item for item in data.items if item is not None]

    fallback_sort_order = 0
    for item in data.items:
        if not item.id or not item.id.strip():
            item.id = uuid.uuid4().hex
        item.text = (item.text or "").strip()
        item.colorMarker = TodoItem.normalize_color_marker(item.colorMarker)
        item.recurrence = TodoRecurrence.normalize(item.recurrence, item.dueDate)
        item.recurrenceSeriesId = todo_recurrence.normalize_series_id(item.recurrenceSeriesId)
        item.notes = None if not (item.notes or "").strip() else item.notes
        item.steps = _normalize_steps(item.steps)
        item.attachments = _normalize_attachments(item.attachments)
        item.reminderOffsetMinutes = _normalize_reminder_offset(item.reminderOffsetMinutes)
        item.generatedNextItemId = (
            item.generatedNextItemId.strip() if item.generatedNextItemId and item.generatedNextItemId.strip() else None
        )
        if item.createdAt is None:
            item.createdAt = datetime.now(timezone.utc)
        if item.updatedAt is None:
            item.updatedAt = item.createdAt

        if item.isCompleted:
            item.completedAt = item.completedAt or item.updatedAt
        else:
            item.completedAt = None

        if item.dueDate is None:
            item.recurrence = None
            item.recurrenceSeriesId = None
            item.reminderLastNotifiedAt = None
            item.reminderDismissedForDueDate = None
            item.snoozedUntil = None
            item.snoozeLastNotifiedAt = None
        elif item.isCompleted:
            item.snoozedUntil = None
            item.snoozeLastNotifiedAt = None
        elif item.reminderDismissedForDueDate is not None and item.reminderDismissedForDueDate != item.dueDate:
            item.reminderLastNotifiedAt = None
            item.reminderDismissedForDueDate = None
            item.snoozedUntil = None
            item.snoozeLastNotifiedAt = None

        if not item.isCompleted or item.recurrence is None:
            item.generatedNextItemId = None
        if item.recurrence is None:
            item.recurrenceSeriesId = None
        if item.sortOrder < 0:
            item.sortOrder = fallback_sort_order
        fallback_sort_order += 1

    # Dedupe ids (first wins), drop blank, then order by sortOrder/updatedAt.
    seen: set[str] = set()
    unique = []
    for item in data.items:
        if item.text and item.id not in seen:
            seen.add(item.id)
            unique.append(item)
    unique.sort(key=lambda i: (i.sortOrder, -(i.updatedAt.timestamp() if i.updatedAt else 0)))
    data.items = unique

    _normalize_recurrence_series_ids(data.items)
    for index, item in enumerate(data.items):
        item.sortOrder = index
    return data


def _normalize_reminder_offset(minutes: Optional[int]) -> Optional[int]:
    from ..models.todo import TodoReminderOptions

    return TodoReminderOptions.normalize_offset_minutes(minutes)


def _normalize_steps(steps: list[TodoStep]) -> list[TodoStep]:
    order = 0
    for step in steps:
        if step is None:
            continue
        step.id = step.id.strip() if step.id and step.id.strip() else uuid.uuid4().hex
        step.text = (step.text or "").strip()
        step.sortOrder = order
        order += 1
    kept = [s for s in steps if s is not None and s.text]
    for index, step in enumerate(kept):
        step.sortOrder = index
    return kept


def _normalize_attachments(attachments: list[TodoAttachment]) -> list[TodoAttachment]:
    for attachment in attachments:
        if attachment is None:
            continue
        attachment.id = attachment.id.strip() if attachment.id and attachment.id.strip() else uuid.uuid4().hex
        attachment.filePath = (attachment.filePath or "").strip()
        attachment.displayName = (
            attachment.displayName.strip()
            if attachment.displayName and attachment.displayName.strip()
            else Path(attachment.filePath).name
        )
        attachment.type = attachment.type.strip() if attachment.type and attachment.type.strip() else "file"
        attachment.storageMode = TodoAttachmentStorage.normalize(attachment.storageMode)
        if attachment.addedAt is None:
            attachment.addedAt = datetime.now(timezone.utc)
    return [a for a in attachments if a is not None and a.filePath]


def _normalize_recurrence_series_ids(items: list[TodoItem]) -> None:
    """Link generated-occurrence chains into one shared series id (BFS)."""
    recurring = [item for item in items if item.recurrence is not None]
    if not recurring:
        return
    by_id = {item.id: item for item in recurring}
    visited: set[str] = set()

    for item in recurring:
        if item.id in visited:
            continue
        component = []
        queue = [item]
        visited.add(item.id)
        while queue:
            current = queue.pop(0)
            component.append(current)
            if current.generatedNextItemId and current.generatedNextItemId in by_id:
                nxt = by_id[current.generatedNextItemId]
                if nxt.id not in visited:
                    visited.add(nxt.id)
                    queue.append(nxt)
            for entry in recurring:
                if entry.generatedNextItemId == current.id and entry.id not in visited:
                    visited.add(entry.id)
                    queue.append(entry)

        series_id = (
            next(
                (
                    todo_recurrence.normalize_series_id(entry.recurrenceSeriesId)
                    for entry in component
                    if todo_recurrence.normalize_series_id(entry.recurrenceSeriesId)
                ),
                None,
            )
            or uuid.uuid4().hex
        )
        for entry in component:
            entry.recurrenceSeriesId = series_id
