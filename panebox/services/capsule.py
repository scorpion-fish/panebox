"""Capsule (compact/collapsed) widget policies — ports of
Services/WidgetCollapseBehavior.cs, WidgetCompactBoundsCalculator.cs,
WidgetCompactExpansionCalculator.cs, WidgetCompactExpansionDirectionPolicy.cs,
WidgetCompactInteractionPolicy.cs, WidgetCompactPrivacyPolicy.cs,
WidgetCompactTransitionPolicy.cs, WidgetCapsuleOrderCalculator.cs and
WidgetCapsuleArrangementCalculator.cs.

Pure logic, no GTK: geometry works on the Rect/Point/Size dataclasses below
(device pixels, like the C# RectInt32). Normalizers mirror the SettingsService
ones (same defaults, case-insensitive, unknown → default).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional, Sequence

# ---- geometry -------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    x: int = 0
    y: int = 0


@dataclass(frozen=True)
class Size:
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class Rect:
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


# ---- setting normalizers (SettingsService capsule family) ------------------------

CONTENT_MODE_SMART = "Smart"
CONTENT_MODE_MINIMAL = "Minimal"
CONTENT_MODE_SUMMARY = "Summary"

WIDTH_MODE_ALIGNED = "Aligned"
WIDTH_MODE_INDEPENDENT = "Independent"

EXPANSION_AUTO = "Auto"
EXPANSION_DOWN = "Down"
EXPANSION_UP = "Up"

ARRANGEMENT_FREE = "Free"
ARRANGEMENT_BAR = "Bar"

BAR_PLACEMENT_FLOATING = "Floating"
BAR_PLACEMENT_TOP = "Top"
BAR_PLACEMENT_BOTTOM = "Bottom"
BAR_PLACEMENT_LEFT = "Left"
BAR_PLACEMENT_RIGHT = "Right"

BAR_DIRECTION_AUTO = "Auto"
BAR_DIRECTION_HORIZONTAL = "Horizontal"
BAR_DIRECTION_VERTICAL = "Vertical"

DEFAULT_BAR_SPACING = 8.0
MIN_BAR_SPACING = 0.0
MAX_BAR_SPACING = 64.0

POSITION_ANCHORS = (
    "LeftTop",
    "RightTop",
    "LeftBottom",
    "RightBottom",
)


def normalize_content_mode(value: Optional[str]) -> str:
    if isinstance(value, str) and value.lower() == CONTENT_MODE_MINIMAL.lower():
        return CONTENT_MODE_MINIMAL
    if isinstance(value, str) and value.lower() == CONTENT_MODE_SUMMARY.lower():
        return CONTENT_MODE_SUMMARY
    return CONTENT_MODE_SMART


def normalize_width_mode(value: Optional[str]) -> str:
    if isinstance(value, str) and value.lower() == WIDTH_MODE_INDEPENDENT.lower():
        return WIDTH_MODE_INDEPENDENT
    return WIDTH_MODE_ALIGNED


def normalize_expansion_direction(value: Optional[str]) -> str:
    if isinstance(value, str) and value.lower() == EXPANSION_DOWN.lower():
        return EXPANSION_DOWN
    if isinstance(value, str) and value.lower() == EXPANSION_UP.lower():
        return EXPANSION_UP
    return EXPANSION_AUTO


def normalize_arrangement_mode(value: Optional[str]) -> str:
    if isinstance(value, str) and value.lower() in (
        "bar",
        "horizontal",
        "vertical",
    ):
        return ARRANGEMENT_BAR
    return ARRANGEMENT_FREE


def normalize_bar_placement(value: Optional[str]) -> str:
    if isinstance(value, str):
        lowered = value.lower()
        for known in (
            BAR_PLACEMENT_TOP,
            BAR_PLACEMENT_BOTTOM,
            BAR_PLACEMENT_LEFT,
            BAR_PLACEMENT_RIGHT,
        ):
            if lowered == known.lower():
                return known
    return BAR_PLACEMENT_FLOATING


def normalize_bar_direction(value: Optional[str]) -> str:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == BAR_DIRECTION_HORIZONTAL.lower():
            return BAR_DIRECTION_HORIZONTAL
        if lowered == BAR_DIRECTION_VERTICAL.lower():
            return BAR_DIRECTION_VERTICAL
    return BAR_DIRECTION_AUTO


def normalize_bar_spacing(value: float) -> float:
    finite = value if math.isfinite(value) else DEFAULT_BAR_SPACING
    return min(max(finite, MIN_BAR_SPACING), MAX_BAR_SPACING)


# ---- WidgetCollapseBehavior -------------------------------------------------------

COLLAPSE_SYSTEM = "System"
COLLAPSE_EXPANDED = "Expanded"
COLLAPSE_CLICK = "Click"
COLLAPSE_SMART = "Smart"

COLLAPSE_METADATA_KEY = "CollapseBehavior"


def normalize_collapse_behavior(
    value: Optional[str], fallback: str = COLLAPSE_CLICK, allow_system: bool = False
) -> str:
    if isinstance(value, str) and value.lower() == "manual":
        return COLLAPSE_CLICK
    if isinstance(value, str) and value.lower() == "auto":
        return COLLAPSE_SMART
    known = {
        COLLAPSE_SYSTEM.lower(),
        COLLAPSE_EXPANDED.lower(),
        COLLAPSE_CLICK.lower(),
        COLLAPSE_SMART.lower(),
    }
    if isinstance(value, str) and value.lower() in known:
        behavior = value[0].upper() + value[1:] if value else value
        if behavior == COLLAPSE_SYSTEM and not allow_system:
            return fallback
        return behavior
    return fallback


def collapse_override(config) -> str:
    """Per-widget override from config.metadata; System = follow global."""
    raw = (config.metadata or {}).get(COLLAPSE_METADATA_KEY) if config is not None else None
    if raw is None:
        return COLLAPSE_SYSTEM
    return normalize_collapse_behavior(raw, fallback=COLLAPSE_SYSTEM, allow_system=True)


def resolve_collapse_behavior(config, global_value: Optional[str]) -> str:
    override = collapse_override(config)
    if override == COLLAPSE_SYSTEM:
        return normalize_collapse_behavior(global_value)
    return override


def set_collapse_override(config, behavior: str) -> None:
    if behavior == COLLAPSE_SYSTEM:
        config.metadata.pop(COLLAPSE_METADATA_KEY, None)
        return
    config.metadata[COLLAPSE_METADATA_KEY] = behavior


# ---- WidgetCompactPrivacyPolicy ---------------------------------------------------

PRIVACY_KINDS = ("File", "Todo", "QuickCapture", "Music")
SMART_DETAIL_KINDS = ("File", "Todo", "QuickCapture")


def hides_sensitive_content(enabled: bool, widget_kind: str) -> bool:
    return enabled and widget_kind in PRIVACY_KINDS


def resolve_privacy_content_mode(content_mode: Optional[str], enabled: bool, widget_kind: str) -> str:
    normalized = normalize_content_mode(content_mode)
    hides_smart_detail = (
        hides_sensitive_content(enabled, widget_kind)
        and normalized == CONTENT_MODE_SMART
        and widget_kind in SMART_DETAIL_KINDS
    )
    return CONTENT_MODE_SUMMARY if hides_smart_detail else normalized


# ---- WidgetCompactTransitionPolicy ------------------------------------------------

TRANSITION_INTERACTION = "Interaction"
TRANSITION_BEHAVIOR_DISABLED = "CollapseBehaviorDisabled"


def resolve_transition_reason(previous: str, current: str) -> str:
    if previous != COLLAPSE_EXPANDED and current == COLLAPSE_EXPANDED:
        return TRANSITION_BEHAVIOR_DISABLED
    return TRANSITION_INTERACTION


def should_capture_compact_placement(
    reason: str, was_target_collapsed: bool, compact_bounds_active: bool, transition_active: bool
) -> bool:
    return reason == TRANSITION_INTERACTION and was_target_collapsed and compact_bounds_active and not transition_active


# ---- WidgetCompactBoundsCalculator ------------------------------------------------

MIN_WIDTH = 144.0
MAX_WIDTH = 480.0
MAX_ALIGNED_WIDTH = 1200.0
MINIMAL_WIDTH = 172.0
SUMMARY_WIDTH = 248.0
SMART_WIDTH = 272.0
SMART_MEDIA_WIDTH = 320.0
CAPSULE_HEIGHT = 42.0
SMART_DETAIL_HEIGHT = 52.0
STANDARD_WIDTH_THRESHOLD = 210.0
WIDE_WIDTH_THRESHOLD = 300.0

WIDTH_TIERS = ("Narrow", "Standard", "Wide")


def clamp_logical_width(width: float) -> float:
    finite = width if math.isfinite(width) else SUMMARY_WIDTH
    return min(max(finite, MIN_WIDTH), MAX_WIDTH)


def clamp_aligned_logical_width(width: float) -> float:
    finite = width if math.isfinite(width) else SUMMARY_WIDTH
    return min(max(finite, MIN_WIDTH), MAX_ALIGNED_WIDTH)


def resolve_logical_width(
    content_mode: Optional[str],
    widget_kind: str = "File",
    compact_width: Optional[float] = None,
    clamp_custom_width: bool = True,
) -> float:
    if compact_width is not None and math.isfinite(compact_width):
        return clamp_logical_width(compact_width) if clamp_custom_width else clamp_aligned_logical_width(compact_width)
    # Raw ordinal comparison, like the C# (normalized values come in from the
    # settings loader; anything else — including None — reads as Summary).
    if content_mode == CONTENT_MODE_MINIMAL:
        return MINIMAL_WIDTH
    if content_mode == CONTENT_MODE_SMART:
        return SMART_MEDIA_WIDTH if widget_kind == "Music" else SMART_WIDTH
    return SUMMARY_WIDTH


def resolve_logical_height(content_mode: Optional[str], widget_kind: str = "File") -> float:
    # Smart is the shared high-density capsule treatment: one outer height for
    # every kind so mixed capsule bars don't alternate short/tall.
    _ = widget_kind
    return SMART_DETAIL_HEIGHT if content_mode == CONTENT_MODE_SMART else CAPSULE_HEIGHT


def resolve_width_tier(logical_width: float) -> str:
    width = logical_width if math.isfinite(logical_width) else SUMMARY_WIDTH
    if width < STANDARD_WIDTH_THRESHOLD:
        return "Narrow"
    return "Standard" if width < WIDE_WIDTH_THRESHOLD else "Wide"


def _anchor_flags(position_anchor: Optional[str]) -> tuple[bool, bool]:
    anchor_right = position_anchor in ("RightTop", "RightBottom")
    anchor_bottom = position_anchor in ("LeftBottom", "RightBottom")
    return anchor_right, anchor_bottom


def calculate_compact_bounds(
    expanded_bounds: Rect,
    position_anchor: Optional[str],
    dpi_scale: float,
    content_mode: Optional[str],
    widget_kind: str = "File",
    compact_width: Optional[float] = None,
    clamp_custom_width: bool = True,
    height_content_mode: Optional[str] = None,
) -> Rect:
    scale = dpi_scale if (isinstance(dpi_scale, (int, float)) and math.isfinite(dpi_scale) and dpi_scale > 0) else 1.0
    logical_width = resolve_logical_width(content_mode, widget_kind, compact_width, clamp_custom_width)
    width = max(1, round(logical_width * scale))
    height = max(1, round(resolve_logical_height(height_content_mode or content_mode, widget_kind) * scale))
    anchor_right, anchor_bottom = _anchor_flags(position_anchor)
    x = expanded_bounds.right - width if anchor_right else expanded_bounds.x
    y = expanded_bounds.bottom - height if anchor_bottom else expanded_bounds.y
    return Rect(x, y, width, height)


def resolve_compact_bounds(
    config,
    expanded_bounds: Rect,
    dpi_scale: float,
    content_mode: Optional[str],
    align_to_expanded_width: bool = False,
    height_content_mode: Optional[str] = None,
) -> Rect:
    """Bounds for a capsule; a captured CompactPlacement wins over the
    expanded-geometry derivation (the C# Resolve overload)."""
    compact_width = config.width if align_to_expanded_width else config.compactWidth
    placement = config.compactPlacement
    if placement is None:
        return calculate_compact_bounds(
            expanded_bounds,
            config.positionAnchor,
            dpi_scale,
            content_mode,
            config.normalized_kind(),
            compact_width,
            clamp_custom_width=not align_to_expanded_width,
            height_content_mode=height_content_mode,
        )
    logical_width = resolve_logical_width(
        content_mode, config.normalized_kind(), compact_width, not align_to_expanded_width
    )
    logical_height = resolve_logical_height(height_content_mode or content_mode, config.normalized_kind())
    return apply_compact_size_to_placement(
        Rect(
            int(placement.x), int(placement.y), 0, 0
        ),  # resolved placement bounds (anchor-aware coords already applied)
        placement.positionAnchor,
        dpi_scale,
        logical_width,
        logical_height,
        clamp_logical_width_flag=not align_to_expanded_width,
    )


def apply_compact_size_to_placement(
    resolved_bounds: Rect,
    position_anchor: Optional[str],
    dpi_scale: float,
    logical_width: float,
    logical_height: float = CAPSULE_HEIGHT,
    clamp_logical_width_flag: bool = True,
) -> Rect:
    scale = dpi_scale if (isinstance(dpi_scale, (int, float)) and math.isfinite(dpi_scale) and dpi_scale > 0) else 1.0
    normalized_width = (
        clamp_logical_width(logical_width) if clamp_logical_width_flag else clamp_aligned_logical_width(logical_width)
    )
    width = max(1, round(normalized_width * scale))
    height = max(1, round(max(1.0, logical_height) * scale))
    anchor_right, anchor_bottom = _anchor_flags(position_anchor)
    x = resolved_bounds.right - width if anchor_right else resolved_bounds.x
    y = resolved_bounds.bottom - height if anchor_bottom else resolved_bounds.y
    return Rect(x, y, width, height)


def apply_size_to_stable_placement(
    stable_bounds: Rect, width: int, height: int, position_anchor: Optional[str]
) -> Rect:
    safe_width, safe_height = max(1, width), max(1, height)
    anchor_right, anchor_bottom = _anchor_flags(position_anchor)
    return Rect(
        stable_bounds.right - safe_width if anchor_right else stable_bounds.x,
        stable_bounds.bottom - safe_height if anchor_bottom else stable_bounds.y,
        safe_width,
        safe_height,
    )


def anchor_expanded_bounds_to_compact(
    compact_bounds: Rect,
    expanded_bounds: Rect,
    position_anchor: Optional[str],
    work_area: Rect,
) -> Rect:
    anchor = from_position_anchor(position_anchor) or "LeftTop"
    layout = resolve_expansion(
        compact_bounds,
        Size(expanded_bounds.width, expanded_bounds.height),
        work_area,
        [anchor],
    )
    return layout.expanded_bounds


def resolve_expanded_size_for_width_mode(compact_bounds: Rect, expanded_size: Size, width_mode: Optional[str]) -> Size:
    width = (
        max(1, compact_bounds.width)
        if normalize_width_mode(width_mode) == WIDTH_MODE_ALIGNED
        else max(1, expanded_size.width)
    )
    return Size(width, max(1, expanded_size.height))


# ---- WidgetCompactExpansionCalculator ---------------------------------------------

EXPANSION_ANCHORS = ("LeftTop", "RightTop", "LeftBottom", "RightBottom")
DEFAULT_ANCHOR_ORDER = ("LeftTop", "RightTop", "LeftBottom", "RightBottom")


@dataclass(frozen=True)
class ExpansionLayout:
    anchor: str
    pivot: Point
    expanded_bounds: Rect
    requested_size: Size
    is_size_constrained: bool
    can_expand: bool = True


def bounds_from_pivot(pivot: Point, size: Size, anchor: str) -> Rect:
    width, height = max(1, size.width), max(1, size.height)
    anchors_right = anchor in ("RightTop", "RightBottom")
    anchors_bottom = anchor in ("LeftBottom", "RightBottom")
    return Rect(
        pivot.x - width if anchors_right else pivot.x,
        pivot.y - height if anchors_bottom else pivot.y,
        width,
        height,
    )


def pivot_of(bounds: Rect, anchor: str) -> Point:
    anchors_right = anchor in ("RightTop", "RightBottom")
    anchors_bottom = anchor in ("LeftBottom", "RightBottom")
    return Point(
        bounds.right if anchors_right else bounds.x,
        bounds.bottom if anchors_bottom else bounds.y,
    )


def interpolate_anchored_bounds(from_bounds: Rect, to_bounds: Rect, pivot: Point, anchor: str, progress: float) -> Rect:
    value = min(max(progress, 0.0), 1.0)
    width = max(1, round(from_bounds.width + (to_bounds.width - from_bounds.width) * value))
    height = max(1, round(from_bounds.height + (to_bounds.height - from_bounds.height) * value))
    return bounds_from_pivot(pivot, Size(width, height), anchor)


def from_position_anchor(anchor: Optional[str]) -> Optional[str]:
    return anchor if anchor in EXPANSION_ANCHORS else None


def resolve_horizontal_resize_anchor(current_anchor: str, resize_direction: Optional[str]) -> str:
    anchors_bottom = current_anchor in ("LeftBottom", "RightBottom")
    if resize_direction == "Right":
        return "LeftBottom" if anchors_bottom else "LeftTop"
    if resize_direction == "Left":
        return "RightBottom" if anchors_bottom else "RightTop"
    return current_anchor


def _is_inside(bounds: Rect, work_area: Rect) -> bool:
    return (
        bounds.x >= work_area.x
        and bounds.y >= work_area.y
        and bounds.right <= work_area.right
        and bounds.bottom <= work_area.bottom
    )


def resolve_expansion(
    compact_bounds: Rect,
    requested_size: Size,
    work_area: Rect,
    anchor_order: Optional[Sequence[str]] = None,
    require_full_size: bool = False,
) -> ExpansionLayout:
    """Pick the anchor that retains the most of the requested size; ties break
    by preference order. Never hard-fails — a non-fitting size is clamped into
    the work area and flagged."""
    original_width = max(1, requested_size.width)
    original_height = max(1, requested_size.height)
    width = min(original_width, max(1, work_area.width))
    height = min(original_height, max(1, work_area.height))
    normalized = Size(width, height)
    candidates = list(dict.fromkeys(anchor_order)) if anchor_order else list(DEFAULT_ANCHOR_ORDER)

    def candidate(anchor: str, preference_index: int):
        pivot = pivot_of(compact_bounds, anchor)
        work_right = work_area.x + max(1, work_area.width)
        work_bottom = work_area.y + max(1, work_area.height)
        anchors_right = anchor in ("RightTop", "RightBottom")
        anchors_bottom = anchor in ("LeftBottom", "RightBottom")
        available_width = pivot.x - work_area.x if anchors_right else work_right - pivot.x
        available_height = pivot.y - work_area.y if anchors_bottom else work_bottom - pivot.y
        cwidth = max(1, min(normalized.width, available_width))
        cheight = max(1, min(normalized.height, available_height))
        bounds = bounds_from_pivot(pivot, Size(cwidth, cheight), anchor)
        fits = cwidth == normalized.width and cheight == normalized.height
        retained = cwidth * cheight
        return anchor, pivot, bounds, fits, retained, preference_index

    def better(a, b) -> bool:
        # a is better than b
        if a[3] != b[3]:
            return a[3]
        if not a[3] and a[4] != b[4]:
            return a[4] > b[4]
        return a[5] < b[5]

    best = None
    for index, anchor in enumerate(candidates):
        entry = candidate(anchor, index)
        if best is None or better(entry, best):
            best = entry
    resolved = best if best is not None else candidate("LeftTop", 0)

    fits_requested = (
        resolved[2].width == original_width
        and resolved[2].height == original_height
        and _is_inside(resolved[2], work_area)
    )
    can_expand = not require_full_size or fits_requested
    return ExpansionLayout(
        anchor=resolved[0],
        pivot=resolved[1],
        expanded_bounds=resolved[2] if can_expand else compact_bounds,
        requested_size=Size(original_width, original_height),
        is_size_constrained=not fits_requested,
        can_expand=can_expand,
    )


# ---- WidgetCompactExpansionDirectionPolicy ----------------------------------------

EXPANSION_DIRECTION_METADATA_KEY = "CompactExpansionDirection"


def expansion_direction_override(config) -> Optional[str]:
    raw = (config.metadata or {}).get(EXPANSION_DIRECTION_METADATA_KEY) if config is not None else None
    if raw is None:
        return None
    for known in (EXPANSION_AUTO, EXPANSION_DOWN, EXPANSION_UP):
        if raw.lower() == known.lower():
            return known
    return None


def set_expansion_direction_override(config, direction: Optional[str]) -> None:
    if direction is None:
        config.metadata.pop(EXPANSION_DIRECTION_METADATA_KEY, None)
        return
    config.metadata[EXPANSION_DIRECTION_METADATA_KEY] = normalize_expansion_direction(direction)


def resolve_effective_expansion_direction(config, global_direction: Optional[str]) -> str:
    override = expansion_direction_override(config)
    return override if override is not None else normalize_expansion_direction(global_direction)


def direction_requires_full_size(configured_direction: Optional[str]) -> bool:
    return normalize_expansion_direction(configured_direction) != EXPANSION_AUTO


def apply_expansion_direction(configured_direction: Optional[str], anchors: Sequence[str]) -> list[str]:
    """Constrain candidate anchors to a fixed expansion direction."""
    direction = normalize_expansion_direction(configured_direction)
    if direction == EXPANSION_AUTO:
        return list(anchors)
    expands_down = direction == EXPANSION_DOWN
    constrained: list[str] = []
    for anchor in anchors:
        if anchor in ("RightTop", "RightBottom"):
            mapped = "RightTop" if expands_down else "RightBottom"
        else:
            mapped = "LeftTop" if expands_down else "LeftBottom"
        if mapped not in constrained:
            constrained.append(mapped)
    if not constrained:
        constrained.append("LeftTop" if expands_down else "LeftBottom")
        constrained.append("RightTop" if expands_down else "RightBottom")
    return constrained


def resolve_adaptive_expansion(
    compact_bounds: Rect,
    requested_size: Size,
    work_area: Rect,
    anchors: Sequence[str],
    configured_direction: Optional[str],
) -> ExpansionLayout:
    """Fixed direction keeps its direction and only adapts size; automatic may
    switch anchors. Expansion never hard-fails for lack of room."""
    constrained = apply_expansion_direction(configured_direction, anchors)
    strict = resolve_expansion(
        compact_bounds,
        requested_size,
        work_area,
        constrained,
        require_full_size=direction_requires_full_size(configured_direction),
    )
    if strict.can_expand:
        return strict
    return resolve_expansion(compact_bounds, requested_size, work_area, constrained)


# ---- WidgetCompactInteractionPolicy ------------------------------------------------

INTERACTION_REGION_HOVER_DELAY_FLOOR_MS = 620


@dataclass(frozen=True)
class InteractionSnapshot:
    is_collapsed: bool = False
    is_pinned: bool = False
    is_pointer_inside: bool = False
    is_expansion_zone_active: bool = False
    is_pointer_over_move_handle: bool = False
    is_pointer_over_actions: bool = False
    is_drop_inside: bool = False
    is_bounds_interaction_active: bool = False
    interaction_depth: int = 0
    is_dragging: bool = False
    is_resizing: bool = False
    has_blocking_surface: bool = False
    suppress_hover_expansion: bool = False

    @property
    def has_active_interaction(self) -> bool:
        return (
            self.is_drop_inside
            or self.is_bounds_interaction_active
            or self.interaction_depth > 0
            or self.is_dragging
            or self.is_resizing
            or self.has_blocking_surface
        )


def synchronize_smart_entry(snapshot: InteractionSnapshot, is_pointer_physically_inside: bool) -> InteractionSnapshot:
    """Routed pointer events belong to the previous expanded layout; only the
    native cursor position is authoritative when Smart mode is entered."""
    return replace(
        snapshot,
        is_pointer_inside=is_pointer_physically_inside,
        is_expansion_zone_active=False,
        is_pointer_over_move_handle=False,
        is_pointer_over_actions=False,
        suppress_hover_expansion=False,
    )


def can_hover_expand(
    behavior: str, snapshot: InteractionSnapshot, allow_interaction_region_dwell: bool = False
) -> bool:
    # The left identity strip is a move/drag affordance and never expands.
    has_eligible_hover_intent = (
        snapshot.is_pointer_inside
        and not snapshot.is_pointer_over_move_handle
        and (
            allow_interaction_region_dwell
            or (snapshot.is_expansion_zone_active and not snapshot.is_pointer_over_actions)
        )
    )
    return (
        behavior == COLLAPSE_SMART
        and snapshot.is_collapsed
        and snapshot.is_pointer_inside
        and has_eligible_hover_intent
        and not snapshot.is_drop_inside
        and not snapshot.has_active_interaction
        and not snapshot.suppress_hover_expansion
    )


def resolve_hover_expand_delay_ms(configured_ms: int, allow_interaction_region_dwell: bool) -> int:
    if allow_interaction_region_dwell:
        return max(configured_ms, INTERACTION_REGION_HOVER_DELAY_FLOOR_MS)
    return configured_ms


def can_auto_collapse(behavior: str, snapshot: InteractionSnapshot) -> bool:
    return (
        behavior == COLLAPSE_SMART
        and not snapshot.is_collapsed
        and not snapshot.is_pinned
        and not snapshot.is_pointer_inside
        and not snapshot.has_active_interaction
    )


def should_retry_auto_collapse(behavior: str, snapshot: InteractionSnapshot) -> bool:
    """Pointer presence and interactions are transient blockers; keep probing
    or a missed exit event strands a Smart widget open forever."""
    return behavior == COLLAPSE_SMART and not snapshot.is_collapsed and not snapshot.is_pinned


VIEW_STATES = ("Glance", "Peek", "Open", "Pinned")


def resolve_view_state(behavior: str, snapshot: InteractionSnapshot) -> str:
    if snapshot.is_collapsed:
        return "Glance"
    if snapshot.is_pinned:
        return "Pinned"
    if behavior != COLLAPSE_SMART or snapshot.has_active_interaction:
        return "Open"
    return "Peek"


def resolve_resize_direction(is_compact_bounds_active: bool, direction: Optional[str]) -> str:
    resolved = direction or ""
    if not is_compact_bounds_active:
        return resolved
    # Capsule end caps overlap the corner resize targets: treat any point on a
    # cap as horizontal resizing.
    if "Left" in resolved:
        return "Left"
    return "Right" if "Right" in resolved else ""


def can_resize(is_compact_transition_active: bool, is_compact_bounds_active: bool, direction: Optional[str]) -> bool:
    if is_compact_transition_active:
        return False
    return not is_compact_bounds_active or resolve_resize_direction(is_compact_bounds_active, direction) in (
        "Left",
        "Right",
    )


# ---- WidgetCapsuleOrderCalculator ---------------------------------------------------


def _primary_center(bounds: Rect, vertical: bool) -> int:
    return (bounds.y * 2 + bounds.height) if vertical else (bounds.x * 2 + bounds.width)


def move_to_nearest_slot(
    ordered_ids: Sequence[str],
    slot_bounds: Sequence[Rect],
    active_id: str,
    proposed_bounds: Rect,
    direction: Optional[str],
) -> list[str]:
    """Reorder bar order when a dragged capsule is dropped near another slot."""
    try:
        active_index = list(ordered_ids).index(active_id)
    except ValueError:
        return list(ordered_ids)
    if active_index < 0 or len(ordered_ids) < 2:
        return list(ordered_ids)
    vertical = normalize_bar_direction(direction) == BAR_DIRECTION_VERTICAL
    proposed_center = _primary_center(proposed_bounds, vertical)
    nearest_index = active_index
    nearest_distance = None
    for index in range(min(len(ordered_ids), len(slot_bounds))):
        distance = abs(proposed_center - _primary_center(slot_bounds[index], vertical))
        if nearest_distance is None or distance < nearest_distance:
            nearest_distance = distance
            nearest_index = index
    if nearest_index == active_index:
        return list(ordered_ids)
    reordered = list(ordered_ids)
    reordered.pop(active_index)
    reordered.insert(nearest_index, active_id)
    return reordered


def merge_group_order(complete_order: Sequence[str], group_order: Sequence[str]) -> list[str]:
    group_ids = set(group_order)
    result = list(complete_order)
    group_index = 0
    for index in range(len(result)):
        if group_index >= len(group_order):
            break
        if result[index] in group_ids:
            result[index] = group_order[group_index]
            group_index += 1
    return result


# ---- WidgetCapsuleArrangementCalculator ---------------------------------------------

_HORIZONTAL_MIN_WIDTH = 96
_VERTICAL_MIN_HEIGHT = 36


@dataclass(frozen=True)
class CapsuleArrangementItem:
    id: str
    width: int
    height: int


def _fit_primary_sizes(
    requested_sizes: Sequence[int],
    available_length: int,
    requested_spacing: int,
    preferred_minimum: int,
) -> tuple[int, list[int]]:
    count = len(requested_sizes)
    safe_available = max(1, available_length)
    safe_spacing = max(0, requested_spacing)
    if count > 1:
        safe_spacing = min(safe_spacing, safe_available // (count - 1))
    sizes = [max(1, size) for size in requested_sizes]
    available_for_items = max(1, safe_available - safe_spacing * max(0, count - 1))
    if sum(sizes) <= available_for_items:
        return safe_spacing, sizes

    effective_minimum = min(preferred_minimum, max(1, available_for_items // count))
    scale = available_for_items / max(1, sum(sizes))
    sizes = [max(effective_minimum, int(size * scale // 1)) for size in sizes]

    excess = sum(sizes) - available_for_items
    while excess > 0:
        reduced = False
        for index in range(len(sizes) - 1, -1, -1):
            if excess <= 0:
                break
            if sizes[index] <= 1:
                continue
            sizes[index] -= 1
            excess -= 1
            reduced = True
        if not reduced:
            break

    remaining = available_for_items - sum(sizes)
    index = 0
    while remaining > 0:
        sizes[index] += 1
        remaining -= 1
        index = (index + 1) % len(sizes)
    return safe_spacing, sizes


def calculate_capsule_arrangement(
    items: Sequence[CapsuleArrangementItem],
    work_area: Rect,
    anchor_point: Point,
    position_anchor: Optional[str],
    direction: Optional[str],
    spacing: int,
) -> dict[str, Rect]:
    """Slot geometry for one capsule bar (single row or single column)."""
    results: dict[str, Rect] = {}
    if not items or work_area.width <= 0 or work_area.height <= 0:
        return results
    vertical = normalize_bar_direction(direction) == BAR_DIRECTION_VERTICAL
    anchor_right, anchor_bottom = _anchor_flags(position_anchor)
    safe_anchor = Point(
        min(max(anchor_point.x, work_area.x), work_area.right),
        min(max(anchor_point.y, work_area.y), work_area.bottom),
    )
    if vertical:
        common_width = min(work_area.width, max(max(1, item.width) for item in items))
        safe_spacing, heights = _fit_primary_sizes(
            [item.height for item in items], work_area.height, spacing, _VERTICAL_MIN_HEIGHT
        )
        cursor_y = safe_anchor.y
        for index, item in enumerate(items):
            height = heights[index]
            x = safe_anchor.x - common_width if anchor_right else safe_anchor.x
            y = cursor_y - height if anchor_bottom else cursor_y
            results[item.id] = Rect(x, y, common_width, height)
            cursor_y = y - safe_spacing if anchor_bottom else y + height + safe_spacing
    else:
        common_height = min(work_area.height, max(max(1, item.height) for item in items))
        safe_spacing, widths = _fit_primary_sizes(
            [item.width for item in items], work_area.width, spacing, _HORIZONTAL_MIN_WIDTH
        )
        cursor_x = safe_anchor.x
        for index, item in enumerate(items):
            width = widths[index]
            x = cursor_x - width if anchor_right else cursor_x
            y = safe_anchor.y - common_height if anchor_bottom else safe_anchor.y
            results[item.id] = Rect(x, y, width, common_height)
            cursor_x = x - safe_spacing if anchor_right else x + width + safe_spacing

    _clamp_group_into_work_area(results, work_area)
    return results


def _clamp_group_into_work_area(results: dict[str, Rect], work_area: Rect) -> None:
    if not results:
        return
    min_x = min(b.x for b in results.values())
    min_y = min(b.y for b in results.values())
    max_x = max(b.right for b in results.values())
    max_y = max(b.bottom for b in results.values())
    offset_x = (
        work_area.x - min_x if min_x < work_area.x else (work_area.right - max_x if max_x > work_area.right else 0)
    )
    offset_y = (
        work_area.y - min_y if min_y < work_area.y else (work_area.bottom - max_y if max_y > work_area.bottom else 0)
    )
    if offset_x == 0 and offset_y == 0:
        return
    for key in list(results):
        bounds = results[key]
        results[key] = Rect(bounds.x + offset_x, bounds.y + offset_y, bounds.width, bounds.height)


# ---- placement capture (CompactPlacement persistence) --------------------------------


def capture_compact_placement(config, bounds: Rect, work_area: Rect, dpi_scale: float) -> None:
    """Store the capsule position the same way expanded anchors are stored:
    anchor + margins against the work area so it survives topology changes."""
    from ..models.widget_config import WidgetCompactPlacement

    scale = dpi_scale if (isinstance(dpi_scale, (int, float)) and math.isfinite(dpi_scale) and dpi_scale > 0) else 1.0

    anchor_right, anchor_bottom = _anchor_flags(config.positionAnchor)
    margin_x = work_area.right - bounds.right if anchor_right else bounds.x - work_area.x
    margin_y = work_area.bottom - bounds.bottom if anchor_bottom else bounds.y - work_area.y

    config.compactPlacement = WidgetCompactPlacement(
        x=bounds.x / scale,
        y=bounds.y / scale,
        positionAnchor=config.positionAnchor,
        positionMarginX=margin_x / scale,
        positionMarginY=margin_y / scale,
        positionMonitorKey=config.positionMonitorKey,
        positionMonitorDeviceName=config.positionMonitorDeviceName,
        positionMonitorWasPrimary=config.positionMonitorWasPrimary,
    )
