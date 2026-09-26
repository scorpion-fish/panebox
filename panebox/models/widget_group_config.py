"""Widget group configuration (mirrors src/PaneBox/Models/WidgetGroupConfig.cs)."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Optional

from .base import JsonModel
from .widget_config import WidgetCompactPlacement, omit_none


class NavigationStyle:
    FOLLOW_DEFAULT = "FollowDefault"
    TABS = "Tabs"
    STACK = "Stack"


class TitleDisplayMode:
    FOLLOW_DEFAULT = "FollowDefault"
    ICON_AND_TEXT = "IconAndText"
    ICON_ONLY = "IconOnly"
    TEXT_ONLY = "TextOnly"


@dataclasses.dataclass
class WidgetGroupConfig(JsonModel):
    id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    surfaceId: str = ""
    name: str = ""
    memberIds: list[str] = dataclasses.field(default_factory=list)
    activeMemberId: str = ""
    isVisible: bool = True
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
    isPositionLocked: bool = False
    isSizeLocked: bool = False
    isCollapsed: bool = False
    compactPlacement: Optional[WidgetCompactPlacement] = omit_none()
    compactWidth: Optional[float] = omit_none()
    navigationStyle: str = NavigationStyle.FOLLOW_DEFAULT
    titleDisplayMode: str = TitleDisplayMode.ICON_AND_TEXT
    wheelSwitchEnabled: Optional[bool] = omit_none()
    hoverSwitchEnabled: Optional[bool] = omit_none()
    chromeMode: str = "Standard"  # groups allow Standard | Compact only
    collapseBehavior: str = "System"

    NESTED = {"compactPlacement": (WidgetCompactPlacement, "one")}

    def resolved_navigation_style(self, default_style: str) -> str:
        style = self.navigationStyle or NavigationStyle.FOLLOW_DEFAULT
        if style == NavigationStyle.FOLLOW_DEFAULT:
            style = default_style or NavigationStyle.TABS
        if style == "Auto":  # legacy value
            return NavigationStyle.STACK
        if style not in (NavigationStyle.TABS, NavigationStyle.STACK):
            return NavigationStyle.TABS
        return style
