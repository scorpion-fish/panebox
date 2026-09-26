"""Traditional calendar layer for the Glance month grid (port of
Services/GlanceTraditionalCalendarService.cs).

Presentation-only, independent from any calendar account/event provider.
Linux v1 scope: ChineseLunar (own table implementation, 1900-2100) plus
Auto resolution; the Windows build's system calendars (UmAlQura, Hijri,
JapaneseEra, ...) have no BCL equivalent here and render as no text —
documented in the README.
"""

from __future__ import annotations

from datetime import date

from ..models.glance import (
    GlanceCalendarMonth,
    GlanceTraditionalCalendarMode,
)
from . import chinese_calendar

_TRADITIONAL_FONT = "Noto Serif CJK SC, serif"


def _language(culture_name: str | None) -> str:
    if not culture_name:
        return ""
    for separator in ("-", "_"):
        index = culture_name.find(separator)
        if index > 0:
            return culture_name[:index].lower()
    return culture_name.lower()


def resolve_mode(configured_mode: str, culture_name: str | None) -> str:
    if configured_mode != GlanceTraditionalCalendarMode.AUTO:
        return configured_mode

    language = _language(culture_name)
    return {
        "zh": GlanceTraditionalCalendarMode.CHINESE_LUNAR,
    }.get(language, GlanceTraditionalCalendarMode.NONE)


def apply(
    month: GlanceCalendarMonth,
    configured_mode: str,
    culture_name: str | None,
    today: date,
) -> GlanceCalendarMonth:
    mode = resolve_mode(configured_mode, culture_name)
    if mode != GlanceTraditionalCalendarMode.CHINESE_LUNAR:
        for day in month.days:
            day.traditionalText = ""
        month.traditionalTitle = ""
        return month

    for day in month.days:
        day.traditionalText = format_day(day.date, mode)
    month.traditionalTitle = format_title(today, mode)
    return month


def format_title(value: date, mode: str) -> str:
    if mode != GlanceTraditionalCalendarMode.CHINESE_LUNAR:
        return ""
    try:
        lunar = chinese_calendar.chinese_date(value)
    except ValueError:
        return ""
    cyclical = f"{chinese_calendar.sexagenary_name(lunar.sexagenary_year)}年"
    month = ("闰" if lunar.is_leap_month else "") + chinese_calendar.MONTH_NAMES[lunar.month]
    return f"{cyclical} {month}{chinese_calendar.DAY_NAMES[lunar.day]}"


def format_day(value: date, mode: str) -> str:
    if mode != GlanceTraditionalCalendarMode.CHINESE_LUNAR:
        return ""
    try:
        lunar = chinese_calendar.chinese_date(value)
    except ValueError:
        return ""
    return chinese_calendar.lunar_month_day_name(lunar)


__all__ = ["apply", "format_day", "format_title", "resolve_mode"]
