"""Widget layout slice + monitor topology models.

Mirrors WidgetLayoutSettingsSlice.cs and WidgetTopologyLayoutModels.cs. Stored
in widget-layout.json as {"schemaVersion": 1, "layout": {...}}.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Optional

from .base import JsonModel
from .widget_config import WidgetCompactPlacement, WidgetConfig, omit_none
from .widget_group_config import WidgetGroupConfig


@dataclasses.dataclass
class WidgetTopologyMonitorProfile(JsonModel):
    stableId: str = ""
    deviceName: str = ""
    isPrimary: bool = False
    monitorX: int = 0
    monitorY: int = 0
    monitorWidth: int = 0
    monitorHeight: int = 0
    workAreaX: int = 0
    workAreaY: int = 0
    workAreaWidth: int = 0
    workAreaHeight: int = 0
    dpiScale: float = 1.0


@dataclasses.dataclass
class WidgetSurfaceLayoutProfile(JsonModel):
    positionMonitorStableId: Optional[str] = omit_none()
    x: float = 100.0
    y: float = 100.0
    positionAnchor: Optional[str] = omit_none()
    positionMarginX: float = 0.0
    positionMarginY: float = 0.0
    positionMonitorKey: Optional[str] = omit_none()
    positionMonitorDeviceName: Optional[str] = omit_none()
    positionMonitorWasPrimary: Optional[bool] = omit_none()
    boundsCoordinateVersion: int = 1
    width: float = 300.0
    height: float = 400.0
    compactPlacement: Optional[WidgetCompactPlacement] = omit_none()
    compactWidth: Optional[float] = omit_none()

    NESTED = {"compactPlacement": (WidgetCompactPlacement, "one")}


@dataclasses.dataclass
class WidgetTopologyLayoutProfile(JsonModel):
    version: int = 1
    lastUsedAtUtc: str = ""
    monitors: list[WidgetTopologyMonitorProfile] = dataclasses.field(default_factory=list)
    surfaces: dict[str, WidgetSurfaceLayoutProfile] = dataclasses.field(default_factory=dict)

    NESTED = {
        "monitors": (WidgetTopologyMonitorProfile, "list"),
        "surfaces": (WidgetSurfaceLayoutProfile, "map"),
    }


@dataclasses.dataclass
class WidgetLayoutSettingsSlice(JsonModel):
    featureWidgetEnabledStates: dict[str, bool] = dataclasses.field(default_factory=dict)
    widgets: list[WidgetConfig] = dataclasses.field(default_factory=list)
    widgetGroups: list[WidgetGroupConfig] = dataclasses.field(default_factory=list)
    widgetTopologyLayouts: dict[str, WidgetTopologyLayoutProfile] = dataclasses.field(default_factory=dict)
    activeWidgetTopologyKey: Optional[str] = omit_none()
    widgetGroupsEnabled: bool = True
    widgetGroupDefaultNavigationStyle: str = "Tabs"
    widgetGroupDefaultTitleDisplayMode: str = "IconAndText"
    widgetGroupWheelSwitchEnabled: bool = True
    widgetGroupHoverSwitchEnabled: bool = False
    deletedWidgetIds: list[str] = dataclasses.field(default_factory=list)

    NESTED = {
        "widgets": (WidgetConfig, "list"),
        "widgetGroups": (WidgetGroupConfig, "list"),
        "widgetTopologyLayouts": (WidgetTopologyLayoutProfile, "map"),
    }

    def copy_from(self, other: "WidgetLayoutSettingsSlice") -> None:
        """In-place copy (AppSettings.WidgetLayout is get-only on Windows)."""
        self.featureWidgetEnabledStates = dict(other.featureWidgetEnabledStates)
        self.widgets = list(other.widgets)
        self.widgetGroups = list(other.widgetGroups)
        self.widgetTopologyLayouts = dict(other.widgetTopologyLayouts)
        self.activeWidgetTopologyKey = other.activeWidgetTopologyKey
        self.widgetGroupsEnabled = other.widgetGroupsEnabled
        self.widgetGroupDefaultNavigationStyle = other.widgetGroupDefaultNavigationStyle
        self.widgetGroupDefaultTitleDisplayMode = other.widgetGroupDefaultTitleDisplayMode
        self.widgetGroupWheelSwitchEnabled = other.widgetGroupWheelSwitchEnabled
        self.widgetGroupHoverSwitchEnabled = other.widgetGroupHoverSwitchEnabled
        self.deletedWidgetIds = list(other.deletedWidgetIds)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
