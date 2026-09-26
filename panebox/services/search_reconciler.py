"""Result reconciliation + selection policy (ports of
SearchResultCollectionReconciler.cs and SearchResultSelectionPolicy.cs).

The reconciler aligns a live result list with a new target sequence using
granular operations — never a wholesale reset — so realized rows, scroll
position and selection survive incremental page appends and background
refreshes. The selection policy holds the pointer/multi-select rules; the
GTK port drives rows with single selection, so only the drag/auto-scroll
helpers that the surface still needs are kept pure here.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from ..models.search_models import SearchResultItem
from .search_ranker import get_identity_key

AUTOSCROLL_EDGE_PX = 32
AUTOSCROLL_MAX_DELTA_PX = 18


def has_same_identity_sequence(current: Sequence[SearchResultItem], incoming: Sequence[SearchResultItem]) -> bool:
    """True when both sequences carry the same identities in the same order."""
    if len(current) != len(incoming):
        return False
    for existing, new in zip(current, incoming):
        if get_identity_key(existing).lower() != get_identity_key(new).lower():
            return False
    return True


def reuse_existing_instances(
    current: List[SearchResultItem], incoming: Sequence[SearchResultItem]
) -> List[SearchResultItem]:
    """Background refresh: keep existing instances (icons/metadata live on them)
    but refresh ModifiedAt / RelevanceScore / TypeDisplay from the new items."""
    by_identity = {get_identity_key(item).lower(): item for item in incoming}
    merged: List[SearchResultItem] = []
    for existing in current:
        replacement = by_identity.get(get_identity_key(existing).lower())
        if replacement is not None:
            existing.modified_at = replacement.modified_at
            existing.relevance_score = replacement.relevance_score
            merged.append(existing)
        # Items absent from the new page are dropped by the caller's reconcile.
    return merged


def reconcile(current: List[SearchResultItem], target: Sequence[SearchResultItem]) -> List[SearchResultItem]:
    """Minimal-diff alignment: returns the target order while reusing instances
    from `current` where identity matches (reference-stable for realized rows)."""
    current_by_identity: dict = {}
    for item in current:
        current_by_identity.setdefault(get_identity_key(item).lower(), item)
    return [current_by_identity.get(get_identity_key(item).lower(), item) for item in target]


def get_range(anchor_index: int, target_index: int, item_count: int) -> Tuple[int, int]:
    if item_count <= 0 or not (0 <= anchor_index < item_count) or not (0 <= target_index < item_count):
        return (-1, -1)
    return (min(anchor_index, target_index), max(anchor_index, target_index))


def get_auto_scroll_delta(pointer_y: float, viewport_height: float) -> float:
    """Linear ramp inside the edge band, ±AUTOSCROLL_MAX_DELTA_PX at the border."""
    if viewport_height <= 0:
        return 0.0
    edge = min(float(AUTOSCROLL_EDGE_PX), viewport_height / 2.0)
    if pointer_y < edge:
        intensity = 1.0 - (pointer_y / edge) if edge > 0 else 1.0
        return -AUTOSCROLL_MAX_DELTA_PX * intensity
    distance_from_bottom = viewport_height - pointer_y
    if distance_from_bottom < edge:
        intensity = 1.0 - (distance_from_bottom / edge) if edge > 0 else 1.0
        return AUTOSCROLL_MAX_DELTA_PX * intensity
    return 0.0
