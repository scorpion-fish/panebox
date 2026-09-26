"""Search popup view-model tests. The runner/dispatch run synchronously and
the schedule hook is manual, so the async pipeline is driven step by step."""

from __future__ import annotations

import tempfile
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

import pytest

from panebox import i18n
from panebox.models.search_models import (
    SearchRecommendationItem,
    SearchResultItem,
    SearchResultKind,
    SearchResponse,
)
from panebox.services.search_history import SearchHistoryService
from panebox.services.search_view_model import (
    PROVIDER_UPDATE,
    SearchPopupViewModel,
    build_hotkey_hint,
    net_format,
)
from panebox.services.settings_service import SettingsService


def immediate_runner(work):
    future = Future()
    try:
        future.set_result(work())
    except Exception as exc:  # pragma: no cover - surfaced via future.result
        future.set_exception(exc)
    return future


class _Schedule:
    """Manual timer queue: collect callbacks, pump them in tests."""

    def __init__(self):
        self.pending = []

    def __call__(self, delay_ms, callback):
        entry = [callback]
        self.pending.append(entry)

        def cancel():
            if entry in self.pending:
                self.pending.remove(entry)

        return cancel

    def pump(self):
        entries = list(self.pending)
        for entry in entries:
            if entry in self.pending:
                self.pending.remove(entry)
                entry[0]()


class _FakeEngine:
    """Scripted search_page responses + a controllable recommendation pool."""

    def __init__(self):
        self.responses = []
        self.calls = []
        self.recommendation_pool = []
        self.recommendations_calls = 0
        self.fail_next = False
        self.on_results_changed = None

    def search_page(self, query, offset, page_size):
        self.calls.append((query, offset, page_size))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("provider down")
        if self.responses:
            return self.responses.pop(0)
        return _response([])

    def recommendations(self):
        self.recommendations_calls += 1
        return list(self.recommendation_pool)


def _item(kind=SearchResultKind.FILE, title="x", path=None, score=10.0, **kwargs):
    values = dict(
        kind=kind,
        title=title,
        detail_path=path if path is not None else f"/tmp/{title}",
        relevance_score=score,
        modified_at=kwargs.pop("modified_at", None),
        file_size=kwargs.pop("file_size", None),
    )
    values.update(kwargs)
    return SearchResultItem(**values)


def _response(items, *, total=None, has_more=False, next_offset=None, elapsed=12, complete=True):
    return SearchResponse(
        query="",
        ranked_items=list(items),
        groups=[],
        total_result_count=total if total is not None else len(items),
        materialized_file_result_count=sum(
            1 for i in items if i.kind in (SearchResultKind.FILE, SearchResultKind.FOLDER)
        ),
        total_file_result_count=total if total is not None else len(items),
        next_file_result_offset=next_offset if next_offset is not None else len(items),
        has_more_results=has_more,
        elapsed_ms=elapsed,
        is_complete=complete,
    )


@pytest.fixture()
def env():
    i18n.set_language("en-US")
    tmp = Path(tempfile.mkdtemp())
    settings = SettingsService(config_root=tmp / "config")
    history = SearchHistoryService(store_path=tmp / "history.json")
    engine = _FakeEngine()
    schedule = _Schedule()
    opened = []
    revealed = []
    events = {"action": [], "content": [], "hide": 0, "query_applied": [], "state": 0}

    vm = SearchPopupViewModel(
        engine=engine,
        settings_service=settings,
        history_service=history,
        runner=immediate_runner,
        dispatch=lambda cb: cb(),
        schedule=schedule,
        open_file=lambda path: opened.append(path) or True,
        reveal_file=lambda path: revealed.append(path) or True,
    )
    vm.on_state_changed = lambda: events.__setitem__("state", events["state"] + 1)
    vm.on_action_requested = lambda action_id: events["action"].append(action_id)
    vm.on_content_requested = lambda item: events["content"].append(item)
    vm.on_hide_requested = lambda: events.__setitem__("hide", events["hide"] + 1)
    vm.on_query_applied = lambda query: events["query_applied"].append(query)
    yield vm, engine, settings, history, schedule, opened, revealed, events
    vm.dispose()
    i18n.set_language(None)


