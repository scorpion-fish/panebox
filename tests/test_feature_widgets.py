"""Feature-widget descriptor registry + Todo filter policy (C#
FeatureWidgetSettings / TodoItemFilterPolicy)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from panebox.models.app_settings import AppSettings
from panebox.models.layout_slice import WidgetLayoutSettingsSlice
from panebox.models.todo import TodoItem
from panebox.services import feature_widgets


@pytest.fixture()
def settings():
    return AppSettings()


@pytest.fixture()
def layout():
    return WidgetLayoutSettingsSlice()


# ---- descriptors ------------------------------------------------------------


def test_descriptor_table_matches_csharp_kinds_order_and_sizes():
    kinds = [d["kind"] for d in feature_widgets.FEATURE_DESCRIPTORS]
    assert kinds == ["QuickCapture", "Todo", "Music", "Weather", "Search", "Glance"]
    by_kind = {d["kind"]: d for d in feature_widgets.FEATURE_DESCRIPTORS}
    assert (by_kind["QuickCapture"]["width"], by_kind["QuickCapture"]["height"]) == (320, 440)
    assert (by_kind["Todo"]["width"], by_kind["Todo"]["height"]) == (360, 520)
    assert (by_kind["Music"]["width"], by_kind["Music"]["height"]) == (320, 210)
    assert (by_kind["Weather"]["width"], by_kind["Weather"]["height"]) == (320, 420)
    assert (by_kind["Search"]["width"], by_kind["Search"]["height"]) == (480, 380)
    assert (by_kind["Glance"]["width"], by_kind["Glance"]["height"]) == (420, 300)
    assert feature_widgets.descriptor_for("Todo")["title_key"] == "Todo.Title"
    assert feature_widgets.descriptor_for("File") is None
    assert feature_widgets.is_feature_widget("Glance")
    assert not feature_widgets.is_feature_widget("File")


def test_enabled_states_default_off_and_set_enabled_only_for_feature_kinds(layout):
    assert feature_widgets.is_enabled(layout, "Todo") is False
    feature_widgets.set_enabled(layout, "Todo", True)
    assert feature_widgets.is_enabled(layout, "Todo") is True
    feature_widgets.set_enabled(layout, "Todo", False)
    assert feature_widgets.is_enabled(layout, "Todo") is False
    feature_widgets.set_enabled(layout, "File", True)  # no-op, not a feature kind
    assert "File" not in layout.featureWidgetEnabledStates


# ---- visible filters ------------------------------------------------------------


def test_visible_filters_follow_tab_flags(settings):
    assert feature_widgets.visible_filters(settings) == ["All", "Today", "Important", "Completed"]
    todo = settings.todo
    todo.todoShowActiveTab = True
    todo.todoShowThisWeekTab = True
    todo.todoShowThisMonthTab = True
    assert feature_widgets.visible_filters(settings) == [
        "All",
        "Active",
        "Today",
        "ThisWeek",
        "ThisMonth",
        "Important",
        "Completed",
    ]


def test_visible_filters_never_empty(settings):
    for flag in (
        "todoShowAllTab",
        "todoShowActiveTab",
        "todoShowTodayTab",
        "todoShowThisWeekTab",
        "todoShowThisMonthTab",
        "todoShowImportantTab",
        "todoShowCompletedTab",
    ):
        setattr(settings.todo, flag, False)
    assert feature_widgets.visible_filters(settings) == ["All"]


# ---- filter matching ------------------------------------------------------------

TODAY = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)  # a Thursday


def _due(days_from_today: int) -> datetime:
    return TODAY + timedelta(days=days_from_today)


def test_all_filter_shows_incomplete_and_hides_completed_by_default(settings):
    settings.todo.todoShowCompletedTasks = False
    plain = TodoItem(text="plain")
    done = TodoItem(text="done", isCompleted=True)
    assert feature_widgets.matches_filter(plain, "All", settings, TODAY)
    assert not feature_widgets.matches_filter(done, "All", settings, TODAY)
    # hidden-completed rule exempts the Completed tab
    assert feature_widgets.matches_filter(done, "Completed", settings, TODAY)


def test_show_completed_tasks_setting_unhides(settings):
    settings.todo.todoShowCompletedTasks = True
    done = TodoItem(text="done", isCompleted=True)
    assert feature_widgets.matches_filter(done, "All", settings, TODAY)
    assert feature_widgets.matches_filter(done, "Active", settings, TODAY) is False


def test_active_today_week_month_buckets(settings):
    plain = TodoItem(text="plain")
    today = TodoItem(text="today", dueDate=_due(0))
    yesterday = TodoItem(text="yesterday", dueDate=_due(-1))
    next_week = TodoItem(text="next week", dueDate=_due(7))
    other_month = TodoItem(text="other month", dueDate=TODAY + timedelta(days=40))

    assert feature_widgets.matches_filter(plain, "Active", settings, TODAY)
    assert feature_widgets.matches_filter(today, "Active", settings, TODAY)

    assert feature_widgets.matches_filter(today, "Today", settings, TODAY)
    assert not feature_widgets.matches_filter(yesterday, "Today", settings, TODAY)
    assert not feature_widgets.matches_filter(plain, "Today", settings, TODAY)

    # Monday-start week: Thursday + 7 days lands in next week
    assert feature_widgets.matches_filter(today, "ThisWeek", settings, TODAY)
    assert feature_widgets.matches_filter(yesterday, "ThisWeek", settings, TODAY)
    assert not feature_widgets.matches_filter(next_week, "ThisWeek", settings, TODAY)

    assert feature_widgets.matches_filter(today, "ThisMonth", settings, TODAY)
    assert not feature_widgets.matches_filter(other_month, "ThisMonth", settings, TODAY)


def test_week_starts_monday(settings):
    # Saturday Sept 26 2026: its week runs Mon Sept 21 .. Sun Sept 27. Build
    # in the machine's local zone so .astimezone().date() doesn't shift days.
    tz = datetime.now().astimezone().tzinfo or timezone.utc
    saturday = datetime(2026, 9, 26, 9, 0, tzinfo=tz)
    monday = datetime(2026, 9, 21, 9, 0, tzinfo=tz)
    sunday = datetime(2026, 9, 27, 23, 0, tzinfo=tz)
    for day, expected in ((monday, True), (saturday, True), (sunday, True)):
        assert (
            feature_widgets.matches_filter(TodoItem(text="d", dueDate=day), "ThisWeek", settings, saturday) is expected
        )


def test_important_and_completed_filters(settings):
    star = TodoItem(text="star", isImportant=True)
    plain = TodoItem(text="plain")
    assert feature_widgets.matches_filter(star, "Important", settings, TODAY)
    assert not feature_widgets.matches_filter(plain, "Important", settings, TODAY)
    done = TodoItem(text="done", isCompleted=True)
    assert feature_widgets.matches_filter(done, "Completed", settings, TODAY)
    assert not feature_widgets.matches_filter(plain, "Completed", settings, TODAY)
