"""Desktop-organization planning (port of DesktopOrganizationClassifier,
Scanner, RuleResolver, Planner, PlacementPlanner, PreviewSummary and
PreviewReconciliation).

Pure logic: scan the desktop directory, classify files into categories,
route them through stable per-widget rules, and produce a reviewable plan.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from .organize_models import (
    CategoryIds,
    Classification,
    DesktopOrganizationRule,
    DestinationMode,
    ExclusionReason,
    FileSnapshot,
    Plan,
    Rect,
    ScanResult,
    SourceScope,
    SubtypeIds,
    TargetPlan,
    TargetSelection,
)

# Files up to and including 100 MiB are eligible for quick organization.
SLOW_ITEM_THRESHOLD_BYTES = 100 * 1024 * 1024
QUICK_BATCH_SIZE_LIMIT_BYTES = 100 * 1024 * 1024
QUICK_BATCH_ITEM_LIMIT = 200

TEMPORARY_SUFFIXES = (
    ".tmp",
    ".temp",
    ".part",
    ".partial",
    ".crdownload",
    ".download",
    ".opdownload",
    ".aria2",
    ".!ut",
    ".bc!",
)

MAX_NEW_WIDGET_COUNT = 4
MINIMUM_STANDALONE_CATEGORY_ITEM_COUNT = 2

DEFAULT_EDGE_MARGIN = 16.0
DEFAULT_GAP = 12.0


# ---- classifier -----------------------------------------------------------------

SHORTCUT_EXTENSIONS = {".desktop", ".lnk", ".url"}  # Linux: .desktop launchers

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".svg",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".raw",
    ".psd",
}

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma"}

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".m4v", ".flv"}

# Windows: .zip/.7z/.rar/.tar/.gz/.bz2/.xz/.iso/.exe/.msi/… — Linux installers
# added, Windows installers kept (organizing a downloaded .exe for a Windows
# box is still a package).
PACKAGE_EXTENSIONS = {
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".iso",
    ".deb",
    ".rpm",
    ".flatpak",
    ".snap",
    ".appimage",
    ".exe",
    ".msi",
    ".msix",
    ".appx",
    ".appxbundle",
    ".msixbundle",
}

DOCUMENT_SUBTYPE_BY_EXTENSION = {
    ".pdf": SubtypeIds.PDF,
    ".doc": SubtypeIds.WORD,
    ".docx": SubtypeIds.WORD,
    ".rtf": SubtypeIds.WORD,
    ".xls": SubtypeIds.EXCEL,
    ".xlsx": SubtypeIds.EXCEL,
    ".csv": SubtypeIds.EXCEL,
    ".ppt": SubtypeIds.POWERPOINT,
    ".pptx": SubtypeIds.POWERPOINT,
    ".txt": SubtypeIds.TEXT,
    ".md": SubtypeIds.TEXT,
    ".markdown": SubtypeIds.TEXT,
    ".odt": SubtypeIds.WORD,
    ".ods": SubtypeIds.EXCEL,
    ".odp": SubtypeIds.POWERPOINT,
}


def normalize_extension(extension: str | None) -> str:
    if not extension or not extension.strip():
        return ""
    trimmed = extension.strip()
    return trimmed if trimmed.startswith(".") else f".{trimmed}"


def classify(path: str) -> Classification:
    extension = normalize_extension(os.path.splitext(path)[1]).lower()

    if extension in SHORTCUT_EXTENSIONS:
        return Classification(CategoryIds.SHORTCUTS, None, extension)
    subtype = DOCUMENT_SUBTYPE_BY_EXTENSION.get(extension)
    if subtype is not None:
        return Classification(CategoryIds.DOCUMENTS, subtype, extension)
    if extension in IMAGE_EXTENSIONS:
        return Classification(CategoryIds.IMAGES, None, extension)
    if extension in AUDIO_EXTENSIONS:
        return Classification(CategoryIds.MEDIA, SubtypeIds.AUDIO, extension)
    if extension in VIDEO_EXTENSIONS:
        return Classification(CategoryIds.MEDIA, SubtypeIds.VIDEO, extension)
    if extension in PACKAGE_EXTENSIONS:
        return Classification(CategoryIds.PACKAGES, None, extension)
    return Classification(CategoryIds.OTHER, None, extension)


def category_extensions(category_id: str) -> list[str]:
    table = {
        CategoryIds.SHORTCUTS: SHORTCUT_EXTENSIONS,
        CategoryIds.IMAGES: IMAGE_EXTENSIONS,
        CategoryIds.MEDIA: AUDIO_EXTENSIONS | VIDEO_EXTENSIONS,
        CategoryIds.PACKAGES: PACKAGE_EXTENSIONS,
    }
    if category_id in table:
        return sorted(table[category_id])
    if category_id == CategoryIds.DOCUMENTS:
        return sorted(DOCUMENT_SUBTYPE_BY_EXTENSION)
    return []


def subtype_extensions(subtype_id: str) -> list[str]:
    if subtype_id == SubtypeIds.AUDIO:
        return sorted(AUDIO_EXTENSIONS)
    if subtype_id == SubtypeIds.VIDEO:
        return sorted(VIDEO_EXTENSIONS)
    return sorted(ext for ext, sub in DOCUMENT_SUBTYPE_BY_EXTENSION.items() if sub == subtype_id)


# ---- scanner ----------------------------------------------------------------------


class DesktopOrganizationScanner:
    def __init__(self, desktop_path_provider=None, public_desktop_path_provider=None):
        self._desktop_path_provider = desktop_path_provider or _default_desktop_path
        self._public_desktop_path_provider = public_desktop_path_provider or (
            lambda: ""
        )  # no shared desktop directory on Linux

    def scan(self, include_slow_items: bool = False) -> ScanResult:
        desktop_path = os.path.abspath(os.path.expanduser(self._desktop_path_provider()))
        public_desktop_path = self._normalize_optional(self._public_desktop_path_provider())
        items: list[FileSnapshot] = []

        self._scan_source(desktop_path, items, SourceScope.PERSONAL, include_slow_items)
        public_unavailable = not public_desktop_path
        if public_desktop_path and not _same_path(desktop_path, public_desktop_path):
            try:
                public_unavailable = not os.path.isdir(public_desktop_path)
                self._scan_source(public_desktop_path, items, SourceScope.PUBLIC, include_slow_items)
            except OSError:
                public_unavailable = True

        return ScanResult(
            desktop_path=desktop_path,
            public_desktop_path=public_desktop_path,
            public_desktop_unavailable=public_unavailable,
            items=items,
        )

    def _scan_source(
        self,
        root: str,
        items: list[FileSnapshot],
        scope: str,
        include_slow_items: bool,
    ) -> None:
        if not os.path.isdir(root):
            return
        source_items: list[FileSnapshot] = []
        try:
            names = os.listdir(root)
        except OSError:
            return
        for name in names:
            snapshot = self._create_snapshot(os.path.join(root, name), "", include_slow_items)
            snapshot.source_scope = scope
            source_items.append(snapshot)
        if not include_slow_items:
            _apply_quick_batch_limit(source_items)
        items.extend(source_items)

    def _create_snapshot(self, path: str, public_desktop_path: str, include_slow_items: bool) -> FileSnapshot:
        full_path = os.path.abspath(path)
        name = os.path.basename(full_path)
        result = classify(full_path)
        size = 0
        mtime = datetime(1, 1, 1, tzinfo=timezone.utc)
        mtime_ns = 0
        is_directory = False

        try:
            stat = os.lstat(full_path)
            mtime_ns = stat.st_mtime_ns
            is_directory = os.path.isdir(full_path) and not os.path.islink(full_path)
            is_hidden = name.startswith(".") or name == "desktop.ini"
            is_temporary = _has_temporary_suffix(name)

            reason = (
                ExclusionReason.HIDDEN_OR_SYSTEM
                if is_hidden
                else ExclusionReason.REPARSE_POINT
                if os.path.islink(full_path)
                else ExclusionReason.TEMPORARY_OR_DOWNLOADING
                if is_temporary
                else ExclusionReason.PUBLIC_DESKTOP_ITEM
                if _is_under_path(full_path, public_desktop_path)
                else ExclusionReason.FOLDER
                if is_directory
                else ExclusionReason.NONE
            )

            if not is_directory:
                size = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                if reason == ExclusionReason.NONE and not include_slow_items and size > SLOW_ITEM_THRESHOLD_BYTES:
                    reason = ExclusionReason.SLOW_ITEM
            else:
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        except OSError:
            reason = ExclusionReason.UNAVAILABLE

        return FileSnapshot(
            source_path=full_path,
            name=name,
            extension=result.extension,
            size=size,
            last_write_time_utc=mtime,
            category_id=result.category_id,
            subtype_id=result.subtype_id,
            exclusion_reason=reason,
            is_directory=is_directory,
            mtime_ns=mtime_ns,
        )

    @staticmethod
    def _normalize_optional(path: str) -> str:
        return os.path.abspath(os.path.expanduser(path)) if path and path.strip() else ""


def _default_desktop_path() -> str:
    from .file_ops import desktop_directory

    return desktop_directory()


def _same_path(a: str, b: str) -> bool:
    return os.path.normpath(a).rstrip(os.sep).lower() == os.path.normpath(b).rstrip(os.sep).lower()


def _is_under_path(candidate: str, parent: str) -> bool:
    if not parent:
        return False
    normalized_parent = os.path.abspath(parent).rstrip(os.sep)
    normalized_candidate = os.path.abspath(candidate)
    return normalized_candidate.lower().startswith(normalized_parent.lower() + os.sep)


def _has_temporary_suffix(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in TEMPORARY_SUFFIXES)


def _apply_quick_batch_limit(items: list[FileSnapshot]) -> None:
    accepted_bytes = 0
    accepted_count = 0
    eligible = sorted(
        (item for item in items if item.is_eligible),
        key=lambda item: (item.size, item.name.lower()),
    )
    for item in eligible:
        if accepted_count >= QUICK_BATCH_ITEM_LIMIT or accepted_bytes + item.size > QUICK_BATCH_SIZE_LIMIT_BYTES:
            item.exclusion_reason = ExclusionReason.BATCH_LIMIT
            continue
        accepted_count += 1
        accepted_bytes += item.size


# ---- rule resolver -------------------------------------------------------------------


def resolve_rule(
    item: FileSnapshot,
    rules: list[DesktopOrganizationRule],
    widgets: list,
) -> DesktopOrganizationRule | None:
    """Highest match rank wins: extension (3) > subtype (2) > category (1)."""
    valid_widget_ids = {w.id for w in widgets if w.widgetKind == "File" and not w.isDisabled and w.mappedFolderPath}
    candidates = []
    for rule in rules:
        if not rule.isEnabled or rule.targetWidgetId not in valid_widget_ids:
            continue
        excluded = {normalize_extension(e).lower() for e in rule.excludedExtensions}
        excluded.discard("")
        if item.extension in excluded:
            continue
        rank = _match_rank(item, rule)
        if rank > 0:
            candidates.append((rank, rule.id, rule))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: (-entry[0], entry[1]))
    return candidates[0][2]


def _match_rank(item: FileSnapshot, rule: DesktopOrganizationRule) -> int:
    extensions = {normalize_extension(e).lower() for e in rule.extensions}
    extensions.discard("")
    if item.extension in extensions:
        return 3
    if item.subtype_id and item.subtype_id in rule.subtypeIds:
        return 2
    return 1 if item.category_id in rule.categoryIds else 0


def find_rule_conflicts(rules: list[DesktopOrganizationRule]) -> list[tuple[str, str, list[str]]]:
    """(kind, value, targetWidgetIds) for values routed to more than one widget."""
    enabled = [rule for rule in rules if rule.isEnabled]
    conflicts: list[tuple[str, str, list[str]]] = []
    for kind, selector in (
        (
            "Extension",
            lambda rule: [normalize_extension(e) for e in rule.extensions if normalize_extension(e)],
        ),
        ("Subtype", lambda rule: [v for v in rule.subtypeIds if v]),
        ("Category", lambda rule: [v for v in rule.categoryIds if v]),
    ):
        by_value: dict[str, set[str]] = {}
        for rule in enabled:
            for value in selector(rule):
                by_value.setdefault(value.lower() if kind == "Extension" else value, set()).add(rule.targetWidgetId)
        for value, widget_ids in by_value.items():
            if len(widget_ids) > 1:
                conflicts.append((kind, value, sorted(widget_ids)))
    return conflicts


def assign_extension_exclusively(
    rules: list[DesktopOrganizationRule],
    target_widget_id: str,
    extension: str,
) -> None:
    normalized = normalize_extension(extension).lower()
    if not normalized:
        return
    for rule in rules:
        kept = [
            value
            for value in (normalize_extension(e) for e in rule.extensions)
            if value and (rule.targetWidgetId == target_widget_id or value.lower() != normalized)
        ]
        rule.extensions = kept
    target = next((r for r in rules if r.targetWidgetId == target_widget_id), None)
    if target is None:
        target = DesktopOrganizationRule(targetWidgetId=target_widget_id)
        rules.append(target)
    if normalized not in {normalize_extension(e).lower() for e in target.extensions}:
        target.extensions.append(normalized)


# ---- planner -----------------------------------------------------------------------


class _MutableTarget:
    """Accumulates items for one destination before freezing into TargetPlan."""

    def __init__(
        self,
        source_bucket_id: str,
        category_id: str,
        widget_id: str,
        display_name: str,
        directory_path: str,
        creates_widget: bool,
    ):
        self.source_bucket_id = source_bucket_id
        self.category_id = category_id
        self.widget_id = widget_id
        self.display_name = display_name
        self.directory_path = directory_path
        self.creates_widget = creates_widget
        self.items: list[FileSnapshot] = []

    def to_plan(self) -> TargetPlan:
        return TargetPlan(
            source_bucket_id=self.source_bucket_id,
            category_id=self.category_id,
            target_widget_id=self.widget_id,
            suggested_display_name=self.display_name,
            target_directory_path=self.directory_path,
            creates_widget=self.creates_widget,
            items=self.items,
        )


def create_plan(
    scan: ScanResult,
    storage_root_path: str,
    widgets: list,
    rules: list[DesktopOrganizationRule],
    category_display_name_resolver=None,
    include_personal_desktop: bool = True,
    include_public_desktop: bool = False,
) -> Plan:
    from .file_ops import get_available_path

    normalize = category_display_name_resolver or (lambda category_id: category_id)
    normalized_root = os.path.abspath(storage_root_path)
    targets: dict[str, _MutableTarget] = {}
    pending_by_category: dict[str, list[FileSnapshot]] = {}

    selected: list[FileSnapshot] = []
    for item in scan.items:
        include = include_public_desktop if item.source_scope == SourceScope.PUBLIC else include_personal_desktop
        if include:
            selected.append(item)
        else:
            changed = FileSnapshot(**{**item.__dict__})
            changed.exclusion_reason = ExclusionReason.SOURCE_NOT_SELECTED
            selected.append(changed)

    for item in selected:
        if not item.is_eligible:
            continue
        rule = resolve_rule(item, rules, widgets)
        widget = next((w for w in widgets if w.id == rule.targetWidgetId), None) if rule is not None else None
        if widget is not None and widget.mappedFolderPath:
            existing = targets.get(widget.id)
            if existing is None:
                existing = _MutableTarget(
                    source_bucket_id=widget.id,
                    category_id=item.category_id,
                    widget_id=widget.id,
                    display_name=widget.name,
                    directory_path=os.path.abspath(widget.mappedFolderPath),
                    creates_widget=False,
                )
                targets[widget.id] = existing
            existing.items.append(item)
            continue
        pending_by_category.setdefault(item.category_id, []).append(item)

    _merge_small_and_overflow_categories(pending_by_category)

    reserved_directories = {os.path.abspath(w.mappedFolderPath) for w in widgets if w.mappedFolderPath}
    for category_id in CategoryIds.DEFAULT_ORDER:
        items = pending_by_category.get(category_id)
        if not items:
            continue
        display_name = normalize(category_id)
        directory = get_available_path(
            os.path.join(normalized_root, _sanitize_folder_name(display_name)),
            reserved_directories,
        )
        target = _MutableTarget(
            source_bucket_id=f"category:{category_id}",
            category_id=category_id,
            widget_id=uuid_hex(),
            display_name=display_name,
            directory_path=directory,
            creates_widget=True,
        )
        target.items.extend(items)
        targets[target.widget_id] = target

    return Plan(
        desktop_path=scan.desktop_path,
        public_desktop_path=scan.public_desktop_path,
        public_desktop_unavailable=scan.public_desktop_unavailable,
        include_personal_desktop=include_personal_desktop,
        include_public_desktop=include_public_desktop,
        source_items=list(scan.items),
        storage_root_path=normalized_root,
        targets=[target.to_plan() for target in targets.values()],
        excluded_items=[item for item in selected if not item.is_eligible],
    )


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def _merge_small_and_overflow_categories(
    categories: dict[str, list[FileSnapshot]],
) -> None:
    other = categories.get(CategoryIds.OTHER, [])
    categories[CategoryIds.OTHER] = other

    for category_id in [cid for cid in categories if cid != CategoryIds.OTHER]:
        if len(categories[category_id]) >= MINIMUM_STANDALONE_CATEGORY_ITEM_COUNT:
            continue
        other.extend(categories[category_id])
        del categories[category_id]

    while sum(1 for items in categories.values() if items) > MAX_NEW_WIDGET_COUNT:
        smallest = min(
            ((cid, items) for cid, items in categories.items() if cid != CategoryIds.OTHER and items),
            key=lambda pair: (len(pair[1]), _category_order(pair[0])),
        )
        other.extend(smallest[1])
        del categories[smallest[0]]

    if not other:
        categories.pop(CategoryIds.OTHER, None)


def _category_order(category_id: str) -> int:
    try:
        return CategoryIds.DEFAULT_ORDER.index(category_id)
    except ValueError:
        return len(CategoryIds.DEFAULT_ORDER)


def _sanitize_folder_name(name: str) -> str:
    sanitized = "".join("_" if character in '/\\\0:"<>|?*' else character for character in name).strip()
    return sanitized or CategoryIds.OTHER


def create_retry_plan(
    previous: Plan,
    remaining_paths: set[str],
    widgets: list,
) -> Plan:
    live_ids = {w.id for w in widgets}
    targets = [
        target.clone_with(
            target.target_widget_id,
            target.suggested_display_name,
            target.target_directory_path,
            target.creates_widget and target.target_widget_id not in live_ids,
            [item for item in target.items if item.source_path in remaining_paths],
        )
        for target in previous.targets
    ]
    return Plan(
        id=previous.id,
        desktop_path=previous.desktop_path,
        public_desktop_path=previous.public_desktop_path,
        include_personal_desktop=previous.include_personal_desktop,
        include_public_desktop=previous.include_public_desktop,
        source_items=previous.source_items,
        storage_root_path=previous.storage_root_path,
        targets=[target for target in targets if target.items],
        excluded_items=previous.excluded_items,
    )


# ---- placement ----------------------------------------------------------------------


def assign_planned_bounds(
    plan: Plan,
    work_area: Rect,
    occupied_bounds: list[Rect],
    widget_width: float,
    widget_height: float,
    edge_margin: float = DEFAULT_EDGE_MARGIN,
    gap: float = DEFAULT_GAP,
) -> bool:
    """Top-to-bottom, left-to-right slots for new widgets; best-effort overlap."""
    occupied = list(occupied_bounds)
    for target in plan.targets:
        if not target.creates_widget:
            continue
        bounds = _find_next_available(work_area, occupied, widget_width, widget_height, edge_margin, gap)
        if bounds is None:
            bounds = _find_best_effort(work_area, occupied, widget_width, widget_height, edge_margin, gap)
        if bounds is None:
            for planned in plan.targets:
                planned.planned_bounds = None
            return False
        target.planned_bounds = bounds
        occupied.append(bounds)
    return True


def _find_next_available(
    work_area: Rect,
    occupied: list[Rect],
    width: float,
    height: float,
    edge_margin: float,
    gap: float,
) -> Rect | None:
    y = work_area.y + edge_margin
    while y + height <= work_area.bottom - edge_margin:
        x = work_area.x + edge_margin
        while x + width <= work_area.right - edge_margin:
            candidate = Rect(x, y, width, height)
            if not any(candidate.intersects(other) for other in occupied):
                return candidate
            x += width + gap
        y += height + gap
    return None


def _find_best_effort(
    work_area: Rect,
    occupied: list[Rect],
    width: float,
    height: float,
    edge_margin: float,
    gap: float,
) -> Rect | None:
    best: Rect | None = None
    best_overlap = None
    y = work_area.y + edge_margin
    while y + height <= work_area.bottom - edge_margin:
        x = work_area.x + edge_margin
        while x + width <= work_area.right - edge_margin:
            candidate = Rect(x, y, width, height)
            overlap = sum(_intersection_area(candidate, other) for other in occupied)
            if best_overlap is None or overlap < best_overlap:
                best, best_overlap = candidate, overlap
            x += width + gap
        y += height + gap
    return best


def _intersection_area(a: Rect, b: Rect) -> float:
    width = max(0.0, min(a.right, b.right) - max(a.x, b.x))
    height = max(0.0, min(a.bottom, b.bottom) - max(a.y, b.y))
    return width * height


# ---- preview ----------------------------------------------------------------------


def includes_source(plan: Plan, item: FileSnapshot) -> bool:
    return plan.include_public_desktop if item.source_scope == SourceScope.PUBLIC else plan.include_personal_desktop


def preview_summary(
    plan: Plan,
    selections: dict[str, TargetSelection],
    excluded_paths: set[str],
) -> tuple[int, int, int, int, int]:
    """(total, selected, retained, destinations, newDestinations)."""
    selected_paths: set[str] = set()
    destinations: set[str] = set()
    new_destinations: set[str] = set()
    excluded = {p.lower() for p in excluded_paths}
    for target in plan.targets:
        selection = selections.get(target.source_bucket_id)
        if selection is not None and not selection.is_selected:
            continue
        chosen = [
            item for item in target.items if includes_source(plan, item) and item.source_path.lower() not in excluded
        ]
        if not chosen:
            continue
        for item in chosen:
            selected_paths.add(item.source_path)
        creates = target.creates_widget and (
            selection is None or selection.destination_mode != DestinationMode.EXISTING_WIDGET
        )
        key = (
            f"new:{target.source_bucket_id}"
            if creates
            else f"existing:{selection.existing_widget_id if selection else target.target_widget_id}"
        )
        destinations.add(key)
        if creates:
            new_destinations.add(key)
    sources = plan.source_items or ([item for target in plan.targets for item in target.items] + plan.excluded_items)
    total = len({item.source_path.lower() for item in sources if includes_source(plan, item)})
    return (
        total,
        len(selected_paths),
        max(0, total - len(selected_paths)),
        len(destinations),
        len(new_destinations),
    )


def reconcile_preview(
    previous: list[FileSnapshot],
    current: list[FileSnapshot],
    excluded_paths,
    optional_paths,
    new_paths,
) -> tuple[set[str], set[str], set[str], int, int]:
    """(excluded, optional, markedNew, addedCount, removedCount) — never silently
    select files discovered after the user's review."""
    old = {item.source_path.lower() for item in previous}
    present = {item.source_path.lower() for item in current}
    added = present - old
    excluded = {p.lower() for p in excluded_paths if p.lower() in present} | added
    optional_candidates = {item.source_path.lower() for item in current if item.can_opt_in}
    optional = {p.lower() for p in optional_paths if p.lower() in optional_candidates}
    marked_new = {p.lower() for p in new_paths if p.lower() in present} | added
    return excluded, optional, marked_new, len(added), len(old - present)
