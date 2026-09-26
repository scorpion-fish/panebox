"""Own incremental filename index — the Linux replacement for Everything IPC.

PaneBox-on-Windows delegates filename search to Everything's SDK over IPC;
Linux has no equivalent service, so the port maintains its own index:

- Scan roots default to $HOME (settings searchIndexRoots overrides), walking
  with a skip-list of heavy/noisy directories and depth/count caps.
- Application launchers (.desktop, the Start-Menu .lnk analog) are indexed
  from the XDG application directories as files — extension categorizes them
  as Apps, so the ranker's app boost applies just like .lnk on Windows.
- watchdog keeps the index live when available; a staleness fallback triggers
  periodic rescans otherwise. Scans run on a worker thread and swap an
  immutable snapshot in; queries never block on scanning.

Query semantics mirror Everything: whitespace-separated terms, all AND-matched
case-insensitively against the full path; name matches outrank path-only
matches. Providers return SearchFileQueryPage pages (offset slicing), which
feeds SearchEngineService's staged paging.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from ..models.search_models import SearchFileQueryPage, SearchResultItem, SearchResultKind

# Walk guards — bounded work on hostile home directories.
SKIP_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".cache",
    ".thumbnails",
    ".Trash",
    ".trash-1000",
    ".venv",
    "venv",
    ".npm",
    ".yarn",
    ".cargo",
    "target",
    "build",
    "dist",
    "proc",
    "sys",
    "dev",
    "run",
    "snap",
    "flatpak",
    ".mozilla",
    "Cache",
    "Caches",
    "code_cache",
    "GPUCache",
}
# Hidden directories that still contain user content.
KEEP_HIDDEN_DIRECTORY_NAMES = {".local", ".config", ".desktop"}
MAX_WALK_DEPTH = 8
MAX_INDEXED_FILES = 200_000
RESCAN_INTERVAL_SECONDS = 300.0

# Provider states surfaced through SearchResponse.fileProviderState.
STATE_IDLE = "Idle"
STATE_INDEXING = "Indexing"
STATE_READY = "Connected"


def default_index_roots() -> List[str]:
    return [str(Path.home())]


def application_directories() -> List[str]:
    """XDG application dirs (Start Menu analog), most specific first."""
    directories: List[str] = []
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    directories.append(os.path.join(data_home, "applications"))
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for entry in data_dirs.split(":"):
        if entry.strip():
            directories.append(os.path.join(entry.strip(), "applications"))
    directories.append(os.path.expanduser("~/.local/share/flatpak/exports/share/applications"))
    directories.append("/var/lib/flatpak/exports/share/applications")
    seen = set()
    unique = []
    for directory in directories:
        if directory not in seen:
            seen.add(directory)
            unique.append(directory)
    return unique


def _entry_key(entry: Tuple[str, str, bool]) -> Tuple:
    """Sort entries by (score-independent) name for stable paging."""
    _path_lower, path, _is_dir = entry
    return (os.path.basename(path).lower(), path.lower())


class SearchFileIndex:
    """Filename index with paged Everything-style queries."""

    def __init__(self, roots: Optional[List[str]] = None, include_apps: bool = True):
        self._roots = [str(root) for root in (roots if roots is not None else default_index_roots())]
        self._include_apps = include_apps
        self._lock = threading.RLock()
        # Immutable snapshot: list of (path_lower, path, is_dir) sorted by name.
        self._entries: List[Tuple[str, str, bool]] = []
        self._state = STATE_IDLE
        self._last_scan_finished = 0.0
        self._scan_thread: Optional[threading.Thread] = None
        self._disposed = False

    # ---- lifecycle ───────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def ensure_started(self) -> None:
        """Kick the initial scan (background) if none has run yet."""
        with self._lock:
            if self._disposed or self._scan_thread is not None:
                return
            if self._entries:
                return
            self._state = STATE_INDEXING
            self._scan_thread = threading.Thread(target=self._scan_worker, name="search-index", daemon=True)
            self._scan_thread.start()

    def rescan_async(self) -> None:
        with self._lock:
            if self._disposed:
                return
            active = self._scan_thread is not None and self._scan_thread.is_alive()
            if active:
                return
            self._state = STATE_INDEXING
            self._scan_thread = threading.Thread(target=self._scan_worker, name="search-index", daemon=True)
            self._scan_thread.start()

    def scan_now(self) -> None:
        """Synchronous scan — used by tests and first-query fallback."""
        with self._lock:
            if self._disposed:
                return
            self._state = STATE_INDEXING
        self._scan_worker()

    def apply_filesystem_changes(self, changed_paths: List[str]) -> None:
        """Fold watchdog-style change notifications into the snapshot."""
        with self._lock:
            if self._disposed or not self._entries:
                return
            entries = list(self._entries)
            known = {path: index for index, (_lower, path, _is_dir) in enumerate(entries)}
            for raw in changed_paths:
                path = os.path.abspath(raw)
                if not path.startswith(tuple(self._roots)) and not path.endswith(".desktop"):
                    continue
                if os.path.exists(path):
                    is_dir = os.path.isdir(path)
                    if path in known:
                        continue
                    entries.append((path.lower(), path, is_dir))
                else:
                    index = known.get(path)
                    if index is not None:
                        entries.pop(index)
                        known = {p: i for i, (_l, p, _d) in enumerate(entries)}
            entries.sort(key=_entry_key)
            self._entries = entries

    def dispose(self) -> None:
        self._disposed = True

    # ---- scanning ────────────────────────────────────────────────────────────

    def _scan_worker(self) -> None:
        try:
            entries = self._collect_entries()
            entries.sort(key=_entry_key)
            with self._lock:
                if self._disposed:
                    return
                self._entries = entries
                self._state = STATE_READY
                self._last_scan_finished = time.monotonic()
                self._scan_thread = None
        except Exception:
            with self._lock:
                self._state = STATE_READY
                self._scan_thread = None

    def _collect_entries(self) -> List[Tuple[str, str, bool]]:
        entries: List[Tuple[str, str, bool]] = []
        seen = set()
        count = 0
        for root in self._roots:
            root = os.path.realpath(root)
            if root in seen or not os.path.isdir(root):
                continue
            seen.add(root)
            for directory, subdirectories, files in os.walk(root, followlinks=False):
                depth = directory[len(root) :].count(os.sep)
                if depth >= MAX_WALK_DEPTH:
                    subdirectories[:] = []
                subdirectories[:] = [
                    name
                    for name in subdirectories
                    if self._should_descend(name, os.path.join(directory, name), root, depth)
                ]
                entries.append((directory.lower() + "/", directory, True))
                count += 1
                for name in files:
                    path = os.path.join(directory, name)
                    entries.append((path.lower(), path, False))
                    count += 1
                if count >= MAX_INDEXED_FILES:
                    return entries
        if self._include_apps:
            for directory in application_directories():
                if os.path.isdir(directory):
                    for name in sorted(os.listdir(directory)):
                        if name.endswith(".desktop"):
                            path = os.path.join(directory, name)
                            entries.append((path.lower(), path, False))
        return entries

    @staticmethod
    def _should_descend(name: str, path: str, root: str, depth: int) -> bool:
        if name in SKIP_DIRECTORY_NAMES:
            return False
        if name.startswith("."):
            if depth == 0 and name in KEEP_HIDDEN_DIRECTORY_NAMES:
                return True
            if (
                depth == 1
                and root.startswith(os.path.expanduser("~"))
                and name
                in {
                    "applications",
                    "autostart",
                }
            ):
                return True
            return False
        return True

    # ---- querying ────────────────────────────────────────────────────────────

    def query(self, query: str, offset: int = 0, page_size: int = 200) -> SearchFileQueryPage:
        """AND-match whitespace terms against the path; name hits outrank path hits."""
        terms = [term.lower() for term in query.split() if term.strip()]
        if not terms:
            return SearchFileQueryPage.empty()
        self.ensure_started()
        self._maybe_refresh_stale()
        with self._lock:
            entries = self._entries

        matches = []  # (score, path, is_dir)
        for path_lower, path, is_dir in entries:
            if not all(term in path_lower for term in terms):
                continue
            name_lower = os.path.basename(path_lower)
            first = terms[0]
            if all(term in name_lower for term in terms):
                if name_lower == first or name_lower == first + os.path.splitext(name_lower)[1]:
                    score = 100.0
                elif name_lower.startswith(first):
                    score = 80.0
                else:
                    score = 60.0
            else:
                score = 25.0  # path-only match
            if is_dir:
                score -= 4.0  # same-name file vs folder: file first
            matches.append((score, path, is_dir))

        matches.sort(key=lambda match: (-match[0], os.path.basename(match[1]).lower(), match[1].lower()))
        total = len(matches)
        normalized_offset = max(0, offset)
        page = matches[normalized_offset : normalized_offset + max(1, page_size)]

        items = []
        for score, path, is_dir in page:
            try:
                stat = os.stat(path)
                modified = datetime.fromtimestamp(stat.st_mtime)
                size = stat.st_size
            except OSError:
                modified, size = None, None
            items.append(
                SearchResultItem(
                    kind=SearchResultKind.FOLDER if is_dir else SearchResultKind.FILE,
                    title=os.path.basename(path),
                    subtitle=None,
                    detail_path=path,
                    modified_at=modified,
                    file_size=size,
                    relevance_score=score,
                )
            )
        next_offset = min(normalized_offset + len(page), total) if page else normalized_offset
        return SearchFileQueryPage(
            items=items,
            total_matched_count=total,
            next_offset=next_offset,
        )

    def _maybe_refresh_stale(self) -> None:
        with self._lock:
            stale = (
                self._state == STATE_READY and time.monotonic() - self._last_scan_finished >= RESCAN_INTERVAL_SECONDS
            )
        if stale:
            self.rescan_async()
