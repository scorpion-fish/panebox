"""Widget shell chrome: title bar with editable title + hover buttons.

PaneBox's widget chrome: glyph, double-click-to-edit title (Placeholder
"Enter widget name"), hover-revealed action buttons (more menu, collapse,
close), locked-position drag affordance. The shell owns no file logic; it
emits callbacks and hosts a content widget.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .. import i18n


class WidgetShell(Gtk.Box):
    """Vertical: title bar / separator / content. Set as window child."""

    def __init__(
        self,
        title: str = "",
        glyph: str = "◆",
        on_renamed: Optional[Callable[[str], bool]] = None,
        on_close: Optional[Callable[[], None]] = None,
        on_collapse: Optional[Callable[[], None]] = None,
        on_menu: Optional[Callable[[Gtk.Widget], None]] = None,
        title_buttons_visible: bool = True,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_renamed = on_renamed
        self.on_close = on_close
        self.on_collapse = on_collapse
        self.on_menu = on_menu

        self.title_label = Gtk.Label(
            label=title,
            xalign=0.0,
            ellipsize=True,
            max_width_chars=18,
            visible=True,
        )
        self.title_label.add_css_class("widget-title")

        self.glyph_label = Gtk.Label(label=glyph, visible=True)
        self.glyph_label.add_css_class("widget-glyph")

        # Buttons reveal on hover (PaneBox behavior).
        self.buttons_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            reveal_child=title_buttons_visible,
            transition_duration=120,
            visible=True,
        )

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, visible=True)
        button_box.add_css_class("title-buttons")
        self.menu_button = self._flat_button("…", i18n.t("Widget.Tooltip.More"), self._menu_clicked)
        self.collapse_button = self._flat_button("—", i18n.t("Widget.Compact.Collapse"), self._collapse_clicked)
        self.close_button = self._flat_button("✕", i18n.t("Widget.Tooltip.DeleteWidget"), self._close_clicked)
        button_box.append(self.menu_button)
        button_box.append(self.collapse_button)
        button_box.append(self.close_button)
        self.buttons_revealer.set_child(button_box)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        title_row.append(self.glyph_label)
        title_row.append(self.title_label)
        title_row.set_hexpand(True)

        self.title_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, visible=True)
        self.title_bar.add_css_class("widget-titlebar")
        self.title_bar.append(title_row)
        self.title_bar.append(self.buttons_revealer)

        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_a: self.buttons_revealer.set_reveal_child(True))
        hover.connect("leave", lambda *_a: self.buttons_revealer.set_reveal_child(title_buttons_visible))
        self.title_bar.add_controller(hover)

        self._editing = False
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._on_title_clicked)
        self.title_label.add_controller(click)

        self.append(self.title_bar)
        self.group_strip_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.append(self.group_strip_slot)
        self.append(Gtk.Separator(visible=True))

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.content.add_css_class("file-surface")
        self.content.set_vexpand(True)
        self.content.set_hexpand(True)
        self.append(self.content)

        self.add_css_class("widget-shell")

    # ---- content -----------------------------------------------------------------

    def set_content(self, widget: Gtk.Widget) -> None:
        while self.content.get_first_child() is not None:
            self.content.remove(self.content.get_first_child())
        self.content.append(widget)

    def set_group_strip(self, widget: Optional[Gtk.Widget]) -> None:
        """Host the group member switcher row (or clear it)."""
        while self.group_strip_slot.get_first_child() is not None:
            self.group_strip_slot.remove(self.group_strip_slot.get_first_child())
        if widget is not None:
            self.group_strip_slot.append(widget)

    # ---- title ----------------------------------------------------------------------

    def set_title(self, title: str) -> None:
        if not self._editing:
            self.title_label.set_text(title)

    def get_title(self) -> str:
        return self.title_label.get_text()

    def set_buttons_visible(self, visible: bool) -> None:
        self.buttons_revealer.set_reveal_child(visible)
        self.collapse_button.set_visible(visible)
        self.close_button.set_visible(visible)

    def set_collapse_available(self, available: bool) -> None:
        """Hide the collapse button when the behavior is Always-expanded."""
        self.collapse_button.set_visible(available)

    def set_menu_available(self, available: bool) -> None:
        self.menu_button.set_visible(available)

    def _on_title_clicked(self, _click: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        if self._editing:
            return
        # Single click that looks like a double click starts editing.
        current = GLib.get_monotonic_time()
        last = getattr(self, "_last_title_click_us", 0)
        self._last_title_click_us = current
        if current - last < 450_000:
            self.begin_title_edit()

    def begin_title_edit(self) -> None:
        if self._editing:
            return
        self._editing = True
        entry = Gtk.Entry(visible=True)
        entry.add_css_class("widget-title-entry")
        entry.set_text(self.title_label.get_text())
        entry.set_placeholder_text(i18n.t("Widget.TitlePlaceholder"))
        entry.set_hexpand(True)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_edit_key)
        entry.add_controller(key)
        focus_loss = Gtk.EventControllerFocus()
        focus_loss.connect("leave", lambda *_a: self.commit_title_edit())
        entry.add_controller(focus_loss)

        self._edit_entry = entry
        box = self.title_label.get_parent()
        box.remove(self.title_label)
        box.prepend(entry)
        entry.grab_focus()

    def commit_title_edit(self) -> None:
        if not self._editing:
            return
        entry = getattr(self, "_edit_entry", None)
        self._editing = False
        new_title = entry.get_text().strip() if entry is not None else ""
        box = entry.get_parent() if entry is not None else None
        if box is not None:
            box.remove(entry)
            box.prepend(self.title_label)
        if new_title and new_title != self.title_label.get_text():
            accepted = True
            if self.on_renamed:
                accepted = self.on_renamed(new_title)
            if accepted is not False:
                self.title_label.set_text(new_title)
        self._edit_entry = None

    def _on_edit_key(self, _key: Gtk.EventControllerKey, keyval: int, _k2: int, _state) -> bool:
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            self.commit_title_edit()
            return True
        if keyval == Gdk.KEY_Escape:
            entry = getattr(self, "_edit_entry", None)
            if entry is not None:
                entry.set_text(self.title_label.get_text())
            self.commit_title_edit()
            return True
        return False

    # ---- buttons --------------------------------------------------------------------

    def _flat_button(self, label: str, tooltip: str, on_clicked) -> Gtk.Button:
        button = Gtk.Button(label=label, visible=True)
        button.set_tooltip_text(tooltip)
        button.set_has_frame(False)
        button.set_can_focus(False)
        button.set_focus_on_click(False)
        button.connect("clicked", lambda *_a: on_clicked())
        return button

    def _menu_clicked(self) -> None:
        if self.on_menu is not None:
            self.on_menu(self.menu_button)

    def _collapse_clicked(self) -> None:
        if self.on_collapse is not None:
            self.on_collapse()

    def _close_clicked(self) -> None:
        if self.on_close is not None:
            self.on_close()
