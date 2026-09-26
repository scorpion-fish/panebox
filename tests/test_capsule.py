"""Capsule policy tests — direct ports of the C# calculator semantics
(bounds, expansion, direction, interaction, privacy, order, arrangement)."""

from __future__ import annotations

from panebox.models.widget_config import WidgetConfig
from panebox.services import capsule as c


def test_normalize_collapse_behavior():
    assert c.normalize_collapse_behavior("manual") == "Click"
    assert c.normalize_collapse_behavior("auto") == "Smart"
    assert c.normalize_collapse_behavior("expanded") == "Expanded"
    assert c.normalize_collapse_behavior(None) == "Click"  # fallback
    assert c.normalize_collapse_behavior("System", allow_system=True) == "System"
    assert c.normalize_collapse_behavior("System") == "Click"  # system needs opt-in


def test_collapse_override_resolution():
    config = WidgetConfig(widgetKind="Todo")
    assert c.collapse_override(config) == "System"
    assert c.resolve_collapse_behavior(config, "Smart") == "Smart"

    c.set_collapse_override(config, "Click")
    assert config.metadata["CollapseBehavior"] == "Click"
    assert c.resolve_collapse_behavior(config, "Smart") == "Click"  # override wins

    c.set_collapse_override(config, "System")
    assert "CollapseBehavior" not in config.metadata


def test_privacy_policy():
    assert c.hides_sensitive_content(True, "Todo") is True
    assert c.hides_sensitive_content(True, "Weather") is False
    assert c.hides_sensitive_content(False, "Todo") is False
    # Smart + privacy-sensitive → Summary; Music keeps Smart (media capsule).
    assert c.resolve_privacy_content_mode("Smart", True, "Todo") == "Summary"
    assert c.resolve_privacy_content_mode("Smart", True, "Music") == "Smart"
    assert c.resolve_privacy_content_mode("Smart", False, "File") == "Smart"
    assert c.resolve_privacy_content_mode("Minimal", True, "Todo") == "Minimal"


def test_transition_policy():
    assert c.resolve_transition_reason("Smart", "Expanded") == "CollapseBehaviorDisabled"
    assert c.resolve_transition_reason("Click", "Smart") == "Interaction"
    assert c.should_capture_compact_placement("Interaction", True, True, False) is True
    assert c.should_capture_compact_placement("CollapseBehaviorDisabled", True, True, False) is False
    assert c.should_capture_compact_placement("Interaction", True, True, True) is False


def test_logical_width_height_by_content_mode():
    assert c.resolve_logical_width("Minimal") == c.MINIMAL_WIDTH
    assert c.resolve_logical_width("Smart", "File") == c.SMART_WIDTH
    assert c.resolve_logical_width("Smart", "Music") == c.SMART_MEDIA_WIDTH
    assert c.resolve_logical_width(None) == c.SUMMARY_WIDTH
    # Custom width wins, clamped to the 144..480 capsule band.
    assert c.resolve_logical_width("Smart", "File", 600) == c.MAX_WIDTH
    assert c.resolve_logical_width("Smart", "File", 100) == c.MIN_WIDTH
    assert c.resolve_logical_width("Smart", "File", 200, clamp_custom_width=False) == 200
    assert c.resolve_logical_height("Smart") == c.SMART_DETAIL_HEIGHT
    assert c.resolve_logical_height("Summary") == c.CAPSULE_HEIGHT
    assert c.resolve_width_tier(150) == "Narrow"
    assert c.resolve_width_tier(250) == "Standard"
    assert c.resolve_width_tier(350) == "Wide"


def test_calculate_compact_bounds_anchor_aware():
    expanded = c.Rect(100, 200, 400, 500)
    top_left = c.calculate_compact_bounds(expanded, "LeftTop", 1.0, "Summary")
    assert top_left == c.Rect(100, 200, int(c.SUMMARY_WIDTH), 42)
    bottom_right = c.calculate_compact_bounds(expanded, "RightBottom", 1.0, "Summary")
    assert bottom_right.x == expanded.right - int(c.SUMMARY_WIDTH)
    assert bottom_right.y == expanded.bottom - 42
    # DPI scaling rounds through.
    scaled = c.calculate_compact_bounds(expanded, "LeftTop", 2.0, "Summary")
    assert scaled.width == int(c.SUMMARY_WIDTH * 2)


