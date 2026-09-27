"""File surface view: icon grid / list, context menus, DnD, clipboard keys.

Renders a FileWidgetController. Icon view = Gtk.FlowBox tiles; list view =
Gtk.ListBox rows. Right-click and the surface background both open the
context menu (Gio.Menu + SimpleActionGroup "file"). Drops read the modifier
state for intent: Ctrl=copy, Shift=move, Ctrl+Shift/Alt=shortcut, plain =
the widget's managed drop action.
"""

from __future__ import annotations

import os
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from ..app_log import get_logger
from .. import i18n
from ..constants import SortMode, ViewMode
from ..i18n import fmt, t
from ..services.file_sort import FileEntry
from ..services.file_widget_controller import (
    Confirmation,
    DropIntent,
    FileWidgetController,
)

_LOG = get_logger("dnd")

_MIN_TILE = 72
_MAX_TILE = 132


def _uris_from_drop_value(value) -> List[str]:
    """Extract file URIs from whatever the drop marshalled to.

    A Gdk.FileList drop arrives as a boxed value (get_files); internal drags
    arrive as plain strings; some compositors deliver a bare list or a single
    Gio.File. Handle them all.
    """
    files = []
    getter = getattr(value, "get_files", None)
    if getter is not None:
        try:
            files = list(getter())
        except Exception as exc:
            _LOG.warning("[DnD] file-list unreadable: %r", exc)
    elif isinstance(value, (list, tuple)):
        files = list(value)
    elif hasattr(value, "get_uri"):
        files = [value]
    uris = []
    for item in files:
        get_uri = getattr(item, "get_uri", None)
        if get_uri is not None:
            try:
                uri = get_uri()
                if uri:
                    uris.append(uri)
            except Exception:
                pass
    return uris


