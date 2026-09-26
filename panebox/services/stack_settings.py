"""Per-widget file-stack settings — port of WidgetFileStackSettings.cs.

Stack overrides and customizations live in the widget config's metadata dict
(string→string JSON cells), mirroring the C# metadata keys exactly so layouts
round-trip between the two implementations.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Set

from ..models.widget_config import WidgetConfig
from . import stack_grouping

ENABLED_OVERRIDE_KEY = "FileStacksEnabled"
GROUP_BY_OVERRIDE_KEY = "FileStackGroupBy"
THRESHOLD_OVERRIDE_KEY = "FileStackThreshold"
ORDER_BY_OVERRIDE_KEY = "FileStackOrderBy"
OPEN_MODE_OVERRIDE_KEY = "FileStackOpenMode"
DISABLED_STACKS_KEY = "FileStackDisabledGroups"
STACK_NAME_OVERRIDES_KEY = "FileStackNameOverrides"
STACK_ORDER_KEY = "FileStackGroupOrder"
STACK_MEMBER_OVERRIDES_KEY = "FileStackMemberOverrides"

_OVERRIDE_KEYS = (
    ENABLED_OVERRIDE_KEY,
    GROUP_BY_OVERRIDE_KEY,
    THRESHOLD_OVERRIDE_KEY,
    ORDER_BY_OVERRIDE_KEY,
    OPEN_MODE_OVERRIDE_KEY,
)


# ---- scalar overrides -------------------------------------------------------------


def get_enabled_override(config: WidgetConfig) -> Optional[bool]:
    value = (config.metadata or {}).get(ENABLED_OVERRIDE_KEY)
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def get_group_by_override(config: WidgetConfig) -> Optional[str]:
    value = (config.metadata or {}).get(GROUP_BY_OVERRIDE_KEY)
    if not isinstance(value, str) or value.casefold() not in (
        "kind",
        "dateadded",
        "datecreated",
        "datemodified",
        "custom",
    ):
        return None
    normalized = stack_grouping.normalize_group_by(value)
    # The per-widget surface never offers DateAdded grouping (matches C#).
    return stack_grouping.GROUP_BY_KIND if normalized == stack_grouping.GROUP_BY_DATE_ADDED else normalized


def get_threshold_override(config: WidgetConfig) -> Optional[int]:
    value = (config.metadata or {}).get(THRESHOLD_OVERRIDE_KEY)
    try:
        threshold = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return threshold if stack_grouping.normalize_threshold(threshold) == threshold else None


def get_order_by_override(config: WidgetConfig) -> Optional[str]:
    value = (config.metadata or {}).get(ORDER_BY_OVERRIDE_KEY)
    if not isinstance(value, str) or value.casefold() not in (
        "widget",
        "name",
        "dateadded",
        "datemodified",
    ):
        return None
    return stack_grouping.normalize_order_by(value)


def get_open_mode_override(config: WidgetConfig) -> Optional[str]:
    value = (config.metadata or {}).get(OPEN_MODE_OVERRIDE_KEY)
    if not isinstance(value, str) or value.casefold() not in ("inline", "popover"):
        return None
    return stack_grouping.normalize_open_mode(value)


def set_enabled_override(config: WidgetConfig, enabled: Optional[bool]) -> None:
    _set_cell(config, ENABLED_OVERRIDE_KEY, None if enabled is None else str(enabled))


def set_group_by_override(config: WidgetConfig, group_by: Optional[str]) -> None:
    if group_by is None:
        _set_cell(config, GROUP_BY_OVERRIDE_KEY, None)
        return
    normalized = stack_grouping.normalize_group_by(group_by)
    if normalized == stack_grouping.GROUP_BY_DATE_ADDED:
        normalized = stack_grouping.GROUP_BY_KIND
    _set_cell(config, GROUP_BY_OVERRIDE_KEY, normalized)


def set_threshold_override(config: WidgetConfig, threshold: Optional[int]) -> None:
    _set_cell(
        config,
        THRESHOLD_OVERRIDE_KEY,
        None if threshold is None else str(stack_grouping.normalize_threshold(threshold)),
    )


def set_order_by_override(config: WidgetConfig, order_by: Optional[str]) -> None:
    _set_cell(
        config,
        ORDER_BY_OVERRIDE_KEY,
        None if order_by is None else stack_grouping.normalize_order_by(order_by),
    )


def set_open_mode_override(config: WidgetConfig, open_mode: Optional[str]) -> None:
    _set_cell(
        config,
        OPEN_MODE_OVERRIDE_KEY,
        None if open_mode is None else stack_grouping.normalize_open_mode(open_mode),
    )


def clear_overrides(config: WidgetConfig) -> None:
    for key in _OVERRIDE_KEYS:
        (config.metadata or {}).pop(key, None)


def follows_global_defaults(config: WidgetConfig) -> bool:
    return (
        get_enabled_override(config) is None
        and get_group_by_override(config) is None
        and get_threshold_override(config) is None
        and get_order_by_override(config) is None
        and get_open_mode_override(config) is None
    )


def resolve_enabled(config: WidgetConfig, global_default: bool) -> bool:
    return get_enabled_override(config) if get_enabled_override(config) is not None else global_default


def resolve_group_by(config: WidgetConfig, global_default: Optional[str]) -> str:
    override = get_group_by_override(config)
    return override if override is not None else stack_grouping.normalize_group_by(global_default)


def resolve_threshold(config: WidgetConfig, global_default) -> int:
    override = get_threshold_override(config)
    return override if override is not None else stack_grouping.normalize_threshold(global_default)


def resolve_order_by(config: WidgetConfig, global_default: Optional[str]) -> str:
    override = get_order_by_override(config)
    return override if override is not None else stack_grouping.normalize_order_by(global_default)


def resolve_open_mode(config: WidgetConfig, global_default: Optional[str]) -> str:
    override = get_open_mode_override(config)
    return override if override is not None else stack_grouping.normalize_open_mode(global_default)


# ---- customizations (stack order / names / disabled / members) ---------------------


def get_disabled_stacks(config: WidgetConfig) -> Set[str]:
    return set(_read_json_list(config, DISABLED_STACKS_KEY))


def set_disabled_stacks(config: WidgetConfig, stack_keys: Iterable[str]) -> None:
    _write_json_list(config, DISABLED_STACKS_KEY, stack_keys)


def get_stack_name_overrides(config: WidgetConfig) -> Dict[str, str]:
    cell = (config.metadata or {}).get(STACK_NAME_OVERRIDES_KEY)
    if not cell:
        return {}
    try:
        values = json.loads(cell)
    except (ValueError, TypeError):
        return {}
    return {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}


def set_stack_name_overrides(config: WidgetConfig, overrides: Dict[str, str]) -> None:
    clean = {k: v for k, v in overrides.items() if k and v is not None}
    if not clean:
        (config.metadata or {}).pop(STACK_NAME_OVERRIDES_KEY, None)
        return
    _ensure_metadata(config)[STACK_NAME_OVERRIDES_KEY] = json.dumps(clean, ensure_ascii=False)


def get_stack_order(config: WidgetConfig) -> List[str]:
    return _read_json_list(config, STACK_ORDER_KEY)


def set_stack_order(config: WidgetConfig, stack_keys: Optional[Iterable[str]]) -> None:
    if stack_keys is None:
        (config.metadata or {}).pop(STACK_ORDER_KEY, None)
        return
    _write_json_list(config, STACK_ORDER_KEY, stack_keys)


def get_stack_member_overrides(config: WidgetConfig) -> Dict[str, List[str]]:
    cell = (config.metadata or {}).get(STACK_MEMBER_OVERRIDES_KEY)
    if not cell:
        return {}
    try:
        values = json.loads(cell)
    except (ValueError, TypeError):
        return {}
    if not isinstance(values, dict):
        return {}
    result: Dict[str, List[str]] = {}
    for key, paths in values.items():
        if not isinstance(key, str) or not key.strip():
            continue
        members: List[str] = []
        for path in paths or []:
            if isinstance(path, str) and path.strip() and path.casefold() not in {m.casefold() for m in members}:
                members.append(path)
        result[key] = members
    return result


def set_stack_member_overrides(config: WidgetConfig, overrides: Dict[str, List[str]]) -> None:
    normalized: Dict[str, List[str]] = {}
    for key, paths in overrides.items():
        if not isinstance(key, str) or not key.strip():
            continue
        members: List[str] = []
        for path in paths or []:
            if isinstance(path, str) and path.strip() and path.casefold() not in {m.casefold() for m in members}:
                members.append(path)
        if members:
            normalized[key] = members
    if not normalized:
        (config.metadata or {}).pop(STACK_MEMBER_OVERRIDES_KEY, None)
        return
    _ensure_metadata(config)[STACK_MEMBER_OVERRIDES_KEY] = json.dumps(normalized, ensure_ascii=False)


def load_customizations(config: WidgetConfig) -> "stack_grouping.StackCustomizations":
    from .stack_grouping import StackCustomizations

    return StackCustomizations(
        member_overrides=get_stack_member_overrides(config),
        name_overrides=get_stack_name_overrides(config),
        disabled=get_disabled_stacks(config),
        order=get_stack_order(config),
    )


def persist_customizations(config: WidgetConfig, customizations: "stack_grouping.StackCustomizations") -> None:
    set_stack_member_overrides(config, customizations.member_overrides)
    set_stack_name_overrides(config, customizations.name_overrides)
    set_disabled_stacks(config, customizations.disabled)
    set_stack_order(config, customizations.order)


def normalize_overrides(config: WidgetConfig) -> bool:
    """Rewrite override cells canonically; drop orphaned manual-stack metadata."""
    metadata = config.metadata
    if not metadata:
        return False
    changed = False
    canonical = {
        ENABLED_OVERRIDE_KEY: get_enabled_override(config),
        GROUP_BY_OVERRIDE_KEY: get_group_by_override(config),
        THRESHOLD_OVERRIDE_KEY: get_threshold_override(config),
        ORDER_BY_OVERRIDE_KEY: get_order_by_override(config),
        OPEN_MODE_OVERRIDE_KEY: get_open_mode_override(config),
    }
    for key, value in canonical.items():
        if key not in metadata:
            continue
        text = None if value is None else str(value)
        if text is None or metadata.get(key) != text:
            if text is None:
                metadata.pop(key, None)
            else:
                metadata[key] = text
            changed = True
    changed |= _prune_orphaned_manual_metadata(config)
    return changed


def _prune_orphaned_manual_metadata(config: WidgetConfig) -> bool:
    """Manual stacks below two persisted members cannot exist (C# prune)."""
    members = get_stack_member_overrides(config)
    active = {
        key for key, paths in members.items() if key.startswith(stack_grouping.MANUAL_KEY_PREFIX) and len(paths) >= 2
    }

    def orphaned(key: str) -> bool:
        return key.startswith(stack_grouping.MANUAL_KEY_PREFIX) and key not in active

    changed = False
    for key in [k for k in members if orphaned(k)]:
        members.pop(key)
        changed = True
    names = get_stack_name_overrides(config)
    for key in [k for k in names if orphaned(k)]:
        names.pop(key)
        changed = True
    disabled = {k for k in get_disabled_stacks(config) if not orphaned(k)}
    changed |= len(disabled) != len(get_disabled_stacks(config))
    order = [key for key in get_stack_order(config) if not orphaned(key)]
    changed |= len(order) != len(get_stack_order(config))
    if changed:
        set_stack_member_overrides(config, members)
        set_stack_name_overrides(config, names)
        set_disabled_stacks(config, disabled)
        set_stack_order(config, order)
    return changed


# ---- cell helpers ------------------------------------------------------------------


def _ensure_metadata(config: WidgetConfig) -> Dict[str, str]:
    if config.metadata is None:
        config.metadata = {}
    return config.metadata


def _set_cell(config: WidgetConfig, key: str, value: Optional[str]) -> None:
    metadata = _ensure_metadata(config)
    if value is None:
        metadata.pop(key, None)
    else:
        metadata[key] = value


def _read_json_list(config: WidgetConfig, key: str) -> List[str]:
    cell = (config.metadata or {}).get(key)
    if not cell:
        return []
    try:
        values = json.loads(cell)
    except (ValueError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in out:
            out.append(value)
    return out


def _write_json_list(config: WidgetConfig, key: str, values: Iterable[str]) -> None:
    cleaned = [v for v in values if isinstance(v, str) and v.strip()]
    deduped = list(dict.fromkeys(cleaned))
    if not deduped:
        (config.metadata or {}).pop(key, None)
        return
    _ensure_metadata(config)[key] = json.dumps(deduped, ensure_ascii=False)
