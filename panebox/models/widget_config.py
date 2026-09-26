"""Widget configuration models (mirrors src/PaneBox/Models/WidgetConfig.cs)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .base import JsonModel

CURRENT_BOUNDS_COORDINATE_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    from dateutil import parser as date_parser

    try:
        parsed = date_parser.isoparse(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def omit_none():
    return dataclasses.field(default=None, metadata={"omit_when_none": True})


@dataclasses.dataclass
class WidgetItemConfig(JsonModel):
    """A persisted item (file/folder/shortcut) reference inside a widget."""

    path: str = ""
    sortOrder: int = 0


@dataclasses.dataclass
class WidgetCompactPlacement(JsonModel):
    x: float = 0.0
    y: float = 0.0
    positionAnchor: Optional[str] = omit_none()
    positionMarginX: float = 0.0
    positionMarginY: float = 0.0
    positionMonitorKey: Optional[str] = omit_none()
    positionMonitorDeviceName: Optional[str] = omit_none()
    positionMonitorWasPrimary: Optional[bool] = omit_none()
    boundsCoordinateVersion: int = CURRENT_BOUNDS_COORDINATE_VERSION


@dataclasses.dataclass
class WidgetConfig(JsonModel):
    id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Deskbox"
    isDefaultTitle: bool = True
    x: float = 100.0
    y: float = 100.0
    needsInitialPlacement: bool = False
    positionAnchor: Optional[str] = omit_none()
    positionMarginX: float = 0.0
    positionMarginY: float = 0.0
    positionMonitorKey: Optional[str] = omit_none()
    positionMonitorDeviceName: Optional[str] = omit_none()
    positionMonitorWasPrimary: Optional[bool] = omit_none()
    boundsCoordinateVersion: int = 0
    width: float = 300.0
    height: float = 400.0
    widgetKind: str = "File"  # WidgetKind string; unknown values coerce to File on load
    viewMode: str = "Icon"  # Icon | List
    iconSizeOverride: Optional[float] = omit_none()
    isVisible: bool = True
    isDisabled: bool = False
    isPositionLocked: bool = False
    isSizeLocked: bool = False
    isCollapsed: bool = False
    compactPlacement: Optional[WidgetCompactPlacement] = omit_none()
    compactWidth: Optional[float] = omit_none()
    metadata: dict[str, str] = dataclasses.field(default_factory=dict)
    mappedFolderPath: Optional[str] = None
    followsDefaultStoragePath: bool = False
    managedFolderName: Optional[str] = None
    sortMode: str = "Name"  # Name | Size | Type | DateModified | Manual
    sortDescending: bool = False
    items: list[WidgetItemConfig] = dataclasses.field(default_factory=list)
    fileAddedAtByPath: dict[str, str] = dataclasses.field(default_factory=dict)
    fileAddedAtTrackingInitialized: bool = False

    NESTED = {
        "compactPlacement": (WidgetCompactPlacement, "one"),
        "items": (WidgetItemConfig, "list"),
    }

    # ---- helpers -----------------------------------------------------------

    def added_at(self, path: str) -> Optional[datetime]:
        return parse_iso(self.fileAddedAtByPath.get(path))

    def record_added(self, path: str, when: Optional[datetime] = None) -> None:
        stamp = iso(when or _now())
        if stamp and path not in self.fileAddedAtByPath:
            self.fileAddedAtByPath[path] = stamp

    def normalized_kind(self) -> str:
        from ..constants import WidgetKind

        kind = (self.widgetKind or "File").strip()
        valid = {
            WidgetKind.FILE,
            WidgetKind.QUICK_CAPTURE,
            WidgetKind.WEATHER,
            WidgetKind.TODO,
            WidgetKind.TAGS,
            WidgetKind.MUSIC,
            WidgetKind.SYSTEM_MONITOR,
            WidgetKind.SEARCH,
            WidgetKind.GLANCE,
            WidgetKind.LEGACY_PRODUCTIVITY,
        }
        if kind not in valid:
            return WidgetKind.FILE
        if kind == WidgetKind.LEGACY_PRODUCTIVITY:
            return WidgetKind.FILE
        return kind

    def manual_order_paths(self) -> list[str]:
        return [item.path for item in sorted(self.items, key=lambda i: i.sortOrder)]


def coerce_widget_config(data: dict[str, Any] | WidgetConfig | None) -> WidgetConfig:
    if isinstance(data, WidgetConfig):
        return data
    config = WidgetConfig.from_dict(data if isinstance(data, dict) else {})
    config.widgetKind = config.normalized_kind()
    return config