# ── tabs ─────────────────────────────────────────────────────────────────────


def test_tabs_order_counts_and_default_tab_normalization(env):
    vm, engine, *_ = env
    assert [tab.id for tab in vm.tabs] == ["home"]  # empty state

    engine.responses.append(
        _response(
            [
                _item(title="app.desktop", path="/a/app.desktop"),
                _item(title="photo.png", path="/p/photo.png"),
                _item(kind=SearchResultKind.TODO, title="sweep", todo_widget_id="w", todo_item_id="1"),
            ]
        )
    )
    vm.set_query("anything")
    assert [tab.id for tab in vm.tabs] == ["all", "app", "file", "image", "document", "panebox"]
    counts = {tab.id: tab.count for tab in vm.tabs}
    assert counts == {"all": 3, "app": 1, "file": 2, "image": 1, "document": 0, "panebox": 1}
    assert vm.selected_tab == "all"  # default tab "all"

    # Default tab applies when the popup is fresh (home tab → first query);
    # a query with a live tab selection keeps that selection.
    vm.settings.settings.search.searchDefaultTab = "panebox"
    vm.popup_opened()
    engine.responses.append(_response([_item(title="m.txt")]))
    vm.set_query("fresh")
    assert vm.selected_tab == "panebox"
    engine.responses.append(_response([_item(title="k.txt")]))
    vm.set_query("another")
    assert vm.selected_tab == "panebox"  # persists across queries in-session

    vm.settings.settings.search.searchDefaultTab = "bogus"
    vm.popup_opened()
    engine.responses.append(_response([_item(title="k.txt")]))
    vm.set_query("third")
    assert vm.selected_tab == "all"  # unknown default falls back to all

    vm.clear_search()
    assert [tab.id for tab in vm.tabs] == ["home"]

    # cycle_tab wraps around the query-active tab ring.
    vm.set_query("again")
    engine.responses.append(_response([_item(title="z.txt")]))
    vm.set_selected_tab("panebox")
    vm.cycle_tab()
    assert vm.selected_tab == "all"
    vm.cycle_tab(backward=True)
    assert vm.selected_tab == "panebox"


# ── query flow ───────────────────────────────────────────────────────────────


def test_query_flow_status_text_and_selection(env):
    vm, engine, *_ = env
    engine.responses.append(
        _response(
            [_item(title="a.txt"), _item(title="b.txt")],
            total=2,
            elapsed=7,
        )
    )
    vm.set_query("notes")
    assert vm.is_query_active and vm.has_results and not vm.is_searching
    assert vm.status_text == "2 results · 7ms"  # net_format {0} {1:F0}
    assert vm.selected_index == 0 and vm.selected_item.title == "a.txt"
    assert vm.current_results[0].type_display == "Doc"  # stamped once per response
    assert engine.calls[-1] == ("notes", 0, 200)

    vm.move_selection_down()
    assert vm.selected_index == 1
    vm.move_selection_down()
    assert vm.selected_index == 1  # clamped without has_more
    vm.move_selection_up()
    assert vm.selected_index == 0

    # Empty query clears; identical query is a no-op.
    vm.set_query("notes")
    assert engine.calls[-1] == ("notes", 0, 200)
    vm.set_query("")
    assert not vm.is_query_active and vm.query == ""


def test_error_response_shows_error_status(env):
    vm, engine, *_ = env
    engine.fail_next = True
    vm.set_query("x")
    assert vm.status_text == "Search error"
    assert not vm.is_searching


# ── load more ────────────────────────────────────────────────────────────────


