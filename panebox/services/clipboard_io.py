"""Clipboard write helpers (GTK4 PyGObject has no Gdk.Clipboard.set_text).

Central so every PaneBox write goes through one place: writers must also
mark the clipboard write scope so the Quick Capture monitor does not record
PaneBox's own writes back as "recent".
"""

from __future__ import annotations

from typing import Optional


def set_clipboard_text(clipboard, text: str) -> None:
    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gdk, GLib

    provider = Gdk.ContentProvider.new_for_bytes(
        "text/plain;charset=utf-8", GLib.Bytes.new((text or "").encode("utf-8"))
    )
    clipboard.set_content(provider)


def set_clipboard_texture(clipboard, image_path: str) -> bool:
    import gi

    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk

    try:
        texture = Gdk.Texture.new_from_filename(image_path)
    except Exception:
        return False
    if texture is None:
        return False
    clipboard.set_texture(texture)
    return True


def write_text_and_mark(clipboard, text: str) -> None:
    """Set text on the clipboard and mark it as a PaneBox write."""
    from . import clipboard_write_scope

    set_clipboard_text(clipboard, text)
    clipboard_write_scope.mark_text(text)


def write_image_and_mark(clipboard, image_path: str, fallback_text: Optional[str] = None) -> bool:
    """Set an image texture (text fallback); marks either payload."""
    from . import clipboard_write_scope

    if set_clipboard_texture(clipboard, image_path):
        # Image writes can't be matched by body; clear stale text marks so a
        # subsequent identical text copy still records.
        clipboard_write_scope.reset()
        return True
    if fallback_text is not None:
        write_text_and_mark(clipboard, fallback_text)
    return False
