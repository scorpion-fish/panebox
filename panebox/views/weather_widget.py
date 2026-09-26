"""Weather widget surface (port of WeatherWidgetContent.xaml + the stateful
half of WeatherWidgetViewModel).

Three responsive layouts — Mini / Compact / Expanded with hysteresis — a
Day/Week segmented switch persisted as per-widget metadata, Standard/Rich
skins, refresh with backoff + status toast, and location resolution
(auto via IP, manual coords, saved city, Beijing last resort).
All presentation computation lives in services.weather_view_model; this class
owns state, threading (worker → GLib.idle_add with a request version), and GTK.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:  # Pango is always present with PyGObject; keep import explicit for ellipsize
    from gi.repository import Pango  # noqa: E402
except ImportError:  # pragma: no cover
    Pango = None

from ..i18n import current_language, t
from ..models.weather import WeatherData
from ..platform.resize_hook import ResizeHook
from ..services import weather_backoff, weather_settings_policy as policy
from ..services.weather_location import get_location
from ..services.weather_service import WeatherService
from ..services.weather_view_model import (
    DEFAULT_TEXT_SIZE,
    MAX_TEXT_SIZE,
    MIN_TEXT_SIZE,
    WeatherDisplay,
    apply_weather_data,
    css_color,
    determine_layout_mode,
    fallback_location_name,
    normalize_text_size,
    refine_location_name,
    resolve_typography_scale,
)

MIN_SYSTEM_TEXT_SCALE = 1.0

# One shared background pool: location + forecast requests per widget.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wx-refresh")

TOAST_AUTOHIDE_MS = 2500


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WeatherSurface(Gtk.Overlay):
    def __init__(
        self,
        config,
        settings_service,
        weather_service: Optional[WeatherService] = None,
        application=None,
    ):
        super().__init__(visible=True)
        self.config = config
        self.settings_service = settings_service
        self.application = application

        def _data_source() -> str:
            return settings_service.settings.weather.weatherDataSource

        self.service = weather_service or WeatherService(data_source_getter=_data_source)

        # ── state (WeatherWidgetViewModel fields) ──
        self._weather_data: Optional[WeatherData] = None
        self._display: Optional[WeatherDisplay] = None
        self._has_data = False
        self._is_refreshing = False
        self._layout_mode = ""
        self._last_width = 0.0
        self._last_height = 0.0
        self._text_size = DEFAULT_TEXT_SIZE
        self._system_text_scale = MIN_SYSTEM_TEXT_SCALE
        self._is_week_view = False
        self._is_using_fallback_location = False

        self._latitude = 0.0
        self._longitude = 0.0
        self._location_name = ""
        self._location_initialized = False
        self._location_resolved_at = datetime.min.replace(tzinfo=timezone.utc)
        self._location_retry_not_before = datetime.min.replace(tzinfo=timezone.utc)

        self._automatic_refresh_not_before = datetime.min.replace(tzinfo=timezone.utc)
        self._consecutive_refresh_failures = 0

        self._request_version = 0
        self._refresh_pending = False
        self._pending_user_triggered = False
        self._pending_force_refresh = False
        self._refresh_was_user_triggered = False

        self._disposed = False
        self._revealed = False
        self._timer_id: Optional[int] = None
        self._toast_hide_id: Optional[int] = None
        self._syncing_view_selection = False

        self._css_prefix = f"wx{id(self) & 0xFFFFFF:x}"
        self._css_provider = Gtk.CssProvider.new()

        self._build()
        self._apply_appearance(rebuild=False)

        self._resize_hook = ResizeHook(self, self._on_available_size_changed)
        self.connect("map", self._on_mapped)
        self.connect("unmap", self._on_unmapped)
        GLib.idle_add(self._initialize)

    # ── widget lifecycle ────────────────────────────────────────────────────

    def do_size_allocate(self, width, height, baseline):
        # vfunc overrides are dead in this PyGObject build; ResizeHook drives
        # _on_available_size_changed instead. Kept for direct test invocation.
        Gtk.Overlay.do_size_allocate(self, width, height, baseline)
        self._on_available_size_changed(width, height)

    def _on_available_size_changed(self, width: float, height: float) -> None:
        if not (math.isfinite(width) and math.isfinite(height)):
            return
        self._last_width = width
        self._last_height = height
        new_mode = determine_layout_mode(width, height, self._layout_mode, self._text_size, self._system_text_scale)
        if new_mode != self._layout_mode:
            self._layout_mode = new_mode
            self.stack.set_visible_child_name(new_mode.lower())
        self._update_layout_gates()

    def _on_mapped(self, *_args) -> None:
        if self._disposed or self._revealed:
            return
        self._revealed = True
        self._start_timer()
        self.refresh()

    def _on_unmapped(self, *_args) -> None:
        # Port of OnWindowVisibilityChanged(false): cancel in-flight work and
        # stop the timer; _on_mapped re-arms both when revealed again.
        self._revealed = False
        self._request_version += 1
        self._refresh_pending = False
        self._pending_user_triggered = False
        self._pending_force_refresh = False
        self._stop_timer()

    def dispose(self) -> None:
        self._disposed = True
        self._request_version += 1
        self._stop_timer()
        if self._toast_hide_id is not None:
            GLib.source_remove(self._toast_hide_id)
            self._toast_hide_id = None

    def _stop_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _start_timer(self) -> None:
        self._stop_timer()
        self._timer_id = GLib.timeout_add_seconds(
            int(max(60, self._refresh_interval().total_seconds())), self._on_timer_tick
        )

    def _on_timer_tick(self) -> bool:
        if self._disposed:
            return False
        self.refresh()
        return True

    # ── initialization (InitializeAsync → LoadCachedWeatherAsync) ────────────

    def _initialize(self) -> bool:
        if self._disposed:
            return False
        self._load_cached_weather()
        if self._revealed:
            self._start_timer()
            self.refresh()
        return False

    def _load_cached_weather(self) -> None:
        state = self.service.load_cache_state()
        interval = self._refresh_interval()
        forecast = state.lastForecast

        if (
            forecast is not None
            and forecast.is_valid
            and forecast.data is not None
            and self._can_use_cached_forecast(forecast)
        ):
            self._latitude = forecast.latitude
            self._longitude = forecast.longitude
            self._location_name = forecast.locationName
            self._location_initialized = True
            if state.lastLocation is not None and state.lastLocation.is_valid:
                from ..services.weather_cache import is_same_location

                if is_same_location(
                    state.lastLocation.latitude,
                    state.lastLocation.longitude,
                    forecast.latitude,
                    forecast.longitude,
                ):
                    self._location_resolved_at = _parse(state.lastLocation.resolvedAtUtc)

            is_stale = _utc_now() - _parse(forecast.fetchedAtUtc) >= interval
            data = forecast.data
            data.locationName = forecast.locationName
            data.isStale = is_stale
            data.isFallback = forecast.requestedSource != forecast.actualSource
            self._weather_data = data
            self._apply_data_to_display()
            self._has_data = True
            self._automatic_refresh_not_before = (
                datetime.min.replace(tzinfo=timezone.utc) if is_stale else _parse(forecast.fetchedAtUtc) + interval
            )
            return

        weather = self.settings_service.settings.weather
        if weather.weatherAutoLocation and state.lastLocation is not None and state.lastLocation.is_valid:
            self._latitude = state.lastLocation.latitude
            self._longitude = state.lastLocation.longitude
            self._location_name = state.lastLocation.name
            self._location_initialized = True
            self._location_resolved_at = _parse(state.lastLocation.resolvedAtUtc)

    def _can_use_cached_forecast(self, forecast) -> bool:
        weather = self.settings_service.settings.weather
        if weather.weatherAutoLocation:
            return True
        if weather.weatherLatitude != 0 or weather.weatherLongitude != 0:
            from ..services.weather_cache import is_same_location

            return is_same_location(
                weather.weatherLatitude,
                weather.weatherLongitude,
                forecast.latitude,
                forecast.longitude,
            )
        name = (weather.weatherCityName or "").strip()
        return bool(name) and name.lower() == (forecast.locationName or "").strip().lower()

    # ── refresh (RefreshAsync port) ─────────────────────────────────────────

    def refresh(self, user_triggered: bool = False, force_refresh: bool = False) -> None:
        if self._disposed:
            return
        if self._is_refreshing:
            # Ordinary requests are satisfied by the in-flight operation;
            # preserve one explicit user/settings request.
            if user_triggered or force_refresh:
                self._request_version += 1
                self._refresh_pending = True
                self._pending_force_refresh = self._pending_force_refresh or force_refresh or user_triggered
                self._pending_user_triggered = self._pending_user_triggered or user_triggered
            return

        if not weather_backoff.can_attempt(
            _utc_now(), self._automatic_refresh_not_before, user_triggered, force_refresh
        ):
            return

        version = self._request_version + 1
        self._request_version = version
        self._refresh_was_user_triggered = user_triggered
        self._set_refreshing(True)

        def worker():
            try:
                self._ensure_location()
                interval = self._refresh_interval()
                data = self.service.get_weather(
                    self._latitude,
                    self._longitude,
                    self._location_name,
                    force_refresh=user_triggered or force_refresh,
                    cache_duration=interval,
                )
                succeeded = data is not None and data.current is not None and not data.isStale
                interval_minutes = interval
            except Exception:
                data, succeeded, interval_minutes = None, False, timedelta(minutes=30)
            GLib.idle_add(self._refresh_finished, version, data, succeeded, interval_minutes)

        _executor.submit(worker)

    def _refresh_finished(
        self, version: int, data: Optional[WeatherData], succeeded: bool, interval: timedelta
    ) -> bool:
        if self._disposed or version != self._request_version:
            return False

        if data is not None and data.current is not None:
            self._weather_data = data
            self._apply_data_to_display()
            self._has_data = True
            if succeeded:
                self._register_refresh_success(interval)
            else:
                self._register_refresh_failure()
        else:
            # Total failure without usable cache for this location: clear the
            # display so a previous city's weather never lingers.
            self._has_data = False
            self._weather_data = None
            self._apply_data_to_display()
            self._register_refresh_failure()

        self._set_refreshing(False)
        if self._refresh_was_user_triggered:
            self._show_refresh_status_toast(succeeded)
        self._refresh_was_user_triggered = False

        if self._refresh_pending and not self._disposed:
            user = self._pending_user_triggered
            force = self._pending_force_refresh
            self._refresh_pending = False
            self._pending_user_triggered = False
            self._pending_force_refresh = False
            self.refresh(user_triggered=user, force_refresh=force)
        return False

    def _register_refresh_success(self, interval: timedelta) -> None:
        self._consecutive_refresh_failures = 0
        self._automatic_refresh_not_before = _utc_now() + interval

    def _register_refresh_failure(self) -> None:
        self._consecutive_refresh_failures += 1
        delay = weather_backoff.get_failure_delay(self._consecutive_refresh_failures)
        self._automatic_refresh_not_before = _utc_now() + delay

    def _refresh_interval(self) -> timedelta:
        minutes = self.settings_service.settings.weather.weatherRefreshIntervalMinutes
        clamped = min(max(minutes, policy.REFRESH_MIN_MINUTES), policy.REFRESH_MAX_MINUTES)
        return timedelta(minutes=clamped)

    def _set_refreshing(self, refreshing: bool) -> None:
        self._is_refreshing = refreshing
        for button in self._refresh_buttons:
            if refreshing:
                button.add_css_class("weather-spin")
            else:
                button.remove_css_class("weather-spin")
        self.loading_revealer.set_reveal_child(refreshing and not self._has_data)

    def _show_refresh_status_toast(self, success: bool) -> None:
        self.toast_label.set_text(t("Weather.RefreshSuccess" if success else "Weather.RefreshFailed"))
        self.toast_revealer.set_reveal_child(True)
        if self._toast_hide_id is not None:
            GLib.source_remove(self._toast_hide_id)
        self._toast_hide_id = GLib.timeout_add(TOAST_AUTOHIDE_MS, self._hide_toast)

    def _hide_toast(self) -> bool:
        self.toast_revealer.set_reveal_child(False)
        self._toast_hide_id = None
        return False

    # ── location (EnsureLocationAsync port; blocking — worker thread only) ──

    def _ensure_location(self) -> None:
        language = current_language() or "zh-CN"
        now = _utc_now()
        weather = self.settings_service.settings.weather

        if weather.weatherAutoLocation:
            if self._location_initialized and now < self._location_retry_not_before:
                return
            if (
                self._location_initialized
                and not self._is_using_fallback_location
                and now - self._location_resolved_at < weather_backoff.LOCATION_REUSE_DURATION
            ):
                return

            result = get_location(language, t("Weather.CurrentLocation"))
            if result is not None:
                latitude, longitude, name = result
                self._latitude = latitude
                self._longitude = longitude
                self._location_name = refine_location_name(latitude, longitude, name, language)
                self._location_initialized = True
                self._location_resolved_at = now
                self._location_retry_not_before = datetime.min.replace(tzinfo=timezone.utc)
                self._is_using_fallback_location = False
                self.service.save_resolved_location(latitude, longitude, self._location_name, now)
                return

            self._location_retry_not_before = now + weather_backoff.LOCATION_FAILURE_DELAY
            if self._location_initialized:
                return

            # Auto-location failed: try the saved city, then a known default.
            saved = self._resolve_saved_city(weather.weatherCityName, language)
            if saved is not None:
                self._latitude, self._longitude, self._location_name = saved
                self._location_initialized = True
                self._location_resolved_at = now
                self._is_using_fallback_location = False
                return
            self._use_fallback_location(language)
            return

        if weather.weatherLatitude != 0 or weather.weatherLongitude != 0:
            self._latitude = weather.weatherLatitude
            self._longitude = weather.weatherLongitude
            self._location_name = weather.weatherCityName
            self._location_initialized = True
            self._location_resolved_at = now
            self._is_using_fallback_location = False
            return

        saved = self._resolve_saved_city(weather.weatherCityName, language)
        if saved is not None:
            self._latitude, self._longitude, self._location_name = saved
            self._location_initialized = True
            self._location_resolved_at = now
            self._is_using_fallback_location = False
            # Persist the resolved coordinates so later refreshes skip geocoding.
            weather.weatherLatitude = self._latitude
            weather.weatherLongitude = self._longitude
            self.settings_service.save_debounced()
        elif not self._location_initialized:
            self._use_fallback_location(language)

    def _resolve_saved_city(self, city_name: str, language: str):
        name = (city_name or "").strip()
        if not name:
            return None
        try:
            item = self.service.resolve_city(name, language)
        except Exception:
            return None
        if item is None:
            return None
        return (item.latitude, item.longitude, item.displayName or item.name)

    def _use_fallback_location(self, language: str) -> None:
        from ..services.weather_view_model import FALLBACK_LOCATION

        self._latitude, self._longitude = FALLBACK_LOCATION
        self._location_name = fallback_location_name(language.split("-")[0])
        self._location_initialized = True
        self._is_using_fallback_location = True

    # ── appearance / settings ────────────────────────────────────────────────

    def apply_appearance(self) -> None:
        """Re-read global settings (text size, skin, units, toggles)."""
        self._apply_appearance(rebuild=True)

    def _apply_appearance(self, rebuild: bool) -> None:
        shell = self.settings_service.settings.widgetShell
        weather = self.settings_service.settings.weather
        self._text_size = normalize_text_size(shell.textSize or DEFAULT_TEXT_SIZE)

        override = policy.try_get_week_view(self.config)
        if override is None:
            override = weather.weatherDefaultView == policy.DEFAULT_VIEW_WEEK
        self._is_week_view = override

        self._apply_typography()
        self._sync_view_selection()
        self._on_available_size_changed(self._last_width, self._last_height)
        if rebuild:
            self._apply_data_to_display()
        self._start_timer_if_revealed()

    def _start_timer_if_revealed(self) -> None:
        if self._revealed and not self._disposed:
            self._start_timer()

    # typography ramp (WeatherWidgetViewModel.TextSize-derived properties)
    @property
    def _effective_scale(self) -> float:
        return resolve_typography_scale(self._text_size, self._system_text_scale)

    @property
    def _title_size(self) -> float:
        return min(MAX_TEXT_SIZE + 1, self._text_size + 1)

    @property
    def _caption_size(self) -> float:
        return max(MIN_TEXT_SIZE - 1, self._text_size - 1)

    @property
    def _temperature_size(self) -> float:
        if self._layout_mode == "Mini":
            return min(24, self._text_size + 10)
        if self._layout_mode == "Compact":
            return min(30, self._text_size + 16)
        return min(32, self._text_size + 18)

    @property
    def _emoji_size(self) -> float:
        return 30 if self._layout_mode == "Mini" else (36 if self._layout_mode == "Compact" else 48)

    @property
    def _forecast_emoji_size(self) -> float:
        return 18 if self._layout_mode == "Expanded" else 15

    @property
    def _week_emoji_size(self) -> float:
        return 18 if self._layout_mode == "Expanded" else 16

    @property
    def _hourly_card_width(self) -> int:
        return math.ceil(56 + 32 * (self._effective_scale - 1))

    @property
    def _hourly_card_height(self) -> int:
        base = 100 if self._last_height >= 360 else (88 if self._last_height >= 310 else 80)
        return math.ceil(base + 52 * (self._effective_scale - 1))

    def _apply_typography(self) -> None:
        classes = {
            "title": f"font-size: {self._title_size:g}px; font-weight: 600;",
            "body": f"font-size: {self._text_size:g}px; font-weight: 600;",
            "body-plain": f"font-size: {self._text_size:g}px;",
            "caption": f"font-size: {self._caption_size:g}px;",
            "temp": f"font-size: {self._temperature_size:g}px; font-weight: 600;",
            "emoji": f"font-size: {self._emoji_size:g}px;",
            "femoji": f"font-size: {self._forecast_emoji_size:g}px;",
            "wemoji": f"font-size: {self._week_emoji_size:g}px;",
        }
        body = "\n".join(f".{self._css_prefix}-{name} {{ {rules} }}" for name, rules in classes.items())
        gradient = ""
        if self._display is not None and self._uses_rich_skin:
            gradient = (
                f".{self._css_prefix}-backdrop {{ background: linear-gradient("
                f"to bottom right, {css_color(self._display.rich_top)}, "
                f"{css_color(self._display.rich_bottom)}); }}"
            )
        self._css_provider.load_from_data((body + "\n" + gradient).encode("utf-8"))

    @property
    def _metric_column_visibility(self):
        weather = self.settings_service.settings.weather
        return (
            weather.weatherShowHumidity,
            weather.weatherShowWind,
            weather.weatherShowPrecipitation,
        )

    # progressive visibility thresholds (WeatherWidgetViewModel visibility props)
    @property
    def _show_primary_metrics(self) -> bool:
        delta = self._effective_scale - 1
        weather = self.settings_service.settings.weather
        if not (weather.weatherShowHumidity or weather.weatherShowWind or weather.weatherShowPrecipitation):
            return False
        if self._layout_mode == "Compact":
            return self._last_width >= 190 + 80 * delta and self._last_height >= 180 + 80 * delta
        if self._layout_mode == "Expanded":
            return self._last_width >= 300 + 170 * delta and self._last_height >= 200 + 90 * delta
        return False

    @property
    def _show_mini_header(self) -> bool:
        return self._last_height >= 104 + 48 * (self._effective_scale - 1)

    @property
    def _show_mini_description(self) -> bool:
        delta = self._effective_scale - 1
        return (
            bool(self._display and self._display.current_description)
            and self._last_width >= 185 + 80 * delta
            and self._last_height >= 118 + 36 * delta
        )

    @property
    def _show_mini_details(self) -> bool:
        return bool(self._display and self._display.apparent_temperature_text) and self._last_height >= 118 + 48 * (
            self._effective_scale - 1
        )

    @property
    def _show_expanded_sunrise(self) -> bool:
        delta = self._effective_scale - 1
        return (
            self._layout_mode == "Expanded"
            and self.settings_service.settings.weather.weatherShowSunrise
            and self._last_height >= 280 + 130 * delta
        )

    @property
    def _show_hourly_precip(self) -> bool:
        return self._layout_mode == "Expanded" and self._last_height >= 310 + 120 * (self._effective_scale - 1)

    @property
    def _show_secondary_metrics(self) -> bool:
        weather = self.settings_service.settings.weather
        return (
            self._layout_mode == "Expanded"
            and self._last_height >= 300 + 120 * (self._effective_scale - 1)
            and (weather.weatherShowUvIndex or weather.weatherShowPressure)
        )

    @property
    def _show_hourly_divider(self) -> bool:
        # Hide when the metric band is the last block above the forecast.
        return not (self._show_primary_metrics and not self._show_secondary_metrics and not self._show_expanded_sunrise)

    def _update_layout_gates(self) -> None:
        display = self._display
        weather = self.settings_service.settings.weather

        self.mini_header.set_visible(self._show_mini_header)
        self.mini_description.set_visible(self._show_mini_description)
        self.mini_details.set_visible(self._show_mini_details)
        self.primary_bands[0].set_visible(self._show_primary_metrics)  # compact
        self.primary_bands[1].set_visible(self._show_primary_metrics)  # expanded
        humidity, wind, precipitation = self._metric_column_visibility
        for column_box, visible in zip(self._metric_columns, (humidity, wind, precipitation) * 2):
            column_box.set_visible(visible)
        self.secondary_row.set_visible(self._show_secondary_metrics)
        self.uv_metric.set_visible(weather.weatherShowUvIndex)
        self.pressure_metric.set_visible(weather.weatherShowPressure)
        self.sunrise_row.set_visible(self._show_expanded_sunrise and bool(display and display.sunrise_text))
        for label in self._hourly_precip_labels:
            label.set_visible(self._show_hourly_precip)
        self.hourly_divider.set_visible(self._show_hourly_divider and weather.weatherShowForecast)

        show_forecast = weather.weatherShowForecast
        self.view_switcher.set_visible(show_forecast and self._layout_mode == "Expanded")
        self.forecast_stack.set_visible(
            show_forecast and self._layout_mode == "Expanded" and bool(display and display.has_data)
        )

        self._apply_typography()
        self._sync_palette_class()
        for card, _row in self._hourly_cards:
            card.set_size_request(self._hourly_card_width, self._hourly_card_height)

    def _sync_palette_class(self) -> None:
        dark = False
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None:
            dark = bool(gtk_settings.get_property("gtk-application-prefer-dark-theme")) or (
                "dark" in (gtk_settings.get_property("gtk-theme-name") or "").lower()
            )
        if dark:
            self.add_css_class("weather-dark-app")
        else:
            self.remove_css_class("weather-dark-app")

    # ── view mode (Day / Week) ───────────────────────────────────────────────

    def set_view_mode(self, use_week_view: bool) -> None:
        if self._is_week_view != use_week_view:
            self._is_week_view = use_week_view
        if policy.set_week_view(self.config, use_week_view):
            self.settings_service.update_widget(self.config)
            self.settings_service.save_debounced()
        self._sync_view_selection()

    def _sync_view_selection(self) -> None:
        self._syncing_view_selection = True
        try:
            self.day_button.set_active(not self._is_week_view)
            self.week_button.set_active(self._is_week_view)
            self.forecast_stack.set_visible_child_name("week" if self._is_week_view else "day")
        finally:
            self._syncing_view_selection = False

    # ── data application (ApplyWeatherData → labels) ─────────────────────────

    def _apply_data_to_display(self) -> None:
        weather = self.settings_service.settings.weather
        if self._weather_data is not None:
            self._display = apply_weather_data(
                self._weather_data,
                temperature_unit=weather.weatherTemperatureUnit,
                wind_speed_unit=weather.weatherWindSpeedUnit,
                skin=weather.weatherSkin,
                fallback_location_name_=fallback_location_name((current_language() or "en").split("-")[0]),
                language=current_language(),
            )
        else:
            self._display = None
        display = self._display

        fallback_visible = self._is_using_fallback_location or bool(display and display.is_fallback_data)
        for label, tooltip in self._fallback_markers:
            label.set_visible(fallback_visible)
            label.set_tooltip_text(t("Weather.LocationFallback"))

        if display is None or not display.has_data:
            for label in self._hero_labels():
                label.set_text("")
            for label in self._location_label_widgets:
                label.set_text("")
            self._rebuild_hourly([])
            self._rebuild_week([])
            self._update_layout_gates()
            self.loading_revealer.set_reveal_child(self._is_refreshing and not self._has_data)
            return

        for label in self._location_label_widgets:
            label.set_text(display.location_display)
        self.emoji_labels.set_text_each(display.current_emoji)
        self.temperature_labels.set_text_each(display.current_temperature_text)
        self.description_labels.set_text_each(display.current_description)
        self.apparent_labels.set_text_each(display.apparent_temperature_text)

        self.humidity_values.set_text_each(display.humidity_value_text)
        self.wind_values.set_text_each(display.wind_value_text)
        self.precipitation_values.set_text_each(display.precipitation_value_text)
        self.uv_value.set_text(display.uv_index_value_text)
        self.pressure_value.set_text(display.pressure_value_text)
        self.sunrise_value.set_text(display.sunrise_text)
        self.sunsetValue.set_text(display.sunset_text)

        self._rebuild_hourly(display.hourly)
        self._rebuild_week(display.daily)
        self._apply_rich_skin(display)
        self._update_layout_gates()
        self.loading_revealer.set_reveal_child(self._is_refreshing and not self._has_data)

    def _apply_rich_skin(self, display: WeatherDisplay) -> None:
        rich = self._uses_rich_skin and display.has_data
        self.backdrop.set_visible(rich)
        if rich:
            self.remove_css_class("weather-rich-light")
            self.remove_css_class("weather-rich-dark")
            self.add_css_class("weather-rich-on")
            self.add_css_class("weather-rich-light" if display.rich_light_text else "weather-rich-dark")
        else:
            self.remove_css_class("weather-rich-on")
            self.remove_css_class("weather-rich-light")
            self.remove_css_class("weather-rich-dark")
        self._apply_typography()  # refreshes the per-instance gradient rule

    @property
    def _uses_rich_skin(self) -> bool:
        return self.settings_service.settings.weather.weatherSkin == policy.SKIN_RICH

    def _rebuild_hourly(self, rows) -> None:
        if self._hourly_box is None:
            return
        child = self._hourly_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._hourly_box.remove(child)
            child = next_child
        self._hourly_cards = []
        self._hourly_precip_labels = []
        width = self._hourly_card_width
        height = self._hourly_card_height
        for row in rows:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
            card.set_size_request(width, height)
            card.add_css_class("weather-hour-card")
            hour = Gtk.Label(label=row.hour_label, visible=True)
            hour.add_css_class(f"{self._css_prefix}-caption")
            hour.add_css_class("weather-faint")
            emoji = Gtk.Label(label=row.emoji, visible=True)
            emoji.add_css_class(f"{self._css_prefix}-femoji")
            emoji.set_vexpand(True)
            temp = Gtk.Label(label=row.temperature_text, visible=True)
            temp.add_css_class(f"{self._css_prefix}-body")
            precip_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
            self._hourly_cards.append((card, row))
            if row.precipitation_text:
                precip = Gtk.Label(label=row.precipitation_text, visible=True)
                precip.add_css_class(f"{self._css_prefix}-caption")
                precip.add_css_class("weather-accent")
                precip_holder.append(precip)
                self._hourly_precip_labels.append(precip)
            card.append(hour)
            card.append(emoji)
            card.append(temp)
            card.append(precip_holder)
            if row.is_current_hour:
                bar = Gtk.Box(visible=True)
                bar.add_css_class("weather-hour-current")
                card.append(bar)
            self._hourly_box.append(card)

    def _rebuild_week(self, rows) -> None:
        if self._week_box is None:
            return
        child = self._week_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._week_box.remove(child)
            child = next_child
        for row in rows:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
            line.add_css_class("weather-week-row")

            day = Gtk.Label(label=row.day_label, visible=True)
            day.set_size_request(56, -1)
            day.set_halign(Gtk.Align.START)
            day.add_css_class(f"{self._css_prefix}-body-plain")
            day.add_css_class("weather-dim")

            emoji = Gtk.Label(label=row.emoji, visible=True)
            emoji.set_size_request(28, -1)
            emoji.add_css_class(f"{self._css_prefix}-wemoji")

            spacer = Gtk.Box(visible=True)
            spacer.set_hexpand(True)

            track = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True)
            track.add_css_class("weather-bar-track")
            fill = Gtk.Box(visible=True)
            fill.add_css_class("weather-bar-fill")
            fill.set_margin_start(int(row.bar_offset * 72))
            fill.set_size_request(max(int(round(row.bar_width * 72)), 3), 4)
            track.append(fill)
            track.set_size_request(72, 4)

            temp_max = Gtk.Label(label=row.temp_max_text, visible=True)
            temp_max.add_css_class(f"{self._css_prefix}-body")
            temp_min = Gtk.Label(label=row.temp_min_text, visible=True)
            temp_min.add_css_class(f"{self._css_prefix}-body-plain")
            temp_min.add_css_class("weather-faint")

            line.append(day)
            line.append(emoji)
            line.append(spacer)
            line.append(track)
            line.append(temp_max)
            line.append(temp_min)
            self._week_box.append(line)

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        from gi.repository import Gdk

        self._refresh_buttons = []
        self._fallback_markers = []
        self._location_label_widgets = []
        self._hourly_cards = []
        self._hourly_precip_labels = []

        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        # Rich-skin backdrop (bottom layer of the Overlay).
        self.backdrop = Gtk.Box(visible=True)
        self.backdrop.set_hexpand(True)
        self.backdrop.set_vexpand(True)
        self.backdrop.add_css_class(f"{self._css_prefix}-backdrop")
        self.backdrop.add_css_class("weather-rich-backdrop")
        self.backdrop.set_visible(False)
        self.set_child(self.backdrop)

        self.stack = Gtk.Stack(visible=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.stack.add_named(self._build_mini(), "mini")
        self.stack.add_named(self._build_compact(), "compact")
        self.stack.add_named(self._build_expanded(), "expanded")
        self.add_overlay(self.stack)

        self.loading_revealer = Gtk.Revealer(visible=True)
        self.loading_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.loading_revealer.set_halign(Gtk.Align.FILL)
        self.loading_revealer.set_valign(Gtk.Align.FILL)
        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=True)
        loading_box.set_halign(Gtk.Align.CENTER)
        loading_box.set_valign(Gtk.Align.CENTER)
        loading_box.add_css_class("weather-loading")
        spinner = Gtk.Spinner(visible=True)
        spinner.start()
        self.loading_label = Gtk.Label(label=t("Weather.Loading"), visible=True)
        self.loading_label.add_css_class(f"{self._css_prefix}-body-plain")
        loading_box.append(spinner)
        loading_box.append(self.loading_label)
        self.loading_revealer.set_child(loading_box)
        self.loading_revealer.set_reveal_child(False)
        self.add_overlay(self.loading_revealer)

        self.toast_revealer = Gtk.Revealer(visible=True)
        self.toast_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.toast_revealer.set_halign(Gtk.Align.CENTER)
        self.toast_revealer.set_valign(Gtk.Align.END)
        self.toast_revealer.set_margin_bottom(8)
        toast_box = Gtk.Box(visible=True)
        toast_box.add_css_class("weather-toast")
        self.toast_label = Gtk.Label(visible=True)
        self.toast_label.add_css_class(f"{self._css_prefix}-caption")
        toast_box.append(self.toast_label)
        self.toast_revealer.set_child(toast_box)
        self.toast_revealer.set_reveal_child(False)
        self.add_overlay(self.toast_revealer)

    def _make_location_label(self, size_class: str) -> Gtk.Label:
        label = Gtk.Label(visible=True)
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END if Pango else Pango.EllipsizeMode.NONE)
        label.add_css_class(size_class)
        label.add_css_class("weather-dim")
        self._location_label_widgets.append(label)
        return label

    def _make_fallback_marker(self) -> Gtk.Label:
        marker = Gtk.Label(label="⚠", visible=True)
        marker.add_css_class("weather-warn")
        self._fallback_markers.append((marker, None))
        return marker

    def _make_refresh_button(self, small: bool = False) -> Gtk.Button:
        button = Gtk.Button(label="⟳", visible=True)
        button.add_css_class("weather-icon-btn")
        if small:
            button.add_css_class("weather-icon-btn-sm")
        button.set_tooltip_text(t("Common.Refresh"))
        button.connect("clicked", self._on_refresh_clicked)
        self._refresh_buttons.append(button)
        return button

    def _on_refresh_clicked(self, _button) -> None:
        self.refresh(user_triggered=True)

    def _build_mini(self) -> Gtk.Box:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
        root.add_css_class("weather-mini")

        self.mini_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        header_left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        header_left.set_hexpand(True)
        location = self._make_location_label(f"{self._css_prefix}-caption")
        header_left.append(location)
        header_left.append(self._make_fallback_marker())
        self.mini_header.append(header_left)
        self.mini_header.append(self._make_refresh_button(small=True))
        root.append(self.mini_header)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=True)
        hero.set_halign(Gtk.Align.CENTER)
        hero.set_valign(Gtk.Align.CENTER)
        hero.set_vexpand(True)
        emoji = Gtk.Label(visible=True)
        emoji.add_css_class(f"{self._css_prefix}-emoji")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        temperature = Gtk.Label(visible=True)
        temperature.add_css_class(f"{self._css_prefix}-temp")
        self.mini_description = Gtk.Label(visible=True)
        self.mini_description.set_ellipsize(Pango.EllipsizeMode.END if Pango else Pango.EllipsizeMode.NONE)
        self.mini_description.add_css_class(f"{self._css_prefix}-caption")
        self.mini_description.add_css_class("weather-dim")
        top.append(temperature)
        top.append(self.mini_description)
        self.mini_details = Gtk.Label(visible=True)
        self.mini_details.set_ellipsize(Pango.EllipsizeMode.END if Pango else Pango.EllipsizeMode.NONE)
        self.mini_details.add_css_class(f"{self._css_prefix}-caption")
        self.mini_details.add_css_class("weather-faint")
        texts.append(top)
        texts.append(self.mini_details)
        hero.append(emoji)
        hero.append(texts)
        root.append(hero)

        self.emoji_labels = _LabelGroup(emoji)
        self.temperature_labels = _LabelGroup(temperature)
        self.description_labels = _LabelGroup(self.mini_description)
        self.apparent_labels = _LabelGroup(self.mini_details)
        return root

    def _build_compact(self) -> Gtk.Box:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        root.add_css_class("weather-compact")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        header_left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        header_left.set_hexpand(True)
        header_left.append(self._make_location_label(f"{self._css_prefix}-caption"))
        header_left.append(self._make_fallback_marker())
        header.append(header_left)
        header.append(self._make_refresh_button())
        root.append(header)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9, visible=True)
        hero.set_halign(Gtk.Align.CENTER)
        hero.set_valign(Gtk.Align.CENTER)
        hero.set_vexpand(True)
        emoji = Gtk.Label(visible=True)
        emoji.add_css_class(f"{self._css_prefix}-emoji")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7, visible=True)
        temperature = Gtk.Label(visible=True)
        temperature.add_css_class(f"{self._css_prefix}-temp")
        description = Gtk.Label(visible=True)
        description.set_ellipsize(Pango.EllipsizeMode.END if Pango else Pango.EllipsizeMode.NONE)
        description.add_css_class(f"{self._css_prefix}-body")
        top.append(temperature)
        top.append(description)
        feels = Gtk.Label(visible=True)
        feels.set_ellipsize(Pango.EllipsizeMode.END if Pango else Pango.EllipsizeMode.NONE)
        feels.add_css_class(f"{self._css_prefix}-caption")
        feels.add_css_class("weather-faint")
        texts.append(top)
        texts.append(feels)
        hero.append(emoji)
        hero.append(texts)
        root.append(hero)

        # Shared VM labels: register this layout's labels into the groups the
        # mini builder created.
        self.emoji_labels.add(emoji)
        self.temperature_labels.add(temperature)
        self.description_labels.add(description)
        self.apparent_labels.add(feels)

        band = self._build_metric_band()
        root.append(band)
        self.primary_bands = (band, None)  # second filled by expanded builder
        return root

    def _build_metric_band(self, expanded: bool = False) -> Gtk.Box:
        band = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        band.set_hexpand(True)
        band.add_css_class("weather-metric-band")
        if expanded:
            band.add_css_class("weather-metric-band-expanded")

        weather = self.settings_service.settings.weather

        def column(label_key: str, visible: bool):
            column_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, visible=True)
            column_box.set_hexpand(True)
            column_box.set_halign(Gtk.Align.CENTER)
            caption = Gtk.Label(label=t(label_key), visible=True)
            caption.add_css_class(f"{self._css_prefix}-caption")
            caption.add_css_class("weather-faint")
            value = Gtk.Label(visible=True)
            value.add_css_class(f"{self._css_prefix}-body")
            column_box.append(caption)
            column_box.append(value)
            column_box.set_visible(visible)
            return column_box, value

        humidity, humidity_value = column("Weather.Metric.Humidity", weather.weatherShowHumidity)
        wind, wind_value = column("Weather.Metric.Wind", weather.weatherShowWind)
        precipitation, precip_value = column("Weather.Metric.Precipitation", weather.weatherShowPrecipitation)
        band.append(humidity)
        band.append(wind)
        band.append(precipitation)

        # Called once per layout (Compact, Expanded): extend the shared groups.
        if not hasattr(self, "humidity_values"):
            self.humidity_values = _LabelGroup(humidity_value)
            self.wind_values = _LabelGroup(wind_value)
            self.precipitation_values = _LabelGroup(precip_value)
            self._metric_columns = [humidity, wind, precipitation]
        else:
            self.humidity_values.add(humidity_value)
            self.wind_values.add(wind_value)
            self.precipitation_values.add(precip_value)
            self._metric_columns.extend((humidity, wind, precipitation))
        return band

    def _build_expanded(self) -> Gtk.Box:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, visible=True)
        root.add_css_class("weather-expanded")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7, visible=True)
        header_left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        header_left.set_hexpand(True)
        location = self._make_location_label(f"{self._css_prefix}-body-plain")
        location.remove_css_class("weather-dim")
        header_left.append(location)
        header_left.append(self._make_fallback_marker())
        header.append(header_left)

        switcher = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True)
        switcher.add_css_class("weather-segmented")
        self.day_button = Gtk.ToggleButton(label=t("Weather.View.DayShort"), visible=True)
        self.week_button = Gtk.ToggleButton(label=t("Weather.View.WeekShort"), visible=True)
        self.week_button.set_group(self.day_button)
        self.day_button.set_tooltip_text(t("Weather.View.Today"))
        self.week_button.set_tooltip_text(t("Weather.View.Week"))
        self.day_button.connect("toggled", self._on_view_toggled, False)
        self.week_button.connect("toggled", self._on_view_toggled, True)
        switcher.append(self.day_button)
        switcher.append(self.week_button)
        self.view_switcher = switcher
        header.append(switcher)
        header.append(self._make_refresh_button())
        root.append(header)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11, visible=True)
        hero.set_halign(Gtk.Align.START)
        hero.set_margin_start(2)
        emoji = Gtk.Label(visible=True)
        emoji.add_css_class(f"{self._css_prefix}-emoji")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9, visible=True)
        temperature = Gtk.Label(visible=True)
        temperature.add_css_class(f"{self._css_prefix}-temp")
        description = Gtk.Label(visible=True)
        description.set_ellipsize(Pango.EllipsizeMode.END if Pango else Pango.EllipsizeMode.NONE)
        description.add_css_class(f"{self._css_prefix}-title")
        top.append(temperature)
        top.append(description)
        feels = Gtk.Label(visible=True)
        feels.set_ellipsize(Pango.EllipsizeMode.END if Pango else Pango.EllipsizeMode.NONE)
        feels.add_css_class(f"{self._css_prefix}-caption")
        feels.add_css_class("weather-faint")
        texts.append(top)
        texts.append(feels)
        hero.append(emoji)
        hero.append(texts)
        root.append(hero)

        self.emoji_labels.add(emoji)
        self.temperature_labels.add(temperature)
        self.description_labels.add(description)
        self.apparent_labels.add(feels)

        band = self._build_metric_band(expanded=True)
        root.append(band)

        self.secondary_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24, visible=True)
        self.secondary_row.set_halign(Gtk.Align.CENTER)
        uv_caption = Gtk.Label(label=t("Weather.Metric.UV"), visible=True)
        uv_caption.add_css_class(f"{self._css_prefix}-caption")
        uv_caption.add_css_class("weather-faint")
        self.uv_value = Gtk.Label(visible=True)
        self.uv_value.add_css_class(f"{self._css_prefix}-body")
        pressure_caption = Gtk.Label(label=t("Weather.Metric.Pressure"), visible=True)
        pressure_caption.add_css_class(f"{self._css_prefix}-caption")
        pressure_caption.add_css_class("weather-faint")
        self.pressure_value = Gtk.Label(visible=True)
        self.pressure_value.add_css_class(f"{self._css_prefix}-body")
        self.uv_metric = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        self.pressure_metric = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        self.uv_metric.append(uv_caption)
        self.uv_metric.append(self.uv_value)
        self.pressure_metric.append(pressure_caption)
        self.pressure_metric.append(self.pressure_value)
        self.secondary_row.append(self.uv_metric)
        self.secondary_row.append(self.pressure_metric)
        root.append(self.secondary_row)

        sunrise_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, visible=True)
        sunrise_row.add_css_class("weather-sunrise-row")
        sunrise_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        sunrise_caption = Gtk.Label(label=t("Weather.Metric.Sunrise"), visible=True)
        sunrise_caption.add_css_class(f"{self._css_prefix}-caption")
        sunrise_caption.add_css_class("weather-faint")
        self.sunrise_value = Gtk.Label(visible=True)
        self.sunrise_value.add_css_class(f"{self._css_prefix}-body")
        sunrise_box.append(sunrise_caption)
        sunrise_box.append(self.sunrise_value)
        arc = _DaylightArc(visible=True)
        arc.set_hexpand(True)
        sunset_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        sunset_caption = Gtk.Label(label=t("Weather.Metric.Sunset"), visible=True)
        sunset_caption.add_css_class(f"{self._css_prefix}-caption")
        sunset_caption.add_css_class("weather-faint")
        self.sunsetValue = Gtk.Label(visible=True)
        self.sunsetValue.add_css_class(f"{self._css_prefix}-body")
        sunset_box.append(sunset_caption)
        sunset_box.append(self.sunsetValue)
        sunrise_row.append(sunrise_box)
        sunrise_row.append(arc)
        sunset_box.set_halign(Gtk.Align.END)
        sunrise_row.append(sunset_box)
        self.sunrise_row = sunrise_row
        root.append(sunrise_row)

        self.forecast_stack = Gtk.Stack(visible=True)
        self.forecast_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.hourly_divider = Gtk.Box(visible=True)
        self.hourly_divider.add_css_class("weather-divider")
        self.hourly_divider.set_hexpand(True)

        day_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=True)
        day_box.append(self.hourly_divider)
        hourly_scroll = Gtk.ScrolledWindow(visible=True)
        hourly_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        hourly_scroll.set_hexpand(True)
        hourly_scroll.set_vexpand(True)
        self._hourly_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, visible=True)
        self._hourly_box.set_valign(Gtk.Align.END)
        hourly_scroll.set_child(self._hourly_box)
        day_box.append(hourly_scroll)
        self.forecast_stack.add_named(day_box, "day")

        week_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        week_box.add_css_class("weather-week-section")
        week_scroll = Gtk.ScrolledWindow(visible=True)
        week_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        week_scroll.set_vexpand(True)
        self._week_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        week_scroll.set_child(self._week_box)
        week_box.append(week_scroll)
        self.forecast_stack.add_named(week_box, "week")

        self.forecast_stack.set_vexpand(True)
        root.append(self.forecast_stack)

        # The compact builder ran first and registered its band; pair them so
        # _update_layout_gates can toggle both layouts' bands.
        self.primary_bands = (self.primary_bands[0], band)
        return root

    def _on_view_toggled(self, button, use_week_view: bool) -> None:
        if self._syncing_view_selection or not button.get_active():
            return
        self.set_view_mode(use_week_view)

    def _hero_labels(self):
        for group in (
            self.emoji_labels,
            self.temperature_labels,
            self.description_labels,
            self.apparent_labels,
        ):
            yield from group.labels


class _LabelGroup:
    """Sets text on one logical label shared across layouts (C# shared VM props)."""

    def __init__(self, *labels: Gtk.Label):
        self.labels = [label for label in labels if label is not None]

    def add(self, label: Gtk.Label) -> None:
        if label is not None and label not in self.labels:
            self.labels.append(label)

    def set_text_each(self, text: str) -> None:
        for label in self.labels:
            label.set_text(text)


class _DaylightArc(Gtk.DrawingArea):
    """Sunrise→sunset arc track (XAML Path "M0,14 C34,7 76,7 110,14")."""

    def __init__(self, visible: bool = True):
        super().__init__(visible=visible)
        self.set_size_request(-1, 17)
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, width, height) -> None:
        base_y = 14.0
        ctrl_y = 7.0
        apex_y = (base_y + 3 * ctrl_y + 3 * ctrl_y + base_y) / 8.0

        cr.save()
        cr.set_line_cap(1)  # round
        for opacity, line_width in ((0.14, 4.0), (0.72, 1.5)):
            cr.set_source_rgba(0.66, 0.37, 0.0, opacity)
            cr.set_line_width(line_width)
            cr.move_to(1, base_y)
            cr.curve_to(width * 0.3, ctrl_y, width * 0.7, ctrl_y, width - 1, base_y)
            cr.stroke()
        cr.restore()

        # Sun dot at the apex: halo + core.
        center_x = width / 2.0
        cr.set_source_rgba(0.66, 0.37, 0.0, 0.16)
        cr.arc(center_x, apex_y, 4.0, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.66, 0.37, 0.0, 1.0)
        cr.arc(center_x, apex_y, 1.5, 0, 2 * math.pi)
        cr.fill()
        _ = height


def _parse(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
