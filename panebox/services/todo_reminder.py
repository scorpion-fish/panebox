"""Todo reminder scanner (port of Services/TodoReminderService.cs).

30s scan loop, 8s startup delay, 1-minute missed-reminder grace. Pure
decision logic is module-level so tests drive it without GLib; the app
installs the timer and the notify hook (libnotify with actions).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from ..i18n import fmt, t
from ..models.todo import TodoItem, TodoReminderOptions

SCAN_INTERVAL_SECONDS = 30
STARTUP_DELAY_SECONDS = 8
MISSED_REMINDER_GRACE = timedelta(minutes=1)


@dataclass
class TodoReminderNotification:
    title: str
    message: str
    count: int
    widget_id: Optional[str] = None
    item_id: Optional[str] = None
    has_today_due_item: bool = False


def normalize_default_offset_minutes(minutes: int) -> int:
    return TodoReminderOptions.normalize_offset_minutes(minutes) if minutes is not None else 5


def try_get_reminder_trigger(
    item: TodoItem,
    now: datetime,
    default_offset_minutes: int,
) -> Optional[tuple[str, Optional[int]]]:
    """Returns ("due"|"snooze", effective_offset) when the item should fire."""
    if item.isCompleted or item.dueDate is None:
        return None
    if TodoReminderOptions.is_reminder_off(item.reminderOffsetMinutes):
        return None

    if item.snoozedUntil is not None:
        return ("snooze", None) if now >= item.snoozedUntil else None

    effective = TodoReminderOptions.normalize_offset_minutes(
        item.reminderOffsetMinutes
    ) or normalize_default_offset_minutes(default_offset_minutes)
    if effective is None or TodoReminderOptions.is_reminder_off(effective):
        return None
    if _should_notify_due(item, now, timedelta(minutes=max(0, effective))):
        return ("due", effective)
    return None


def _should_notify_due(item: TodoItem, now: datetime, reminder_offset: timedelta) -> bool:
    if item.isCompleted or item.dueDate is None:
        return False
    if item.reminderDismissedForDueDate is not None and item.reminderDismissedForDueDate == item.dueDate:
        return False
    reminder_at = item.dueDate - reminder_offset
    if now < reminder_at:
        return False
    return now <= item.dueDate + MISSED_REMINDER_GRACE


def should_notify(item: TodoItem, now: datetime, default_offset_minutes: int) -> bool:
    return try_get_reminder_trigger(item, now, default_offset_minutes) is not None


class TodoReminderService:
    """Timer-driven scanner. `store_factory(widget_id) -> TodoWidgetStore`."""

    def __init__(
        self,
        settings_service,
        store_factory: Callable[[str], object],
        notify: Callable[[TodoReminderNotification], None],
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        log: Callable[[str], None] = lambda _m: None,
    ):
        self.settings = settings_service
        self.store_factory = store_factory
        self.notify = notify
        self.clock = clock
        self.log = log
        self._session_notified_keys: set[str] = set()
        self._timer_id = 0
        self._is_checking = False
        self._scheduler = None

    # ---- lifecycle ----------------------------------------------------------

    def start(self, scheduler) -> None:
        self._scheduler = scheduler
        if not self.should_be_running():
            return
        self._session_notified_keys.clear()
        if not self._timer_id:
            self._timer_id = scheduler(SCAN_INTERVAL_SECONDS * 1000, self._on_tick)
        scheduler(STARTUP_DELAY_SECONDS * 1000, lambda: (self.check_now(), False)[1])

    def stop(self) -> None:
        self._timer_id = 0
        self._session_notified_keys.clear()

    def refresh(self, scheduler=None) -> None:
        if scheduler is not None:
            self._scheduler = scheduler
        if self.should_be_running():
            if not self._timer_id and self._scheduler is not None:
                self._session_notified_keys.clear()
                self._timer_id = self._scheduler(SCAN_INTERVAL_SECONDS * 1000, self._on_tick)
        elif self._timer_id:
            self._timer_id = 0
            self._session_notified_keys.clear()

    def should_be_running(self) -> bool:
        settings = self.settings.settings
        feature_states = self.settings.layout.featureWidgetEnabledStates or {}
        return settings.todo.todoReminderEnabled and feature_states.get("Todo", True)

    def _on_tick(self) -> bool:
        self.check_now()
        return True  # GLib repeating source

    # ---- scan ----------------------------------------------------------------

    def check_now(self) -> int:
        if self._is_checking:
            return 0
        self._is_checking = True
        try:
            settings = self.settings.settings
            if not settings.todo.todoReminderEnabled:
                return 0
            default_offset = normalize_default_offset_minutes(settings.todo.todoDefaultReminderOffsetMinutes)
            widgets = [
                w
                for w in self.settings.layout.widgets
                if w.widgetKind == "Todo"
                and not w.isDisabled
                and w.id not in (self.settings.layout.deletedWidgetIds or [])
            ]
            candidates: List[tuple[str, str, TodoItem]] = []
            for widget in widgets:
                self._collect_widget_candidates(widget, self.clock(), default_offset, candidates)
            if not candidates:
                return 0
            self.notify(_build_notification(candidates, self.clock()))
            return len(candidates)
        except Exception as exc:  # scan must never kill the timer
            self.log(f"[TodoReminder] check failed: {exc}")
            return 0
        finally:
            self._is_checking = False

    def _collect_widget_candidates(self, widget, now, default_offset, candidates):
        store = self.store_factory(widget.id)
        data = store.load()
        changed = False
        for item in data.items:
            trigger = try_get_reminder_trigger(item, now, default_offset)
            if trigger is None:
                continue
            kind, effective = trigger
            key = _reminder_key(widget.id, item, kind, effective)
            if key in self._session_notified_keys:
                continue
            self._session_notified_keys.add(key)
            if kind == "snooze":
                item.snoozeLastNotifiedAt = now
                item.snoozedUntil = None
            else:
                item.reminderLastNotifiedAt = now
                item.reminderDismissedForDueDate = item.dueDate
            item.updatedAt = datetime.now(timezone.utc)
            changed = True
            candidates.append((widget.id, widget.name, item))
        if changed:
            store.save(data)

    # ---- notification actions ---------------------------------------------------

    def snooze(self, widget_id: Optional[str], item_id: Optional[str], snooze_for: timedelta) -> bool:
        if not widget_id or not item_id or snooze_for <= timedelta(0):
            return False
        return self.snooze_until(widget_id, item_id, self.clock() + snooze_for)

    def snooze_until(self, widget_id: Optional[str], item_id: Optional[str], snoozed_until: datetime) -> bool:
        if not widget_id or not item_id or snoozed_until <= self.clock():
            return False
        try:
            widget = self._find_widget(widget_id, require_reminder_enabled=True)
            if widget is None:
                return False
            store = self.store_factory(widget.id)
            data = store.load()
            item = next((i for i in data.items if i.id == item_id), None)
            if (
                item is None
                or item.isCompleted
                or item.dueDate is None
                or TodoReminderOptions.is_reminder_off(item.reminderOffsetMinutes)
            ):
                return False
            item.snoozedUntil = snoozed_until
            item.snoozeLastNotifiedAt = None
            item.reminderDismissedForDueDate = item.dueDate
            item.updatedAt = datetime.now(timezone.utc)
            store.save(data)
            self.log(f"[TodoReminder] snoozed widget={widget_id} item={item_id}")
            return True
        except Exception as exc:
            self.log(f"[TodoReminder] snooze failed: {exc}")
            return False

    def complete(self, widget_id: Optional[str], item_id: Optional[str]) -> bool:
        from . import todo_recurrence

        if not widget_id or not item_id:
            return False
        try:
            widget = self._find_widget(widget_id, require_reminder_enabled=False)
            if widget is None:
                return False
            store = self.store_factory(widget.id)
            data = store.load()
            index = next((i for i, item in enumerate(data.items) if item.id == item_id), None)
            if index is None:
                return False
            item = data.items[index]
            if item.isCompleted:
                return True

            now = datetime.now(timezone.utc)
            if item.recurrence is not None and not item.recurrenceSeriesId:
                item.recurrenceSeriesId = _new_id()
            item.isCompleted = True
            item.completedAt = now
            item.updatedAt = now
            item.generatedNextItemId = None
            item.snoozedUntil = None
            item.snoozeLastNotifiedAt = None

            next_item = todo_recurrence.try_create_next_occurrence(item, now)
            if next_item is not None:
                item.generatedNextItemId = next_item.id
                data.items.insert(min(index + 1, len(data.items)), next_item)
            store.save(data)
            self.log(f"[TodoReminder] completed from notification widget={widget_id} item={item_id}")
            return True
        except Exception as exc:
            self.log(f"[TodoReminder] complete failed: {exc}")
            return False

    def _find_widget(self, widget_id: str, require_reminder_enabled: bool):
        settings = self.settings.settings
        if require_reminder_enabled and not settings.todo.todoReminderEnabled:
            return None
        return next(
            (
                w
                for w in self.settings.layout.widgets
                if w.widgetKind == "Todo"
                and not w.isDisabled
                and w.id not in (self.settings.layout.deletedWidgetIds or [])
                and w.id == widget_id
            ),
            None,
        )


# ---- notification formatting -------------------------------------------------


def _build_notification(candidates, now: datetime) -> TodoReminderNotification:
    first = min(candidates, key=lambda c: c[2].dueDate or now)
    widget_id, _name, item = first
    title = t("Todo.Reminder.NotificationTitle")
    due_text = _format_due_date(item.dueDate, now)
    item_text = _normalize_notification_text(item.text)
    if len(candidates) == 1:
        message = fmt("Todo.Reminder.NotificationSingle", item_text, due_text)
    else:
        message = fmt("Todo.Reminder.NotificationMultiple", len(candidates), item_text, due_text)
    has_today = any(
        c[2].dueDate is not None and c[2].dueDate.astimezone().date() == now.astimezone().date() for c in candidates
    )
    return TodoReminderNotification(title, message, len(candidates), widget_id, item.id, has_today)


def _format_due_date(due_date: datetime, now: datetime) -> str:
    local_due = due_date.astimezone()
    today = now.astimezone().date()
    time = local_due.strftime("%H:%M") if local_due.second == 0 else local_due.strftime("%H:%M:%S")
    if local_due.date() == today:
        return fmt("Todo.Due.TodayAt", time)
    if local_due.date() == today + timedelta(days=1):
        return fmt("Todo.Due.TomorrowAt", time)
    return f"{local_due.year}/{local_due.month}/{local_due.day} {time}"


def _normalize_notification_text(text: Optional[str]) -> str:
    parts = [part.strip() for part in (text or "").replace("\r", "\n").replace("\t", "\n").split("\n")]
    normalized = " ".join(part for part in parts if part)
    if not normalized:
        return t("Todo.Reminder.Untitled")
    return normalized if len(normalized) <= 48 else normalized[:48] + "..."


def _reminder_key(widget_id: str, item: TodoItem, kind: str, effective: Optional[int]) -> str:
    due_key = item.dueDate.astimezone(timezone.utc).isoformat() if item.dueDate else "none"
    if kind == "snooze":
        trigger_key = item.snoozedUntil.astimezone(timezone.utc).isoformat() if item.snoozedUntil else "none"
    else:
        trigger_key = str(effective) if effective is not None else "default"
    return f"{widget_id}:{item.id}:{kind}:{due_key}:{trigger_key}"


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex
