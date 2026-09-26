"""FeatureWidgets settings page tests.

Two layers:
- page wiring (GUI, Xvfb): controls land in the settings slices and fire the
  new SettingsHooks (set_feature_enabled / refresh_feature);
- WidgetManager.set_feature_enabled / refresh_feature semantics (no X needed:
  runtime creation is patched out).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


GUI = _display_reachable(SMOKE_DISPLAY) and not os.environ.get("PANEBOX_SKIP_GUI")
if GUI:
    # Must be pinned before the first Gdk.Display is opened.
    os.environ["DISPLAY"] = SMOKE_DISPLAY
    os.environ.setdefault("GDK_BACKEND", "x11")
    os.environ.setdefault("GSK_RENDERER", "cairo")

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not GUI, reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)")

if GUI:
    import gi  # noqa: E402

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # noqa: E402

from panebox import i18n  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.services.widget_manager import WidgetManager  # noqa: E402
from panebox.views.settings_window import SettingsHooks, SettingsWindow  # noqa: E402


# ---- WidgetManager.set_feature_enabled / refresh_feature (no display) ----------


def _manager(tmp: Path) -> WidgetManager:
    settings = SettingsService(config_root=tmp / "config")
    settings.load()
    manager = WidgetManager(settings)
    # Patch out everything that touches real X11 windows.
    manager.created: list[str] = []  # type: ignore[attr-defined]
    manager.closed: list[str] = []  # type: ignore[attr-defined]
    manager.create_runtime = (  # type: ignore[method-assign]
        lambda config, x, y, w, h, present=True: manager.created.append(config.id)
    )
    manager.close_widget = lambda widget_id: manager.closed.append(widget_id)  # type: ignore[method-assign]
    manager.capture_geometries = lambda: None  # type: ignore[method-assign]
    return manager


def test_set_feature_enabled_true_mints_widget_and_gates_restore(tmp_path):
    manager = _manager(tmp_path)
    manager.set_feature_enabled("Todo", True)

    assert manager.created, "enable must mint a widget when none exists"
    assert manager.settings.layout.featureWidgetEnabledStates["Todo"] is True
    kinds = [w.widgetKind for w in manager.settings.layout.widgets]
    assert kinds.count("Todo") == 1


def test_set_feature_enabled_true_restores_gated_config(tmp_path):
    manager = _manager(tmp_path)
    config = manager.create_feature_widget("Weather")  # mints + enables
    manager.runtimes.clear()
    manager.created.clear()
    manager.set_feature_enabled("Weather", True)

    # No second widget: the existing config is restored, not re-minted.
    assert manager.created == [config.id]
    kinds = [w.widgetKind for w in manager.settings.layout.widgets]
    assert kinds.count("Weather") == 1


def test_set_feature_enabled_false_closes_and_ungates(tmp_path):
    manager = _manager(tmp_path)
    config = manager.create_feature_widget("Music")
    manager.runtimes[config.id] = SimpleNamespace(config=config)
    manager.set_feature_enabled("Music", False)

    assert manager.closed == [config.id]
    assert manager.settings.layout.featureWidgetEnabledStates["Music"] is False


def test_set_feature_enabled_rejects_non_feature_kind(tmp_path):
    manager = _manager(tmp_path)
    manager.set_feature_enabled("File", True)  # no-op, not a feature kind
    assert manager.created == []
    assert "File" not in (manager.settings.layout.featureWidgetEnabledStates or {})


def test_refresh_feature_calls_surface_entry_points(tmp_path):
    manager = _manager(tmp_path)
    calls = []

    class _Surface:
        def refresh(self):
            calls.append("refresh")

        def queue_rebuild(self):
            calls.append("queue_rebuild")

    class _GlanceSurface:
        def apply_settings(self):
            calls.append("apply_settings")

    todo = SimpleNamespace(config=SimpleNamespace(widgetKind="Todo"), surface=_Surface())
    glance = SimpleNamespace(config=SimpleNamespace(widgetKind="Glance"), surface=_GlanceSurface())
    manager.runtimes.update({"t": todo, "g": glance})

    manager.refresh_feature("Todo")
    assert calls == ["refresh", "queue_rebuild"]
    calls.clear()
    manager.refresh_feature("Glance")
    assert calls == ["apply_settings"]
    calls.clear()
    manager.refresh_feature("NoSuchKind")
    assert calls == []


# ---- settings page wiring (GUI) ------------------------------------------------


class FakeHooks(SettingsHooks):
    def __init__(self, with_feature_hooks=True):
        self.calls = {"save": 0, "set_feature_enabled": [], "refresh_feature": []}
        super().__init__(
            save=self._save,
            apply_theme=lambda _t: None,
            apply_language=lambda: None,
            apply_hotkey=lambda: SimpleNamespace(state="registered", detail=""),
            apply_autostart=lambda _on: None,
            apply_appearance=lambda: None,
            **(
                {"set_feature_enabled": self._set_enabled, "refresh_feature": self._refresh}
                if with_feature_hooks
                else {}
            ),
        )

    def _save(self):
        self.calls["save"] += 1

    def _set_enabled(self, kind, enabled):
        self.calls["set_feature_enabled"].append((kind, enabled))

    def _refresh(self, kind):
        self.calls["refresh_feature"].append(kind)


def _row_controls(page) -> dict:
    """Map row title label → the row's control, in build order."""
    out: dict = {}

    def walk(widget):
        classes = widget.get_css_classes() if isinstance(widget, Gtk.Widget) else []
        if isinstance(widget, Gtk.Box) and "settings-row" in classes:
            texts = widget.get_first_child()
            title_label = texts.get_first_child() if texts is not None else None
            control = texts.get_next_sibling() if texts is not None else None
            if title_label is not None and control is not None:
                out.setdefault(title_label.get_label(), control)
            return  # rows never nest rows
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(page)
    return out


