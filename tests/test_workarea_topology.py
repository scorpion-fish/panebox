"""Monitor topology: signatures, profile find/seed/prune, reprojection.

Pure logic on hand-built MonitorInfo lists — no display required.
"""

from __future__ import annotations

from panebox.constants import MAX_TOPOLOGY_PROFILES
from panebox.models.layout_slice import WidgetLayoutSettingsSlice, WidgetSurfaceLayoutProfile
from panebox.platform.workarea import (
    MonitorInfo,
    TopologyLayoutService,
    find_monitor_at,
    primary_monitor,
    topology_signature,
    topology_signature_from_profiles,
)


def monitor(connector="DP-1", x=0, y=0, w=1920, h=1080, scale=1.0, primary=True):
    info = MonitorInfo(
        connector=connector,
        x=x,
        y=y,
        width=w,
        height=h,
        scale=scale,
        is_primary=primary,
        work_x=x,
        work_y=y,
        work_width=w,
        work_height=h,
    )
    return info


# ---- MonitorInfo ------------------------------------------------------------


def test_stable_id_generic_connectors_fall_back_to_geometry():
    assert monitor(connector="DP-1").stable_id() == "DP-1"
    assert monitor(connector="").stable_id() == "geometry-only"
    assert monitor(connector="unknown").stable_id() == "geometry-only"
    assert monitor(connector=r"\\.\DISPLAY3").stable_id() == "geometry-only"


def test_clamp_rect_keeps_widget_inside_work_area():
    info = monitor(x=100, y=50, w=800, h=600)
    assert info.clamp_rect(20, 10, 200, 100) == (100, 50)
    assert info.clamp_rect(900, 700, 200, 100) == (700, 550)
    assert info.clamp_rect(400, 300, 200, 100) == (400, 300)


def test_find_monitor_at_and_primary():
    monitors = [monitor(connector="DP-1"), monitor(connector="HDMI-1", x=1920, primary=False)]
    assert find_monitor_at(monitors, 500, 500) is monitors[0]
    assert find_monitor_at(monitors, 2000, 500) is monitors[1]
    # Off-screen points fall back to the first monitor (never None).
    assert find_monitor_at(monitors, 9999, 9999) is monitors[0]
    assert find_monitor_at([], 0, 0) is None
    assert primary_monitor(monitors) is monitors[0]


# ---- signatures ---------------------------------------------------------------


def test_signature_is_order_independent_and_geometry_sensitive():
    a, b = monitor(connector="DP-1"), monitor(connector="HDMI-1", x=1920, primary=False)
    same_shuffled = [b, a]
    assert topology_signature([a, b]) == topology_signature(same_shuffled)

    moved = monitor(connector="HDMI-1", x=1930, primary=False)
    assert topology_signature([a, b]) != topology_signature([a, moved])


def test_profile_signature_round_trips_monitor_signature():
    monitors = [monitor(), monitor(connector="HDMI-1", x=1920, primary=False)]
    key = topology_signature(monitors)
    profile_key = topology_signature_from_profiles(
        TopologyLayoutService(WidgetLayoutSettingsSlice()).capture_current(monitors)[1]
    )
    assert key == profile_key, "saved profiles must match live topologies (verbatim-restore path)"


# ---- profile service -----------------------------------------------------------


def dual_monitor_layout():
    layout = WidgetLayoutSettingsSlice()
    service = TopologyLayoutService(layout)
    monitors = [monitor(), monitor(connector="HDMI-1", x=1920, primary=False)]
    return layout, service, monitors


def test_seed_and_find_compatible_round_trip():
    layout, service, monitors = dual_monitor_layout()
    surfaces = {"w1": WidgetSurfaceLayoutProfile(x=100, y=100, width=300, height=400)}
    key, _profile = service.seed_profile(monitors, surfaces)
    assert layout.activeWidgetTopologyKey == key

    found = service.find_compatible(monitors)
    assert found is not None
    assert "w1" in found.surfaces

    # Different topology → no match.
    other = [monitor(connector="DP-2", w=2560, h=1440)]
    assert service.find_compatible(other) is None


def test_seed_profile_updates_existing_key_in_place():
    layout, service, monitors = dual_monitor_layout()
    service.seed_profile(monitors, {"w1": WidgetSurfaceLayoutProfile(x=1, y=1, width=50, height=50)})
    service.seed_profile(monitors, {"w2": WidgetSurfaceLayoutProfile(x=2, y=2, width=60, height=60)})
    assert len(layout.widgetTopologyLayouts) == 1
    profile = next(iter(layout.widgetTopologyLayouts.values()))
    assert set(profile.surfaces) == {"w1", "w2"}


def test_prune_keeps_active_profile():
    layout, service, monitors = dual_monitor_layout()
    from panebox.models.layout_slice import WidgetTopologyLayoutProfile

    for index in range(MAX_TOPOLOGY_PROFILES + 2):
        layout.widgetTopologyLayouts[f"v3-{index:012d}"] = WidgetTopologyLayoutProfile(
            lastUsedAtUtc=f"2026-01-{index + 1:02d}T00:00:00Z"
        )
    active = "v3-000000000013"
    layout.activeWidgetTopologyKey = active
    service.prune_profiles()
    assert len(layout.widgetTopologyLayouts) == MAX_TOPOLOGY_PROFILES
    assert active in layout.widgetTopologyLayouts


# ---- reprojection ----------------------------------------------------------------


def test_reproject_moves_surfaces_between_monitors_proportionally():
    layout, service, monitors = dual_monitor_layout()
    # Surface in the middle-left of the primary monitor.
    rects = {"w1": (480, 290, 300, 400)}
    out = service.reproject(monitors, rects)
    x, y, w, h = out["w1"]
    # Reprojection is fractional (of the space left beside the widget), so the
    # exact x drifts, but the rect stays inside the primary monitor and keeps size.
    assert 0 <= x <= 1920 - w and 0 <= y <= 1080 - h
    assert (w, h) == (300, 400)

    # Now a single larger monitor replaces the pair: surface must land inside it.
    bigger = [monitor(connector="DP-9", w=3840, h=2160)]
    out = service.reproject(bigger, rects)
    x, y, w, h = out["w1"]
    assert 0 <= x <= 3840 - w and 0 <= y <= 2160 - h
    assert w <= 3840 * 0.95 and h <= 2160 * 0.95
