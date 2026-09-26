"""Glance settings update policy (port of Services/GlanceWidgetSettingsPolicy.cs).

Pure mutations over GlanceWidgetData; the store normalizes afterwards.
"""

from __future__ import annotations

from typing import Iterable

from ..models.glance import (
    GlanceBackgroundSource,
    GlanceDisplayElement,
    GlanceLayoutMode,
    GlanceWidgetData,
)


def set_local_image_files(settings: GlanceWidgetData, image_paths: Iterable[str]) -> None:
    settings.backgroundSource = GlanceBackgroundSource.LOCAL_FILES
    seen: set[str] = set()
    paths = []
    for path in image_paths or []:
        cleaned = (path or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(cleaned)
    settings.localImagePaths = paths


def clear_local_source(settings: GlanceWidgetData) -> None:
    if settings.backgroundSource == GlanceBackgroundSource.LOCAL_FOLDER:
        settings.localFolderPath = None
    else:
        settings.localImagePaths = []


def is_display_element_visible(settings: GlanceWidgetData, element: str) -> bool:
    return {
        GlanceDisplayElement.TIME: settings.showTime,
        GlanceDisplayElement.DATE: settings.showDate,
        GlanceDisplayElement.YEAR: settings.showYear,
        GlanceDisplayElement.WEEKDAY: settings.showWeekday,
        GlanceDisplayElement.CALENDAR: settings.showCalendar,
    }.get(element, False)


def set_display_element(settings: GlanceWidgetData, element: str, is_visible: bool) -> None:
    if element == GlanceDisplayElement.TIME:
        settings.showTime = is_visible
    elif element == GlanceDisplayElement.DATE:
        settings.showDate = is_visible
        if not is_visible:
            settings.showYear = False
    elif element == GlanceDisplayElement.YEAR:
        settings.showYear = is_visible
        if is_visible:
            settings.showDate = True
    elif element == GlanceDisplayElement.WEEKDAY:
        settings.showWeekday = is_visible
    elif element == GlanceDisplayElement.CALENDAR:
        settings.showCalendar = is_visible
        if is_visible:
            settings.layout = GlanceLayoutMode.CALENDAR
        elif settings.layout == GlanceLayoutMode.CALENDAR:
            settings.layout = GlanceLayoutMode.CENTERED


def set_layout(settings: GlanceWidgetData, layout: str) -> None:
    settings.layout = layout
    settings.showCalendar = layout == GlanceLayoutMode.CALENDAR


def set_photo_playback(
    settings: GlanceWidgetData,
    rotation_interval_minutes: float,
    random_order: bool,
    transition: str,
    transition_speed: str,
    readability: str,
    show_photo_controls: bool,
) -> None:
    settings.rotationIntervalMinutes = rotation_interval_minutes
    settings.randomOrder = random_order
    settings.transition = transition
    settings.transitionSpeed = transition_speed
    settings.readability = readability
    settings.showPhotoControls = show_photo_controls
