"""Widget group integration tests under Xvfb: merge two widgets into one
group surface, switch members (tabs / wheel / Ctrl+Tab path), reorder, detach
and dissolve back to standalone windows."""

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
from panebox.models.widget_config import WidgetConfig  # noqa: E402
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
def env():
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


def _make(manager, settings, name, kind="Todo", x=200, y=200):
    config = WidgetConfig(name=name, widgetKind=kind, x=x, y=y, width=360, height=520)
    settings.add_widget(config)
    runtime = manager.create_runtime(config, x, y, 360, 520)
    assert settle(lambda rt=runtime: rt.window.xid != 0)
    return config, runtime


def _visible_ids(manager):
    return sorted(rt.config.id for rt in manager.runtimes.values() if rt.window.get_visible())


def test_merge_creates_one_surface(env):
    manager, settings = env
    a, rt_a = _make(manager, settings, "Alpha")
    b, rt_b = _make(manager, settings, "Beta", x=400)

    assert manager.merge_widgets(a.id, b.id) is True  # b is the destination
    group = manager.group_of(b.id)
    assert group is not None
    assert group.memberIds == [b.id, a.id]
    assert group.activeMemberId == b.id
    assert group.surfaceId
    # One visible surface: the destination's window; the source is hidden.
    assert rt_b.window.get_visible() is True
    assert rt_a.window.get_visible() is False
    assert manager.is_surface_window(b.id) and not manager.is_surface_window(a.id)
    # Both configs carry the group geometry.
    assert a.x == b.x and a.y == b.y

    settings.flush_pending_save()
    layout = json.loads(next(Path(settings.config_root).glob("widget-layout*.json")).read_text())
    groups = layout["layout"]["widgetGroups"]
    assert len(groups) == 1 and set(groups[0]["memberIds"]) == {a.id, b.id}


def test_merge_rejects_same_group_and_cap(env):
    manager, settings = env
    a, _ = _make(manager, settings, "A")
    b, _ = _make(manager, settings, "B")
    manager.merge_widgets(a.id, b.id)
    assert manager.merge_widgets(a.id, b.id) is False  # already together

    # Grow the group to the 8-member cap; the next merge is rejected.
    for index in range(6):
        extra, _ = _make(manager, settings, f"W{index}")
        assert manager.merge_widgets(extra.id, b.id) is True
    group = manager.group_of(b.id)
    assert len(group.memberIds) == 8
    outsider, _ = _make(manager, settings, "Outsider")
    assert manager.merge_widgets(outsider.id, b.id) is False  # cap reached


def test_join_targets_lists_other_widgets(env):
    manager, settings = env
    a, _ = _make(manager, settings, "Solo")
    b, _ = _make(manager, settings, "Pair1")
    c, _ = _make(manager, settings, "Pair2")
    manager.merge_widgets(b.id, c.id)

    targets = manager.join_targets(a.id)
    ids = [t["target_id"] for t in targets]
    assert c.id in ids  # the group exposes its active member
    target = next(t for t in targets if t["target_id"] == c.id)
    assert target["member_count"] == 2
    assert b.id not in ids  # non-active members are not offered
    # From inside the group, the group's own members are not targets.
    inner_ids = [t["target_id"] for t in manager.join_targets(b.id)]
    assert a.id in inner_ids and c.id not in inner_ids and b.id not in inner_ids


def test_switch_member_swaps_surface_window(env):
    manager, settings = env
    a, rt_a = _make(manager, settings, "Alpha")
    b, rt_b = _make(manager, settings, "Beta")
    manager.merge_widgets(a.id, b.id)
    x, y, w, h = rt_b.window.window_geometry()

    assert manager.switch_group_member(a.id) is True
    group = manager.group_of(a.id)
    assert group.activeMemberId == a.id
    assert rt_a.window.get_visible() is True
    assert rt_b.window.get_visible() is False
    sx, sy, sw, sh = rt_a.window.window_geometry()
    assert (sx, sy, sw, sh) == (x, y, w, h)  # surface geometry preserved

    # Wheel-step path: two +1 steps wrap around to the destination member.
    assert manager.switch_group_relative(a.id, +1, wrap=True) is True
    assert manager.group_of(a.id).activeMemberId == b.id


