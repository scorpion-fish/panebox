"""Snap solver: work-area flush, widget spacing, sticky hold, ranking, resize."""

from __future__ import annotations

from panebox.services.snap import (
    BOTTOM,
    LEFT,
    RIGHT,
    Rect,
    SnapTarget,
    resolve_move,
    resolve_resize_edge,
)

WORK = Rect(0, 0, 1920, 1080)
SPACING = 5
ENGAGE = 8
RELEASE = 12


def move(rect: Rect, targets=None, work_area=WORK, sticky_h=None, sticky_v=None):
    return resolve_move(
        rect,
        targets or [],
        work_area,
        SPACING,
        ENGAGE,
        RELEASE,
        sticky_horizontal=sticky_h,
        sticky_vertical=sticky_v,
    )


def test_no_snap_beyond_engage_threshold():
    result = move(Rect(200, 200, 300, 200))
    assert result.horizontal_match is None
    assert result.vertical_match is None
    assert result.bounds == Rect(200, 200, 300, 200)


def test_snap_left_edge_to_work_area():
    result = move(Rect(6, 300, 300, 200))  # 6px from work-area left, within 8
    match = result.horizontal_match
    assert match is not None
    assert match.source_edge == LEFT and match.coordinate == 0
    assert result.bounds.x == 0
    assert result.bounds.y == 300  # vertical untouched


def test_snap_right_edge_to_work_area():
    # right edge at 1912 → 8px from work-area right (1920)
    result = move(Rect(1612, 100, 300, 200))
    match = result.horizontal_match
    assert match is not None
    assert match.coordinate == 1920
    assert result.bounds.x == 1620  # 1920 - 300


def test_spaced_snap_to_neighbor_widget():
    neighbor = Rect(500, 200, 300, 200)
    # left edge lands 4px from neighbor.right()+spacing = 805 → snaps to 805
    result = move(Rect(801, 200, 300, 200), targets=[SnapTarget(neighbor, 0x42)])
    match = result.horizontal_match
    assert match is not None
    assert match.uses_spacing is True
    assert match.coordinate == 805
    assert match.target_window_handle == 0x42


def test_flush_beats_spaced_at_equal_delta():
    """Priority order: work-area(0) < spaced(1) < flush-to-widget(2)."""
    neighbor = Rect(0, 200, 300, 200)  # sits on the work-area left edge
    # left edge at 4: delta 4 to work-area x=0, delta 4 to neighbor.right()+5=305? no —
    # craft instead: candidate equally distant from work-area edge and spaced edge.
    neighbor = Rect(10, 200, 300, 200)  # right+spacing = 315
    result = move(Rect(311, 200, 300, 200), targets=[SnapTarget(neighbor, 0x42)])
    # left edge 311: work-area x=0 (delta 311, out), neighbor spaced 315 (delta 4),
    # neighbor left 10 (flush, delta 301, out) → only spaced qualifies.
    match = result.horizontal_match
    assert match is not None and match.uses_spacing
    assert result.bounds.x == 315


def test_work_area_beats_spaced_at_equal_delta():
    neighbor = Rect(316, 200, 300, 200)  # neighbor.left - spacing = 311
    # left edge 4: work-area x=0 delta 4 (priority 0); neighbor.left-5=311 no.
    # spaced candidate via RIGHT edge: neighbor.right()+5=621 no. flush 316 no.
    result = move(Rect(4, 200, 300, 200), targets=[SnapTarget(neighbor, 0x42)])
    match = result.horizontal_match
    assert match is not None
    assert match.target_window_handle == 0  # work-area pseudo-target
    assert result.bounds.x == 0


def test_sticky_match_holds_until_release_threshold():
    first = move(Rect(4, 200, 300, 200))
    assert first.horizontal_match is not None

    # Drag past ENGAGE=8 but under RELEASE=12 (delta measured from the snapped
    # origin 0): stays stuck.
    held = move(Rect(11, 200, 300, 200), sticky_h=first.horizontal_match)
    assert held.horizontal_match is not None
    assert held.bounds.x == 0

    # Beyond release: sticky releases, no snap at all.
    released = move(Rect(13, 200, 300, 200), sticky_h=first.horizontal_match)
    assert released.horizontal_match is None
    assert released.bounds.x == 13


def test_vertical_snap_bottom_to_work_area():
    result = move(Rect(100, 874, 300, 200))  # bottom at 1074, 6 from 1080
    match = result.vertical_match
    assert match is not None and match.source_edge == BOTTOM
    assert result.bounds.y == 880


def test_resize_snaps_moving_edge_only():
    neighbor = Rect(500, 200, 300, 200)
    # resizing the right edge toward neighbor.left(): proposed right = 503
    match = resolve_resize_edge(Rect(100, 200, 403, 200), RIGHT, [SnapTarget(neighbor, 0x42)], WORK, SPACING, ENGAGE)
    assert match is not None
    assert match.coordinate == 495  # neighbor.left - spacing
    assert match.source_edge == RIGHT


def test_resize_out_of_threshold_returns_none():
    assert resolve_resize_edge(Rect(100, 200, 600, 200), RIGHT, [], WORK, SPACING, ENGAGE) is None
