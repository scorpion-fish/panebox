"""Per-widget stack override metadata tests (no GUI)."""

from __future__ import annotations

import json

from panebox.models.widget_config import WidgetConfig
from panebox.services import stack_settings


def config() -> WidgetConfig:
    return WidgetConfig(name="Files", widgetKind="File")


# ---- scalar overrides -------------------------------------------------------------


def test_overrides_default_to_none():
    cfg = config()
    assert stack_settings.get_enabled_override(cfg) is None
    assert stack_settings.get_group_by_override(cfg) is None
    assert stack_settings.get_threshold_override(cfg) is None
    assert stack_settings.get_order_by_override(cfg) is None
    assert stack_settings.get_open_mode_override(cfg) is None
    assert stack_settings.follows_global_defaults(cfg) is True


def test_set_and_resolve_overrides():
    cfg = config()
    stack_settings.set_enabled_override(cfg, False)
    stack_settings.set_group_by_override(cfg, "datemodified")
    stack_settings.set_threshold_override(cfg, 5)
    stack_settings.set_order_by_override(cfg, "name")
    stack_settings.set_open_mode_override(cfg, "popover")

    assert stack_settings.resolve_enabled(cfg, True) is False
    assert stack_settings.resolve_group_by(cfg, "Kind") == "DateModified"
    assert stack_settings.resolve_threshold(cfg, 3) == 5
    assert stack_settings.resolve_order_by(cfg, "Widget") == "Name"
    assert stack_settings.resolve_open_mode(cfg, "Inline") == "Popover"
    assert not stack_settings.follows_global_defaults(cfg)


def test_group_by_override_maps_date_added_to_kind():
    cfg = config()
    stack_settings.set_group_by_override(cfg, "DateAdded")
    assert stack_settings.get_group_by_override(cfg) == "Kind"


def test_malformed_override_cells_are_ignored():
    cfg = config()
    cfg.metadata.update(
        {
            stack_settings.ENABLED_OVERRIDE_KEY: "maybe",
            stack_settings.GROUP_BY_OVERRIDE_KEY: "Bogus",
            stack_settings.THRESHOLD_OVERRIDE_KEY: "4",  # not in {2,3,5}
            stack_settings.ORDER_BY_OVERRIDE_KEY: "size",
            stack_settings.OPEN_MODE_OVERRIDE_KEY: "window",
        }
    )
    assert stack_settings.get_enabled_override(cfg) is None
    assert stack_settings.get_group_by_override(cfg) is None
    assert stack_settings.get_threshold_override(cfg) is None
    assert stack_settings.get_order_by_override(cfg) is None
    assert stack_settings.get_open_mode_override(cfg) is None


def test_threshold_set_normalizes_invalid_to_default():
    cfg = config()
    stack_settings.set_threshold_override(cfg, 4)
    assert cfg.metadata[stack_settings.THRESHOLD_OVERRIDE_KEY] == "3"


def test_clear_overrides():
    cfg = config()
    stack_settings.set_enabled_override(cfg, True)
    stack_settings.set_order_by_override(cfg, "Name")
    stack_settings.clear_overrides(cfg)
    assert stack_settings.follows_global_defaults(cfg)
    assert stack_settings.ENABLED_OVERRIDE_KEY not in cfg.metadata


def test_normalize_overrides_rewrites_and_prunes():
    cfg = config()
    cfg.metadata[stack_settings.GROUP_BY_OVERRIDE_KEY] = "dateModified"  # non-canonical case
    cfg.metadata[stack_settings.DISABLED_STACKS_KEY] = json.dumps(["Manual:gone"])
    cfg.metadata[stack_settings.STACK_MEMBER_OVERRIDES_KEY] = json.dumps({"Manual:gone": ["/a"]})

    assert stack_settings.normalize_overrides(cfg) is True
    assert cfg.metadata[stack_settings.GROUP_BY_OVERRIDE_KEY] == "DateModified"
    assert stack_settings.get_disabled_stacks(cfg) == set()  # orphaned manual pruned
    assert stack_settings.get_stack_member_overrides(cfg) == {}


# ---- customizations ----------------------------------------------------------------


def test_customizations_round_trip():
    cfg = config()
    customizations = stack_settings.load_customizations(cfg)
    customizations.member_overrides["Manual:abc"] = ["/tmp/a", "/tmp/b", "/tmp/b"]
    customizations.name_overrides["Manual:abc"] = "Stuff"
    customizations.disabled.add("Images")
    customizations.order = ["Manual:abc", "Documents"]

    stack_settings.persist_customizations(cfg, customizations)
    loaded = stack_settings.load_customizations(cfg)
    assert loaded.member_overrides == {"Manual:abc": ["/tmp/a", "/tmp/b"]}
    assert loaded.name_overrides == {"Manual:abc": "Stuff"}
    assert loaded.disabled == {"Images"}
    assert loaded.order == ["Manual:abc", "Documents"]


def test_corrupt_customization_cells_return_empty():
    cfg = config()
    cfg.metadata.update(
        {
            stack_settings.DISABLED_STACKS_KEY: "{not json",
            stack_settings.STACK_NAME_OVERRIDES_KEY: "[1,2]",
            stack_settings.STACK_ORDER_KEY: '"just a string"',
            stack_settings.STACK_MEMBER_OVERRIDES_KEY: "nope",
        }
    )
    assert stack_settings.get_disabled_stacks(cfg) == set()
    assert stack_settings.get_stack_name_overrides(cfg) == {}
    assert stack_settings.get_stack_order(cfg) == []
    assert stack_settings.get_stack_member_overrides(cfg) == {}


def test_empty_customizations_remove_cells():
    cfg = config()
    stack_settings.set_disabled_stacks(cfg, ["Images"])
    stack_settings.set_stack_order(cfg, ["Images"])
    stack_settings.set_stack_name_overrides(cfg, {"Images": "Pics"})
    stack_settings.set_stack_member_overrides(cfg, {"Images": ["/x"]})

    stack_settings.set_disabled_stacks(cfg, [])
    stack_settings.set_stack_order(cfg, [])
    stack_settings.set_stack_name_overrides(cfg, {})
    stack_settings.set_stack_member_overrides(cfg, {})
    for key in (
        stack_settings.DISABLED_STACKS_KEY,
        stack_settings.STACK_ORDER_KEY,
        stack_settings.STACK_NAME_OVERRIDES_KEY,
        stack_settings.STACK_MEMBER_OVERRIDES_KEY,
    ):
        assert key not in cfg.metadata
