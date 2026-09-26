"""Widget snap solver — port of Services/WidgetSnapCalculator.cs.

Pure physical-pixel logic shared by move and resize sessions. All candidates
in one session share one coordinate space; callers convert once per
interaction. Ranking: delta, then priority (0 work-area < 1 spaced < 2 flush),
then perpendicular gap, then perpendicular center distance.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

LEFT = "Left"
RIGHT = "Right"
TOP = "Top"
BOTTOM = "Bottom"

HorizontalEdges = (LEFT, RIGHT)
VerticalEdges = (TOP, BOTTOM)

# ResizeGuideOverlayService constants (DIPs; multiply by monitor scale).
SNAP_ENAGE_THRESHOLD_DIPS = 8.0  # sic — PaneBox spelling preserved in behavior
SNAP_RELEASE_THRESHOLD_DIPS = 12.0


@dataclass(frozen=True)
class Rect:
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    def right(self) -> int:
        return self.x + self.width

    def bottom(self) -> int:
        return self.y + self.height


@dataclass(frozen=True)
class SnapTarget:
    bounds: Rect
    window_handle: int = 0  # XID; 0 = work area pseudo-target


@dataclass(frozen=True)
class SnapMatch:
    source_edge: str
    target_edge: str
    coordinate: int
    resolved_origin: int
    target_window_handle: int
    uses_spacing: bool
    delta: int


@dataclass(frozen=True)
class MoveSnapResult:
    bounds: Rect
    horizontal_match: Optional[SnapMatch]
    vertical_match: Optional[SnapMatch]


@dataclass(frozen=True)
class _Candidate:
    match: SnapMatch
    priority: int
    perpendicular_gap: int
    perpendicular_center_distance: int


def resolve_move(
    proposed: Rect,
    targets: list[SnapTarget],
    work_area: Optional[Rect],
    spacing: int,
    engage_threshold: int,
    release_threshold: int,
    sticky_horizontal: Optional[SnapMatch] = None,
    sticky_vertical: Optional[SnapMatch] = None,
) -> MoveSnapResult:
    spacing = max(0, spacing)
    engage_threshold = max(0, engage_threshold)
    release_threshold = max(engage_threshold, release_threshold)

    horizontal = _resolve_sticky_move_match(
        proposed.x, release_threshold, sticky_horizontal, horizontal=True
    ) or _resolve_best_move_match(proposed, targets, work_area, spacing, engage_threshold, horizontal=True)
    vertical = _resolve_sticky_move_match(
        proposed.y, release_threshold, sticky_vertical, horizontal=False
    ) or _resolve_best_move_match(proposed, targets, work_area, spacing, engage_threshold, horizontal=False)

    bounds = Rect(
        horizontal.resolved_origin if horizontal else proposed.x,
        vertical.resolved_origin if vertical else proposed.y,
        proposed.width,
        proposed.height,
    )
    return MoveSnapResult(bounds, horizontal, vertical)


def resolve_resize_edge(
    proposed: Rect,
    source_edge: str,
    targets: list[SnapTarget],
    work_area: Optional[Rect],
    spacing: int,
    threshold: int,
) -> Optional[SnapMatch]:
    """Resize snaps only the moving edge itself (flush or spaced)."""
    spacing = max(0, spacing)
    threshold = max(0, threshold)
    best: Optional[_Candidate] = None
    best = _evaluate_edge_candidates(proposed, targets, work_area, spacing, source_edge, threshold, best)
    return best.match if best else None


def _resolve_best_move_match(
    source: Rect,
    targets: list[SnapTarget],
    work_area: Optional[Rect],
    spacing: int,
    threshold: int,
    horizontal: bool,
) -> Optional[SnapMatch]:
    best: Optional[_Candidate] = None
    edges = HorizontalEdges if horizontal else VerticalEdges
    for edge in edges:
        best = _evaluate_edge_candidates(source, targets, work_area, spacing, edge, threshold, best)
    return best.match if best else None


def _resolve_sticky_move_match(
    proposed_origin: int,
    release_threshold: int,
    sticky: Optional[SnapMatch],
    horizontal: bool,
) -> Optional[SnapMatch]:
    if sticky is None:
        return None
    if horizontal != (sticky.source_edge in HorizontalEdges):
        return None
    delta = abs(proposed_origin - sticky.resolved_origin)
    if delta <= release_threshold:
        return replace(sticky, delta=delta)
    return None


def _evaluate_edge_candidates(
    source: Rect,
    targets: list[SnapTarget],
    work_area: Optional[Rect],
    spacing: int,
    source_edge: str,
    threshold: int,
    best: Optional[_Candidate],
) -> Optional[_Candidate]:
    for target in targets:
        best = _evaluate_widget_candidates(source, target, spacing, source_edge, threshold, best)
    if work_area is not None:
        best = _consider_candidate(_create_work_area_candidate(source, work_area, source_edge), threshold, best)
    return best


def _evaluate_widget_candidates(
    source: Rect,
    target: SnapTarget,
    spacing: int,
    source_edge: str,
    threshold: int,
    best: Optional[_Candidate],
) -> Optional[_Candidate]:
    other = target.bounds
    if source_edge in HorizontalEdges:
        perpendicular_gap = _interval_gap(source.y, source.bottom(), other.y, other.bottom())
        perpendicular_center = abs((source.y + source.height // 2) - (other.y + other.height // 2))
    else:
        perpendicular_gap = _interval_gap(source.x, source.right(), other.x, other.right())
        perpendicular_center = abs((source.x + source.width // 2) - (other.x + other.width // 2))

    handle = target.window_handle
    pairs = {
        LEFT: ((LEFT, other.x, False, 2), (RIGHT, other.right() + spacing, True, 1)),
        RIGHT: ((RIGHT, other.right(), False, 2), (LEFT, other.x - spacing, True, 1)),
        TOP: ((TOP, other.y, False, 2), (BOTTOM, other.bottom() + spacing, True, 1)),
        BOTTOM: ((BOTTOM, other.bottom(), False, 2), (TOP, other.y - spacing, True, 1)),
    }[source_edge]
    for target_edge, coordinate, uses_spacing, priority in pairs:
        best = _consider_candidate(
            _create_candidate(
                source,
                source_edge,
                target_edge,
                coordinate,
                handle,
                uses_spacing,
                priority,
                perpendicular_gap,
                perpendicular_center,
            ),
            threshold,
            best,
        )
    return best


def _create_work_area_candidate(source: Rect, work_area: Rect, source_edge: str) -> _Candidate:
    coordinate = {
        LEFT: work_area.x,
        RIGHT: work_area.right(),
        TOP: work_area.y,
        BOTTOM: work_area.bottom(),
    }[source_edge]
    return _create_candidate(source, source_edge, source_edge, coordinate, 0, False, 0, 0, 0)


def _create_candidate(
    source: Rect,
    source_edge: str,
    target_edge: str,
    coordinate: int,
    target_window_handle: int,
    uses_spacing: bool,
    priority: int,
    perpendicular_gap: int,
    perpendicular_center_distance: int,
) -> _Candidate:
    current = _edge_coordinate(source, source_edge)
    resolved_origin = {
        LEFT: coordinate,
        RIGHT: coordinate - source.width,
        TOP: coordinate,
        BOTTOM: coordinate - source.height,
    }[source_edge]
    delta = abs(current - coordinate)
    return _Candidate(
        SnapMatch(
            source_edge,
            target_edge,
            coordinate,
            resolved_origin,
            target_window_handle,
            uses_spacing,
            delta,
        ),
        priority,
        perpendicular_gap,
        perpendicular_center_distance,
    )


def _consider_candidate(candidate: _Candidate, threshold: int, best: Optional[_Candidate]) -> Optional[_Candidate]:
    if candidate.match.delta > threshold:
        return best
    if best is not None and not _is_better(candidate, best):
        return best
    return candidate


def _is_better(candidate: _Candidate, current: _Candidate) -> bool:
    c, k = candidate.match, current.match
    return c.delta < k.delta or (
        c.delta == k.delta
        and (
            candidate.priority < current.priority
            or (
                candidate.priority == current.priority
                and (
                    candidate.perpendicular_gap < current.perpendicular_gap
                    or (
                        candidate.perpendicular_gap == current.perpendicular_gap
                        and candidate.perpendicular_center_distance < current.perpendicular_center_distance
                    )
                )
            )
        )
    )


def _edge_coordinate(bounds: Rect, edge: str) -> int:
    return {
        LEFT: bounds.x,
        RIGHT: bounds.right(),
        TOP: bounds.y,
        BOTTOM: bounds.bottom(),
    }[edge]


def _interval_gap(first_start: int, first_end: int, second_start: int, second_end: int) -> int:
    if first_end < second_start:
        return second_start - first_end
    if second_end < first_start:
        return first_start - second_end
    return 0
