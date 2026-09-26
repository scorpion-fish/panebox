"""Resize notification for widget surfaces.

The GTK4 ``size-allocate`` vfunc override never fires in this PyGObject build
(verified against 4.22: a plain ``Gtk.Box`` subclass override stays silent
while ``widget.get_width()`` tracks allocation), so surfaces learn their
content size from the toplevel ``Gdk.Surface`` configure notifications and
read the widget's own allocated size on the next idle, after GTK has run the
layout pass.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402


class ResizeHook:
    """Call ``on_resized(width, height)`` whenever ``widget``'s allocation
    changes (window moved/resized, initial map, monitor changes)."""

    def __init__(self, widget: Gtk.Widget, on_resized):
        self._widget = widget
        self._on_resized = on_resized
        self._handlers: list[tuple[object, int]] = []
        widget.connect("destroy", lambda *_: self.detach())
        if widget.get_realized():
            self._connect_root()
        else:
            widget.connect("realize", lambda *_: self._connect_root())

    def _connect_root(self) -> None:
        root = self._widget.get_root()
        surface = root.get_surface() if isinstance(root, Gtk.Window) else None
        if surface is None or any(s is surface for s, _ in self._handlers):
            return
        for prop in ("width", "height"):
            self._handlers.append((surface, surface.connect(f"notify::{prop}", self._changed)))

    def _changed(self, _surface, _pspec) -> None:
        GLib.idle_add(self._emit)

    def _emit(self) -> bool:
        width, height = self._widget.get_width(), self._widget.get_height()
        if width > 0 and height > 0:
            try:
                self._on_resized(width, height)
            except Exception:
                pass
        return GLib.SOURCE_REMOVE

    def detach(self) -> None:
        for surface, handler in self._handlers:
            try:
                surface.disconnect(handler)
            except Exception:
                pass
        self._handlers.clear()
