"""Per-widget Glance preference store (port of Services/GlanceWidgetStore.cs).

Separate from app settings so a future sync layer can classify portable
preferences, device-local paths, and disposable media independently. One
JSON document per widget under data/glance/widgets/, with a one-time
migration from the legacy shared data/glance/glance.json.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, List, Optional

from ..models.glance import (
    CURRENT_VERSION,
    GlanceBackgroundSource,
    GlanceCalendarMaterialMode,
    GlanceImageFocus,
    GlanceImageFitMode,
    GlanceLayoutMode,
    GlanceOnlineImageCategory,
    GlanceReadabilityMode,
    GlanceTimeFormatMode,
    GlanceTraditionalCalendarMode,
    GlanceTransitionMode,
    GlanceTransitionSpeed,
    GlanceWidgetData,
)
from .resilient_json_store import ResilientJsonStore

_WIDGET_STORES: dict[str, "GlanceWidgetStore"] = {}
_WIDGET_STORES_LOCK = threading.Lock()

SUPPORTED_ROTATION_INTERVALS = (
    0,
    10 / 60,
    30 / 60,
    1,
    2,
    5,
    10,
    30,
    60,
    360,
    1440,
)


def glance_directory(data_root: str) -> str:
    return os.path.join(data_root, "data", "glance")


def safe_widget_file_name(widget_id: str) -> str:
    widget_id = (widget_id or "").strip()
    if not widget_id:
        raise ValueError("widget id is required")
    return "".join("_" if (c in '/\\:*?"<>|' or ord(c) < 32) else c for c in widget_id)


class GlanceWidgetStore:
    def __init__(self, store_path: str, legacy_store_path: Optional[str] = None):
        os.makedirs(os.path.dirname(store_path), exist_ok=True)
        self._lock = threading.RLock()
        self._store = ResilientJsonStore(Path(store_path))
        self._legacy_store = ResilientJsonStore(Path(legacy_store_path)) if legacy_store_path else None
        self._cached: Optional[GlanceWidgetData] = None
        self._listeners: List[Callable[[], None]] = []

    # ---- construction ---------------------------------------------------------

    @classmethod
    def for_widget(cls, widget_id: str, data_root: str) -> "GlanceWidgetStore":
        base = glance_directory(data_root)
        with _WIDGET_STORES_LOCK:
            store = _WIDGET_STORES.get(widget_id)
            if store is None:
                store = cls(
                    os.path.join(base, "widgets", f"{safe_widget_file_name(widget_id)}.json"),
                    legacy_store_path=os.path.join(base, "glance.json"),
                )
                _WIDGET_STORES[widget_id] = store
            return store

    @classmethod
    def release_widget(cls, widget_id: str) -> None:
        with _WIDGET_STORES_LOCK:
            _WIDGET_STORES.pop(widget_id, None)

    @classmethod
    def cached_store_count(cls) -> int:
        with _WIDGET_STORES_LOCK:
            return len(_WIDGET_STORES)

    @property
    def store_path(self) -> str:
        return str(self._store.path)

    # ---- change notification ---------------------------------------------------

    def add_changed_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def remove_changed_listener(self, listener: Callable[[], None]) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _raise_changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                pass

    # ---- persistence ------------------------------------------------------------

    def _load_document_locked(self) -> GlanceWidgetData:
        result = self._store.load()
        document = result.data if result.data else {}
        return normalize(GlanceWidgetData.from_dict(document))

    def load(self) -> GlanceWidgetData:
        with self._lock:
            self._migrate_legacy_store_if_needed_locked()
            if self._cached is None:
                self._cached = self._load_document_locked()
            return clone(self._cached)

    def save(self, data: GlanceWidgetData) -> None:
        with self._lock:
            self._cached = normalize(clone(data))
            self._store.save(self._cached.to_dict())
        self._raise_changed()

    def update(self, update_action) -> None:
        with self._lock:
            self._migrate_legacy_store_if_needed_locked()
            if self._cached is None:
                self._cached = self._load_document_locked()
            update_action(self._cached)
            self._cached = normalize(self._cached)
            self._store.save(self._cached.to_dict())
        self._raise_changed()

    def reset(self) -> None:
        self.save(GlanceWidgetData())

    def delete(self) -> None:
        with self._lock:
            self._cached = None
            _try_delete(str(self._store.path))
            _try_delete(str(self._store.backup_path))
        self._raise_changed()

    def _migrate_legacy_store_if_needed_locked(self) -> None:
        if self._legacy_store is None or self._legacy_store.path.exists() is False:
            return
        if self._store.path.exists():
            return
        try:
            migrated = normalize(GlanceWidgetData.from_dict(self._legacy_store.load().data))
            self._store.save(migrated.to_dict())
            _try_delete(str(self._legacy_store.path))
            _try_delete(str(self._legacy_store.backup_path))
        except Exception:
            pass


def normalize(data: Optional[GlanceWidgetData]) -> GlanceWidgetData:
    data = data or GlanceWidgetData()
    data.version = CURRENT_VERSION
    seen: set[str] = set()
    paths: List[str] = []
    for path in data.localImagePaths or []:
        cleaned = (path or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(cleaned)
    data.localImagePaths = paths
    data.localFolderPath = None if not (data.localFolderPath or "").strip() else data.localFolderPath.strip()
    if not data.showDate:
        data.showYear = False
    matched = next(
        (i for i in SUPPORTED_ROTATION_INTERVALS if abs(i - data.rotationIntervalMinutes) < 0.0001),
        None,
    )
    data.rotationIntervalMinutes = 30 if matched is None else matched
    data.timeScale = min(max(data.timeScale if _finite(data.timeScale) else 1.0, 0.75), 1.35)
    data.timeFontFamily = None if not (data.timeFontFamily or "").strip() else data.timeFontFamily.strip()
    data.timeFormat = _enum(
        data.timeFormat,
        GlanceTimeFormatMode.FOLLOW_SYSTEM,
        (
            GlanceTimeFormatMode.FOLLOW_SYSTEM,
            GlanceTimeFormatMode.HOUR24,
            GlanceTimeFormatMode.HOUR12,
        ),
    )
    data.layout = _enum(
        data.layout,
        GlanceLayoutMode.CENTERED,
        (
            GlanceLayoutMode.IMMERSIVE,
            GlanceLayoutMode.CENTERED,
            GlanceLayoutMode.EDITORIAL,
            GlanceLayoutMode.CALENDAR,
        ),
    )
    data.backgroundSource = _enum(
        data.backgroundSource,
        GlanceBackgroundSource.BING,
        (
            GlanceBackgroundSource.ONLINE,
            GlanceBackgroundSource.LOCAL_FILES,
            GlanceBackgroundSource.LOCAL_FOLDER,
            GlanceBackgroundSource.BING,
        ),
    )
    data.onlineImageCategory = _enum(
        data.onlineImageCategory,
        GlanceOnlineImageCategory.FEATURED,
        GlanceOnlineImageCategory.ALL,
    )
    data.transition = _enum(
        data.transition,
        GlanceTransitionMode.CROSS_FADE,
        (
            GlanceTransitionMode.NONE,
            GlanceTransitionMode.CROSS_FADE,
            GlanceTransitionMode.SLIDE_FADE,
            GlanceTransitionMode.ZOOM_FADE,
        ),
    )
    data.transitionSpeed = _enum(
        data.transitionSpeed,
        GlanceTransitionSpeed.STANDARD,
        (GlanceTransitionSpeed.FAST, GlanceTransitionSpeed.STANDARD, GlanceTransitionSpeed.RELAXED),
    )
    data.readability = _enum(
        data.readability,
        GlanceReadabilityMode.SOFT,
        (GlanceReadabilityMode.NONE, GlanceReadabilityMode.SOFT, GlanceReadabilityMode.STRONG),
    )
    data.backgroundImageTransparency = (
        min(max(data.backgroundImageTransparency, 0.0), 1.0) if _finite(data.backgroundImageTransparency) else 0.0
    )
    data.calendarMaterialMode = _enum(
        data.calendarMaterialMode,
        GlanceCalendarMaterialMode.FOLLOW_SYSTEM,
        (GlanceCalendarMaterialMode.FOLLOW_SYSTEM, GlanceCalendarMaterialMode.FOLLOW_IMAGE),
    )
    data.calendarImageMaterialTransparency = (
        min(max(data.calendarImageMaterialTransparency, 0.0), 1.0)
        if _finite(data.calendarImageMaterialTransparency)
        else 0.32
    )
    data.traditionalCalendarMode = _enum(
        data.traditionalCalendarMode,
        GlanceTraditionalCalendarMode.NONE,
        (
            GlanceTraditionalCalendarMode.NONE,
            GlanceTraditionalCalendarMode.AUTO,
            GlanceTraditionalCalendarMode.CHINESE_LUNAR,
            GlanceTraditionalCalendarMode.UM_AL_QURA,
            GlanceTraditionalCalendarMode.HIJRI,
            GlanceTraditionalCalendarMode.INDIAN_SAKA,
            GlanceTraditionalCalendarMode.JAPANESE_ERA,
            GlanceTraditionalCalendarMode.BANGLA,
            GlanceTraditionalCalendarMode.JULIAN,
            GlanceTraditionalCalendarMode.HEBREW,
            GlanceTraditionalCalendarMode.PERSIAN,
            GlanceTraditionalCalendarMode.THAI_BUDDHIST,
        ),
    )
    data.imageFit = _enum(
        data.imageFit,
        GlanceImageFitMode.FILL,
        (GlanceImageFitMode.FILL, GlanceImageFitMode.FIT),
    )
    data.imageFocus = _enum(
        data.imageFocus,
        GlanceImageFocus.CENTER,
        (
            GlanceImageFocus.CENTER,
            GlanceImageFocus.TOP,
            GlanceImageFocus.BOTTOM,
            GlanceImageFocus.LEFT,
            GlanceImageFocus.RIGHT,
        ),
    )
    return data


def clone(data: GlanceWidgetData) -> GlanceWidgetData:
    return GlanceWidgetData.from_dict(data.to_dict())


def _finite(value: float) -> bool:
    import math

    return isinstance(value, (int, float)) and math.isfinite(value)


def _enum(value: str, fallback: str, supported) -> str:
    return value if value in supported else fallback


def _try_delete(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