def test_resolve_compact_bounds_prefers_placement():
    from panebox.models.widget_config import WidgetCompactPlacement

    config = WidgetConfig(widgetKind="File")
    config.compactPlacement = WidgetCompactPlacement(x=10, y=20, positionAnchor="LeftTop")
    resolved = c.resolve_compact_bounds(config, c.Rect(0, 0, 400, 400), 1.0, "Summary")
    assert (resolved.x, resolved.y) == (10, 20)
    assert resolved.height == 42

    config.compactPlacement = None
    derived = c.resolve_compact_bounds(config, c.Rect(0, 0, 400, 400), 1.0, "Summary")
    assert derived.x == 0 and derived.width == int(c.SUMMARY_WIDTH)


def test_capture_compact_placement_records_margins():
    config = WidgetConfig(widgetKind="File")
    config.positionAnchor = "RightBottom"
    work = c.Rect(0, 0, 1920, 1040)
    bounds = c.Rect(1700, 990, 200, 42)
    c.capture_compact_placement(config, bounds, work, 1.0)
    placement = config.compactPlacement
    assert placement is not None
    assert placement.positionAnchor == "RightBottom"
    assert placement.x == 1700 and placement.y == 990
    assert placement.positionMarginX == work.right - bounds.right  # 20
    assert placement.positionMarginY == work.bottom - bounds.bottom  # 8


# ---- expansion -----------------------------------------------------------------


def test_expansion_prefers_fitting_anchor():
    work = c.Rect(0, 0, 1920, 1040)
    compact = c.Rect(10, 500, 250, 42)  # left edge: LeftTop fits, LeftBottom too
    layout = c.resolve_expansion(compact, c.Size(400, 600), work)
    assert layout.can_expand
    assert layout.expanded_bounds.x == compact.x  # grows right from LeftTop
    assert layout.expanded_bounds.width == 400

    # Capsule near the bottom: growing down has no room, so the calculator
    # picks a bottom anchor and expands upward from the capsule's bottom edge.
    compact_bottom = c.Rect(900, 1000, 250, 42)
    layout = c.resolve_expansion(compact_bottom, c.Size(400, 600), work)
    assert layout.can_expand
    assert layout.anchor == "LeftBottom"
    assert layout.expanded_bounds.width == 400 and layout.expanded_bounds.height == 600
    assert layout.expanded_bounds.bottom == compact_bottom.bottom
    assert layout.is_size_constrained  # pivot sits at the work-area edge


def test_expansion_clamps_when_work_area_too_small():
    work = c.Rect(0, 0, 300, 200)
    compact = c.Rect(100, 80, 200, 40)
    layout = c.resolve_expansion(compact, c.Size(500, 400), work)
    assert layout.is_size_constrained
    assert not layout.can_expand or layout.expanded_bounds.width <= work.width


def test_expansion_direction_constraints():
    anchors = ["LeftTop", "LeftBottom", "RightTop", "RightBottom"]
    assert c.apply_expansion_direction("Auto", anchors) == anchors
    assert c.apply_expansion_direction("Down", anchors) == ["LeftTop", "RightTop"]
    assert c.apply_expansion_direction("Up", anchors) == ["LeftBottom", "RightBottom"]
    assert c.direction_requires_full_size("Down") is True
    assert c.direction_requires_full_size("Auto") is False


def test_expansion_direction_override_metadata():
    config = WidgetConfig(widgetKind="File")
    assert c.expansion_direction_override(config) is None
    assert c.resolve_effective_expansion_direction(config, "Down") == "Down"
    c.set_expansion_direction_override(config, "up")
    assert c.expansion_direction_override(config) == "Up"  # normalized
    assert c.resolve_effective_expansion_direction(config, "Down") == "Up"
    c.set_expansion_direction_override(config, None)
    assert c.expansion_direction_override(config) is None


def test_pivot_and_interpolation():
    compact = c.Rect(100, 100, 200, 40)
    pivot = c.pivot_of(compact, "LeftTop")
    assert pivot == c.Point(100, 100)
    pivot_br = c.pivot_of(compact, "RightBottom")
    assert pivot_br == c.Point(300, 140)
    expanded = c.bounds_from_pivot(pivot_br, c.Size(300, 200), "RightBottom")
    assert expanded == c.Rect(0, -60, 300, 200)
    mid = c.interpolate_anchored_bounds(compact, c.Rect(100, 100, 300, 200), pivot, "LeftTop", 0.5)
    assert mid.width == 250 and mid.height == 120
    assert c.from_position_anchor("RightTop") == "RightTop"
    assert c.from_position_anchor("Center") is None


