"""Feature-widget registry + Todo list filtering (ports of
Services/FeatureWidgetSettings.cs + the Todo filter helpers).

The descriptor table mirrors WidgetContentFactory's: which kinds are
user-placeable feature widgets, their i18n keys, glyphs, and default sizes.
Enabled states persist in WidgetLayoutSettingsSlice.featureWidgetEnabledStates.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

# kind, title i18n key, glyph, default size — mirrors the C# descriptor table
FEATURE_DESCRIPTORS: tuple[dict, ...] = (
    {
        "kind": "QuickCapture",
        "title_key": "QuickCapture.Name",
        "glyph": "✶",
        "width": 320,
        "height": 440,
    },
    {"kind": "Todo", "title_key": "Todo.Title", "glyph": "☑", "width": 360, "height": 520},
    {"kind": "Music", "title_key": "Music.Title", "glyph": "♪", "width": 320, "height": 210},
    {"kind": "Weather", "title_key": "Weather.Title", "glyph": "☁", "width": 320, "height": 420},
    {"kind": "Search", "title_key": "Search.Title", "glyph": "⌕", "width": 480, "height": 380},
    {"kind": "Glance", "title_key": "Glance.Title", "glyph": "◔", "width": 420, "height": 300},
)

FEATURE_KINDS = tuple(d["kind"] for d in FEATURE_DESCRIPTORS)


def descriptor_for(kind: str) -> Optional[dict]:
    return next((d for d in FEATURE_DESCRIPTORS if d["kind"] == kind), None)


def is_feature_widget(kind: str) -> bool:
    return kind in FEATURE_KINDS


def is_enabled(layout, kind: str) -> bool:
    """featureWidgetEnabledStates gate (unset kinds default off, like the
    legacy slice bools the C# layer mirrors)."""
    states = layout.featureWidgetEnabledStates or {}
    return bool(states.get(kind, False))


def set_enabled(layout, kind: str, enabled: bool) -> None:
    if not is_feature_widget(kind):
        return
    layout.featureWidgetEnabledStates[kind] = enabled


# ---- Todo filters (TodoWidgetViewModel.FilteringAndAppearance) ----------------

TODO_FILTERS = ("All", "Active", "Today", "ThisWeek", "ThisMonth", "Important", "Completed")


def visible_filters(settings) -> list[str]:
    todo = settings.todo
    flags = {
        "All": todo.todoShowAllTab,
        "Active": todo.todoShowActiveTab,
        "Today": todo.todoShowTodayTab,
        "ThisWeek": todo.todoShowThisWeekTab,
        "ThisMonth": todo.todoShowThisMonthTab,
        "Important": todo.todoShowImportantTab,
        "Completed": todo.todoShowCompletedTab,
    }
    order = [f for f in TODO_FILTERS if flags.get(f)]
    return order or ["All"]


def matches_filter(item, selected: str, settings, now: Optional[datetime] = None) -> bool:
    """Port of TodoItemFilterPolicy: hidden completed + bucket rules."""
    if not settings.todo.todoShowCompletedTasks and item.isCompleted and selected != "Completed":
        return False
    if selected == "Active":
        return not item.isCompleted
    if selected == "Today":
        return _is_due_today(item, now)
    if selected == "ThisWeek":
        return _is_due_this_week(item, now)
    if selected == "ThisMonth":
        return _is_due_this_month(item, now)
    if selected == "Important":
        return item.isImportant
    if selected == "Completed":
        return item.isCompleted
    return True


def _local_due_date(item, now: Optional[datetime]):
    if item.dueDate is None:
        return None
    return item.dueDate.astimezone().date()


def _is_due_today(item, now: Optional[datetime] = None) -> bool:
    due = _local_due_date(item, now)
    return due is not None and due == (now or datetime.now().astimezone()).date()


def _is_due_this_week(item, now: Optional[datetime] = None) -> bool:
    due = _local_due_date(item, now)
    if due is None:
        return False
    today = (now or datetime.now().astimezone()).date()
    days_since_monday = today.weekday()  # Monday=0 in Python
    week_start = today - timedelta(days=days_since_monday)
    return week_start <= due < week_start + timedelta(days=7)


def _is_due_this_month(item, now: Optional[datetime] = None) -> bool:
    due = _local_due_date(item, now)
    if due is None:
        return False
    today = (now or datetime.now().astimezone()).date()
    return due.year == today.year and due.month == today.month
