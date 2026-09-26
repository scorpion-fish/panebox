"""Search engine (port of Services/SearchEngineService.cs).

Coordinates the filename index (Everything replacement, search_file_index)
with a PaneBox-content snapshot (Quick Capture + Todo widgets) and the
built-in action catalog. The content snapshot refreshes at most once a
second, off the query hot path — a refresh that lands while a query is
running only affects the next one.

The service is synchronous; GTK callers wrap calls in the shared executor
(music-w style) and marshal results back via idle_add.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from ..i18n import t as default_localize
from ..models.search_models import (
    GLYPH_NOTE,
    GLYPH_SETTINGS,
    GLYPH_THEME,
    GLYPH_TODO,
    GLYPH_WIDGETS,
    SearchFileQueryPage,
    SearchResultGroup,
    SearchResultItem,
    SearchResultKind,
    SearchRecommendationItem,
    SearchResponse,
)
from .search_file_index import (
    SearchFileIndex,
    application_directories,
)
from .search_ranker import merge_and_rank

INITIAL_FILE_RESULT_PAGE_SIZE = 200
FILE_RESULT_PAGE_SIZE = 200
PANEBOX_CONTENT_REFRESH_INTERVAL_MS = 1000

MAX_START_MENU_APPS = 40
RECENT_NOTE_RECOMMENDATIONS = 3
UPCOMING_TODO_DAYS = 7
TRUNCATE_TITLE_LENGTH = 60

# (id, name key, glyph) — SearchActions table.
ACTION_CATALOG = (
    ("new-todo", "Search.Action.NewTodo", GLYPH_TODO),
    ("new-note", "Search.Action.NewNote", GLYPH_NOTE),
    ("open-settings", "Search.Action.OpenSettings", GLYPH_SETTINGS),
    ("toggle-widgets", "Search.Action.ToggleWidgets", GLYPH_WIDGETS),
    ("toggle-theme", "Search.Action.ToggleTheme", GLYPH_THEME),
)

GROUP_ORDER = (
    (SearchResultKind.ACTION, "Search.Group.Actions"),
    (SearchResultKind.TODO, "Search.Group.Todos"),
    (SearchResultKind.QUICK_CAPTURE, "Search.Group.Notes"),
    (SearchResultKind.FILE, "Search.Group.Files"),
    (SearchResultKind.FOLDER, "Search.Group.Folders"),
)


def compute_text_relevance(text: str, query: str) -> float:
    if text.lower() == query.lower():
        return 100.0
    if text.lower().startswith(query.lower()):
        return 80.0
    if query.lower() in text.lower():
        return 50.0
    return 30.0


def truncate_text(text: Optional[str], max_length: int) -> str:
    if not text or not text.strip():
        return ""
    single_line = " ".join(text.split())
    return single_line if len(single_line) <= max_length else single_line[:max_length] + "..."


@dataclass
class _PaneBoxSearchDocument:
    kind: str
    title: str
    body_text: Optional[str]
    auxiliary_text: Optional[str]
    subtitle: str
    todo_widget_id: Optional[str]
    todo_item_id: Optional[str]
    todo_is_completed: bool
    quick_capture_item_id: Optional[str]
    is_pinned: bool
    modified_at: Optional[datetime]


class SearchEngineService:
    def __init__(
        self,
        settings_service,
        file_index: Optional[SearchFileIndex] = None,
        quick_capture_service=None,
        todo_store_factory: Optional[Callable[[str], object]] = None,
        localize: Callable[[str], str] = default_localize,
    ):
        self._settings = settings_service
        self._localize = localize
        self._quick_capture = quick_capture_service
        self._todo_store_factory = todo_store_factory or _default_todo_store_factory
        self.file_index = (
            file_index
            if file_index is not None
            else SearchFileIndex(
                roots=(settings_service.settings.search.searchIndexRoots or None),
            )
        )
        self.on_results_changed: Optional[Callable[[], None]] = None

        self._content_lock = threading.Lock()
        self._content_snapshot: List[_PaneBoxSearchDocument] = []
        self._content_initialized = False
        self._content_last_refresh = 0.0
        self._content_refreshing = False
        self._disposed = False

    def dispose(self) -> None:
        self._disposed = True
        self.file_index.dispose()

    # ── search ────────────────────────────────────────────────────────────────

    def search(self, query: str) -> SearchResponse:
        return self.search_page(query, 0, INITIAL_FILE_RESULT_PAGE_SIZE)

    def search_page(self, query: str, file_offset: int, file_page_size: int) -> SearchResponse:
        """Unified search across file index + PaneBox content + actions."""
        started = time.monotonic()
        normalized_offset = max(0, file_offset)
        normalized_page = max(1, file_page_size)

        include_content = self._settings.settings.search.searchIncludePaneBoxContent
        try:
            # First query pays a one-time synchronous scan (bounded by the
            # index caps); later staleness refreshes stay asynchronous.
            if self.file_index.state == "Idle":
                self.file_index.scan_now()
            file_page = self.file_index.query(query, normalized_offset, normalized_page)
        except Exception:
            file_page = SearchFileQueryPage.empty()
        try:
            panebox_results = self._search_panebox_content(query) if include_content else []
        except Exception:
            panebox_results = []
        actions = self._search_actions(query)

        response = self._build_response(query, file_page, panebox_results, actions, started)
        return response

    def _build_response(self, query, file_page, panebox_results, actions, started) -> SearchResponse:
        ranked = merge_and_rank(
            [*file_page.items, *panebox_results, *actions],
            query.strip(),
            2**31 - 1,
        )
        groups = self._build_groups(ranked)
        materialized_files = sum(1 for item in ranked if item.kind in (SearchResultKind.FILE, SearchResultKind.FOLDER))
        non_file_results = len(ranked) - materialized_files
        return SearchResponse(
            query=query,
            ranked_items=ranked,
            groups=groups,
            total_result_count=file_page.total_matched_count + non_file_results,
            materialized_file_result_count=materialized_files,
            total_file_result_count=file_page.total_matched_count,
            next_file_result_offset=file_page.next_offset,
            has_more_results=file_page.next_offset < file_page.total_matched_count,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            is_complete=True,
            file_provider_state=self.file_index.state,
        )

    # ── PaneBox content snapshot ──────────────────────────────────────────────

    def content_changed(self) -> None:
        """External content mutation hook (QuickCaptureService.Changed analog)."""
        if not self._disposed and self._settings.settings.search.searchIncludePaneBoxContent:
            self._ensure_content_snapshot(force=True)

    def set_panebox_content_enabled(self, enabled: bool) -> None:
        if self._disposed:
            return
        if enabled:
            self._ensure_content_snapshot(force=True)
            return
        with self._content_lock:
            self._content_snapshot = []
            self._content_initialized = False
            self._content_last_refresh = 0.0

    def _ensure_content_snapshot(self, force: bool = False) -> None:
        with self._content_lock:
            if self._disposed:
                return
            if self._content_refreshing:
                return  # in flight; the next query will see it
            elapsed_ms = (time.monotonic() - self._content_last_refresh) * 1000.0
            if not force and self._content_initialized and elapsed_ms < PANEBOX_CONTENT_REFRESH_INTERVAL_MS:
                return
            self._content_refreshing = True
        try:
            snapshot = self._build_content_snapshot()
            with self._content_lock:
                was_initialized = self._content_initialized
                previous = self._content_snapshot
                self._content_snapshot = snapshot
                self._content_initialized = True
                self._content_last_refresh = time.monotonic()
                changed = previous != snapshot
            if was_initialized and changed and self.on_results_changed is not None:
                self.on_results_changed()
        except Exception:
            with self._content_lock:
                self._content_last_refresh = time.monotonic()
        finally:
            with self._content_lock:
                self._content_refreshing = False

    def _build_content_snapshot(self) -> List[_PaneBoxSearchDocument]:
        documents: List[_PaneBoxSearchDocument] = []
        if self._quick_capture is not None:
            data = self._quick_capture.get_data()
            for item in data.items:
                if item.isDeleted:
                    continue
                display_title = (
                    item.title if item.title and item.title.strip() else truncate_text(item.body, TRUNCATE_TITLE_LENGTH)
                )
                documents.append(
                    _PaneBoxSearchDocument(
                        kind=SearchResultKind.QUICK_CAPTURE,
                        title=display_title,
                        body_text=item.body,
                        auxiliary_text=item.url,
                        subtitle=str(item.type),
                        todo_widget_id=None,
                        todo_item_id=None,
                        todo_is_completed=False,
                        quick_capture_item_id=item.id,
                        is_pinned=item.isPinned,
                        modified_at=item.updatedAt,
                    )
                )

        for widget in self._settings.widgets():
            if widget.widgetKind != "Todo" or widget.isDisabled:
                continue
            try:
                data = self._todo_store_factory(widget.id).load()
            except Exception:
                continue
            for item in data.items:
                subtitle = (
                    f"{self._localize('Search.Todo.Due')}: {item.dueDate:%Y-%m-%d}" if item.dueDate else widget.name
                )
                documents.append(
                    _PaneBoxSearchDocument(
                        kind=SearchResultKind.TODO,
                        title=item.text,
                        body_text=item.notes,
                        auxiliary_text=None,
                        subtitle=subtitle,
                        todo_widget_id=widget.id,
                        todo_item_id=item.id,
                        todo_is_completed=item.isCompleted,
                        quick_capture_item_id=None,
                        is_pinned=False,
                        modified_at=item.updatedAt,
                    )
                )
        return documents

    def _search_panebox_content(self, query: str) -> List[SearchResultItem]:
        with self._content_lock:
            initialized = self._content_initialized
        if not initialized:
            self._ensure_content_snapshot(force=True)
        else:
            # Return the current snapshot immediately; refresh outside the
            # query hot path raises on_results_changed only if content changed.
            self._ensure_content_snapshot(force=False)
        with self._content_lock:
            snapshot = list(self._content_snapshot)

        results: List[SearchResultItem] = []
        needle = query.lower()
        for document in snapshot:
            title_matches = needle in document.title.lower()
            body_matches = document.body_text is not None and needle in document.body_text.lower()
            auxiliary_matches = document.auxiliary_text is not None and needle in document.auxiliary_text.lower()
            if not (title_matches or body_matches or auxiliary_matches):
                continue

            score = 1.0
            if title_matches:
                score = max(score, compute_text_relevance(document.title, query))
            if body_matches:
                score = max(score, compute_text_relevance(document.body_text, query) - 5)
            if auxiliary_matches:
                score = max(score, compute_text_relevance(document.auxiliary_text, query) - 10)
            if document.kind == SearchResultKind.TODO:
                score += -20 if document.todo_is_completed else 10
            elif document.is_pinned:
                score += 5

            results.append(
                SearchResultItem(
                    kind=document.kind,
                    title=document.title,
                    subtitle=document.subtitle,
                    todo_widget_id=document.todo_widget_id,
                    todo_item_id=document.todo_item_id,
                    todo_is_completed=document.todo_is_completed,
                    quick_capture_item_id=document.quick_capture_item_id,
                    glyph=GLYPH_TODO if document.kind == SearchResultKind.TODO else GLYPH_NOTE,
                    modified_at=document.modified_at,
                    relevance_score=max(1, score),
                )
            )
        return results

    # ── actions ───────────────────────────────────────────────────────────────

    def _search_actions(self, query: str) -> List[SearchResultItem]:
        results: List[SearchResultItem] = []
        needle = query.lower()
        for action_id, name_key, glyph in ACTION_CATALOG:
            name = self._localize(name_key)
            if needle in name.lower():
                results.append(
                    SearchResultItem(
                        kind=SearchResultKind.ACTION,
                        title=name,
                        action_id=action_id,
                        glyph=glyph,
                        relevance_score=compute_text_relevance(name, query) + 5,
                    )
                )
        return results

    # ── recommendations (empty state) ─────────────────────────────────────────

    def recommendations(self) -> List[SearchRecommendationItem]:
        """Widget-curated launchers first, then app-directory launchers (≤40)."""
        recommendations: List[SearchRecommendationItem] = []
        seen_paths = set()

        def add_launcher(path: str, subtitle: str) -> None:
            if not path.lower().endswith(".desktop") or not os.path.isfile(path):
                return
            full_path = os.path.abspath(path)
            if full_path.lower() in seen_paths:
                return
            seen_paths.add(full_path.lower())
            recommendations.append(
                SearchRecommendationItem(
                    kind=SearchResultKind.FILE,
                    title=os.path.basename(full_path),
                    subtitle=subtitle,
                    detail_path=full_path,
                )
            )

        # Enabled file widgets are an explicit curation signal — their
        # shortcuts outrank generic application-directory entries.
        for widget in self._settings.widgets():
            if widget.widgetKind != "File" or widget.isDisabled:
                continue
            for path in widget.manual_order_paths():
                add_launcher(path, widget.name)
            if widget.mappedFolderPath:
                try:
                    entries = sorted(os.listdir(widget.mappedFolderPath))
                except OSError:
                    entries = []
                for name in entries:
                    add_launcher(os.path.join(widget.mappedFolderPath, name), widget.name)

        start_menu_label = self._localize("Search.Recommend.StartMenu")
        launcher_paths: List[str] = []
        for directory in application_directories():
            if not os.path.isdir(directory):
                continue
            try:
                launcher_paths.extend(
                    os.path.join(directory, name) for name in os.listdir(directory) if name.lower().endswith(".desktop")
                )
            except OSError:
                continue
        launcher_paths.sort(key=lambda path: os.path.basename(path).lower())
        added = 0
        for path in launcher_paths:
            before = len(recommendations)
            add_launcher(path, start_menu_label)
            if len(recommendations) > before:
                added += 1
                if added >= MAX_START_MENU_APPS:
                    break
        return recommendations

    def recent_notes(self) -> List[SearchRecommendationItem]:
        """Most recent Quick Capture items (≤3) for the empty-state widget."""
        results: List[SearchRecommendationItem] = []
        if self._quick_capture is None:
            return results
        try:
            items = [item for item in self._quick_capture.get_data().items if not item.isDeleted]
        except Exception:
            return results
        items.sort(key=lambda item: item.updatedAt, reverse=True)
        for item in items[:RECENT_NOTE_RECOMMENDATIONS]:
            results.append(
                SearchRecommendationItem(
                    kind=SearchResultKind.QUICK_CAPTURE,
                    title=item.title
                    if item.title and item.title.strip()
                    else truncate_text(item.body, TRUNCATE_TITLE_LENGTH),
                    subtitle=str(item.type),
                    glyph=GLYPH_NOTE,
                    quick_capture_item_id=item.id,
                )
            )
        return results

    def upcoming_todos(self) -> List[SearchRecommendationItem]:
        """Unfinished todos due within 7 days (≤3)."""
        results: List[SearchRecommendationItem] = []
        now = datetime.now()
        horizon = now + timedelta(days=UPCOMING_TODO_DAYS)
        for widget in self._settings.widgets():
            if len(results) >= RECENT_NOTE_RECOMMENDATIONS:
                break
            if widget.widgetKind != "Todo" or widget.isDisabled:
                continue
            try:
                data = self._todo_store_factory(widget.id).load()
            except Exception:
                continue
            upcoming = [
                item for item in data.items if not item.isCompleted and item.dueDate and now <= item.dueDate <= horizon
            ]
            upcoming.sort(key=lambda item: item.dueDate)
            for item in upcoming[: RECENT_NOTE_RECOMMENDATIONS - len(results)]:
                results.append(
                    SearchRecommendationItem(
                        kind=SearchResultKind.TODO,
                        title=item.text,
                        subtitle=f"{self._localize('Search.Todo.Due')}: {item.dueDate:%m-%d}",
                        glyph=GLYPH_TODO,
                        todo_widget_id=widget.id,
                        todo_item_id=item.id,
                    )
                )
        return results

    # ── groups ────────────────────────────────────────────────────────────────

    def _build_groups(self, ranked: List[SearchResultItem]) -> List[SearchResultGroup]:
        groups: List[SearchResultGroup] = []
        for kind, name_key in GROUP_ORDER:
            items = [item for item in ranked if item.kind == kind]
            if items:
                groups.append(
                    SearchResultGroup(
                        kind=kind,
                        display_name=self._localize(name_key),
                        items=items,
                        total_count=len(items),
                    )
                )
        return groups


def _default_todo_store_factory(widget_id: str):
    from .todo_store import TodoWidgetStore

    return TodoWidgetStore(widget_id)