def test_horizontal_resize_anchor_flip():
    assert c.resolve_horizontal_resize_anchor("LeftTop", "Right") == "LeftTop"
    assert c.resolve_horizontal_resize_anchor("LeftBottom", "Right") == "LeftBottom"
    assert c.resolve_horizontal_resize_anchor("LeftTop", "Left") == "RightTop"
    assert c.resolve_horizontal_resize_anchor("RightBottom", "Left") == "RightBottom"


# ---- interaction ----------------------------------------------------------------


def _snap(**kwargs):
    return c.InteractionSnapshot(**kwargs)


def test_can_hover_expand_requires_smart_and_eligible_intent():
    snap = _snap(is_collapsed=True, is_pointer_inside=True, is_expansion_zone_active=True)
    assert c.can_hover_expand("Smart", snap) is True
    assert c.can_hover_expand("Click", snap) is False
    # Move handle (identity strip) never expands.
    assert (
        c.can_hover_expand(
            "Smart",
            _snap(
                is_collapsed=True,
                is_pointer_inside=True,
                is_expansion_zone_active=True,
                is_pointer_over_move_handle=True,
            ),
        )
        is False
    )
    # Trailing actions only expand after the longer dwell.
    over_actions = _snap(
        is_collapsed=True,
        is_pointer_inside=True,
        is_expansion_zone_active=True,
        is_pointer_over_actions=True,
    )
    assert c.can_hover_expand("Smart", over_actions) is False
    assert c.can_hover_expand("Smart", over_actions, allow_interaction_region_dwell=True) is True
    # Any active interaction blocks.
    assert (
        c.can_hover_expand(
            "Smart",
            _snap(
                is_collapsed=True,
                is_pointer_inside=True,
                is_expansion_zone_active=True,
                is_dragging=True,
            ),
        )
        is False
    )


def test_hover_dwell_floor():
    assert c.resolve_hover_expand_delay_ms(100, False) == 100
    assert c.resolve_hover_expand_delay_ms(100, True) == c.INTERACTION_REGION_HOVER_DELAY_FLOOR_MS


def test_auto_collapse_and_view_states():
    assert c.can_auto_collapse("Smart", _snap()) is True
    assert c.can_auto_collapse("Smart", _snap(is_pointer_inside=True)) is False
    assert c.can_auto_collapse("Smart", _snap(is_pinned=True)) is False
    assert c.can_auto_collapse("Click", _snap()) is False
    assert c.should_retry_auto_collapse("Smart", _snap(is_pointer_inside=True)) is True

    assert c.resolve_view_state("Smart", _snap(is_collapsed=True)) == "Glance"
    assert c.resolve_view_state("Smart", _snap(is_pinned=True)) == "Pinned"
    assert c.resolve_view_state("Smart", _snap()) == "Peek"
    assert c.resolve_view_state("Smart", _snap(is_dragging=True)) == "Open"
    assert c.resolve_view_state("Click", _snap()) == "Open"


def test_smart_entry_synchronization():
    snap = _snap(is_expansion_zone_active=True, is_pointer_over_actions=True, suppress_hover_expansion=True)
    synced = c.synchronize_smart_entry(snap, is_pointer_physically_inside=True)
    assert synced.is_pointer_inside is True
    assert synced.is_expansion_zone_active is False
    assert synced.is_pointer_over_actions is False
    assert synced.suppress_hover_expansion is False


def test_capsule_resize_direction_rules():
    assert c.resolve_resize_direction(True, "TopLeft") == "Left"
    assert c.resolve_resize_direction(True, "BottomRight") == "Right"
    assert c.resolve_resize_direction(True, "Top") == ""
    assert c.resolve_resize_direction(False, "TopLeft") == "TopLeft"
    assert c.can_resize(False, False, "TopLeft") is True
    assert c.can_resize(False, True, "Left") is True
    assert c.can_resize(False, True, "Top") is False
    assert c.can_resize(True, True, "Left") is False  # transition blocks


# ---- capsule order ----------------------------------------------------------------


