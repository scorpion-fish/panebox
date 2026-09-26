"""Toggle-all raise controller.

Widgets live on the desktop layer. "Show/hide widgets" either raises every
widget above normal windows (flip type to NORMAL + _NET_WM_STATE_ABOVE) or
drops them back. While raised, any external window activation lowers them
again — driven by _NET_ACTIVE_WINDOW change events with a mouse-down sampler
fallback for WMs that don't keep that property fresh.
"""

from __future__ import annotations

from typing import Callable, Optional

from . import x11


class RaiseToggleController:
    def __init__(
        self,
        windows_provider: Callable[[], list],
        on_state_changed: Optional[Callable[[bool], None]] = None,
        poll_interval_ms: int = 250,
    ):
        self._windows_provider = windows_provider
        self._on_state_changed = on_state_changed
        self._poll_interval_ms = poll_interval_ms
        self.raised = False
        self._poll_source: int = 0

    # ---- toggle -----------------------------------------------------------------

    def toggle(self) -> bool:
        if self.raised:
            self.lower_all()
        else:
            self.raise_all()
        return self.raised

    def raise_all(self) -> None:
        if self.raised:
            return
        for window in self._visible_windows():
            window.raise_above_all()
        self.raised = True
        self._start_polling()
        if self._on_state_changed:
            self._on_state_changed(True)

    def lower_all(self) -> None:
        if not self.raised:
            return
        for window in self._visible_windows():
            window.restore_desktop_layer()
        self.raised = False
        self._stop_polling()
        if self._on_state_changed:
            self._on_state_changed(False)

    # ---- external activation ------------------------------------------------------

    def on_active_window_changed(self, active_xid: int) -> None:
        """Called from the X11 event thread (via GLib idle) on _NET_ACTIVE_WINDOW."""
        if not self.raised or active_xid == 0:
            return
        own = {w.xid for w in self._windows_provider() if w.xid}
        # 0 = no active window (desktop focused) -> keep raised.
        if active_xid not in own:
            self.lower_all()

    # ---- poll fallback ---------------------------------------------------------------

    def _start_polling(self) -> None:
        if self._poll_source:
            return
        try:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            self._poll_source = GLib.timeout_add(self._poll_interval_ms, self._poll_once)
        except Exception:
            self._poll_source = 0

    def _stop_polling(self) -> None:
        if not self._poll_source:
            return
        try:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            GLib.source_remove(self._poll_source)
        except Exception:
            pass
        self._poll_source = 0

    def _poll_once(self) -> bool:
        if not self.raised:
            self._poll_source = 0
            return False
        try:
            conn = x11.shared_connection()
            active = conn.get_property_windows(conn.root_window(), x11.ATOM_ACTIVE_WINDOW)
            if active:
                self.on_active_window_changed(active[0])
        except Exception:
            pass
        return self.raised  # keep polling until lowered

    # ---- helpers --------------------------------------------------------------------

    def _visible_windows(self) -> list:
        return [w for w in self._windows_provider() if w.get_visible() and w.xid]
