"""Monitor enumeration, per-monitor work areas, and topology-keyed layouts.

Work areas = monitor geometry minus dock struts (_NET_WM_STRUT_PARTIAL of
_NET_WM_WINDOW_TYPE_DOCK windows). Root _NET_WORKAREA is only a union rect on
GNOME, useless multi-monitor, so struts are parsed per dock window.

The topology signature mirrors WidgetTopologyLayoutService: SHA-256 over the
sorted per-monitor render (stableId;isPrimary;dpiScale;x,y,w,h), work areas
excluded so taskbar changes do not fragment profiles.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from ..constants import MAX_TOPOLOGY_PROFILES
from ..models.layout_slice import (
    WidgetSurfaceLayoutProfile,
    WidgetTopologyLayoutProfile,
    WidgetTopologyMonitorProfile,
)
from . import x11


@dataclass
class MonitorInfo:
    connector: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    scale: float = 1.0
    is_primary: bool = False
    # work area (after struts)
    work_x: int = 0
    work_y: int = 0
    work_width: int = 0
    work_height: int = 0

    def stable_id(self) -> str:
        if self.connector and not self._is_generic_connector():
            return self.connector
        return "geometry-only"

    def _is_generic_connector(self) -> bool:
        return (
            not self.connector
            or self.connector in ("unknown", "None")
            or re.match(r"^\\\\\.\\DISPLAY\d+$", self.connector)
        )

    def contains_point(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def contains_rect(self, x: float, y: float, w: float, h: float) -> bool:
        return (
            x >= self.work_x
            and y >= self.work_y
            and x + w <= self.work_x + self.work_width
            and y + h <= self.work_y + self.work_height
        )

    def clamp_rect(self, x: float, y: float, w: float, h: float) -> tuple[float, float]:
        """Clamp a rect to stay inside this monitor's work area (keeps size)."""
        x = min(max(x, self.work_x), self.work_x + max(0, self.work_width - w))
        y = min(max(y, self.work_y), self.work_y + max(0, self.work_height - h))
        return x, y

    def proportional_rect(self, fx: float, fy: float, fw: float, fh: float) -> tuple[float, float, float, float]:
        """Proportional in-bounds rect (replacement/differently-scaled monitor)."""
        w = min(fw, self.work_width * 0.95)
        h = min(fh, self.work_height * 0.95)
        x = self.work_x + fx * max(0.0, self.work_width - w)
        y = self.work_y + fy * max(0.0, self.work_height - h)
        return x, y, w, h


def list_monitors() -> list[MonitorInfo]:
    """Live monitor list from GDK (requires GTK initialized)."""
    import gi

    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None:
        return [MonitorInfo(width=1280, height=800, work_width=1280, work_height=800, is_primary=True)]
    monitors: list[MonitorInfo] = []
    for monitor in list(display.get_monitors()):
        geo = monitor.get_geometry()
        scale = monitor.get_scale_factor()
        connector = ""
        try:
            connector = monitor.get_connector() or ""
        except AttributeError:
            connector = ""
        info = MonitorInfo(
            connector=connector,
            x=geo.x,
            y=geo.y,
            width=geo.width,
            height=geo.height,
            scale=float(scale),
        )
        info.work_x, info.work_y = info.x, info.y
        info.work_width, info.work_height = info.width, info.height
        monitors.append(info)
    # GDK4 dropped the primary-monitor concept; the X root origin (0,0) is the
    # conventional primary (RandR primary sits at 0,0 by default).
    for m in monitors:
        if m.x == 0 and m.y == 0:
            m.is_primary = True
            break
    if monitors and not any(m.is_primary for m in monitors):
        monitors[0].is_primary = True
    _apply_struts(monitors)
    return monitors


def _apply_struts(monitors: list[MonitorInfo]) -> None:
    try:
        conn = x11.shared_connection()
    except x11.X11Error:
        return
    root = conn.root_window()
    clients = conn.get_property_windows(root, x11.ATOM_CLIENT_LIST)
    for xid in clients:
        try:
            types = conn.get_property_atoms(xid, x11.ATOM_WINDOW_TYPE)
        except Exception:
            continue
        if x11.ATOM_WINDOW_TYPE_DOCK not in types:
            continue
        struts = conn.get_property_cardinals(xid, x11.ATOM_STRUT_PARTIAL)
        if len(struts) < 12:
            struts = conn.get_property_cardinals(xid, "_NET_WM_STRUT")
            if len(struts) < 4:
                continue
            struts = struts[:4] + [0, 0, 0, 0, 0, 0, 0, 0]
        left, right, top, bottom = struts[0], struts[1], struts[2], struts[3]
        for info in monitors:
            before = (info.work_x, info.work_y, info.work_width, info.work_height)
            # Apply only the strut edges that touch this monitor's span.
            if left and info.x == 0 and info.work_x < left:
                info.work_x = left
                info.work_width = max(0, info.work_width - (left - before[0]))
            if top and info.y == 0 and info.work_y < top:
                info.work_y = top
                info.work_height = max(0, info.work_height - (top - before[1]))
            if right:
                right_edge = max(m.x + m.width for m in monitors)
                desired = right_edge - right
                if info.x + info.width == right_edge and info.work_x + info.work_width > desired:
                    info.work_width = max(0, desired - info.work_x)
            if bottom:
                bottom_edge = max(m.y + m.height for m in monitors)
                desired = bottom_edge - bottom
                if info.y + info.height == bottom_edge and info.work_y + info.work_height > desired:
                    info.work_height = max(0, desired - info.work_y)