def test_move_to_nearest_slot():
    ids = ["a", "b", "c"]
    slots = [c.Rect(0, 0, 100, 40), c.Rect(110, 0, 100, 40), c.Rect(220, 0, 100, 40)]
    # "a" dragged to the far right → last slot.
    assert c.move_to_nearest_slot(ids, slots, "a", c.Rect(230, 0, 100, 40), "Horizontal") == [
        "b",
        "c",
        "a",
    ]
    # Drop near its own slot → unchanged.
    assert c.move_to_nearest_slot(ids, slots, "a", c.Rect(5, 0, 100, 40), "Horizontal") == [
        "a",
        "b",
        "c",
    ]
    # Vertical arrangement compares Y centers.
    vslots = [c.Rect(0, 0, 40, 30), c.Rect(0, 40, 40, 30), c.Rect(0, 80, 40, 30)]
    assert c.move_to_nearest_slot(ids, vslots, "c", c.Rect(0, 5, 40, 30), "Vertical") == [
        "c",
        "a",
        "b",
    ]


def test_merge_group_order():
    # Group members keep their complete-order slots but receive the group's
    # internal order: 'a' slot gets 'c', 'c' slot gets 'a'.
    assert c.merge_group_order(["a", "b", "c", "d"], ["c", "a"]) == ["c", "b", "a", "d"]


# ---- capsule bar arrangement --------------------------------------------------------


def test_arrangement_single_row():
    items = [c.CapsuleArrangementItem("a", 200, 42), c.CapsuleArrangementItem("b", 250, 42)]
    slots = c.calculate_capsule_arrangement(
        items, c.Rect(0, 0, 1920, 1040), c.Point(100, 500), "LeftTop", "Horizontal", 8
    )
    assert slots["a"].x == 100
    assert slots["b"].x == 100 + 200 + 8
    assert slots["a"].y == slots["b"].y == 500
    assert slots["a"].height == slots["b"].height == 42  # common row height


def test_arrangement_single_column_and_anchor_flip():
    items = [c.CapsuleArrangementItem("a", 200, 52), c.CapsuleArrangementItem("b", 200, 42)]
    slots = c.calculate_capsule_arrangement(
        items, c.Rect(0, 0, 1920, 1040), c.Point(300, 100), "RightBottom", "Vertical", 8
    )
    # Right anchor: grow leftward from the anchor X; common width = max item width.
    assert slots["a"].x == 300 - 200
    assert slots["b"].x == slots["a"].x
    # Bottom anchor: the column grows upward, then clamps at the work-area top.
    assert slots["b"].y < slots["a"].y
    assert slots["a"].y >= 0 and slots["b"].y >= 0


def test_arrangement_scales_down_when_space_runs_out():
    items = [
        c.CapsuleArrangementItem("a", 200, 42),
        c.CapsuleArrangementItem("b", 200, 42),
        c.CapsuleArrangementItem("c", 200, 42),
    ]
    # Work area only 400 wide: 3×200 + 2×8 = 616 > 400 → shrink to fit.
    slots = c.calculate_capsule_arrangement(items, c.Rect(0, 0, 400, 1040), c.Point(0, 100), "LeftTop", "Horizontal", 8)
    total = sum(s.width for s in slots.values()) + 2 * 8
    assert total <= 400
    # Group is clamped inside the work area.
    assert all(0 <= s.x and s.right <= 400 for s in slots.values())


def test_arrangement_empty_inputs():
    assert c.calculate_capsule_arrangement([], c.Rect(0, 0, 100, 100), c.Point(0, 0), None, "Horizontal", 8) == {}
    assert (
        c.calculate_capsule_arrangement(
            [c.CapsuleArrangementItem("a", 10, 10)],
            c.Rect(0, 0, 0, 0),
            c.Point(0, 0),
            None,
            "Horizontal",
            8,
        )
        == {}
    )


# ---- normalizers --------------------------------------------------------------------


def test_capsule_setting_normalizers():
    assert c.normalize_content_mode("minimal") == "Minimal"
    assert c.normalize_content_mode("bogus") == "Smart"
    assert c.normalize_width_mode("independent") == "Independent"
    assert c.normalize_width_mode(None) == "Aligned"
    assert c.normalize_expansion_direction("up") == "Up"
    assert c.normalize_expansion_direction("x") == "Auto"
    assert c.normalize_arrangement_mode("vertical") == "Bar"  # legacy values fold into Bar
    assert c.normalize_arrangement_mode("free") == "Free"
    assert c.normalize_bar_placement("top") == "Top"
    assert c.normalize_bar_placement("side") == "Floating"
    assert c.normalize_bar_direction("vertical") == "Vertical"
    assert c.normalize_bar_direction(None) == "Auto"
    assert c.normalize_bar_spacing(999) == c.MAX_BAR_SPACING
    assert c.normalize_bar_spacing(-5) == c.MIN_BAR_SPACING
    assert c.normalize_bar_spacing(float("nan")) == c.DEFAULT_BAR_SPACING
