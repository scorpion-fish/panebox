"""WidgetManager group mixin (port of WidgetManager.Groups.cs runtime half).

Mapping of the Windows unified-surface-host model onto our one-window-per-
widget architecture: a group's "surface host" is the active member's window.
Non-active members keep their runtimes but their windows stay hidden, and
every member config carries the group's geometry so expansion, restore and
topology code see one coherent surface.
"""

from __future__ import annotations

from typing import Optional

from .. import i18n
from ..models.widget_group_config import NavigationStyle, TitleDisplayMode
from ..services import feature_widgets
from ..services import group_policy
from ..views.group_tabs import GroupTabStrip


class WidgetManagerGroupsMixin:
    """Mixed into WidgetManager; requires self.settings / self.runtimes."""

    # ---- lookups -------------------------------------------------------------

    def group_of(self, widget_id: str):
        return group_policy.find_group_by_member(self.settings.layout, widget_id)

    def is_grouped(self, widget_id: str) -> bool:
        return self.group_of(widget_id) is not None

    def is_surface_window(self, widget_id: str) -> bool:
        """Only the active member of a group (or any ungrouped widget) owns a
        visible desktop window."""
        group = self.group_of(widget_id)
        return group is None or group.activeMemberId == widget_id

    def _member_config(self, widget_id: str):
        return next((w for w in self.settings.layout.widgets if w.id == widget_id), None)

    def _member_runtime(self, widget_id: str):
        return self.runtimes.get(widget_id)

    def _group_members(self, group) -> list:
        configs = []
        for member_id in group.memberIds:
            config = self._member_config(member_id)
            if config is not None:
                configs.append(config)
        return configs

    # ---- presentation ----------------------------------------------------------

    def _member_glyph(self, kind: str) -> str:
        if feature_widgets.is_feature_widget(kind):
            return feature_widgets.descriptor_for(kind)["glyph"]
        return "▤"

    def group_presentation(self, widget_id: str) -> Optional[dict]:
        group = self.group_of(widget_id)
        if group is None:
            return None
        layout = self.settings.layout
        members = [
            (
                config.id,
                group_policy.member_display_name(config, i18n.t),
                self._member_glyph(config.widgetKind),
            )
            for config in self._group_members(group)
        ]
        return {
            "members": members,
            "active_id": group.activeMemberId,
            "style": group.resolved_navigation_style(layout.widgetGroupDefaultNavigationStyle),
            "title_display": group.titleDisplayMode
            if group.titleDisplayMode != TitleDisplayMode.FOLLOW_DEFAULT
            else layout.widgetGroupDefaultTitleDisplayMode,
            "wheel_enabled": (
                layout.widgetGroupWheelSwitchEnabled if group.wheelSwitchEnabled is None else group.wheelSwitchEnabled
            ),
            "hover_enabled": (
                layout.widgetGroupHoverSwitchEnabled if group.hoverSwitchEnabled is None else group.hoverSwitchEnabled
            ),
        }

    def _refresh_group_strips(self) -> None:
        for runtime in self.runtimes.values():
            group = self.group_of(runtime.config.id)
            if group is None:
                if runtime.shell.group_strip_slot.get_first_child() is not None:
                    runtime.shell.set_group_strip(None)
                # Always on: the "…" menu carries the PaneBox app entries even
                # for ungrouped widgets (the tray fallback on GNOME/Wayland).
                runtime.shell.set_menu_available(True)
                continue
            runtime.shell.set_menu_available(True)
            is_active = group.activeMemberId == runtime.config.id
            if not is_active:
                runtime.shell.set_group_strip(None)
                continue
            presentation = self.group_presentation(runtime.config.id)
            strip = getattr(runtime, "group_strip", None)
            if strip is None:
                strip = GroupTabStrip(
                    on_select=lambda mid: self.switch_group_member(mid, origin="Tab"),
                    on_wheel_step=lambda step, rt=runtime: self._group_wheel_step(rt, step),
                )
                runtime.group_strip = strip
            strip.refresh(
                presentation["members"],
                presentation["active_id"],
                presentation["style"],
                presentation["title_display"],
                presentation["hover_enabled"],
            )
            if strip.get_parent() is not runtime.shell.group_strip_slot:
                runtime.shell.set_group_strip(strip)

    def _group_wheel_step(self, runtime, step: int) -> None:
        group = self.group_of(runtime.config.id)
        if group is None:
            return
        presentation = self.group_presentation(runtime.config.id)
        if not presentation or not presentation["wheel_enabled"]:
            return
        self.switch_group_relative(runtime.config.id, step, wrap=True, origin="Wheel")

    def _group_key_pressed(self, config, keyval, state) -> bool:
        """Ctrl+Tab / Ctrl+Shift+Tab cycle group members (keyboard wrap)."""
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk

        if keyval not in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab, Gdk.KEY_ISO_Left_Tab):
            return False
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        group = self.group_of(config.id)
        if group is None or len(group.memberIds) < 2:
            return False
        delta = -1 if (state & Gdk.ModifierType.SHIFT_MASK) else 1
        return self.switch_group_relative(config.id, delta, wrap=True, origin="Keyboard")

    # ---- join targets ------------------------------------------------------------

    def join_targets(self, source_widget_id: str) -> list[dict]:
        """GetWidgetGroupJoinTargets — eligible merge destinations."""
        layout = self.settings.layout
        source_config = self._member_config(source_widget_id)
        if source_config is None or not layout.widgetGroupsEnabled:
            return []
        source_group = self.group_of(source_widget_id)
        source_count = len(source_group.memberIds) if source_group else 1
        seen_groups: set = set()
        targets: list[dict] = []
        for config in layout.widgets:
            if config.id == source_widget_id or config.isDisabled:
                continue
            target_group = self.group_of(config.id)
            if target_group is not None:
                if (source_group is not None and source_group.id == target_group.id) or target_group.id in seen_groups:
                    continue
                if len(target_group.memberIds) + source_count > group_policy.MAXIMUM_MEMBER_COUNT:
                    continue
                seen_groups.add(target_group.id)
                active = self._member_config(target_group.activeMemberId) or config
                count = len(target_group.memberIds)
                targets.append(
                    {
                        "target_id": target_group.activeMemberId,
                        "display_name": group_policy.member_display_name(active, i18n.t),
                        "member_count": count,
                        "can_join": True,
                    }
                )
                continue
            if not group_policy.is_active_member(layout, config.id):
                continue
            if source_count + 1 > group_policy.MAXIMUM_MEMBER_COUNT:
                continue
            targets.append(
                {
                    "target_id": config.id,
                    "display_name": group_policy.member_display_name(config, i18n.t),
                    "member_count": 1,
                    "can_join": True,
                }
            )
        targets.sort(key=lambda t: t["display_name"].lower())
        return targets

    # ---- merge / switch / detach / dissolve -----------------------------------------

    def merge_widgets(self, source_widget_id: str, target_widget_id: str) -> bool:
        """MergeWidgetsAsync — the destination group keeps its identity and
        active member; source members append in their stored order."""
        layout = self.settings.layout
        if not layout.widgetGroupsEnabled:
            return False
        if not source_widget_id or not target_widget_id or source_widget_id == target_widget_id:
            return False
        source_config = self._member_config(source_widget_id)
        target_config = self._member_config(target_widget_id)
        if source_config is None or target_config is None:
            return False
        source_group = self.group_of(source_widget_id)
        target_group = self.group_of(target_widget_id)
        if source_group is not None and target_group is not None and source_group.id == target_group.id:
            return False

        source_members = list(source_group.memberIds) if source_group else [source_widget_id]
        target_members = list(target_group.memberIds) if target_group else [target_widget_id]
        combined = target_members + [m for m in source_members if m not in target_members]
        if len(combined) > group_policy.MAXIMUM_MEMBER_COUNT:
            return False

        active_target_id = target_group.activeMemberId if target_group else target_widget_id
        active_config = self._member_config(active_target_id) or target_config
        merged = target_group or group_policy.create_group_from_target(active_config)
        if target_group is not None:
            runtime = self._member_runtime(active_target_id)
            if runtime is not None:
                x, y, w, h = runtime.window.window_geometry()
                active_config.x, active_config.y = float(x), float(y)
                active_config.width, active_config.height = float(w), float(h)
                group_policy.capture_group_layout(merged, active_config)

        merged.memberIds = combined
        merged.activeMemberId = active_target_id
        if source_group is not None:
            layout.widgetGroups.remove(source_group)
        if target_group is None:
            layout.widgetGroups.append(merged)
        group_policy.apply_group_layout_to_member(merged, target_config)
        for member_config in self._group_members(merged):
            group_policy.apply_group_layout_to_member(merged, member_config)

        # One visible window: the active member keeps (or takes) the surface.
        active_runtime = self._member_runtime(active_target_id)
        if active_runtime is None:
            active_runtime = self.create_runtime(
                active_config,
                merged.x,
                merged.y,
                merged.width,
                merged.height,
                present=merged.isVisible,
            )
        else:
            active_runtime.window.set_window_geometry(
                int(merged.x), int(merged.y), int(merged.width), int(merged.height)
            )
            if merged.isVisible and not active_runtime.window.get_visible():
                active_runtime.window.present()
        for member_id in combined:
            if member_id == active_target_id:
                continue
            runtime = self._member_runtime(member_id)
            if runtime is not None:
                runtime.window.hide()
        self.settings.save_debounced()
        self._refresh_group_strips()
        self._notify_widgets_changed()
        return True

    def switch_group_member(self, target_widget_id: str, origin: str = "Programmatic") -> bool:
        """Show the target member's window as the group surface."""
        group = self.group_of(target_widget_id)
        if group is None:
            return False
        if group.activeMemberId == target_widget_id:
            return True
        target_config = self._member_config(target_widget_id)
        if target_config is None:
            return False
        previous = self._member_runtime(group.activeMemberId)
        if previous is not None:
            x, y, w, h = previous.window.window_geometry()
            group.x, group.y, group.width, group.height = float(x), float(y), float(w), float(h)
            previous.window.hide()
        group_policy.apply_group_layout_to_member(group, target_config)
        runtime = self._member_runtime(target_widget_id)
        if runtime is None:
            runtime = self.create_runtime(
                target_config,
                group.x,
                group.y,
                group.width,
                group.height,
                present=group.isVisible,
            )
        else:
            runtime.window.set_window_geometry(int(group.x), int(group.y), int(group.width), int(group.height))
            if group.isVisible:
                runtime.window.present()
        group.activeMemberId = target_widget_id
        self.settings.save_debounced()
        self._refresh_group_strips()
        return True

    def switch_group_relative(self, widget_id: str, delta: int, wrap: bool = True, origin: str = "Keyboard") -> bool:
        group = self.group_of(widget_id)
        if group is None:
            return False
        try:
            active_index = group.memberIds.index(group.activeMemberId)
        except ValueError:
            active_index = 0
        target = group_policy.try_resolve_relative_target(active_index, len(group.memberIds), delta, wrap=wrap)
        if target is None:
            return False
        return self.switch_group_member(group.memberIds[target], origin=origin)

    def remove_widget_from_group(self, widget_id: str, reveal_standalone: bool = True, detached_position=None) -> bool:
        layout = self.settings.layout
        group = self.group_of(widget_id)
        removed_config = self._member_config(widget_id)
        if group is None or removed_config is None:
            return False

        removed_index = group.memberIds.index(widget_id)
        removed_was_active = group.activeMemberId == widget_id
        group.memberIds.remove(widget_id)
        group_policy.place_detached_member(removed_config, group, removed_index + 1, detached_position)
        removed_config.isVisible = reveal_standalone and group.isVisible

        surviving = group if len(group.memberIds) >= 2 else None
        if surviving is None:
            layout.widgetGroups.remove(group)
            if group.memberIds:
                remaining = self._member_config(group.memberIds[0])
                if remaining is not None:
                    group_policy.apply_group_layout_to_member(group, remaining)
                    remaining.isVisible = group.isVisible
        else:
            if removed_was_active:
                next_index = max(0, min(removed_index, len(group.memberIds) - 1))
                group.activeMemberId = group.memberIds[next_index]
            for member_config in self._group_members(group):
                group_policy.apply_group_layout_to_member(group, member_config)

        # Windows: show the detached member and the (possibly new) active one.
        detached_runtime = self._member_runtime(widget_id)
        if detached_runtime is None and removed_config.isVisible:
            detached_runtime = self.create_runtime(
                removed_config,
                removed_config.x,
                removed_config.y,
                removed_config.width,
                removed_config.height,
                present=True,
            )
        elif detached_runtime is not None:
            detached_runtime.window.set_window_geometry(
                int(removed_config.x),
                int(removed_config.y),
                int(removed_config.width),
                int(removed_config.height),
            )
            if removed_config.isVisible:
                detached_runtime.window.present()
            else:
                detached_runtime.window.hide()
        if surviving is not None:
            active = self._member_runtime(surviving.activeMemberId)
            if active is not None and surviving.isVisible:
                active.window.set_window_geometry(
                    int(surviving.x),
                    int(surviving.y),
                    int(surviving.width),
                    int(surviving.height),
                )
                active.window.present()
            for member_id in surviving.memberIds:
                if member_id == surviving.activeMemberId:
                    continue
                member_runtime = self._member_runtime(member_id)
                if member_runtime is not None:
                    member_runtime.window.hide()
        self.settings.save_debounced()
        self._refresh_group_strips()
        self._notify_widgets_changed()
        return True

    def dissolve_group_containing(self, widget_id: str) -> bool:
        """DissolveWidgetGroupContainingAsync — every member returns to its own
        window, cascading out from the group's top-left corner."""
        layout = self.settings.layout
        group = self.group_of(widget_id)
        if group is None:
            return False
        members = self._group_members(group)
        for index, member in enumerate(members):
            group_policy.apply_group_layout_to_member(group, member)
            if index > 0:
                group_policy.place_detached_member(member, group, index)
            member.isVisible = group.isVisible
        layout.widgetGroups.remove(group)
        for member in members:
            runtime = self._member_runtime(member.id)
            if runtime is None:
                if member.isVisible:
                    self.create_runtime(member, member.x, member.y, member.width, member.height, present=True)
                continue
            runtime.window.set_window_geometry(int(member.x), int(member.y), int(member.width), int(member.height))
            if member.isVisible:
                runtime.window.present()
            else:
                runtime.window.hide()
        self.settings.save_debounced()
        self._refresh_group_strips()
        self._notify_widgets_changed()
        return True

    def dissolve_all_groups(self) -> bool:
        any_dissolved = False
        for group in list(self.settings.layout.widgetGroups):
            if group.memberIds and self.dissolve_group_containing(group.memberIds[0]):
                any_dissolved = True
        return any_dissolved

    def reorder_group_member(self, source_widget_id: str, target_widget_id: str) -> bool:
        group = self.group_of(source_widget_id)
        if group is None or target_widget_id not in group.memberIds:
            return False
        if not group_policy.move_to_target_slot(group.memberIds, source_widget_id, target_widget_id):
            return False
        self.settings.save_debounced()
        self._refresh_group_strips()
        return True

    # ---- per-group settings setters -----------------------------------------------

    def _set_group_field(self, widget_id: str, field: str, value) -> bool:
        group = self.group_of(widget_id)
        if group is None:
            return False
        setattr(group, field, value)
        if field == "navigationStyle":
            # Tabs groups pin the wheel off at the style level (C# Normalize).
            layout = self.settings.layout
            style = group.resolved_navigation_style(layout.widgetGroupDefaultNavigationStyle)
            if group.wheelSwitchEnabled is None and style == NavigationStyle.TABS:
                group.wheelSwitchEnabled = False
        self.settings.save_debounced()
        self._refresh_group_strips()
        return True

    def set_group_navigation_style(self, widget_id: str, style: str) -> bool:
        return self._set_group_field(widget_id, "navigationStyle", style)

    def set_group_title_display_mode(self, widget_id: str, mode: str) -> bool:
        return self._set_group_field(widget_id, "titleDisplayMode", mode)

    def set_group_wheel_switch_enabled(self, widget_id: str, value: Optional[bool]) -> bool:
        return self._set_group_field(widget_id, "wheelSwitchEnabled", value)

    def set_group_hover_switch_enabled(self, widget_id: str, value: Optional[bool]) -> bool:
        return self._set_group_field(widget_id, "hoverSwitchEnabled", value)

    # ---- restore / teardown integration ---------------------------------------------

    def normalize_groups_for_restore(self) -> None:
        """NormalizeWidgetGroupsForRuntime (simplified for our runtime model):
        repair persisted state, then resolve a restorable active member."""
        layout = self.settings.layout
        group_policy.normalize_groups(layout)
        for group in list(layout.widgetGroups):
            active = group_policy.resolve_restorable_active_member_id(
                layout, group, lambda config: self._restorable(config)
            )
            if active is not None and group.activeMemberId != active:
                group.activeMemberId = active
            elif not group.memberIds or (group.activeMemberId not in group.memberIds and group.memberIds):
                group.activeMemberId = group.memberIds[0] if group.memberIds else ""
            for member_config in self._group_members(group):
                group_policy.apply_group_layout_to_member(group, member_config)

    def _teardown_group_membership(self, widget_id: str) -> None:
        """close_widget hook: a deleted member leaves its group; a group under
        two members dissolves its record."""
        group = self.group_of(widget_id)
        if group is None:
            return
        group.memberIds.remove(widget_id)
        if len(group.memberIds) < 2:
            self.settings.layout.widgetGroups.remove(group)
            if group.memberIds:
                remaining = self._member_config(group.memberIds[0])
                if remaining is not None:
                    group_policy.apply_group_layout_to_member(group, remaining)
                    runtime = self._member_runtime(remaining.id)
                    if runtime is not None and remaining.isVisible:
                        runtime.window.present()
        else:
            if group.activeMemberId == widget_id:
                group.activeMemberId = group.memberIds[0]
            active = self._member_runtime(group.activeMemberId)
            if active is not None and group.isVisible:
                active.window.present()
        self._refresh_group_strips()

    def _sync_group_geometry_from(self, runtime) -> None:
        """Geometry commit on an active grouped member updates the group and
        every member config (SynchronizeGroupLayoutFromMember)."""
        group = self.group_of(runtime.config.id)
        if group is None or group.activeMemberId != runtime.config.id:
            return
        x, y, w, h = runtime.window.window_geometry()
        runtime.config.x, runtime.config.y = float(x), float(y)
        runtime.config.width, runtime.config.height = float(w), float(h)
        group_policy.capture_group_layout(group, runtime.config)
        for member_config in self._group_members(group):
            if member_config is runtime.config:
                continue
            group_policy.apply_group_layout_to_member(group, member_config)

    # ---- widget menu ("…" button, port of WidgetGroupMenuBuilder) --------------------

    def open_widget_menu(self, widget_id: str, relative_to) -> None:
        """ "…" popover: PaneBox app entries (always present — on GNOME/Wayland
        there is no tray icon, so this is the primary entry point) plus the
        group entries when the widget is grouped or has join targets."""
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gio, GLib, Gtk

        group = self.group_of(widget_id)
        targets = self.join_targets(widget_id)
        has_entries = group is not None or bool(targets)
        runtime = self._member_runtime(widget_id)
        if runtime is not None:
            runtime.shell.set_menu_available(True)

        menu = Gio.Menu.new()
        action_group = Gio.SimpleActionGroup.new()

        def add_action(name: str, callback):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            action_group.add_action(action)

        # Per-widget section first: rename is about THIS window (the title
        # bar is also double-click-to-edit, but that alone is undiscoverable).
        if runtime is not None:
            widget_section = Gio.Menu.new()
            widget_section.append(i18n.t("Common.Rename"), "wg.rename")

            def start_rename(rt=runtime) -> bool:
                rt.shell.begin_title_edit()
                return False

            # Let the popover finish unmapping first — its focus restore on
            # close would immediately trigger the entry's focus-leave commit.
            add_action("rename", lambda: GLib.timeout_add(150, start_rename))
            menu.append_section(runtime.config.name, widget_section)

        app_section = Gio.Menu.new()
        app_section.append(i18n.t("Common.NewWidget"), "wg.app-new-widget")
        app_section.append(i18n.t("Common.NewFolderMapping"), "wg.app-map-folder")
        app_section.append(i18n.t("Common.AddFeatureWidget"), "wg.app-add-feature")
        app_section.append(i18n.t("Tray.OpenManagedStorage"), "wg.app-open-storage")
        app_section.append(i18n.t("Tray.Settings"), "wg.app-settings")
        add_action("app-new-widget", lambda: self._app_entry("new-widget"))
        add_action("app-map-folder", lambda: self._app_entry("map-folder"))
        add_action("app-add-feature", lambda: self._app_entry("add-feature"))
        add_action("app-open-storage", lambda: self._app_entry("open-storage"))
        add_action("app-settings", lambda: self._app_entry("settings"))
        menu.append_section("PaneBox", app_section)
        if not has_entries:
            popover = Gtk.PopoverMenu.new_from_model(menu)
            popover.set_halign(Gtk.Align.START)
            relative_to.insert_action_group("wg", action_group)
            popover.set_parent(relative_to)
            popover.popup()
            return

        def add_action(name: str, callback):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            action_group.add_action(action)

        if targets:
            join_section = Gio.Menu.new()
            for index, target in enumerate(targets):
                label = (
                    i18n.fmt(
                        "Widget.Group.TargetWithCount",
                        target["display_name"],
                        target["member_count"],
                    )
                    if target["member_count"] > 1
                    else target["display_name"]
                )
                join_section.append(label, f"wg.join{index}")
                add_action(
                    f"join{index}",
                    lambda t=target["target_id"]: self.merge_widgets(widget_id, t),
                )
            menu.append_section(i18n.t("Widget.Group.Join"), join_section)

        if group is not None:
            control = Gio.Menu.new()
            nav = Gio.Menu.new()
            group.resolved_navigation_style(self.settings.layout.widgetGroupDefaultNavigationStyle)
            nav.append(
                i18n.fmt(
                    "Settings.WidgetGroups.FollowDefaultWithValue",
                    i18n.t("Widget.Group.Navigation.Tabs"),
                ),
                "wg.nav-follow",
            )
            nav.append(i18n.t("Widget.Group.Navigation.Tabs"), "wg.nav-tabs")
            nav.append(i18n.t("Widget.Group.Navigation.Stack"), "wg.nav-stack")
            control.append_submenu(i18n.t("Widget.Group.NavigationStyle"), nav)
            add_action(
                "nav-follow",
                lambda: self.set_group_navigation_style(widget_id, NavigationStyle.FOLLOW_DEFAULT),
            )
            add_action("nav-tabs", lambda: self.set_group_navigation_style(widget_id, NavigationStyle.TABS))
            add_action(
                "nav-stack",
                lambda: self.set_group_navigation_style(widget_id, NavigationStyle.STACK),
            )

            title = Gio.Menu.new()
            title.append(i18n.t("Widget.Group.TitleDisplay.IconAndText"), "wg.title-icon-text")
            title.append(i18n.t("Widget.Group.TitleDisplay.IconOnly"), "wg.title-icon")
            title.append(i18n.t("Widget.Group.TitleDisplay.TextOnly"), "wg.title-text")
            control.append_submenu(i18n.t("Widget.Group.TitleDisplayMode"), title)
            add_action(
                "title-icon-text",
                lambda: self.set_group_title_display_mode(widget_id, TitleDisplayMode.ICON_AND_TEXT),
            )
            add_action(
                "title-icon",
                lambda: self.set_group_title_display_mode(widget_id, TitleDisplayMode.ICON_ONLY),
            )
            add_action(
                "title-text",
                lambda: self.set_group_title_display_mode(widget_id, TitleDisplayMode.TEXT_ONLY),
            )

            wheel = Gio.Menu.new()
            wheel.append(i18n.t("Common.On"), "wg.wheel-on")
            wheel.append(i18n.t("Common.Off"), "wg.wheel-off")
            control.append_submenu(i18n.t("Widget.Group.WheelSwitch"), wheel)
            add_action("wheel-on", lambda: self.set_group_wheel_switch_enabled(widget_id, True))
            add_action("wheel-off", lambda: self.set_group_wheel_switch_enabled(widget_id, False))

            hover = Gio.Menu.new()
            hover.append(i18n.t("Common.On"), "wg.hover-on")
            hover.append(i18n.t("Common.Off"), "wg.hover-off")
            control.append_submenu(i18n.t("Widget.Group.HoverSwitch"), hover)
            add_action("hover-on", lambda: self.set_group_hover_switch_enabled(widget_id, True))
            add_action("hover-off", lambda: self.set_group_hover_switch_enabled(widget_id, False))

            control.append(i18n.t("Widget.Group.Dissolve"), "wg.dissolve")
            add_action("dissolve", lambda: self._confirm_dissolve(widget_id))
            control.append(i18n.t("Widget.Group.RemoveCurrent"), "wg.remove")
            add_action("remove", lambda: self.remove_widget_from_group(widget_id, reveal_standalone=True))
            menu.append_section(i18n.t("Widget.Group.Control"), control)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_halign(Gtk.Align.START)
        relative_to.insert_action_group("wg", action_group)
        popover.set_parent(relative_to)
        popover.popup()

    def _app_entry(self, name: str) -> None:
        """Route a "…" menu entry to the application's tray-equivalent handler."""
        import sys

        if self.application is None:
            return
        handler = {
            "new-widget": "_tray_new_widget",
            "map-folder": "_tray_map_folder",
            "add-feature": "_tray_add_feature",
            "open-storage": "_tray_open_storage",
            "settings": "open_settings",
        }.get(name)
        method = getattr(self.application, handler, None) if handler else None
        if method is None:
            return
        try:
            method()
        except Exception as exc:
            print(f"[WidgetMenu] {name} failed: {exc}", file=sys.stderr, flush=True)

    def _confirm_dissolve(self, widget_id: str) -> None:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        runtime = self._member_runtime(widget_id)
        parent = runtime.window if runtime is not None else None
        dialog = Gtk.MessageDialog(
            transient_for=parent,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=i18n.t("Settings.WidgetGroups.DissolveDialog.Title"),
        )
        dialog.format_secondary_text(i18n.t("Settings.WidgetGroups.DissolveDialog.Description"))
        dialog.add_button(i18n.t("Common.Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(i18n.t("Settings.WidgetGroups.DissolveDialog.Confirm"), Gtk.ResponseType.OK)
        dialog.present()
        dialog.connect(
            "response",
            lambda _d, response: (
                self.dissolve_group_containing(widget_id) if response == Gtk.ResponseType.OK else None,
                _d.destroy(),
            ),
        )
