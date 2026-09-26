"""Placeholder surface for feature widgets whose content arrives later.

Keeps create/restore/tray plumbing uniform across all descriptor kinds: the
window, shell, move/resize, persistence, and enabled-state gating all work;
only the body is pending.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..i18n import t
from ..services import feature_widgets


class FeaturePlaceholder(Gtk.Box):
    def __init__(self, config, settings_service=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.config = config
        self.settings_service = settings_service
        descriptor = feature_widgets.descriptor_for(config.widgetKind)
        glyph = descriptor["glyph"] if descriptor else "◦"
        title = t(descriptor["title_key"]) if descriptor else config.widgetKind

        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(True)
        self.set_vexpand(True)

        glyph_label = Gtk.Label(label=glyph, visible=True)
        glyph_label.add_css_class("feature-placeholder-glyph")
        title_label = Gtk.Label(label=title, visible=True)
        title_label.add_css_class("feature-placeholder-title")
        self.append(glyph_label)
        self.append(title_label)
