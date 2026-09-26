"""WidgetWindow: a Gtk.Window pinned to the desktop layer via EWMH.

Desktop layer = _NET_WM_WINDOW_TYPE_DESKTOP (Mutter keeps these windows below
all normal windows). The type is set in `realize` (before map, so the WM reads
it at map time) and re-applied on `map` because GDK may rewrite _NET_WM_STATE
when it syncs its own hints.

Absolute positioning goes through XMoveResizeWindow: GTK4 has no move API.
"""

from __future__ import annotations

import os
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkX11", "4.0")
from gi.repository import Gdk, GdkX11, Gtk  # noqa: E402

from . import x11
from .x11 import X11Error


class WidgetWindow(Gtk.Window):
    """Base window for desktop widgets."""

    def __init__(self, **kwargs):
        super().__init__(decorated=False, resizable=True, **kwargs)
        self.xid: int = 0
        self.layer_applied = False
        self.raised_mode = False
        self._published = False
        self.connect("realize", self._on_realize)
        self.connect("map", self._on_map)

    # ---- desktop layer ------------------------------------------------------

    def _on_realize(self, *_args) -> None:
        surface = self.get_surface()
        if not isinstance(surface, GdkX11.X11Surface):
            return
        self.xid = surface.get_xid()
        try:
            surface.set_skip_taskbar_hint(True)
            surface.set_skip_pager_hint(True)
        except AttributeError:
            pass
        self.apply_desktop_layer()
        self._publish_xid()

    def _on_map(self, *_args) -> None:
        # Re-apply: GDK may stomp _NET_WM_STATE while syncing its own hints.
        self.apply_desktop_layer()
        # Placement requested before realize is applied on idle once mapped: a
        # WM-less map resets the position, and GDK's own first configure pass
        # (which centers/zeroes the window) must land BEFORE our move or it
        # overwrites us.
        if self._pending_geometry is not None and self.xid:
            x, y, w, h = self._pending_geometry
            self._pending_geometry = None

            def apply_geometry(*_a):
                if self.xid:
                    x11.shared_connection().move_resize(self.xid, x, y, w, h)
                return False

            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            GLib.idle_add(apply_geometry)

    def apply_desktop_layer(self) -> None:
        if not self.xid:
            return
        try:
            conn = x11.shared_connection()
        except X11Error:
            return
        conn.set_atoms(self.xid, x11.ATOM_WINDOW_TYPE, [x11.ATOM_WINDOW_TYPE_DESKTOP])
        conn.set_atoms(
            self.xid,
            x11.ATOM_WM_STATE,
            [x11.ATOM_STATE_STICKY, x11.ATOM_STATE_SKIP_TASKBAR, x11.ATOM_STATE_SKIP_PAGER],
        )
        conn.set_cardinals(self.xid, x11.ATOM_USER_TIME, [0])
        conn.set_cardinals(self.xid, x11.ATOM_PID, [os.getpid()])
        self.raised_mode = False
        self.layer_applied = True

    def raise_above_all(self) -> None:
        """Temporarily leave the desktop layer: NORMAL type + STATE_ABOVE."""
        if not self.xid:
            return
        conn = x11.shared_connection()
        conn.set_atoms(self.xid, x11.ATOM_WINDOW_TYPE, [x11.ATOM_WINDOW_TYPE_NORMAL])
        conn.send_state_message(self.xid, x11.ATOM_STATE_ADD, [x11.ATOM_STATE_ABOVE])
        self.raised_mode = True

    def restore_desktop_layer(self) -> None:
        if not self.xid:
            return
        conn = x11.shared_connection()
        conn.send_state_message(self.xid, x11.ATOM_STATE_REMOVE, [x11.ATOM_STATE_ABOVE])
        self.apply_desktop_layer()

    # ---- positioning ----------------------------------------------------------

    def set_window_geometry(self, x: int, y: int, width: int, height: int) -> None:
        """Absolute placement. Before realize, uses GTK defaults; after, X11."""
        self.set_default_size(width, height)
        if self.xid:
            x11.shared_connection().move_resize(self.xid, x, y, width, height)
        else:
            self._pending_geometry = (x, y, width, height)

    def move_only(self, x: int, y: int) -> None:
        if self.xid:
            x11.shared_connection().move(self.xid, x, y)
        else:
            pending = self._pending_geometry or (100, 100, 300, 400)
            self._pending_geometry = (x, y, pending[2], pending[3])

    def resize_to(self, x: int, y: int, w: int, h: int) -> None:
        if self.xid:
            x11.shared_connection().move_resize(self.xid, x, y, w, h)
        else:
            self._pending_geometry = (x, y, w, h)

    def set_cursor_from_name(self, name: Optional[str]) -> None:
        try:
            if name:
                self.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))
            else:
                self.set_cursor(None)
        except Exception:
            pass

    _pending_geometry: Optional[tuple[int, int, int, int]] = None

    def window_geometry(self) -> tuple[int, int, int, int]:
        if self.xid:
            return x11.shared_connection().geometry(self.xid)
        pending = self._pending_geometry
        if pending:
            return pending
        width = self.get_width()
        height = self.get_height()
        return 100, 100, width, height

    def _publish_xid(self) -> None:
        """Print a stable marker for headless test harnesses (no WM on Xvfb)."""
        if self._published or not os.environ.get("PANEBOX_TEST_MARKERS"):
            return
        self._published = True
        print(f"DESKBOX-XID {self.get_title() or 'widget'} {hex(self.xid)}", flush=True)