def test_load_more_identity_union_and_offset_progression(env):
    vm, engine, *_ = env
    first_items = [_item(title="one.txt", score=30), _item(title="two.txt", score=20)]
    engine.responses.append(_response(first_items, total=5, has_more=True, next_offset=2))
    vm.set_query("doc")
    assert engine.calls[-1] == ("doc", 0, 200)

    vm.selected_index = 1
    engine.responses.append(
        _response(
            [_item(title="two.txt", score=20), _item(title="three.txt", score=10)],  # one overlap
            total=5,
            has_more=False,
            next_offset=5,
        )
    )
    assert vm.load_more_results() is True
    assert engine.calls[-1] == ("doc", 2, 200)  # offset from page 1
    titles = [item.title for item in vm.current_results]
    assert titles == ["one.txt", "two.txt", "three.txt"]  # union, no duplicate
    # Existing instance wins the merge (identity union keeps page-1 objects).
    assert vm.current_results[1] is first_items[1]
    assert not vm.has_more_results and not vm.is_loading_more

    # Guard: nothing to load or already running.
    assert vm.load_more_results() is False


def test_selection_load_more_advance_lands_next_row(env):
    vm, engine, *_ = env
    engine.responses.append(
        _response(
            [_item(title="one.txt", score=40), _item(title="two.txt", score=30)],
            total=4,
            has_more=True,
            next_offset=2,
        )
    )
    vm.set_query("doc")
    vm.move_selection_down()  # index 1 (last visible)
    assert vm.selected_index == 1

    engine.responses.append(
        _response(
            [
                _item(title="two.txt", score=30),
                _item(title="three.txt", score=20),
                _item(title="four.txt", score=10),
            ],
            total=4,
            has_more=False,
            next_offset=4,
        )
    )
    vm.move_selection_down()  # at the end + has_more → load more + advance
    assert vm.selected_index == 1  # anchor held until settled
    vm.notify_load_more_settled()  # view calls after is_loading_more flips
    assert vm.selected_index == 2
    assert vm.selected_item.title == "three.txt"
    assert [i.title for i in vm.current_results] == ["one.txt", "two.txt", "three.txt", "four.txt"]


# ── provider refresh ─────────────────────────────────────────────────────────


def test_provider_update_same_identity_no_churn(env):
    vm, engine, settings, history, schedule, opened, revealed, events = env
    first = [_item(title="a.txt", score=90), _item(title="b.txt", score=80)]
    engine.responses.append(_response(first, total=2, has_more=False, next_offset=2))
    vm.set_query("q")
    instance_ids = [id(item) for item in vm.current_results]

    # Content changed → debounced refresh; two fires coalesce into one timer.
    engine.on_results_changed()
    engine.on_results_changed()
    assert len(schedule.pending) == 1
    engine.responses.append(
        _response(
            [_item(title="a.txt", score=95), _item(title="b.txt", score=85)],
            total=2,
            has_more=False,
            next_offset=2,
        )
    )
    schedule.pump()  # timer fires the refresh
    assert engine.calls[-1][1:] == (0, 200)  # offset reset, page ≥ initial
    assert [id(item) for item in vm.current_results] == instance_ids  # no churn
    assert vm.total_result_count == 2


def test_provider_update_structural_change_reorders(env):
    vm, engine, *_ = env
    engine.responses.append(_response([_item(title="a.txt")], total=1))
    vm.set_query("q")
    engine.responses.append(
        _response(
            [_item(title="b.txt"), _item(title="a.txt")],
            total=2,
        )
    )
    vm.search_async("q", PROVIDER_UPDATE)
    assert [i.title for i in vm.current_results] == ["b.txt", "a.txt"]

    # Incomplete provider responses are discarded outright.
    engine.responses.append(_response([_item(title="zz.txt")], complete=False))
    vm.search_async("q", PROVIDER_UPDATE)
    assert [i.title for i in vm.current_results] == ["b.txt", "a.txt"]


# ── sorting + result filter ──────────────────────────────────────────────────


