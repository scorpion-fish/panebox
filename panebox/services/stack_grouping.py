"""File-stack grouping + display projection — port of
WidgetStackGroupingService.cs and the projection core of
WidgetViewModel.Stacks.cs (pure logic, no GTK).

Auto grouping buckets widget entries into categories (Kind) or date buckets
(DateAdded / DateModified) or user custom rules; the projection layer decides
which buckets collapse into a stack tile (threshold), honors the user's
per-stack customizations (manual stacks, disabled groups, renamed stacks,
persisted display order) and expands one stack inline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

from .file_sort import FileEntry

# ---- categories (names match the C# enum → i18n key suffixes) -------------------

FOLDERS = "Folders"
APPLICATIONS = "Applications"
DOCUMENTS = "Documents"
IMAGES = "Images"
VIDEOS = "Videos"
AUDIO = "Audio"
ARCHIVES = "Archives"
OTHER = "Other"
TODAY = "Today"
YESTERDAY = "Yesterday"
PREVIOUS_SEVEN_DAYS = "PreviousSevenDays"
PREVIOUS_THIRTY_DAYS = "PreviousThirtyDays"
EARLIER = "Earlier"

CATEGORY_ORDER: Dict[str, int] = {
    FOLDERS: 0,
    APPLICATIONS: 1,
    DOCUMENTS: 2,
    IMAGES: 3,
    VIDEOS: 4,
    AUDIO: 5,
    ARCHIVES: 6,
    OTHER: 7,
    TODAY: 10,
    YESTERDAY: 11,
    PREVIOUS_SEVEN_DAYS: 12,
    PREVIOUS_THIRTY_DAYS: 13,
    EARLIER: 14,
}

DATE_CATEGORIES = (TODAY, YESTERDAY, PREVIOUS_SEVEN_DAYS, PREVIOUS_THIRTY_DAYS, EARLIER)

# ---- extension tables ------------------------------------------------------------
# Applications adapted for Linux (.desktop launchers, AppImages, scripts);
# the Windows-specific installer formats stay so shared folders keep sorting.

APPLICATION_EXTENSIONS = frozenset(
    {
        ".desktop",
        ".appimage",
        ".sh",
        ".py",
        ".run",
        ".flatpakref",
        ".exe",
        ".msi",
        ".bat",
        ".cmd",
        ".ps1",
    }
)
DOCUMENT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".rtf",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".csv",
        ".odt",
        ".ods",
        ".odp",
        ".json",
        ".xml",
        ".html",
        ".htm",
    }
)
IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".svg",
    }
)
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".m4v", ".flv"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".cab", ".iso"})

# ---- setting value domains (SettingsService constants) ---------------------------

GROUP_BY_KIND = "Kind"
GROUP_BY_DATE_ADDED = "DateAdded"
GROUP_BY_DATE_MODIFIED = "DateModified"
GROUP_BY_CUSTOM = "Custom"
ORDER_BY_WIDGET = "Widget"
ORDER_BY_NAME = "Name"
ORDER_BY_DATE_ADDED = "DateAdded"
ORDER_BY_DATE_MODIFIED = "DateModified"
OPEN_MODE_INLINE = "Inline"
OPEN_MODE_POPOVER = "Popover"
POPOVER_LAYOUT_ADAPTIVE = "Adaptive"
POPOVER_LAYOUT_GRID3 = "Grid3"
POPOVER_LAYOUT_GRID5 = "Grid5"
POPOVER_STYLE_NEUTRAL = "Neutral"
POPOVER_STYLE_FOLLOW_MATERIAL = "FollowMaterial"
UNMATCHED_KEEP_LOOSE = "KeepLoose"
UNMATCHED_OTHER = "Other"

DEFAULT_THRESHOLD = 3
THRESHOLD_VALUES = frozenset({2, 3, 5})
MAX_CUSTOM_RULES = 32
MAX_EXTENSIONS_PER_RULE = 64

LOOSE_KEY_PREFIX = "Item:"
MANUAL_KEY_PREFIX = "Manual:"

_INVALID_EXTENSION_CHARS = set('/\\\0:"<>|?*')


# ---- normalizers (SettingsService.NormalizeFileStack*) ---------------------------


def normalize_group_by(value) -> str:
    if isinstance(value, str):
        folded = value.casefold()
        if folded in ("dateadded", "datecreated"):
            return GROUP_BY_DATE_ADDED
        if folded == "datemodified":
            return GROUP_BY_DATE_MODIFIED
        if folded == "custom":
            return GROUP_BY_CUSTOM
    return GROUP_BY_KIND


def normalize_order_by(value) -> str:
    if isinstance(value, str):
        folded = value.casefold()
        if folded == "name":
            return ORDER_BY_NAME
        if folded == "dateadded":
            return ORDER_BY_DATE_ADDED
        if folded == "datemodified":
            return ORDER_BY_DATE_MODIFIED
    return ORDER_BY_WIDGET


def normalize_threshold(value) -> int:
    try:
        threshold = int(value)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return threshold if threshold in THRESHOLD_VALUES else DEFAULT_THRESHOLD


def normalize_open_mode(value) -> str:
    return OPEN_MODE_POPOVER if isinstance(value, str) and value.casefold() == "popover" else OPEN_MODE_INLINE


def normalize_popover_layout(value) -> str:
    if isinstance(value, str) and value in (POPOVER_LAYOUT_GRID3, POPOVER_LAYOUT_GRID5):
        return value
    return POPOVER_LAYOUT_ADAPTIVE


def normalize_popover_style(value) -> str:
    if isinstance(value, str) and value.casefold() == POPOVER_STYLE_FOLLOW_MATERIAL.casefold():
        return POPOVER_STYLE_FOLLOW_MATERIAL
    return POPOVER_STYLE_NEUTRAL


def normalize_unmatched_behavior(value) -> str:
    return UNMATCHED_OTHER if isinstance(value, str) and value.casefold() == "other" else UNMATCHED_KEEP_LOOSE


def normalize_extensions(values: Optional[Iterable], cap: int = MAX_EXTENSIONS_PER_RULE) -> List[str]:
    """ ".mp3", "mp3", "*.MP3" → ".mp3"; deduped case-insensitively, capped."""
    normalized: List[str] = []
    seen = set()
    if not values:
        return normalized
    for raw in values:
        extension = (raw or "").strip()
        if extension.startswith("*."):
            extension = extension[2:]
        elif extension.startswith("*"):
            extension = extension[1:]
        if not extension:
            continue
        if not extension.startswith("."):
            extension = "." + extension
        extension = extension.lower()
        if len(extension) > 24 or _INVALID_EXTENSION_CHARS & set(extension) or extension in seen:
            continue
        seen.add(extension)
        normalized.append(extension)
        if len(normalized) >= cap:
            break
    return normalized


# ---- grouping --------------------------------------------------------------------


@dataclass
class StackGroup:
    category: str
    items: List[FileEntry]
    stack_key: Optional[str] = None
    display_name: Optional[str] = None
    can_stack: bool = True
    force_stack: bool = False

    @property
    def effective_key(self) -> str:
        return self.stack_key if self.stack_key is not None else self.category

    @property
    def is_manual(self) -> bool:
        return bool(self.stack_key and self.stack_key.startswith(MANUAL_KEY_PREFIX))


@dataclass
class _Indexed:
    entry: FileEntry
    index: int


def _rule_extensions(rule) -> List[str]:
    extensions = getattr(rule, "extensions", None)
    if extensions is None and isinstance(rule, dict):
        extensions = rule.get("extensions")
    return normalize_extensions(extensions or [])


def _rule_id(rule) -> str:
    value = getattr(rule, "id", None)
    if value is None and isinstance(rule, dict):
        value = rule.get("id")
    return str(value or "")


def _rule_name(rule) -> str:
    value = getattr(rule, "name", None)
    if value is None and isinstance(rule, dict):
        value = rule.get("name")
    return str(value or "").strip()


def _entry_extension(entry: FileEntry) -> str:
    return (entry.extension or "").lower()


def resolve_date_category(value: Optional[datetime], today: date) -> str:
    if value is None:
        return EARLIER
    moment = value.date() if isinstance(value, datetime) else value
    if moment >= today:
        return TODAY
    if moment >= today - timedelta(days=1):
        return YESTERDAY
    if moment >= today - timedelta(days=7):
        return PREVIOUS_SEVEN_DAYS
    if moment >= today - timedelta(days=30):
        return PREVIOUS_THIRTY_DAYS
    return EARLIER


def resolve_category(
    entry: FileEntry,
    group_by: Optional[str],
    now: Optional[datetime] = None,
    added_at_by_path: Optional[Dict[str, str]] = None,
) -> str:
    normalized = normalize_group_by(group_by)
    today = (now or datetime.now()).date()
    return _category_for(entry, normalized, today, added_at_by_path or {})


def _added_at(entry: FileEntry, added_at_by_path: Dict[str, str]) -> Optional[datetime]:
    from ..models.widget_config import parse_iso

    return parse_iso(added_at_by_path.get(entry.path))


def _category_for(
    entry: FileEntry,
    normalized_group_by: str,
    today: date,
    added_at_by_path: Dict[str, str],
) -> str:
    if normalized_group_by == GROUP_BY_DATE_ADDED:
        return resolve_date_category(_added_at(entry, added_at_by_path), today)
    if normalized_group_by == GROUP_BY_DATE_MODIFIED:
        return resolve_date_category(
            datetime.fromtimestamp(entry.last_modified) if entry.last_modified else None, today
        )
    if entry.is_folder:
        return FOLDERS
    if entry.is_shortcut:
        return APPLICATIONS
    extension = _entry_extension(entry)
    if extension in APPLICATION_EXTENSIONS:
        return APPLICATIONS
    if extension in DOCUMENT_EXTENSIONS:
        return DOCUMENTS
    if extension in IMAGE_EXTENSIONS:
        return IMAGES
    if extension in VIDEO_EXTENSIONS:
        return VIDEOS
    if extension in AUDIO_EXTENSIONS:
        return AUDIO
    if extension in ARCHIVE_EXTENSIONS:
        return ARCHIVES
    return OTHER


def _order_members(members: Sequence[_Indexed], order_by: str, added_at_by_path: Dict[str, str]):
    if order_by == ORDER_BY_NAME:
        return sorted(members, key=lambda m: (m.entry.name.casefold(), m.index))
    if order_by == ORDER_BY_DATE_ADDED:
        return sorted(
            members,
            key=lambda m: (
                -(_added_at(m.entry, added_at_by_path) or datetime.min).timestamp(),
                m.index,
            ),
        )
    if order_by == ORDER_BY_DATE_MODIFIED:
        return sorted(members, key=lambda m: (-m.entry.last_modified, m.index))
    return sorted(members, key=lambda m: m.index)


def group(
    items: Sequence[FileEntry],
    group_by: Optional[str] = None,
    now: Optional[datetime] = None,
    order_by: Optional[str] = None,
    custom_rules: Optional[Sequence] = None,
    unmatched_behavior: Optional[str] = None,
    added_at_by_path: Optional[Dict[str, str]] = None,
) -> List[StackGroup]:
    """Bucket entries into ordered stack groups (WidgetStackGroupingService.Group)."""
    normalized = normalize_group_by(group_by)
    normalized_order = normalize_order_by(order_by)
    today = (now or datetime.now()).date()
    added_map = added_at_by_path or {}
    indexed = [_Indexed(entry, index) for index, entry in enumerate(items)]
    if normalized == GROUP_BY_CUSTOM:
        return _group_by_custom_rules(indexed, custom_rules, unmatched_behavior, normalized_order, added_map)

    buckets: Dict[str, List[_Indexed]] = {}
    for unit in indexed:
        buckets.setdefault(_category_for(unit.entry, normalized, today, added_map), []).append(unit)

    groups = [
        StackGroup(category, [m.entry for m in _order_members(members, normalized_order, added_map)])
        for category, members in sorted(buckets.items(), key=lambda pair: CATEGORY_ORDER.get(pair[0], 14))
    ]
    return groups


def _group_by_custom_rules(
    indexed: Sequence[_Indexed],
    custom_rules: Optional[Sequence],
    unmatched_behavior: Optional[str],
    order_by: str,
    added_map: Dict[str, str],
) -> List[StackGroup]:
    rules = [
        (rule, _rule_id(rule), _rule_name(rule), set(_rule_extensions(rule)))
        for rule in (custom_rules or [])
        if _rule_extensions(rule)
    ]
    matches: Dict[int, List[_Indexed]] = {slot: [] for slot in range(len(rules))}
    unmatched: List[_Indexed] = []
    for unit in indexed:
        extension = _entry_extension(unit.entry)
        slot = next(
            (i for i, (_r, _id, _n, extensions) in enumerate(rules) if extension in extensions),
            None,
        )
        if slot is None:
            unmatched.append(unit)
        else:
            matches[slot].append(unit)

    groups: List[StackGroup] = []
    for slot, (rule, rule_id, rule_name, extensions) in enumerate(rules):
        members = matches.get(slot) or []
        if not members:
            continue
        display = rule_name if rule_name else ", ".join(sorted(extensions))
        groups.append(
            StackGroup(
                OTHER,
                [m.entry for m in _order_members(members, order_by, added_map)],
                f"Custom:{rule_id}",
                display,
            )
        )

    if normalize_unmatched_behavior(unmatched_behavior) == UNMATCHED_OTHER:
        if unmatched:
            groups.append(
                StackGroup(
                    OTHER,
                    [m.entry for m in _order_members(unmatched, order_by, added_map)],
                    "Custom:Other",
                )
            )
    else:
        groups.extend(
            StackGroup(OTHER, [unit.entry], f"Loose:{unit.index}:{unit.entry.path}", can_stack=False)
            for unit in unmatched
        )
    return groups


# ---- display projection (WidgetViewModel.Stacks core) -----------------------------


def normalize_member_path(path: str) -> str:
    try:
        return os.path.normpath(os.path.abspath(path))
    except Exception:
        return path


def loose_order_key(entry: FileEntry) -> str:
    return LOOSE_KEY_PREFIX + normalize_member_path(entry.path).upper()


@dataclass
class StackCustomizations:
    """Per-widget stack customizations (persisted via services.stack_settings)."""

    member_overrides: Dict[str, List[str]] = field(default_factory=dict)
    name_overrides: Dict[str, str] = field(default_factory=dict)
    disabled: set = field(default_factory=set)
    order: List[str] = field(default_factory=list)


@dataclass
class DisplayUnit:
    order_key: str
    entry: Optional[FileEntry] = None  # loose item
    stack: Optional[StackGroup] = None
    stack_name: str = ""  # resolved (non-localized) tile name
    is_manual: bool = False
    expanded: bool = False
    child_of: Optional[str] = None  # set on an expanded stack's inline members

    @property
    def is_stack(self) -> bool:
        return self.stack is not None


@dataclass
class StackProjection:
    units: List[DisplayUnit] = field(default_factory=list)  # top-level units
    expanded_key: Optional[str] = None

    @property
    def visible_units(self) -> List[DisplayUnit]:
        """Top-level units with the expanded stack's members interleaved."""
        visible: List[DisplayUnit] = []
        for unit in self.units:
            visible.append(unit)
            if unit.is_stack and unit.expanded:
                visible.extend(
                    DisplayUnit(loose_order_key(entry), entry=entry, child_of=unit.order_key)
                    for entry in unit.stack.items
                )
        return visible

    def current_order(self) -> List[str]:
        return [unit.order_key for unit in self.units]

    def unit_for_key(self, key: str) -> Optional[DisplayUnit]:
        return next((unit for unit in self.units if unit.order_key == key), None)


