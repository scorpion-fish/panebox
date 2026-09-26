"""Search surfaces GUI tests under Xvfb: popup window state machine + desktop
widget surface against a fake engine (no real index). Mirrors the
test_music_surface.py harness."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


if _display_reachable(SMOKE_DISPLAY) and not os.environ.get("PANEBOX_SKIP_GUI"):
    # Must be pinned before the first Gdk.Display is opened.
    os.environ["DISPLAY"] = SMOKE_DISPLAY
    os.environ.setdefault("GDK_BACKEND", "x11")
    os.environ.setdefault("GSK_RENDERER", "cairo")

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    _display_reachable(SMOKE_DISPLAY) is False or os.environ.get("PANEBOX_SKIP_GUI"),
    reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)",
)

from panebox import i18n  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from panebox.models.search_models import (  # noqa: E402
    SearchRecommendationItem,
    SearchResultItem,
    SearchResultKind,
    SearchResponse,
)
from panebox.services.search_history import SearchHistoryService  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.views.search_popup import SearchPopupWindow  # noqa: E402
from panebox.views.search_widget import SearchSurface  # noqa: E402


class FakeEngine:
    def __init__(self):
        self.on_results_changed = None
        self.recommendation_pool = [
            SearchRecommendationItem(
                kind=SearchResultKind.FILE,
                title="editor.desktop",
                detail_path="/apps/editor.desktop",
            ),
            SearchRecommendationItem(
                kind=SearchResultKind.FILE,
                title="readme.txt",
                detail_path="/d/readme.txt",
            ),
        ]

    def search_page(self, query, offset, page_size):
        items = [
            SearchResultItem(
                kind=SearchResultKind.FILE,
                title=f"{query}-notes.txt",
                detail_path=f"/tmp/{query}-notes.txt",
                relevance_score=80,
                file_size=1536,
            ),
            SearchResultItem(
                kind=SearchResultKind.TODO,
                title=f"{query} sweep",
                subtitle="widget",
                todo_widget_id="w1",
                todo_item_id="t1",
                relevance_score=60,
            ),
        ]
        return SearchResponse(
            query=query,
            ranked_items=items,
            total_result_count=2,
            total_file_result_count=2,
            next_file_result_offset=2,
            has_more_results=False,
            elapsed_ms=5,
            is_complete=True,
        )

    def recommendations(self):
        return list(self.recommendation_pool)


def settle(predicate, timeout_ms: int = 4000) -> bool:
    """Pump the loop until predicate() or timeout (async VM plumbing lands)."""
    loop = GLib.MainLoop()
    state = {"done": False}

    def check():
        if predicate():
            state["done"] = True
            loop.quit()
            return False
        return True

    GLib.timeout_add(25, check)
    GLib.timeout_add(timeout_ms, loop.quit)
    loop.run()
    return state["done"]


@pytest.fixture()
def env():
    i18n.set_language("en-US")
    tmp = Path(tempfile.mkdtemp())
    settings = SettingsService(config_root=tmp / "config")
    settings.load()
    settings.settings.search.searchSaveHistory = True
    history = SearchHistoryService(store_path=tmp / "history.json")
    yield settings, history
    i18n.set_language(None)


def _make_popup(settings, history, **kwargs):
    return SearchPopupWindow(settings, FakeEngine(), history, **kwargs)


# ---- popup ------------------------------------------------------------------------


def test_popup_query_flow_tabs_rows_and_status(env):
    settings, history = env
    popup = _make_popup(settings, history)
    popup.open_search()
    popup._entry.set_text("desk")

    assert settle(lambda: popup._stack.get_visible_child_name() == "results")
    tab_names = []
    child = popup._tab_box.get_first_child()
    while child is not None:
        tab_names.append(child.get_name())
        child = child.get_next_sibling()
    assert tab_names == ["all", "app", "file", "image", "document", "panebox"]
    rows = list(popup._list_box)
    assert len(rows) == 2
    assert rows[0].item.type_display  # stamped
    assert popup.vm.status_text == "2 results · 5ms"
    popup.hide_search()


def test_popup_escape_chain_and_reopen_with_query(env):
    settings, history = env
    popup = _make_popup(settings, history)
    popup.open_search()
    popup._entry.set_text("desk")
    assert settle(lambda: popup._stack.get_visible_child_name() == "results")

    popup._handle_escape()  # text present → clears the query
    assert popup._entry.get_text() == "" and popup.vm.query == ""
    assert settle(lambda: popup._stack.get_visible_child_name() == "empty")

    popup._handle_escape()  # empty → hides
    assert not popup.get_visible()
    assert popup.vm.query == ""  # hidden state reset

    popup.open_search(initial_query="preset")  # initial query skips recommendations
    assert settle(lambda: popup.vm.is_query_active)
    assert popup._entry.get_text() == "preset"
    popup.hide_search()


def test_popup_empty_state_recommendations_and_chips(env):
    settings, history = env
    history.record_query("alpha")
    history.record_query("beta")
    history.toggle_favorite("alpha")
    popup = _make_popup(settings, history)
    popup.open_search()

    # Recommendations load in the background; chips come from history.
    assert settle(lambda: len(list(popup._apps_flow)) == 1 and popup._stack.get_visible_child_name() == "empty")
    cards = [child for child in popup._apps_flow]
    assert len(cards) == 1  # readme.txt filtered (apps only)
    chips = [chip.get_label() for chip in popup._favorite_chips]
    assert chips == ["alpha"]
    recent = [chip.get_label() for chip in popup._recent_chips]
    assert recent == ["beta", "alpha"]
    popup.hide_search()


def test_popup_history_toggle_persists_setting(env):
    settings, history = env
    popup = _make_popup(settings, history)
    popup.open_search()
    popup._history_toggle.set_active(False)
    assert settings.settings.search.searchSaveHistory is False
    assert popup._history_toggle.get_label() == "Search history: Off"
    popup._history_toggle.set_active(True)
    assert settings.settings.search.searchSaveHistory is True
    popup.hide_search()


def test_popup_action_routing(env):
    settings, history = env
    fired = []
    popup = _make_popup(settings, history, hooks={"new-todo": lambda: fired.append("new-todo")})
    popup.vm.on_action_requested("new-todo")
    assert fired == ["new-todo"]
    popup.hide_search()


# ---- desktop widget surface ---------------------------------------------------------


def _widget_config():
    from panebox.models.widget_config import WidgetConfig

    return WidgetConfig(id="sw1", name="Search", widgetKind="Search")


def test_search_surface_launcher_and_recent_queries(env):
    settings, history = env
    history.record_query("alpha")
    history.record_query("beta")
    requested = []
    surface = SearchSurface(_widget_config(), settings, history, search_requested=requested.append)
    surface.refresh()

    launcher = surface.get_first_child()  # launcher button
    assert launcher.get_css_classes().count("search-launcher") == 1
    launcher.emit("clicked")
    assert requested == [None]

    rows = list(surface._history_list)
    assert [row.query for row in rows] == ["beta", "alpha"]  # newest first

    # Per-row delete removes from the store and the view.
    surface.remove_query("beta")
    assert [row.query for row in surface._history_list] == ["alpha"]
    assert history.recent_queries == ["alpha"]

    # Clear button empties history.
    surface._on_clear_clicked()
    assert history.recent_queries == []
    assert surface._history_empty.get_visible()


def test_search_surface_thresholds_and_hotkey_badge(env):
    settings, history = env
    settings.settings.search.searchHotkeyEnabled = True
    settings.settings.search.searchHotkeyModifiers = 0x4 | 0x8  # Ctrl+Alt
    settings.settings.search.searchHotkeyKey = 0x64  # 'd'
    surface = SearchSurface(_widget_config(), settings, history)

    window = Gtk.Window()
    window.set_child(surface)
    window.set_default_size(480, 380)
    window.present()
    assert settle(lambda: surface._width >= 220 and surface._hotkey_badge.get_label())
    assert surface._hotkey_badge.get_visible()
    assert surface._hotkey_badge.get_label() == "Ctrl+Alt+D"
    assert surface._history_box.get_visible()
    window.destroy()


def test_search_surface_hotkey_badge_hidden_when_disabled(env):
    settings, history = env
    settings.settings.search.searchHotkeyEnabled = False
    surface = SearchSurface(_widget_config(), settings, history)
    window = Gtk.Window()
    window.set_child(surface)
    window.set_default_size(480, 380)
    window.present()
    assert settle(lambda: surface._width >= 220)  # tick applied thresholds
    assert not surface._hotkey_badge.get_visible()
    window.destroy()
