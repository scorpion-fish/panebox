#!/usr/bin/env python3
"""Driver for test_exit_on_empty: real XTest ✕ clicks, expects the app to
quit once the LAST window closes — and not before.

Phases (a GC-scheduled state machine):
  1. boot restored the initial file widget; create a Todo feature widget.
  2. click the file widget's ✕ → app must SURVIVE (a window is still open).
  3. click the Todo widget's ✕ → app.run() must return.

Exit code 0 + "EXIT-ON-EMPTY: PASS" only when both hold.
"""

import ctypes
import os
import sys

# A dead bus address, not a pop: GDBus falls back to $XDG_RUNTIME_DIR/bus and
# would forward activate to the user's live instance (which then exits us).
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/panebox-test-bus"
DISPLAY = os.environ.get("DISPLAY", ":99")
os.environ["GDK_BACKEND"] = "x11"
os.environ["GSK_RENDERER"] = "cairo"
os.environ["NO_AT_BRIDGE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

from panebox.app import PaneBoxApplication  # noqa: E402

libX11 = ctypes.CDLL("libX11.so.6")
libXtst = ctypes.CDLL("libXtst.so.6")
libX11.XOpenDisplay.restype = ctypes.c_void_p
libXtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
libXtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_bool, ctypes.c_ulong]
XDISPLAY = libX11.XOpenDisplay(DISPLAY.encode())


def click_button(window, button) -> None:
    """XTest hover + click at the button's exact translated center.

    Every scheduled callback must return False — XTest calls return a status
    int that GLib would read as "keep firing", re-slamming the pointer.
    """
    alloc = button.get_allocation()
    assert alloc.width > 0, "close button not allocated"
    translated = button.translate_coordinates(window, 0, 0)
    assert translated is not None, "translate_coordinates failed"
    win_x, win_y, _w, _h = window.window_geometry()
    cx = int(win_x + translated[0] + alloc.width // 2)
    cy = int(win_y + translated[1] + alloc.height // 2)

    def _fake(motion: bool, press: bool = False) -> bool:
        if motion:
            libXtst.XTestFakeMotionEvent(XDISPLAY, -1, cx, cy, 0)
        else:
            libXtst.XTestFakeButtonEvent(XDISPLAY, 1, press, 0)
        libX11.XFlush(XDISPLAY)
        return False

    libXtst.XTestFakeMotionEvent(XDISPLAY, -1, cx - 40, cy, 0)
    libX11.XFlush(XDISPLAY)
    GLib.timeout_add(300, lambda: _fake(motion=True))
    GLib.timeout_add(600, lambda: _fake(motion=False, press=True))
    GLib.timeout_add(650, lambda: _fake(motion=False, press=False))


app = PaneBoxApplication()
state = {"phase": 0}


def phase1() -> bool:
    wm = app.widget_manager
    if wm is None or not wm.runtimes:
        return True  # restore not finished yet
    wm.create_feature_widget("Todo")
    state["phase"] = 1
    GLib.timeout_add(1500, phase2)
    return False


def phase2() -> bool:
    wm = app.widget_manager
    file_rt = next((r for r in wm.runtimes.values() if r.config.widgetKind == "File"), None)
    if file_rt is None:
        print("EXIT-ON-EMPTY: FAIL (no file widget to close)", flush=True)
        os._exit(3)
    click_button(file_rt.window, file_rt.shell.close_button)
    print("CLICKED-FILE-CLOSE", flush=True)
    GLib.timeout_add(3000, phase3)
    return False


def phase3() -> bool:
    # Still alive with the Todo window open — the early exit did NOT happen.
    print("SURVIVED-ONE-WINDOW", flush=True)
    wm = app.widget_manager
    todo_rt = next((r for r in wm.runtimes.values() if r.config.widgetKind == "Todo"), None)
    if todo_rt is None:
        print("EXIT-ON-EMPTY: FAIL (todo widget gone early)", flush=True)
        os._exit(3)
    state["phase"] = 2
    click_button(todo_rt.window, todo_rt.shell.close_button)
    print("CLICKED-LAST-CLOSE", flush=True)
    return False


GLib.timeout_add(2500, phase1)
status = app.run(sys.argv)
ok = state["phase"] >= 2
verdict = "PASS" if ok else f"FAIL (exited during phase {state['phase']})"
print(f"EXIT-ON-EMPTY: {verdict}", flush=True)
sys.exit(0 if ok else 3)
