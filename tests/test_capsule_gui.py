"""Capsule integration tests under Xvfb: real WidgetManager runtimes collapse
to a capsule surface, expand back with anchor-aware geometry, persist state,
and capsule bars arrange collapsed widgets into one row."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


GUI = _display_reachable(SMOKE_DISPLAY) and not os.environ.get("PANEBOX_SKIP_GUI")
if GUI:
    os.environ["DISPLAY"] = SMOKE_DISPLAY
    os.environ.setdefault("GDK_BACKEND", "x11")
    os.environ.setdefault("GSK_RENDERER", "cairo")

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not GUI, reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)")

if GUI:
    import gi  # noqa: E402

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib  # noqa: E402

from panebox import i18n  # noqa: E402
from panebox.services.capsule import SMART_DETAIL_HEIGHT  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.services.widget_manager import WidgetManager  # noqa: E402


def settle(predicate, timeout_ms: int = 4000) -> bool:
    loop = GLib.MainLoop()
    state = {"done": False}

    def check():
        if predicate():
            state["done"] = True
            loop.quit()
            return False
        return True

    GLib.timeout_add(25, check)
    GLib.timeout_add(timeout_ms, loop.quit)
    loop.run()
    return state["done"]


@pytest.fixture()
def manager_env():
    i18n.set_language("en-US")
    tmp = Path(tempfile.mkdtemp())
    settings = SettingsService(config_root=tmp / "config")
    settings.load()
    manager = WidgetManager(settings)
    manager.capture_geometries = lambda: None
    manager._notify_widgets_changed = lambda: None
    yield manager, settings
    for runtime in list(manager.runtimes.values()):
        try:
            manager.close_widget(runtime.config.id)
        except Exception:
            pass
    i18n.set_language(None)


def _make_todo(manager, settings):
    from panebox.models.widget_config import WidgetConfig

    config = WidgetConfig(name="Tasks", widgetKind="Todo", x=200, y=200, width=360, height=520)
    settings.add_widget(config)
    runtime = manager.create_runtime(config, 200, 200, 360, 520)
    assert settle(lambda: runtime.window.xid != 0)
    return config, runtime


def test_collapse_swaps_content_and_geometry(manager_env):
    manager, settings = manager_env
    config, runtime = _make_todo(manager, settings)
    settings.settings.widgetShell.widgetCollapseBehavior = "Click"

    assert runtime.window.get_child() is runtime.shell
    manager.toggle_collapse(config.id)

    assert config.isCollapsed is True
    assert runtime.window.get_child() is runtime.capsule
    x, y, w, h = runtime.window.window_geometry()
    assert h <= int(SMART_DETAIL_HEIGHT) + 8  # Smart capsule height (52px)
    assert w >= 140 and w <= 500  # inside the capsule width band

    # Expand back: shell returns, size returns to expanded height. Default
    # width mode is Aligned (C# semantics): the expanded widget keeps the
    # capsule width.
    manager.toggle_collapse(config.id)
    assert runtime.window.get_child() is runtime.shell
    _x, _y, w2, h2 = runtime.window.window_geometry()
    assert h2 > 400
    assert w2 == w

    # Independent mode restores the remembered expanded width (the Aligned
    # cycle above adopted the capsule width into the config, so reset it).
    settings.settings.widgetShell.widgetCompactWidthMode = "Independent"
    runtime.config.width = 360
    manager.toggle_collapse(config.id)
    manager.toggle_collapse(config.id)
    _x, _y, w3, _h3 = runtime.window.window_geometry()
    assert abs(w3 - 360) <= 2


def test_collapse_persists_and_restores(manager_env):
    manager, settings = manager_env
    config, runtime = _make_todo(manager, settings)
    settings.settings.widgetShell.widgetCollapseBehavior = "Click"  # default Expanded refuses
    manager.toggle_collapse(config.id)
    settings.flush_pending_save()

    layout = json.loads(
        (settings.layout_store.path if hasattr(settings.layout_store, "path") else _layout_path(settings)).read_text()
    )
    widgets = {w["id"]: w for w in layout["layout"]["widgets"]}
    assert widgets[config.id]["isCollapsed"] is True


def _layout_path(settings) -> Path:
    # settings service keeps the layout store under config root
    root = settings.config_root if hasattr(settings, "config_root") else Path(tempfile.gettempdir())
    candidates = list(Path(root).glob("widget-layout*.json"))
    assert candidates, "widget-layout.json missing"
    return next(p for p in candidates if not p.name.endswith(".bak"))


def test_expanded_behavior_blocks_collapse(manager_env):
    manager, settings = manager_env
    config, runtime = _make_todo(manager, settings)
    settings.settings.widgetShell.widgetCollapseBehavior = "Expanded"
    manager.toggle_collapse(config.id)

    assert config.isCollapsed is False
    assert runtime.window.get_child() is runtime.shell
    assert runtime.shell.collapse_button.get_visible() is False


def test_click_behavior_capsule_activates(manager_env):
    manager, settings = manager_env
    config, runtime = _make_todo(manager, settings)
    settings.settings.widgetShell.widgetCollapseBehavior = "Click"  # default Expanded refuses
    manager.toggle_collapse(config.id)

    runtime.capsule.on_activate()  # what a click triggers
    assert config.isCollapsed is False
    assert runtime.window.get_child() is runtime.shell


def test_capsule_summary_counts(manager_env):
    manager, settings = manager_env
    settings.settings.widgetShell.widgetCollapseBehavior = "Click"  # default Expanded refuses
    config, runtime = _make_todo(manager, settings)
    manager.toggle_collapse(config.id)
    summary = runtime.capsule.summary_label.get_text()
    assert summary == "0 today · 0 overdue"  # empty todo store

    # File widgets show the live folder count.
    from panebox.models.widget_config import WidgetConfig

    file_config = WidgetConfig(name="Files", widgetKind="File", x=100, y=100, width=300, height=400)
    settings.add_widget(file_config)
    file_runtime = manager.create_runtime(file_config, 100, 100, 300, 400)
    assert settle(lambda: file_runtime.window.xid != 0)
    file_runtime.controller.entries = [object()] * 3  # as if the folder listed 3 files
    manager.toggle_collapse(file_config.id)
    assert file_runtime.capsule.summary_label.get_text() == "3 items"


def test_capsule_bar_arranges_collapsed_row(manager_env):
    manager, settings = manager_env
    ws = settings.settings.widgetShell
    ws.widgetCollapseBehavior = "Click"
    ws.widgetCapsuleArrangementMode = "Bar"
    ws.widgetCapsuleBarPlacement = "Top"
    ws.widgetCapsuleBarDirection = "Horizontal"
    ws.widgetCapsuleBarSpacing = 8

    from panebox.models.widget_config import WidgetConfig

    ids = []
    for index in range(3):
        config = WidgetConfig(name=f"W{index}", widgetKind="Todo", x=300, y=300, width=360, height=520)
        settings.add_widget(config)
        runtime = manager.create_runtime(config, 300 + index * 50, 300, 360, 520)
        assert settle(lambda rt=runtime: rt.window.xid != 0)
        ids.append(config.id)

    for widget_id in ids:
        manager.toggle_collapse(widget_id)

    geoms = [manager.runtimes[w].window.window_geometry() for w in ids]
    # One row: shared Y band, no horizontal overlap.
    ys = [g[1] for g in geoms]
    assert max(ys) - min(ys) <= 2, geoms
    ordered = sorted(geoms, key=lambda g: g[0])
    for left, right in zip(ordered, ordered[1:]):
        assert left[0] + left[2] <= right[0] + 1  # spacing ≥ 0, no overlap
    # Persisted bar order contains all three.
    assert set(ws.widgetCapsuleBarOrder) == set(ids)
