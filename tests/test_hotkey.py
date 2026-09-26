"""GlobalHotkeyController: XGrabKey mapping, fallback kinds, dispatch."""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

from panebox.platform.hotkey import (  # noqa: E402
    MODIFIER_ALT,
    MODIFIER_CONTROL,
    MODIFIER_SHIFT,
    MODIFIER_WINDOWS,
    GlobalHotkeyController,
)

# X11 modifier masks (from the controller's private mapping)
SHIFT_MASK = 1 << 0
CONTROL_MASK = 1 << 2
MOD1_MASK = 1 << 3
MOD4_MASK = 1 << 6


class FakeEventThread:
    def __init__(self):
        self.grabbed = []
        self.ungrabbed = []

    def grab_key(self, name, modifiers, handler):
        self.grabbed.append((name, modifiers, handler))
        return True

    def ungrab_key(self, name, modifiers):
        self.ungrabbed.append((name, modifiers))


def make_controller() -> tuple[GlobalHotkeyController, FakeEventThread]:
    thread = FakeEventThread()
    return GlobalHotkeyController(thread), thread


def test_disabled_returns_without_grabbing():
    controller, thread = make_controller()
    status = controller.apply(False, "Chord", 0, Gdk.KEY_F7, lambda: None)
    assert status.state == "disabled"
    assert thread.grabbed == []


def test_chord_grabs_with_translated_modifiers():
    controller, thread = make_controller()
    modifiers = MODIFIER_CONTROL | MODIFIER_ALT | MODIFIER_SHIFT | MODIFIER_WINDOWS
    status = controller.apply(True, "Chord", modifiers, Gdk.KEY_F7, lambda: None)
    assert status.state == "registered"
    assert status.detail == ""
    name, grabbed_mods, _handler = thread.grabbed[0]
    assert name == "F7"
    assert grabbed_mods == SHIFT_MASK | CONTROL_MASK | MOD1_MASK | MOD4_MASK


def test_reserved_hook_kinds_degrade_to_bare_key():
    controller, thread = make_controller()
    status = controller.apply(True, "DoubleControl", MODIFIER_CONTROL, Gdk.KEY_F7, lambda: None)
    assert status.state == "registered"
    assert "DoubleControl" in status.detail
    _name, grabbed_mods, _handler = thread.grabbed[0]
    assert grabbed_mods == 0, "reserved kinds must grab the bare key"


def test_failed_grab_reports_failure():
    controller, thread = make_controller()
    thread.grab_key = lambda *_a: False
    status = controller.apply(True, "Chord", 0, Gdk.KEY_F7, lambda: None)
    assert status.state == "failed"


def test_clear_ungrabs_with_matching_parameters():
    controller, thread = make_controller()
    controller.apply(True, "Chord", MODIFIER_ALT, Gdk.KEY_space, lambda: None)
    controller.clear()
    assert thread.ungrabbed == [("space", MOD1_MASK)]


def test_reapply_ungrabs_previous_registration_first():
    controller, thread = make_controller()
    controller.apply(True, "Chord", 0, Gdk.KEY_F7, lambda: None)
    controller.apply(True, "Chord", MODIFIER_CONTROL, Gdk.KEY_F8, lambda: None)
    assert thread.ungrabbed == [("F7", 0)]
    assert thread.grabbed[-1][:2] == ("F8", CONTROL_MASK)


def test_fire_dispatches_latest_callback():
    controller, thread = make_controller()
    fired = []
    controller.apply(True, "Chord", 0, Gdk.KEY_F7, lambda: fired.append(1))
    _name, _mods, handler = thread.grabbed[0]
    handler("F7", 71)
    assert fired == [1]