@pytest.fixture()
def feature_page(tmp_path):
    i18n.set_language("en-US")
    settings = SettingsService(config_root=tmp_path / "config")
    settings.load()
    hooks = FakeHooks()
    window = SettingsWindow(None, settings, hooks)
    window.show_section("FeatureWidgets")
    page = window._stack.get_child_by_name("FeatureWidgets")
    controls = _row_controls(page)
    yield window, settings, hooks, controls
    window.destroy()
    i18n.set_language(None)


def test_nav_has_feature_widgets_section(feature_page):
    window, _settings, _hooks, _controls = feature_page
    assert "FeatureWidgets" in window._nav_rows
    assert window._stack.get_visible_child_name() == "FeatureWidgets"


def test_feature_enable_switches_route_through_hooks(feature_page):
    _window, _settings, hooks, controls = feature_page
    controls["Todo"].set_active(True)
    controls["Search"].set_active(True)
    assert hooks.calls["set_feature_enabled"] == [("Todo", True), ("Search", True)]
    controls["Todo"].set_active(False)
    assert hooks.calls["set_feature_enabled"][-1] == ("Todo", False)


def test_feature_enable_fallback_flips_layout_gate(tmp_path):
    i18n.set_language("en-US")
    settings = SettingsService(config_root=tmp_path / "config")
    settings.load()
    window = SettingsWindow(None, settings, FakeHooks(with_feature_hooks=False))
    window.show_section("FeatureWidgets")
    controls = _row_controls(window._stack.get_child_by_name("FeatureWidgets"))
    controls["Quick Capture"].set_active(True)
    assert settings.layout.featureWidgetEnabledStates["QuickCapture"] is True
    window.destroy()
    i18n.set_language(None)


def test_todo_fields_persist_and_refresh(feature_page):
    _window, settings, hooks, controls = feature_page
    controls["New task position"].set_selected(1)  # Bottom
    assert settings.settings.todo.todoNewTaskPosition == "Bottom"
    assert hooks.calls["refresh_feature"][-1] == "Todo"
    assert hooks.calls["save"] >= 1

    controls["Show completed tasks"].set_active(False)
    assert settings.settings.todo.todoShowCompletedTasks is False
    assert hooks.calls["refresh_feature"][-1] == "Todo"


