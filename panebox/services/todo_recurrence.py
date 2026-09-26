"""Recurrence engine (port of Services/TodoRecurrenceService.cs).

All date math happens in local wall-clock time (the C# converts to local,
adds calendar units, converts back), so a daily task at 09:00 stays at 09:00
across DST boundaries.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timedelta
from typing import Optional

from ..models.todo import TodoItem, TodoRecurrence, TodoRecurrenceMode
from .todo_reminder import TodoReminderOptions


def is_recurring(item: TodoItem) -> bool:
    return (
        item.dueDate is not None
        and item.recurrence is not None
        and TodoRecurrenceMode.normalize(item.recurrence.mode) != TodoRecurrenceMode.NONE
    )


def create_recurrence(mode: Optional[str], due_date: Optional[datetime]) -> Optional[TodoRecurrence]:
    if due_date is None:
        return None
    normalized = TodoRecurrenceMode.normalize(mode)
    if normalized == TodoRecurrenceMode.NONE:
        return None
    return TodoRecurrence(mode=normalized, anchorDueDate=due_date)


def localization_key(mode: Optional[str]) -> str:
    normalized = TodoRecurrenceMode.normalize(mode)
    return {
        TodoRecurrenceMode.DAILY: "Todo.Recurrence.Daily",
        TodoRecurrenceMode.WEEKLY: "Todo.Recurrence.Weekly",
        TodoRecurrenceMode.MONTHLY: "Todo.Recurrence.Monthly",
        TodoRecurrenceMode.WEEKDAYS: "Todo.Recurrence.Weekdays",
    }.get(normalized, "Todo.Recurrence.None")


def try_create_next_occurrence(item: TodoItem, completed_at: datetime) -> Optional[TodoItem]:
    next_due = try_get_next_due_date(item, completed_at)
    if next_due is None:
        return None

    series_id = normalize_series_id(item.recurrenceSeriesId) or uuid.uuid4().hex
    from ..models.todo import TodoAttachment, TodoStep

    return TodoItem(
        text=item.text,
        isCompleted=False,
        isImportant=item.isImportant,
        colorMarker=item.colorMarker,
        dueDate=next_due,
        recurrence=item.recurrence.clone() if item.recurrence else None,
        steps=[
            TodoStep(text=step.text, isCompleted=False, sortOrder=step.sortOrder)
            for step in sorted(item.steps, key=lambda s: s.sortOrder)
        ],
        notes=item.notes,
        attachments=[
            TodoAttachment(
                filePath=a.filePath,
                displayName=a.displayName,
                type=a.type,
                storageMode=a.storageMode,
                addedAt=completed_at,
            )
            for a in item.attachments
        ],
        reminderOffsetMinutes=TodoReminderOptions.normalize_offset_minutes(item.reminderOffsetMinutes),
        recurrenceSeriesId=series_id,
        sortOrder=item.sortOrder + 1,
        createdAt=completed_at,
        updatedAt=completed_at,
    )


def try_get_next_due_date(item: TodoItem, completed_at: datetime) -> Optional[datetime]:
    if not is_recurring(item) or item.dueDate is None:
        return None

    mode = TodoRecurrenceMode.normalize(item.recurrence.mode)
    anchor = item.recurrence.anchorDueDate or item.dueDate
    local_due = _to_local(item.dueDate)
    local_completed = _to_local(completed_at)

    if mode == TodoRecurrenceMode.DAILY:
        return _from_local(_advance_by_fixed_days(local_due, local_completed, 1))
    if mode == TodoRecurrenceMode.WEEKLY:
        return _from_local(_advance_by_fixed_days(local_due, local_completed, 7))
    if mode == TodoRecurrenceMode.MONTHLY:
        return _from_local(_advance_monthly(local_due, _to_local(anchor), local_completed))
    if mode == TodoRecurrenceMode.WEEKDAYS:
        return _from_local(_advance_weekdays(local_due, local_completed))
    return None


def should_remove_generated_occurrence(source: TodoItem, generated: TodoItem) -> bool:
    if not source.generatedNextItemId or source.generatedNextItemId != generated.id:
        return False
    if generated.isCompleted or generated.createdAt != generated.updatedAt:
        return False
    return (
        source.text == generated.text
        and source.isImportant == generated.isImportant
        and source.colorMarker == generated.colorMarker
        and TodoReminderOptions.normalize_offset_minutes(source.reminderOffsetMinutes)
        == TodoReminderOptions.normalize_offset_minutes(generated.reminderOffsetMinutes)
        and normalize_series_id(source.recurrenceSeriesId) == normalize_series_id(generated.recurrenceSeriesId)
        and _recurrence_equivalent(source.recurrence, generated.recurrence)
    )


def normalize_series_id(series_id: Optional[str]) -> Optional[str]:
    return series_id.strip() if series_id and series_id.strip() else None


# ---- internals -----------------------------------------------------------------


def _recurrence_equivalent(left: Optional[TodoRecurrence], right: Optional[TodoRecurrence]) -> bool:
    left_mode = TodoRecurrenceMode.normalize(left.mode if left else None)
    right_mode = TodoRecurrenceMode.normalize(right.mode if right else None)
    if left_mode != right_mode:
        return False
    if left_mode == TodoRecurrenceMode.NONE:
        return True
    return (left.anchorDueDate if left else None) == (right.anchorDueDate if right else None)


def _to_local(value: datetime) -> datetime:
    return value.astimezone() if value.tzinfo is not None else value.astimezone()


def _from_local(value: datetime) -> datetime:
    return value  # stays timezone-aware in the local zone (DateTimeOffset local)


def _add_local_days(value: datetime, days: int) -> datetime:
    # Wall-clock addition, like DateTime.AddDays on a Local DateTime.
    return (value.replace(tzinfo=None) + timedelta(days=days)).replace(tzinfo=value.tzinfo)


def _advance_by_fixed_days(current_due: datetime, completed_at: datetime, days: int) -> datetime:
    candidate = _add_local_days(current_due, days)
    while candidate <= completed_at:
        candidate = _add_local_days(candidate, days)
    return candidate


def _advance_weekdays(current_due: datetime, completed_at: datetime) -> datetime:
    candidate = current_due
    while True:
        candidate = _add_local_days(candidate, 1)
        if candidate > completed_at and candidate.weekday() < 5:
            return candidate


def _advance_monthly(current_due: datetime, anchor_due: datetime, completed_at: datetime) -> datetime:
    month_offset = _months_between(anchor_due, current_due) + 1
    candidate = _monthly_candidate(anchor_due, month_offset)
    while candidate <= completed_at:
        month_offset += 1
        candidate = _monthly_candidate(anchor_due, month_offset)
    return candidate


def _months_between(anchor: datetime, target: datetime) -> int:
    return (target.year - anchor.year) * 12 + (target.month - anchor.month)


def _monthly_candidate(anchor: datetime, month_offset: int) -> datetime:
    target_year = anchor.year + (anchor.month - 1 + month_offset) // 12
    target_month = (anchor.month - 1 + month_offset) % 12 + 1
    target_day = min(anchor.day, calendar.monthrange(target_year, target_month)[1])
    return anchor.replace(year=target_year, month=target_month, day=target_day)
