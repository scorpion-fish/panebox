"""File sorting + manual order policy — port of WidgetViewModel.SortingAndWatchers.

Favicon: folders first ascending; mode key (Name natural / Size / Type / Date /
Manual sortOrder); ties by natural name then path; descending negates the whole
comparison, folders included. Manual order lives only at the mapped root.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..constants import SortMode
from ..models.widget_config import WidgetItemConfig

_DIGIT_RUN = re.compile(r"(\d+)")


def natural_compare(a: str, b: str) -> int:
    """StrCmpLogicalW-style compare: digit runs compare numerically."""
    if a == b:
        return 0
    for chunk_a, chunk_b in zip(_chunks(a), _chunks(b)):
        if chunk_a == chunk_b:
            continue
        both_digits = chunk_a.isdigit() and chunk_b.isdigit()
        if both_digits:
            num_a, num_b = int(chunk_a), int(chunk_b)
            if num_a != num_b:
                return -1 if num_a < num_b else 1
            # equal value, shorter run sorts first (StrCmpLogicalW keeps
            # leading-zero runs ordered by length)
            return -1 if len(chunk_a) < len(chunk_b) else 1
        la, lb = chunk_a.casefold(), chunk_b.casefold()
        if la != lb:
            return -1 if la < lb else 1
    len_a, len_b = len(_chunks(a)), len(_chunks(b))
    if len_a == len_b:
        return 0
    return -1 if len_a < len_b else 1


def _chunks(value: str) -> list[str]:
    return [c for c in _DIGIT_RUN.split(value) if c != ""]


@dataclass
class FileEntry:
    path: str
    name: str
    is_folder: bool = False
    size: int = 0
    last_modified: float = 0.0
    extension: str = ""
    sort_order: int = 0
    is_symlink: bool = False
    is_shortcut: bool = False

    def type_key(self) -> str:
        return self.extension.casefold()


def compare_entries(left: FileEntry, right: FileEntry, mode: str) -> int:
    if left.is_folder != right.is_folder:
        return -1 if left.is_folder else 1

    if mode == SortMode.SIZE:
        result = (left.size > right.size) - (left.size < right.size)
    elif mode == SortMode.TYPE:
        result = natural_compare(left.extension, right.extension)
        if result == 0:
            # C# uses OrdinalIgnoreCase on extension; emulate via casefold cmp
            la, lb = left.extension.casefold(), right.extension.casefold()
            result = (la > lb) - (la < lb)
    elif mode == SortMode.DATE_MODIFIED:
        result = (left.last_modified > right.last_modified) - (left.last_modified < right.last_modified)
    elif mode == SortMode.MANUAL:
        result = (left.sort_order > right.sort_order) - (left.sort_order < right.sort_order)
    else:  # Name
        result = natural_compare(left.name, right.name)

    if result == 0:
        result = natural_compare(left.name, right.name)
    if result == 0:
        result = natural_compare(left.path, right.path)
    return result


def sort_entries(entries: list[FileEntry], mode: str, descending: bool) -> list[FileEntry]:
    """Sort a copy. Descending negates the whole comparison (C# CompareItems),
    so folders flip to the back along with everything else."""
    if descending:
        compare = lambda a, b: -compare_entries(a, b, mode)  # noqa: E731
    else:
        compare = lambda a, b: compare_entries(a, b, mode)  # noqa: E731
    return sorted(entries, key=functools.cmp_to_key(compare))


def sorted_insert_index(item: FileEntry, entries: list[FileEntry], mode: str, descending: bool) -> int:
    ordered = sort_entries(entries + [item], mode, descending)
    for index, entry in enumerate(ordered):
        if entry is item:
            return index
    return len(entries)


class ManualOrderBook:
    """Path -> index bookkeeping for Manual sort mode (mapped root only)."""

    def __init__(self):
        self.paths: list[str] = []

    def clear(self) -> None:
        self.paths.clear()

    def contains(self, path: str) -> bool:
        return path in self.paths

    def index_of(self, path: str) -> int:
        try:
            return self.paths.index(path)
        except ValueError:
            return -1

    def upsert(
        self,
        path: str,
        preferred_index: Optional[int] = None,
    ) -> None:
        """Insert honoring a drop position; re-add keeps the requested slot."""
        existing = self.index_of(path)
        if existing >= 0:
            if preferred_index is None:
                return
            self.paths.pop(existing)
            adjusted = preferred_index
            if existing < adjusted:
                adjusted -= 1
            adjusted = max(0, min(adjusted, len(self.paths)))
            self.paths.insert(adjusted, path)
            return
        if preferred_index is not None:
            index = max(0, min(preferred_index, len(self.paths)))
            self.paths.insert(index, path)
        else:
            self.paths.append(path)

    def remove(self, path: str) -> None:
        try:
            self.paths.remove(path)
        except ValueError:
            pass

    def move(self, path: str, target_index: int) -> bool:
        current = self.index_of(path)
        if current < 0:
            return False
        target_index = max(0, min(target_index, len(self.paths) - 1))
        if current == target_index:
            return False
        self.paths.pop(current)
        self.paths.insert(target_index, path)
        return True

    def order_for(self, path: str) -> int:
        index = self.index_of(path)
        return index if index >= 0 else len(self.paths)

    def entries_with_order(self, entries: Iterable[FileEntry]) -> list[FileEntry]:
        for entry in entries:
            entry.sort_order = self.order_for(entry.path)
        return list(entries)


def sync_config_items(current_paths: list[str], config_items: list[WidgetItemConfig], at_root: bool) -> bool:
    """Rebuild config items from the live path order; returns True when changed."""
    if not at_root:
        return False
    changed = len(config_items) != len(current_paths)
    if not changed:
        for index, path in enumerate(current_paths):
            persisted = config_items[index]
            if persisted.path != path or persisted.sortOrder != index:
                changed = True
                break
    if not changed:
        return False
    config_items.clear()
    for index, path in enumerate(current_paths):
        config_items.append(WidgetItemConfig(path=path, sortOrder=index))
    return True


@dataclass
class SortSession:
    mode: str = SortMode.NAME
    descending: bool = False
    at_root: bool = True
    manual: ManualOrderBook = field(default_factory=ManualOrderBook)

    def apply(self, entries: list[FileEntry]) -> list[FileEntry]:
        if self.mode == SortMode.MANUAL:
            self.manual.entries_with_order(entries)
            return sort_entries(entries, SortMode.MANUAL, False)
        return sort_entries(entries, self.mode, self.descending)
