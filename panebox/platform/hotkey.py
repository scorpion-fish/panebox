"""Global hotkey registration (port of Services/GlobalHotkeyService.cs).

Chord activations map directly to XGrabKey. DoubleControl / WindowsTap need a
low-level keyboard hook (Windows reserved-hotkey mechanism) that has no X11
equivalent without swallowing every keypress; they fall back to the plain key
grab and the limitation surfaces in Settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .event_thread import X11EventThread

# HotkeyModifierKeys bitfield (GlobalHotkeyService.cs)
MODIFIER_ALT = 0x0001
MODIFIER_CONTROL = 0x0002
MODIFIER_SHIFT = 0x0004
MODIFIER_WINDOWS = 0x0008

_MODIFIER_TO_X11 = [
    (MODIFIER_ALT, 1 << 3),  # Mod1Mask
    (MODIFIER_CONTROL, 1 << 2),  # ControlMask
    (MODIFIER_SHIFT, 1 << 0),  # ShiftMask
    (MODIFIER_WINDOWS, 1 << 6),  # Mod4Mask
]

# The search settings slice stores raw X11 masks (Shift=1, Ctrl=4, Alt=8,
# Super=64 — the C# encoding); apply() takes the app flags above.
_X11_TO_MODIFIER = {mask: flag for flag, mask in _MODIFIER_TO_X11}


def modifiers_from_x11(x11_modifiers: int) -> int:
    """Convert an X11 modifier mask (as stored by the search slice) to the
    app-level MODIFIER_* flags apply() expects."""
    flags = 0
    for mask, flag in _X11_TO_MODIFIER.items():
        if x11_modifiers & mask:
            flags |= flag
    return flags


@dataclass
class HotkeyStatus:
    state: str  # "registered" | "disabled" | "unsupported-kind" | "failed"
    detail: str = ""


class GlobalHotkeyController:
    def __init__(self, event_thread: X11EventThread, log: Callable[[str], None] = lambda _m: None):
        self.event_thread = event_thread
        self.log = log
        self.on_triggered: Optional[Callable[[], None]] = None
        self._current_name = ""
        self._current_modifiers = 0

    # ---- apply / clear ------------------------------------------------------------

    def apply(
        self,
        enabled: bool,
        activation_kind: str,
        modifiers: int,
        keysym: int,
        on_triggered: Callable[[], None],
    ) -> HotkeyStatus:
        self.clear()
        if not enabled:
            return HotkeyStatus("disabled")
        self.on_triggered = on_triggered

        x11_modifiers = 0
        for flag, mask in _MODIFIER_TO_X11:
            if modifiers & flag:
                x11_modifiers |= mask

        key_name = _keysym_name(keysym)
        if not key_name:
            return HotkeyStatus("failed", f"unknown keysym {keysym}")

        detail = ""
        # Reserved-hook kinds degrade to a bare-key grab on X11.
        if activation_kind in ("DoubleControl", "WindowsTap"):
            detail = f"{activation_kind} unsupported on X11; using bare key"
            x11_modifiers = 0

        if self.event_thread.grab_key(key_name, x11_modifiers, self._fire):
            self._current_name = key_name
            self._current_modifiers = x11_modifiers
            return HotkeyStatus("registered", detail)
        return HotkeyStatus("failed", f"grab failed for {key_name}")

    def clear(self) -> None:
        if self._current_name:
            self.event_thread.ungrab_key(self._current_name, self._current_modifiers)
            self._current_name = ""
            self._current_modifiers = 0

    # ---- dispatch ---------------------------------------------------------------------

    def _fire(self, _name: str, _keycode: int) -> None:
        if self.on_triggered:
            self.on_triggered()


def _keysym_name(keysym: int) -> str:
    try:
        import gi

        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk

        name = Gdk.keyval_name(keysym)
        return name or ""
    except Exception:
        return ""