class FileSurface(Gtk.Box):
    def __init__(
        self,
        controller: FileWidgetController,
        icon_size: int = 32,
        settings_service=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.controller = controller
        self.icon_size = icon_size
        self.settings_service = settings_service  # for the empty-state drop action
        self.rename_popover: Optional[Gtk.Popover] = None
        self._stack_popover: Optional[Gtk.Popover] = None

        controller.on_entries_changed = self.queue_rebuild
        controller.on_config_changed = lambda _cfg: None

        # navigation bar (hidden at root)
        self.nav_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, visible=False)
        self.nav_bar.add_css_class("nav-bar")
        self.back_button = Gtk.Button(label="←", visible=True)
        self.back_button.set_tooltip_text(i18n.t("Widget.FolderNavigation.Up"))
        self.back_button.connect("clicked", lambda *_: self.controller.go_back())
        self.crumb_label = Gtk.Label(visible=True)
        self.crumb_label.add_css_class("nav-crumb-label")
        self.crumb_label.set_ellipsize(True)
        self.nav_bar.append(self.back_button)
        self.nav_bar.append(self.crumb_label)
        self.append(self.nav_bar)

        # views
        self.stack = Gtk.Stack(visible=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.flow = self._build_flow()
        self.list = self._build_list()
        self.stack.add_named(self.flow, ViewMode.ICON)
        self.stack.add_named(self.list, ViewMode.LIST)
        self.append(self.stack)
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)

        # empty state
        self.empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        self.empty_box.add_css_class("empty-state")
        self.empty_title = Gtk.Label(visible=True)
        self.empty_title.add_css_class("empty-state-title")
        self.empty_text = Gtk.Label(visible=True)
        self.empty_text.add_css_class("empty-state-text")
        self.empty_text.set_wrap(True)
        self.empty_hint = Gtk.Label(visible=True)
        self.empty_hint.add_css_class("empty-state-text")
        self.empty_box.append(self.empty_title)
        self.empty_box.append(self.empty_text)
        self.empty_box.append(self.empty_hint)
        self.stack.add_named(self.empty_box, "empty")

        # context menu
        self.actions = self._build_actions()
        self.insert_action_group("file", self.actions)
        self.stack_actions = self._build_stack_actions()
        self.insert_action_group("stack", self.stack_actions)
        self.menu_model = self._build_menu_model()

        # background interactions
        self._setup_dnd()
        self._setup_keys()
        background = Gtk.GestureClick()
        background.set_button(3)
        background.connect("pressed", self._on_background_menu)
        self.flow.add_controller(background)
        list_bg = Gtk.GestureClick()
        list_bg.set_button(3)
        list_bg.connect("pressed", self._on_background_menu)
        self.list.add_controller(list_bg)

        # Surface owns the controller lifecycle (start is idempotent).
        controller.start()
        self.rebuild()

    # ---- construction -----------------------------------------------------------

    def _build_flow(self) -> Gtk.FlowBox:
        flow = Gtk.FlowBox(visible=True)
        flow.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        flow.set_homogeneous(True)
        # 1, not 3: with a per-line floor the cells stretch to share the full
        # widget width, so two icons sit far apart. Natural-width cells keep
        # desktop-like pitch at any file count.
        flow.set_min_children_per_line(1)
        # GTK defaults this to TRUE — a plain click would activate (open).
        # Plain-click behavior is owned by _attach_item_gestures; the
        # built-in activation path stays available to the keyboard only.
        flow.set_activate_on_single_click(False)
        flow.connect("child-activated", self._on_activate_flow)
        return flow

    def _apply_grid_spacing(self) -> None:
        """Icon pitch from the density settings: gap = icon size × scale.

        Runs on every rebuild — iconSize / spacing scales change through
        Settings → apply_appearance → queue_rebuild, long after construction.
        """
        h_scale, v_scale = 0.40, 0.60  # Standard preset, when no service
        if self.settings_service is not None:
            shell = self.settings_service.settings.widgetShell
            h_scale = max(0.0, shell.horizontalSpacingScale)
            v_scale = max(0.0, shell.verticalSpacingScale)
        self.flow.set_column_spacing(round(self.icon_size * h_scale))
        self.flow.set_row_spacing(round(self.icon_size * v_scale))

    def _build_list(self) -> Gtk.ListBox:
        listbox = Gtk.ListBox(visible=True)
        listbox.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        listbox.connect("row-activated", self._on_activate_row)
        return listbox

    def _build_actions(self) -> Gio.SimpleActionGroup:
        group = Gio.SimpleActionGroup()

        def add(name: str, callback) -> None:
            action = Gio.SimpleAction.new(name)
            action.connect("activate", callback)
            group.add_action(action)

        add("open", lambda *_a: self._open_selected())
        add("reveal", lambda *_a: self.controller.reveal(self.selected_entries()))
        add(
            "shortcut",
            lambda *_a: [self.controller.create_shortcut(e) for e in self.selected_entries()],
        )
        add("copy-path", lambda *_a: self.controller.copy_paths(self.selected_entries()))
        add("cut", lambda *_a: self.controller.cut(self.selected_entries()))
        add("copy", lambda *_a: self.controller.copy(self.selected_entries()))
        add("paste", lambda *_a: self._paste())
        add("rename", lambda *_a: self._rename_selected())
        add("trash", lambda *_a: self._trash_selected())
        add("delete", lambda *_a: self._delete_selected())
        add("new-folder", lambda *_a: self.controller.new_folder())
        add("refresh", lambda *_a: self.refresh())
        add(
            "back-desktop",
            lambda *_a: self.controller.move_back_to_desktop(self.selected_entries()),
        )
        add("view-icon", lambda *_a: self._set_view(ViewMode.ICON))
        add("view-list", lambda *_a: self._set_view(ViewMode.LIST))
        for mode in (
            SortMode.NAME,
            SortMode.SIZE,
            SortMode.TYPE,
            SortMode.DATE_MODIFIED,
            SortMode.MANUAL,
        ):
            add(f"sort-{mode.lower()}", lambda *_a, m=mode: self.controller.set_sort_mode(m))
        return group

    def _build_menu_model(self) -> Gio.Menu:
        menu = Gio.Menu.new()

        item_menu = Gio.Menu.new()
        item_menu.append(t("Widget.Open"), "file.open")
        item_menu.append(t("Widget.ShowInExplorer"), "file.reveal")
        item_menu.append(t("Widget.CreateShortcut"), "file.shortcut")
        item_menu.append(t("Widget.CopyPath"), "file.copy-path")
        menu.append_section(None, item_menu)

        clip_menu = Gio.Menu.new()
        clip_menu.append(t("Common.Cut"), "file.cut")
        clip_menu.append(t("Common.Copy"), "file.copy")
        clip_menu.append(t("Common.Paste"), "file.paste")
        clip_menu.append(t("Common.Rename"), "file.rename")
        menu.append_section(None, clip_menu)

        delete_menu = Gio.Menu.new()
        delete_menu.append(t("Widget.MoveToRecycleBin"), "file.trash")
        delete_menu.append(t("Common.Delete"), "file.delete")
        menu.append_section(None, delete_menu)

        surface_menu = Gio.Menu.new()
        surface_menu.append(t("Common.NewFolder"), "file.new-folder")
        surface_menu.append(t("Widget.MoveBackToDesktop"), "file.back-desktop")
        surface_menu.append(t("Common.Refresh"), "file.refresh")
        menu.append_section(None, surface_menu)

        view_menu = Gio.Menu.new()
        view_menu.append(t("Widget.IconView"), "file.view-icon")
        view_menu.append(t("Widget.ListView"), "file.view-list")
        sort_menu = Gio.Menu.new()
        for mode, key in (
            (SortMode.NAME, "Widget.Sort.Name"),
            (SortMode.SIZE, "Widget.Sort.Size"),
            (SortMode.TYPE, "Widget.Sort.Type"),
            (SortMode.DATE_MODIFIED, "Widget.Sort.DateModified"),
            (SortMode.MANUAL, "Widget.Sort.Manual"),
        ):
            sort_menu.append(t(key), f"file.sort-{mode.lower()}")
        view_menu.append_submenu(t("Widget.SortBy"), sort_menu)
        menu.append_section(t("Widget.ViewAndSort"), view_menu)

        stacks_menu = Gio.Menu.new()
        stacks_menu.append(t("Widget.Stack.EnableForWidget"), "stack.enable-widget")
        stacks_menu.append(t("Widget.Stack.FollowDefaults"), "stack.follow-defaults")
        stacks_menu.append(t("Widget.Stack.RestoreGroups"), "stack.restore-groups")
        stacks_menu.append(t("Widget.Stack.Start"), "stack.start")
        menu.append_section(t("Widget.Stack.Menu"), stacks_menu)
        return menu

    # ---- rendering -----------------------------------------------------------------

    def queue_rebuild(self) -> None:
        GLib.idle_add(self.rebuild)

    def rebuild(self) -> None:
        self._apply_grid_spacing()
        at_root = self.controller.is_at_root
        self.nav_bar.set_visible(not at_root)
        if not at_root:
            self.crumb_label.set_text("  ›  ".join(self.controller.crumbs()))

        for child in list(self._flow_children()):
            self.flow.remove(child)
        for row in list(self._list_rows()):
            self.list.remove(row)

        units = self.controller.visible_units()
        for unit in units:
            if unit.is_stack:
                self.flow.append(self._build_stack_tile(unit))
                self.list.append(self._build_stack_row(unit))
            else:
                child = self._build_tile(unit.entry)
                row = self._build_row(unit.entry)
                if unit.child_of:
                    child.add_css_class("stack-child")
                    row.add_css_class("stack-child")
                self.flow.append(child)
                self.list.append(row)

        empty = not units
        if empty:
            self._fill_empty_state()
        visible_name = "empty" if empty else self.controller.config.viewMode
        self.stack.set_visible_child_name(visible_name)

    def _flow_children(self):
        child = self.flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            yield child
            child = nxt

    def _list_rows(self):
        row = self.list.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            yield row
            row = nxt

    def _build_tile(self, entry: FileEntry) -> Gtk.FlowBoxChild:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
        box.add_css_class("file-tile")
        image = self._icon_image(entry)
        image.add_css_class("file-tile-icon")
        image.set_halign(Gtk.Align.CENTER)
        image.set_valign(Gtk.Align.START)
        box.append(image)
        label = Gtk.Label(visible=True)
        label.add_css_class("file-tile-label")
        label.set_text(self.controller.display_name_for(entry))
        label.set_ellipsize(True)  # 2-line clamp via lines
        label.set_lines(self._name_lines())
        label.set_wrap(True)
        label.set_max_width_chars(14)
        box.append(label)
        child = Gtk.FlowBoxChild(visible=True)
        child.set_child(box)
        # FlowBox cells fill the box vertically; without START the tile
        # stretches to the whole content height (one file = one giant column).
        child.set_valign(Gtk.Align.START)
        child.entry = entry  # type: ignore[attr-defined]
        self._attach_item_gestures(child, box, entry)
        self._attach_drag_source(box, entry)
        return child

    def _build_row(self, entry: FileEntry) -> Gtk.ListBoxRow:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        box.add_css_class("file-row")
        image = self._icon_image(entry, small=True)
        box.append(image)
        name = Gtk.Label(visible=True)
        name.add_css_class("file-row-name")
        name.set_text(self.controller.display_name_for(entry))
        name.set_hexpand(True)
        name.set_xalign(0.0)
        name.set_ellipsize(True)
        box.append(name)
        detail = Gtk.Label(visible=True)
        detail.add_css_class("file-row-detail")
        detail.set_text(self._detail_text(entry))
        box.append(detail)
        row = Gtk.ListBoxRow(visible=True)
        row.set_child(box)
        row.entry = entry  # type: ignore[attr-defined]
        self._attach_item_gestures(row, box, entry)
        self._attach_drag_source(box, entry)
        return row

    # ---- stack tiles -----------------------------------------------------------------

    def _stack_tile_name(self, unit) -> str:
        name = unit.stack_name
        if name.startswith("Widget.Stack.Category."):
            return t(name)
        return name

    def _build_stack_tile(self, unit) -> Gtk.FlowBoxChild:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
        box.add_css_class("file-tile")
        box.add_css_class("stack-tile")
        icon_holder = Gtk.Overlay(visible=True)
        glyph = Gtk.Image.new_from_icon_name("folder")
        glyph.set_pixel_size(self.icon_size)
        glyph.add_css_class("stack-tile-icon")
        icon_holder.set_child(glyph)
        badge = Gtk.Label(visible=True)
        badge.add_css_class("stack-count-badge")
        badge.set_text(str(len(unit.stack.items)))
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.START)
        icon_holder.add_overlay(badge)
        box.append(icon_holder)
        label = Gtk.Label(visible=True)
        label.add_css_class("file-tile-label")
        label.set_text(self._stack_tile_name(unit))
        label.set_ellipsize(True)
        label.set_lines(1)
        label.set_max_width_chars(14)
        box.append(label)
        if unit.expanded:
            box.add_css_class("stack-tile-expanded")
        child = Gtk.FlowBoxChild(visible=True)
        child.set_child(box)
        child.set_valign(Gtk.Align.START)  # keep the tile off the cell floor
        child.stack_key = unit.order_key  # type: ignore[attr-defined]
        self._attach_stack_gestures(child, box, unit)
        return child

    def _build_stack_row(self, unit) -> Gtk.ListBoxRow:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        box.add_css_class("file-row")
        box.add_css_class("stack-tile")
        image = self._icon_image_for_name("folder", small=True)
        box.append(image)
        name = Gtk.Label(visible=True)
        name.add_css_class("file-row-name")
        name.set_text(self._stack_tile_name(unit))
        name.set_hexpand(True)
        name.set_xalign(0.0)
        name.set_ellipsize(True)
        box.append(name)
        detail = Gtk.Label(visible=True)
        detail.add_css_class("file-row-detail")
        detail.set_text(fmt("Widget.Stack.ItemCount", len(unit.stack.items)))
        box.append(detail)
        marker = Gtk.Label(visible=True)
        marker.set_text("▾" if unit.expanded else "▸")
        box.append(marker)
        row = Gtk.ListBoxRow(visible=True)
        row.set_child(box)
        row.stack_key = unit.order_key  # type: ignore[attr-defined]
        self._attach_stack_gestures(row, box, unit)
        return row

    def _icon_image_for_name(self, icon_name: str, small: bool = False) -> Gtk.Image:
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(max(16, self.icon_size - 8) if small else self.icon_size)
        return image

    def _attach_stack_gestures(self, holder: Gtk.Widget, widget: Gtk.Widget, unit) -> None:
        menu = Gtk.GestureClick()
        menu.set_button(3)
        menu.connect(
            "pressed",
            lambda g, _n, _x, _y: self._on_stack_menu(g, widget, unit),
        )
        widget.add_controller(menu)

    def _on_stack_menu(self, gesture: Gtk.GestureClick, widget: Gtk.Widget, unit) -> None:
        menu = Gio.Menu.new()

        def item(label_key: str, action: str) -> Gio.MenuItem:
            menu_item = Gio.MenuItem.new(t(label_key), None)
            menu_item.set_action_and_target_value(action, GLib.Variant("s", unit.order_key))
            return menu_item

        state_section = Gio.Menu.new()
        state_section.append_item(
            item("Widget.Stack.Collapse" if unit.expanded else "Widget.Stack.Expand", "stack.toggle")
        )
        state_section.append_item(item("Widget.Stack.SelectContents", "stack.select-contents"))
        state_section.append_item(item("Widget.Stack.CopyContentPaths", "stack.copy-paths"))
        menu.append_section(None, state_section)

        organize_section = Gio.Menu.new()
        organize_section.append_item(item("Widget.Stack.Rename", "stack.rename"))
        organize_section.append_item(item("Widget.Stack.MoveUp", "stack.move-up"))
        organize_section.append_item(item("Widget.Stack.MoveDown", "stack.move-down"))
        if unit.is_manual:
            organize_section.append_item(item("Widget.Stack.Dissolve", "stack.dissolve"))
        else:
            organize_section.append_item(item("Widget.Stack.DisableGroup", "stack.disable"))
        menu.append_section(None, organize_section)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(widget)
        popover.set_has_arrow(False)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.popup()
        popover.connect("closed", lambda *_a: popover.unparent())

    # ---- stack activation + popover ------------------------------------------------------

    def _activate_stack(self, unit, holder: Gtk.Widget) -> None:
        settings = self.controller.stack_settings()
        if settings["open_mode"] == "Popover":
            self._open_stack_popover(unit, holder)
        else:
            self.controller.toggle_stack(unit.order_key)

    def _open_stack_popover(self, unit, holder: Gtk.Widget) -> None:
        from ..services.stack_popover import calculate_layout

        members = unit.stack.items
        is_list = self.controller.config.viewMode == ViewMode.LIST
        item_width = self.icon_size + 44
        item_height = self.icon_size + 34
        layout = calculate_layout(
            is_list_mode=is_list,
            item_count=len(members),
            widget_width=max(240, self.get_width()),
            work_area_width=1280,
            work_area_height=800,
            item_width=item_width,
            item_height=item_height,
            layout_mode=self.controller.stack_settings()["popover_layout"],
        )

        surface = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=True)
        surface.add_css_class("stack-popover")
        surface.set_size_request(int(min(layout.width, 720)), -1)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        title = Gtk.Label(visible=True)
        title.set_xalign(0.0)
        title.set_hexpand(True)
        title.set_ellipsize(True)
        title.set_text(self._stack_tile_name(unit))
        title.add_css_class("stack-popover-title")
        header.append(title)
        count = Gtk.Label(visible=True)
        count.add_css_class("stack-popover-count")
        count.set_text(fmt("Widget.Stack.ItemCount", len(members)))
        header.append(count)
        close = Gtk.Button(label="✕", visible=True)
        close.set_tooltip_text(t("Widget.Stack.Popover.Close"))
        header.append(close)
        surface.append(header)

        scrolled = Gtk.ScrolledWindow(visible=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_max_content_height(int(layout.items_height))
        scrolled.set_propagate_natural_height(True)
        surface.append(scrolled)
        if not members:
            empty = Gtk.Label(visible=True)
            empty.set_text(t("Widget.Stack.Popover.Empty"))
            scrolled.set_child(empty)
        elif is_list:
            listing = Gtk.ListBox(visible=True)
            listing.set_selection_mode(Gtk.SelectionMode.SINGLE)
            for entry in members:
                listing.append(self._build_row(entry))
            listing.connect("row-activated", self._on_activate_row)
            scrolled.set_child(listing)
        else:
            grid = self._build_flow()
            grid.set_min_children_per_line(max(1, layout.columns))
            grid.set_max_children_per_line(max(1, layout.columns))
            for entry in members:
                grid.append(self._build_tile(entry))
            grid.connect("child-activated", self._on_activate_flow)
            scrolled.set_child(grid)

        popover = Gtk.Popover()  # visible only after set_parent: realize needs a native parent
        popover.set_child(surface)
        popover.set_parent(holder)
        popover.set_position(Gtk.PositionType.BOTTOM)
        close.connect("clicked", lambda *_a: popover.popdown())
        popover.connect("closed", lambda *_a: popover.unparent())
        self._stack_popover = popover
        popover.popup()

    # ---- stack actions -------------------------------------------------------------------

    def _build_stack_actions(self) -> Gio.SimpleActionGroup:
        group = Gio.SimpleActionGroup()
        string_type = GLib.VariantType.new("s")

        def add(name: str, callback) -> None:
            action = Gio.SimpleAction.new(name, string_type)
            action.connect("activate", lambda a, param: callback(param.get_string()))
            group.add_action(action)

        def add_stateless(name: str, callback) -> None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            group.add_action(action)

        add("toggle", lambda key: self.controller.toggle_stack(key))
        add("select-contents", self._stack_select_contents)
        add("copy-paths", self._stack_copy_paths)
        add("rename", self._stack_rename)
        add("move-up", lambda key: self.controller.move_stack(key, -1))
        add("move-down", lambda key: self.controller.move_stack(key, +1))
        add("dissolve", lambda key: self.controller.dissolve_stack(key))
        add("disable", lambda key: self.controller.disable_stack_group(key))
        add("remove-from", self._stack_remove_selected)
        add_stateless("enable-widget", self._stack_toggle_widget_enabled)
        add_stateless("follow-defaults", self.controller.follow_global_stack_defaults)
        add_stateless("restore-groups", self.controller.restore_disabled_groups)
        add_stateless("start", self._stack_start)
        return group

    def _stack_toggle_widget_enabled(self) -> None:
        self.controller.set_widget_stacks_enabled(not self.controller.stacks_enabled)

    def _stack_select_contents(self, key: str) -> None:
        self.controller.expand_stack(key)
        GLib.idle_add(lambda: self._select_stack_children(key) or False)

    def _select_stack_children(self, key: str) -> bool:
        if self.stack.get_visible_child_name() == ViewMode.LIST:
            row = self.list.get_first_child()
            while row is not None:
                entry = getattr(row, "entry", None)
                if entry is not None and self.controller.containing_stack_key(entry.path) == key:
                    self.list.select_row(row)
                row = row.get_next_sibling()
        else:
            for child in self._flow_children():
                entry = getattr(child, "entry", None)
                if entry is not None and self.controller.containing_stack_key(entry.path) == key:
                    self.flow.select_child(child)
        return False

    def _stack_copy_paths(self, key: str) -> None:
        try:
            import gi

            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            Gdk.Display.get_default().get_clipboard().set_text("\n".join(self.controller.stack_member_paths(key)))
        except Exception:
            pass

    def _stack_rename(self, key: str) -> None:
        unit = self.controller.stack_unit(key)
        if unit is None or not unit.is_stack:
            return
        holder = next(
            (c for c in self._flow_children() if getattr(c, "stack_key", None) == key),
            None,
        )
        if holder is None:
            holder = next(
                (r for r in self._list_rows() if getattr(r, "stack_key", None) == key),
                None,
            )
        if holder is None:
            return
        popover = Gtk.Popover()  # parented before showing (see _open_stack_popover)
        popover.set_parent(holder)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        popover.set_child(box)
        entry = Gtk.Entry(visible=True)
        entry.set_text(self._stack_tile_name(unit))
        entry.set_width_chars(18)
        box.append(entry)
        apply_btn = Gtk.Button(label=t("Common.Ok"), visible=True)
        box.append(apply_btn)

        def commit(*_a):
            self.controller.rename_stack(key, entry.get_text())
            popover.popdown()

        keys = Gtk.EventControllerKey()
        entry.add_controller(keys)

        def on_key(_k, keyval: int, _raw, _state) -> bool:
            if keyval == Gdk.KEY_Return:
                commit()
                return True
            if keyval == Gdk.KEY_Escape:
                popover.popdown()
                return True
            return False

        keys.connect("key-pressed", on_key)
        apply_btn.connect("clicked", commit)
        popover.connect("closed", lambda *_a: popover.unparent())
        popover.popup()
        entry.grab_focus()

    def _stack_start(self) -> None:
        self.controller.create_manual_stack_from([e.path for e in self.selected_entries()])

    def _stack_remove_selected(self, _key: str) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        containing = next(
            (
                self.controller.containing_stack_key(e.path)
                for e in entries
                if self.controller.containing_stack_key(e.path)
            ),
            None,
        )
        if containing is None:
            return
        self.controller.remove_paths_from_stack(containing, [e.path for e in entries])

    def _name_lines(self) -> int:
        return 2  # settings' fileNameLineCount wires in with the settings window

    def _detail_text(self, entry: FileEntry) -> str:
        if entry.is_folder:
            return ""
        size = entry.size
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{int(size)} {unit}" if unit == "B" else f"{size:.0f} {unit}"
            size /= 1024.0
        return ""

    def _icon_image(self, entry: FileEntry, small: bool = False) -> Gtk.Image:
        size = max(16, self.icon_size - 8) if small else self.icon_size
        icon_name = self._icon_name_for(entry)
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(size)
        return image

    def _icon_name_for(self, entry: FileEntry) -> str:
        if entry.is_folder:
            return "folder"
        try:
            content_type = Gio.content_type_guess(entry.name, None)[0]
            icon = Gio.content_type_get_icon(content_type)
            names = icon.get_names() if hasattr(icon, "get_names") else []
        except Exception:
            names = []
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        for name in names:
            if name and theme.lookup_icon(name, None, 16, 1, Gtk.TextDirection.NONE, 0) is not None:
                return name
        return "text-x-generic"

    def _fill_empty_state(self) -> None:
        controller = self.controller
        if controller.is_managed:
            self.empty_title.set_text(t("Widget.Empty.ManagedTitle"))
            self.empty_text.set_text(
                fmt(
                    "Widget.Empty.ManagedText",
                    self._managed_action_text(),
                    self._mapped_folder_display_name(),
                )
            )
        else:
            self.empty_title.set_text(t("Widget.Empty.MappedTitle"))
            self.empty_text.set_text(fmt("Widget.Empty.MappedText", self._mapped_folder_display_name()))
        self.empty_hint.set_text(t("Widget.Empty.ActionsHint"))

    def _managed_action_text(self) -> str:
        """WidgetViewModel.GetManagedActionText — Move/FollowSystem/Copy labels."""
        if self.settings_service is not None:
            action = self.settings_service.settings.fileWidget.managedDropAction
            if action == "Move":
                return t("Common.Move")
            if action == "FollowSystem":
                return t("Settings.DropAction.System")
        return t("Common.Copy")

    def _mapped_folder_display_name(self) -> str:
        """WidgetViewModel.GetMappedFolderDisplayName — basename, Desktop special-case."""
        path = self.controller.folder_path
        if not path.strip():
            return t("Common.CurrentLocation")
        try:
            if os.path.realpath(path) == os.path.realpath(os.path.expanduser("~/Desktop")):
                return t("Common.Desktop")
        except OSError:
            pass
        name = os.path.basename(path.rstrip("/"))
        return name or path

    # ---- selection -------------------------------------------------------------------

    def selected_entries(self) -> List[FileEntry]:
        if self.stack.get_visible_child_name() == ViewMode.LIST:
            rows = self.list.get_selected_rows()
            return [row.entry for row in rows]  # type: ignore[attr-defined]
        entries = []
        for child in self._flow_children():
            if child.is_selected():
                entries.append(child.entry)  # type: ignore[attr-defined]
        return entries

    def _on_activate_flow(self, _flow: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        stack_key = getattr(child, "stack_key", None)
        if stack_key:
            unit = self.controller.stack_unit(stack_key)
            if unit is not None:
                self._activate_stack(unit, child)
            return
        entry = child.entry  # type: ignore[attr-defined]
        self.controller.open(entry)

    def _on_activate_row(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        stack_key = getattr(row, "stack_key", None)
        if stack_key:
            unit = self.controller.stack_unit(stack_key)
            if unit is not None:
                self._activate_stack(unit, row)
            return
        entry = row.entry  # type: ignore[attr-defined]
        self.controller.open(entry)

    # ---- gestures ---------------------------------------------------------------------

    def _attach_item_gestures(self, holder: Gtk.Widget, widget: Gtk.Widget, entry: FileEntry) -> None:
        menu = Gtk.GestureClick()
        menu.set_button(3)
        menu.connect(
            "pressed",
            lambda g, _n, _x, _y: self._on_item_menu(g, widget, entry),
        )
        widget.add_controller(menu)

        # Plain clicks belong to us, not the built-in MULTIPLE-mode handler:
        # it counts presses across the WHOLE FlowBox, so A1-then-C2 within
        # the double-click time reads as a double click and it activates a
        # selected RANGE (files "opening" on a plain click). Claim the
        # sequence at release, in capture phase, so the built-in release
        # processing never runs; double clicks are then judged per-tile by
        # this gesture, exactly like the system double-click standard.
        click = Gtk.GestureClick()
        click.set_button(1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        # A toggle-off click may still turn out to be the first half of a
        # double click — committing it instantly made the icon flash
        # unselected→selected before opening. It is deferred past the
        # double-click window; the second press cancels it.
        state = {"was_selected": False, "pending_deselect": 0}

        def cancel_pending() -> None:
            if state["pending_deselect"]:
                GLib.source_remove(state["pending_deselect"])
                state["pending_deselect"] = 0

        def on_pressed(_g, n, _x, _y):
            if n >= 2:
                cancel_pending()
            state["was_selected"] = n == 1 and holder.is_selected()

        def on_released(g, n, _x, _y):
            if g.get_current_event_state() & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                return  # ctrl/shift multi-select stays with the built-in
            g.set_state(Gtk.EventSequenceState.CLAIMED)
            if n == 1 and state["was_selected"]:

                def commit_deselect() -> bool:
                    state["pending_deselect"] = 0
                    if holder.get_parent() is not None:  # tile may be rebuilt away
                        self._set_holder_selected(holder, selected=False)
                    return GLib.SOURCE_REMOVE

                cancel_pending()
                settings = Gtk.Settings.get_default()
                window_ms = settings.get_property("gtk-double-click-time") if settings else 400
                state["pending_deselect"] = GLib.timeout_add(int(window_ms) + 100, commit_deselect)
                return
            self._handle_item_click(holder, n, state["was_selected"])

        click.connect("pressed", on_pressed)
        click.connect("released", on_released)
        widget.add_controller(click)

    def _handle_item_click(self, holder: Gtk.Widget, n_press: int, was_selected: bool) -> None:
        """Plain-click verdict: single click selects this item alone (or
        deselects it when it was the selection); double click opens it — and,
        as on the desktop, opens every selected item when this item is part
        of a multi-selection."""
        if n_press >= 2:
            selected = self.selected_entries()
            entry = getattr(holder, "entry", None)
            if entry is not None and len(selected) > 1 and entry in selected:
                for item in selected:
                    self.controller.open(item)
                return
            self._set_holder_selected(holder, selected=True, single=True)
            self._activate_holder(holder)
        elif was_selected:
            self._set_holder_selected(holder, selected=False)
        else:
            self._set_holder_selected(holder, selected=True, single=True)

    def _set_holder_selected(self, holder: Gtk.Widget, selected: bool, single: bool = False) -> None:
        """single: this plain click's item becomes THE selection (the previous
        selection goes away), matching every desktop file manager."""
        is_row = isinstance(holder, Gtk.ListBoxRow)
        if single:
            (self.list if is_row else self.flow).unselect_all()
        if is_row:
            if selected:
                self.list.select_row(holder)
            else:
                self.list.unselect_row(holder)
        else:
            if selected:
                self.flow.select_child(holder)
            else:
                self.flow.unselect_child(holder)

    def _activate_holder(self, holder: Gtk.Widget) -> None:
        if isinstance(holder, Gtk.ListBoxRow):
            self._on_activate_row(self.list, holder)
        else:
            self._on_activate_flow(self.flow, holder)

    def _on_item_menu(self, gesture: Gtk.GestureClick, widget: Gtk.Widget, entry: FileEntry) -> None:
        # Select the item under the cursor unless a multi-selection exists.
        if entry not in self.selected_entries():
            holder = widget.get_ancestor(Gtk.FlowBoxChild) or widget.get_ancestor(Gtk.ListBoxRow)
            if holder is not None:
                if isinstance(holder, Gtk.FlowBoxChild):
                    self.flow.select_child(holder)
                else:
                    self.list.select_row(holder)
        popover = Gtk.PopoverMenu.new_from_model(self.menu_model)
        popover.set_parent(widget)
        popover.set_has_arrow(False)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.popup()
        self._current_popover = popover
        popover.connect("closed", lambda *_a: popover.unparent())

    def _on_background_menu(self, gesture: Gtk.GestureClick, _n: int, _x: float, _y: float) -> None:
        self.flow.unselect_all()
        self.list.unselect_all()
        popover = Gtk.PopoverMenu.new_from_model(self.menu_model)
        popover.set_parent(self)
        popover.set_has_arrow(False)
        popover.popup()
        popover.connect("closed", lambda *_a: popover.unparent())

    # ---- DnD ------------------------------------------------------------------------------

    def _setup_dnd(self) -> None:
        # ONE target with a gtype union: two separate targets (FileList + str)
        # race for the drop, and a str target that wins cannot read from
        # uri-list-only sources (Nautilus) — the drop data read then fails.
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        target.set_gtypes([Gdk.FileList.__gtype__, str])
        target.connect("drop", self._on_drop)
        target.connect("motion", self._on_drop_motion)
        target.connect("accept", self._on_drag_enter)
        self.stack.add_controller(target)

    def _on_drag_enter(self, _target: Gtk.DropTarget, drop: Gdk.Drop) -> bool:
        try:
            formats = drop.get_formats().to_string()
            _LOG.info("[DnD] accept actions=%s formats=%s", int(drop.get_actions()), formats)
        except Exception as exc:
            _LOG.warning("[DnD] accept (unreadable: %r)", exc)
        return True

    def _on_drop_motion(self, target: Gtk.DropTarget, _x: float, _y: float):
        state = _current_modifiers()
        if state & Gdk.ModifierType.CONTROL_MASK and state & Gdk.ModifierType.SHIFT_MASK:
            desired = Gdk.DragAction.COPY  # shortcut intent, harmless to report copy
        elif state & Gdk.ModifierType.CONTROL_MASK:
            desired = Gdk.DragAction.COPY
        elif state & Gdk.ModifierType.SHIFT_MASK:
            desired = Gdk.DragAction.MOVE
        else:
            desired = Gdk.DragAction.MOVE
        # Reply with something the source actually offers: an empty action
        # intersection cancels the whole drop (the file bounces off).
        offered = 0
        try:
            current = target.get_current_drop()
            if current is not None:
                offered = current.get_actions()
        except Exception:
            pass
        if desired & offered:
            return desired
        if Gdk.DragAction.MOVE & offered:
            return Gdk.DragAction.MOVE
        if Gdk.DragAction.COPY & offered:
            return Gdk.DragAction.COPY
        return offered or Gdk.DragAction.COPY

    def _drop_intent(self) -> str:
        state = _current_modifiers()
        if state & Gdk.ModifierType.CONTROL_MASK and state & Gdk.ModifierType.SHIFT_MASK:
            return DropIntent.SHORTCUT
        if state & Gdk.ModifierType.SHIFT_MASK:
            return DropIntent.MOVE
        if state & Gdk.ModifierType.CONTROL_MASK:
            return DropIntent.COPY
        if state & Gdk.ModifierType.ALT_MASK:
            return DropIntent.SHORTCUT
        return DropIntent.MOVE  # default managed drop action

    def _on_drop(self, _target: Gtk.DropTarget, value, x: float, y: float) -> bool:
        if isinstance(value, str):
            # Internal drags deliver the uri list as a raw string.
            uris = [line for line in value.splitlines() if line.startswith("file://")]
        else:
            uris = _uris_from_drop_value(value)
        if not uris:
            _LOG.warning("[DnD] drop value produced no URIs (%s)", type(value).__name__)
            return False
        self.controller.handle_drop(uris, self._drop_intent(), self._index_at(y))
        return True

    def _index_at(self, y: float) -> Optional[int]:
        if self.stack.get_visible_child_name() != ViewMode.ICON:
            return None
        children = list(self._flow_children())
        if not children:
            return 0
        closest, best = None, None
        for index, child in enumerate(children):
            _ox, oy = child.translate_coordinates(self.flow, 0, 0) or (0, 0)
            distance = abs(oy + child.get_height() / 2 - y)
            if best is None or distance < best:
                best, closest = distance, index
        return closest

    def _attach_drag_source(self, widget: Gtk.Widget, entry: FileEntry) -> None:
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        source.connect("prepare", lambda _s, _x, _y: self._drag_content([entry]))
        source.connect("drag-begin", lambda _s, drag: drag.set_hotspot(0, 0))
        widget.add_controller(source)

    def _drag_content(self, entries: List[FileEntry]) -> Optional[Gdk.ContentProvider]:
        selected = self.selected_entries()
        dragged = selected if entries[0] in selected else entries
        text = "".join("file://" + e.path + "\r\n" for e in dragged)
        uri = Gdk.ContentProvider.new_for_value(text)
        plain = Gdk.ContentProvider.new_for_value("\n".join(e.path for e in dragged))
        return Gdk.ContentProvider.new_union([uri, plain])

    # ---- keys ---------------------------------------------------------------------------

    def _setup_keys(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        keys.connect("key-pressed", self._on_key)
        self.stack.add_controller(keys)

    def _on_key(self, _ctrl: Gtk.EventControllerKey, keyval: int, _raw, state) -> bool:
        entries = self.selected_entries()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_Delete:
            if not entries:
                return False
            if shift:
                self._delete_selected()
            else:
                self._trash_selected()
            return True
        if keyval == Gdk.KEY_F2 and entries:
            self._rename_selected()
            return True
        if ctrl and keyval == Gdk.KEY_c and entries:
            self.controller.copy(entries)
            return True
        if ctrl and keyval == Gdk.KEY_x and entries:
            self.controller.cut(entries)
            return True
        if ctrl and keyval == Gdk.KEY_v:
            self._paste()
            return True
        if ctrl and keyval == Gdk.KEY_a:
            self.select_all()
            return True
        return False

    def select_all(self) -> None:
        if self.stack.get_visible_child_name() == ViewMode.LIST:
            row = self.list.get_first_child()
            while row is not None:
                self.list.select_row(row)
                row = row.get_next_sibling()
        else:
            for child in self._flow_children():
                self.flow.select_child(child)

    # ---- operations ------------------------------------------------------------------------

    def refresh(self) -> None:
        self.controller.refresh()
        self.rebuild()

    def _set_view(self, mode: str) -> None:
        self.controller.set_view_mode(mode)
        self.stack.set_visible_child_name(mode)

    def _open_selected(self) -> None:
        for entry in self.selected_entries():
            self.controller.open(entry)

    def _paste(self) -> None:
        clipboard = Gdk.Display.get_default().get_clipboard()
        formats = clipboard.get_formats()
        if hasattr(Gdk, "FileList") and formats.contain_gtype(Gdk.FileList):

            def on_files(_obj, result, _user):
                try:
                    file_list = clipboard.read_value_finish(result)
                    paths = [f.get_path() for f in file_list.get_files()]
                    self.controller.paste([p for p in paths if p])
                except Exception:
                    self.controller.paste(None)

            clipboard.read_value_async(Gdk.FileList, 0, None, on_files, None)
        else:
            self.controller.paste(None)

    def _rename_selected(self) -> None:
        entries = self.selected_entries()
        if len(entries) != 1:
            return
        entry = entries[0]
        holder = None
        for child in self._flow_children():
            if child.entry is entry:  # type: ignore[attr-defined]
                holder = child
                break
        if holder is None:
            for row in self._list_rows():
                if row.entry is entry:  # type: ignore[attr-defined]
                    holder = row
                    break
        if holder is None:
            return
        popover = Gtk.Popover()  # parented before showing (see _open_stack_popover)
        popover.set_parent(holder)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        popover.set_child(box)
        entry_widget = Gtk.Entry(visible=True)
        entry_widget.set_text(self.controller.display_name_for(entry))
        entry_widget.set_width_chars(18)
        box.append(entry_widget)
        apply_btn = Gtk.Button(label=t("Common.Ok"), visible=True)
        box.append(apply_btn)

        def commit(*_a):
            typed = entry_widget.get_text()
            confirm = self.controller.plan_rename(entry, typed)
            if confirm is not None:
                popover.popdown()
                self._show_confirmation(
                    confirm,
                    lambda: self._apply_rename(entry, typed),
                )
                return
            self._apply_rename(entry, typed)
            popover.popdown()

        applyBtnKey = Gtk.EventControllerKey()
        entry_widget.add_controller(applyBtnKey)

        def on_entry_key(_k, keyval: int, _raw, _state) -> bool:
            if keyval == Gdk.KEY_Return:
                commit()
                return True
            if keyval == Gdk.KEY_Escape:
                popover.popdown()
                return True
            return False

        applyBtnKey.connect("key-pressed", on_entry_key)
        apply_btn.connect("clicked", commit)
        self.rename_popover = popover
        popover.connect("closed", lambda *_a: popover.unparent())
        popover.popup()
        entry_widget.grab_focus()

    def _apply_rename(self, entry: FileEntry, typed: str) -> None:
        try:
            self.controller.rename(entry, typed)
        except ValueError as exc:
            self._toast(str(exc))

    def _trash_selected(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        self._show_confirmation(self.controller.plan_delete(entries), lambda: self.controller.trash(entries))

    def _delete_selected(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        self._show_confirmation(
            self.controller.plan_permanent_delete(entries),
            lambda: self.controller.delete_permanently(entries),
        )

    # ---- dialogs --------------------------------------------------------------------------------

    def _show_confirmation(self, confirmation: Confirmation, on_confirm) -> None:
        root = self.get_root()
        dialog = Gtk.MessageDialog(
            transient_for=root if isinstance(root, Gtk.Window) else None,
            modal=True,
            message_type=Gtk.MessageType.QUESTION if not confirmation.danger else Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL,
            text=confirmation.title,
        )
        if confirmation.message:
            dialog.format_secondary_text(confirmation.message)
        dialog.add_button(confirmation.confirm_label, Gtk.ResponseType.OK)
        if confirmation.danger:
            dialog.get_widget_for_response(Gtk.ResponseType.OK).add_css_class("destructive-action")
        dialog.connect(
            "response",
            lambda _d, response: (
                dialog.destroy(),
                on_confirm() if response == Gtk.ResponseType.OK else None,
            ),
        )
        dialog.present()

    def _toast(self, message: str) -> None:
        # Minimal inline feedback; a real toast host arrives with the app shell.
        root = self.get_root()
        if isinstance(root, Gtk.Window):
            title = root.get_title()
            root.set_title(message)
            GLib.timeout_add(1500, lambda: (root.set_title(title), False)[1])


def _current_modifiers() -> Gdk.ModifierType:
    try:
        event = Gtk.get_current_event()
        if event is not None:
            return event.get_modifier_state()
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        _ = seat.get_pointer() if seat is not None else None
        return Gdk.ModifierType(0)
    except Exception:
        return Gdk.ModifierType(0)
