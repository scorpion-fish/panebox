"""Widget group policy tests — ports of WidgetGroupSettings.Normalize,
WidgetGroupOrder, WidgetGroupChromePolicy, and the navigation interaction
policy semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from panebox.models.layout_slice import WidgetLayoutSettingsSlice
from panebox.models.widget_config import WidgetConfig
from panebox.models.widget_group_config import WidgetGroupConfig
from panebox.services import group_policy as g


def _layout():
    return WidgetLayoutSettingsSlice()


def _widget(wid: str, name: str = None, kind: str = "File") -> WidgetConfig:
    return WidgetConfig(id=wid, name=name or wid, widgetKind=kind)


def _group(members, active=None, **kwargs) -> WidgetGroupConfig:
    group = WidgetGroupConfig(
        id="g1",
        surfaceId="s1",
        memberIds=list(members),
        activeMemberId=active or members[0],
    )
    for key, value in kwargs.items():
        setattr(group, key, value)
    return group


# ---- normalize ---------------------------------------------------------------


def test_normalize_drops_deleted_and_duplicate_members():
    layout = _layout()
    layout.widgets = [_widget("a"), _widget("b"), _widget("c")]
    layout.deletedWidgetIds = ["b"]
    layout.widgetGroups = [_group(["a", "b", "a", "c"])]

    assert g.normalize_groups(layout) is True
    assert layout.widgetGroups[0].memberIds == ["a", "c"]


def test_normalize_dissolves_groups_under_two_members():
    layout = _layout()
    layout.widgets = [_widget("a")]
    layout.widgetGroups = [_group(["a", "ghost"])]

    g.normalize_groups(layout)
    assert layout.widgetGroups == []
    assert g.find_group_by_member(layout, "a") is None  # a is standalone again


def test_normalize_fixes_active_member_and_visibility():
    layout = _layout()
    a = _widget("a")
    b = _widget("b")
    a.isVisible = True
    b.isVisible = False
    layout.widgets = [a, b]
    layout.widgetGroups = [_group(["a", "b"], active="gone", isVisible=False)]

    g.normalize_groups(layout)
    group = layout.widgetGroups[0]
    assert group.activeMemberId == "a"  # first member
    assert a.isVisible is False and b.isVisible is False  # group owns visibility


def test_normalize_enforces_member_cap():
    layout = _layout()
    layout.widgets = [_widget(f"w{i}") for i in range(10)]
    layout.widgetGroups = [_group([f"w{i}" for i in range(10)])]

    g.normalize_groups(layout)
    assert len(layout.widgetGroups[0].memberIds) == g.MAXIMUM_MEMBER_COUNT


def test_normalize_styles_chrome_and_tabs_wheel_default():
    layout = _layout()
    layout.widgets = [_widget("a"), _widget("b"), _widget("c"), _widget("d")]
    tabs_group = _group(["a", "b"])
    tabs_group.navigationStyle = "Auto"
    stack_group = _group(["c", "d"])
    stack_group.navigationStyle = "Stack"
    stack_group.titleDisplayMode = "Bogus"
    stack_group.chromeMode = "Overlay"
    stack_group.width = -5
    layout.widgetGroups = [tabs_group, stack_group]

    g.normalize_groups(layout)
    assert tabs_group.navigationStyle == "Stack"  # Auto → Stack (legacy)
    # The wheel pin only applies to Tabs-style groups; Auto→Stack keeps None.
    assert tabs_group.wheelSwitchEnabled is None
    assert stack_group.wheelSwitchEnabled is None  # Stack follows default
    tabs2 = _group(["a", "b"])
    tabs2.navigationStyle = "Tabs"  # explicit Tabs (FollowDefault keeps None)
    layout.widgetGroups = [tabs2]
    g.normalize_groups(layout)
    assert tabs2.wheelSwitchEnabled is False and tabs2.navigationStyle == "Tabs"
    assert stack_group.titleDisplayMode == "IconAndText"
    assert stack_group.chromeMode == "Standard"  # Overlay cannot be persisted
    assert stack_group.width == 300.0


def test_normalize_reissues_duplicate_ids_and_surface_ids():
    layout = _layout()
    layout.widgets = [_widget("a"), _widget("b"), _widget("c"), _widget("d")]
    first = _group(["a", "b"])
    second = _group(["c", "d"])
    second.id = first.id
    second.surfaceId = first.surfaceId
    layout.widgetGroups = [first, second]

    g.normalize_groups(layout)
    assert layout.widgetGroups[1].id != layout.widgetGroups[0].id
    assert layout.widgetGroups[1].surfaceId != layout.widgetGroups[0].surfaceId


def test_restorable_active_member_falls_back_to_first_available():
    layout = _layout()
    a = _widget("a")
    b = _widget("b")
    layout.widgets = [a, b]
    group = _group(["a", "b"], active="a")
    layout.widgetGroups = [group]

    assert g.resolve_restorable_active_member_id(layout, group, lambda c: True) == "a"
    assert g.resolve_restorable_active_member_id(layout, group, lambda c: c.id != "a") == "b"
    assert g.resolve_restorable_active_member_id(layout, group, lambda c: False) is None


# ---- chrome --------------------------------------------------------------------


def test_chrome_merge_destination_wins():
    ok = g.evaluate_merge("Standard", "Compact")
    assert ok.is_allowed and ok.group_mode == "Compact"
    # Overlay members may join and adopt the group's visible chrome.
    assert g.evaluate_merge("Overlay", "Standard").is_allowed
    assert g.evaluate_merge("Hidden", "Standard").is_allowed
    # Unresolved/unknown modes reject.
    rejected = g.evaluate_merge("System", "Standard")
    assert not rejected.is_allowed
    assert rejected.rejection_reason == g.REJECTION_EFFECTIVE_MODE_UNRESOLVED
    assert not g.evaluate_merge("Standard", "Nonsense").is_allowed


def test_chrome_persisted_normalization():
    assert g.normalize_persisted_chrome("compact") == "Compact"
    assert g.normalize_persisted_chrome("System") == "Standard"
    assert g.normalize_persisted_chrome(None) == "Standard"


# ---- navigation interaction ------------------------------------------------------


def test_relative_target_with_and_without_wrap():
    assert g.try_resolve_relative_target(0, 3, 1) == 1
    assert g.try_resolve_relative_target(2, 3, 1, wrap=False) is None
    assert g.try_resolve_relative_target(2, 3, 1, wrap=True) == 0
    assert g.try_resolve_relative_target(0, 3, -1, wrap=True) == 2
    assert g.try_resolve_relative_target(-1, 3, 1) is None
    assert g.try_resolve_relative_target(0, 0, 1) is None


def test_wheel_gesture_commits_one_step_per_burst():
    wheel = g.WheelGesture()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Sub-threshold deltas accumulate; scroll-up (positive) → previous (-1).
    assert wheel.consume(60, now) == 0
    assert wheel.consume(60, now + timedelta(milliseconds=10)) == -1  # ≥120
    # Same-direction continuation within the quiet period: one gesture, no
    # second commit.
    assert wheel.consume(200, now + timedelta(milliseconds=50)) == 0
    assert wheel.consume(200, now + timedelta(milliseconds=80)) == 0
    # Quiet period elapsed → a fresh gesture may commit again (scroll-down →
    # next member, +1).
    assert wheel.consume(-120, now + timedelta(milliseconds=400)) == 1


def test_wheel_gesture_reversal_resets_accumulator():
    wheel = g.WheelGesture()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert wheel.consume(100, now) == 0
    # Counter-delta reverses direction: stale accumulation is dropped.
    assert wheel.consume(-100, now + timedelta(milliseconds=20)) == 0
    assert wheel.consume(-30, now + timedelta(milliseconds=30)) == 1


def test_position_rail_slots():
    assert g.resolve_position_rail_slots(0, 2) == [g.RailSlot(0, True), g.RailSlot(1, False)]
    slots = g.resolve_position_rail_slots(4, 6)
    assert [s.member_index for s in slots] == [3, 4, 5]
    assert slots[1].is_active
    # Leading edge clamps the window to the start.
    assert [s.member_index for s in g.resolve_position_rail_slots(0, 6)] == [0, 1, 2]


def test_gesture_and_damping_helpers():
    assert g.should_lock_vertical(2, 10) is True
    assert g.should_lock_vertical(10, 10) is False  # horizontal wins
    assert g.apply_edge_damping(10, 0, 3) == 3.5  # damped at the start edge
    assert g.apply_edge_damping(10, 1, 3) == 10.0
    assert g.apply_edge_damping(-10, 2, 3) == -3.5
    assert g.should_commit_gesture(False, True, 60, timedelta(seconds=1)) is True
    assert g.should_commit_gesture(True, True, 60, timedelta(seconds=1)) is False
    assert g.should_commit_gesture(False, False, 600, timedelta(seconds=1)) is False
    fast = g.should_commit_gesture(False, True, 30, timedelta(milliseconds=10))
    assert fast is True  # velocity path


# ---- order -----------------------------------------------------------------------


def test_move_to_target_slot():
    members = ["a", "b", "c"]
    assert g.move_to_target_slot(members, "a", "c") is True
    assert members == ["b", "c", "a"]
    assert g.move_to_target_slot(members, "a", "a") is False
    assert g.move_to_target_slot(members, "x", "a") is False
    members = ["a", "b", "c"]
    assert g.move_to_target_slot(members, "c", "a") is True
    assert members == ["c", "a", "b"]


def test_try_resolve_drag_target():
    original = ["a", "b", "c"]
    # Tab 'a' dragged onto the slot of 'c' → target is 'c'.
    assert g.try_resolve_drag_target(original, ["b", "c", "a"], "a") == "c"
    # Unmoved layout resolves to no target (source slot unchanged).
    assert g.try_resolve_drag_target(original, ["a", "b", "c"], "a") is None
    # Membership change during the drag rejects the result.
    assert g.try_resolve_drag_target(original, ["b", "c", "x"], "a") is None


def test_drop_hit_test():
    assert g.drop_hit_test_contains(10, 20, 100, 40, 50, 30) is True
    assert g.drop_hit_test_contains(10, 20, 100, 40, 110, 30) is False
    assert g.drop_hit_test_contains(10, 20, 0, 40, 10, 30) is False


# ---- layout sync -------------------------------------------------------------------


def test_capture_and_apply_group_layout():
    member = _widget("a")
    member.x, member.y, member.width, member.height = 11, 22, 333, 444
    member.isCollapsed = True
    member.compactWidth = 200.0
    group = _group(["a"])
    g.capture_group_layout(group, member)
    assert (group.x, group.y, group.width, group.height) == (11, 22, 333, 444)
    assert group.isCollapsed is True

    other = _widget("b")
    g.apply_group_layout_to_member(group, other)
    assert (other.x, other.y, other.width, other.height) == (11, 22, 333, 444)
    assert other.isVisible == group.isVisible


def test_place_detached_member_cascades_and_clears_anchors():
    group = _group(["a", "b"])
    group.x, group.y = 100, 100
    member = _widget("b")
    member.positionAnchor = "RightBottom"
    member.compactPlacement = object()

    g.place_detached_member(member, group, 2)
    assert (member.x, member.y) == (100 + 2 * 24, 100 + 2 * 24)
    assert member.positionAnchor is None
    assert member.compactPlacement is None

    far = _widget("b")
    g.place_detached_member(far, group, 99)  # clamped cascade index
    assert (far.x, far.y) == (100 + 5 * 24, 100 + 5 * 24)

    pinned = _widget("b")
    g.place_detached_member(pinned, group, 1, detached_position=(7, 9))
    assert (pinned.x, pinned.y) == (7.0, 9.0)


def test_create_group_from_target_and_display_name():
    target = _widget("a", name="My Files")
    target.isDefaultTitle = False
    group = g.create_group_from_target(target)
    assert group.memberIds == ["a"] and group.activeMemberId == "a"
    assert group.name == "My Files" and group.surfaceId
    assert group.collapseBehavior == "System"

    default = _widget("t1", name="Tasks", kind="Todo")
    default.isDefaultTitle = True
    assert g.member_display_name(default, lambda k: "Tasks" if k == "Todo.Title" else k) == "Tasks"
    custom = _widget("t2", name="Shopping", kind="Todo")
    custom.isDefaultTitle = False
    assert g.member_display_name(custom, lambda k: k) == "Shopping"
