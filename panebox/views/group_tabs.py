"""Group tab strip (port of the WidgetGroupTitleSwitcher presentation).

One row of member tabs under the widget title bar (Tabs style) or a compact
"n / m" position label (Stack style, wheel/Ctrl+Tab driven). The strip owns no
group logic: it reports clicks, hover dwell (80 ms) and wheel gesture steps,
and the widget manager decides what those do.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import i18n
from ..models.widget_group_config import NavigationStyle, TitleDisplayMode
from ..services import group_policy

TAB_HOVER_SWITCH_DELAY_MS = 80  # BeginTabHoverSwitch dwell
DRAG_HOVER_SWITCH_DELAY_MS = 200  # ObserveDragHoverSwitch dwell


class GroupTabStrip(Gtk.Box):
    """Member switcher row hosted by the widget shell."""

    def __init__(
        self,
        on_select: Callable[[str], None],
        on_wheel_step: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, visible=True)
        self.add_css_class("group-tabs")
        self.on_select = on_select
        self.on_wheel_step = on_wheel_step
        self.style = NavigationStyle.TABS
        self.title_display = TitleDisplayMode.ICON_AND_TEXT
        self.hover_enabled = False
        self.members: list[tuple[str, str, str]] = []  # (id, name, glyph)
        self.active_id = ""
        self._hover_source = 0
        self._hover_member_id = ""
        self._wheel = group_policy.WheelGesture()

        self.tabs_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, visible=True)
        self.position_label = Gtk.Label(visible=True)
        self.position_label.add_css_class("group-position")
        self.position_label.set_valign(Gtk.Align.CENTER)
        self.append(self.tabs_box)
        self.append(self.position_label)

        scroll = Gtk.EventControllerScroll()
        scroll.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        self._sync_visibility()

    # ---- presentation ---------------------------------------------------------

    def refresh(
        self,
        members: list[tuple[str, str, str]],
        active_id: str,
        style: str,
        title_display: str,
        hover_enabled: bool,
    ) -> None:
        self.members = list(members)
        self.active_id = active_id
        self.style = style
        self.title_display = title_display
        self.hover_enabled = hover_enabled
        self._rebuild_tabs()
        self._sync_visibility()

    def _sync_visibility(self) -> None:
        tabs_visible = len(self.members) >= 2 and self.style == NavigationStyle.TABS
        self.tabs_box.set_visible(tabs_visible)
        show_position = len(self.members) >= 2 and self.style == NavigationStyle.STACK
        self.position_label.set_visible(show_position)

    def _rebuild_tabs(self) -> None:
        for child in list(self.tabs_box):
            self.tabs_box.remove(child)
        self._cancel_hover()
        for member_id, name, glyph in self.members:
            if self.title_display == TitleDisplayMode.ICON_ONLY:
                label, tooltip = glyph, name
            elif self.title_display == TitleDisplayMode.TEXT_ONLY:
                label, tooltip = name, name
            else:
                label, tooltip = f"{glyph} {name}", name
            tab = Gtk.ToggleButton(label=label, visible=True)
            tab.set_tooltip_text(tooltip)
            tab.set_active(member_id == self.active_id)
            tab.add_css_class("group-tab")
            tab.set_can_focus(False)
            tab.connect("toggled", lambda b, mid=member_id: self._tab_toggled(b, mid))
            hover = Gtk.EventControllerMotion()
            hover.connect("enter", lambda *_a, mid=member_id: self._hover_tab(mid))
            hover.connect("leave", lambda *_a: self._cancel_hover())
            tab.add_controller(hover)
            self.tabs_box.append(tab)
        if self.style == NavigationStyle.STACK and self.members:
            try:
                index = [m[0] for m in self.members].index(self.active_id) + 1
            except ValueError:
                index = 1
            # Compact rail: "i / n" (the localized long form is the a11y
            # description "Widget.Group.Position").
            self.position_label.set_text(f"{index} / {len(self.members)}")
            self.position_label.set_tooltip_text(i18n.fmt("Widget.Group.Position", index, index, len(self.members)))

    # ---- interaction ------------------------------------------------------------

    def _tab_toggled(self, button: Gtk.ToggleButton, member_id: str) -> None:
        if button.get_active():
            self._cancel_hover()
            self.on_select(member_id)
        else:  # keep the active tab pressed; switching re-presses it
            button.set_active(True)

    def _hover_tab(self, member_id: str) -> None:
        self._cancel_hover()
        if not self.hover_enabled or member_id == self.active_id:
            return
        self._hover_member_id = member_id
        self._hover_source = GLib.timeout_add(TAB_HOVER_SWITCH_DELAY_MS, self._hover_fire, member_id)

    def _hover_fire(self, member_id: str) -> bool:
        self._hover_source = 0
        if self.hover_enabled and member_id != self.active_id:
            self.on_select(member_id)
        return GLib.SOURCE_REMOVE

    def _cancel_hover(self) -> None:
        if self._hover_source:
            GLib.source_remove(self._hover_source)
            self._hover_source = 0
        self._hover_member_id = ""

    def _on_scroll(self, _ctrl, dx: float, dy: float) -> bool:
        if self.on_wheel_step is None:
            return False
        delta = dy if dy != 0 else dx
        step = self._wheel.consume(delta, datetime.now(timezone.utc))
        if step:
            self.on_wheel_step(step)
        return step != 0

    def reset_wheel(self) -> None:
        self._wheel.reset()
