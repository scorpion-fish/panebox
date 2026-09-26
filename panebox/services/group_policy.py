"""Widget group policies (port of WidgetGroupSettings / WidgetGroupOrder /
WidgetGroupChromePolicy / WidgetGroupNavigationInteractionPolicy and the
layout-sync helpers of WidgetManager.Groups.cs).

Pure logic only: everything here works on the settings/layout models and is
unit-testable without a window. The interactive half (merge/switch/detach
against live runtimes) lives in WidgetManager.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from ..models.layout_slice import WidgetLayoutSettingsSlice
from ..models.widget_config import WidgetConfig
from ..models.widget_group_config import NavigationStyle, TitleDisplayMode, WidgetGroupConfig
from . import capsule as capsule_policy

MAXIMUM_MEMBER_COUNT = 8  # WidgetGroupSettings.MaximumMemberCount

DETACH_CASCADE_STEP_DIP = 24.0
DETACH_CASCADE_MAX_INDEX = 5

# WheelSwitch defaults: Tabs groups pin wheel switching off (the wheel scrolls
# the tab row); Stack groups follow the global default (on).
WHEEL_STEP = 120.0
WHEEL_GESTURE_QUIET_PERIOD = timedelta(milliseconds=220)


# ---- style normalizers (WidgetGroupNavigationStyles / TitleDisplayModes) -------


def normalize_navigation_style(value: Optional[str], allow_follow_default: bool = False) -> str:
    if value == NavigationStyle.FOLLOW_DEFAULT and allow_follow_default:
        return NavigationStyle.FOLLOW_DEFAULT
    if value == NavigationStyle.STACK or value == "Auto":  # Auto is the legacy name
        return NavigationStyle.STACK
    return NavigationStyle.TABS


def normalize_title_display_mode(value: Optional[str], allow_follow_default: bool = False) -> str:
    if value == TitleDisplayMode.FOLLOW_DEFAULT and allow_follow_default:
        return TitleDisplayMode.FOLLOW_DEFAULT
    if value in (TitleDisplayMode.ICON_ONLY, TitleDisplayMode.TEXT_ONLY):
        return value
    return TitleDisplayMode.ICON_AND_TEXT


# ---- membership lookups (WidgetGroupSettings) ----------------------------------


def find_group_by_member(layout: WidgetLayoutSettingsSlice, widget_id: str) -> Optional[WidgetGroupConfig]:
    if not widget_id:
        return None
    for group in layout.widgetGroups:
        if widget_id in group.memberIds:
            return group
    return None


def is_active_member(layout: WidgetLayoutSettingsSlice, widget_id: str) -> bool:
    group = find_group_by_member(layout, widget_id)
    return group is None or group.activeMemberId == widget_id


def resolve_restorable_active_member_id(
    layout: WidgetLayoutSettingsSlice,
    group: WidgetGroupConfig,
    is_available: Callable[[WidgetConfig], bool],
) -> Optional[str]:
    """The persisted active member wins when restorable this session;
    otherwise the first available member — without mutating membership."""
    configs = {config.id: config for config in layout.widgets}
    active = configs.get(group.activeMemberId)
    if active is not None and active.id in group.memberIds and is_available(active):
        return active.id
    for member_id in group.memberIds:
        member = configs.get(member_id)
        if member is not None and is_available(member):
            return member.id
    return None


def _create_unique_id(claimed: set) -> str:
    while True:
        candidate = str(uuid.uuid4())
        if candidate not in claimed:
            claimed.add(candidate)
            return candidate


def normalize_groups(layout: WidgetLayoutSettingsSlice) -> bool:
    """WidgetGroupSettings.Normalize — repair corrupt/partial group state
    before any window is created. Returns True when something changed."""
    changed = False

    default_style = normalize_navigation_style(layout.widgetGroupDefaultNavigationStyle)
    default_title = normalize_title_display_mode(layout.widgetGroupDefaultTitleDisplayMode)
    if layout.widgetGroupDefaultNavigationStyle != default_style:
        layout.widgetGroupDefaultNavigationStyle = default_style
        changed = True
    if layout.widgetGroupDefaultTitleDisplayMode != default_title:
        layout.widgetGroupDefaultTitleDisplayMode = default_title
        changed = True

    deleted = set(layout.deletedWidgetIds)
    valid_ids = {config.id for config in layout.widgets if config.id and config.id not in deleted}
    claimed_members: set = set()
    group_ids: set = set()
    surface_ids: set = set()
    configs_by_id = {config.id: config for config in layout.widgets}
    normalized: List[WidgetGroupConfig] = []

    for candidate in layout.widgetGroups:
        if not candidate.memberIds:
            candidate.memberIds = []
        if not candidate.id or candidate.id in group_ids:
            candidate.id = _create_unique_id(group_ids)
            changed = True
        group_ids.add(candidate.id)
        if not candidate.surfaceId or candidate.surfaceId in surface_ids:
            candidate.surfaceId = _create_unique_id(surface_ids)
            changed = True
        surface_ids.add(candidate.surfaceId)

        members: List[str] = []
        for member_id in candidate.memberIds:
            if member_id in valid_ids and member_id not in claimed_members and member_id not in members:
                members.append(member_id)
                claimed_members.add(member_id)
            if len(members) >= MAXIMUM_MEMBER_COUNT:
                break
        if list(candidate.memberIds) != members:
            candidate.memberIds = members
            changed = True

        if len(candidate.memberIds) < 2:
            for member_id in candidate.memberIds:
                claimed_members.discard(member_id)
            changed = True
            continue  # group dropped

        if candidate.activeMemberId not in candidate.memberIds:
            candidate.activeMemberId = candidate.memberIds[0]
            changed = True

        # A group owns one desktop surface: member visibility must agree.
        for member_id in candidate.memberIds:
            member = configs_by_id.get(member_id)
            if member is not None and member.isVisible != candidate.isVisible:
                member.isVisible = candidate.isVisible
                changed = True

        style = normalize_navigation_style(candidate.navigationStyle, allow_follow_default=True)
        if candidate.navigationStyle != style:
            candidate.navigationStyle = style
            changed = True
        title = normalize_title_display_mode(candidate.titleDisplayMode, allow_follow_default=True)
        if candidate.titleDisplayMode != title:
            candidate.titleDisplayMode = title
            changed = True
        if candidate.wheelSwitchEnabled is None and style == NavigationStyle.TABS:
            candidate.wheelSwitchEnabled = False
            changed = True

        chrome = normalize_persisted_chrome(candidate.chromeMode)
        if candidate.chromeMode != chrome:
            candidate.chromeMode = chrome
            changed = True

        collapse = capsule_policy.normalize_collapse_behavior(
            candidate.collapseBehavior, fallback="System", allow_system=True
        )
        if candidate.collapseBehavior != collapse:
            candidate.collapseBehavior = collapse
            changed = True

        if not (candidate.width > 0):
            candidate.width = 300.0
            changed = True
        if not (candidate.height > 0):
            candidate.height = 400.0
            changed = True

        normalized.append(candidate)

    if len(normalized) != len(layout.widgetGroups) or any(a is not b for a, b in zip(normalized, layout.widgetGroups)):
        layout.widgetGroups = normalized
        changed = True
    return changed


# ---- chrome policy (WidgetGroupChromePolicy) ------------------------------------
# Our shells always show a visible title bar, so every widget resolves to a
# groupable mode; the policy itself is ported verbatim for config parity.


class ChromeMode:
    SYSTEM = "System"
    STANDARD = "Standard"
    COMPACT = "Compact"
    OVERLAY = "Overlay"
    HIDDEN = "Hidden"


REJECTION_EFFECTIVE_MODE_UNRESOLVED = "EffectiveModeIsUnresolved"
REJECTION_OVERLAY = "OverlayChromeCannotBeGrouped"
REJECTION_HIDDEN = "HiddenChromeCannotBeGrouped"
REJECTION_UNSUPPORTED = "UnsupportedChromeMode"


def is_supported_group_mode(mode: Optional[str]) -> bool:
    return mode in (ChromeMode.STANDARD, ChromeMode.COMPACT)


def normalize_persisted_chrome(value: Optional[str]) -> str:
    if isinstance(value, str) and value in (ChromeMode.STANDARD, ChromeMode.COMPACT):
        return value
    if isinstance(value, str):
        titled = value[:1].upper() + value[1:]
        if titled in (ChromeMode.STANDARD, ChromeMode.COMPACT):
            return titled
    return ChromeMode.STANDARD


@dataclass(frozen=True)
class ChromeDecision:
    is_allowed: bool
    group_mode: Optional[str] = None
    rejected_mode: Optional[str] = None
    rejection_reason: Optional[str] = None


def _evaluate_effective_mode(mode: Optional[str]) -> ChromeDecision:
    if mode in (ChromeMode.STANDARD, ChromeMode.COMPACT):
        return ChromeDecision(True, group_mode=mode)
    reason = {
        ChromeMode.SYSTEM: REJECTION_EFFECTIVE_MODE_UNRESOLVED,
        ChromeMode.OVERLAY: REJECTION_OVERLAY,
        ChromeMode.HIDDEN: REJECTION_HIDDEN,
    }.get(mode, REJECTION_UNSUPPORTED)
    return ChromeDecision(False, rejected_mode=mode, rejection_reason=reason)


def evaluate_merge(source_mode: Optional[str], target_mode: Optional[str]) -> ChromeDecision:
    """The destination owns the resulting surface, so its visible mode wins.
    Overlay/Hidden members may join and adopt the group chrome temporarily."""
    for mode in (source_mode, target_mode):
        if mode == ChromeMode.SYSTEM or mode not in (
            ChromeMode.STANDARD,
            ChromeMode.COMPACT,
            ChromeMode.OVERLAY,
            ChromeMode.HIDDEN,
        ):
            return _evaluate_effective_mode(mode)
    if is_supported_group_mode(target_mode):
        return ChromeDecision(True, group_mode=target_mode)
    if is_supported_group_mode(source_mode):
        return ChromeDecision(True, group_mode=source_mode)
    return ChromeDecision(True, group_mode=ChromeMode.STANDARD)


def evaluate_group_mode(requested_mode: Optional[str]) -> ChromeDecision:
    return _evaluate_effective_mode(requested_mode)


# ---- navigation interaction (WidgetGroupNavigationInteractionPolicy) ------------

DIRECTION_LOCK_DISTANCE = 7.0
GESTURE_COMMIT_DISTANCE = 56.0
GESTURE_COMMIT_VELOCITY = 520.0


def should_lock_vertical(delta_x: float, delta_y: float) -> bool:
    return abs(delta_y) >= DIRECTION_LOCK_DISTANCE and abs(delta_y) > abs(delta_x) * 1.2


def apply_edge_damping(delta_y: float, active_index: int, member_count: int) -> float:
    beyond_start = active_index == 0 and delta_y > 0
    beyond_end = active_index == member_count - 1 and delta_y < 0
    return delta_y * 0.35 if (beyond_start or beyond_end) else delta_y


def should_commit_gesture(cancelled: bool, direction_locked: bool, delta_y: float, elapsed: timedelta) -> bool:
    seconds = max(0.001, elapsed.total_seconds())
    velocity = delta_y / seconds
    return (
        not cancelled
        and direction_locked
        and (abs(delta_y) >= GESTURE_COMMIT_DISTANCE or abs(velocity) >= GESTURE_COMMIT_VELOCITY)
    )


def try_resolve_relative_target(active_index: int, member_count: int, delta: int, wrap: bool = False) -> Optional[int]:
    if delta == 0 or member_count <= 0 or not (0 <= active_index < member_count):
        return None
    target = active_index + (1 if delta > 0 else -1)
    if 0 <= target < member_count:
        return target
    if not wrap:
        return None
    return member_count - 1 if target < 0 else 0


@dataclass
class WheelGesture:
    """One continuous wheel gesture: same-direction deltas coalesce until the
    input goes quiet or reverses; at most one switch is committed per gesture."""

    accumulator: float = 0.0
    last_observed_at: Optional[datetime] = None
    last_direction: int = 0
    committed: bool = False

    def consume(self, wheel_delta: float, observed_at: datetime) -> int:
        """Returns +1/-1 when this delta commits a switch step, else 0."""
        if wheel_delta == 0:
            return 0
        input_direction = 1 if wheel_delta < 0 else -1
        starts_new = (
            self.last_observed_at is None
            or input_direction != self.last_direction
            or (observed_at - self.last_observed_at) < timedelta(0)
            or (observed_at - self.last_observed_at) >= WHEEL_GESTURE_QUIET_PERIOD
        )
        self.last_observed_at = observed_at
        self.last_direction = input_direction
        if starts_new:
            self.accumulator = 0.0
            self.committed = False

        # A counter-delta from touchpad inertia must not swallow a reversal.
        if self.accumulator != 0 and (self.accumulator > 0) != (wheel_delta > 0):
            self.accumulator = 0.0
        self.accumulator += wheel_delta
        if self.committed or abs(self.accumulator) < WHEEL_STEP:
            return 0
        self.accumulator = 0.0
        self.committed = True
        return input_direction

    def reset(self) -> None:
        self.accumulator = 0.0
        self.last_observed_at = None
        self.last_direction = 0
        self.committed = False


@dataclass(frozen=True)
class RailSlot:
    member_index: int
    is_active: bool


def resolve_position_rail_slots(active_index: int, member_count: int) -> List[RailSlot]:
    """Compact title-bar rail: 2-3 members map one-to-one; larger groups show
    a rolling three-slot window around the active member."""
    if member_count < 2:
        return []
    resolved_active = max(0, min(active_index, member_count - 1))
    visible_count = min(3, member_count)
    start_index = 0 if member_count <= visible_count else max(0, min(resolved_active - 1, member_count - visible_count))
    return [RailSlot(start_index + offset, start_index + offset == resolved_active) for offset in range(visible_count)]


# ---- member order (WidgetGroupOrder) ---------------------------------------------


def move_to_target_slot(member_ids: List[str], source_id: str, target_id: str) -> bool:
    try:
        source_index = member_ids.index(source_id)
        target_index = member_ids.index(target_id)
    except ValueError:
        return False
    if source_index == target_index:
        return False
    member_ids.pop(source_index)
    member_ids.insert(max(0, min(target_index, len(member_ids))), source_id)
    return True


def try_resolve_drag_target(original_order: List[str], visual_order: List[str], source_id: str) -> Optional[str]:
    """Translate one tab drag into the target-slot contract; rejects
    interrupted drags and concurrent membership changes."""
    if len(original_order) != len(visual_order) or len(set(original_order)) != len(original_order):
        return None
    try:
        target_index = visual_order.index(source_id)
    except ValueError:
        return None
    expected = list(original_order)
    target_id = original_order[target_index]
    if not move_to_target_slot(expected, source_id, target_id):
        return None
    return target_id if expected == visual_order else None


def drop_hit_test_contains(x: int, y: int, w: int, h: int, screen_x: int, screen_y: int) -> bool:
    if w <= 0 or h <= 0:
        return False
    return x <= screen_x < x + w and y <= screen_y < y + h


# ---- layout sync (WidgetManager.Groups.cs Capture/Apply) --------------------------

_GROUP_LAYOUT_FIELDS = (
    "x",
    "y",
    "positionAnchor",
    "positionMarginX",
    "positionMarginY",
    "positionMonitorKey",
    "positionMonitorDeviceName",
    "positionMonitorWasPrimary",
    "boundsCoordinateVersion",
    "width",
    "height",
    "isPositionLocked",
    "isSizeLocked",
    "isCollapsed",
    "compactWidth",
)


def capture_group_layout(group: WidgetGroupConfig, member: WidgetConfig) -> None:
    for name in _GROUP_LAYOUT_FIELDS:
        setattr(group, name, getattr(member, name))
    group.compactPlacement = _clone_placement(member.compactPlacement)


def apply_group_layout_to_member(group: WidgetGroupConfig, member: WidgetConfig) -> None:
    for name in _GROUP_LAYOUT_FIELDS:
        setattr(member, name, getattr(group, name))
    member.compactPlacement = _clone_placement(group.compactPlacement)
    member.isVisible = group.isVisible
    capsule_policy.set_collapse_override(
        member,
        capsule_policy.normalize_collapse_behavior(group.collapseBehavior, fallback="System", allow_system=True),
    )


def _clone_placement(placement):
    if placement is None:
        return None
    copy = placement.__class__.__new__(placement.__class__)
    for f in placement.__dataclass_fields__:
        setattr(copy, f, getattr(placement, f))
    return copy


def place_detached_member(
    member: WidgetConfig,
    group: WidgetGroupConfig,
    cascade_index: int,
    detached_position: Optional[tuple] = None,
) -> None:
    """Detached members cascade from the group's top-left corner (or land at
    an explicit drop point) and lose every anchored/compact placement hint."""
    if detached_position is not None:
        member.x, member.y = float(detached_position[0]), float(detached_position[1])
    else:
        offset = max(1, min(cascade_index, DETACH_CASCADE_MAX_INDEX)) * DETACH_CASCADE_STEP_DIP
        member.x = group.x + offset
        member.y = group.y + offset
    member.positionAnchor = None
    member.positionMarginX = 0.0
    member.positionMarginY = 0.0
    member.positionMonitorKey = None
    member.positionMonitorDeviceName = None
    member.positionMonitorWasPrimary = None
    member.compactPlacement = None


def create_group_from_target(target: WidgetConfig) -> WidgetGroupConfig:
    """CreateGroupFromTarget — new group seeded from the merge destination."""
    group = WidgetGroupConfig(
        surfaceId=str(uuid.uuid4()),
        name=target.name,
        activeMemberId=target.id,
        memberIds=[target.id],
        isVisible=target.isVisible,
        chromeMode=ChromeMode.STANDARD,
        collapseBehavior="System",
    )
    capture_group_layout(group, target)
    return group


def member_display_name(config: WidgetConfig, translate: Callable[[str], str]) -> str:
    """ResolveGroupMemberDisplayName — custom titles win; default titles use
    the localized feature-widget name."""
    if not config.isDefaultTitle:
        return config.name or config.widgetKind
    title_key = {
        "QuickCapture": "QuickCapture.Name",
        "Todo": "Todo.Title",
        "Weather": "Weather.Title",
        "Music": "Music.Title",
        "Search": "Search.Title",
        "Glance": "Glance.Title",
    }.get(config.widgetKind)
    if title_key:
        localized = translate(title_key)
        if localized and localized != title_key:
            return localized
    return config.name or config.widgetKind