def test_quick_capture_recent_limit_spin(feature_page):
    _window, settings, _hooks, controls = feature_page
    controls["Recent item limit"].set_value(50)
    assert settings.settings.quickCapture.quickCaptureRecentLimit == 50


def test_weather_skin_and_show_switch(feature_page):
    _window, settings, hooks, controls = feature_page
    controls["Skin"].set_selected(1)  # Rich
    assert settings.settings.weather.weatherSkin == "Rich"
    assert hooks.calls["refresh_feature"][-1] == "Weather"
    controls["Show Forecast"].set_active(False)
    assert settings.settings.weather.weatherShowForecast is False


def test_music_display_mode(feature_page):
    _window, settings, hooks, controls = feature_page
    controls["Display mode"].set_selected(2)  # Controls
    assert settings.settings.music.musicDisplayMode == "Controls"
    assert hooks.calls["refresh_feature"][-1] == "Music"


def test_search_fields_and_hotkey_status(feature_page):
    window, settings, hooks, controls = feature_page
    controls["Max search results"].set_value(200)
    assert settings.settings.search.searchMaxResults == 200
    assert hooks.calls["refresh_feature"][-1] == "Search"

    controls["Search Hotkey"].set_active(True)
    assert settings.settings.search.searchHotkeyEnabled is True
    label = window._search_hotkey_status_label.get_text()
    assert "Hotkey enabled" in label
    assert "Alt+D" in label  # default gesture rendered

    settings.settings.search.searchHotkeyModifiers = 0x4  # Ctrl
    settings.settings.search.searchHotkeyKey = 0x71  # q
    window._reset_search_hotkey()
    assert settings.settings.search.searchHotkeyModifiers == 8
    assert settings.settings.search.searchHotkeyKey == 0x64
    assert "Alt+D" in window._search_hotkey_status_label.get_text()

    controls["Default result tab"].set_selected(1)  # app
    assert settings.settings.search.searchDefaultTab == "app"


# ---- Widget groups settings page ------------------------------------------------


class FakeGroupHooks(FakeHooks):
    def __init__(self):
        super().__init__()
        self.calls["refresh_groups"] = 0
        self.calls["dissolve_all_groups"] = 0
        self.refresh_groups = lambda: self.calls.__setitem__("refresh_groups", self.calls["refresh_groups"] + 1)
        self.dissolve_all_groups = lambda: (
            self.calls.__setitem__("dissolve_all_groups", self.calls["dissolve_all_groups"] + 1) or True
        )


@pytest.fixture()
def groups_page(tmp_path):
    i18n.set_language("en-US")
    settings = SettingsService(config_root=tmp_path / "config")
    settings.load()
    hooks = FakeGroupHooks()
    window = SettingsWindow(None, settings, hooks)
    window.show_section("WidgetGroups")
    page = window._stack.get_child_by_name("WidgetGroups")
    controls = _row_controls(page)
    yield window, settings, hooks, controls
    window.destroy()
    i18n.set_language(None)


def test_groups_page_defaults_and_dissolve(groups_page):
    window, settings, hooks, controls = groups_page
    assert "WidgetGroups" in window._nav_rows
    assert window._stack.get_visible_child_name() == "WidgetGroups"

    layout = settings.layout
    controls["Title bar layout"].set_selected(1)  # Collapsed (Stack)
    assert layout.widgetGroupDefaultNavigationStyle == "Stack"
    controls["Group title style"].set_selected(2)  # Text only
    assert layout.widgetGroupDefaultTitleDisplayMode == "TextOnly"
    controls["Allow title-bar wheel switching"].set_active(False)
    assert layout.widgetGroupWheelSwitchEnabled is False
    controls["Switch tabs on pointer hover"].set_active(True)
    assert layout.widgetGroupHoverSwitchEnabled is True
    assert hooks.calls["refresh_groups"] >= 4  # live strips refresh on change

    # Dissolve-all routes through the hook (the manager owns live windows).
    controls["Dissolve widget group"].emit("clicked")
    assert hooks.calls["dissolve_all_groups"] == 1