def _category_display_key(category: str) -> str:
    return f"Widget.Stack.Category.{category}"


def _should_project_as_stack(group: StackGroup, threshold: int, disabled: set) -> bool:
    minimum = 2 if group.force_stack else threshold
    return group.can_stack and group.effective_key not in disabled and len(group.items) >= minimum


def _apply_member_overrides(
    automatic: List[StackGroup],
    items: Sequence[FileEntry],
    member_overrides: Dict[str, List[str]],
) -> List[StackGroup]:
    """Manual stacks pin members; pinned paths leave their automatic group."""
    if not member_overrides:
        return automatic
    by_path: Dict[str, FileEntry] = {}
    for entry in items:
        if entry.path.strip():
            by_path.setdefault(normalize_member_path(entry.path).casefold(), entry)
    assigned: set = set()
    forced: Dict[str, List[FileEntry]] = {}
    for stack_key, paths in member_overrides.items():
        members = []
        for path in paths:
            normalized = normalize_member_path(path).casefold()
            if normalized not in assigned:
                assigned.add(normalized)
                entry = by_path.get(normalized)
                if entry is not None:
                    members.append(entry)
        if members:
            forced[stack_key] = members

    result: List[StackGroup] = []
    for auto in automatic:
        members = [e for e in auto.items if normalize_member_path(e.path).casefold() not in assigned]
        forced_members = forced.pop(auto.effective_key, None)
        has_forced = forced_members is not None
        if has_forced:
            members.extend(forced_members)
        if members:
            result.append(
                StackGroup(
                    auto.category,
                    members,
                    auto.stack_key,
                    auto.display_name,
                    auto.can_stack,
                    force_stack=auto.force_stack or has_forced,
                )
            )
    for stack_key, members in forced.items():
        manual = stack_key.startswith(MANUAL_KEY_PREFIX)
        category = auto_category_for_key(stack_key)
        result.append(
            StackGroup(
                category,
                members,
                stack_key,
                None if manual else stack_key,
                can_stack=True,
                force_stack=True,
            )
        )
    return result


