"""Glance widget surface (port of GlanceWidgetContent + GlanceWidgetViewModel).

One photo, the time, and an optional lunar calendar: background picture with
transparency + readability scrim under a foreground block laid out Immersive /
Centered / Editorial / Calendar, a minute-aligned clock, background rotation
(Bing / Wikimedia / local files / local folder), and Chinese lunar dates with
traditional festivals on the calendar grid.
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gdk, Gtk  # noqa: E402

from ..constants import DATA_ROOT
from ..i18n import t
from ..platform.resize_hook import ResizeHook
from ..models.glance import (
    GlanceBackgroundSource,
    GlanceImageFitMode,
    GlanceImageInfo,
    GlanceLayoutMode,
    GlanceOnlineImageCategory,
    GlanceReadabilityMode,
    GlanceTimeFormatMode,
    GlanceTraditionalCalendarMode,
    GlanceWidgetData,
)
from ..services import glance_calendar
from ..services.glance_image_service import GlanceImageService, is_online_source
from ..services.glance_store import GlanceWidgetStore
from ..services.glance_widget_settings_policy import (
    set_display_element,
    set_layout,
    set_local_image_files,
)

_ZH_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_ZH_MONTH_NAMES = (
    "",
    "一月",
    "二月",
    "三月",
    "四月",
    "五月",
    "六月",
    "七月",
    "八月",
    "九月",
    "十月",
    "十一月",
    "十二月",
)

ROTATION_INTERVAL_KEYS = [
    (0, "Glance.Rotation.Manual"),
    (10 / 60, "Glance.Rotation.10Seconds"),
    (30 / 60, "Glance.Rotation.30Seconds"),
    (1, "Glance.Rotation.60Seconds"),
    (2, "Glance.Rotation.2Minutes"),
    (5, "Glance.Rotation.5Minutes"),
    (10, "Glance.Rotation.10Minutes"),
    (30, "Glance.Rotation.30Minutes"),
    (60, "Glance.Rotation.1Hour"),
    (360, "Glance.Rotation.6Hours"),
    (1440, "Glance.Rotation.Daily"),
]

READABILITY_OPACITY = {
    GlanceReadabilityMode.NONE: 0.0,
    GlanceReadabilityMode.SOFT: 0.28,
    GlanceReadabilityMode.STRONG: 0.5,
}


# ---- pure formatting helpers (unit-tested) --------------------------------------


def _language() -> str:
    from .. import i18n

    return (i18n.current_language() or "zh-CN").split("-")[0].lower()


def _locale_is_12_hour() -> bool:
    try:
        return bool(datetime(2026, 1, 1, 13, 0).strftime("%p").strip())
    except Exception:
        return False


def format_time_text(value: datetime, mode: str) -> str:
    if mode == GlanceTimeFormatMode.HOUR24:
        return f"{value.hour:02d}:{value.minute:02d}"
    hour12 = value.hour % 12 or 12
    if mode == GlanceTimeFormatMode.HOUR12:
        return f"{hour12}:{value.minute:02d}"
    # FollowSystem: locale's short-time pattern with the AM/PM designator
    # stripped (the C# RemoveAmPmDesignator behavior).
    if _locale_is_12_hour():
        return f"{hour12}:{value.minute:02d}"
    return f"{value.hour:02d}:{value.minute:02d}"


def format_date_text(value: datetime, include_year: bool, language: str | None = None) -> str:
    language = language or _language()
    if language == "zh":
        if include_year:
            return f"{value.year}年{value.month}月{value.day}日"
        return f"{value.month}月{value.day}日"
    try:
        if include_year:
            return value.strftime("%B %d, %Y").replace(" 0", " ")
        return value.strftime("%B %d").replace(" 0", " ")
    except ValueError:
        return f"{value.month}/{value.day}/{value.year}" if include_year else f"{value.month}/{value.day}"


def format_weekday_text(value: datetime, language: str | None = None) -> str:
    language = language or _language()
    if language == "zh":
        return _ZH_WEEKDAYS[value.weekday()]
    from calendar import day_name

    return day_name[value.weekday()]


def format_compact_calendar_date_text(value: datetime, language: str | None = None) -> str:
    language = language or _language()
    if language in ("zh", "ja"):
        return f"{value.day}日"
    if language == "ko":
        return f"{value.day}일"
    return str(value.day)


def format_month_title(month: date, language: str | None = None) -> str:
    language = language or _language()
    if language == "zh":
        return f"{month.year}年{_ZH_MONTH_NAMES[month.month]}"
    return f"{month.year} · {month.strftime('%B')}"


class GlanceSurface(Gtk.Overlay):
    def __init__(
        self,
        config,
        settings_service,
        image_service: Optional[GlanceImageService] = None,
        application=None,
    ):
        super().__init__(visible=True)
        self.config = config
        self.settings_service = settings_service
        self.image_service = image_service or GlanceImageService(str(DATA_ROOT / "data" / "cache" / "glance"))
        self.application = application
        self.store = GlanceWidgetStore.for_widget(config.id, str(DATA_ROOT))
        self.settings = self.store.load()

        self.images: List[GlanceImageInfo] = []
        self.current_image: Optional[GlanceImageInfo] = None
        self._current_index = -1
        self._is_paused = False
        self._is_loading = False
        self._disposed = False
        self._online_refresh_done = False
        self._load_generation = 0
        self._available_width = 360.0
        self._available_height = 260.0
        self.displayed_month = date.today().replace(day=1)

        self._build()
        self.apply_settings()
        self.update_date_and_time()
        self._refresh_calendar()

        self.store.add_changed_listener(self._on_store_changed)
        self._resize_hook = ResizeHook(self, self._on_available_size_changed)
        self._start_image_load(refresh_online=False)
        self._update_clock_timer()
        self._update_rotation_timer()

        self.connect("destroy", lambda *_: self.dispose())

    # ---- structure -----------------------------------------------------------

    def _build(self) -> None:
        self.background_picture = Gtk.Picture(visible=True)
        self.background_picture.set_hexpand(True)
        self.background_picture.set_vexpand(True)
        self.background_picture.set_can_shrink(True)
        self.background_picture.set_keep_aspect_ratio(False)
        self.set_child(self.background_picture)

        self.scrim = Gtk.Box(visible=True)
        self.scrim.add_css_class("glance-scrim")
        self.add_overlay(self.scrim)

        self.foreground = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=True)
        self.foreground.set_hexpand(True)
        self.foreground.set_vexpand(True)
        self.foreground.add_css_class("glance-foreground")
        self.add_overlay(self.foreground)

        # time / date / weekday block (Immersive / Centered / Editorial)
        self.time_label = Gtk.Label(visible=True)
        self.time_label.add_css_class("glance-time")
        self.date_label = Gtk.Label(visible=True)
        self.date_label.add_css_class("glance-date")
        self.weekday_label = Gtk.Label(visible=True)
        self.weekday_label.add_css_class("glance-weekday")
        self.clock_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        self.clock_box.append(self.time_label)
        self.clock_box.append(self.date_label)
        self.clock_box.append(self.weekday_label)

        # calendar layout block
        self.calendar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=True)
        self.calendar_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        self.month_prev = Gtk.Button(label="‹", visible=True)
        self.month_prev.add_css_class("glance-nav")
        self.month_prev.connect("clicked", lambda *_: self._navigate_month(-1))
        self.month_title = Gtk.Label(xalign=0.5, visible=True)
        self.month_title.add_css_class("glance-month-title")
        self.month_title.set_hexpand(True)
        self.month_next = Gtk.Button(label="›", visible=True)
        self.month_next.add_css_class("glance-nav")
        self.month_next.connect("clicked", lambda *_: self._navigate_month(1))
        self.calendar_header.append(self.month_prev)
        self.calendar_header.append(self.month_title)
        self.calendar_header.append(self.month_next)
        self.traditional_title = Gtk.Label(xalign=0.5, visible=True)
        self.traditional_title.add_css_class("glance-traditional-title")
        self.calendar_grid_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.calendar_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        self.calendar_panel.add_css_class("glance-calendar-panel")
        self.calendar_panel.append(self.calendar_header)
        self.calendar_panel.append(self.traditional_title)
        self.calendar_panel.append(self.calendar_grid_holder)
        self.calendar_box.append(self.calendar_panel)

        self.stack = Gtk.Stack(visible=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.clock_box, "clock")
        self.stack.add_named(self.calendar_box, "calendar")
        self.foreground.append(self.stack)

        self.status_label = Gtk.Label(visible=True)
        self.status_label.add_css_class("glance-status")
        self.foreground.append(self.status_label)

        # hover photo controls
        self.controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        self.controls.add_css_class("glance-controls")
        self.controls.set_halign(Gtk.Align.END)
        self.controls.set_valign(Gtk.Align.END)
        self.pause_button = Gtk.Button(visible=True)
        self.pause_button.connect("clicked", lambda *_: self.toggle_pause())
        self.next_button = Gtk.Button(label="⏭", visible=True)
        self.next_button.set_tooltip_text(t("Glance.Actions.Next"))
        self.next_button.connect("clicked", lambda *_: self.next_image())
        self.info_button = Gtk.Button(label="ℹ", visible=True)
        self.info_button.set_tooltip_text(t("Glance.Actions.PhotoInfo"))
        self.info_button.connect("clicked", lambda *_: self.open_photo_info())
        self.controls.append(self.pause_button)
        self.controls.append(self.next_button)
        self.controls.append(self.info_button)
        self.add_overlay(self.controls)

        gear = Gtk.Button(label="⚙", visible=True)
        gear.add_css_class("glance-gear")
        gear.set_halign(Gtk.Align.END)
        gear.set_valign(Gtk.Align.START)
        gear.set_tooltip_text(t("Glance.Settings.Title"))
        gear.connect("clicked", lambda *_: self._show_settings_menu(gear))
        self.add_overlay(gear)
        self.gear_button = gear

        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_: self._set_controls_visible(True))
        hover.connect("leave", lambda *_: self._set_controls_visible(False))
        self.add_controller(hover)
        self._set_controls_visible(False)

        scroll = Gtk.EventControllerScroll()
        scroll.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.calendar_box.add_controller(scroll)

        self._font_provider = Gtk.CssProvider.new()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self._font_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self._font_class = f"glance-time-{id(self) & 0xFFFFFF:x}"
        self.time_label.add_css_class(self._font_class)

    def do_size_allocate(self, width, height, baseline):
        # vfunc overrides are dead in this PyGObject build; ResizeHook (wired
        # in __init__) drives _on_available_size_changed instead. Kept for
        # direct invocation in tests.
        Gtk.Overlay.do_size_allocate(self, width, height, baseline)
        self._on_available_size_changed(width, height)

    def _on_available_size_changed(self, width: float, height: float) -> None:
        if width <= 0 or height <= 0:
            return
        if abs(self._available_width - width) < 0.01 and abs(self._available_height - height) < 0.01:
            return
        self._available_width = width
        self._available_height = height
        self._apply_time_font()
        self._apply_layout()
        self._refresh_calendar()  # day-height gate depends on the new size

    # ---- settings application ---------------------------------------------------

    def apply_settings(self) -> None:
        settings = self.settings
        show_calendar = settings.showCalendar and self._available_width >= 300 and self._available_height >= 280
        self.time_label.set_visible(settings.showTime)
        self.date_label.set_visible(settings.showDate)
        self.weekday_label.set_visible(settings.showWeekday)
        self.stack.set_visible_child_name("calendar" if show_calendar else "clock")
        self._apply_layout()
        self._apply_time_font()
        self._apply_background_appearance()
        self._refresh_calendar()
        self._update_pause_button()
        self._set_controls_visible(False)

    def _apply_settings(self) -> None:
        self.settings = self.store.load()
        self.apply_settings()

    def _apply_layout(self) -> None:
        settings = self.settings
        show_calendar = settings.showCalendar and self._available_width >= 300 and self._available_height >= 280
        layout = settings.layout
        if layout == GlanceLayoutMode.CALENDAR and not show_calendar:
            layout = GlanceLayoutMode.IMMERSIVE

        clock = self.clock_box
        if layout == GlanceLayoutMode.CALENDAR:
            self.foreground.set_valign(Gtk.Align.END)
            clock.set_halign(Gtk.Align.CENTER)
        elif layout == GlanceLayoutMode.EDITORIAL:
            self.foreground.set_valign(Gtk.Align.END)
            clock.set_halign(Gtk.Align.START)
            self.foreground.add_css_class("glance-editorial")
        else:
            self.foreground.set_valign(Gtk.Align.CENTER if layout == GlanceLayoutMode.CENTERED else Gtk.Align.CENTER)
            clock.set_halign(Gtk.Align.CENTER)
        if layout != GlanceLayoutMode.EDITORIAL:
            self.foreground.remove_css_class("glance-editorial")

    def _apply_time_font(self) -> None:
        settings = self.settings
        show_calendar = settings.showCalendar and self._available_width >= 300 and self._available_height >= 280
        if show_calendar and settings.layout == GlanceLayoutMode.CALENDAR:
            base = min(self._available_width * 0.078, self._available_height * 0.095)
            size = min(max(base, 22), 28)
        elif settings.layout == GlanceLayoutMode.IMMERSIVE:
            size = min(max(min(self._available_width * 0.22, self._available_height * 0.42), 44), 96)
        else:
            size = min(max(min(self._available_width * 0.18, self._available_height * 0.28), 38), 78)
        size = round(size * settings.timeScale * 2) / 2
        family = (settings.timeFontFamily or "").strip()
        family_css = f"font-family: '{family}';" if family else ""
        self._font_provider.load_from_data(
            f".{self._font_class} {{ font-size: {size:.0f}px; {family_css} }}".encode("utf-8")
        )

    def _apply_background_appearance(self) -> None:
        settings = self.settings
        opacity = 1.0 - min(max(settings.backgroundImageTransparency, 0.0), 1.0)
        self.background_picture.set_opacity(opacity)
        self.background_picture.set_keep_aspect_ratio(settings.imageFit == GlanceImageFitMode.FIT)
        focus = settings.imageFocus
        self.background_picture.set_halign(Gtk.Align.CENTER)
        self.background_picture.set_valign(Gtk.Align.CENTER)
        if settings.imageFit == GlanceImageFitMode.FILL:
            # Fill crops to the widget; focus nudges the crop region.
            self.background_picture.set_halign(
                {
                    "Left": Gtk.Align.START,
                    "Right": Gtk.Align.END,
                }.get(focus, Gtk.Align.CENTER)
            )
            self.background_picture.set_valign(
                {
                    "Top": Gtk.Align.START,
                    "Bottom": Gtk.Align.END,
                }.get(focus, Gtk.Align.CENTER)
            )
        scrim_opacity = READABILITY_OPACITY.get(settings.readability, 0.0)
        foreground_visible = (
            self.settings.showTime or self.settings.showDate or self.settings.showWeekday or self.settings.showCalendar
        )
        self.scrim.set_opacity(scrim_opacity if foreground_visible else 0.0)
        self.status_label.set_visible(bool(self.status_label.get_text()))

    # ---- clock ----------------------------------------------------------------

    def update_date_and_time(self) -> None:
        now = datetime.now()
        self.time_label.set_text(format_time_text(now, self.settings.timeFormat))
        self.date_label.set_text(format_date_text(now, self.settings.showYear))
        self.weekday_label.set_text(format_weekday_text(now))

    def _update_clock_timer(self) -> None:
        self._clear_timer("_clock_source")
        needs_calendar_clock = (
            self.settings.showDate
            or self.settings.showWeekday
            or self.settings.showCalendar
            or self.settings.traditionalCalendarMode != GlanceTraditionalCalendarMode.NONE
        )
        if not (self.settings.showTime or needs_calendar_clock):
            return
        now = datetime.now()
        if self.settings.showTime:
            delay = max(1.0, 60 - now.second - now.microsecond / 1_000_000)
        else:
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            delay = max(1.0, (tomorrow - now).total_seconds() + 0.1)
        self._clock_source = GLib.timeout_add(int(delay * 1000), self._on_clock_tick)

    def _on_clock_tick(self) -> bool:
        previous_date = (datetime.now() - timedelta(minutes=1)).date()
        current_date = datetime.now().date()
        self.update_date_and_time()
        if previous_date != current_date:
            previous_month = previous_date.replace(day=1)
            current_month = current_date.replace(day=1)
            if self.displayed_month == previous_month and previous_month != current_month:
                self.displayed_month = current_month
            self._refresh_calendar()
        self._update_clock_timer()
        return False  # one-shot; _update_clock_timer re-arms

    # ---- calendar ----------------------------------------------------------------

    def _refresh_calendar(self) -> None:
        settings = self.settings
        today = date.today()
        month = glance_calendar.build_month(
            self.displayed_month,
            today,
            first_day_of_week=glance_calendar.locale_first_weekday(),
            culture_name=None,
        )
        glance_calendar.decorate_month(
            month,
            settings.traditionalCalendarMode,
            _language(),
            settings.showChineseFestivals,
            today,
        )
        self.month_title.set_text(format_month_title(month.month))
        self.traditional_title.set_text(month.traditionalTitle)
        self.traditional_title.set_visible(bool(month.traditionalTitle.strip()))

        for child in list(self.calendar_grid_holder):
            self.calendar_grid_holder.remove(child)
        grid = Gtk.Grid(visible=True)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(False)
        grid.set_column_spacing(2)
        grid.set_row_spacing(2)
        for column, header in enumerate(month.weekdayHeaders):
            label = Gtk.Label(label=header, visible=True)
            label.add_css_class("glance-weekday-header")
            grid.attach(label, column, 0, 1, 1)
        compact = glance_calendar.is_compact(self._available_height)
        panel_height = glance_calendar.calculate_panel_height(self._available_height, compact)
        day_height = glance_calendar.calculate_day_height(panel_height, compact)
        show_details = glance_calendar.should_show_traditional_details(
            self._available_width, day_height, compact, bool(month.traditionalTitle.strip())
        )
        for index, day in enumerate(month.days):
            row, column = divmod(index, 7)
            grid.attach(self._build_day_cell(day, show_details), column, row + 1, 1, 1)
        self.calendar_grid_holder.append(grid)

    def _build_day_cell(self, day, show_details: bool) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, visible=True)
        box.add_css_class("glance-day-cell")
        if not day.isCurrentMonth:
            box.add_css_class("glance-day-dim")
        if day.isToday:
            box.add_css_class("glance-day-today")
        number = Gtk.Label(label=day.dayText, visible=True)
        number.add_css_class("glance-day-number")
        box.append(number)
        # C# ApplyCalendarDayDecoration: the details gate hides ALL secondary
        # text (festivals included), and a festival replaces the day name.
        secondary = (day.festivalText or day.traditionalText or "") if show_details else ""
        if secondary:
            sub = Gtk.Label(label=secondary, visible=True)
            sub.add_css_class("glance-day-secondary")
            if day.festivalText:
                sub.add_css_class("glance-day-festival")
            box.append(sub)
        return box

    def _navigate_month(self, delta: int) -> None:
        target = glance_calendar.resolve_wheel_target(self.displayed_month, delta, date(1900, 1, 1), date(2100, 12, 1))
        if target == self.displayed_month:
            return
        self.displayed_month = target
        self._refresh_calendar()

    def _on_scroll(self, _controller, dx: float, dy: float) -> bool:
        self._navigate_month(1 if dy > 0 else -1)
        return True

    # ---- images ------------------------------------------------------------------

    def _start_image_load(self, refresh_online: bool) -> None:
        if self._disposed:
            return
        generation = self._load_generation = self._load_generation + 1
        if is_online_source(self.settings.backgroundSource) and refresh_online and not self._online_refresh_done:
            self._is_loading = True
        elif is_online_source(self.settings.backgroundSource) and not self.images:
            self._is_loading = True
            refresh_online = True

        did_attempt_refresh = refresh_online and is_online_source(self.settings.backgroundSource)

        def worker() -> None:
            try:
                if did_attempt_refresh:
                    provider = "Bing" if self.settings.backgroundSource == GlanceBackgroundSource.BING else "Wikimedia"
                    category = (
                        GlanceOnlineImageCategory.FEATURED
                        if self.settings.backgroundSource == GlanceBackgroundSource.BING
                        else self.settings.onlineImageCategory
                    )
                    images = self.image_service.refresh_online_images(provider, category)
                else:
                    images = self.image_service.get_available_images(self.settings)
            except Exception:
                images = []
            GLib.idle_add(self._apply_images, images, generation, did_attempt_refresh)

        threading.Thread(target=worker, daemon=True, name=f"glance-{self.config.id}").start()

    def _apply_images(self, images: List[GlanceImageInfo], generation: int, did_attempt_refresh: bool = False) -> bool:
        if self._disposed or generation != self._load_generation:
            return False
        self._is_loading = False
        if did_attempt_refresh and images:
            self._online_refresh_done = True
        images = [image for image in images if os.path.isfile(image.localPath)]
        self.images = images
        if not images:
            self.current_image = None
            self._current_index = -1
            self.status_label.set_text(
                t(
                    "Glance.Status.OnlineFallback"
                    if is_online_source(self.settings.backgroundSource)
                    else "Glance.Status.NoLocalImages"
                )
            )
            self.status_label.set_visible(True)
            self._apply_background()
            self._update_rotation_timer()
            return False

        previous_id = self.current_image.id if self.current_image else None
        index = next((i for i, image in enumerate(images) if image.id == previous_id), -1)
        if index < 0:
            import random

            index = random.randrange(len(images)) if self.settings.randomOrder else 0
        self._current_index = index
        self.current_image = images[index]
        self.status_label.set_text("")
        self.status_label.set_visible(False)
        self._apply_background()
        self._update_rotation_timer()
        return False

    def _apply_background(self) -> None:
        image = self.current_image
        if image is None or not os.path.isfile(image.localPath):
            self.background_picture.set_paintable(None)
            return
        try:
            texture = Gdk.Texture.new_from_filename(image.localPath)
        except Exception:
            texture = None
        self.background_picture.set_paintable(texture)

    def next_image(self) -> None:
        self._advance_image(reset_rotation_timer=True)

    def _advance_image(self, reset_rotation_timer: bool) -> None:
        if not self.images:
            if is_online_source(self.settings.backgroundSource):
                self._online_refresh_done = False
                self._start_image_load(refresh_online=True)
            return
        if len(self.images) == 1:
            next_index = 0
        elif self.settings.randomOrder:
            import random

            next_index = random.randrange(len(self.images))
            guard = 0
            while next_index == self._current_index and len(self.images) > 1 and guard < 16:
                next_index = random.randrange(len(self.images))
                guard += 1
        else:
            next_index = (self._current_index + 1) % len(self.images)
        self._current_index = next_index
        self.current_image = self.images[next_index]
        self._apply_background()
        if reset_rotation_timer:
            self._update_rotation_timer()

    def toggle_pause(self) -> None:
        self._is_paused = not self._is_paused
        self._update_pause_button()
        self._update_rotation_timer()

    def _update_pause_button(self) -> None:
        can_pause = self.settings.rotationIntervalMinutes > 0 and len(self.images) > 1
        self.pause_button.set_visible(can_pause)
        self.pause_button.set_label("▶" if self._is_paused else "⏸")
        self.pause_button.set_tooltip_text(t("Glance.Actions.Resume" if self._is_paused else "Glance.Actions.Pause"))

    def open_photo_info(self) -> None:
        import subprocess

        image = self.current_image
        if image is None:
            return
        url = (image.sourcePageUrl or "").strip()
        if url:
            try:
                subprocess.Popen(["xdg-open", url])
            except OSError:
                pass
            return
        if image.localPath and os.path.isfile(image.localPath):
            try:
                subprocess.Popen(["xdg-open", os.path.dirname(image.localPath)])
            except OSError:
                pass

    def _update_rotation_timer(self) -> None:
        self._clear_timer("_rotation_source")
        allow_rotation = True
        performance = getattr(self.settings_service.settings, "performance", None) if self.settings_service else None
        if performance is not None:
            allow_rotation = getattr(performance, "enableGlanceImageAutoRotation", True)
        if (
            self._is_paused
            or self._disposed
            or not allow_rotation
            or self.settings.rotationIntervalMinutes <= 0
            or len(self.images) < 2
        ):
            return
        interval_ms = int(self.settings.rotationIntervalMinutes * 60 * 1000)
        self._rotation_source = GLib.timeout_add(interval_ms, self._on_rotation_tick)

    def _on_rotation_tick(self) -> bool:
        self._advance_image(reset_rotation_timer=False)
        return GLib.SOURCE_CONTINUE

    # ---- settings popover -----------------------------------------------------------

    def _show_settings_menu(self, anchor: Gtk.Widget) -> None:
        from .glance_settings_menu import build_glance_menu

        popover = build_glance_menu(self)
        popover.set_pointing_to(Gdk.Rectangle())
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_has_arrow(False)
        popover.present(anchor)

    def set_display_element(self, element: str, visible: bool) -> None:
        self.store.update(lambda settings: set_display_element(settings, element, visible))

    def set_layout(self, layout: str) -> None:
        self.store.update(lambda settings: set_layout(settings, layout))

    def choose_local_files(self, paths: List[str]) -> None:
        self.store.update(lambda settings: set_local_image_files(settings, paths))

    def set_background_source(self, source: str) -> None:
        def update(settings: GlanceWidgetData) -> None:
            settings.backgroundSource = source
            if source == GlanceBackgroundSource.BING:
                settings.onlineImageCategory = GlanceOnlineImageCategory.FEATURED

        self.store.update(update)

    def choose_local_folder(self, path: Optional[str]) -> None:
        def update(settings: GlanceWidgetData) -> None:
            settings.backgroundSource = GlanceBackgroundSource.LOCAL_FOLDER
            settings.localFolderPath = path

        self.store.update(update)

    def set_rotation_interval(self, minutes: float) -> None:
        self.store.update(lambda settings: setattr(settings, "rotationIntervalMinutes", minutes))

    def set_traditional_calendar(self, mode: str) -> None:
        self.store.update(lambda settings: setattr(settings, "traditionalCalendarMode", mode))

    def set_chinese_festivals(self, enabled: bool) -> None:
        self.store.update(lambda settings: setattr(settings, "showChineseFestivals", enabled))

    def set_readability(self, mode: str) -> None:
        self.store.update(lambda settings: setattr(settings, "readability", mode))

    def set_background_transparency(self, value: float) -> None:
        self.store.update(lambda settings: setattr(settings, "backgroundImageTransparency", min(max(value, 0.0), 1.0)))

    # ---- lifecycle -------------------------------------------------------------

    def _on_store_changed(self) -> None:
        if self._disposed:
            return
        previous_source = self.settings.backgroundSource
        previous_category = self.settings.onlineImageCategory
        self.settings = self.store.load()
        self.apply_settings()
        self.update_date_and_time()
        if previous_source != self.settings.backgroundSource or previous_category != self.settings.onlineImageCategory:
            self._online_refresh_done = False
            self.images = []
            self.current_image = None
        self._start_image_load(refresh_online=False)
        self._update_clock_timer()
        self._update_rotation_timer()

    def _clear_timer(self, attribute: str) -> None:
        source = getattr(self, attribute, None)
        if source is not None:
            GLib.source_remove(source)
            setattr(self, attribute, None)

    def _set_controls_visible(self, visible: bool) -> None:
        self.controls.set_visible(visible and self.settings.showPhotoControls and self.current_image is not None)

    def queue_rebuild(self) -> None:  # appearance/language refresh hook
        self.apply_settings()
        self.update_date_and_time()
        self._refresh_calendar()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._clear_timer("_clock_source")
        self._clear_timer("_rotation_source")
        self.store.remove_changed_listener(self._on_store_changed)
        GlanceWidgetStore.release_widget(self.config.id)
