"""Resilient JSON store (port of Services/ResilientJsonStore.cs).

Atomic write via tmp+rename (File.Replace semantics: rotate primary -> .bak),
load order primary -> quarantine corrupt -> .bak -> defaults, and load-state
reporting for diagnostics.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class LoadState:
    PRIMARY = "Primary"
    RECOVERED_FROM_BACKUP = "RecoveredFromBackup"
    DEFAULTS_FOR_MISSING_FILE = "DefaultsForMissingFile"
    DEFAULTS_AFTER_FAILURE = "DefaultsAfterFailure"


@dataclass
class LoadResult:
    data: dict[str, Any]
    state: str
    detail: str = ""


class ResilientJsonStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".bak")

    # ---- load ---------------------------------------------------------------

    def load(self) -> LoadResult:
        primary_existed = self.path.exists()
        result = self._try_load_file(self.path, quarantine_as="primary")
        if result is not None:
            return LoadResult(result, LoadState.PRIMARY)
        if self.backup_path.exists():
            data = self._try_load_file(self.backup_path, quarantine_as="backup")
            if data is not None:
                # Restore backup into primary via temp+move.
                try:
                    tmp = self.path.with_name(f"{self.path.name}.restore-{uuid.uuid4().hex[:8]}.tmp")
                    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(tmp, self.path)
                except OSError:
                    pass
                return LoadResult(data, LoadState.RECOVERED_FROM_BACKUP)
            return LoadResult({}, LoadState.DEFAULTS_AFTER_FAILURE, "primary and backup unreadable")
        if not primary_existed:
            return LoadResult({}, LoadState.DEFAULTS_FOR_MISSING_FILE)
        return LoadResult({}, LoadState.DEFAULTS_AFTER_FAILURE)

    def _try_load_file(self, path: Path, quarantine_as: str) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("root is not an object")
            return data
        except (OSError, ValueError) as exc:
            quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}")
            try:
                os.replace(path, quarantine)
            except OSError:
                pass
            self.last_quarantine = (path, quarantine, str(exc), quarantine_as)
            return None

    last_quarantine: tuple | None = None

    # ---- save ---------------------------------------------------------------

    def save(self, data: dict[str, Any]) -> None:
        """Atomic save: write tmp, rotate old primary to .bak, move tmp in."""
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        for attempt in range(3):
            try:
                fd, name = tempfile.mkstemp(dir=str(directory), prefix=f"{self.path.name}.", suffix=".tmp")
                tmp_path = Path(name)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.path.exists():
                    try:
                        os.replace(self.path, self.backup_path)
                    except OSError:
                        # Replace can fail transiently; retry once then fall through.
                        time.sleep(0.05)
                        os.replace(self.path, self.backup_path)
                else:
                    # First save: seed the backup so recovery never lacks a .bak.
                    try:
                        self.backup_path.write_text(payload, encoding="utf-8")
                    except OSError:
                        pass
                os.replace(tmp_path, self.path)
                return
            except OSError:
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))


def debounce(interval: float):
    """Simple trailing debounce for main-thread GLib use.

    The real scheduling lives in SettingsService (GLib timeout); this helper is
    the pure-logic core reused by tests.
    """

    def decorator(func: Callable) -> Callable:
        state = {"generation": 0}

        def bump() -> int:
            state["generation"] += 1
            return state["generation"]

        def current() -> int:
            return state["generation"]

        func.bump = bump  # type: ignore[attr-defined]
        func.current = current  # type: ignore[attr-defined]
        return func

    return decorator
