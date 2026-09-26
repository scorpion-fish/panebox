"""ctypes wrapper over libX11: EWMH properties, client messages, geometry, grabs.

GTK4 has no API for window type hints, absolute positioning, or global key
grabs, so PaneBox talks to Xlib directly. One connection per process is fine
for property writes; the event thread opens its own (X11 connections are not
thread-safe to share across threads).

XIDs are server-global integers, so a window created by GDK can be
manipulated through this separate connection.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import Sequence

# X constants
PropModeReplace = 0
AnyPropertyType = 0
SuccessCode = 0
ClientMessage = 33
PropertyNotify = 28
KeyPress = 2
KeyRelease = 3
SubstructureNotifyMask = 1 << 19
SubstructureRedirectMask = 1 << 20
PropertyChangeMask = 1 << 22
GrabModeAsync = 1

# EWMH atoms (name strings)
ATOM_WINDOW_TYPE = "_NET_WM_WINDOW_TYPE"
ATOM_WINDOW_TYPE_DESKTOP = "_NET_WM_WINDOW_TYPE_DESKTOP"
ATOM_WINDOW_TYPE_NORMAL = "_NET_WM_WINDOW_TYPE_NORMAL"
ATOM_WINDOW_TYPE_DOCK = "_NET_WM_WINDOW_TYPE_DOCK"
ATOM_WM_STATE = "_NET_WM_STATE"
ATOM_STATE_STICKY = "_NET_WM_STATE_STICKY"
ATOM_STATE_SKIP_TASKBAR = "_NET_WM_STATE_SKIP_TASKBAR"
ATOM_STATE_SKIP_PAGER = "_NET_WM_STATE_SKIP_PAGER"
ATOM_STATE_ABOVE = "_NET_WM_STATE_ABOVE"
ATOM_STATE_ADD = 1
ATOM_STATE_REMOVE = 0
ATOM_USER_TIME = "_NET_WM_USER_TIME"
ATOM_PID = "_NET_WM_PID"
ATOM_ACTIVE_WINDOW = "_NET_ACTIVE_WINDOW"
ATOM_CLIENT_LIST = "_NET_CLIENT_LIST"
ATOM_STRUT_PARTIAL = "_NET_WM_STRUT_PARTIAL"
ATOM_WORKAREA = "_NET_WORKAREA"
ATOM_DESKTOP_VIEWPORT = "_NET_DESKTOP_VIEWPORT"
WM_PROTOCOLS = "WM_PROTOCOLS"

XA_ATOM = 4
XA_CARDINAL = 6
XA_WINDOW = 33


class X11Error(RuntimeError):
    pass


# ---- async X error capture --------------------------------------------------
# XSetErrorHandler is process-global (not per-display), so one handler routes
# by display pointer. GDK installs its own handler for its connection; this
# one only ever sees our ctypes connections (and harmlessly ignores others).

BadAccessCode = 10

_ERROR_REGISTRY: dict[int, "X11Connection"] = {}
_ERROR_HANDLER = None


@ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
def _route_x_error(display: int, event: int) -> int:
    conn = _ERROR_REGISTRY.get(display)
    if conn is not None:
        # XErrorEvent (x64): type@0 pad, display@8, resourceid@16, serial@24,
        # error_code@32 (uchar), request_code@33, minor_code@34
        raw = ctypes.string_at(event, 40)
        conn.async_errors.append((raw[32], raw[33], int.from_bytes(raw[16:24], "little")))
    return 0


def _install_error_handler(lib) -> None:
    global _ERROR_HANDLER
    if _ERROR_HANDLER is not None:
        return
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    lib.XSetErrorHandler(None)  # read and discard current, if any
    lib.XSetErrorHandler(ctypes.cast(_route_x_error, ctypes.c_void_p))
    _ERROR_HANDLER = _route_x_error


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
        ("padding", ctypes.c_byte * 40),  # XEvent union headroom
    ]


def _load_libx11() -> ctypes.CDLL:
    name = ctypes.util.find_library("X11")
    if not name:
        raise X11Error("libX11 not found")
    lib = ctypes.CDLL(name)
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XChangeProperty.restype = ctypes.c_int
    lib.XChangeProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.XGetWindowProperty.restype = ctypes.c_int
    lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_long,
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    lib.XDeleteProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XMoveResizeWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    lib.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
    lib.XResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint, ctypes.c_uint]
    lib.XSendEvent.restype = ctypes.c_int
    lib.XSendEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_long,
        ctypes.c_void_p,
    ]
    lib.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XKeysymToKeycode.restype = ctypes.c_uint
    lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XStringToKeysym.restype = ctypes.c_ulong
    lib.XStringToKeysym.argtypes = [ctypes.c_char_p]
    lib.XGrabKey.restype = None  # void: failures arrive as async BadAccess
    lib.XGrabKey.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.XUngrabKey.restype = None
    lib.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
    lib.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
    lib.XConnectionNumber.restype = ctypes.c_int
    lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
    lib.XNextEvent.restype = ctypes.c_int
    lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XGetGeometry.restype = ctypes.c_int
    lib.XGetGeometry.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    lib.XQueryPointer.restype = ctypes.c_int
    lib.XQueryPointer.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
    ]
    lib.XTranslateCoordinates.restype = ctypes.c_int
    lib.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    lib.XQueryTree.restype = ctypes.c_int
    lib.XQueryTree.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    return lib


class X11Connection:
    """One X11 connection with atom caching and EWMH helpers."""

    def __init__(self, display_name: str | None = None):
        self.lib = _load_libx11()
        name = display_name if display_name is not None else os.environ.get("DISPLAY")
        self.handle = self.lib.XOpenDisplay(name.encode() if name else None)
        if not self.handle:
            raise X11Error(f"cannot open display {name!r}")
        self._atoms: dict[str, int] = {}
        self.async_errors: list[tuple[int, int, int]] = []
        _install_error_handler(self.lib)
        _ERROR_REGISTRY[self.handle] = self

    def close(self) -> None:
        if self.handle:
            _ERROR_REGISTRY.pop(self.handle, None)
            self.lib.XCloseDisplay(self.handle)
            self.handle = None

    def take_errors(self) -> list[tuple[int, int, int]]:
        errors = self.async_errors
        self.async_errors = []
        return errors

    # ---- basics -------------------------------------------------------------

    def root_window(self) -> int:
        return self.lib.XDefaultRootWindow(self.handle)

    def atom(self, name: str) -> int:
        value = self._atoms.get(name)
        if value is None:
            value = self.lib.XInternAtom(self.handle, name.encode(), False)
            self._atoms[name] = value
        return value

    def sync(self) -> None:
        self.lib.XSync(self.handle, False)

    def flush(self) -> None:
        self.lib.XFlush(self.handle)

    # ---- properties ---------------------------------------------------------

    def set_atoms(self, window: int, prop: str, names: Sequence[str]) -> None:
        atoms = (ctypes.c_ulong * len(names))(*[self.atom(n) for n in names])
        self.lib.XChangeProperty(
            self.handle,
            window,
            self.atom(prop),
            XA_ATOM,
            32,
            PropModeReplace,
            ctypes.cast(atoms, ctypes.c_void_p),
            len(names),
        )
        self.flush()

    def set_cardinals(self, window: int, prop: str, values: Sequence[int]) -> None:
        data = (ctypes.c_ulong * len(values))(*values)
        self.lib.XChangeProperty(
            self.handle,
            window,
            self.atom(prop),
            XA_CARDINAL,
            32,
            PropModeReplace,
            ctypes.cast(data, ctypes.c_void_p),
            len(values),
        )
        self.flush()

    def set_window_list(self, window: int, prop: str, windows: Sequence[int]) -> None:
        data = (ctypes.c_ulong * len(windows))(*windows)
        self.lib.XChangeProperty(
            self.handle,
            window,
            self.atom(prop),
            XA_WINDOW,
            32,
            PropModeReplace,
            ctypes.cast(data, ctypes.c_void_p),
            len(windows),
        )
        self.flush()

    def get_property_atoms(self, window: int, prop: str) -> list[str]:
        raw = self.get_property_raw(window, prop, XA_ATOM)
        if not raw:
            return []
        nitems = raw[1]
        data = ctypes.cast(raw[0], ctypes.POINTER(ctypes.c_ulong))
        result = []
        for i in range(nitems):
            atom_id = data[i]
            name = self.atom_name(atom_id)
            if name:
                result.append(name)
        self.lib.XFree(raw[0])
        return result

    def get_property_cardinals(self, window: int, prop: str) -> list[int]:
        raw = self.get_property_raw(window, prop, XA_CARDINAL)
        if not raw:
            return []
        _, nitems, data = raw
        values = [data[i] for i in range(nitems)]
        self.lib.XFree(raw[0])
        return values

    def get_property_windows(self, window: int, prop: str) -> list[int]:
        raw = self.get_property_raw(window, prop, XA_WINDOW)
        if not raw:
            return []
        _, nitems, data = raw
        values = [data[i] for i in range(nitems)]
        self.lib.XFree(raw[0])
        return values

    def get_property_raw(self, window: int, prop: str, req_type: int):
        """Returns (data_voidptr, nitems, typed_pointer) or None."""
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.lib.XGetWindowProperty(
            self.handle,
            window,
            self.atom(prop),
            0,
            1024,
            False,
            req_type,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(nitems),
            ctypes.byref(bytes_after),
            ctypes.byref(data),
        )
        if status != 0 or nitems.value == 0:
            return None
        raw = ctypes.cast(data, ctypes.c_void_p).value
        if raw is None:
            return None
        if actual_format.value == 32:
            return raw, nitems.value, ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
        if actual_format.value == 16:
            return raw, nitems.value, ctypes.cast(data, ctypes.POINTER(ctypes.c_ushort))
        return raw, nitems.value, data

    _atom_names: dict[int, str] = {}

    def atom_name(self, atom_id: int) -> str | None:
        cached = self._atom_names.get(atom_id)
        if cached:
            return cached
        # XGetAtomName returns a server-owned string; copy it out, then XFree.
        self.lib.XGetAtomName.restype = ctypes.c_void_p
        self.lib.XGetAtomName.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        raw = self.lib.XGetAtomName(self.handle, atom_id)
        if not raw:
            return None
        try:
            name = ctypes.string_at(raw).decode("utf-8", "replace")
        finally:
            self.lib.XFree(ctypes.c_void_p(raw))
        self._atom_names[atom_id] = name
        return name

    # ---- client messages ------------------------------------------------------

    def send_state_message(self, window: int, action: int, atoms: Sequence[str]) -> None:
        """_NET_WM_STATE ClientMessage on the root, forwarded to the window."""
        event = XClientMessageEvent()
        event.type = ClientMessage
        event.display = self.handle
        event.window = window
        event.message_type = self.atom(ATOM_WM_STATE)
        event.format = 32
        event.data[0] = action
        if len(atoms) > 0:
            event.data[1] = self.atom(atoms[0])
        if len(atoms) > 1:
            event.data[2] = self.atom(atoms[1])
        root = self.root_window()
        self.lib.XSendEvent(
            self.handle,
            root,
            False,
            SubstructureNotifyMask | SubstructureRedirectMask,
            ctypes.byref(event),
        )
        self.sync()

    # ---- geometry -------------------------------------------------------------

    def move_resize(self, window: int, x: int, y: int, w: int, h: int) -> None:
        self.lib.XMoveResizeWindow(self.handle, window, int(x), int(y), int(max(1, w)), int(max(1, h)))
        self.flush()

    def move(self, window: int, x: int, y: int) -> None:
        self.lib.XMoveWindow(self.handle, window, int(x), int(y))
        self.flush()

    def resize(self, window: int, w: int, h: int) -> None:
        self.lib.XResizeWindow(self.handle, window, int(max(1, w)), int(max(1, h)))
        self.flush()

    def geometry(self, window: int) -> tuple[int, int, int, int]:
        """Root-absolute position + size (XGetGeometry alone is parent-relative,
        which breaks when a WM reparents the window)."""
        root = ctypes.c_ulong()
        x = ctypes.c_int()
        y = ctypes.c_int()
        w = ctypes.c_uint()
        h = ctypes.c_uint()
        bw = ctypes.c_uint()
        depth = ctypes.c_uint()
        if (
            self.lib.XGetGeometry(
                self.handle,
                window,
                ctypes.byref(root),
                ctypes.byref(x),
                ctypes.byref(y),
                ctypes.byref(w),
                ctypes.byref(h),
                ctypes.byref(bw),
                ctypes.byref(depth),
            )
            != 0
        ):
            abs_x, abs_y = self.translate_to_root(window)
            return abs_x, abs_y, w.value, h.value
        return 0, 0, 0, 0

    def translate_to_root(self, window: int) -> tuple[int, int]:
        """Window origin in root coordinates (0,0 of the window, translated)."""
        dest_x = ctypes.c_int()
        dest_y = ctypes.c_int()
        child = ctypes.c_ulong()
        if (
            self.lib.XTranslateCoordinates(
                self.handle,
                window,
                self.root_window(),
                0,
                0,
                ctypes.byref(dest_x),
                ctypes.byref(dest_y),
                ctypes.byref(child),
            )
            != 0
        ):
            return dest_x.value, dest_y.value
        return 0, 0

    def query_pointer_root(self, window: int) -> tuple[int, int] | None:
        """Server-truth pointer position in root coordinates.

        Drag positioning must use this, not gesture deltas: after we move the
        window behind GDK's back, GDK re-translates subsequent motion events
        against the NEW window origin, so gesture offsets feed back into
        themselves (the window oscillates and converges to half the drag).
        """
        root = ctypes.c_ulong()
        child = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        win_x = ctypes.c_int()
        win_y = ctypes.c_int()
        mask = ctypes.c_uint()
        if self.lib.XQueryPointer(
            self.handle,
            window,
            ctypes.byref(root),
            ctypes.byref(child),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(win_x),
            ctypes.byref(win_y),
            ctypes.byref(mask),
        ):
            return root_x.value, root_y.value
        return None

    # ---- hotkeys ----------------------------------------------------------------

    def keycode_for(self, keysym_name: str) -> int:
        keysym = self.lib.XStringToKeysym(keysym_name.encode())
        return self.lib.XKeysymToKeycode(self.handle, keysym)

    def grab_key(self, keycode: int, modifiers: int, grab_window: int) -> None:
        """Passive grab; raises X11Error on async BadAccess (combo owned by another client)."""
        self.lib.XGrabKey(self.handle, keycode, modifiers, grab_window, False, GrabModeAsync, GrabModeAsync)
        self.sync()
        for error_code, request_code, resource in self.take_errors():
            if error_code == BadAccessCode:
                raise X11Error(f"XGrabKey failed for keycode {keycode} mods {modifiers}: BadAccess")

    def ungrab_key(self, keycode: int, modifiers: int, grab_window: int) -> None:
        self.lib.XUngrabKey(self.handle, keycode, modifiers, grab_window)
        self.sync()

    def select_input(self, window: int, mask: int) -> None:
        self.lib.XSelectInput(self.handle, window, mask)
        self.flush()

    def connection_number(self) -> int:
        return self.lib.XConnectionNumber(self.handle)


_shared: X11Connection | None = None


def shared_connection() -> X11Connection:
    global _shared
    if _shared is None:
        _shared = X11Connection()
    return _shared
