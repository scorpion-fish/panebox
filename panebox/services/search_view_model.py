"""Search popup view model (port of ViewModels/SearchPopupViewModel.cs).

State machine for the search popup: query flow with generation guards,
staged incremental paging (200/page, identity-union appends), dynamic tabs,
sorting/filtering, selection movement with load-more advance, execution
dispatch per result kind, history recording, and the cached recommendation
(empty-state) pool. Pure logic — no GTK; async work runs through injected
runner/dispatcher hooks so tests drive it synchronously.

GTK divergences (README): multi-selection batch operations are not ported
(single selection + context menu instead); display modes (Spotlight/Home/
Palette) are no-ops — the WinUI window never read them either.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

from ..i18n import t
from ..models.search_models import (
    FileCategory,
    SearchResultItem,
    SearchResultKind,
    SearchTabItem,
    categorize_file,
)
from .search_engine import (
    FILE_RESULT_PAGE_SIZE,
    INITIAL_FILE_RESULT_PAGE_SIZE,
    SearchEngineService,
)
from .search_history import SearchHistoryService
from .search_ranker import get_identity_key
from .search_reconciler import has_same_identity_sequence, reconcile, reuse_existing_instances

MAX_ENRICHED_SEARCH_RESULTS = 40
PROVIDER_REFRESH_DEBOUNCE_MS = 1000
RECOMMENDATION_CACHE_TTL_SECONDS = 60.0

# Input coalescing lives in the view (WinUI: 35ms DispatcherTimer).
INPUT_DEBOUNCE_MS = 35

SEARCH_HOTKEY_ID = 0x4444

# X11 modifier masks (Linux port gesture encoding).
MOD_SHIFT = 0x1
MOD_CONTROL = 0x4
MOD_ALT = 0x8

USER_QUERY = "UserQuery"
PROVIDER_UPDATE = "ProviderUpdate"
LOAD_MORE = "LoadMore"

_TAB_KEYS = {
    "all": "Search.Tab.All",
    "app": "Search.Tab.App",
    "file": "Search.Tab.File",
    "image": "Search.Filter.Images",
    "document": "Search.Filter.Documents",
    "panebox": "Search.Tab.PaneBox",
    "home": "Search.Tab.App",
}
QUERY_TAB_ORDER = ["all", "app", "file", "image", "document", "panebox"]
DEFAULT_TAB_ALLOWED = {"all", "app", "file", "panebox"}

_TYPE_KEYS = {
    FileCategory.APP: "Search.Type.App",
    FileCategory.DOCUMENT: "Search.Type.Document",
    FileCategory.IMAGE: "Search.Type.Image",
    FileCategory.VIDEO: "Search.Type.Video",
    FileCategory.MUSIC: "Search.Type.Music",
    FileCategory.ARCHIVE: "Search.Type.Archive",
    FileCategory.OTHER: "Search.Type.File",
}
_KIND_TYPE_KEYS = {
    SearchResultKind.FOLDER: "Search.Type.Folder",
    SearchResultKind.TODO: "Search.Type.Todo",
    SearchResultKind.QUICK_CAPTURE: "Search.Type.Note",
    SearchResultKind.ACTION: "Search.Type.Action",
    SearchResultKind.HISTORY: "Search.Type.Action",
    SearchResultKind.FAVORITE: "Search.Type.Action",
}

_NET_FORMAT_ARG = re.compile(r"\{(\d+)(?::([FfGgD][0-9]*|[^}]*))?\}")


def net_format(template: str, *values) -> str:
    """Expand .NET-style {0} / {1:F0} placeholders."""

    def substitute(match: "re.Match") -> str:
        index = int(match.group(1))
        spec = match.group(2) or ""
        value = values[index]
        if spec[:1] in ("F", "f"):
            digits = int(spec[1:] or "0")
            return f"{value:.{digits}f}"
        return str(value)

    return _NET_FORMAT_ARG.sub(substitute, template)


def type_display(item: SearchResultItem) -> str:
    if item.kind in _KIND_TYPE_KEYS:
        return t(_KIND_TYPE_KEYS[item.kind])
    return t(_TYPE_KEYS.get(categorize_file(item.title), "Search.Type.File"))


def _is_file_of_category(item: SearchResultItem, category: str) -> bool:
    return item.kind == SearchResultKind.FILE and categorize_file(item.title) == category


def _stable_order_key(item: SearchResultItem):
    modified = item.modified_at.timestamp() if item.modified_at else float("-inf")
    return (-item.relevance_score, -modified, item.title.lower())


class SearchPopupViewModel:
    def __init__(
        self,
        engine: SearchEngineService,
        settings_service,
        history_service: Optional[SearchHistoryService] = None,
        runner: Optional[Callable[[Callable[[], object]], object]] = None,
        dispatch: Optional[Callable[[Callable[[], None]], None]] = None,
        schedule: Optional[Callable[[int, Callable[[], None]], Callable[[], None]]] = None,
        open_file: Optional[Callable[[str], bool]] = None,
        reveal_file: Optional[Callable[[str], bool]] = None,
    ):
        self.engine = engine
        self.settings = settings_service
        self.history = history_service if history_service is not None else SearchHistoryService()
        self._runner = runner or _default_runner
        self._dispatch = dispatch or _default_dispatch
        self._schedule = schedule or _default_schedule
        self._open_file = open_file or (lambda path: False)
        self._reveal_file = reveal_file or (lambda path: False)

        # Observables (L83-128).
        self.query: str = ""
        self.is_searching = False
        self.has_results = False
        self.is_query_active = False
        self.selected_item: Optional[SearchResultItem] = None
        self.selected_index = -1
        self.status_text = ""
        self.selected_tab = ""
        self.sort_column = "Relevance"
        self.sort_ascending = True
        self.result_filter = "All"
        self.has_more_results = False
        self.is_loading_more = False
        self.total_result_count = 0
        self.tabs: List[SearchTabItem] = []
        self.current_results: List[SearchResultItem] = []
        self.empty_state_items: List[SearchResultItem] = []
        self.recommendations_loading = False

        # Internal pools/counters.
        self._all_results: List[SearchResultItem] = []
        self._recommendation_cache: List[SearchResultItem] = []
        self._recommendation_loaded_at = 0.0
        self._search_generation = 0
        self._recommendation_generation = 0
        self._next_file_result_offset = 0
        self._load_more_running = False
        self._provider_debounce_cancel: Optional[Callable[[], None]] = None
        self._last_query_searched = ""

        # Events.
        self.on_state_changed: Optional[Callable[[], None]] = None
        self.on_action_requested: Optional[Callable[[str], None]] = None
        self.on_content_requested: Optional[Callable[[SearchResultItem], None]] = None
        self.on_query_applied: Optional[Callable[[str], None]] = None
        self.on_hide_requested: Optional[Callable[[], None]] = None
        self.on_open_path: Optional[Callable[[str], None]] = None

        self.engine.on_results_changed = self._on_engine_results_changed
        self.rebuild_tabs()
        self.recommendations_cache_age_refresh()

    # ── infrastructure ────────────────────────────────────────────────────────

    def _changed(self) -> None:
        if self.on_state_changed is not None:
            self._dispatch(self.on_state_changed)

    def _submit(self, work: Callable[[], object], applied: Callable[[object], None]) -> None:
        generation = self._search_generation

        def done(future) -> None:
            def apply() -> None:
                if self._search_generation != generation:
                    return
                applied(future)

            self._dispatch(apply)

        future = self._runner(work)
        future.add_done_callback(done)

    # ── query flow ────────────────────────────────────────────────────────────

    def set_query(self, value: str) -> None:
        """User query (already debounced by the view)."""
        if value == self.query:
            return
        self.query = value or ""
        self.result_filter = "All"
        self.search_async(value, USER_QUERY)

    def apply_query(self, query: str) -> None:
        """History/Favorite activation — re-runs the query."""
        if self.on_query_applied is not None:
            self.on_query_applied(query)
        self.set_query(query)

    def clear_search(self) -> None:
        self._search_generation += 1
        self.query = ""
        self._all_results = []
        self._next_file_result_offset = 0
        self.is_query_active = False
        self.has_results = False
        self.has_more_results = False
        self.total_result_count = 0
        self.is_searching = False
        self.status_text = ""
        self.current_results = []
        self.selected_item = None
        self.selected_index = -1
        self.rebuild_tabs()
        self._rebuild_empty_state_items()
        self._changed()

    def refresh_search(self) -> None:
        self.search_async(self.query, PROVIDER_UPDATE)

    def search_async(self, query: str, refresh_kind: str) -> None:
        if not (query or "").strip():
            self.clear_search()
            return

        self._search_generation += 1
        if refresh_kind == USER_QUERY:
            self._next_file_result_offset = 0

        preserve_visible = refresh_kind != USER_QUERY and self.is_query_active and self.has_results

        self.is_searching = refresh_kind != LOAD_MORE
        if self.is_searching:
            self.status_text = t("Search.Status.Searching")
        self._changed()

        if refresh_kind == LOAD_MORE:
            offset = self._next_file_result_offset
            page_size = FILE_RESULT_PAGE_SIZE
        elif refresh_kind == PROVIDER_UPDATE:
            offset = 0
            page_size = max(INITIAL_FILE_RESULT_PAGE_SIZE, self._next_file_result_offset)
        else:
            offset = 0
            page_size = FILE_RESULT_PAGE_SIZE
        self._last_query_searched = query

        def work():
            return self.engine.search_page(query, offset, page_size)

        def applied(future) -> None:
            try:
                response = future.result()
            except Exception:
                response = None
            if response is None:
                self.status_text = t("Search.Status.Error")
                self.is_searching = False
                self._changed()
                return
            self.apply_search_response(response, refresh_kind, preserve_visible)

        self._submit(work, applied)

    def apply_search_response(self, response, refresh_kind: str, preserve_visible: bool) -> None:
        if refresh_kind == PROVIDER_UPDATE and not response.is_complete:
            return

        incoming = list(response.ranked_items)
        if not incoming:
            for group in response.groups:
                incoming.extend(group.items)
        for item in incoming:
            item.type_display = type_display(item)  # type label stamped once

        if refresh_kind == LOAD_MORE:
            self._merge_loaded_page(incoming)
        elif refresh_kind == PROVIDER_UPDATE and has_same_identity_sequence(self._all_results, incoming):
            # No structural change: refresh counters only, no visual churn.
            self.has_more_results = response.has_more_results
            self.total_result_count = response.total_result_count
            self._next_file_result_offset = response.next_file_result_offset
            self.status_text = self._status_text_for(response)
            self.rebuild_tabs()
            self.is_searching = False
            self._changed()
            return
        elif refresh_kind == PROVIDER_UPDATE:
            self._all_results = reuse_existing_instances(self._all_results, incoming)
            self._all_results = reconcile(self._all_results, incoming)

        if refresh_kind == USER_QUERY:
            # LoadMore already merged into _all_results above; a provider
            # refresh reused instances. Only a fresh query replaces wholesale.
            self._all_results = incoming

        self.is_query_active = True
        self.has_results = bool(self._all_results)
        self.has_more_results = response.has_more_results
        self.total_result_count = response.total_result_count
        self._next_file_result_offset = response.next_file_result_offset
        self.status_text = self._status_text_for(response)
        self.rebuild_tabs()
        self._rebuild_current_results(preserve_selection=True)
        self.is_searching = False
        if refresh_kind == LOAD_MORE:
            self.is_loading_more = False
            self._load_more_running = False
        self._changed()

    def _merge_loaded_page(self, incoming: List[SearchResultItem]) -> None:
        """Identity union: existing page entries win."""
        existing_keys = {get_identity_key(item).lower() for item in self._all_results}
        merged = list(self._all_results)
        for item in incoming:
            if get_identity_key(item).lower() not in existing_keys:
                merged.append(item)
        merged.sort(key=_stable_order_key)
        self._all_results = merged

    def _status_text_for(self, response) -> str:
        if response.is_complete:
            return net_format(t("Search.Status.Results"), response.total_result_count, response.elapsed_ms)
        return net_format(t("Search.Status.PartialResults"), response.total_result_count)

    # ── tabs ──────────────────────────────────────────────────────────────────

    def rebuild_tabs(self) -> None:
        query_active = self.is_query_active
        tab_ids = QUERY_TAB_ORDER if query_active else ["home"]

        if [tab.id for tab in self.tabs] == tab_ids:
            for tab in self.tabs:
                tab.count = self._count_for_tab(tab)
            return

        self.tabs = [self._make_tab(tab_id) for tab_id in tab_ids]
        previous = self.selected_tab
        if not query_active:
            self.selected_tab = "home"
        elif previous in ("", "home"):
            self.selected_tab = self._normalize_default_tab(self.settings.settings.search.searchDefaultTab)
        else:
            self.selected_tab = previous if previous in tab_ids else "all"
        for tab in self.tabs:
            tab.count = self._count_for_tab(tab)

    def _make_tab(self, tab_id: str) -> SearchTabItem:
        predicates = {
            "all": lambda item: True,
            "app": lambda item: _is_file_of_category(item, FileCategory.APP),
            "file": lambda item: item.kind in (SearchResultKind.FILE, SearchResultKind.FOLDER),
            "image": lambda item: _is_file_of_category(item, FileCategory.IMAGE),
            "document": lambda item: _is_file_of_category(item, FileCategory.DOCUMENT),
            "panebox": lambda item: (
                item.kind
                in (
                    SearchResultKind.TODO,
                    SearchResultKind.QUICK_CAPTURE,
                    SearchResultKind.ACTION,
                )
            ),
            "home": lambda item: True,
        }
        file_sort = tab_id in ("all", "file", "image", "document")
        return SearchTabItem(
            id=tab_id,
            display_name=t(_TAB_KEYS[tab_id]),
            predicate=predicates[tab_id],
            supports_file_sort=file_sort,
        )

    def _count_for_tab(self, tab: SearchTabItem) -> int:
        if not self.is_query_active:
            return 0
        return sum(1 for item in self._all_results if tab.predicate(item))

    def _normalize_default_tab(self, value: Optional[str]) -> str:
        normalized = (value or "").strip().lower()
        return normalized if normalized in DEFAULT_TAB_ALLOWED else "all"

    def cycle_tab(self, backward: bool = False) -> None:
        if not self.tabs:
            return
        ids = [tab.id for tab in self.tabs]
        current = ids.index(self.selected_tab) if self.selected_tab in ids else 0
        delta = -1 if backward else 1
        self.selected_tab = ids[(current + delta) % len(ids)]
        self._rebuild_current_results(preserve_selection=False)
        self._changed()

    def set_selected_tab(self, tab_id: str) -> None:
        if tab_id == self.selected_tab:
            return
        self.selected_tab = tab_id
        self._rebuild_current_results(preserve_selection=False)
        self._changed()

    # ── sorting + filtering ───────────────────────────────────────────────────

    def toggle_sort(self, column: str) -> None:
        if column == self.sort_column:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_column = column
            self.sort_ascending = column in ("Name", "Type")
        self._rebuild_current_results(preserve_selection=True)
        self._changed()

    def set_result_filter(self, value: str) -> None:
        if value == self.result_filter:
            return
        self.result_filter = value
        self._rebuild_current_results(preserve_selection=True)
        self._changed()

    def _matches_result_filter(self, item: SearchResultItem) -> bool:
        filters = {
            "All": lambda _item: True,
            "FilesAndFolders": lambda item: item.kind in (SearchResultKind.FILE, SearchResultKind.FOLDER),
            "Apps": lambda item: _is_file_of_category(item, FileCategory.APP),
            "Images": lambda item: _is_file_of_category(item, FileCategory.IMAGE),
            "Documents": lambda item: _is_file_of_category(item, FileCategory.DOCUMENT),
            "PaneBox": lambda item: (
                item.kind
                in (
                    SearchResultKind.TODO,
                    SearchResultKind.QUICK_CAPTURE,
                    SearchResultKind.ACTION,
                )
            ),
        }
        matcher = filters.get(self.result_filter, filters["All"])
        return matcher(item)

    def _sorted_tab_items(self) -> List[SearchResultItem]:
        tab = next((entry for entry in self.tabs if entry.id == self.selected_tab), None)
        pool = self._all_results if self.is_query_active else self._empty_pool()
        items = [item for item in pool if (tab.predicate(item) if tab else True)]
        if self.is_query_active and self.selected_tab == "all":
            items = [item for item in items if self._matches_result_filter(item)]

        if tab is not None and tab.supports_file_sort and self.sort_column != "Relevance":
            column = self.sort_column
            reverse = not self.sort_ascending
            if column == "Name":
                items.sort(key=lambda item: item.title.lower(), reverse=reverse)
            elif column == "Size":
                # Missing sizes always sort last (C#: long.MaxValue/MinValue).
                present = [item for item in items if item.file_size is not None]
                missing = [item for item in items if item.file_size is None]
                present.sort(key=lambda item: item.file_size, reverse=reverse)
                items = present + missing
            elif column == "Date":
                present = [item for item in items if (item.created_at or item.modified_at) is not None]
                missing = [item for item in items if (item.created_at or item.modified_at) is None]
                present.sort(key=lambda item: item.created_at or item.modified_at, reverse=reverse)
                items = present + missing
            elif column == "Type":
                items.sort(key=lambda item: (item.type_display or "").lower(), reverse=reverse)
        return items

    def _empty_pool(self) -> List[SearchResultItem]:
        return self.empty_state_items

    def _rebuild_current_results(self, preserve_selection: bool) -> None:
        target = self._sorted_tab_items()
        previous_selection = self.selected_item if preserve_selection else None
        self.current_results = reconcile(self.current_results, target)
        if not self.current_results:
            self.selected_item = None
            self.selected_index = -1
            return
        if previous_selection is not None:
            try:
                index = next(
                    index
                    for index, item in enumerate(self.current_results)
                    if get_identity_key(item).lower() == get_identity_key(previous_selection).lower()
                )
                self.selected_index = index
                self.selected_item = self.current_results[index]
                return
            except StopIteration:
                pass
        self.selected_index = 0
        self.selected_item = self.current_results[0]

    def _rebuild_empty_state_items(self) -> None:
        self.empty_state_items = list(self._recommendation_cache)

    # ── load more ─────────────────────────────────────────────────────────────

    def load_more_results(self) -> bool:
        if not self.has_more_results or not (self.query or "").strip() or self._load_more_running:
            return False
        self._load_more_running = True
        self.is_loading_more = True
        self._changed()
        self.search_async(self.query, LOAD_MORE)
        return True

    # ── selection ─────────────────────────────────────────────────────────────

    def move_selection_up(self) -> None:
        self.selected_index = max(0, self.selected_index - 1)
        self.selected_item = (
            self.current_results[self.selected_index] if 0 <= self.selected_index < len(self.current_results) else None
        )
        self._changed()

    def move_selection_down(self) -> None:
        if self.current_results and self.selected_index == len(self.current_results) - 1 and self.has_more_results:
            self._load_more_and_advance_selection()
            return
        if self.current_results:
            self.selected_index = min(len(self.current_results) - 1, self.selected_index + 1)
            self.selected_item = self.current_results[self.selected_index]
        self._changed()

    def _load_more_and_advance_selection(self) -> None:
        anchor_index = self.selected_index
        anchor = self.selected_item
        count_before = len(self.current_results)
        if not self.load_more_results():
            return  # guard rejected; selection stays put

        def land() -> None:
            if self.selected_item is anchor and len(self.current_results) > count_before:
                self.selected_index = min(len(self.current_results) - 1, anchor_index + 1)
            else:
                self.selected_index = (
                    min(len(self.current_results) - 1, self.selected_index + 1) if self.current_results else -1
                )
            self.selected_item = (
                self.current_results[self.selected_index]
                if 0 <= self.selected_index < len(self.current_results)
                else None
            )
            self._changed()

        # After a LoadMore the selection lands via the apply pipeline; the view
        # calls notify_load_more_settled() from its state-changed handler once
        # is_loading_more flips back to False.
        self._pending_selection_anchor = (anchor_index, anchor, count_before, land)

    def notify_load_more_settled(self) -> None:
        land = getattr(self, "_pending_selection_anchor", None)
        if land is not None:
            self._pending_selection_anchor = None
            land[3]()

    # ── execution ─────────────────────────────────────────────────────────────

    def execute_selected_item(self) -> bool:
        if self.selected_item is None:
            return False
        return self.execute_item(self.selected_item)

    def execute_item(self, item: SearchResultItem) -> bool:
        if item is None:
            return False
        if item.kind == SearchResultKind.FILE:
            if self._open_file(item.detail_path or ""):
                self._commit_execution(item)
                self._request_hide()
                return True
            return False
        if item.kind == SearchResultKind.FOLDER:
            if self._open_file(item.detail_path or ""):
                self._commit_execution(item)
                self._request_hide()
                return True
            return False
        if item.kind == SearchResultKind.ACTION:
            self._commit_execution(item, record_result=False)
            self.execute_action(item.action_id)
            return True
        if item.kind == SearchResultKind.TODO or item.kind == SearchResultKind.QUICK_CAPTURE:
            self._commit_execution(item)
            if self.on_content_requested is not None:
                self.on_content_requested(item)
            self._request_hide()
            return True
        if item.kind in (SearchResultKind.HISTORY, SearchResultKind.FAVORITE):
            self.apply_query(item.history_query or "")
            return True
        return False

    def open_selected_location(self) -> bool:
        item = self.selected_item
        if item is None or item.kind not in (SearchResultKind.FILE, SearchResultKind.FOLDER):
            return False
        if self._reveal_file(item.detail_path or ""):
            self._commit_execution(item)
            self._request_hide()
            return True
        return False

    def execute_action(self, action_id: Optional[str]) -> None:
        if action_id and self.on_action_requested is not None:
            self.on_action_requested(action_id)

    def invoke_action(self, action_id: str) -> None:
        self.execute_action(action_id)

    def _request_hide(self) -> None:
        if self.on_hide_requested is not None:
            self.on_hide_requested()

    def _commit_execution(self, item: SearchResultItem, record_result: bool = True) -> None:
        if not self.settings.settings.search.searchSaveHistory:
            return
        self.history.record_query(self.query)
        if record_result:
            self.history.record_result(item)

    # ── history surface ───────────────────────────────────────────────────────

    @property
    def recent_queries(self) -> List[str]:
        return self.history.recent_queries

    @property
    def favorite_queries(self) -> List[str]:
        return self.history.favorite_queries

    @property
    def is_current_query_favorite(self) -> bool:
        return self.history.is_favorite(self.query)

    def toggle_favorite_for_current_query(self) -> bool:
        return self.history.toggle_favorite(self.query)

    def clear_recent_searches(self) -> None:
        self.history.clear_recent_history()

    def clear_all_history(self) -> None:
        self.history.clear_all_history()

    def set_save_search_history(self, enabled: bool) -> None:
        self.settings.settings.search.searchSaveHistory = enabled
        self.settings.save_debounced()

    # ── recommendations (empty state) ─────────────────────────────────────────

    def recommendations_cache_age_refresh(self) -> None:
        self._recommendation_loaded_at = time.monotonic()

    def has_fresh_recommendation_cache(self) -> bool:
        return (
            bool(self._recommendation_cache)
            and time.monotonic() - self._recommendation_loaded_at < RECOMMENDATION_CACHE_TTL_SECONDS
        )

    def load_recommendations(self) -> None:
        if not self.settings.settings.search.searchShowRecommendations:
            self._recommendation_cache = []
            self._recommendation_loaded_at = 0.0
            self._rebuild_empty_state_items()
            self._changed()
            return

        self._recommendation_generation += 1
        generation = self._recommendation_generation
        self.recommendations_loading = True
        self._changed()

        def work():
            return self.engine.recommendations()

        def applied(future) -> None:
            if self._recommendation_generation != generation:
                return
            self.recommendations_loading = False
            try:
                recommendations = future.result()
            except Exception:
                recommendations = []
            items: List[SearchResultItem] = []
            seen = set()
            for recommendation in [*recommendations, *self.history.recent_results]:
                if recommendation.kind != SearchResultKind.FILE:
                    continue
                if categorize_file(recommendation.title) != FileCategory.APP:
                    continue  # app recommendations only (IsApplicationRecommendation)
                identity = f"path:{recommendation.detail_path or ''}".lower()
                if identity in seen:
                    continue
                seen.add(identity)
                items.append(
                    SearchResultItem(
                        kind=recommendation.kind,
                        title=recommendation.title,
                        subtitle=recommendation.subtitle,
                        detail_path=recommendation.detail_path,
                        modified_at=None,
                        relevance_score=1,
                    )
                )
            if items or not self._recommendation_cache:
                self._recommendation_cache = items
                self._recommendation_loaded_at = time.monotonic()
                self._rebuild_empty_state_items()
            self._changed()

        self._submit(work, applied)

    # ── popup lifecycle ───────────────────────────────────────────────────────

    def popup_opened(self, initial_query: Optional[str] = None) -> None:
        self.result_filter = "All"
        self.clear_search()
        if initial_query:
            # C#: an initial query skips the recommendation pipeline entirely.
            self.set_query(initial_query)
            return
        # Fresh cache (<60s) is reused; a stale-or-missing cache reloads.
        if not self.has_fresh_recommendation_cache():
            self.load_recommendations()

    def popup_hidden(self) -> None:
        self._search_generation += 1
        self._recommendation_generation += 1
        if self._provider_debounce_cancel is not None:
            self._provider_debounce_cancel()
            self._provider_debounce_cancel = None
        self.clear_search()  # keeps the recommendation cache

    def _on_engine_results_changed(self) -> None:
        def refresh() -> None:
            if self.query and self.query == self._last_query_searched and not self.is_searching:
                self.search_async(self.query, PROVIDER_UPDATE)

        if self._provider_debounce_cancel is not None:
            self._provider_debounce_cancel()
        self._provider_debounce_cancel = self._schedule(PROVIDER_REFRESH_DEBOUNCE_MS, refresh)

    def dispose(self) -> None:
        self._search_generation += 1
        self._recommendation_generation += 1
        self.engine.on_results_changed = None
        self.on_state_changed = None

    # ── hotkey hint ───────────────────────────────────────────────────────────

    @property
    def hotkey_hint(self) -> str:
        return build_hotkey_hint(
            self.settings.settings.search.searchHotkeyModifiers,
            self.settings.settings.search.searchHotkeyKey,
        )


def build_hotkey_hint(modifiers: int, key: int) -> str:
    parts = []
    if modifiers & MOD_CONTROL:
        parts.append("Ctrl")
    if modifiers & MOD_ALT:
        parts.append("Alt")
    if modifiers & MOD_SHIFT:
        parts.append("Shift")
    if 0x20 == key:
        parts.append("Space")
    elif 0x41 <= key <= 0x5A or 0x61 <= key <= 0x7A:
        parts.append(chr(key).upper())
    elif 0x30 <= key <= 0x39:
        parts.append(chr(key))
    elif key:
        parts.append(f"VK:{key:02X}")
    return "+".join(parts)


# ── default async plumbing (GLib when present) ─────────────────────────────

_shared_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="search-w")


def _default_runner(work):
    return _shared_executor.submit(work)


def _default_dispatch(callback):
    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        GLib.idle_add(callback)
    except Exception:
        callback()


def _default_schedule(delay_ms: int, callback):
    cancel_handles: list = []

    def cancel() -> None:
        for handle in cancel_handles:
            try:
                import gi

                gi.require_version("GLib", "2.0")
                from gi.repository import GLib

                GLib.source_remove(handle)
            except Exception:
                pass
        cancel_handles.clear()

    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        cancel_handles.append(GLib.timeout_add(delay_ms, _once, callback))
    except Exception:
        callback()
    return cancel


def _once(callback):
    callback()
    return False
