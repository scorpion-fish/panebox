"""Feature-widget picker — small popover-style window listing the descriptor
table (glyph, localized name, default size). Used from the tray's
"添加功能格子" entry."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..i18n import t
from ..services import feature_widgets


class FeaturePickerWindow(Gtk.Window):
    def __init__(self, application, on_pick: Callable[[str], None]):
        super().__init__(application=application, visible=True)
        self.on_pick = on_pick
        self.set_title(t("Common.AddFeatureWidget"))
        self.set_default_size(260, -1)
        self.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=True)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        title = Gtk.Label(label=t("Common.AddFeatureWidget"), xalign=0.0, visible=True)
        title.add_css_class("settings-group-title")
        box.append(title)

        listbox = Gtk.ListBox(visible=True)
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("settings-group")
        for descriptor in feature_widgets.FEATURE_DESCRIPTORS:
            listbox.append(_PickerRow(descriptor))
        listbox.connect("row-activated", self._on_row_activated)
        box.append(listbox)
        self.set_child(box)

    def _on_row_activated(self, _box, row) -> None:
        kind = row.descriptor["kind"]
        self.destroy()
        self.on_pick(kind)


class _PickerRow(Gtk.ListBoxRow):
    def __init__(self, descriptor: dict):
        super().__init__(visible=True)
        self.descriptor = descriptor

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, visible=True)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        glyph = Gtk.Label(label=descriptor["glyph"], visible=True)
        glyph.add_css_class("feature-placeholder-glyph")
        glyph.set_size_request(24, -1)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, visible=True)
        name = Gtk.Label(label=t(descriptor["title_key"]), xalign=0.0, visible=True)
        name.add_css_class("settings-row-title")
        size = Gtk.Label(label=f"{descriptor['width']}×{descriptor['height']}", xalign=0.0, visible=True)
        size.add_css_class("settings-row-description")
        text.append(name)
        text.append(size)
        box.append(glyph)
        box.append(text)
        self.set_child(box)
