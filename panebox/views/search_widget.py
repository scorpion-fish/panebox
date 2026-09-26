"""Search widget surface (port of Views/SearchWidgetContent / SearchTile).

Desktop-widget face of Search: a launcher bar (placeholder + global-hotkey
badge when wide enough) plus the recent-queries list (with per-row delete and
clear). Results live only in the popup — activating anything here opens it.

Content thresholds (C# adaptive states): hotkey badge needs ≥220px width,
the recent list needs ≥180×112. GTK port: visibility tracked on size-allocate.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..i18n import t
from ..services.search_view_model import build_hotkey_hint

HOTKEY_BADGE_MIN_WIDTH = 220
HISTORY_LIST_MIN_WIDTH = 180
HISTORY_LIST_MIN_HEIGHT = 112
RECENT_QUERY_LIMIT = 8


class SearchSurface(Gtk.Box):
    """Launcher bar + recent searches; emits search_requested via callback."""

    def __init__(
        self,
        config,
        settings_service,
        history_service,
        search_requested=None,
        query_removed=None,
        history_cleared=None,
        application=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.config = config
        self.settings_service = settings_service
        self.history = history_service
        self.application = application
        self.search_requested = search_requested
        self.query_removed = query_removed
        self.history_cleared = history_cleared
        self.set_css_classes(["search-widget"])
        self._width = 0
        self._height = 0

        self._build_launcher()
        self._build_recent_list()

        # GTK4 has no size-allocate signal: track thresholds on the frame clock.
        self.add_tick_callback(self._on_tick)
        if self.history is not None:
            self.history.on_recent_queries_changed = self._on_history_changed

    # ---- construction -------------------------------------------------------

    def _build_launcher(self) -> None:
        bar = Gtk.Button()
        bar.set_css_classes(["search-launcher"])
        bar.connect("clicked", lambda *_a: self._request_search(None))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        glyph = Gtk.Label(label="⌕")
        glyph.set_css_classes(["search-launcher-glyph"])
        self._placeholder_label = Gtk.Label(label=t("Search.Placeholder"))
        self._placeholder_label.set_hexpand(True)
        self._placeholder_label.set_xalign(0)
        self._placeholder_label.set_css_classes(["search-launcher-text", "dim-label"])
        self._hotkey_badge = Gtk.Label(label="")
        self._hotkey_badge.set_css_classes(["search-hotkey-badge"])
        row.append(glyph)
        row.append(self._placeholder_label)
        row.append(self._hotkey_badge)
        bar.set_child(row)
        self.append(bar)

    def _build_recent_list(self) -> None:
        self._history_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._history_box.set_css_classes(["search-history"])
        self._history_box.set_vexpand(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=t("Search.Section.RecentSearches"))
        title.set_xalign(0)
        title.set_hexpand(True)
        title.set_css_classes(["search-section-title"])
        clear = Gtk.Button(label=t("Search.Section.ClearHistory"))
        clear.set_css_classes(["flat"])
        clear.connect("clicked", self._on_clear_clicked)
        header.append(title)
        header.append(clear)
        self._history_box.append(header)

        self._history_list = Gtk.ListBox()
        self._history_list.set_css_classes(["search-history-list"])
        self._history_list.set_vexpand(True)
        self._history_box.append(self._history_list)

        self._history_empty = Gtk.Label(label=t("Search.History.EmptyText"))
        self._history_empty.set_css_classes(["dim-label", "search-history-empty"])
        self._history_empty.set_wrap(True)
        self._history_empty.set_xalign(0)
        self._history_box.append(self._history_empty)

        self.append(self._history_box)

    # ---- adaptive thresholds --------------------------------------------------

    def _on_tick(self, _widget, _clock) -> bool:
        width, height = self.get_width(), self.get_height()
        if width != self._width or height != self._height:
            self._width, self._height = width, height
            self._apply_thresholds()
        return GLib.SOURCE_CONTINUE

    def _apply_thresholds(self) -> None:
        width, height = self._width, self._height
        search = self.settings_service.settings.search
        if search.searchHotkeyEnabled:
            self._hotkey_badge.set_label(build_hotkey_hint(search.searchHotkeyModifiers, search.searchHotkeyKey))
        else:
            self._hotkey_badge.set_label("")
        self._hotkey_badge.set_visible(bool(self._hotkey_badge.get_label()) and width >= HOTKEY_BADGE_MIN_WIDTH)
        self._history_box.set_visible(width >= HISTORY_LIST_MIN_WIDTH and height >= HISTORY_LIST_MIN_HEIGHT)

    # ---- data + events ---------------------------------------------------------

    def refresh(self) -> None:
        self._rebuild_history()

    def _on_history_changed(self) -> None:
        self._rebuild_history()

    def _rebuild_history(self) -> None:
        self._history_list.remove_all()
        queries = self.history.recent_queries[:RECENT_QUERY_LIMIT] if self.history else []
        self._history_empty.set_visible(not queries)
        self._history_list.set_visible(bool(queries))
        for query in queries:
            self._history_list.append(_RecentQueryRow(query, self))

    def _request_search(self, query: Optional[str]) -> None:
        if self.search_requested is not None:
            self.search_requested(query)

    def _on_clear_clicked(self, *_args) -> None:
        if self.history is not None:
            self.history.clear_recent_history()
        if self.history_cleared is not None:
            self.history_cleared()
        self._rebuild_history()

    def remove_query(self, query: str) -> None:
        if self.history is not None:
            self.history.remove_recent_query(query)
        if self.query_removed is not None:
            self.query_removed(query)
        self._rebuild_history()

    def queue_rebuild(self) -> None:
        """Language-switch hook (app retranslates surfaces by this name)."""
        self._placeholder_label.set_label(t("Search.Placeholder"))
        self._rebuild_history()

    def destroy(self) -> None:
        if self.history is not None and self.history.on_recent_queries_changed is self._on_history_changed:
            self.history.on_recent_queries_changed = None
        Gtk.Box.destroy(self)


class _RecentQueryRow(Gtk.ListBoxRow):
    def __init__(self, query: str, surface: SearchSurface):
        super().__init__()
        self.query = query
        self.set_css_classes(["search-history-row"])
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clock = Gtk.Label(label="🕘")
        clock.set_css_classes(["search-history-glyph"])
        label = Gtk.Label(label=query)
        label.set_hexpand(True)
        label.set_xalign(0)
        label.set_ellipsize(3)
        label.set_single_line_mode(True)
        remove = Gtk.Button(label="✕")
        remove.set_css_classes(["flat", "search-history-remove"])
        remove.set_tooltip_text(query)
        remove.connect("clicked", lambda *_a: surface.remove_query(query))
        box.append(clock)
        box.append(label)
        box.append(remove)
        self.set_child(box)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: surface._request_search(query))
        self.add_controller(click)