def test_sort_columns_with_missing_values_last(env):
    vm, engine, *_ = env
    now = datetime(2026, 9, 26, 12, 0, 0)
    old = datetime(2026, 1, 1, 8, 0, 0)
    engine.responses.append(
        _response(
            [
                _item(title="mid.txt", file_size=200, modified_at=now, created_at=now),
                _item(title="big.txt", file_size=900, modified_at=old, created_at=old),
                _item(title="ghost.txt"),  # no size, no dates
            ]
        )
    )
    vm.set_query("q")

    vm.toggle_sort("Size")  # first activation: desc
    assert [i.title for i in vm.current_results] == ["big.txt", "mid.txt", "ghost.txt"]
    vm.toggle_sort("Size")  # asc — missing still last
    assert [i.title for i in vm.current_results] == ["mid.txt", "big.txt", "ghost.txt"]

    vm.toggle_sort("Date")
    assert [i.title for i in vm.current_results] == ["mid.txt", "big.txt", "ghost.txt"]

    vm.toggle_sort("Name")
    assert [i.title for i in vm.current_results] == ["big.txt", "ghost.txt", "mid.txt"]
    assert vm.sort_column == "Name" and vm.sort_ascending

    vm.toggle_sort("Relevance")  # back to rank order
    assert vm.sort_column == "Relevance"


def test_result_filter_only_applies_on_all_tab(env):
    vm, engine, *_ = env
    engine.responses.append(
        _response(
            [
                _item(title="a.png", path="/i/a.png"),
                _item(title="b.txt", path="/d/b.txt"),
            ]
        )
    )
    vm.set_query("q")

    vm.set_result_filter("Images")
    assert [i.title for i in vm.current_results] == ["a.png"]

    vm.set_selected_tab("image")  # filter ignored off the All tab
    assert [i.title for i in vm.current_results] == ["a.png"]
    vm.set_selected_tab("all")
    assert [i.title for i in vm.current_results] == ["a.png"]

    vm.set_query("q2")  # new query resets the filter
    engine.responses.append(_response([_item(title="c.txt")]))
    assert vm.result_filter == "All"


# ── execution ────────────────────────────────────────────────────────────────


def test_execute_dispatch_per_kind(env):
    vm, engine, settings, history, _s, opened, revealed, events = env
    engine.responses.append(
        _response(
            [
                _item(title="doc.txt", path="/d/doc.txt"),
                _item(kind=SearchResultKind.FOLDER, title="dir", path="/d"),
                _item(kind=SearchResultKind.ACTION, title="New Todo", action_id="new-todo"),
                _item(kind=SearchResultKind.TODO, title="sweep", todo_widget_id="w", todo_item_id="1"),
                _item(kind=SearchResultKind.HISTORY, title="notes", history_query="notes"),
            ]
        )
    )
    vm.set_query("notes")
    items = {item.title: item for item in vm._all_results}

    # FILE → open + commit + hide.
    assert vm.execute_item(items["doc.txt"]) is True
    assert opened == ["/d/doc.txt"] and events["hide"] == 1
    # FOLDER → open.
    assert vm.execute_item(items["dir"]) is True
    assert opened == ["/d/doc.txt", "/d"]
    # ACTION → action event, never recorded as a recent result.
    assert vm.execute_item(items["New Todo"]) is True
    assert events["action"] == ["new-todo"]
    # TODO → content event + hide + recorded.
    assert vm.execute_item(items["sweep"]) is True
    assert events["content"] == [items["sweep"]] and events["hide"] == 3
    # HISTORY → re-runs the query.
    assert vm.execute_item(items["notes"]) is True
    assert events["query_applied"] == ["notes"]

    assert "notes" in history.recent_queries
    recorded_paths = [item.detail_path for item in history.recent_results]
    assert "/d/doc.txt" in recorded_paths and "/d" in recorded_paths
    # Actions and history queries never enter recent results.
    assert all(item.kind != SearchResultKind.ACTION for item in history.recent_results)

    # Reveal path for the selected file.
    vm.selected_item = items["doc.txt"]
    assert vm.open_selected_location() is True
    assert revealed == ["/d/doc.txt"]


