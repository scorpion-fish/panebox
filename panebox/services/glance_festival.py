"""Chinese festival decorations for the Glance calendar (port of
Services/GlanceFestivalService.cs).

Festival rules, verbatim from the Windows build: 清明 by solar formula,
fixed lunar-date festivals (skipped in leap months), and 除夕 detected as
the day before a non-leap 正月初一.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..models.glance import (
    GlanceCalendarMonth,
    GlanceTraditionalCalendarMode,
)
from . import chinese_calendar
from .glance_traditional import resolve_mode

_FIXED_FESTIVALS = {
    (1, 1): "春节",
    (1, 15): "元宵",
    (5, 5): "端午",
    (7, 7): "七夕",
    (7, 15): "中元",
    (8, 15): "中秋",
    (9, 9): "重阳",
    (12, 8): "腊八",
}


def apply(
    month: GlanceCalendarMonth,
    show_chinese_festivals: bool,
    configured_mode: str,
    culture_name: str | None,
) -> GlanceCalendarMonth:
    mode = resolve_mode(configured_mode, culture_name)
    for day in month.days:
        day.festivalText = (
            get_chinese_festival(day.date)
            if (show_chinese_festivals and mode == GlanceTraditionalCalendarMode.CHINESE_LUNAR)
            else ""
        )
    return month


def get_chinese_festival(value: date) -> str:
    try:
        if value.month == 4 and value.day == chinese_calendar.qingming_day(value.year):
            return "清明"

        lunar = chinese_calendar.chinese_date(value)
        if not lunar.is_leap_month:
            fixed = _FIXED_FESTIVALS.get((lunar.month, lunar.day), "")
            if fixed:
                return fixed

        # Lunar December can contain 29 or 30 days. The day immediately
        # before a non-leap first day of the first month is always 除夕.
        following = chinese_calendar.chinese_date(value + timedelta(days=1))
        if not following.is_leap_month and following.month == 1 and following.day == 1:
            return "除夕"
        return ""
    except ValueError:
        return ""
