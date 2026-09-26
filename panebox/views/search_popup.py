"""Search popup window (port of Views/SearchPopupWindow.xaml(.cs)).

Borderless 680×500 (min 400×300) overlay centered on the primary work area at
25% height, driven by SearchPopupViewModel. Single instance per application;
hiding keeps the window alive (C# hides too — the popup is recycled).

Linux divergences (README): the WinUI show/hide composition animations
(167ms/4px, 83ms/−2px) collapse to an instant map/unmap; multi-select batch
operations are not ported (single selection + context menu); custom popup
bounds settings are absent from the settings slice and not ported.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from ..app_log import get_logger
from ..i18n import t
from ..models.search_models import SearchResultItem, SearchResultKind, SearchTabItem
from ..services import search_actions
from ..services.search_ranker import get_identity_key
from ..services.search_view_model import (
    INPUT_DEBOUNCE_MS,
    SearchPopupViewModel,
)

_LOG = get_logger("search")

DEFAULT_WIDTH = 680
DEFAULT_HEIGHT = 500
MIN_WIDTH = 400
MIN_HEIGHT = 300
WORK_AREA_HEIGHT_FRACTION = 0.25

INFINITE_SCROLL_MIN_REMAINING = 320
INFINITE_SCROLL_VIEWPORT_FACTOR = 0.75

SORT_COLUMNS = ("Relevance", "Name", "Size", "Date", "Type")
FILTER_VALUES = ("All", "FilesAndFolders", "Apps", "Images", "Documents", "PaneBox")

RECENT_SEARCH_LIMIT = 8


def format_size(size: Optional[int]) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return ""


def format_date(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else ""


class SearchPopupWindow(Gtk.Window):
    """Singleton search overlay: open_search() presents, hide_search() recycles."""

    def __init__(
        self,
        settings_service,
        engine,
        history_service,
        hooks: Optional[dict] = None,
        application=None,
        todo_store_factory=None,
        quick_capture_service=None,
    ):
        super().__init__(decorated=False, resizable=True)
        self.set_name("search-popup")
        self.set_title(t("Search.Title"))
        self.set_size_request(MIN_WIDTH, MIN_HEIGHT)
        self.set_default_size(DEFAULT_WIDTH, DEFAULT_HEIGHT)
        if application is not None:
            application.add_window(self)

        self._hooks = hooks or {}
        self._debounce_source = 0
        self._suppress_selection_sync = False
        # Row context-menu services (attach to todo / save to note).
        from ..services.quick_capture_service import QuickCaptureService
        from ..services.quick_capture_store import QuickCaptureStore
        from ..services.todo_store import TodoWidgetStore

        self._row_hooks = dict(self._hooks)
        self._row_hooks.setdefault("settings_service", settings_service)
        self._row_hooks.setdefault("todo_store_factory", todo_store_factory or TodoWidgetStore)
        self._row_hooks.setdefault(
            "quick_capture_service",
            quick_capture_service or QuickCaptureService(store=QuickCaptureStore()),
        )

        self.vm = SearchPopupViewModel(
            engine=engine,
            settings_service=settings_service,
            history_service=history_service,
            open_file=search_actions.open_result,
            reveal_file=search_actions.reveal_result,
        )
        self.vm.on_state_changed = self._on_state_changed
        self.vm.on_hide_requested = self.hide_search
        self.vm.on_action_requested = self._on_action_requested
        self.vm.on_content_requested = self._on_content_requested
        self.vm.on_query_applied = self._set_entry_text

        self._build_ui()

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)
        self.connect("map", self._on_mapped)

    # ---- UI construction --------------------------------------------------------

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_css_classes(["search-popup-root"])
        self.set_child(root)

        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        entry_row.set_css_classes(["search-entry-row"])
        glyph = Gtk.Label(label="⌕")
        glyph.set_css_classes(["search-glyph"])
        self._entry = Gtk.Entry()
        self._entry.set_placeholder_text(t("Search.Placeholder"))
        self._entry.set_hexpand(True)
        self._entry.set_css_classes(["search-entry"])
        self._entry.connect("changed", self._on_entry_changed)
        self._entry.connect("activate", self._on_entry_activate)
        entry_row.append(glyph)
        entry_row.append(self._entry)
        root.append(entry_row)

        self._tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._tab_box.set_css_classes(["search-tab-row"])
        root.append(self._tab_box)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header.set_css_classes(["search-sort-row"])
        self._status_label = Gtk.Label(label="")
        self._status_label.set_hexpand(True)
        self._status_label.set_xalign(0)
        self._status_label.set_css_classes(["search-status", "dim-label"])
        header.append(self._status_label)
        self._sort_buttons: dict[str, tuple[Gtk.ToggleButton, object]] = {}
        for column in SORT_COLUMNS:
            button = Gtk.ToggleButton()
            button.set_label("★" if column == "Relevance" else t(f"Search.Sort.{column}"))
            button.set_css_classes(["flat", "search-sort-button"])
            handler = self._make_sort_handler(column)
            handler_id = button.connect("toggled", handler)
            self._sort_buttons[column] = (button, handler_id)
            header.append(button)
        root.append(header)

        self._filter_drop = Gtk.DropDown.new_from_strings(
            [
                t("Search.Filter.All"),
                t("Search.Filter.Files"),
                t("Search.Filter.Apps"),
                t("Search.Filter.Images"),
                t("Search.Filter.Documents"),
                t("Search.Filter.PaneBox"),
            ]
        )
        self._filter_drop.connect("notify::selected", self._on_filter_changed)

        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        filter_row.set_css_classes(["search-filter-row"])
        filter_row.append(Gtk.Label(label=t("Search.Filter.Label")))
        filter_row.append(self._filter_drop)
        root.append(filter_row)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._stack.set_vexpand(True)
        root.append(self._stack)

        # Query results: scrolling ListBox.
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.set_css_classes(["search-results"])
        self._list_box.connect("row-activated", self._on_row_activated)
        self._list_box.connect("selected-rows-changed", self._on_selected_row_changed)
        scrolled.set_child(self._list_box)
        self._vadjust = scrolled.get_vadjustment()
        self._vadjust.connect("value-changed", self._on_scroll)
        self._stack.add_named(scrolled, "results")

        self._stack.add_named(self._build_empty_state(), "empty")

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_css_classes(["search-footer"])
        self._history_toggle = Gtk.ToggleButton()
        self._history_toggle.set_label(t("Search.HistoryToggle.On"))
        self._history_toggle.set_tooltip_text(t("Search.HistoryToggle.Tooltip"))
        self._history_toggle.set_css_classes(["flat", "search-history-toggle"])
        self._history_toggle.set_active(self.vm.settings.settings.search.searchSaveHistory)
        self._history_toggle.connect("toggled", self._on_history_toggled)
        clear_button = Gtk.Button(label=t("Search.Section.ClearHistory"))
        clear_button.set_css_classes(["flat"])
        clear_button.connect("clicked", lambda *_a: self.vm.clear_all_history())
        self._footer_status = Gtk.Label(label="")
        self._footer_status.set_hexpand(True)
        self._footer_status.set_xalign(1)
        self._footer_status.set_css_classes(["dim-label"])
        footer.append(self._history_toggle)
        footer.append(clear_button)
        footer.append(self._footer_status)
        root.append(footer)

    def _build_empty_state(self) -> Gtk.Widget:
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        empty.set_css_classes(["search-empty"])

        self._no_results_title = Gtk.Label(label=t("Search.NoResults.Title"))
        self._no_results_title.set_css_classes(["search-empty-title"])
        self._no_results_subtitle = Gtk.Label(label=t("Search.NoResults.Subtitle"))
        self._no_results_subtitle.set_css_classes(["dim-label"])

        self._apps_flow = Gtk.FlowBox()
        self._apps_flow.set_css_classes(["search-apps-grid"])
        self._apps_flow.set_min_children_per_line(1)
        self._apps_flow.set_max_children_per_line(12)
        self._apps_flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._apps_flow.set_homogeneous(True)
        self._apps_flow.connect("child-activated", self._on_app_activated)
        apps_scroll = Gtk.ScrolledWindow()
        apps_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        apps_scroll.set_vexpand(True)
        apps_scroll.set_child(self._apps_flow)
        self._apps_section = self._section(t("Search.Section.RecommendedApps"), apps_scroll)

        self._recently_box = Gtk.ListBox()
        self._recently_box.set_css_classes(["search-results"])
        self._recently_box.connect("row-activated", self._on_recently_activated)
        self._recently_section = self._section(t("Search.Section.RecentlyUsed"), self._recently_box)

        self._recent_chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._recent_section = self._section(t("Search.Section.RecentSearches"), self._recent_chips)

        self._favorite_chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._favorites_section = self._section(t("Search.Section.Favorites"), self._favorite_chips)

        self._empty_hint = Gtk.Label(label=t("Search.History.EmptyText"))
        self._empty_hint.set_css_classes(["dim-label"])

        empty.append(self._no_results_title)
        empty.append(self._no_results_subtitle)
        empty.append(self._apps_section)
        empty.append(self._recently_section)
        empty.append(self._recent_section)
        empty.append(self._favorites_section)
        empty.append(self._empty_hint)
        return empty

    def _section(self, title: str, content: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(label=title)
        label.set_xalign(0)
        label.set_css_classes(["search-section-title"])
        box.append(label)
        box.append(content)
        return box

    # ---- presentation --------------------------------------------------------

    def open_search(self, initial_query: Optional[str] = None) -> None:
        self.present()
        self.vm.popup_opened(initial_query)
        self._set_entry_text(initial_query or "")
        self._sync_from_vm()
        self._entry.grab_focus()

    def hide_search(self) -> None:
        if self._debounce_source:
            GLib.source_remove(self._debounce_source)
            self._debounce_source = 0
        self.vm.popup_hidden()
        self.set_visible(False)

    def _on_mapped(self, *_args) -> None:
        # Center horizontally over the primary work area, top + 25% (C# placement).
        def place() -> bool:
            try:
                from ..platform import x11
                from ..platform.workarea import list_monitors, primary_monitor

                surface = self.get_surface()
                xid = surface.get_xid() if surface is not None else 0
                if not xid:
                    return False
                monitor = primary_monitor(list_monitors())
                width, height = self.get_width(), self.get_height()
                x = monitor.work_x + (monitor.work_width - width) // 2
                y = monitor.work_y + int((monitor.work_height - height) * WORK_AREA_HEIGHT_FRACTION)
                x11.shared_connection().move(xid, x, y)
            except Exception:
                pass  # headless / non-X11: GTK default placement
            return False

        GLib.idle_add(place)

    # ---- entry + debounce ------------------------------------------------------

    def _set_entry_text(self, text: str) -> None:
        self._entry.set_text(text)
        self._entry.set_position(-1)

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        if self._debounce_source:
            GLib.source_remove(self._debounce_source)
            self._debounce_source = 0
        text = entry.get_text()
        self._debounce_source = GLib.timeout_add(INPUT_DEBOUNCE_MS, self._fire_debounced, text)

    def _fire_debounced(self, text: str) -> bool:
        self._debounce_source = 0
        self.vm.set_query(text)
        return False

    def _on_entry_activate(self, _entry) -> None:
        self.vm.execute_selected_item()

    # ---- keyboard --------------------------------------------------------------

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            self._handle_escape()
            return True
        if control and keyval == Gdk.KEY_Tab:
            self.vm.cycle_tab(backward=bool(state & Gdk.ModifierType.SHIFT_MASK))
            return True
        if keyval == Gdk.KEY_Down:
            self.vm.move_selection_down()
            return True
        if keyval == Gdk.KEY_Up:
            self.vm.move_selection_up()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if control:
                self.vm.open_selected_location()
            else:
                self.vm.execute_selected_item()
            return True
        return False

    def _handle_escape(self) -> None:
        """Esc: text → clear query; apps selection → clear; else hide (C# order)."""
        if self._entry.get_text():
            self._set_entry_text("")
            self.vm.set_query("")
            return
        selected = self._apps_flow.get_selected_children()
        if self._stack.get_visible_child_name() == "empty" and selected:
            self._apps_flow.unselect_child(selected[0])
            return
        self.hide_search()

    # ---- tabs / sort / filter ---------------------------------------------------

    def _make_sort_handler(self, column: str):
        def handler(button: Gtk.ToggleButton) -> None:
            if button.get_active():
                self.vm.toggle_sort(column)
            self._sync_sort_buttons()

        return handler

    def _sync_sort_buttons(self) -> None:
        for column, (button, handler) in self._sort_buttons.items():
            button.handler_block(handler)
            try:
                button.set_active(column == self.vm.sort_column)
            finally:
                button.handler_unblock(handler)

    def _on_filter_changed(self, drop: Gtk.DropDown, _param) -> None:
        self.vm.set_result_filter(FILTER_VALUES[drop.get_selected()])

    def _on_history_toggled(self, button: Gtk.ToggleButton) -> None:
        enabled = button.get_active()
        self.vm.set_save_search_history(enabled)
        button.set_label(t("Search.HistoryToggle.On" if enabled else "Search.HistoryToggle.Off"))

    # ---- state sync ------------------------------------------------------------

    def _on_state_changed(self) -> None:
        self._sync_from_vm()

    def _sync_from_vm(self) -> None:
        vm = self.vm
        self._status_label.set_text(vm.status_text)
        self._sync_tabs()
        self._sync_results()
        self._sync_sort_buttons()
        self._sync_footer()
        if not vm.is_loading_more and getattr(vm, "_pending_selection_anchor", None):
            vm.notify_load_more_settled()

    def _sync_tabs(self) -> None:
        vm = self.vm
        existing: dict[str, Gtk.ToggleButton] = {}
        child = self._tab_box.get_first_child()
        while child is not None:
            existing[child.get_name()] = child
            child = child.get_next_sibling()
        wanted = [tab.id for tab in vm.tabs]
        if list(existing) != wanted:
            for widget in existing.values():
                self._tab_box.remove(widget)
            for tab in vm.tabs:
                self._tab_box.append(self._make_tab_button(tab))
            return
        for tab in vm.tabs:
            self._update_tab_button(existing[tab.id], tab)

    def _make_tab_button(self, tab: SearchTabItem) -> Gtk.ToggleButton:
        button = Gtk.ToggleButton()
        button.set_name(tab.id)
        button.set_css_classes(["search-tab", "flat"])
        button.connect("toggled", self._make_tab_handler(tab.id))
        self._update_tab_button(button, tab)
        return button

    def _update_tab_button(self, button: Gtk.ToggleButton, tab: SearchTabItem) -> None:
        if self.vm.is_query_active:
            button.set_label(f"{tab.display_name} ({tab.count})")
        else:
            button.set_label(tab.display_name)
        active = button.get_name() == self.vm.selected_tab
        button.set_active(active)  # handler no-ops on same-tab re-select

    def _make_tab_handler(self, tab_id: str):
        def handler(button: Gtk.ToggleButton) -> None:
            if button.get_active() and self.vm.selected_tab != tab_id:
                self.vm.set_selected_tab(tab_id)

        return handler

    def _sync_results(self) -> None:
        vm = self.vm
        if not vm.is_query_active:
            self._stack.set_visible_child_name("empty")
            self._no_results_title.set_visible(False)
            self._no_results_subtitle.set_visible(False)
            self._rebuild_apps_grid(vm.empty_state_items)
            self._rebuild_recently_used()
            self._rebuild_chips()
            return
        if not vm.has_results:
            self._stack.set_visible_child_name("empty")
            self._no_results_title.set_visible(True)
            self._no_results_subtitle.set_visible(True)
            self._apps_section.set_visible(False)
            self._recently_section.set_visible(False)
            self._recent_section.set_visible(False)
            self._favorites_section.set_visible(False)
            self._empty_hint.set_visible(False)
            return
        self._stack.set_visible_child_name("results")
        self._apps_section.set_visible(True)  # restore for the next empty state
        self._recently_section.set_visible(True)
        self._recent_section.set_visible(True)
        self._favorites_section.set_visible(True)
        self._empty_hint.set_visible(True)
        self._rebuild_rows(vm.current_results)

    def _rebuild_rows(self, items) -> None:
        selected = self.vm.selected_item
        selected_key = get_identity_key(selected).lower() if selected is not None else None
        self._suppress_selection_sync = True
        try:
            self._list_box.remove_all()
            for item in items:
                row = _ResultRow(item, self._row_hooks)
                self._list_box.append(row)
                if selected_key is not None and get_identity_key(item).lower() == selected_key:
                    self._list_box.select_row(row)
        finally:
            self._suppress_selection_sync = False

    def _rebuild_apps_grid(self, items) -> None:
        self._apps_flow.remove_all()
        for item in items:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.set_css_classes(["search-app-card"])
            card.set_size_request(104, 92)
            icon = Gtk.Label(label=item.glyph or "▦")
            icon.set_css_classes(["search-app-icon"])
            icon.set_size_request(36, 36)
            name = Gtk.Label(label=item.app_display_name)
            name.set_css_classes(["search-app-name"])
            name.set_ellipsize(3)
            card.append(icon)
            card.append(name)
            child = Gtk.FlowBoxChild()
            child.set_child(card)
            child.panebox_item = item
            self._apps_flow.append(child)
        self._apps_section.set_visible(bool(items))

    def _rebuild_recently_used(self) -> None:
        self._recently_box.remove_all()
        recent = self.vm.history.recent_results[:8]
        self._recently_section.set_visible(bool(recent))
        for item in recent:
            row = _ResultRow(item, self._row_hooks)
            self._recently_box.append(row)

    def _rebuild_chips(self) -> None:
        for box, values in (
            (self._recent_chips, self.vm.recent_queries[:RECENT_SEARCH_LIMIT]),
            (self._favorite_chips, self.vm.favorite_queries[:RECENT_SEARCH_LIMIT]),
        ):
            child = box.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                box.remove(child)
                child = next_child
            for value in values:
                chip = Gtk.Button(label=value)
                chip.set_css_classes(["search-chip", "flat"])
                chip.connect("clicked", self._make_chip_handler(value))
                box.append(chip)
        self._recent_section.set_visible(bool(self.vm.recent_queries))
        self._favorites_section.set_visible(bool(self.vm.favorite_queries))
        self._empty_hint.set_visible(not self.vm.recent_queries and not self.vm.favorite_queries)

    def _make_chip_handler(self, value: str):
        def handler(*_a) -> None:
            self.vm.apply_query(value)

        return handler

    def _sync_footer(self) -> None:
        saving = self.vm.settings.settings.search.searchSaveHistory
        if self._history_toggle.get_active() != saving:
            self._history_toggle.handler_block_by_func(self._on_history_toggled)
            try:
                self._history_toggle.set_active(saving)
            finally:
                self._history_toggle.handler_unblock_by_func(self._on_history_toggled)
        self._history_toggle.set_label(t("Search.HistoryToggle.On" if saving else "Search.HistoryToggle.Off"))

    # ---- row interactions ---------------------------------------------------

    def _on_row_activated(self, _box, row) -> None:
        if isinstance(row, _ResultRow):
            self.vm.execute_item(row.item)

    def _on_recently_activated(self, _box, row) -> None:
        if isinstance(row, _ResultRow):
            self.vm.execute_item(row.item)

    def _on_app_activated(self, _flow, child) -> None:
        item = getattr(child, "panebox_item", None)
        if item is not None:
            self.vm.execute_item(item)

    def _on_selected_row_changed(self, _box) -> None:
        if self._suppress_selection_sync:
            return
        row = self._list_box.get_selected_row()
        if isinstance(row, _ResultRow):
            self.vm.selected_item = row.item
            self.vm.selected_index = row.get_index()

    def _on_scroll(self, adjustment) -> None:
        remaining = adjustment.get_upper() - adjustment.get_value() - adjustment.get_page_size()
        threshold = max(
            INFINITE_SCROLL_MIN_REMAINING,
            adjustment.get_page_size() * INFINITE_SCROLL_VIEWPORT_FACTOR,
        )
        if remaining <= threshold:
            self.vm.load_more_results()

    # ---- action routing ------------------------------------------------------

    def _on_action_requested(self, action_id: str) -> None:
        handler = self._hooks.get(action_id)
        if handler is not None:
            handler()

    def _on_content_requested(self, item: SearchResultItem) -> None:
        handler = self._hooks.get("reveal_content")
        if handler is not None:
            handler(item)


class _ResultRow(Gtk.ListBoxRow):
    """Result row: glyph | title+subtitle | type | size | date (C# 22/34/*/75/90/110)."""

    def __init__(self, item: SearchResultItem, hooks: Optional[dict] = None):
        super().__init__()
        self.item = item
        self._hooks = hooks or {}
        self.set_css_classes(["search-row"])
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        glyph = Gtk.Label(label=item.display_glyph)
        glyph.set_css_classes(["search-row-glyph"])
        glyph.set_size_request(28, -1)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_box.set_hexpand(True)
        title = Gtk.Label(label=item.title)
        title.set_xalign(0)
        title.set_ellipsize(3)
        title.set_single_line_mode(True)
        title.set_css_classes(["search-row-title"])
        subtitle = Gtk.Label(label=item.subtitle or "")
        subtitle.set_xalign(0)
        subtitle.set_ellipsize(3)
        subtitle.set_single_line_mode(True)
        subtitle.set_css_classes(["search-row-subtitle", "dim-label"])
        subtitle.set_visible(bool(item.subtitle))
        title_box.append(title)
        title_box.append(subtitle)

        type_label = Gtk.Label(label=item.type_display or "")
        type_label.set_xalign(0)
        type_label.set_size_request(75, -1)
        type_label.set_css_classes(["search-row-type", "dim-label"])
        size_label = Gtk.Label(label=format_size(item.file_size))
        size_label.set_xalign(1)
        size_label.set_size_request(90, -1)
        size_label.set_css_classes(["search-row-size", "dim-label"])
        date_label = Gtk.Label(label=format_date(item.created_at or item.modified_at))
        date_label.set_xalign(1)
        date_label.set_size_request(110, -1)
        date_label.set_css_classes(["search-row-date", "dim-label"])

        box.append(glyph)
        box.append(title_box)
        box.append(type_label)
        box.append(size_label)
        box.append(date_label)
        self.set_child(box)

        if item.kind in (SearchResultKind.FILE, SearchResultKind.FOLDER):
            secondary = Gtk.GestureClick()
            secondary.set_button(3)
            secondary.connect("released", self._on_secondary_click)
            self.add_controller(secondary)

    # ---- context menu (attach/save included for files and folders) -----------

    def _on_secondary_click(self, gesture, _n, x: float, y: float) -> None:
        menu = self._build_menu()
        menu.set_parent(self)
        point = Gdk.Rectangle()
        point.x, point.y = int(x), int(y)
        menu.set_pointing_to(point)
        menu.popup()
        menu.connect("closed", lambda _m: menu.unparent())

    def _build_menu(self) -> Gtk.PopoverMenu:
        self._actions = Gio.SimpleActionGroup.new()
        entries = (
            ("open", t("Search.Menu.Open"), self._menu_open),
            ("open-location", t("Search.Menu.OpenLocation"), self._menu_open_location),
            ("copy-path", t("Search.Menu.CopyPath"), self._menu_copy_path),
            ("attach", t("Search.Menu.AttachToTodo"), self._menu_attach),
            ("save-note", t("Search.Menu.SaveToNote"), self._menu_save_note),
        )
        section = Gio.Menu.new()
        for name, label, callback in entries:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=callback: cb())
            self._actions.add_action(action)
            section.append(label, f"row.{name}")
        model = Gio.Menu.new()
        model.append_section(None, section)
        menu = Gtk.PopoverMenu.new_from_model(model)
        menu.insert_action_group("row", self._actions)
        return menu

    def _toast(self, key: str) -> None:
        message = t(key)
        toast = self._hooks.get("toast")
        if callable(toast):
            toast(message)
        else:
            _toast(message)

    def _menu_open(self) -> None:
        if not search_actions.open_result(self.item):
            self._toast("Search.Action.FileOperationFailed")

    def _menu_open_location(self) -> None:
        if not search_actions.reveal_result(self.item):
            self._toast("Search.Action.FileOperationFailed")

    def _menu_copy_path(self) -> None:
        display = Gdk.Display.get_default()
        clipboard = display.get_clipboard() if display is not None else None
        if clipboard is None:
            return
        self._toast(search_actions.copy_path(self.item, clipboard.set_text))

    def _menu_attach(self) -> None:
        settings = self._hooks.get("settings_service")
        factory = self._hooks.get("todo_store_factory")
        widget_id = (
            search_actions.pick_attach_target(settings) if settings is not None and factory is not None else None
        )
        if widget_id is None:
            self._toast("Search.Action.AttachFailed")
            return
        self._toast(search_actions.attach_to_todo(self.item, factory, widget_id))

    def _menu_save_note(self) -> None:
        service = self._hooks.get("quick_capture_service")
        if service is None:
            self._toast("Search.Action.SaveFailed")
            return
        self._toast(search_actions.save_to_note(self.item, service))


def _toast(message: str) -> None:
    """Best-effort outcome surface (libnotify; falls back to stderr like C# toasts)."""
    try:
        import gi

        gi.require_version("Notify", "0.7")
        from gi.repository import Notify

        if not Notify.is_initted():
            Notify.init("PaneBox")
        Notify.Notification.new("PaneBox", message, "panebox").show()
    except Exception:
        _LOG.info("[Search] %s", message)