def test_execute_open_failure_does_not_commit_or_hide(env):
    vm, engine, settings, history, schedule, opened, revealed, events = env
    vm._open_file = lambda path: False
    engine.responses.append(_response([_item(title="gone.txt", path="/g/gone.txt")]))
    vm.set_query("q")
    item = vm._all_results[0]
    assert vm.execute_item(item) is False
    assert events["hide"] == 0
    assert history.recent_results == []


def test_save_history_disabled_skips_recording(env):
    vm, engine, settings, history, *_ = env
    settings.settings.search.searchSaveHistory = False
    engine.responses.append(_response([_item(title="x.txt", path="/x.txt")]))
    vm.set_query("q")
    assert vm.execute_item(vm._all_results[0]) is True
    assert history.recent_queries == [] and history.recent_results == []


# ── recommendations (empty state) ────────────────────────────────────────────


def test_recommendations_app_filter_dedupe_and_ttl(env):
    vm, engine, settings, history, *_ = env
    engine.recommendation_pool = [
        SearchRecommendationItem(kind=SearchResultKind.FILE, title="a.desktop", detail_path="/apps/a.desktop"),
        SearchRecommendationItem(kind=SearchResultKind.FILE, title="b.txt", detail_path="/d/b.txt"),  # not an app
        SearchRecommendationItem(kind=SearchResultKind.FILE, title="c.png", detail_path="/i/c.png"),  # not an app
    ]
    history.record_result(_item(title="a.desktop", path="/apps/a.desktop"))  # dupe of engine entry
    history.record_result(_item(title="z.desktop", path="/apps/z.desktop"))

    vm.load_recommendations()
    assert [i.title for i in vm.empty_state_items] == ["a.desktop", "z.desktop"]
    assert vm.has_fresh_recommendation_cache()

    # Fresh cache: reopening the popup does not reload.
    vm.popup_hidden()
    vm.popup_opened()
    assert engine.recommendations_calls == 1

    # Stale cache: reopening reloads.
    vm._recommendation_loaded_at = 0.0
    vm.popup_opened()
    assert engine.recommendations_calls == 2

    # Disabled setting clears the cache entirely.
    settings.settings.search.searchShowRecommendations = False
    vm.load_recommendations()
    assert vm.empty_state_items == [] and not vm.has_fresh_recommendation_cache()


def test_popup_opened_with_initial_query_skips_recommendations(env):
    vm, engine, *_ = env
    engine.responses.append(_response([_item(title="hit.txt")]))
    vm.popup_opened(initial_query="hit")
    assert engine.recommendations_calls == 0
    assert vm.query == "hit" and vm.is_query_active


def test_popup_hidden_clears_query_keeps_cache_cancels_timer(env):
    vm, engine, _s, _h, schedule, *_ = env
    vm.load_recommendations()
    cache = vm._recommendation_cache
    engine.on_results_changed()
    assert len(schedule.pending) == 1

    vm.popup_hidden()
    assert schedule.pending == []
    assert vm.query == "" and not vm.is_query_active
    assert vm._recommendation_cache is cache


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_net_format():
    assert net_format("{0} results · {1:F0}ms", 5, 12) == "5 results · 12ms"
    assert net_format("{1} before {0}", "a", "b") == "b before a"
    assert net_format("no placeholders", 1) == "no placeholders"
    assert net_format("pi {0:F2}", 3.14159) == "pi 3.14"


def test_build_hotkey_hint():
    assert build_hotkey_hint(0x4 | 0x8, 0x64) == "Ctrl+Alt+D"  # default: Alt+D
    assert build_hotkey_hint(0x1, 0x20) == "Shift+Space"
    assert build_hotkey_hint(0, 0x35) == "5"
    assert build_hotkey_hint(0, 0x00) == ""
    assert build_hotkey_hint(0, 0xB8) == "VK:B8"
