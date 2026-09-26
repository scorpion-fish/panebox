"""Calendar grid building + navigation + layout math for the Glance widget
(ports of LocalCalendarPresentationSource, GlanceCalendarNavigationResolver
and GlanceCalendarLayoutCalculator).
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Iterable, List

from ..i18n import current_language
from ..models.glance import GlanceCalendarDay, GlanceCalendarMonth
from . import glance_festival, glance_traditional
from .weather_view_model import WEEKDAY_ABBREVIATIONS

COMPACT_CALENDAR_THRESHOLD = 320
CALENDAR_PANEL_MAXIMUM_WIDTH = 360
CALENDAR_PANEL_HORIZONTAL_INSET = 28

_MONTH_DISTANCE_MAX = 12 * 200  # far enough for any navigation clamp


def locale_first_weekday() -> int:
    """0=Monday..6=Sunday (matches the C# CalendarView default; zh-CN and
    most locales start Monday, en-US users get Sunday via locale probing)."""
    try:
        import locale

        name = (locale.getlocale()[0] or "").lower()
        if name.startswith("en_"):
            return 6  # Sunday start for en-US style locales
    except Exception:
        pass
    return 0  # Monday


def weekday_headers(first_day_of_week: int, culture_name: str | None = None) -> List[str]:
    """Abbreviated day names for the app language — NOT the process locale
    (GTK's locale init would otherwise pin headers to the OS locale)."""
    language = culture_name or current_language() or "en"
    table = (
        WEEKDAY_ABBREVIATIONS.get(language)
        or WEEKDAY_ABBREVIATIONS.get(language.split("-")[0])
        or WEEKDAY_ABBREVIATIONS["en"]
    )
    return [table[(first_day_of_week + index) % 7] for index in range(7)]


def build_month(
    month: date,
    today: date,
    first_day_of_week: int = 1,
    culture_name: str | None = None,
) -> GlanceCalendarMonth:
    first = month.replace(day=1)
    leading = (first.weekday() - first_day_of_week + 7) % 7
    grid_start = first - timedelta(days=leading)
    headers = weekday_headers(first_day_of_week, culture_name)
    days: List[GlanceCalendarDay] = []
    for index in range(42):
        value = grid_start + timedelta(days=index)
        days.append(
            GlanceCalendarDay(
                date=value,
                dayText=str(value.day),
                isCurrentMonth=value.month == first.month and value.year == first.year,
                isToday=value == today,
            )
        )
    return GlanceCalendarMonth(month=first, weekdayHeaders=headers, days=days)


def decorate_month(
    month: GlanceCalendarMonth,
    configured_mode: str,
    culture_name: str | None,
    show_chinese_festivals: bool,
    today: date,
) -> GlanceCalendarMonth:
    traditional_title_date = today if month.month == today.replace(day=1) else month.month + timedelta(days=14)
    glance_traditional.apply(month, configured_mode, culture_name, traditional_title_date)
    glance_festival.apply(month, show_chinese_festivals, configured_mode, culture_name)
    return month


def resolve_wheel_target(current_month: date, wheel_delta: int, minimum_month: date, maximum_month: date) -> date:
    normalized_current = current_month.replace(day=1)
    normalized_minimum = minimum_month.replace(day=1)
    normalized_maximum = maximum_month.replace(day=1)
    if wheel_delta == 0:
        return normalized_current

    target = _add_months(normalized_current, -1 if wheel_delta > 0 else 1)
    if target < normalized_minimum:
        return normalized_minimum
    return normalized_maximum if target > normalized_maximum else target


def resolve_displayed_month(visible_dates: Iterable[date], fallback_month: date) -> date:
    normalized_fallback = fallback_month.replace(day=1)
    counts: dict[tuple[int, int], int] = {}
    for value in visible_dates:
        key = (value.year, value.month)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return normalized_fallback

    def distance(key: tuple[int, int]) -> int:
        return abs((key[0] - normalized_fallback.year) * 12 + key[1] - normalized_fallback.month)

    best = min(counts, key=lambda key: (-counts[key], distance(key)))
    return date(best[0], best[1], 1)


def is_compact(available_height: float) -> bool:
    return available_height < COMPACT_CALENDAR_THRESHOLD


def calculate_panel_height(available_height: float, compact: bool, has_traditional_calendar: bool = False) -> float:
    if compact:
        # Compact calendars keep the clock inside the material surface; give the
        # surface nearly all height so six week rows survive the compact header.
        return min(max(available_height - 40, 238), 268)
    # Above the breakpoint the clock sits outside the surface; grow with the
    # widget while retaining room for it.
    return min(max(available_height - 102, 244), 310)


def calculate_day_height(panel_height: float, compact: bool, has_traditional_calendar: bool = False) -> float:
    fixed_content_height = 104 if compact else 58
    return min(max((panel_height - fixed_content_height) / 6, 24), 42)


def should_show_traditional_details(
    panel_width: float,
    day_height: float,
    compact: bool,
    has_traditional_calendar: bool,
) -> bool:
    return has_traditional_calendar and not compact and panel_width >= 280 and day_height >= 28


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    year, month_index = divmod(total, 12)
    day = min(value.day, monthrange(year, month_index + 1)[1])
    return date(year, month_index + 1, day)
