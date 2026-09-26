"""Settings service (port of Services/SettingsService.cs core responsibilities).

Owns AppSettings + WidgetLayoutSettingsSlice persistence:
- load with default-seeding for fresh profiles and normalization,
- debounced saves with shutdown flush,
- two-file commit ordering (widget-layout.json first, settings.json second),
- widget config CRUD with deletion tombstones.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from ..constants import (
    CONFIG_ROOT,
    DEFAULT_MANAGED_STORAGE_ROOT,
    DENSITY_PRESETS,
    PERFORMANCE_PRESETS,
    SAVE_DEBOUNCE_SECONDS,
    SETTINGS_FILE,
    VALID_SORT_MODES,
    WIDGET_LAYOUT_FILE,
    WIDGET_MIN_HEIGHT,
    WIDGET_MIN_WIDTH,
    ensure_dirs,
)
from ..models.app_settings import AppSettings
from ..models.layout_slice import WidgetLayoutSettingsSlice, now_iso
from ..models.widget_config import WidgetConfig
from .resilient_json_store import LoadState, ResilientJsonStore

LAYOUT_SCHEMA_VERSION = 1


class SettingsService:
    def __init__(self, config_root: Path = CONFIG_ROOT):
        self.config_root = Path(config_root)
        self.settings_store = ResilientJsonStore(self.config_root / SETTINGS_FILE)
        self.layout_store = ResilientJsonStore(self.config_root / WIDGET_LAYOUT_FILE)
        self.settings = AppSettings()
        self.layout = WidgetLayoutSettingsSlice()
        self.load_state = LoadState.DEFAULTS_FOR_MISSING_FILE
        self.layout_load_state = LoadState.DEFAULTS_FOR_MISSING_FILE
        self.last_persistence_error: str = ""
        self._lock = threading.RLock()
        self._pending_save = False
        self._scheduler: Callable[[float, Callable[[], bool]], object] | None = None
        self._timeout_token = None

    # ---- scheduling ---------------------------------------------------------

    def use_scheduler(self, scheduler: Callable[[float, Callable[[], bool]], object]) -> None:
        """Install a scheduling function (GLib.timeout_add in the app; tests use fakes)."""
        self._scheduler = scheduler

    def _schedule_flush(self) -> None:
        self._pending_save = True
        if self._timeout_token is not None or self._scheduler is None:
            return

        def tick() -> bool:
            # Only one timer is ever active (guarded by _timeout_token), so a
            # fire always flushes whatever is pending — later save_debounced
            # calls must not orphan the data they marked pending.
            self._timeout_token = None
            if self._pending_save:
                self.flush_pending_save()
            return False

        self._timeout_token = self._scheduler(int(SAVE_DEBOUNCE_SECONDS * 1000), tick)

    # ---- load ---------------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            ensure_dirs()
            result = self.settings_store.load()
            self.load_state = result.state
            if result.data:
                self.settings = AppSettings.from_dict(result.data)
            self._seed_fresh_defaults(result.state)
            self._normalize()

            layout_result = self._load_layout()
            self.layout_load_state = layout_result
            if self.layout_load_state == LoadState.PRIMARY:
                pass  # already applied
            elif self.layout_load_state == LoadState.RECOVERED_FROM_BACKUP:
                self.save_now()

    def _load_layout(self) -> str:
        result = self.layout_store.load()
        if result.data:
            envelope = result.data
            layout_data = envelope.get("layout")
            if isinstance(layout_data, dict):
                self.layout = WidgetLayoutSettingsSlice.from_dict(layout_data)
                return result.state
            # Envelope without a usable layout fails closed to defaults (Windows
            # WidgetLayoutStore requires the `layout` member).
            return LoadState.DEFAULTS_AFTER_FAILURE
        return result.state

    def _seed_fresh_defaults(self, state: str) -> None:
        if state != LoadState.DEFAULTS_FOR_MISSING_FILE:
            return
        if not self.settings.fileWidget.defaultManagedStorageRootPath:
            self.settings.fileWidget.defaultManagedStorageRootPath = str(DEFAULT_MANAGED_STORAGE_ROOT)

    def _normalize(self) -> None:
        shell = self.settings.widgetShell
        shell.widgetOpacity = min(1.0, max(0.0, shell.widgetOpacity))
        shell.defaultWidgetWidth = max(WIDGET_MIN_WIDTH, shell.defaultWidgetWidth)
        shell.defaultWidgetHeight = max(WIDGET_MIN_HEIGHT, shell.defaultWidgetHeight)
        shell.iconSize = min(56.0, max(24.0, shell.iconSize))
        shell.textSize = min(16.0, max(10.0, shell.textSize))
        for axis in ("layoutDensityScale", "horizontalSpacingScale", "verticalSpacingScale"):
            setattr(shell, axis, min(1.0, max(0.0, getattr(shell, axis))))

        fw = self.settings.fileWidget
        fw.fileNameLineCount = min(2, max(0, fw.fileNameLineCount))
        fw.fileStackThreshold = min(99, max(2, fw.fileStackThreshold))
        fw.fileStackCustomRules = fw.fileStackCustomRules[:32]
        for rule in fw.fileStackCustomRules:
            rule.extensions = rule.extensions[:64]

        qc = self.settings.quickCapture
        qc.quickCaptureRecentLimit = min(100, max(10, qc.quickCaptureRecentLimit))

        todo = self.settings.todo
        valid_offsets = {0, 5, 10, 15, 30, 60, 1440}
        if todo.todoDefaultReminderOffsetMinutes not in valid_offsets:
            todo.todoDefaultReminderOffsetMinutes = 5

        weather = self.settings.weather
        if weather.weatherRefreshIntervalMinutes not in (15, 30, 60, 180):
            weather.weatherRefreshIntervalMinutes = 60

        for widget in self.layout.widgets:
            if widget.sortMode not in VALID_SORT_MODES:
                widget.sortMode = "Name"
            widget.widgetKind = widget.normalized_kind()
            widget.width = max(WIDGET_MIN_WIDTH, widget.width)
            widget.height = max(WIDGET_MIN_HEIGHT, widget.height)

        # Tombstone pruning: drop tombstones for ids no longer referenced anywhere.
        known = {w.id for w in self.layout.widgets} | {g.id for g in self.layout.widgetGroups}
        self.layout.deletedWidgetIds = [wid for wid in dict.fromkeys(self.layout.deletedWidgetIds) if wid not in known]

    # ---- save ---------------------------------------------------------------

    def save_debounced(self) -> None:
        with self._lock:
            self._schedule_flush()

    def flush_pending_save(self) -> None:
        with self._lock:
            if not self._pending_save:
                return
            self._pending_save = False
            self.save_now()

    def save_now(self) -> None:
        """Two-file commit: layout first, then settings; revert layout on failure."""
        with self._lock:
            layout_payload = {
                "schemaVersion": LAYOUT_SCHEMA_VERSION,
                "layout": self.layout.to_dict(),
            }
            settings_payload = self.settings.to_dict()
            layout_committed = False
            try:
                self.layout_store.save(layout_payload)
                layout_committed = True
                self.settings_store.save(settings_payload)
                self.last_persistence_error = ""
            except OSError as exc:
                self.last_persistence_error = f"{type(exc).__name__}: {exc}"
                if layout_committed and self.layout_store.backup_path.exists():
                    try:
                        import os

                        os.replace(self.layout_store.backup_path, self.layout_store.path)
                    except OSError:
                        pass
                raise

    # ---- widget CRUD --------------------------------------------------------

    def widgets(self) -> list[WidgetConfig]:
        return list(self.layout.widgets)

    def add_widget(self, config: WidgetConfig) -> None:
        with self._lock:
            if config.id in self.layout.deletedWidgetIds:
                self.layout.deletedWidgetIds.remove(config.id)
            self.layout.widgets.append(config)
            self.save_debounced()

    def update_widget(self, config: WidgetConfig) -> None:
        with self._lock:
            if config.id in self.layout.deletedWidgetIds:
                return  # tombstoned ids refuse writes
            for index, existing in enumerate(self.layout.widgets):
                if existing.id == config.id:
                    self.layout.widgets[index] = config
                    break
            else:
                self.layout.widgets.append(config)
            self.save_debounced()

    def remove_widget(self, widget_id: str) -> None:
        with self._lock:
            self.layout.widgets = [w for w in self.layout.widgets if w.id != widget_id]
            self.layout.widgetGroups = [dataclasses_replace_group(g, widget_id) for g in self.layout.widgetGroups]
            self.layout.widgetGroups = [g for g in self.layout.widgetGroups if len(g.memberIds) >= 2]
            if widget_id not in self.layout.deletedWidgetIds:
                self.layout.deletedWidgetIds.append(widget_id)
            self.save_debounced()

    # ---- helpers ------------------------------------------------------------

    def density_values(self) -> dict:
        shell = self.settings.widgetShell
        if shell.layoutDensity in DENSITY_PRESETS:
            return dict(DENSITY_PRESETS[shell.layoutDensity])
        return {
            "icon_size": shell.iconSize,
            "text_size": shell.textSize,
            "density": shell.layoutDensityScale,
            "h_spacing": shell.horizontalSpacingScale,
            "v_spacing": shell.verticalSpacingScale,
            "name_width": self.settings.fileWidget.fileNameWidthScale,
        }

    def performance_values(self) -> dict:
        perf = self.settings.performance
        if perf.performanceMode in PERFORMANCE_PRESETS:
            return dict(PERFORMANCE_PRESETS[perf.performanceMode])
        return {
            "hidden_cache_cleanup_delay_seconds": perf.hiddenCacheCleanupDelaySeconds,
            "visible_idle_cache_cleanup_delay_seconds": perf.visibleIdleCacheCleanupDelaySeconds,
            "transient_window_release_delay_seconds": perf.transientWindowReleaseDelaySeconds,
            "performance_cache_budget": perf.performanceCacheBudget,
            "hidden_cache_cleanup_scope": perf.hiddenCacheCleanupScope,
        }

    def stamp_topology_use(self, key: str) -> None:
        profile = self.layout.widgetTopologyLayouts.get(key)
        if profile is not None:
            profile.lastUsedAtUtc = now_iso()


def dataclasses_replace_group(group, removed_id: str):
    import dataclasses

    return dataclasses.replace(
        group,
        memberIds=[m for m in group.memberIds if m != removed_id],
        activeMemberId="" if group.activeMemberId == removed_id else group.activeMemberId,
    )