def auto_category_for_key(stack_key: str) -> str:
    """Non-manual override keys reuse their category name when valid."""
    if stack_key in CATEGORY_ORDER:
        return stack_key
    return OTHER


def project(
    items: Sequence[FileEntry],
    *,
    stacks_enabled: bool,
    auto_stacking: bool = True,
    group_by: Optional[str] = None,
    order_by: Optional[str] = None,
    threshold: int = DEFAULT_THRESHOLD,
    custom_rules: Optional[Sequence] = None,
    unmatched_behavior: Optional[str] = None,
    added_at_by_path: Optional[Dict[str, str]] = None,
    customizations: Optional[StackCustomizations] = None,
    expanded_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> StackProjection:
    """Project entries into display units (RebuildStackDisplayItems)."""
    customizations = customizations or StackCustomizations()
    if not stacks_enabled:
        units = [DisplayUnit(loose_order_key(entry), entry=entry) for entry in items]
        return StackProjection(units, None)

    if auto_stacking:
        automatic = group(
            items,
            group_by=group_by,
            now=now,
            order_by=order_by,
            custom_rules=custom_rules,
            unmatched_behavior=unmatched_behavior,
            added_at_by_path=added_at_by_path,
        )
    else:
        automatic = [
            StackGroup(OTHER, [entry], f"Loose:{index}:{entry.path}", can_stack=False)
            for index, entry in enumerate(items)
        ]

    merged = _apply_member_overrides(automatic, items, customizations.member_overrides)
    normalized_threshold = normalize_threshold(threshold)

    # A stack that no longer projects clears the expansion.
    if expanded_key is not None and not any(
        _should_project_as_stack(g, normalized_threshold, customizations.disabled) and g.effective_key == expanded_key
        for g in merged
    ):
        expanded_key = None

    units: List[DisplayUnit] = []
    for group_ in merged:
        if _should_project_as_stack(group_, normalized_threshold, customizations.disabled):
            key = group_.effective_key
            name = group_.display_name
            if name is None:
                name = customizations.name_overrides.get(key) or _category_display_key(group_.category)
            units.append(
                DisplayUnit(
                    key,
                    stack=group_,
                    stack_name=name,
                    is_manual=key.startswith(MANUAL_KEY_PREFIX),
                    expanded=key == expanded_key,
                )
            )
        else:
            units.extend(DisplayUnit(loose_order_key(entry), entry=entry) for entry in group_.items)

    ordered = _order_units(units, customizations.order)
    return StackProjection(ordered, expanded_key)


def _order_units(units: List[DisplayUnit], persisted_order: Sequence[str]) -> List[DisplayUnit]:
    if not persisted_order:
        return units
    by_key: Dict[str, DisplayUnit] = {}
    for unit in units:
        by_key.setdefault(unit.order_key, unit)
    ordered: List[DisplayUnit] = []
    seen: set = set()
    for key in persisted_order:
        unit = by_key.get(key)
        if unit is not None and unit.order_key not in seen:
            ordered.append(unit)
            seen.add(unit.order_key)
    for unit in units:
        if unit.order_key not in seen:
            ordered.append(unit)
            seen.add(unit.order_key)
    return ordered


# ---- manual stack operations -------------------------------------------------------


def _members_from_paths(items: Sequence[FileEntry], paths: Iterable[str]) -> List[FileEntry]:
    """Distinct entries for the given paths, in item order (NormalizeStackMembers)."""
    wanted = [normalize_member_path(p).casefold() for p in paths if p and p.strip()]
    seen: set = set()
    members = []
    for entry in items:
        normalized = normalize_member_path(entry.path).casefold()
        if normalized in wanted and normalized not in seen:
            seen.add(normalized)
            members.append(entry)
    return members


def _remove_member_overrides(customizations: StackCustomizations, paths: set) -> None:
    for stack_key in list(customizations.member_overrides):
        members = [
            p for p in customizations.member_overrides[stack_key] if normalize_member_path(p).casefold() not in paths
        ]
        if not members:
            del customizations.member_overrides[stack_key]
        elif stack_key.startswith(MANUAL_KEY_PREFIX) and len(members) < 2:
            _remove_manual_customization(customizations, stack_key)
        else:
            customizations.member_overrides[stack_key] = members


def _remove_manual_customization(customizations: StackCustomizations, stack_key: str) -> None:
    customizations.member_overrides.pop(stack_key, None)
    customizations.name_overrides.pop(stack_key, None)
    customizations.disabled.discard(stack_key)
    customizations.order = [key for key in customizations.order if key != stack_key]


def _insertion_index(current_order: List[str], members: Sequence[FileEntry], projection: StackProjection) -> int:
    """First visible slot of any member (or right after its containing stack)."""
    insertion = len(current_order)
    for member in members:
        key = loose_order_key(member)
        if key in current_order:
            insertion = min(insertion, current_order.index(key))
            continue
        containing = next(
            (unit for unit in projection.units if unit.is_stack and member in unit.stack.items),
            None,
        )
        if containing is not None and containing.order_key in current_order:
            insertion = min(insertion, current_order.index(containing.order_key) + 1)
    return insertion


def create_manual_stack(
    items: Sequence[FileEntry],
    member_paths: Sequence[str],
    customizations: StackCustomizations,
    projection: StackProjection,
) -> Optional[str]:
    """Stack the given loose members into a new manual stack (≥2 members)."""
    members = _members_from_paths(items, member_paths)
    if len(members) < 2:
        return None
    current_order = projection.current_order()
    insertion = _insertion_index(current_order, members, projection)
    member_keys = {normalize_member_path(m.path).casefold() for m in members}
    _remove_member_overrides(customizations, member_keys)
    current_order = [
        key
        for key in current_order
        if not (key.startswith(MANUAL_KEY_PREFIX) and key not in customizations.member_overrides)
    ]
    stack_key = f"{MANUAL_KEY_PREFIX}{os.urandom(16).hex()}"
    customizations.member_overrides[stack_key] = [normalize_member_path(m.path) for m in members]
    loose_keys = {LOOSE_KEY_PREFIX + key.upper() for key in member_keys}
    current_order = [key for key in current_order if key not in loose_keys]
    insertion = max(0, min(insertion, len(current_order)))
    current_order.insert(insertion, stack_key)
    customizations.order = list(dict.fromkeys(current_order))
    return stack_key


def convert_to_manual(
    source_key: str,
    items: Sequence[FileEntry],
    customizations: StackCustomizations,
    projection: StackProjection,
    additional_paths: Sequence[str] = (),
    expanded: bool = False,
) -> Optional[str]:
    """Pin an automatic stack's members (plus extras) into a manual stack."""
    source = projection.unit_for_key(source_key)
    if source is None or not source.is_stack or source.is_manual:
        return None
    base_paths = [entry.path for entry in source.stack.items]
    members = _members_from_paths(items, list(base_paths) + list(additional_paths))
    if len(members) < 2:
        return None
    current_order = projection.current_order()
    insertion = (
        current_order.index(source_key)
        if source_key in current_order
        else _insertion_index(current_order, members, projection)
    )
    member_keys = {normalize_member_path(m.path).casefold() for m in members}
    _remove_member_overrides(customizations, member_keys)

    manual_key = f"{MANUAL_KEY_PREFIX}{os.urandom(16).hex()}"
    customizations.member_overrides[manual_key] = [normalize_member_path(m.path) for m in members]
    source_name = customizations.name_overrides.get(source_key) or source.stack_name
    customizations.name_overrides.pop(source_key, None)
    customizations.name_overrides[manual_key] = source_name
    customizations.disabled.discard(source_key)

    loose_keys = {LOOSE_KEY_PREFIX + key.upper() for key in member_keys}
    current_order = [
        key
        for key in current_order
        if key != source_key
        or (key.startswith(MANUAL_KEY_PREFIX) and key not in customizations.member_overrides)
        or key in loose_keys
    ]
    current_order = [key for key in current_order if key not in loose_keys]
    insertion = max(0, min(insertion, len(current_order)))
    current_order.insert(insertion, manual_key)
    customizations.order = list(dict.fromkeys(current_order))
    return manual_key


def add_items_to_stack(
    stack_key: str,
    items: Sequence[FileEntry],
    customizations: StackCustomizations,
    projection: StackProjection,
    dragged_paths: Sequence[str],
) -> bool:
    """Drop loose entries onto a stack; automatic targets convert to manual."""
    unit = projection.unit_for_key(stack_key)
    if unit is None or not unit.is_stack:
        return False
    target_paths = {normalize_member_path(e.path).casefold() for e in unit.stack.items}
    additions = [
        e
        for e in _members_from_paths(items, dragged_paths)
        if normalize_member_path(e.path).casefold() not in target_paths
    ]
    if not additions:
        return False
    if not unit.is_manual:
        return (
            convert_to_manual(
                stack_key,
                items,
                customizations,
                projection,
                [e.path for e in additions],
                expanded=unit.expanded,
            )
            is not None
        )

    member_keys = {normalize_member_path(e.path).casefold() for e in additions}
    _remove_member_overrides(customizations, member_keys)
    forced = customizations.member_overrides.setdefault(stack_key, [])
    for key in [normalize_member_path(e.path) for e in additions]:
        if key.casefold() not in {p.casefold() for p in forced}:
            forced.append(key)

    loose_keys = {LOOSE_KEY_PREFIX + key.upper() for key in member_keys}
    customizations.order = [
        key
        for key in projection.current_order()
        if key not in loose_keys and (not key.startswith(MANUAL_KEY_PREFIX) or key in customizations.member_overrides)
    ]
    # Dedupe while keeping display order.
    customizations.order = list(dict.fromkeys(customizations.order))
    return True


def remove_items_from_stack(
    stack_key: str,
    items: Sequence[FileEntry],
    customizations: StackCustomizations,
    projection: StackProjection,
    selected_paths: Sequence[str],
) -> bool:
    """Remove members from a stack; under two members the stack dissolves."""
    unit = projection.unit_for_key(stack_key)
    if unit is None or not unit.is_stack:
        return False
    stack_paths = {normalize_member_path(e.path).casefold() for e in unit.stack.items}
    removed = {
        normalize_member_path(p).casefold()
        for p in selected_paths
        if normalize_member_path(p).casefold() in stack_paths
    }
    if not removed:
        return False
    if len(unit.stack.items) - len(removed) < 2:
        if unit.is_manual:
            _remove_manual_customization(customizations, stack_key)
        else:
            customizations.disabled.add(stack_key)
        return True
    if not unit.is_manual:
        manual_key = convert_to_manual(stack_key, items, customizations, projection)
        if manual_key is None:
            return False
        stack_key = manual_key
    members = [
        p
        for p in customizations.member_overrides.get(stack_key, [])
        if normalize_member_path(p).casefold() not in removed
    ]
    if len(members) == len(customizations.member_overrides.get(stack_key, [])):
        return False
    customizations.member_overrides[stack_key] = members
    if len(members) < 2:
        _remove_manual_customization(customizations, stack_key)
    return True


def dissolve_stack(
    stack_key: str,
    customizations: StackCustomizations,
    projection: StackProjection,
) -> bool:
    """Dissolve: manual stacks drop their customization, automatic get disabled."""
    unit = projection.unit_for_key(stack_key)
    if unit is None or not unit.is_stack:
        return False
    if not unit.is_manual:
        customizations.disabled.add(stack_key)
        return True
    if stack_key not in customizations.member_overrides:
        return False
    _remove_manual_customization(customizations, stack_key)
    return True


def move_stack(
    stack_key: str,
    delta: int,
    customizations: StackCustomizations,
    projection: StackProjection,
) -> bool:
    """Move a stack tile up (−1) / down (+1) in the display order."""
    order = list(customizations.order) or projection.current_order()
    if stack_key not in order:
        return False
    index = order.index(stack_key)
    target = index + delta
    if target < 0 or target >= len(order):
        return False
    order[index], order[target] = order[target], order[index]
    customizations.order = order
    return True


def move_stack_to_index(
    stack_key: str,
    visible_insertion_index: int,
    visible_units: Sequence[DisplayUnit],
    customizations: StackCustomizations,
    projection: StackProjection,
) -> bool:
    """Reorder by visible index (top-level units only, children skipped)."""
    current_order = projection.current_order()
    if stack_key not in current_order:
        return False
    desired = 0
    capped = max(0, min(visible_insertion_index, len(visible_units)))
    for index in range(capped):
        candidate = visible_units[index]
        if candidate.child_of is None and candidate.order_key != stack_key:
            desired += 1
    reordered = [key for key in current_order if key != stack_key]
    desired = max(0, min(desired, len(reordered)))
    reordered.insert(desired, stack_key)
    if reordered == current_order:
        return False
    customizations.order = reordered
    return True


def set_stack_disabled(stack_key: str, disabled: bool, customizations: StackCustomizations) -> None:
    if disabled:
        customizations.disabled.add(stack_key)
    else:
        customizations.disabled.discard(stack_key)
