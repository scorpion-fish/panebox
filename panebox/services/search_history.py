"""Search history service (port of Services/SearchHistoryService.cs).

Persists recent queries (auto-recorded while searching), pinned favorites
(toggled explicitly, always ahead of history in the empty state) and the
results the user actually opened (file mtime is not usage — keeps the empty
state free of downloads and cache churn). Plain atomic JSON, camelCase,
indented — corrupt files simply restart from defaults (same as C#).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from ..constants import DATA_DIR
from ..models.search_models import SearchRecommendationItem, SearchResultItem
from .search_ranker import get_identity_key

MAX_HISTORY_ENTRIES = 20
MAX_RECENT_RESULT_ENTRIES = 12

# RecordResult ignores kinds that are not real content.
_NON_RECORDABLE_KINDS = {"Action", "History", "Favorite"}


class SearchHistoryService:
    def __init__(self, store_path: Path = DATA_DIR / "search-history.json"):
        self.store_path = Path(store_path)
        self.on_recent_queries_changed: Optional[Callable[[], None]] = None
        self._recent: List[str] = []
        self._favorites: List[str] = []
        self._recent_results: List[dict] = []
        self._load()

    # ---- reads ───────────────────────────────────────────────────────────────

    @property
    def recent_queries(self) -> List[str]:
        return list(self._recent)

    @property
    def favorite_queries(self) -> List[str]:
        return list(self._favorites)

    @property
    def recent_results(self) -> List[SearchRecommendationItem]:
        return [
            SearchRecommendationItem(
                kind=entry.get("kind", "File"),
                title=entry.get("title", ""),
                subtitle=entry.get("subtitle"),
                glyph=entry.get("glyph"),
                detail_path=entry.get("detailPath"),
                todo_widget_id=entry.get("todoWidgetId"),
                todo_item_id=entry.get("todoItemId"),
                quick_capture_item_id=entry.get("quickCaptureItemId"),
            )
            for entry in self._recent_results
        ]

    # ---- writes ──────────────────────────────────────────────────────────────

    def record_query(self, query: Optional[str]) -> None:
        """Deduplicated, newest first, capped at MAX_HISTORY_ENTRIES."""
        normalized = (query or "").strip()
        if not normalized:
            return
        self._recent = [q for q in self._recent if q.lower() != normalized.lower()]
        self._recent.insert(0, normalized)
        del self._recent[MAX_HISTORY_ENTRIES:]
        self._save()
        self._notify()

    def record_result(self, item: Optional[SearchResultItem]) -> None:
        """Called only after the user executes (opens) a result."""
        if item is None or item.kind in _NON_RECORDABLE_KINDS:
            return
        stored = _persisted_result_from(item)
        self._recent_results = [
            entry for entry in self._recent_results if entry.get("identity", "").lower() != stored["identity"].lower()
        ]
        self._recent_results.insert(0, stored)
        del self._recent_results[MAX_RECENT_RESULT_ENTRIES:]
        self._save()

    def remove_recent_query(self, query: Optional[str]) -> bool:
        normalized = (query or "").strip()
        if not normalized:
            return False
        remaining = [q for q in self._recent if q.lower() != normalized.lower()]
        if len(remaining) == len(self._recent):
            return False
        self._recent = remaining
        self._save()
        self._notify()
        return True

    def toggle_favorite(self, query: Optional[str]) -> bool:
        normalized = (query or "").strip()
        if not normalized:
            return False
        existing = next((q for q in self._favorites if q.lower() == normalized.lower()), None)
        if existing is not None:
            self._favorites.remove(existing)
            is_favorite = False
        else:
            self._favorites.insert(0, normalized)
            is_favorite = True
        self._save()
        return is_favorite

    def is_favorite(self, query: Optional[str]) -> bool:
        normalized = (query or "").strip()
        if not normalized:
            return False
        return any(q.lower() == normalized.lower() for q in self._favorites)

    def clear_recent_history(self) -> None:
        if not self._recent:
            return
        self._recent = []
        self._save()
        self._notify()

    def clear_all_history(self) -> None:
        self._recent = []
        self._favorites = []
        self._recent_results = []
        self._save()
        self._notify()

    def clear_history_and_results(self) -> None:
        """Search widget's clear button: wipes auto-recorded data, keeps pins."""
        self._recent = []
        self._recent_results = []
        self._save()
        self._notify()

    # ---- persistence ─────────────────────────────────────────────────────────

    def _notify(self) -> None:
        if self.on_recent_queries_changed is not None:
            self.on_recent_queries_changed()

    def _load(self) -> None:
        try:
            raw = self.store_path.read_text("utf-8")
            data = json.loads(raw)
            self._recent = [str(q) for q in data.get("recent") or []]
            self._favorites = [str(q) for q in data.get("favorites") or []]
            self._recent_results = [dict(entry) for entry in data.get("recentResults") or []]
        except (OSError, ValueError):
            self._recent, self._favorites, self._recent_results = [], [], []

    def _save(self) -> None:
        payload = {
            "recent": list(self._recent),
            "favorites": list(self._favorites),
            "recentResults": [dict(entry) for entry in self._recent_results],
        }
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.store_path.parent,
                prefix=self.store_path.name + ".",
                suffix=".tmp",
                delete=False,
            )
            try:
                with handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                os.replace(handle.name, self.store_path)
            finally:
                if os.path.exists(handle.name):
                    os.unlink(handle.name)
        except OSError:
            pass  # same as C#: log-free best effort


def _persisted_result_from(item: SearchResultItem) -> dict:
    return {
        "identity": get_identity_key(item),
        "kind": item.kind,
        "title": item.title,
        "subtitle": item.subtitle,
        "detailPath": item.detail_path,
        "glyph": item.glyph,
        "todoWidgetId": item.todo_widget_id,
        "todoItemId": item.todo_item_id,
        "quickCaptureItemId": item.quick_capture_item_id,
    }