def test_tabs_strip_present_for_active_member(env):
    manager, settings = env
    a, rt_a = _make(manager, settings, "Alpha")
    b, rt_b = _make(manager, settings, "Beta")
    manager.merge_widgets(a.id, b.id)

    # Default style is Tabs: active member's shell hosts the strip.
    strip = getattr(rt_b, "group_strip", None)
    assert strip is not None
    assert strip.tabs_box.get_visible() is True
    assert strip.position_label.get_visible() is False
    labels = [child.get_label() for child in strip.tabs_box]
    assert len(labels) == 2

    manager.set_group_navigation_style(b.id, "Stack")
    strip = getattr(rt_b, "group_strip", None)
    assert strip.tabs_box.get_visible() is False
    assert strip.position_label.get_visible() is True
    assert strip.position_label.get_text() == "1 / 2"

    manager.set_group_title_display_mode(b.id, "IconOnly")
    manager.set_group_navigation_style(b.id, "Tabs")
    strip = getattr(rt_b, "group_strip", None)
    labels = [child.get_label() for child in strip.tabs_box]
    assert all(" " not in label for label in labels)  # glyph only


def test_reorder_moves_member_slot(env):
    manager, settings = env
    a, _ = _make(manager, settings, "A")
    b, _ = _make(manager, settings, "B")
    c, _ = _make(manager, settings, "C")
    manager.merge_widgets(a.id, b.id)
    manager.merge_widgets(c.id, b.id)  # group: [b, a, c]

    assert manager.reorder_group_member(c.id, b.id) is True
    assert manager.group_of(b.id).memberIds == [c.id, b.id, a.id]
    assert manager.reorder_group_member(c.id, c.id) is False


def test_detach_and_dissolve(env):
    manager, settings = env
    a, rt_a = _make(manager, settings, "A")
    b, rt_b = _make(manager, settings, "B")
    c, rt_c = _make(manager, settings, "C")
    manager.merge_widgets(a.id, b.id)
    manager.merge_widgets(c.id, b.id)
    group = manager.group_of(b.id)
    gx, _gy = group.x, group.y

    # Detach the active member (b, slot 0 → cascade step 1): the group
    # survives with the next member.
    assert manager.remove_widget_from_group(b.id) is True
    group = manager.group_of(a.id)
    assert group is not None and len(group.memberIds) == 2
    assert b.id not in group.memberIds
    assert rt_b.window.get_visible() is True  # detached becomes standalone
    bx, by, _bw, _bh = rt_b.window.window_geometry()
    assert abs(bx - (gx + 1 * 24)) <= 1  # cascade placement (slot 0 → step 1)
    assert group.activeMemberId in (a.id, c.id)

    # Dissolve: every member returns to its own window.
    assert manager.dissolve_group_containing(a.id) is True
    assert manager.group_of(a.id) is None
    assert rt_a.window.get_visible() is True
    assert rt_c.window.get_visible() is True
    assert _visible_ids(manager) == sorted([a.id, b.id, c.id])


def test_close_grouped_member_shrinks_group(env):
    manager, settings = env
    a, rt_a = _make(manager, settings, "A")
    b, rt_b = _make(manager, settings, "B")
    c, rt_c = _make(manager, settings, "C")
    manager.merge_widgets(a.id, b.id)
    manager.merge_widgets(c.id, b.id)

    manager.close_widget(a.id)
    group = manager.group_of(b.id)
    assert group is not None and group.memberIds == [b.id, c.id]
    manager.close_widget(c.id)
    assert manager.group_of(b.id) is None  # under two members → dissolved
    assert rt_b.window.get_visible() is True


def test_restore_groups_after_restart(env):
    from panebox.services import feature_widgets

    manager, settings = env
    feature_widgets.set_enabled(settings.layout, "Todo", True)  # restore gate
    a, rt_a = _make(manager, settings, "A")
    b, rt_b = _make(manager, settings, "B")
    manager.merge_widgets(a.id, b.id)
    settings.flush_pending_save()
    for runtime in list(manager.runtimes.values()):
        runtime.window.destroy()
    manager.runtimes.clear()

    manager2 = WidgetManager(settings)
    manager2.capture_geometries = lambda: None
    manager2._notify_widgets_changed = lambda: None
    manager2.restore_widgets()
    assert settle(
        lambda: (
            manager2.runtimes
            and all(rt.window.xid != 0 or not rt.window.get_visible() for rt in manager2.runtimes.values())
        )
    )
    group = manager2.group_of(b.id)
    assert group is not None and group.activeMemberId == b.id
    rt_a2, rt_b2 = manager2.runtimes[a.id], manager2.runtimes[b.id]
    assert rt_b2.window.get_visible() is True  # only the active member maps
    assert rt_a2.window.get_visible() is False
    for runtime in list(manager2.runtimes.values()):
        try:
            manager2.close_widget(runtime.config.id)
        except Exception:
            pass
