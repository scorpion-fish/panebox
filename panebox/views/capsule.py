"""Capsule presentation (port of the collapsed-widget chrome).

A capsule replaces the whole widget window content while collapsed: a
rounded pill with the widget glyph, title, and (Smart/Summary modes) one
summary line. Content-mode widths/heights come from services.capsule; the
surface itself owns no policy — it reports clicks and pointer presence and
the widget manager decides what those mean.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..services import capsule as capsule_policy


class CapsuleSurface(Gtk.Box):
    """Collapsed widget face. Click → on_activate; hover → on_pointer_entered/left."""

    def __init__(
        self,
        config,
        summary_provider: Optional[Callable[[], str]] = None,
        on_activate: Optional[Callable[[], None]] = None,
        on_pointer_entered: Optional[Callable[[], None]] = None,
        on_pointer_left: Optional[Callable[[], None]] = None,
        content_mode: Optional[str] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.config = config
        self.summary_provider = summary_provider
        self.on_activate = on_activate
        self.on_pointer_entered = on_pointer_entered
        self.on_pointer_left = on_pointer_left
        self.content_mode = capsule_policy.normalize_content_mode(content_mode)
        self.set_css_classes(["capsule", f"capsule-{self.content_mode.lower()}"])
        self.is_pointer_inside = False

        self.glyph_label = Gtk.Label(label=self._glyph_for(config.widgetKind), visible=True)
        self.glyph_label.add_css_class("capsule-glyph")

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, visible=True)
        text_box.set_hexpand(True)
        self.title_label = Gtk.Label(label=config.name, xalign=0.0, ellipsize=True, single_line_mode=True, visible=True)
        self.title_label.add_css_class("capsule-title")
        self.summary_label = Gtk.Label(label="", xalign=0.0, ellipsize=True, single_line_mode=True, visible=True)
        self.summary_label.add_css_class("capsule-summary")
        self.summary_label.add_css_class("dim-label")
        text_box.append(self.title_label)
        text_box.append(self.summary_label)

        self.append(self.glyph_label)
        self.append(text_box)

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._clicked)
        self.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._pointer_enter)
        motion.connect("leave", self._pointer_left)
        self.add_controller(motion)

        self.refresh()

    # ---- content ----------------------------------------------------------------

    @staticmethod
    def _glyph_for(kind: str) -> str:
        from ..services import feature_widgets

        if feature_widgets.is_feature_widget(kind):
            return feature_widgets.descriptor_for(kind)["glyph"]
        return "▤"

    def set_content_mode(self, content_mode: str) -> None:
        mode = capsule_policy.normalize_content_mode(content_mode)
        if mode == self.content_mode:
            return
        self.content_mode = mode
        self.set_css_classes(["capsule", f"capsule-{mode.lower()}"])
        self.refresh()

    def refresh(self) -> None:
        """Re-read the summary and lay out for the current content mode."""
        summary = ""
        if self.summary_provider is not None and self.content_mode != "Minimal":
            try:
                summary = self.summary_provider() or ""
            except Exception:
                summary = ""
        self.title_label.set_visible(True)
        # Smart keeps a dedicated second line; Summary shares one row and only
        # shows the summary when there is one; Minimal is glyph + title only.
        self.summary_label.set_text(summary)
        if self.content_mode == "Smart":
            self.summary_label.set_visible(True)
        else:
            self.summary_label.set_visible(False)
        self.glyph_label.set_visible(self.content_mode != "Smart" or not summary)

    def queue_rebuild(self) -> None:  # language-switch hook name
        self.refresh()

    # ---- interaction --------------------------------------------------------------

    def _clicked(self, *_args) -> None:
        if self.on_activate is not None:
            self.on_activate()

    def _pointer_enter(self, *_args) -> None:
        self.is_pointer_inside = True
        if self.on_pointer_entered is not None:
            self.on_pointer_entered()

    def _pointer_left(self, *_args) -> None:
        self.is_pointer_inside = False
        if self.on_pointer_left is not None:
            self.on_pointer_left()

    # ---- smart hover plumbing -------------------------------------------------------

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> int:
        return GLib.timeout_add(max(0, int(delay_ms)), callback)

    def cancel(self, source_id: int) -> None:
        if source_id:
            GLib.source_remove(source_id)
