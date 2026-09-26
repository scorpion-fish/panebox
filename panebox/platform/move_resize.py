"""Interactive move + resize with snapping.

GTK4's begin_move/begin_resize need a cooperating WM (they send root
 ClientMessages); widgets live on the desktop layer and need snap-aware
positioning anyway, so both interactions run manually: GestureDrag deltas
applied through XMoveResizeWindow, resolved against the snap calculator on
every update (engage 8 DIP, release 12 DIP, spacing from settings, ×scale).
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..constants import WIDGET_MIN_HEIGHT, WIDGET_MIN_WIDTH
from ..services import snap
from . import x11

EDGE_HIT_PX = 12

Rect = snap.Rect


class MoveResizeController:
    """Attach to a WidgetWindow: `attach(title_bar, content)`."""

    def __init__(
        self,
        window,
        snap_enabled: Callable[[], bool],
        snap_spacing: Callable[[], float],
        work_area: Callable[[], Optional[Rect]],
        snap_targets: Callable[[], List[snap.SnapTarget]],
        on_geometry_committed: Optional[Callable[[int, int, int, int], None]] = None,
        move_locked: Callable[[], bool] = lambda: False,
        size_locked: Callable[[], bool] = lambda: False,
    ):
        self.window = window
        self.snap_enabled = snap_enabled
        self.snap_spacing = snap_spacing
        self.work_area = work_area
        self.snap_targets = snap_targets
        self.on_geometry_committed = on_geometry_committed
        self.move_locked = move_locked
        self.size_locked = size_locked

        self._move_active = False
        self._move_origin = (0, 0)
        self._content: Optional[Gtk.Widget] = None
        self._sticky_h: Optional[snap.SnapMatch] = None
        self._sticky_v: Optional[snap.SnapMatch] = None
        self._resize_active = False
        self._resize_edge: Optional[str] = None
        self._resize_start = (0, 0, 0, 0)
        # Offset from the root-truth pointer position to the window origin,
        # captured at drag begin. Gesture dx/dy are NOT used for positioning:
        # after an XMoveWindow, GDK translates later motion events against the
        # new origin, so gesture deltas feed back into themselves.
        self._move_grab_offset: Optional[Tuple[int, int]] = None
        self._resize_pointer_start: Optional[Tuple[int, int]] = None

    # ---- attach --------------------------------------------------------------

    def attach(self, title_bar: Gtk.Widget, content: Gtk.Widget) -> None:
        # Gesture coordinates are content-local; edge hit-testing must use the
        # content's own size (the window is taller — title bar — so testing
        # against window height makes the bottom edge unreachable).
        self._content = content
        move = Gtk.GestureDrag()
        move.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        move.connect("drag-begin", self._on_move_begin)
        move.connect("drag-update", self._on_move_update)
        move.connect("drag-end", self._on_move_end)
        title_bar.add_controller(move)

        resize = Gtk.GestureDrag()
        resize.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        resize.connect("drag-begin", self._on_resize_begin)
        resize.connect("drag-update", self._on_resize_update)
        resize.connect("drag-end", self._on_resize_end)
        content.add_controller(resize)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_hover_edge)
        motion.connect("leave", self._on_leave_edges)
        content.add_controller(motion)

    # ---- move ------------------------------------------------------------------

    def _pointer_root(self) -> Optional[Tuple[int, int]]:
        """Server-truth pointer position; None falls interactions back to deltas."""
        if not self.window.xid:
            return None
        try:
            return x11.shared_connection().query_pointer_root(self.window.xid)
        except Exception:
            return None

    def _on_move_begin(self, _gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if self.move_locked():
            return
        self._move_active = True
        self._sticky_h = None
        self._sticky_v = None
        gx, gy, _w, _h = self.window.window_geometry()
        self._move_origin = (gx - x, gy - y)
        pointer = self._pointer_root()
        self._move_grab_offset = (gx - pointer[0], gy - pointer[1]) if pointer is not None else None

    def _on_move_update(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if not self._move_active or self.move_locked():
            return
        _gx, _gy, w, h = self.window.window_geometry()
        pointer = self._pointer_root()
        if pointer is not None and self._move_grab_offset is not None:
            # Window follows the real pointer: position = pointer + grab offset.
            proposed = Rect(
                int(pointer[0] + self._move_grab_offset[0]),
                int(pointer[1] + self._move_grab_offset[1]),
                w,
                h,
            )
        else:
            # No server pointer (headless/no X11): legacy delta path.
            origin_x, origin_y = self._move_origin
            proposed = Rect(int(origin_x + dx), int(origin_y + dy), w, h)
        x, y = self._apply_move_snap(proposed)
        self.window.move_only(x, y)

    def _on_move_end(self, *_args) -> None:
        if not self._move_active:
            return
        self._move_active = False
        self._commit()

    def _apply_move_snap(self, proposed: Rect) -> Tuple[int, int]:
        if not self.snap_enabled():
            return proposed.x, proposed.y
        scale = self.window.get_scale_factor()
        result = snap.resolve_move(
            proposed,
            self.snap_targets(),
            self.work_area(),
            int(round(self.snap_spacing() * scale)),
            max(1, int(round(snap.SNAP_ENAGE_THRESHOLD_DIPS * scale))),
            int(round(snap.SNAP_RELEASE_THRESHOLD_DIPS * scale)),
            self._sticky_h,
            self._sticky_v,
        )
        self._sticky_h = result.horizontal_match
        self._sticky_v = result.vertical_match
        return result.bounds.x, result.bounds.y

    # ---- resize -------------------------------------------------------------------

    def _content_size(self) -> Tuple[int, int]:
        """Size of the widget the resize gesture is attached to (content-local
        hit-testing); windows built without attach() fall back to the window."""
        if self._content is not None:
            return self._content.get_width(), self._content.get_height()
        return self.window.get_width(), self.window.get_height()

    def _edge_at(self, x: float, y: float, width: int, height: int) -> Optional[str]:
        near_left = x <= EDGE_HIT_PX
        near_right = x >= width - EDGE_HIT_PX
        near_top = y <= EDGE_HIT_PX
        near_bottom = y >= height - EDGE_HIT_PX
        if near_top and near_left:
            return "top-left"
        if near_top and near_right:
            return "top-right"
        if near_bottom and near_left:
            return "bottom-left"
        if near_bottom and near_right:
            return "bottom-right"
        if near_left:
            return "left"
        if near_right:
            return "right"
        if near_top:
            return "top"
        if near_bottom:
            return "bottom"
        return None

    def _on_resize_begin(self, _gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if self.size_locked():
            return
        width, height = self._content_size()
        edge = self._edge_at(x, y, width, height)
        if edge is None:
            return
        self._resize_active = True
        self._resize_edge = edge
        gx, gy, gw, gh = self.window.window_geometry()
        self._resize_start = (gx, gy, gw, gh)
        self._resize_pointer_start = self._pointer_root()

    def _on_resize_update(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if not self._resize_active or self.size_locked():
            return
        gx, gy, gw, gh = self._resize_start
        edge = self._resize_edge
        pointer = self._pointer_root()
        if pointer is not None and self._resize_pointer_start is not None:
            dx = pointer[0] - self._resize_pointer_start[0]
            dy = pointer[1] - self._resize_pointer_start[1]
        x, y, w, h = gx, gy, gw, gh
        if "right" in edge:
            w = max(WIDGET_MIN_WIDTH, int(gw + dx))
        if "bottom" in edge:
            h = max(WIDGET_MIN_HEIGHT, int(gh + dy))
        if "left" in edge:
            w = max(WIDGET_MIN_WIDTH, int(gw - dx))
            x = gx + (gw - w)
        if "top" in edge:
            h = max(WIDGET_MIN_HEIGHT, int(gh - dy))
            y = gy + (gh - h)
        proposed = Rect(x, y, w, h)
        x, y, w, h = self._apply_resize_snap(proposed, edge)
        self.window.resize_to(x, y, w, h)

    def _on_resize_end(self, *_args) -> None:
        if not self._resize_active:
            return
        self._resize_active = False
        self._resize_edge = None
        self._commit()

    def _apply_resize_snap(self, proposed: Rect, edge: str) -> Tuple[int, int, int, int]:
        if not self.snap_enabled():
            return proposed.x, proposed.y, proposed.width, proposed.height
        scale = self.window.get_scale_factor()
        spacing = int(round(self.snap_spacing() * scale))
        threshold = max(1, int(round(snap.SNAP_ENAGE_THRESHOLD_DIPS * scale)))
        targets = self.snap_targets()
        work = self.work_area()
        x, y, w, h = proposed.x, proposed.y, proposed.width, proposed.height

        horizontal_edge = {"left": snap.LEFT, "right": snap.RIGHT}.get(edge.split("-")[-1] if "-" in edge else edge)
        vertical_edge = {"top": snap.TOP, "bottom": snap.BOTTOM}.get(edge.split("-")[0] if "-" in edge else edge)

        if horizontal_edge is not None:
            match = snap.resolve_resize_edge(proposed, horizontal_edge, targets, work, spacing, threshold)
            if match is not None:
                if horizontal_edge == snap.LEFT:
                    x = match.coordinate
                    w = proposed.right() - x
                else:
                    w = match.coordinate - proposed.x
                w = max(WIDGET_MIN_WIDTH, w)
                if horizontal_edge == snap.LEFT:
                    x = proposed.right() - w
        if vertical_edge is not None:
            match = snap.resolve_resize_edge(proposed, vertical_edge, targets, work, spacing, threshold)
            if match is not None:
                if vertical_edge == snap.TOP:
                    y = match.coordinate
                    h = proposed.bottom() - y
                else:
                    h = match.coordinate - proposed.y
                h = max(WIDGET_MIN_HEIGHT, h)
                if vertical_edge == snap.TOP:
                    y = proposed.bottom() - h
        return x, y, w, h

    # ---- cursor hints -------------------------------------------------------------

    def _on_hover_edge(self, _motion: Gtk.EventControllerMotion, x: float, y: float) -> None:
        if self._resize_active:
            return
        edge = self._edge_at(x, y, *self._content_size())
        cursor_name = _EDGE_CURSORS.get(edge)
        try:
            if cursor_name:
                self.window.set_cursor_from_name(cursor_name)
            else:
                self.window.set_cursor_from_name(None)
        except Exception:
            pass

    def _on_leave_edges(self, _motion: Gtk.EventControllerMotion) -> None:
        if self._resize_active:
            return
        try:
            self.window.set_cursor_from_name(None)
        except Exception:
            pass

    # ---- commit ------------------------------------------------------------------------

    def _commit(self) -> None:
        x, y, w, h = self.window.window_geometry()
        if self.on_geometry_committed:
            self.on_geometry_committed(x, y, w, h)


_EDGE_CURSORS = {
    "left": "w-resize",
    "right": "e-resize",
    "top": "n-resize",
    "bottom": "s-resize",
    "top-left": "nw-resize",
    "top-right": "ne-resize",
    "bottom-left": "sw-resize",
    "bottom-right": "se-resize",
}
