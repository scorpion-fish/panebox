"""Weather surface GUI tests under Xvfb: staged assertions through a
GLib.MainLoop with a stubbed WeatherService and patched IP location.

Skips when display :99 (the test rig's Xvfb) is not reachable — mirrors
test_gui_smoke.py. Real GTK4 layout runs: hysteresis stack switching, label
groups shared across the three layouts, week-view metadata persistence,
fallback marker visibility, and rich-skin backdrop.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


if _display_reachable(SMOKE_DISPLAY) and not os.environ.get("PANEBOX_SKIP_GUI"):
    # Must be pinned before the first Gdk.Display is opened.
    os.environ["DISPLAY"] = SMOKE_DISPLAY
    os.environ.setdefault("GDK_BACKEND", "x11")
    os.environ.setdefault("GSK_RENDERER", "cairo")

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    _display_reachable(SMOKE_DISPLAY) is False or os.environ.get("PANEBOX_SKIP_GUI"),
    reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)",
)

from panebox import i18n  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import panebox.views.weather_widget as wv  # noqa: E402
from panebox.models.weather import DATA_SOURCE_OPEN_METEO  # noqa: E402
from panebox.models.widget_config import WidgetConfig  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.services.weather_service import WeatherService  # noqa: E402
from panebox.views.weather_widget import WeatherSurface  # noqa: E402


def _fake_fetch(url: str) -> dict:
    if "open-meteo.com/v1/forecast" in url:
        # Dates are generated at call time: the surface trims hourly rows
        # before "now" and marks the current one, so a hardcoded date turns
        # the whole fixture stale the next day.
        from datetime import timedelta

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        day_after = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        return {
            "latitude": 39.9,
            "longitude": 116.4,
            "timezone": "Asia/Shanghai",
            "current": {
                "time": f"{today}T{now.hour:02d}:00",
                "temperature_2m": 21.6,
                "relative_humidity_2m": 55,
                "apparent_temperature": 20.1,
                "weather_code": 1,
                "wind_speed_10m": 18,
                "wind_direction_10m": 225,
                "pressure_msl": 1014,
                "is_day": 1,
            },
            "hourly": {
                "time": [f"{today}T{h:02d}:00" for h in range(24)],
                "temperature_2m": [20 + h / 10 for h in range(24)],
                "precipitation_probability": [60 - h for h in range(24)],
                "weather_code": [61 if h < 3 else (2 if h < 8 else 0) for h in range(24)],
            },
            "daily": {
                "time": [today, tomorrow, day_after],
                "weather_code": [1, 61, 71],
                "temperature_2m_max": [26, 20, 16],
                "temperature_2m_min": [14, 12, 9],
                "precipitation_probability_max": [0, 70, 40],
                "sunrise": [f"{today}T06:02"],
                "sunset": [f"{today}T18:07"],
                "uv_index_max": [5.0, 2.0, 1.0],
            },
        }
    raise RuntimeError(f"unexpected url {url}")


def _make_surface(config_root: Path, widget_id: str):
    config_root.mkdir(exist_ok=True)
    settings = SettingsService(config_root=config_root)
    config = WidgetConfig(id=widget_id, name="Weather", widgetKind="Weather")
    settings.add_widget(config)
    surface = WeatherSurface(
        config,
        settings,
        weather_service=WeatherService(
            data_source_getter=lambda: DATA_SOURCE_OPEN_METEO,
            fetch_json=_fake_fetch,
        ),
    )
    window = Gtk.Window(default_width=330, default_height=470)
    window.set_child(surface)
    window.present()
    return surface, settings, window


def _run_phases(phases, timeout_ms: int = 15000) -> None:
    """Schedule (delay_ms, fn) checks inside a MainLoop; first failure stops."""
    failures: list = []
    loop = GLib.MainLoop()

    def guard(fn):
        def run():
            try:
                fn()
            except Exception as error:  # noqa: BLE001
                failures.append(error)
                loop.quit()
            return False

        return run

    offset = 0
    for delay, fn in phases:
        offset += delay
        GLib.timeout_add(offset, guard(fn))
    GLib.timeout_add(timeout_ms, loop.quit)
    loop.run()
    if failures:
        raise failures[0]


def test_surface_data_layouts_and_week_toggle(tmp_path):
    # Set at test time (not import time) — module imports run during pytest
    # collection and would leak the language into other test files.
    i18n.set_language("zh-CN")
    original_get_location = wv.get_location
    wv.get_location = lambda *a, **k: (39.9, 116.4, "北京")
    try:
        surface, settings, window = _make_surface(tmp_path / "config", "wx-test")

        def assert_expanded():
            assert surface._layout_mode == "Expanded"
            assert surface._display is not None and surface._display.has_data
            assert surface._location_label_widgets[0].get_text() == "北京"
            assert surface.temperature_labels.labels[0].get_text() == "22°C"
            assert surface.description_labels.labels[0].get_text() == "晴间多云"
            assert surface.apparent_labels.labels[0].get_text() == "体感 20°C"
            assert surface.humidity_values.labels[0].get_text() == "55%"
            assert surface.wind_values.labels[0].get_text() == "18 km/h"
            assert surface.precipitation_values.labels[0].get_text() == "0%"
            assert surface.uv_value.get_text() == "5"
            assert surface.sunrise_value.get_text() == "06:02"
            assert surface.sunsetValue.get_text() == "18:07"
            assert len(surface._hourly_cards) == 24 - datetime.now().hour
            assert surface._hourly_cards[0][1].is_current_hour
            # Not fallback: requested OpenMeteo, served OpenMeteo.
            assert surface._weather_data.isFallback is False
            assert all(not marker.get_visible() for marker, _ in surface._fallback_markers)
            assert surface.backdrop.get_visible() is False  # Standard skin

        def toggle_week():
            surface.week_button.set_active(True)
            assert surface.forecast_stack.get_visible_child_name() == "week"
            assert surface.config.metadata.get("Weather.ViewMode") == "Week"
            settings.flush_pending_save()
            layout_json = (tmp_path / "config" / "widget-layout.json").read_text("utf-8")
            assert "Week" in layout_json

        def toggle_back_and_downsize():
            surface.day_button.set_active(True)
            assert surface.forecast_stack.get_visible_child_name() == "day"
            surface._on_available_size_changed(210, 190)

        def assert_compact():
            assert surface._layout_mode == "Compact"
            assert surface.stack.get_visible_child_name() == "compact"
            # Label groups are shared across layouts: compact labels carry data.
            assert surface.humidity_values.labels[-1].get_text() == "55%"
            assert surface.temperature_labels.labels[-1].get_text() == "22°C"

        def go_mini():
            surface._on_available_size_changed(150, 120)

        def assert_mini():
            assert surface._layout_mode == "Mini"
            assert surface.stack.get_visible_child_name() == "mini"
            assert surface.temperature_labels.labels[0].get_text() == "22°C"

        try:
            _run_phases(
                [
                    (2400, assert_expanded),
                    (200, toggle_week),
                    (200, toggle_back_and_downsize),
                    (200, assert_compact),
                    (200, go_mini),
                    (200, assert_mini),
                ]
            )
        finally:
            # Destroying the window drives the allocation to 0×0, so teardown
            # must not run before the assertions.
            surface.dispose()
            window.destroy()
    finally:
        wv.get_location = original_get_location
        i18n.set_language(None)


def test_surface_rich_skin(tmp_path):
    i18n.set_language("zh-CN")
    original_get_location = wv.get_location
    wv.get_location = lambda *a, **k: (39.9, 116.4, "北京")
    try:
        surface, _settings, window = _make_surface(tmp_path / "config", "wx-rich")
        surface.settings_service.settings.weather.weatherSkin = "Rich"

        def assert_rich():
            assert surface._display is not None and surface._display.has_data
            assert surface._uses_rich_skin is True
            assert surface.backdrop.get_visible() is True
            assert surface.has_css_class("weather-rich-on")
            assert surface.has_css_class("weather-rich-light")  # deep blue gradient

        try:
            _run_phases([(2400, assert_rich)])
        finally:
            surface.dispose()
            window.destroy()
    finally:
        wv.get_location = original_get_location
        i18n.set_language(None)
