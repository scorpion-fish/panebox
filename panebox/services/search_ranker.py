"""Search result ranker (port of Services/SearchResultRanker.cs).

Normalizes, de-duplicates and globally ranks results from all providers.
Provider scores stay simple; this layer owns the cross-provider quality
rules. Path noise tables are the Linux equivalents of the Windows ones
(cache/git/node_modules/partial downloads), plus recycle-bin analogs.
"""

from __future__ import annotations

import math
import os
from typing import Iterable, List, Optional

from ..models.search_models import (
    FileCategory,
    SearchResultItem,
    SearchResultKind,
    categorize_file,
)

NOISY_DIRECTORY_SEGMENTS = (
    "/.git/",
    "/node_modules/",
    "/bin/debug/",
    "/obj/debug/",
    "/bin/release/",
    "/obj/release/",
    "/.cache/",
    "/cache/",
    "/caches/",
    "/.trash/",
    "/.trash-1000/",
    "/system volume information/",
    "/proc/",
    "/sys/",
)

PARTIAL_EXTENSIONS = {".crdownload", ".download", ".partial", ".part", ".tmp"}

KIND_BOOSTS = {
    SearchResultKind.ACTION: 6,
    SearchResultKind.TODO: 5,
    SearchResultKind.QUICK_CAPTURE: 4,
    SearchResultKind.FOLDER: 1,
}
NOISY_EXACT_PENALTY = 35
NOISY_BROAD_PENALTY = 70
APP_FILE_BOOST = 3


def is_noisy_path(path: Optional[str]) -> bool:
    if not path:
        return False
    extension = os.path.splitext(path)[1].lower()
    if extension in PARTIAL_EXTENSIONS:
        return True
    normalized = path.replace("\\", "/").lower()
    return any(segment in normalized for segment in NOISY_DIRECTORY_SEGMENTS)


def get_identity_key(item: SearchResultItem) -> str:
    if item.kind in (SearchResultKind.FILE, SearchResultKind.FOLDER) and item.detail_path:
        return f"path:{normalize_path(item.detail_path)}"
    if item.kind == SearchResultKind.TODO and item.todo_item_id:
        return f"todo:{item.todo_widget_id}:{item.todo_item_id}"
    if item.kind == SearchResultKind.QUICK_CAPTURE and item.quick_capture_item_id:
        return f"note:{item.quick_capture_item_id}"
    if item.kind == SearchResultKind.ACTION and item.action_id:
        return f"action:{item.action_id}"
    return f"{item.kind}:{item.title}:{item.subtitle}"


def normalize_path(path: str) -> str:
    try:
        absolute = os.path.normpath(os.path.abspath(path))
    except Exception:
        absolute = path.strip().rstrip("/\\")
    return absolute.rstrip("/")


def _adjust_score(item: SearchResultItem, query: str) -> float:
    score = item.relevance_score
    score += KIND_BOOSTS.get(item.kind, 0)

    if item.kind == SearchResultKind.FILE and categorize_file(item.title) == FileCategory.APP:
        score += APP_FILE_BOOST

    if is_noisy_path(item.detail_path):
        # Exact queries can still surface a cache or partial file, but broad
        # queries should not let those crowd out user documents.
        stem = os.path.splitext(os.path.basename(item.title))[0]
        exact = item.title.lower() == query.lower() or stem.lower() == query.lower()
        score -= NOISY_EXACT_PENALTY if exact else NOISY_BROAD_PENALTY
    return score


def _is_better_duplicate(candidate: SearchResultItem, existing: SearchResultItem) -> bool:
    if candidate.relevance_score != existing.relevance_score:
        return candidate.relevance_score > existing.relevance_score
    candidate_has_extension = bool(os.path.splitext(candidate.title)[1])
    existing_has_extension = bool(os.path.splitext(existing.title)[1])
    if candidate_has_extension != existing_has_extension:
        return candidate_has_extension
    candidate_time = candidate.modified_at.timestamp() if candidate.modified_at else -math.inf
    existing_time = existing.modified_at.timestamp() if existing.modified_at else -math.inf
    return candidate_time > existing_time


def merge_and_rank(results: Iterable[SearchResultItem], query: str, max_results: int) -> List[SearchResultItem]:
    if max_results <= 0:
        return []

    by_identity: dict = {}
    for item in results:
        if item.relevance_score <= 0:
            continue
        # No matching item is silently discarded; noisy paths stay reachable
        # at the end of the paged result stream.
        item.relevance_score = max(1, _adjust_score(item, query))
        identity = get_identity_key(item)
        existing = by_identity.get(identity)
        if existing is None or _is_better_duplicate(item, existing):
            by_identity[identity] = item

    ranked = sorted(
        by_identity.values(),
        key=lambda item: (
            -item.relevance_score,
            -(item.modified_at.timestamp() if item.modified_at else -math.inf),
            item.title.lower(),
        ),
    )
    return ranked[:max_results]
