"""Dedicated X11 connection thread: root property watch + global hotkey grabs.

GTK owns the main display connection; a second XOpenDisplay here can select
for root PropertyChangeMask and XGrabKey without fighting GDK. Events are
pumped by a select() loop on the connection fd and marshalled to the GLib
main loop via GLib.idle_add (PyGObject releases the GIL around select).

Modifier constants (X.h): consumed masks Lock/Mod2/Mod5 get grab variants so
NumLock/CapsLock/ScrollLock states don't silently disable a hotkey.
"""

from __future__ import annotations

import ctypes
import select
import threading
from typing import Callable, Optional

from . import x11
from .x11 import X11Connection

ShiftMask = 1 << 0
LockMask = 1 << 1
ControlMask = 1 << 2
Mod1Mask = 1 << 3  # Alt
Mod2Mask = 1 << 4  # NumLock
Mod3Mask = 1 << 5
Mod4Mask = 1 << 6  # Super
Mod5Mask = 1 << 7  # ScrollLock

_IGNORED_VARIANTS = [
    0,
    LockMask,
    Mod2Mask,
    LockMask | Mod2Mask,
    Mod5Mask,
    Mod5Mask | LockMask,
    Mod5Mask | Mod2Mask,
    Mod5Mask | LockMask | Mod2Mask,
]


class XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class XPropertyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("atom", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("state", ctypes.c_int),
    ]


KeyEventCallback = Callable[[str, int], None]
ActiveWindowCallback = Callable[[int], None]


class X11EventThread:
    """Owns a private X connection; watches _NET_ACTIVE_WINDOW and key grabs."""

    def __init__(self, display_name: Optional[str] = None):
        self.conn = X11Connection(display_name)
        self.lib = self.conn.lib
        self.lib.XPending.restype = ctypes.c_int
        self.lib.XPending.argtypes = [ctypes.c_void_p]
        self._root = self.conn.root_window()
        self._atom_active = self.conn.atom(x11.ATOM_ACTIVE_WINDOW)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hotkeys: dict[int, KeyEventCallback] = {}  # keycode -> cb
        self._active_window_cb: Optional[ActiveWindowCallback] = None
        self._event = ctypes.create_string_buffer(256)
        self._dispatch_on_gl = True

    # ---- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        with self._lock:
            self.conn.select_input(self._root, x11.PropertyChangeMask)
        self._thread = threading.Thread(target=self._run, name="panebox-x11-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def _run(self) -> None:
        fd = self.conn.connection_number()
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            with self._lock:
                try:
                    pending = self.lib.XPending(self.conn.handle)
                except Exception:
                    break
                if pending <= 0:
                    continue
                status = self.lib.XNextEvent(self.conn.handle, self._event)
                if status == 0:
                    self._dispatch(self._event)

    def _dispatch(self, buf) -> None:
        event_type = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
        if event_type == x11.KeyPress:
            key = ctypes.cast(buf, ctypes.POINTER(XKeyEvent)).contents
            cb = self._hotkeys.get(key.keycode)
            if cb is not None:
                self._post(cb, key.keycode)
        elif event_type == x11.PropertyNotify:
            prop = ctypes.cast(buf, ctypes.POINTER(XPropertyEvent)).contents
            if prop.window == self._root and prop.atom == self._atom_active:
                if self._active_window_cb is not None:
                    active = self.conn.get_property_windows(self._root, x11.ATOM_ACTIVE_WINDOW)
                    xid = active[0] if active else 0
                    self._post_active(self._active_window_cb, xid)

    def _post(self, cb: KeyEventCallback, keycode: int) -> None:
        if self._dispatch_on_gl:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            GLib.idle_add(self._safe_call, cb, keycode)
        else:
            self._safe_call(cb, keycode)

    def _post_active(self, cb: ActiveWindowCallback, xid: int) -> None:
        if self._dispatch_on_gl:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            GLib.idle_add(self._safe_call_active, cb, xid)
        else:
            self._safe_call_active(cb, xid)

    @staticmethod
    def _safe_call(cb: KeyEventCallback, keycode: int) -> bool:
        try:
            cb("", keycode)
        except Exception:
            pass
        return False  # run once, then remove

    @staticmethod
    def _safe_call_active(cb: ActiveWindowCallback, xid: int) -> bool:
        try:
            cb(xid)
        except Exception:
            pass
        return False

    # ---- hotkeys ----------------------------------------------------------------

    def grab_key(self, keysym_name: str, modifiers: int, callback: KeyEventCallback) -> bool:
        """Grab keycode under modifiers + ignored-mask variants."""
        keycode = self.conn.keycode_for(keysym_name)
        if keycode == 0:
            return False
        with self._lock:
            for variant in _IGNORED_VARIANTS:
                try:
                    self.conn.grab_key(keycode, modifiers | variant, self._root)
                except x11.X11Error:
                    # Already grabbed (possibly by another app): undo and fail.
                    for done in _IGNORED_VARIANTS[: _IGNORED_VARIANTS.index(variant)]:
                        self.conn.ungrab_key(keycode, modifiers | done, self._root)
                    return False
            self._hotkeys[keycode] = callback
        return True

    def ungrab_key(self, keysym_name: str, modifiers: int) -> None:
        keycode = self.conn.keycode_for(keysym_name)
        if keycode == 0:
            return
        with self._lock:
            for variant in _IGNORED_VARIANTS:
                self.conn.ungrab_key(keycode, modifiers | variant, self._root)
            self._hotkeys.pop(keycode, None)

    def ungrab_all(self) -> None:
        with self._lock:
            self._hotkeys.clear()

    # ---- active window watch ------------------------------------------------------

    def set_active_window_callback(self, cb: Optional[ActiveWindowCallback]) -> None:
        self._active_window_cb = cb

    def current_active_window(self) -> int:
        windows = self.conn.get_property_windows(self._root, x11.ATOM_ACTIVE_WINDOW)
        return windows[0] if windows else 0