def monitor_profiles(monitors: list[MonitorInfo]) -> list[WidgetTopologyMonitorProfile]:
    return [
        WidgetTopologyMonitorProfile(
            stableId=m.stable_id(),
            deviceName=m.connector,
            isPrimary=m.is_primary,
            monitorX=m.x,
            monitorY=m.y,
            monitorWidth=m.width,
            monitorHeight=m.height,
            workAreaX=m.work_x,
            workAreaY=m.work_y,
            workAreaWidth=m.work_width,
            workAreaHeight=m.work_height,
            dpiScale=m.scale,
        )
        for m in monitors
    ]


def topology_signature(monitors: list[MonitorInfo]) -> str:
    """v3-style key: sha256 over sorted monitor renders; work area excluded."""

    def render(m: MonitorInfo) -> str:
        return f"{m.stable_id()};{int(m.is_primary)};{m.scale:.3f};{m.x},{m.y},{m.width},{m.height}"

    signature = "|".join(sorted(render(m) for m in monitors))
    digest = hashlib.sha256(signature.encode()).hexdigest()[:12]
    return f"v3-{digest}"


def find_monitor_at(monitors: list[MonitorInfo], x: float, y: float) -> Optional[MonitorInfo]:
    for m in monitors:
        if m.contains_point(x, y):
            return m
    return monitors[0] if monitors else None


def primary_monitor(monitors: list[MonitorInfo]) -> MonitorInfo:
    for m in monitors:
        if m.is_primary:
            return m
    return monitors[0]


class TopologyLayoutService:
    """Capture/find/seed/apply topology layout profiles on a layout slice."""

    def __init__(self, layout):
        self.layout = layout  # WidgetLayoutSettingsSlice

    def capture_current(self, monitors: list[MonitorInfo]) -> tuple[str, WidgetTopologyLayoutProfile]:
        key = topology_signature(monitors)
        profile = WidgetTopologyLayoutProfile(monitors=monitor_profiles(monitors))
        return key, profile

    def find_compatible(self, monitors: list[MonitorInfo]) -> Optional[WidgetTopologyLayoutProfile]:
        key = topology_signature(monitors)
        profile = self.layout.widgetTopologyLayouts.get(key)
        if profile is not None:
            return profile
        # Signature equality only; newest first.
        candidates = sorted(
            self.layout.widgetTopologyLayouts.values(),
            key=lambda p: p.lastUsedAtUtc or "",
            reverse=True,
        )
        for candidate in candidates:
            if topology_signature_from_profiles(candidate) == key:
                return candidate
        return None

    def seed_profile(
        self, monitors: list[MonitorInfo], surfaces: dict[str, WidgetSurfaceLayoutProfile]
    ) -> tuple[str, WidgetTopologyLayoutProfile]:
        key, profile = self.capture_current(monitors)
        saved = self.layout.widgetTopologyLayouts.get(key)
        if saved is not None:
            saved.monitors = profile.monitors
            saved.surfaces.update(surfaces)
            profile = saved
        else:
            profile.surfaces = dict(surfaces)
        self.layout.widgetTopologyLayouts[key] = profile
        self.layout.activeWidgetTopologyKey = key
        self.prune_profiles()
        return key, profile

    def prune_profiles(self) -> None:
        if len(self.layout.widgetTopologyLayouts) <= MAX_TOPOLOGY_PROFILES:
            return
        ordered = sorted(
            self.layout.widgetTopologyLayouts.items(),
            key=lambda kv: kv[1].lastUsedAtUtc or "",
            reverse=True,
        )
        keep = dict(ordered[:MAX_TOPOLOGY_PROFILES])
        if self.layout.activeWidgetTopologyKey not in keep:
            keep[ordered[MAX_TOPOLOGY_PROFILES - 1][0]] = ordered[MAX_TOPOLOGY_PROFILES - 1][1]
        self.layout.widgetTopologyLayouts = keep

    def reproject(
        self,
        monitors: list[MonitorInfo],
        surface_sizes: dict[str, tuple[float, float, float, float]],
    ) -> dict[str, tuple[float, float, float, float]]:
        """Map saved surface rects onto the current monitor set.

        surface_sizes: surfaceId -> (x, y, w, h) currently known. Returns
        surfaceId -> (x, y, w, h) clamped/proportional for the current set.
        """
        out: dict[str, tuple[float, float, float, float]] = {}
        for surface_id, (x, y, w, h) in surface_sizes.items():
            source = find_monitor_at(monitors, x, y) or primary_monitor(monitors)
            fx = _relative(source.work_width, x - source.work_x)
            fy = _relative(source.work_height, y - source.work_y)
            target_rect = source.proportional_rect(fx, fy, w, h)
            out[surface_id] = target_rect
        return out


def _relative(total: float, value: float) -> float:
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, value / total))


def topology_signature_from_profiles(profile: WidgetTopologyLayoutProfile) -> str:
    def render(m: WidgetTopologyMonitorProfile) -> str:
        stable = m.stableId or "geometry-only"
        geometry = f"{m.monitorX},{m.monitorY},{m.monitorWidth},{m.monitorHeight}"
        return f"{stable};{int(m.isPrimary)};{m.dpiScale:.3f};{geometry}"

    signature = "|".join(sorted(render(m) for m in profile.monitors))
    return f"v3-{hashlib.sha256(signature.encode()).hexdigest()[:12]}"
