"""Settings window (port of Views/SettingsWindow for the M1 sections).

M1 pages: General, Appearance, File widgets, Shortcuts & interaction,
Advanced (performance). M2/M3 features add their own pages later; nav uses
the same Settings.Nav.* tags as PaneBox so deep links stay stable.

Every change applies immediately and persists through the debounced save —
PaneBox has no OK/Cancel. Side effects beyond the model (theme, language,
hotkey grab, autostart file, live widget appearance) go through hooks
supplied by the application.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .. import i18n
from ..i18n import fmt, t
from ..platform.hotkey import (
    MODIFIER_ALT,
    MODIFIER_CONTROL,
    MODIFIER_SHIFT,
    MODIFIER_WINDOWS,
)
from ..services.search_view_model import build_hotkey_hint

# Native language names (not localized content).
LANGUAGE_DISPLAY_NAMES = {
    "en-US": "English (United States)",
    "zh-CN": "简体中文（中国）",
    "zh-TW": "繁體中文（台灣）",
    "ja-JP": "日本語",
    "de-DE": "Deutsch",
    "pt-BR": "Português (Brasil)",
    "hi-IN": "हिन्दी",
    "es-ES": "Español",
    "fr-FR": "Français",
    "ar-SA": "العربية",
    "bn-BD": "বাংলা",
}

THEMES = ("System", "Light", "Dark")

# fileNameLineCount value -> settings string key
NAME_LINE_KEYS = {0: "Hidden", 1: "Single", 2: "Double"}

# (name, kind, modifiers, keysym) — same preset set as the C# window.
HOTKEY_PRESETS = (
    ("F7", "Chord", 0, Gdk.KEY_F7),
    ("AltSpace", "Chord", MODIFIER_ALT, Gdk.KEY_space),
    ("WinSpace", "Chord", MODIFIER_WINDOWS, Gdk.KEY_space),
    ("DoubleControl", "DoubleControl", 0, Gdk.KEY_F7),
    ("WindowsTap", "WindowsTap", 0, Gdk.KEY_F7),
)


def hotkey_preset_label(name: str) -> str:
    return t(f"Settings.GlobalHotkey.Preset.{name}")


# Feature-widgets page tables (order mirrors FEATURE_DESCRIPTORS).
# (kind, enable-row title key, description key or "")
FEATURE_ROWS = (
    ("QuickCapture", "QuickCapture.Name", "Settings.QuickCapture.Description"),
    ("Todo", "Todo.Title", "Settings.Todo.Description"),
    ("Music", "Music.Title", ""),
    ("Weather", "Weather.Title", ""),
    ("Search", "Search.Title", "Settings.Search.Description"),
    ("Glance", "Glance.Title", ""),
)

TODO_FILTERS = ("All", "Active", "Today", "ThisWeek", "ThisMonth", "Important", "Completed")

# minutes -> label key (Settings.Todo.ReminderOffset.*)
REMINDER_OFFSETS = (0, 5, 10, 15, 30, 60, 1440)

# Default search hotkey (searchHotkeyModifiers/searchHotkeyKey defaults).
# The search slice stores raw X11 masks (Shift=1, Ctrl=4, Alt=8, Super=64).
SEARCH_HOTKEY_DEFAULT = (1 << 3, 0x64)  # Alt+D


@dataclass
class SettingsHooks:
    save: Callable[[], None]
    apply_theme: Callable[[str], None]
    apply_language: Callable[[], None]
    apply_hotkey: Callable[[], object]  # returns HotkeyStatus
    apply_autostart: Callable[[bool], None]
    apply_appearance: Callable[[], None]  # push opacity/density to live widgets
    log: Callable[[str], None] = lambda _msg: None
    # Feature-widgets page: create/close the widget when the gate flips,
    # push changed settings into live surfaces of that kind.
    set_feature_enabled: Optional[Callable[[str, bool], None]] = None
    refresh_feature: Optional[Callable[[str], None]] = None
    # Widget-groups page: default style/title/wheel/hover + dissolve all.
    refresh_groups: Optional[Callable[[], None]] = None
    dissolve_all_groups: Optional[Callable[[], bool]] = None
    # Desktop-organization entry: open the preview window / undo the latest run.
    open_organize: Optional[Callable[[], None]] = None
    undo_latest_organization: Optional[Callable[[], str]] = None
    # Cloud backup: the CloudBackupService (page is hidden when absent).
    cloud_backup: Optional[object] = None
    # Diagnostics bundle: destination dir -> archive path (raises on failure).
    export_diagnostics: Optional[Callable[[str], object]] = None
    # Update check: runs the check off-thread, returns UpdateCheckResult.
    check_updates: Optional[Callable[[], object]] = None


class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, application, settings_service, hooks: SettingsHooks):
        super().__init__(application=application)
        self.settings_service = settings_service
        self.hooks = hooks
        self.set_title(t("Settings.WindowTitle"))
        self.set_default_size(880, 640)

        self._nav_rows: dict[str, Gtk.ListBoxRow] = {}
        self._stack: Optional[Gtk.Stack] = None
        self._hotkey_status_label: Optional[Gtk.Label] = None
        self._hotkey_banner: Optional[Gtk.Label] = None
        self._hotkey_capture_button: Optional[Gtk.ToggleButton] = None
        self._recording = False
        self._search_hotkey_status_label: Optional[Gtk.Label] = None
        self._search_capture_button: Optional[Gtk.ToggleButton] = None
        self._search_recording = False
        self._update_status: Optional[Gtk.Label] = None
        self._update_check_result = None

        self._build()
        self.connect("close-request", self._on_close_request)

    # ---- layout ------------------------------------------------------------------

    def _build(self) -> None:
        self.set_title(t("Settings.WindowTitle"))
        header = Gtk.HeaderBar(visible=True)
        self.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, visible=True)
        root.add_css_class("settings-root")

        nav = Gtk.ListBox(visible=True)
        nav.add_css_class("settings-nav")
        nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        nav.connect("row-activated", self._on_nav_activated)

        tags = (
            "General",
            "Appearance",
            "FileWidget",
            "FeatureWidgets",
            "WidgetGroups",
            "Interaction",
            "Advanced",
        )
        if self.hooks.cloud_backup is not None:
            tags += ("CloudBackup",)
        for tag in tags:
            # CloudBackup has no Settings.Nav.* key — reuse the page title.
            nav_text = t("Settings.CloudBackup.Title") if tag == "CloudBackup" else t(f"Settings.Nav.{tag}")
            label = Gtk.Label(label=nav_text, xalign=0.0, visible=True)
            row = Gtk.ListBoxRow(visible=True)
            row.activatable = True
            row.set_child(label)
            nav.append(row)
            self._nav_rows[tag] = row

        nav_scroll = Gtk.ScrolledWindow(visible=True)
        nav_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        nav_scroll.set_min_content_width(190)
        nav_scroll.set_child(nav)

        self._stack = Gtk.Stack(visible=True)
        self._stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._stack.add_named(self._wrap_scroll(self._page_general()), "General")
        self._stack.add_named(self._wrap_scroll(self._page_appearance()), "Appearance")
        self._stack.add_named(self._wrap_scroll(self._page_file_widget()), "FileWidget")
        self._stack.add_named(self._wrap_scroll(self._page_feature_widgets()), "FeatureWidgets")
        self._stack.add_named(self._wrap_scroll(self._page_widget_groups()), "WidgetGroups")
        self._stack.add_named(self._wrap_scroll(self._page_interaction()), "Interaction")
        self._stack.add_named(self._wrap_scroll(self._page_advanced()), "Advanced")
        if self.hooks.cloud_backup is not None:
            self._stack.add_named(self._wrap_scroll(self._page_cloud_backup()), "CloudBackup")

        root.append(nav_scroll)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, visible=True))
        root.append(self._stack)
        self.set_child(root)

        nav.select_row(self._nav_rows["General"])
        self._stack.set_visible_child_name("General")

    def rebuild(self) -> None:
        """Re-create the whole tree (language switch retranslates everything)."""
        current = self._stack.get_visible_child_name() if self._stack is not None else None
        child = self.get_child()
        if child is not None:
            self.set_child(None)
        self._nav_rows.clear()
        self._hotkey_status_label = None
        self._hotkey_banner = None
        self._hotkey_capture_button = None
        self._search_hotkey_status_label = None
        self._search_capture_button = None
        self._build()
        if current is not None and current in self._nav_rows:
            self.show_section(current)

    def show_section(self, section: str) -> None:
        if section in self._nav_rows:
            self._stack.set_visible_child_name(section)
            self._nav_rows[section].activate()

    def _on_nav_activated(self, _nav: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        for tag, nav_row in self._nav_rows.items():
            if nav_row is row:
                self._stack.set_visible_child_name(tag)
                break

    def _on_close_request(self, *_a) -> bool:
        # A failed save must never block closing the window.
        try:
            self.settings_service.flush_pending_save()
        except Exception:
            pass
        # Drop the application's reference: a later open must build a fresh
        # window, not present() this destroyed one (a zombie that shows
        # nothing and ignores its close button).
        app = self.get_application()
        if app is not None and getattr(app, "settings_window", None) is self:
            app.settings_window = None
        return False

    # ---- row helpers -----------------------------------------------------------------

    def _page(self, title_key: str, description_key: str = "") -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, visible=True)
        box.add_css_class("settings-page")
        title = Gtk.Label(label=t(title_key), xalign=0.0, visible=True)
        title.add_css_class("settings-page-title")
        box.append(title)
        if description_key:
            subtitle = Gtk.Label(label=t(description_key), xalign=0.0, wrap=True, visible=True)
            subtitle.add_css_class("settings-page-subtitle")
            box.append(subtitle)
        return box

    @staticmethod
    def _wrap_scroll(box: Gtk.Box) -> Gtk.ScrolledWindow:
        scroll = Gtk.ScrolledWindow(visible=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(box)
        return scroll

    def _group(self, page: Gtk.Box, title_key: str = "") -> Gtk.ListBox:
        if title_key:
            label = Gtk.Label(label=t(title_key), xalign=0.0, visible=True)
            label.add_css_class("settings-group-title")
            page.append(label)
        group = Gtk.ListBox(visible=True)
        group.add_css_class("settings-group")
        group.set_selection_mode(Gtk.SelectionMode.NONE)
        page.append(group)
        return group

    def _row(
        self,
        group: Gtk.ListBox,
        title: str,
        description: str = "",
        control: Optional[Gtk.Widget] = None,
    ) -> Gtk.Box:
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, visible=True)
        row_box.add_css_class("settings-row")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
        texts.set_valign(Gtk.Align.CENTER)
        texts.set_hexpand(True)
        title_label = Gtk.Label(label=title, xalign=0.0, wrap=True, visible=True)
        title_label.add_css_class("settings-row-title")
        texts.append(title_label)
        if description:
            desc_label = Gtk.Label(label=description, xalign=0.0, wrap=True, visible=True)
            desc_label.add_css_class("settings-row-description")
            texts.append(desc_label)
        row_box.append(texts)
        if control is not None:
            control.set_valign(Gtk.Align.CENTER)
            row_box.append(control)
        row = Gtk.ListBoxRow(visible=True)
        row.set_activatable(False)
        row.set_child(row_box)
        group.append(row)
        return row_box

    # ---- controls ----------------------------------------------------------------------

    def _switch(self, value: bool, on_change: Callable[[bool], None]) -> Gtk.Switch:
        switch = Gtk.Switch(visible=True)
        switch.set_active(value)
        # Returning False lets the default handler flip the visual state.
        switch.connect("state-set", lambda _w, state: on_change(state) or False)
        return switch

    def _dropdown(self, options: list[str], selected: str, on_change: Callable[[str], None]) -> Gtk.DropDown:
        drop = Gtk.DropDown.new_from_strings(options)
        drop.set_visible(True)
        if selected in options:
            drop.set_selected(options.index(selected))
        drop.connect("notify::selected", lambda w, _p: on_change(options[w.get_selected()]))
        return drop

    def _spin(
        self,
        value: float,
        minimum: float,
        maximum: float,
        step: float,
        digits: int,
        on_change: Callable[[float], None],
    ) -> Gtk.SpinButton:
        spin = Gtk.SpinButton.new_with_range(minimum, maximum, step)
        spin.set_visible(True)
        spin.set_digits(digits)
        spin.set_value(value)
        spin.connect("value-changed", lambda w: on_change(w.get_value()))
        return spin

    # ---- General ---------------------------------------------------------------------------

    def _page_general(self) -> Gtk.Widget:
        core = self.settings_service.settings.core
        page = self._page("Settings.Nav.General")

        group = self._group(page, "Settings.Group.General.Title")

        language_options = ["System"] + list(i18n.SUPPORTED_LANGUAGES)
        display = [t("Settings.Theme.System")] + [
            LANGUAGE_DISPLAY_NAMES.get(code, code) for code in i18n.SUPPORTED_LANGUAGES
        ]
        language_drop = Gtk.DropDown.new_from_strings(display)
        language_drop.set_visible(True)
        current = core.language if core.language in language_options else "System"
        language_drop.set_selected(language_options.index(current))
        language_drop.connect(
            "notify::selected",
            lambda w, _p: self._change_language(language_options[w.get_selected()]),
        )
        self._row(group, t("Settings.Language.Title"), t("Settings.Language.Description"), language_drop)

        self._row(
            group,
            t("Settings.AutoStart.Title"),
            t("Settings.AutoStart.Description"),
            self._switch(core.autoStart, self._change_autostart),
        )
        return page

    def _change_language(self, value: str) -> None:
        core = self.settings_service.settings.core
        if core.language == value:
            return
        core.language = value
        self.hooks.save()
        i18n.set_language(value if value != "System" else None)
        self.hooks.apply_language()  # tray + widgets
        self.rebuild()  # retranslate this window in place

    def _change_autostart(self, enabled: bool) -> None:
        self.settings_service.settings.core.autoStart = enabled
        self.hooks.apply_autostart(enabled)
        self.hooks.save()

    # ---- Appearance --------------------------------------------------------------------------

    def _page_appearance(self) -> Gtk.Widget:
        shell = self.settings_service.settings.widgetShell
        page = self._page("Settings.Section.Appearance", "Settings.Theme.Description")

        group = self._group(page, "Settings.Theme.Title")
        theme_drop = self._dropdown(
            [t(f"Settings.Theme.{name}") for name in THEMES],
            t(f"Settings.Theme.{core_theme(self.settings_service)}"),
            self._change_theme,
        )
        self._row(group, t("Settings.Theme.Title"), "", theme_drop)

        group = self._group(page, "Settings.Group.AppVisual.Title")
        opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.10, 1.0, 0.05)
        opacity.set_visible(True)
        opacity.set_size_request(180, -1)
        opacity.set_value(shell.widgetOpacity)
        opacity.set_draw_value(True)
        opacity.set_digits(2)
        opacity.connect("value-changed", lambda w: self._change_appearance("widgetOpacity", w.get_value()))
        self._row(group, t("Settings.Opacity.Title"), t("Settings.Opacity.Description"), opacity)

        width_spin = self._spin(
            shell.defaultWidgetWidth,
            50,
            2000,
            10,
            0,
            lambda v: self._change_appearance("defaultWidgetWidth", v),
        )
        self._row(group, t("Settings.DefaultWidth.Title"), "", width_spin)
        height_spin = self._spin(
            shell.defaultWidgetHeight,
            50,
            2000,
            10,
            0,
            lambda v: self._change_appearance("defaultWidgetHeight", v),
        )
        self._row(group, t("Settings.DefaultHeight.Title"), "", height_spin)

        group = self._group(page, "Settings.Density.Title")
        density_drop = self._dropdown(
            [t(f"Settings.Density.{name}") for name in ("Compact", "Standard", "Relaxed", "Custom")],
            t(f"Settings.Density.{shell.layoutDensity}"),
            self._change_density,
        )
        self._row(group, t("Settings.Density.Title"), t("Settings.Density.Description"), density_drop)

        icon_spin = self._spin(shell.iconSize, 16, 56, 1, 0, lambda v: self._change_density_field("iconSize", v))
        self._row(group, t("Settings.IconSize.Title"), "", icon_spin)
        text_spin = self._spin(shell.textSize, 8, 20, 0.5, 1, lambda v: self._change_density_field("textSize", v))
        self._row(group, t("Settings.TextSize.Title"), "", text_spin)

        # Spins show PIXELS (icon size × scale); the store keeps the scale so
        # gaps grow proportionally when the icon size changes.
        def spacing_spin(scale: float, field: str):
            return self._spin(
                round(shell.iconSize * scale),
                0,
                48,
                1,
                0,
                lambda v: self._change_density_field(field, v / max(1.0, shell.iconSize)),
            )

        self._row(
            group,
            t("Settings.IconSpacing.Horizontal"),
            t("Settings.IconSpacing.Description"),
            spacing_spin(shell.horizontalSpacingScale, "horizontalSpacingScale"),
        )
        self._row(
            group,
            t("Settings.IconSpacing.Vertical"),
            "",
            spacing_spin(shell.verticalSpacingScale, "verticalSpacingScale"),
        )

        group = self._group(page, "Settings.Interaction.Window.Title")
        snap_switch = self._switch(shell.resizeSnapEnabled, lambda v: self._change_appearance("resizeSnapEnabled", v))
        self._row(group, t("Settings.ResizeSnap.Title"), t("Settings.ResizeSnap.Description"), snap_switch)
        snap_spin = self._spin(
            shell.widgetSnapSpacing,
            1,
            48,
            1,
            0,
            lambda v: self._change_appearance("widgetSnapSpacing", v),
        )
        self._row(
            group,
            t("Settings.WidgetSnap.Spacing.Title"),
            t("Settings.WidgetSnap.Spacing.Description"),
            snap_spin,
        )
        return page

    def _change_theme(self, display_name: str) -> None:
        for name in THEMES:
            if t(f"Settings.Theme.{name}") == display_name:
                if self.settings_service.settings.core.theme != name:
                    self.settings_service.settings.core.theme = name
                    self.hooks.apply_theme(name)
                    self.hooks.save()
                return

    def _change_appearance(self, field: str, value) -> None:
        setattr(self.settings_service.settings.widgetShell, field, value)
        self.hooks.apply_appearance()
        self.hooks.save()

    def _change_density(self, display_name: str) -> None:
        from ..constants import DENSITY_PRESETS

        shell = self.settings_service.settings.widgetShell
        for name in ("Compact", "Standard", "Relaxed", "Custom"):
            if t(f"Settings.Density.{name}") == display_name:
                if name != "Custom" and name in DENSITY_PRESETS:
                    preset = DENSITY_PRESETS[name]
                    shell.iconSize = float(preset["icon_size"])
                    shell.textSize = float(preset["text_size"])
                    shell.layoutDensityScale = float(preset["density"])
                    shell.horizontalSpacingScale = float(preset["h_spacing"])
                    shell.verticalSpacingScale = float(preset["v_spacing"])
                shell.layoutDensity = name
                self.hooks.apply_appearance()
                self.hooks.save()
                self.rebuild()
                return

    def _change_density_field(self, field: str, value) -> None:
        shell = self.settings_service.settings.widgetShell
        setattr(shell, field, float(value))
        shell.layoutDensity = "Custom"
        self.hooks.apply_appearance()
        self.hooks.save()

    # ---- File widgets ------------------------------------------------------------------------

    def _page_file_widget(self) -> Gtk.Widget:
        fw = self.settings_service.settings.fileWidget
        page = self._page("Settings.Nav.FileWidget")

        group = self._group(page, "Settings.Group.FileStorage.Title")
        path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        entry = Gtk.Entry(visible=True)
        entry.set_text(fw.defaultManagedStorageRootPath)
        entry.set_hexpand(True)
        entry.set_width_chars(24)
        entry.connect(
            "changed",
            lambda w: self._change_file_setting("defaultManagedStorageRootPath", w.get_text()),
        )
        browse = Gtk.Button(label="…", visible=True)
        browse.set_tooltip_text(t("Settings.ManagedPath.ChangeTooltip"))
        browse.connect("clicked", lambda *_a: self._choose_storage_root())
        path_box.append(entry)
        path_box.append(browse)
        self._row(group, t("Settings.ManagedPath.Title"), t("Settings.ManagedPath.Description"), path_box)

        group = self._group(page, "Settings.Group.FileLayout.Title")
        self._row(
            group,
            t("Settings.ShowFileExtensions.Title"),
            t("Settings.ShowFileExtensions.Description"),
            self._switch(fw.showFileExtensions, lambda v: self._change_file_setting("showFileExtensions", v)),
        )
        lines_drop = self._dropdown(
            [
                t("Settings.FileNameLines.Hidden"),
                t("Settings.FileNameLines.Single"),
                t("Settings.FileNameLines.Double"),
            ],
            t(f"Settings.FileNameLines.{NAME_LINE_KEYS[fw.fileNameLineCount]}"),
            self._change_name_lines,
        )
        self._row(
            group,
            t("Settings.FileNameLines.Title"),
            t("Settings.FileNameLines.Description"),
            lines_drop,
        )

        drop_options = [
            t("Settings.DropAction.Move"),
            t("Settings.DropAction.Copy"),
            t("Settings.DropAction.System"),
        ]
        drop_values = ["Move", "Copy", "FollowSystem"]
        drop_drop = self._dropdown(
            drop_options,
            t(f"Settings.DropAction.{fw.managedDropAction if fw.managedDropAction != 'FollowSystem' else 'System'}"),
            lambda name: self._change_file_setting("managedDropAction", drop_values[drop_options.index(name)]),
        )
        self._row(group, t("Settings.DropAction.Title"), t("Settings.DropAction.Description"), drop_drop)

        group = self._group(page, "Settings.FileStacks.Title")
        self._row(
            group,
            t("Settings.FileStacks.Auto.Title"),
            t("Settings.FileStacks.Auto.Description"),
            self._switch(fw.fileStacksEnabled and fw.fileStackAutoStacking, self._change_auto_stacking),
        )
        group_by = ["Kind", "DateAdded", "DateModified", "Custom"]
        group_drop = self._dropdown(
            [t(f"Settings.FileStacks.GroupBy.{name}") for name in group_by],
            t(f"Settings.FileStacks.GroupBy.{fw.fileStackGroupBy}"),
            lambda name: self._change_file_setting(
                "fileStackGroupBy",
                group_by[[t(f"Settings.FileStacks.GroupBy.{n}") for n in group_by].index(name)],
            ),
        )
        self._row(
            group,
            t("Settings.FileStacks.GroupBy.Title"),
            t("Settings.FileStacks.GroupBy.Description"),
            group_drop,
        )
        # The threshold domain is {2, 3, 5} (normalize maps anything else to 3).
        threshold = fw.fileStackThreshold if fw.fileStackThreshold in (2, 3, 5) else 3
        self._row(
            group,
            t("Settings.FileStacks.Threshold.Title"),
            t("Settings.FileStacks.Threshold.Description"),
            self._value_dropdown(
                [(v, str(v)) for v in (2, 3, 5)],
                threshold,
                lambda v: self._change_file_setting("fileStackThreshold", int(v)),
            ),
        )
        self._row(
            group,
            t("Settings.FileStacks.OpenMode.Title"),
            t("Settings.FileStacks.OpenMode.Description"),
            self._value_dropdown(
                [
                    ("Inline", t("Settings.FileStacks.OpenMode.Inline")),
                    ("Popover", t("Settings.FileStacks.OpenMode.Popover")),
                ],
                fw.fileStackOpenMode,
                lambda v: self._change_file_setting("fileStackOpenMode", v),
            ),
        )
        self._row(
            group,
            t("Settings.FileStacks.PopoverLayout.Title"),
            t("Settings.FileStacks.PopoverLayout.Description"),
            self._value_dropdown(
                [
                    ("Grid3", t("Settings.FileStacks.PopoverLayout.Grid3")),
                    ("Grid5", t("Settings.FileStacks.PopoverLayout.Grid5")),
                    ("Adaptive", t("Settings.FileStacks.PopoverLayout.Adaptive")),
                ],
                fw.fileStackPopoverLayout,
                lambda v: self._change_file_setting("fileStackPopoverLayout", v),
            ),
        )
        self._group_feature_file_stack_rules(page)
        self._group_organize(page)
        return page

    # ---- desktop organization ------------------------------------------------------

    def _group_organize(self, page: Gtk.Widget) -> None:
        rules = self.settings_service.settings.desktopOrganization.desktopOrganizationRules
        group = self._group(page, "DesktopOrganization.Rules.SectionTitle")

        status = Gtk.Label(xalign=0.0, wrap=True, visible=True)
        status.add_css_class("settings-row-description")
        if rules:
            status.set_text(t("DesktopOrganization.Status.Ready"))
        else:
            status.set_text(t("DesktopOrganization.Status.NoRules"))

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=True)
        preview = Gtk.Button(label=t("DesktopOrganization.Settings.StartAction"), visible=True)
        preview.add_css_class("suggested-action")
        preview.connect(
            "clicked",
            lambda *_a: self.hooks.open_organize and self.hooks.open_organize(),
        )
        undo = Gtk.Button(label=t("DesktopOrganization.Layout.Undo"), visible=True)
        undo.set_sensitive(self.hooks.undo_latest_organization is not None)
        undo.connect("clicked", lambda *_a: self._undo_latest_organization(status))
        buttons.append(preview)
        buttons.append(undo)

        row = self._row(
            group,
            t("DesktopOrganization.Settings.Title"),
            t("DesktopOrganization.Settings.EntryDescription"),
            buttons,
        )
        row.append(status)

    def _undo_latest_organization(self, status: Gtk.Label) -> None:
        hook = self.hooks.undo_latest_organization
        if hook is None:
            return
        try:
            status.set_text(hook())
        except Exception as exc:
            status.set_text(f"{t('DesktopOrganization.Undo.Failed')}: {exc}")

    # ---- file stack custom rules ---------------------------------------------------

    def _group_feature_file_stack_rules(self, page: Gtk.Widget) -> None:
        """Compact custom-rules editor (SettingsViewModel.FileStackOptions)."""
        from ..services.stack_grouping import MAX_CUSTOM_RULES

        fw = self.settings_service.settings.fileWidget
        group = self._group(page, "Settings.FileStacks.Rules.Title")

        add = Gtk.Button(label=t("Settings.FileStacks.Rules.Add"), visible=True)
        add.set_sensitive(len(fw.fileStackCustomRules) < MAX_CUSTOM_RULES)
        add.connect("clicked", lambda *_a: self._add_file_stack_rule())
        self._row(
            group,
            t("Settings.FileStacks.Rules.Title"),
            t("Settings.FileStacks.Rules.Description"),
            add,
        )

        for rule in fw.fileStackCustomRules:
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
            name = Gtk.Entry(visible=True)
            name.set_text(rule.name)
            name.set_width_chars(12)
            name.set_placeholder_text(t("Settings.FileStacks.Rules.NamePlaceholder"))
            name.connect(
                "changed",
                lambda w, r=rule: self._write_file_stack_rule(r, name=w.get_text()),
            )
            extensions = Gtk.Entry(visible=True)
            extensions.set_text(", ".join(rule.extensions))
            extensions.set_hexpand(True)
            extensions.set_placeholder_text(t("Settings.FileStacks.Rules.ExtensionsPlaceholder"))
            extensions.connect(
                "changed",
                lambda w, r=rule: self._write_file_stack_rule(r, extensions=w.get_text()),
            )
            remove = Gtk.Button(label="✕", visible=True)
            remove.set_tooltip_text(t("Settings.FileStacks.Rules.Remove"))
            remove.connect("clicked", lambda *_a, r=rule: self._remove_file_stack_rule(r))
            row_box.append(name)
            row_box.append(extensions)
            row_box.append(remove)
            self._row(group, "", "", row_box)

    def _write_file_stack_rule(self, rule, *, name: str | None = None, extensions: str | None = None) -> None:
        from ..services.stack_grouping import normalize_extensions

        if name is not None:
            rule.name = name.strip()[:80]
        if extensions is not None:
            rule.extensions = normalize_extensions([piece.strip() for piece in extensions.replace(",", " ").split()])
        self._change_file_setting(
            "fileStackCustomRules", self.settings_service.settings.fileWidget.fileStackCustomRules
        )

    def _add_file_stack_rule(self) -> None:
        import uuid

        from ..models.settings_slices import FileStackCustomRule
        from ..services.stack_grouping import MAX_CUSTOM_RULES

        fw = self.settings_service.settings.fileWidget
        if len(fw.fileStackCustomRules) >= MAX_CUSTOM_RULES:
            return
        fw.fileStackCustomRules.append(FileStackCustomRule(id=uuid.uuid4().hex))
        self._change_file_setting("fileStackCustomRules", fw.fileStackCustomRules)
        self.rebuild()
        self.show_section("FileWidget")

    def _remove_file_stack_rule(self, rule) -> None:
        fw = self.settings_service.settings.fileWidget
        if rule in fw.fileStackCustomRules:
            fw.fileStackCustomRules.remove(rule)
            self._change_file_setting("fileStackCustomRules", fw.fileStackCustomRules)
        self.rebuild()
        self.show_section("FileWidget")

    def _choose_storage_root(self) -> None:
        dialog = Gtk.FileChooserNative.new(
            t("Settings.ManagedPath.ChangeTooltip"),
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            None,
            None,
        )
        dialog.set_modal(True)

        def respond(_dlg, response: int):
            if response == Gtk.ResponseType.ACCEPT:
                folder = dialog.get_file()
                path = folder.get_path() if folder is not None else None
                if path:
                    self._change_file_setting("defaultManagedStorageRootPath", path)
                    self.rebuild()
            dialog.destroy()

        dialog.connect("response", respond)
        dialog.show()

    def _change_file_setting(self, field: str, value) -> None:
        setattr(self.settings_service.settings.fileWidget, field, value)
        if field in (
            "showFileExtensions",
            "fileNameLineCount",
            # Stack fields rebuild the live file surfaces' projections.
            "fileStacksEnabled",
            "fileStackAutoStacking",
            "fileStackGroupBy",
            "fileStackThreshold",
            "fileStackOrderBy",
            "fileStackOpenMode",
            "fileStackPopoverLayout",
            "fileStackUnmatchedBehavior",
            "fileStackCustomRules",
        ):
            self.hooks.apply_appearance()
        self.hooks.save()

    def _change_name_lines(self, display_name: str) -> None:
        for count, key in NAME_LINE_KEYS.items():
            if t(f"Settings.FileNameLines.{key}") == display_name:
                self._change_file_setting("fileNameLineCount", count)
                return

    def _change_auto_stacking(self, enabled: bool) -> None:
        fw = self.settings_service.settings.fileWidget
        fw.fileStacksEnabled = enabled
        fw.fileStackAutoStacking = enabled
        self.hooks.apply_appearance()
        self.hooks.save()

    # ---- Interaction (hotkey) ---------------------------------------------------------------------

    def _page_interaction(self) -> Gtk.Widget:
        core = self.settings_service.settings.core
        page = self._page("Settings.Nav.Interaction")

        group = self._group(page)
        self._row(
            group,
            t("Settings.GlobalHotkey.Title"),
            t("Settings.GlobalHotkey.Description"),
            self._switch(core.globalHotkeyEnabled, self._change_hotkey_enabled),
        )

        preset_labels = [hotkey_preset_label(p[0]) for p in HOTKEY_PRESETS]
        preset_drop = self._dropdown(preset_labels, self._current_preset_label(), self._change_hotkey_preset)
        self._row(
            group,
            t("Settings.GlobalHotkey.PresetsTitle"),
            t("Settings.Interaction.Hotkeys.Description"),
            preset_drop,
        )

        capture = Gtk.ToggleButton(label=t("Settings.GlobalHotkey.Recording"), visible=True)
        capture.set_tooltip_text(t("Settings.GlobalHotkey.ChangeTooltip"))
        capture.connect("toggled", self._on_capture_toggled)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_capture_key)
        capture.add_controller(key_controller)
        self._hotkey_capture_button = capture
        self._row(group, t("Settings.GlobalHotkey.CustomTitle"), "", capture)

        status = Gtk.Label(xalign=0.0, wrap=True, visible=True)
        status.add_css_class("settings-hotkey-status")
        self._hotkey_status_label = status
        self._row(group, "", "", status)

        banner = Gtk.Label(xalign=0.0, wrap=True, visible=True)
        banner.add_css_class("settings-limitation-banner")
        self._hotkey_banner = banner
        self._row(group, "", "", banner)

        self._refresh_hotkey_status()
        return page

    def _current_preset_label(self) -> str:
        core = self.settings_service.settings.core
        for name, kind, modifiers, keysym in HOTKEY_PRESETS:
            if kind in ("DoubleControl", "WindowsTap"):
                if core.globalHotkeyActivationKind == kind:
                    return hotkey_preset_label(name)
            elif (
                core.globalHotkeyActivationKind == "Chord"
                and core.globalHotkeyModifiers == modifiers
                and core.globalHotkeyKey == keysym
            ):
                return hotkey_preset_label(name)
        return gesture_label(core.globalHotkeyModifiers, core.globalHotkeyKey)

    def _change_hotkey_enabled(self, enabled: bool) -> None:
        self.settings_service.settings.core.globalHotkeyEnabled = enabled
        self._refresh_hotkey_status()

    def _change_hotkey_preset(self, label: str) -> None:
        for name, kind, modifiers, keysym in HOTKEY_PRESETS:
            if hotkey_preset_label(name) != label:
                continue
            core = self.settings_service.settings.core
            if kind in ("DoubleControl", "WindowsTap"):
                core.globalHotkeyActivationKind = kind
            else:
                core.globalHotkeyActivationKind = "Chord"
                core.globalHotkeyModifiers = modifiers
                core.globalHotkeyKey = keysym
            self._refresh_hotkey_status()
            return

    def _on_capture_toggled(self, button: Gtk.ToggleButton) -> None:
        self._recording = button.get_active()

    def _on_capture_key(self, _controller, keyval: int, _keycode: int, state) -> bool:
        if not self._recording:
            return False
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
            self._stop_capture()
            return True
        if Gdk.keyval_name(keyval) in (
            "Control_L",
            "Control_R",
            "Alt_L",
            "Alt_R",
            "Shift_L",
            "Shift_R",
            "Super_L",
            "Super_R",
            "Meta_L",
            "Meta_R",
        ):
            return True  # modifier alone: keep recording
        modifiers = 0
        if state & Gdk.ModifierType.CONTROL_MASK:
            modifiers |= MODIFIER_CONTROL
        if state & Gdk.ModifierType.MOD1_MASK:
            modifiers |= MODIFIER_ALT
        if state & Gdk.ModifierType.SHIFT_MASK:
            modifiers |= MODIFIER_SHIFT
        if state & Gdk.ModifierType.MOD4_MASK or state & Gdk.ModifierType.SUPER_MASK:
            modifiers |= MODIFIER_WINDOWS
        core = self.settings_service.settings.core
        core.globalHotkeyActivationKind = "Chord"
        core.globalHotkeyModifiers = modifiers
        core.globalHotkeyKey = keyval
        self._stop_capture()
        self._refresh_hotkey_status()
        return True

    def _stop_capture(self) -> None:
        self._recording = False
        if self._hotkey_capture_button is not None:
            self._hotkey_capture_button.set_active(False)

    def _refresh_hotkey_status(self) -> None:
        core = self.settings_service.settings.core
        status = self.hooks.apply_hotkey()
        self.hooks.save()
        if self._hotkey_status_label is not None:
            if not core.globalHotkeyEnabled:
                text = t("Settings.GlobalHotkey.Status.Disabled")
            elif getattr(status, "state", "") == "registered":
                if core.globalHotkeyActivationKind == "Chord":
                    gesture = gesture_label(core.globalHotkeyModifiers, core.globalHotkeyKey)
                else:
                    gesture = hotkey_preset_label(
                        next(
                            (n for n, k, *_r in HOTKEY_PRESETS if k == core.globalHotkeyActivationKind),
                            "F7",
                        )
                    )
                text = fmt("Settings.GlobalHotkey.Status.Active", gesture)
            else:
                text = t("Settings.GlobalHotkey.Status.Unregistered")
            detail = getattr(status, "detail", "")
            if detail:
                text = f"{text}\n{detail}"
            self._hotkey_status_label.set_text(text)
        if self._hotkey_banner is not None:
            if core.globalHotkeyActivationKind == "WindowsTap":
                self._hotkey_banner.set_text(t("Settings.GlobalHotkey.WindowsTapWarning"))
            elif core.globalHotkeyActivationKind == "AltSpaceChord" or (
                core.globalHotkeyActivationKind == "Chord"
                and core.globalHotkeyModifiers == MODIFIER_ALT
                and core.globalHotkeyKey == Gdk.KEY_space
            ):
                self._hotkey_banner.set_text(t("Settings.GlobalHotkey.AltSpaceWarning"))
            else:
                self._hotkey_banner.set_text("")

    # ---- Advanced (performance) ---------------------------------------------------------------------

    def _page_advanced(self) -> Gtk.Widget:
        perf = self.settings_service.settings.performance
        page = self._page("Settings.Nav.Advanced")

        group = self._group(page)
        mode_names = ["ResourceSaver", "Balanced", "BestVisual", "Custom"]
        mode_drop = self._dropdown(
            [t(f"Settings.Performance.Mode.{name}") for name in mode_names],
            t(f"Settings.Performance.Mode.{perf.performanceMode}"),
            lambda name: self._change_performance_mode(
                mode_names[[t(f"Settings.Performance.Mode.{n}") for n in mode_names].index(name)]
            ),
        )
        self._row(
            group,
            t("Settings.Performance.Mode.Title"),
            t("Settings.Performance.Mode.Description"),
            mode_drop,
        )

        budget_names = ["Small", "Balanced", "Large"]
        budget_drop = self._dropdown(
            [t(f"Settings.Performance.CacheBudget.{name}") for name in budget_names],
            t(f"Settings.Performance.CacheBudget.{perf.performanceCacheBudget}"),
            lambda name: self._change_performance_field(
                "performanceCacheBudget",
                budget_names[[t(f"Settings.Performance.CacheBudget.{n}") for n in budget_names].index(name)],
            ),
        )
        self._row(
            group,
            t("Settings.Performance.CacheBudget.Title"),
            t("Settings.Performance.CacheBudget.Description"),
            budget_drop,
        )

        hidden_spin = self._spin(
            perf.hiddenCacheCleanupDelaySeconds,
            5,
            3600,
            5,
            0,
            lambda v: self._change_performance_field("hiddenCacheCleanupDelaySeconds", int(v)),
        )
        self._row(
            group,
            t("Settings.Performance.HiddenCleanup.Title"),
            t("Settings.Performance.HiddenCleanup.Description"),
            hidden_spin,
        )

        idle_spin = self._spin(
            perf.visibleIdleCacheCleanupDelaySeconds,
            30,
            7200,
            30,
            0,
            lambda v: self._change_performance_field("visibleIdleCacheCleanupDelaySeconds", int(v)),
        )
        self._row(
            group,
            t("Settings.Performance.VisibleIdleCleanup.Title"),
            t("Settings.Performance.VisibleIdleCleanup.Description"),
            idle_spin,
        )

        diagnostics_group = self._group(page, "Settings.Diagnostics.Export")
        export = Gtk.Button(label=t("Settings.Diagnostics.Export"), visible=True)
        export.set_sensitive(self.hooks.export_diagnostics is not None)
        export.connect("clicked", lambda *_a: self._export_diagnostics(export))
        self._row(
            diagnostics_group,
            t("Settings.Diagnostics.Export"),
            t("Settings.Diagnostics.ExportTooltip"),
            export,
        )

        core = self.settings_service.settings.core
        update_group = self._group(page, "Settings.Update.Title")
        self._row(
            update_group,
            t("Settings.Update.AutoCheck"),
            t("Settings.Update.AutoCheckDescription"),
            self._switch(core.autoCheckForUpdates, self._change_auto_update_check),
        )
        self._update_status = Gtk.Label(xalign=0.0, wrap=True, visible=True)
        self._update_status.add_css_class("settings-row-description")
        check = Gtk.Button(label=t("Settings.Update.Check"), visible=True)
        check.set_sensitive(self.hooks.check_updates is not None)
        check.connect("clicked", lambda *_a, b=check: self._run_update_check(b))
        manual = Gtk.Button(label=t("Settings.Update.ManualDownload"), visible=True)
        manual.connect("clicked", lambda *_a: self._open_manual_download())
        update_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=True)
        update_buttons.append(check)
        update_buttons.append(manual)
        update_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        update_column.append(update_buttons)
        update_column.append(self._update_status)
        self._row(
            update_group,
            t("Settings.Update.Title"),
            t("Settings.Update.Description"),
            update_column,
        )
        self._update_status.set_text(self._update_status_text())
        return page

    def _change_performance_mode(self, name: str) -> None:
        from ..constants import PERFORMANCE_PRESETS

        perf = self.settings_service.settings.performance
        if name in PERFORMANCE_PRESETS:
            preset = PERFORMANCE_PRESETS[name]
            perf.hiddenCacheCleanupDelaySeconds = int(preset["hidden_cache_cleanup_delay_seconds"])
            perf.visibleIdleCacheCleanupDelaySeconds = int(preset["visible_idle_cache_cleanup_delay_seconds"])
            perf.transientWindowReleaseDelaySeconds = int(preset["transient_window_release_delay_seconds"])
            perf.performanceCacheBudget = str(preset["performance_cache_budget"])
            perf.hiddenCacheCleanupScope = str(preset["hidden_cache_cleanup_scope"])
        perf.performanceMode = name
        self.hooks.save()
        self.rebuild()

    def _change_performance_field(self, field: str, value) -> None:
        perf = self.settings_service.settings.performance
        setattr(perf, field, value)
        perf.performanceMode = "Custom"
        self.hooks.save()

    def _export_diagnostics(self, button: Gtk.Button) -> None:
        """Port of ExportDiagnosticsButton_Click: pick a folder, export the
        privacy-filtered bundle, report the archive path."""
        hook = self.hooks.export_diagnostics
        if hook is None:
            return
        dialog = Gtk.FileChooserNative.new(
            t("Settings.Diagnostics.Export"), self, Gtk.FileChooserAction.SELECT_FOLDER, None, None
        )
        dialog.set_modal(True)

        def respond(_dlg, response):
            folder = dialog.get_file()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT or folder is None:
                return
            button.set_sensitive(False)
            try:
                archive = hook(folder.get_path())
                self._show_diagnostics_result(True, str(archive))
            except Exception as exc:
                self._show_diagnostics_result(False, str(exc))
            finally:
                button.set_sensitive(True)

        dialog.connect("response", respond)
        dialog.show()

    def _show_diagnostics_result(self, ok: bool, detail: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO if ok else Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text=t("Settings.Diagnostics.SuccessTitle" if ok else "Settings.Diagnostics.FailedTitle"),
        )
        dialog.format_secondary_text(
            fmt(
                "Settings.Diagnostics.SuccessBody" if ok else "Settings.Diagnostics.FailedBody",
                detail,
            )
        )
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.present()

    # ---- App updates ---------------------------------------------------------------

    def _change_auto_update_check(self, enabled: bool) -> None:
        self.settings_service.settings.core.autoCheckForUpdates = enabled
        self.hooks.save()

    def _update_status_text(self) -> str:
        from ..constants import APP_VERSION

        core = self.settings_service.settings.core
        result = self._update_check_result
        if result is not None and result.is_update_available:
            state = fmt("Settings.Update.Status.Available", result.remote_version)
        elif result is not None and result.status == "UpToDate":
            state = t("Settings.Update.Status.UpToDate")
        elif result is not None and result.status == "Failed":
            state = t("Settings.Update.Status.Failed")
        elif result is not None:
            state = t("Settings.Update.Detail.InvalidManifest")
        else:
            state = t("Settings.Update.Status.Ready")
        checked = _localize_stamp(core.lastUpdateCheckAt) if core.lastUpdateCheckAt else ""
        if checked:
            return fmt("Settings.Update.Detail.CurrentVersion", APP_VERSION, checked, state)
        return f"v{APP_VERSION} · {state}"

    def _run_update_check(self, button: Gtk.Button) -> None:
        hook = self.hooks.check_updates
        if hook is None:
            return
        self._update_status.set_text(t("Settings.Update.Status.Checking"))
        button.set_sensitive(False)

        def worker():
            result = hook()
            GLib.idle_add(lambda: self._finish_update_check(button, result))

        threading.Thread(target=worker, daemon=True, name="update-check").start()

    def _finish_update_check(self, button: Gtk.Button, result) -> None:
        button.set_sensitive(True)
        self._update_check_result = result
        self.settings_service.settings.core.lastUpdateCheckAt = result.checked_at_utc
        self.hooks.save()
        self._update_status.set_text(self._update_status_text())

    def _open_manual_download(self) -> None:
        from ..services.update_check import MANUAL_DOWNLOAD_URL

        try:
            Gtk.UriLauncher.new(MANUAL_DOWNLOAD_URL).launch(self, None, lambda *_a: None)
        except Exception as exc:
            self.hooks.log(f"[Update] could not open the download page: {exc}")

    # ---- Feature widgets -------------------------------------------------------

    def _write(self, kind: str, setter) -> None:
        """Apply a feature setting: mutate, persist, refresh live surfaces."""
        setter()
        self.hooks.save()
        if self.hooks.refresh_feature is not None:
            self.hooks.refresh_feature(kind)

    def _value_dropdown(self, pairs, selected, on_change) -> Gtk.DropDown:
        """Dropdown over (value, label) pairs — reverse-maps the label."""
        labels = [label for _value, label in pairs]
        current = next((label for value, label in pairs if value == selected), labels[0])
        return self._dropdown(labels, current, lambda label: on_change(next(v for v, text in pairs if text == label)))

    def _page_feature_widgets(self) -> Gtk.Widget:
        layout = self.settings_service.layout
        page = self._page("Settings.Nav.FeatureWidgets")

        states = layout.featureWidgetEnabledStates or {}
        group = self._group(page, "Settings.Section.FeatureWidgets")
        for kind, title_key, desc_key in FEATURE_ROWS:
            self._row(
                group,
                t(title_key),
                t(desc_key) if desc_key else "",
                self._switch(
                    bool(states.get(kind, False)),
                    lambda on, k=kind: self._change_feature_enabled(k, on),
                ),
            )

        self._group_feature_todo(page)
        self._group_feature_quick_capture(page)
        self._group_feature_weather(page)
        self._group_feature_music(page)
        self._group_feature_search(page)
        return page

    def _change_feature_enabled(self, kind: str, enabled: bool) -> None:
        if self.hooks.set_feature_enabled is not None:
            self.hooks.set_feature_enabled(kind, enabled)  # manager persists
        else:
            from ..services import feature_widgets

            feature_widgets.set_enabled(self.settings_service.layout, kind, enabled)
            self.hooks.save()

    # ---- Widget groups ------------------------------------------------------------

    def _page_widget_groups(self) -> Gtk.Widget:
        layout = self.settings_service.layout
        page = self._page("Settings.Nav.WidgetGroups")

        group = self._group(page, "Settings.Section.WidgetGroups")
        self._row(
            group,
            t("Widget.Group.NavigationStyle"),
            t("Settings.WidgetGroupNavigation.Description"),
            self._value_dropdown(
                [
                    ("Tabs", t("Widget.Group.Navigation.Tabs")),
                    ("Stack", t("Widget.Group.Navigation.Stack")),
                ],
                layout.widgetGroupDefaultNavigationStyle,
                lambda value: self._set_group_default("widgetGroupDefaultNavigationStyle", value),
            ),
        )
        self._row(
            group,
            t("Widget.Group.TitleDisplayMode"),
            "",
            self._value_dropdown(
                [
                    ("IconAndText", t("Widget.Group.TitleDisplay.IconAndText")),
                    ("IconOnly", t("Widget.Group.TitleDisplay.IconOnly")),
                    ("TextOnly", t("Widget.Group.TitleDisplay.TextOnly")),
                ],
                layout.widgetGroupDefaultTitleDisplayMode,
                lambda value: self._set_group_default("widgetGroupDefaultTitleDisplayMode", value),
            ),
        )
        self._row(
            group,
            t("Widget.Group.WheelSwitch"),
            "",
            self._switch(
                layout.widgetGroupWheelSwitchEnabled,
                lambda on: self._set_group_default("widgetGroupWheelSwitchEnabled", on),
            ),
        )
        self._row(
            group,
            t("Widget.Group.HoverSwitch"),
            t("Settings.WidgetGroupHover.Description"),
            self._switch(
                layout.widgetGroupHoverSwitchEnabled,
                lambda on: self._set_group_default("widgetGroupHoverSwitchEnabled", on),
            ),
        )

        maintenance = self._group(page, "Settings.Group.Maintenance.Title")
        dissolve = Gtk.Button(label=t("Widget.Group.Dissolve"), visible=True)
        dissolve.add_css_class("destructive-action")
        dissolve.connect("clicked", lambda *_a: self._dissolve_all_groups())
        self._row(
            maintenance,
            t("Widget.Group.Dissolve"),
            t("Settings.WidgetGroups.DissolveDialog.Description"),
            dissolve,
        )
        return page

    def _set_group_default(self, field: str, value) -> None:
        setattr(self.settings_service.layout, field, value)
        self.hooks.save()
        if self.hooks.refresh_groups is not None:
            self.hooks.refresh_groups()

    def _dissolve_all_groups(self) -> None:
        if self.hooks.dissolve_all_groups is not None:
            self.hooks.dissolve_all_groups()
        else:
            layout = self.settings_service.layout
            for group in list(layout.widgetGroups):
                layout.widgetGroups.remove(group)
            self.hooks.save()

    # ---- Todo -------------------------------------------------------------------

    def _group_feature_todo(self, page: Gtk.Widget) -> None:
        todo = self.settings_service.settings.todo
        group = self._group(page, "Settings.Todo.Title")

        self._row(
            group,
            t("Settings.Todo.DefaultFilter.Title"),
            t("Settings.Todo.DefaultFilter.Description"),
            self._value_dropdown(
                [(f, t(f"Settings.Todo.DefaultFilter.{f}")) for f in TODO_FILTERS],
                todo.todoDefaultFilter,
                lambda v: self._write("Todo", lambda: setattr(todo, "todoDefaultFilter", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Todo.NewTaskPosition.Title"),
            t("Settings.Todo.NewTaskPosition.Description"),
            self._value_dropdown(
                [(p, t(f"Settings.Todo.NewTaskPosition.{p}")) for p in ("Top", "Bottom")],
                todo.todoNewTaskPosition,
                lambda v: self._write("Todo", lambda: setattr(todo, "todoNewTaskPosition", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Todo.ShowCompleted.Title"),
            t("Settings.Todo.ShowCompleted.Description"),
            self._switch(
                todo.todoShowCompletedTasks,
                lambda v: self._write("Todo", lambda: setattr(todo, "todoShowCompletedTasks", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Todo.ReminderEnabled.Title"),
            t("Settings.Todo.ReminderEnabled.Description"),
            self._switch(
                todo.todoReminderEnabled,
                lambda v: self._write("Todo", lambda: setattr(todo, "todoReminderEnabled", v)),
            ),
        )

        def offset_label(minutes: int) -> str:
            if minutes == 0:
                return t("Settings.Todo.ReminderOffset.AtDueTime")
            if minutes == 60:
                return t("Settings.Todo.ReminderOffset.OneHour")
            if minutes == 1440:
                return t("Settings.Todo.ReminderOffset.OneDay")
            return fmt("Settings.Todo.ReminderOffset.Minutes", minutes)

        self._row(
            group,
            t("Settings.Todo.ReminderOffset.Title"),
            t("Settings.Todo.ReminderOffset.Description"),
            self._value_dropdown(
                [(m, offset_label(m)) for m in REMINDER_OFFSETS],
                todo.todoDefaultReminderOffsetMinutes,
                lambda v: self._write("Todo", lambda: setattr(todo, "todoDefaultReminderOffsetMinutes", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Todo.ShowFooterStats.Title"),
            t("Settings.Todo.ShowFooterStats.Description"),
            self._switch(
                todo.todoShowFooterStats,
                lambda v: self._write("Todo", lambda: setattr(todo, "todoShowFooterStats", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Todo.ShowClearCompleted.Title"),
            t("Settings.Todo.ShowClearCompleted.Description"),
            self._switch(
                todo.todoShowClearCompletedButton,
                lambda v: self._write("Todo", lambda: setattr(todo, "todoShowClearCompletedButton", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Todo.LayoutMode.Title"),
            t("Settings.Todo.LayoutMode.Description"),
            self._value_dropdown(
                [
                    ("Auto", t("Settings.Todo.LayoutMode.Auto")),
                    ("SinglePane", t("Settings.Todo.LayoutMode.SinglePane")),
                    ("DualPane", t("Settings.Todo.LayoutMode.DualPane")),
                ],
                todo.todoLayoutMode or "Auto",
                lambda v: self._write("Todo", lambda: setattr(todo, "todoLayoutMode", v)),
            ),
        )

    # ---- Quick Capture ------------------------------------------------------------

    def _group_feature_quick_capture(self, page: Gtk.Widget) -> None:
        qc = self.settings_service.settings.quickCapture
        group = self._group(page, "Settings.QuickCapture.Title")

        self._row(
            group,
            t("Settings.QuickCapture.ClipboardTitle"),
            t("Settings.QuickCapture.ClipboardDescription"),
            self._switch(
                qc.quickCaptureClipboardEnabled,
                lambda v: self._write("QuickCapture", lambda: setattr(qc, "quickCaptureClipboardEnabled", v)),
            ),
        )
        self._row(
            group,
            t("Settings.QuickCapture.ImageClipboardTitle"),
            t("Settings.QuickCapture.ImageClipboardDescription"),
            self._switch(
                qc.quickCaptureImageClipboardEnabled,
                lambda v: self._write("QuickCapture", lambda: setattr(qc, "quickCaptureImageClipboardEnabled", v)),
            ),
        )
        self._row(
            group,
            t("Settings.QuickCapture.RecentLimitTitle"),
            "",
            self._spin(
                qc.quickCaptureRecentLimit,
                10,
                100,
                5,
                0,
                lambda v: self._write("QuickCapture", lambda: setattr(qc, "quickCaptureRecentLimit", int(v))),
            ),
        )
        self._row(
            group,
            t("Settings.QuickCapture.ShowCreatedTime.Title"),
            t("Settings.QuickCapture.ShowCreatedTime.Description"),
            self._switch(
                qc.quickCaptureShowCreatedTime,
                lambda v: self._write("QuickCapture", lambda: setattr(qc, "quickCaptureShowCreatedTime", v)),
            ),
        )
        self._row(
            group,
            t("Settings.QuickCapture.Format.Title"),
            t("Settings.QuickCapture.Format.Description"),
            self._value_dropdown(
                [
                    ("Markdown", t("Settings.QuickCapture.Format.Markdown")),
                    ("PlainText", t("Settings.QuickCapture.Format.PlainText")),
                ],
                qc.quickCaptureDefaultFormat,
                lambda v: self._write("QuickCapture", lambda: setattr(qc, "quickCaptureDefaultFormat", v)),
            ),
        )
        self._row(
            group,
            t("Settings.QuickCapture.DefaultView.Title"),
            t("Settings.QuickCapture.DefaultView.Description"),
            self._value_dropdown(
                [
                    ("Records", t("Settings.QuickCapture.DefaultView.Records")),
                    ("Pinned", t("Settings.QuickCapture.DefaultView.Pinned")),
                    ("Recent", t("Settings.QuickCapture.DefaultView.Recent")),
                ],
                qc.quickCaptureDefaultView,
                lambda v: self._write("QuickCapture", lambda: setattr(qc, "quickCaptureDefaultView", v)),
            ),
        )

    # ---- Weather -------------------------------------------------------------------

    def _group_feature_weather(self, page: Gtk.Widget) -> None:
        weather = self.settings_service.settings.weather
        group = self._group(page, "Settings.Weather.Title")

        def write(setter) -> None:
            self._write("Weather", setter)

        self._row(
            group,
            t("Settings.Weather.LocationMode.Title"),
            t("Settings.Weather.LocationMode.Description"),
            self._value_dropdown(
                [
                    (True, t("Settings.Weather.LocationMode.Auto")),
                    (False, t("Settings.Weather.LocationMode.Manual")),
                ],
                weather.weatherAutoLocation,
                lambda v: write(lambda: setattr(weather, "weatherAutoLocation", v)),
            ),
        )

        city = Gtk.Entry(visible=True)
        city.set_hexpand(True)
        city.set_text(weather.weatherCityName)
        city.set_placeholder_text(t("Settings.Weather.CityName.Description"))
        city.connect(
            "changed",
            lambda w: write(lambda: setattr(weather, "weatherCityName", w.get_text().strip())),
        )
        self._row(group, t("Settings.Weather.CityName.Title"), "", city)

        self._row(
            group,
            t("Settings.Weather.TemperatureUnit.Title"),
            t("Settings.Weather.TemperatureUnit.Description"),
            self._value_dropdown(
                [("Celsius", "Celsius (°C)"), ("Fahrenheit", "Fahrenheit (°F)")],
                weather.weatherTemperatureUnit,
                lambda v: write(lambda: setattr(weather, "weatherTemperatureUnit", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Weather.WindSpeedUnit.Title"),
            t("Settings.Weather.WindSpeedUnit.Description"),
            self._value_dropdown(
                [("kmh", "km/h"), ("ms", "m/s"), ("mph", "mph")],
                weather.weatherWindSpeedUnit,
                lambda v: write(lambda: setattr(weather, "weatherWindSpeedUnit", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Weather.RefreshInterval.Title"),
            t("Settings.Weather.RefreshInterval.Description"),
            self._value_dropdown(
                [(m, f"{m} min") for m in (15, 30, 60, 180)],
                weather.weatherRefreshIntervalMinutes,
                lambda v: write(lambda: setattr(weather, "weatherRefreshIntervalMinutes", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Weather.DefaultView.Title"),
            t("Settings.Weather.DefaultView.Description"),
            self._value_dropdown(
                [("Today", "Today"), ("Week", "Week")],
                weather.weatherDefaultView,
                lambda v: write(lambda: setattr(weather, "weatherDefaultView", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Weather.Skin.Title"),
            t("Settings.Weather.Skin.Description"),
            self._value_dropdown(
                [("Standard", "Standard"), ("Rich", "Rich")],
                weather.weatherSkin,
                lambda v: write(lambda: setattr(weather, "weatherSkin", v)),
            ),
        )
        show_rows = (
            ("weatherShowForecast", "Settings.Weather.ShowForecast"),
            ("weatherShowSunrise", "Settings.Weather.ShowSunrise"),
            ("weatherShowUvIndex", "Settings.Weather.ShowUvIndex"),
            ("weatherShowPrecipitation", "Settings.Weather.ShowPrecipitation"),
            ("weatherShowHumidity", "Settings.Weather.ShowHumidity"),
            ("weatherShowWind", "Settings.Weather.ShowWind"),
            ("weatherShowPressure", "Settings.Weather.ShowPressure"),
        )
        for field, key in show_rows:
            self._row(
                group,
                t(f"{key}.Title"),
                t(f"{key}.Description"),
                self._switch(
                    getattr(weather, field),
                    lambda v, f=field: write(lambda: setattr(weather, f, v)),
                ),
            )

    # ---- Music --------------------------------------------------------------------

    def _group_feature_music(self, page: Gtk.Widget) -> None:
        music = self.settings_service.settings.music
        group = self._group(page, "Settings.Music.Title")

        modes = ("Auto", "Cover", "Controls", "RecordVertical", "RecordHorizontal")
        self._row(
            group,
            t("Settings.Music.DisplayMode.Title"),
            t("Settings.Music.DisplayMode.Description"),
            self._value_dropdown(
                [(m, t(f"Settings.Music.DisplayMode.{m}")) for m in modes],
                music.musicDisplayMode,
                lambda v: self._write("Music", lambda: setattr(music, "musicDisplayMode", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Music.ArtworkBackdrop.Title"),
            t("Settings.Music.ArtworkBackdrop.Description"),
            self._switch(
                music.musicUseArtworkBackdrop,
                lambda v: self._write("Music", lambda: setattr(music, "musicUseArtworkBackdrop", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Music.CoverHoverMotion.Title"),
            t("Settings.Music.CoverHoverMotion.Description"),
            self._switch(
                music.musicEnableCoverHoverMotion,
                lambda v: self._write("Music", lambda: setattr(music, "musicEnableCoverHoverMotion", v)),
            ),
        )

    # ---- Search -------------------------------------------------------------------

    def _group_feature_search(self, page: Gtk.Widget) -> None:
        search = self.settings_service.settings.search
        group = self._group(page, "Settings.Search.Title")

        def write(setter) -> None:
            self._write("Search", setter)

        self._row(
            group,
            t("Settings.Search.Hotkey.Title"),
            t("Settings.Search.Hotkey.Description"),
            self._switch(
                search.searchHotkeyEnabled,
                lambda v: (
                    setattr(search, "searchHotkeyEnabled", v),
                    self._refresh_search_hotkey_status(),
                ),
            ),
        )

        capture = Gtk.ToggleButton(label=t("Settings.Search.Hotkey.Recording"), visible=True)
        capture.set_tooltip_text(t("Settings.Search.Hotkey.ChangeTooltip"))
        capture.connect("toggled", self._on_search_capture_toggled)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_search_capture_key)
        capture.add_controller(key_controller)
        self._search_capture_button = capture

        reset = Gtk.Button(label="↺", visible=True)
        reset.set_tooltip_text(t("Settings.Search.Hotkey.ResetTooltip"))
        reset.connect("clicked", lambda *_a: self._reset_search_hotkey())

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        box.append(capture)
        box.append(reset)
        self._row(group, t("Settings.Search.Hotkey.CustomTitle"), "", box)

        status = Gtk.Label(xalign=0.0, wrap=True, visible=True)
        status.add_css_class("settings-hotkey-status")
        self._search_hotkey_status_label = status
        self._row(group, "", "", status)
        self._refresh_search_hotkey_status()

        tabs = (
            ("all", "Search.Tab.All"),
            ("app", "Search.Tab.App"),
            ("file", "Search.Tab.File"),
            ("panebox", "Search.Tab.PaneBox"),
        )
        self._row(
            group,
            t("Settings.Search.DefaultTab.Title"),
            t("Settings.Search.DefaultTab.Description"),
            self._value_dropdown(
                [(v, t(k)) for v, k in tabs],
                search.searchDefaultTab,
                lambda v: write(lambda: setattr(search, "searchDefaultTab", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Search.MaxResults.Title"),
            t("Settings.Search.MaxResults.Description"),
            self._spin(
                search.searchMaxResults,
                10,
                500,
                10,
                0,
                lambda v: write(lambda: setattr(search, "searchMaxResults", int(v))),
            ),
        )
        self._row(
            group,
            t("Settings.Search.Recommendations.Title"),
            t("Settings.Search.Recommendations.Description"),
            self._switch(
                search.searchShowRecommendations,
                lambda v: write(lambda: setattr(search, "searchShowRecommendations", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Search.Privacy.Title"),
            t("Settings.Search.Privacy.Description"),
            self._switch(
                search.searchSaveHistory,
                lambda v: write(lambda: setattr(search, "searchSaveHistory", v)),
            ),
        )
        self._row(
            group,
            t("Settings.Search.Scope.PaneBox"),
            t("Settings.Search.Scope.PaneBox.Description"),
            self._switch(
                search.searchIncludePaneBoxContent,
                lambda v: write(lambda: setattr(search, "searchIncludePaneBoxContent", v)),
            ),
        )

    def _on_search_capture_toggled(self, button: Gtk.ToggleButton) -> None:
        self._search_recording = button.get_active()

    def _on_search_capture_key(self, _controller, keyval: int, _keycode: int, state) -> bool:
        if not self._search_recording:
            return False
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
            self._stop_search_capture()
            return True
        if Gdk.keyval_name(keyval) in (
            "Control_L",
            "Control_R",
            "Alt_L",
            "Alt_R",
            "Shift_L",
            "Shift_R",
            "Super_L",
            "Super_R",
            "Meta_L",
            "Meta_R",
        ):
            return True  # modifier alone: keep recording
        # The search slice stores raw X11 masks (ShiftMask/ControlMask/Mod1/Mod4).
        modifiers = 0
        if state & Gdk.ModifierType.SHIFT_MASK:
            modifiers |= 1 << 0
        if state & Gdk.ModifierType.CONTROL_MASK:
            modifiers |= 1 << 2
        if state & Gdk.ModifierType.MOD1_MASK:
            modifiers |= 1 << 3
        if state & Gdk.ModifierType.MOD4_MASK or state & Gdk.ModifierType.SUPER_MASK:
            modifiers |= 1 << 6
        search = self.settings_service.settings.search
        search.searchHotkeyModifiers = modifiers
        search.searchHotkeyKey = keyval
        self._stop_search_capture()
        self._refresh_search_hotkey_status()
        return True

    def _stop_search_capture(self) -> None:
        self._search_recording = False
        if self._search_capture_button is not None:
            self._search_capture_button.set_active(False)

    def _reset_search_hotkey(self) -> None:
        modifiers, key = SEARCH_HOTKEY_DEFAULT
        search = self.settings_service.settings.search
        search.searchHotkeyModifiers = modifiers
        search.searchHotkeyKey = key
        self._refresh_search_hotkey_status()

    def _refresh_search_hotkey_status(self) -> None:
        """Persist + re-grab both hotkeys, then render the search status."""
        search = self.settings_service.settings.search
        status = self.hooks.apply_hotkey()  # app re-grabs global + search
        self.hooks.save()
        if self._search_hotkey_status_label is None:
            return
        if not search.searchHotkeyEnabled:
            text = t("Settings.Search.Hotkey.Status.Disabled")
        elif getattr(status, "state", "") == "registered":
            text = t("Settings.Search.Hotkey.Status.Active")
        else:
            text = t("Settings.Search.Hotkey.Status.Failed")
        gesture = build_hotkey_hint(search.searchHotkeyModifiers, search.searchHotkeyKey)
        self._search_hotkey_status_label.set_text(f"{text}\n{gesture}")

    # ---- cloud backup --------------------------------------------------------------

    def _page_cloud_backup(self) -> Gtk.Widget:
        """Port of SettingsWindow.CloudBackup: provider, connection, data
        types, schedule, and the remote snapshot list with staged restore."""
        from ..services.cloud_backup import (
            PROVIDER_NONE,
            PROVIDER_WEBDAV,
            SUPPORTED_INTERVAL_HOURS,
            SUPPORTED_RETENTION_COUNTS,
            get_options,
        )

        service = self.hooks.cloud_backup
        slice_ = self.settings_service.settings.cloudBackup
        page = self._page("Settings.CloudBackup.Title", "Settings.CloudBackup.SyncNotice.Description")
        self._cloud_status = Gtk.Label(xalign=0.0, wrap=True, visible=True)
        self._cloud_status.add_css_class("settings-row-description")
        self._cloud_snapshots = Gtk.ListBox(visible=True)
        self._cloud_snapshots.set_selection_mode(Gtk.SelectionMode.NONE)
        self._cloud_snapshots.add_css_class("settings-group")

        def persist() -> None:
            # Destination edits invalidate the old destination's stamps.
            self.hooks.save()
            try:
                service.refresh_options()
            except Exception as exc:
                self.hooks.log(f"[CloudBackup] refresh_options failed: {exc}")

        # -- provider ---------------------------------------------------------------
        provider_group = self._group(page, "Settings.CloudBackup.Provider.Title")
        off_label = t("Settings.CloudBackup.Provider.Off")
        webdav_label = t("Settings.CloudBackup.Provider.WebDav")
        current = (slice_.cloudBackupProvider or PROVIDER_NONE).strip().lower()
        current_label = webdav_label if current == PROVIDER_WEBDAV else off_label

        def _on_provider(name: str) -> None:
            slice_.cloudBackupProvider = PROVIDER_WEBDAV if name == webdav_label else PROVIDER_NONE
            slice_.cloudBackupEnabled = slice_.cloudBackupProvider != PROVIDER_NONE
            persist()

        self._row(
            provider_group,
            t("Settings.CloudBackup.Provider.Title"),
            t("Settings.CloudBackup.Provider.Description"),
            self._dropdown([off_label, webdav_label], current_label, _on_provider),
        )

        # -- connection --------------------------------------------------------------
        connection = self._group(page, "Settings.CloudBackup.Connection.Title")

        def _entry(value: str, on_commit) -> Gtk.Entry:
            entry = Gtk.Entry(visible=True)
            entry.set_text(value)
            entry.set_hexpand(True)
            entry.connect("activate", lambda w: on_commit(w.get_text().strip()))
            return entry

        url_entry = _entry(slice_.cloudBackupUrl, lambda text: (setattr(slice_, "cloudBackupUrl", text), persist()))
        user_entry = _entry(
            slice_.cloudBackupUsername,
            lambda text: (setattr(slice_, "cloudBackupUsername", text), persist()),
        )
        remote_entry = _entry(
            slice_.cloudBackupRemotePath,
            lambda text: (setattr(slice_, "cloudBackupRemotePath", text), persist()),
        )
        self._row(
            connection,
            t("Settings.CloudBackup.ServerUrl.Label"),
            t("Settings.CloudBackup.Connection.Description"),
            url_entry,
        )
        self._row(connection, t("Settings.CloudBackup.Username.Label"), "", user_entry)
        self._row(connection, t("Settings.CloudBackup.RemotePath.Label"), "", remote_entry)

        # -- password ----------------------------------------------------------------
        password_entry = Gtk.Entry(visible=True)
        password_entry.set_visibility(False)
        password_entry.set_placeholder_text(t("Settings.CloudBackup.Password.Label"))
        password_entry.set_hexpand(True)
        password_status = Gtk.Label(xalign=0.0, visible=True)
        password_status.add_css_class("settings-row-description")

        def _refresh_password_status() -> None:
            password_status.set_text(
                t("Settings.CloudBackup.Password.Saved")
                if slice_.cloudBackupPasswordStored
                else t("Settings.CloudBackup.Password.NotSaved")
            )

        def _save_password(*_a) -> None:
            password = password_entry.get_text()
            if not password:
                password_status.set_text(
                    f"{t('Settings.CloudBackup.Password.EmptyTitle')} — {t('Settings.CloudBackup.Password.EmptyBody')}"
                )
                return
            if service.store_credential(get_options(self.settings_service), password):
                password_entry.set_text("")
                _refresh_password_status()
            else:
                password_status.set_text(t("Settings.CloudBackup.Password.SaveFailed"))

        save_button = Gtk.Button(label=t("Settings.CloudBackup.Password.Save"), visible=True)
        save_button.connect("clicked", _save_password)
        password_entry.connect("activate", _save_password)
        password_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=True)
        password_box.append(password_entry)
        password_box.append(save_button)
        password_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        password_column.append(password_box)
        password_column.append(password_status)
        _refresh_password_status()
        self._row(connection, t("Settings.CloudBackup.Password.Label"), "", password_column)

        # -- test connection ------------------------------------------------------------
        test_status = Gtk.Label(xalign=0.0, wrap=True, visible=True)
        test_status.add_css_class("settings-row-description")
        test_button = Gtk.Button(label=t("Settings.CloudBackup.TestConnection"), visible=True)
        test_button.set_halign(Gtk.Align.END)

        def _run_test(*_a) -> None:
            self.hooks.save()
            test_status.set_text("…")

            def worker():
                ok, message = service.test_connection()
                text = (
                    t("Settings.CloudBackup.TestConnection.Success")
                    if ok
                    else fmt("Settings.CloudBackup.TestConnection.Failed", message)
                )
                GLib.idle_add(lambda: test_status.set_text(text))

            threading.Thread(target=worker, daemon=True, name="cloud-backup-test").start()

        test_button.connect("clicked", _run_test)
        test_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        test_column.append(test_button)
        test_column.append(test_status)
        warning = t("Settings.CloudBackup.HttpWarning") if (slice_.cloudBackupUrl or "").startswith("http://") else ""
        self._row(connection, t("Settings.CloudBackup.TestConnection"), warning, test_column)

        # -- data types --------------------------------------------------------------------
        data_group = self._group(page)
        for title_key, description_key, field in (
            (
                "Settings.CloudBackup.TodoData.Title",
                "Settings.CloudBackup.TodoData.Description",
                "cloudBackupTodoEnabled",
            ),
            (
                "Settings.CloudBackup.QuickCaptureData.Title",
                "Settings.CloudBackup.QuickCaptureData.Description",
                "cloudBackupQuickCaptureEnabled",
            ),
            (
                "Settings.CloudBackup.WidgetStyle.Title",
                "Settings.CloudBackup.WidgetStyle.Description",
                "cloudBackupWidgetStyleEnabled",
            ),
        ):

            def _on_domain(state: bool, field=field) -> None:
                setattr(slice_, field, state)
                persist()

            self._row(
                data_group,
                t(title_key),
                t(description_key),
                self._switch(getattr(slice_, field), _on_domain),
            )

        # -- schedule ------------------------------------------------------------------------
        schedule_group = self._group(page, "Settings.CloudBackup.Interval.Title")

        def interval_label(hours: int) -> str:
            if hours == 1:
                return t("Settings.CloudBackup.Interval.Hour")
            if hours == 24:
                return t("Settings.CloudBackup.Interval.Day")
            if hours % 24 == 0:
                return fmt("Settings.CloudBackup.Interval.Days", hours // 24)
            return fmt("Settings.CloudBackup.Interval.Hours", hours)

        interval_labels = [interval_label(hours) for hours in SUPPORTED_INTERVAL_HOURS]
        current_interval = slice_.cloudBackupIntervalHours
        if current_interval not in SUPPORTED_INTERVAL_HOURS:
            current_interval = 24

        def _on_interval(name: str) -> None:
            slice_.cloudBackupIntervalHours = SUPPORTED_INTERVAL_HOURS[interval_labels.index(name)]
            self.hooks.save()

        self._row(
            schedule_group,
            t("Settings.CloudBackup.Interval.Title"),
            t("Settings.CloudBackup.Interval.Description"),
            self._dropdown(interval_labels, interval_label(current_interval), _on_interval),
        )

        retention_labels = [fmt("Settings.CloudBackup.Retention.Count", count) for count in SUPPORTED_RETENTION_COUNTS]
        current_retention = slice_.cloudBackupRetainCount
        if current_retention not in SUPPORTED_RETENTION_COUNTS:
            current_retention = 10

        def _on_retention(name: str) -> None:
            slice_.cloudBackupRetainCount = SUPPORTED_RETENTION_COUNTS[retention_labels.index(name)]
            self.hooks.save()

        self._row(
            schedule_group,
            t("Settings.CloudBackup.Retention.Title"),
            t("Settings.CloudBackup.Retention.Description"),
            self._dropdown(
                retention_labels,
                fmt("Settings.CloudBackup.Retention.Count", current_retention),
                _on_retention,
            ),
        )

        # -- snapshots ---------------------------------------------------------------------------
        snapshots_group = self._group(page, "Settings.CloudBackup.Snapshots.Title")
        backup_now = Gtk.Button(label=t("Settings.CloudBackup.BackupNow"), visible=True)
        backup_now.add_css_class("suggested-action")
        backup_now.connect("clicked", lambda *_a: self._cloud_backup_run_now())
        refresh = Gtk.Button(label=t("Settings.CloudBackup.RefreshSnapshots"), visible=True)
        refresh.connect("clicked", lambda *_a: self._cloud_refresh_snapshots())
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=True)
        buttons.append(backup_now)
        buttons.append(refresh)
        actions_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=True)
        actions_column.append(buttons)
        actions_column.append(self._cloud_status)
        actions_column.append(self._cloud_snapshots)
        self._row(
            snapshots_group,
            t("Settings.CloudBackup.Snapshots.Title"),
            t("Settings.CloudBackup.Snapshots.Description"),
            actions_column,
        )
        self._cloud_refresh_status()
        self._cloud_refresh_snapshots()
        return page

    def _cloud_backup_run_now(self) -> None:
        service = self.hooks.cloud_backup

        def worker():
            result = service.run_backup_now()
            GLib.idle_add(lambda: self._cloud_finish_run(result))

        threading.Thread(target=worker, daemon=True, name="cloud-backup-run").start()

    def _cloud_finish_run(self, result) -> None:
        from ..services.cloud_backup import (
            OUTCOME_IN_PROGRESS,
            OUTCOME_MISSING_CREDENTIAL,
            OUTCOME_NO_SCOPE,
            OUTCOME_NOT_CONFIGURED,
            OUTCOME_PENDING_RESTORE,
            OUTCOME_UPLOADED,
        )

        if result.outcome == OUTCOME_UPLOADED:
            message = fmt("Settings.CloudBackup.BackupNow.Success", result.message)
            if result.unverified:
                message += " " + t("Settings.CloudBackup.BackupNow.UnverifiedSuffix")
            if result.pruned:
                message += " " + fmt("Settings.CloudBackup.BackupNow.PrunedSuffix", result.pruned)
            self._cloud_status.set_text(message)
        elif result.outcome == OUTCOME_IN_PROGRESS:
            self._cloud_status.set_text(t("Settings.CloudBackup.BackupNow.InProgress"))
        elif result.outcome == OUTCOME_NOT_CONFIGURED:
            self._cloud_status.set_text(t("Settings.CloudBackup.NotConfigured"))
        elif result.outcome == OUTCOME_NO_SCOPE:
            self._cloud_status.set_text(t("Settings.CloudBackup.NoScope"))
        elif result.outcome == OUTCOME_MISSING_CREDENTIAL:
            self._cloud_status.set_text(t("Settings.CloudBackup.Password.NotSaved"))
        elif result.outcome == OUTCOME_PENDING_RESTORE:
            self._cloud_status.set_text(t("Settings.CloudBackup.RestorePending"))
        else:
            self._cloud_status.set_text(fmt("Settings.CloudBackup.BackupNow.Failed", result.message))
        self._cloud_refresh_status()
        self._cloud_refresh_snapshots()

    def _cloud_refresh_status(self) -> None:
        slice_ = self.settings_service.settings.cloudBackup
        if slice_.cloudBackupLastUnverifiedUtc:
            text = fmt(
                "Settings.CloudBackup.LastUnverified",
                _localize_stamp(slice_.cloudBackupLastUnverifiedUtc),
            )
        elif slice_.cloudBackupLastSuccessUtc:
            text = fmt(
                "Settings.CloudBackup.LastSuccess",
                _localize_stamp(slice_.cloudBackupLastSuccessUtc),
            )
        elif slice_.cloudBackupLastFailureUtc:
            text = fmt(
                "Settings.CloudBackup.LastFailure",
                _localize_stamp(slice_.cloudBackupLastFailureUtc),
            )
        else:
            text = t("Settings.CloudBackup.LastSuccess.Never")
        try:
            pending = self.hooks.cloud_backup.data_backup.has_pending_restore()
        except Exception:
            pending = False
        if pending:
            text = f"{text}\n{t('Settings.CloudBackup.RestorePending')}"
        self._cloud_status.set_text(text)

    def _cloud_refresh_snapshots(self) -> None:
        service = self.hooks.cloud_backup

        def worker():
            try:
                snapshots = service.list_remote_snapshots()
                error = ""
            except Exception as exc:
                snapshots, error = [], str(exc)
            GLib.idle_add(lambda: self._cloud_render_snapshots(snapshots, error))

        threading.Thread(target=worker, daemon=True, name="cloud-backup-list").start()

    def _cloud_render_snapshots(self, snapshots, error: str) -> None:
        from .search_popup import format_size

        box = self._cloud_snapshots
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        if error:
            self._cloud_status.set_text(fmt("Settings.CloudBackup.RefreshSnapshots.Failed", error))
            return
        if not snapshots:
            empty = Gtk.Label(label=t("Settings.CloudBackup.SnapshotList.Empty"), xalign=0.0, visible=True)
            empty.add_css_class("settings-row-description")
            box.append(empty)
            return
        for snapshot in snapshots:
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=True)
            stamp = (
                snapshot.created_utc.astimezone().strftime("%Y-%m-%d %H:%M") if snapshot.created_utc else snapshot.name
            )
            details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
            details.set_hexpand(True)
            name_label = Gtk.Label(label=stamp, xalign=0.0, visible=True)
            name_label.add_css_class("settings-row-title")
            meta_label = Gtk.Label(
                label=fmt(
                    "Settings.CloudBackup.SnapshotDetails",
                    snapshot.device_id or "—",
                    f" · {format_size(snapshot.size)}",
                ),
                xalign=0.0,
                visible=True,
            )
            meta_label.add_css_class("settings-row-description")
            details.append(name_label)
            details.append(meta_label)
            row_box.append(details)
            restore = Gtk.Button(label=t("Settings.CloudBackup.Restore"), visible=True)
            restore.connect("clicked", lambda *_a, s=snapshot: self._cloud_restore_snapshot(s))
            delete = Gtk.Button(label=t("Settings.DataBackup.Snapshots.Delete"), visible=True)
            delete.connect("clicked", lambda *_a, s=snapshot: self._cloud_delete_snapshot(s))
            row_box.append(restore)
            row_box.append(delete)
            row = Gtk.ListBoxRow(visible=True)
            row.set_activatable(False)
            row.set_child(row_box)
            box.append(row)

    def _cloud_delete_snapshot(self, snapshot) -> None:
        service = self.hooks.cloud_backup
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL,
            text=fmt("Settings.CloudBackup.DeleteSnapshot.Title", snapshot.name),
        )
        dialog.format_secondary_text(fmt("Settings.CloudBackup.DeleteSnapshot.Body", snapshot.name))
        dialog.add_button(t("Settings.DataBackup.Snapshots.Delete"), Gtk.ResponseType.OK)
        ok_button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
        if ok_button is not None:
            ok_button.add_css_class("destructive-action")

        def respond(_d, response):
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return

            def worker():
                error = ""
                try:
                    service.delete_snapshot(snapshot.name)
                except Exception as exc:
                    error = str(exc)
                GLib.idle_add(lambda: self._cloud_after_delete(error))

            threading.Thread(target=worker, daemon=True, name="cloud-backup-delete").start()

        dialog.connect("response", respond)
        dialog.present()

    def _cloud_after_delete(self, error: str) -> None:
        if error:
            self._cloud_status.set_text(fmt("Settings.CloudBackup.DeleteSnapshot.Failed", error))
        self._cloud_refresh_snapshots()

    def _cloud_restore_snapshot(self, snapshot) -> None:
        from ..constants import DATA_ROOT

        service = self.hooks.cloud_backup
        destination = DATA_ROOT / "restore-downloads" / snapshot.name

        def worker():
            from ..services.data_backup import DataBackupService

            try:
                service.download_snapshot(snapshot.name, destination)
                manifest = DataBackupService.peek_manifest(destination)
                GLib.idle_add(lambda: self._cloud_show_restore_dialog(snapshot, destination, manifest))
            except Exception as exc:
                message = str(exc)
                GLib.idle_add(
                    lambda: self._cloud_status.set_text(fmt("Settings.CloudBackup.RestoreFailed.Body", message))
                )

        threading.Thread(target=worker, daemon=True, name="cloud-backup-download").start()

    def _cloud_show_restore_dialog(self, snapshot, destination, manifest) -> None:
        from .search_popup import format_size
        from ..services import backup_domains as domains
        from ..services.backup_domains import try_from_manifest_name

        service = self.hooks.cloud_backup
        dialog = Gtk.Dialog(transient_for=self, modal=True)
        dialog.set_title(t("Settings.CloudBackup.RestoreConfirm.Title"))
        dialog.add_button(t("Common.Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(t("Settings.CloudBackup.RestoreConfirm.Button"), Gtk.ResponseType.OK)
        ok_button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
        if ok_button is not None:
            ok_button.add_css_class("suggested-action")
        content = dialog.get_content_area()  # a Gtk.Box in GTK4
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, visible=True)
        content_box.set_margin_top(14)
        content_box.set_margin_bottom(14)
        content_box.set_margin_start(16)
        content_box.set_margin_end(16)

        manifest_domains = 0
        for name in manifest.get("domains") or []:
            domain = try_from_manifest_name(str(name))
            if domain:
                manifest_domains |= domain

        domain_rows = (
            (domains.TODO_DATA, "Settings.CloudBackup.TodoData.Title"),
            (domains.QUICK_CAPTURE_DATA, "Settings.CloudBackup.QuickCaptureData.Title"),
            (domains.WIDGET_STYLE, "Settings.CloudBackup.WidgetStyle.Title"),
        )
        checks: dict[int, Gtk.CheckButton] = {}
        domain_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        for domain, key in domain_rows:
            check = Gtk.CheckButton(label=t(key), visible=True)
            check.set_active(bool(manifest_domains & domain))
            check.set_sensitive(bool(manifest_domains & domain))
            checks[domain] = check
            domain_list.append(check)

        files = manifest.get("files") or []
        total = sum(int(entry.get("length") or 0) for entry in files if isinstance(entry, dict))
        selected_names = [t(key) for domain, key in domain_rows if manifest_domains & domain]
        body = fmt(
            "Settings.CloudBackup.RestoreConfirm.Body",
            manifest.get("createdAtUtc") or snapshot.name,
            manifest.get("appVersion") or "?",
            len(files),
            format_size(total),
            ", ".join(selected_names) if selected_names else t("Settings.CloudBackup.RestoreDomains.None"),
        )
        content_box.append(Gtk.Label(label=body, xalign=0.0, wrap=True, visible=True))
        content_box.append(domain_list)

        mode_title = Gtk.Label(label=t("Settings.CloudBackup.RestoreConfirm.Mode.Title"), xalign=0.0, visible=True)
        mode_title.add_css_class("settings-row-title")
        merge = Gtk.CheckButton(label=t("Settings.CloudBackup.RestoreConfirm.Mode.Merge"), visible=True)
        merge.set_active(True)
        overwrite = Gtk.CheckButton(label=t("Settings.CloudBackup.RestoreConfirm.Mode.Overwrite"), visible=True)
        overwrite.set_group(merge)
        warning = Gtk.Label(
            label=t("Settings.CloudBackup.RestoreConfirm.OverwriteWarning"),
            xalign=0.0,
            wrap=True,
            visible=True,
        )
        warning.add_css_class("settings-row-description")
        content_box.append(mode_title)
        content_box.append(merge)
        content_box.append(overwrite)
        content_box.append(warning)
        content.append(content_box)

        def respond(_d, response):
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
            requested = 0
            for domain, check in checks.items():
                if check.get_active():
                    requested |= domain
            if not requested:
                self._cloud_status.set_text(t("Settings.CloudBackup.NoScope"))
                return
            try:
                preparation = service.data_backup.prepare_scoped_restore(destination, requested)
                if overwrite.get_active():
                    service.data_backup.set_pending_restore_item_replace_mode(True)
                details = []
                if preparation.remapped:
                    details.append(fmt("Settings.CloudBackup.RestoreConfirm.Remapped", preparation.remapped))
                if preparation.unmapped:
                    details.append(fmt("Settings.CloudBackup.RestoreConfirm.Unmapped", preparation.unmapped))
                if preparation.attachment_refs:
                    details.append(
                        fmt(
                            "Settings.CloudBackup.RestoreConfirm.AttachmentRefs",
                            preparation.attachment_refs,
                        )
                    )
                text = t("Settings.CloudBackup.RestorePending")
                if details:
                    text = f"{text}\n" + "\n".join(details)
                self._cloud_status.set_text(text)
            except Exception as exc:
                self._cloud_status.set_text(fmt("Settings.CloudBackup.RestoreFailed.Body", exc))
            self._cloud_refresh_status()

        dialog.connect("response", respond)
        dialog.present()


def core_theme(settings_service) -> str:
    theme = settings_service.settings.core.theme
    return theme if theme in THEMES else "System"


def gesture_label(modifiers: int, keysym: int) -> str:
    parts = []
    if modifiers & MODIFIER_CONTROL:
        parts.append("Ctrl")
    if modifiers & MODIFIER_ALT:
        parts.append("Alt")
    if modifiers & MODIFIER_SHIFT:
        parts.append("Shift")
    if modifiers & MODIFIER_WINDOWS:
        parts.append("Win")
    try:
        key_name = Gdk.keyval_name(keysym) or f"0x{keysym:x}"
    except Exception:
        key_name = f"0x{keysym:x}"
    parts.append(key_name)
    return " + ".join(parts)


def _localize_stamp(value) -> str:
    """ISO-UTC stamp → brief local representation for status labels."""
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value or "")
