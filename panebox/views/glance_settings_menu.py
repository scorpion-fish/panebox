"""Glance in-widget settings popover (port of the context-menu part of
GlanceWidgetContextMenuBuilder): layout, display elements, background source,
rotation, traditional calendar, readability.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..i18n import t
from ..models.glance import (
    GlanceBackgroundSource,
    GlanceDisplayElement,
    GlanceLayoutMode,
    GlanceReadabilityMode,
    GlanceTraditionalCalendarMode,
)
from .glance_widget import ROTATION_INTERVAL_KEYS

_LAYOUT_KEYS = {
    GlanceLayoutMode.IMMERSIVE: "Glance.Layout.Immersive",
    GlanceLayoutMode.CENTERED: "Glance.Layout.Centered",
    GlanceLayoutMode.EDITORIAL: "Glance.Layout.Editorial",
    GlanceLayoutMode.CALENDAR: "Glance.Layout.Calendar",
}

_DISPLAY_KEYS = {
    GlanceDisplayElement.TIME: "Glance.Display.Time",
    GlanceDisplayElement.DATE: "Glance.Display.Date",
    GlanceDisplayElement.YEAR: "Glance.Display.Year",
    GlanceDisplayElement.WEEKDAY: "Glance.Display.Weekday",
    GlanceDisplayElement.CALENDAR: "Glance.Display.Calendar",
}

_READABILITY_KEYS = {
    GlanceReadabilityMode.NONE: "Glance.Readability.None",
    GlanceReadabilityMode.SOFT: "Glance.Readability.Soft",
    GlanceReadabilityMode.STRONG: "Glance.Readability.Strong",
}

_TRADITIONAL_KEYS = {
    GlanceTraditionalCalendarMode.NONE: "Glance.TraditionalCalendar.None",
    GlanceTraditionalCalendarMode.AUTO: "Glance.TraditionalCalendar.Auto",
    GlanceTraditionalCalendarMode.CHINESE_LUNAR: "Glance.TraditionalCalendar.ChineseLunar",
}


def build_glance_menu(surface) -> Gtk.Popover:
    settings = surface.settings

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
    box.add_css_class("glance-menu")
    popover = Gtk.Popover()
    popover.set_child(box)

    box.append(_section(box, t("Glance.Layout.Title")))
    for layout, key in _LAYOUT_KEYS.items():
        box.append(_check(t(key), settings.layout == layout, lambda checked, chosen=layout: surface.set_layout(chosen)))

    box.append(_section(box, t("Glance.Display.Title")))
    for element, key in _DISPLAY_KEYS.items():
        from ..services.glance_widget_settings_policy import is_display_element_visible

        current = is_display_element_visible(settings, element)
        box.append(
            _check(
                t(key),
                current,
                lambda checked, e=element: surface.set_display_element(e, checked),
            )
        )

    box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, visible=True))
    box.append(_section(box, t("Glance.Background.Title")))
    box.append(
        _action(
            t("Glance.Background.Bing"),
            lambda: surface.set_background_source(GlanceBackgroundSource.BING),
        )
    )
    box.append(
        _action(
            t("Glance.Background.Online"),
            lambda: surface.set_background_source(GlanceBackgroundSource.ONLINE),
        )
    )
    box.append(_action(t("Glance.Background.Files"), _choose_files(surface)))
    box.append(_action(t("Glance.Background.Folder"), _choose_folder(surface)))
    if settings.backgroundSource == GlanceBackgroundSource.LOCAL_FILES:
        box.append(_label(t("Glance.Background.LocalSummary").format(len(settings.localImagePaths))))
    elif settings.backgroundSource == GlanceBackgroundSource.LOCAL_FOLDER:
        box.append(_label(settings.localFolderPath or t("Glance.Background.Folder")))

    box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, visible=True))
    box.append(_section(box, t("Glance.Rotation.Title")))
    current_interval = settings.rotationIntervalMinutes
    for minutes, key in ROTATION_INTERVAL_KEYS:
        box.append(
            _check(
                t(key),
                abs(current_interval - minutes) < 0.0001,
                lambda checked, m=minutes: surface.set_rotation_interval(m),
            )
        )

    box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, visible=True))
    box.append(_section(box, t("Glance.TraditionalCalendar.Title")))
    from ..services.glance_traditional import resolve_mode

    resolved = resolve_mode(settings.traditionalCalendarMode, _culture_hint())
    for mode, key in (
        (GlanceTraditionalCalendarMode.NONE, _TRADITIONAL_KEYS[GlanceTraditionalCalendarMode.NONE]),
        (GlanceTraditionalCalendarMode.AUTO, "Glance.TraditionalCalendar.Auto"),
        (
            GlanceTraditionalCalendarMode.CHINESE_LUNAR,
            _TRADITIONAL_KEYS[GlanceTraditionalCalendarMode.CHINESE_LUNAR],
        ),
    ):
        label = t(key)
        if mode == GlanceTraditionalCalendarMode.AUTO:
            label = label.format(
                t(_TRADITIONAL_KEYS[resolved]) if resolved != GlanceTraditionalCalendarMode.AUTO else ""
            )
        box.append(
            _check(
                label,
                settings.traditionalCalendarMode == mode,
                lambda checked, m=mode: surface.set_traditional_calendar(m),
            )
        )
    box.append(
        _check(
            t("Glance.Festivals.Title"),
            settings.showChineseFestivals,
            lambda checked: surface.set_chinese_festivals(checked),
        )
    )

    box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, visible=True))
    box.append(_section(box, t("Glance.Readability.Title")))
    for mode, key in _READABILITY_KEYS.items():
        box.append(
            _check(
                t(key),
                settings.readability == mode,
                lambda checked, m=mode: surface.set_readability(m),
            )
        )

    box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL, visible=True))
    transparency = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
    transparency.set_value(settings.backgroundImageTransparency)
    transparency.set_draw_value(False)
    transparency.set_hexpand(True)
    transparency.connect("value-changed", lambda scale: surface.set_background_transparency(scale.get_value()))
    transparency_label = Gtk.Label(label=t("Glance.Background.Transparency.Title"), xalign=0.0, visible=True)
    transparency_label.add_css_class("glance-menu-caption")
    transparency_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
    transparency_row.append(transparency_label)
    transparency_row.append(transparency)
    transparency.set_visible(True)
    box.append(transparency_row)

    return popover


def _culture_hint() -> str:
    from .. import i18n

    return i18n.current_language() or "zh-CN"


def _section(_parent: Gtk.Widget, title: str) -> Gtk.Widget:
    label = Gtk.Label(label=title, xalign=0.0, visible=True)
    label.add_css_class("glance-menu-section")
    return label


def _label(text: str) -> Gtk.Widget:
    label = Gtk.Label(label=text, xalign=0.0, visible=True)
    label.add_css_class("glance-menu-caption")
    label.set_ellipsize(True)
    return label


def _action(title: str, handler) -> Gtk.Widget:
    button = Gtk.Button(label=title, visible=True)
    button.set_halign(Gtk.Align.START)
    button.add_css_class("glance-menu-action")
    button.connect("clicked", lambda *_: handler())
    return button


def _check(title: str, active: bool, handler) -> Gtk.Widget:
    check = Gtk.CheckButton(label=title, visible=True)
    check.set_active(active)
    check.connect("toggled", lambda toggle: handler(toggle.get_active()) if toggle.get_active() else None)
    return check


def _choose_files(surface):
    def handler() -> None:
        dialog = Gtk.FileChooserNative.new(
            t("Glance.Background.ChooseFiles"), None, Gtk.FileChooserAction.OPEN, None, None
        )
        dialog.set_select_multiple(True)
        dialog.set_modal(True)
        root = surface.get_root()
        if isinstance(root, Gtk.Window):
            dialog.set_transient_for(root)

        def respond(_dlg, response: int):
            try:
                if response == Gtk.ResponseType.ACCEPT:
                    paths = [f.get_path() for f in dialog.get_files() if f.get_path()]
                    if paths:
                        surface.choose_local_files(paths)
            finally:
                dialog.destroy()

        dialog.connect("response", respond)
        dialog.show()

    return handler


def _choose_folder(surface):
    def handler() -> None:
        dialog = Gtk.FileChooserNative.new(
            t("Glance.Background.ChooseFolder"),
            None,
            Gtk.FileChooserAction.SELECT_FOLDER,
            None,
            None,
        )
        dialog.set_modal(True)
        root = surface.get_root()
        if isinstance(root, Gtk.Window):
            dialog.set_transient_for(root)

        def respond(_dlg, response: int):
            try:
                if response == Gtk.ResponseType.ACCEPT:
                    surface.choose_local_folder(dialog.get_file().get_path() if dialog.get_file() else None)
            finally:
                dialog.destroy()

        dialog.connect("response", respond)
        dialog.show()

    return handler
