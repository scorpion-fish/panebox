"""Debounced folder watcher (watchdog inotify) — port of FolderWatcherService.

Raw FSEvents land on the observer thread, accumulate into an event set, and a
250ms trailing debounce (matching PaneBox) coalesces them before the callback
fires — marshalled to the GLib main loop. `pause()` suppresses events around
operations that would otherwise echo back (case-only renames, our own drops).
"""

from __future__ import annotations

import threading
from typing import Callable, Optional, Set

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

DEBOUNCE_DELAY_MS = 250


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "FolderWatcher"):
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._watcher._record(event)


class FolderWatcher:
    """Watches one directory (non-recursive) with debounced change delivery."""

    def __init__(
        self,
        path: str,
        on_change: Callable[[Set[str]], None],
        debounce_ms: int = DEBOUNCE_DELAY_MS,
        dispatch: Optional[Callable[[Callable[[], None]], None]] = None,
        recursive: bool = False,
    ):
        self.path = path
        self.on_change = on_change
        self.debounce_ms = debounce_ms
        self._recursive = recursive
        self._dispatch = dispatch or _default_dispatch
        self._lock = threading.Lock()
        self._pending: Set[str] = set()
        self._timer: Optional[threading.Timer] = None
        self._paused = False
        self._observer: Optional[Observer] = None
        self._failure_count = 0

    # ---- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        if self._observer is not None:
            return True
        try:
            self._observer = Observer(timeout=0.05)
            self._observer.schedule(_Handler(self), self.path, recursive=self._recursive)
            self._observer.start()
        except Exception:
            self._observer = None
            return False
        return True

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending.clear()
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._observer is not None

    # ---- pause (suppress echoes of our own operations) --------------------------

    def __enter__(self) -> "FolderWatcher":
        self.pause()
        return self

    def __exit__(self, *_exc) -> None:
        self.resume()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        # Drop anything that leaked while paused.
        with self._lock:
            self._pending.clear()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # ---- events ------------------------------------------------------------------

    def _record(self, event: FileSystemEvent) -> None:
        if self._paused or self._observer is None:
            return
        src = getattr(event, "dest_path", None) or event.src_path
        with self._lock:
            self._pending.add(str(src))
            if event.event_type in ("moved", "created"):
                dest = getattr(event, "dest_path", None)
                if dest:
                    self._pending.add(str(dest))
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self.debounce_ms / 1000.0, self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            pending = self._pending
            self._pending = set()
        if not pending:
            return
        try:
            self._dispatch(lambda: self.on_change(pending))
        except Exception:
            self._failure_count += 1


def _default_dispatch(fn: Callable[[], None]) -> None:
    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        GLib.idle_add(fn)
    except Exception:
        fn()
