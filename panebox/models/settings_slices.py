"""Settings slices mirroring PaneBox (Models/*SettingsSlice.cs), field names = wire keys.

All slices for every widget family live here so M2 features bind against a
stable schema from day one.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Optional

from .base import JsonModel
from .widget_config import omit_none


@dataclasses.dataclass
class DesktopOrganizationRule(JsonModel):
    """Stable routing rule; the target is a widget id, never a display name."""

    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    targetWidgetId: str = ""
    isEnabled: bool = True
    categoryIds: list[str] = dataclasses.field(default_factory=list)
    subtypeIds: list[str] = dataclasses.field(default_factory=list)
    extensions: list[str] = dataclasses.field(default_factory=list)
    excludedExtensions: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class FileStackCustomRule(JsonModel):
    id: str = ""
    name: str = ""
    extensions: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CoreSettingsSlice(JsonModel):
    theme: str = "System"  # System | Light | Dark
    trayIconStyle: str = "Colorful"  # System | Colorful | Black | White
    language: str = "System"
    accentColorMode: str = "System"  # System | Custom
    customAccentColor: str = "#0078D4"
    autoStart: bool = True
    autoStartDefaultApplied: bool = False
    autoStartMode: Optional[str] = omit_none()  # None | "Standard" | "SystemdUnit" (Linux)
    autoCheckForUpdates: bool = True
    lastUpdateCheckAt: Optional[str] = omit_none()
    globalHotkeyEnabled: bool = True
    globalHotkeyActivationKind: str = "Chord"  # Chord | DoubleControl | WinSpace | WindowsTap
    globalHotkeyModifiers: int = 0
    globalHotkeyKey: int = 65476  # F7 keysym
    desktopDoubleClickEnabled: bool = False
    hasCompletedOnboarding: bool = False
    onboardingStepIndex: int = 0
    completedOnboardingVersion: int = 0
    hasResolvedInitialFileWidgetSetup: bool = False


@dataclasses.dataclass
class WidgetShellSettingsSlice(JsonModel):
    # geometry / material
    defaultWidgetWidth: float = 280.0
    defaultWidgetHeight: float = 400.0
    widgetOpacity: float = 0.80
    widgetMaterialType: str = "Mica"  # Mica | MicaAlt | Acrylic | AcrylicBase | Solid
    widgetMaterialIntensity: float = 0.65
    widgetForegroundMode: str = "FollowTheme"  # FollowTheme | Light | Dark | Custom
    widgetForegroundColor: str = "#F5F5F5"
    widgetBorderColorMode: str = "Neutral"  # Neutral | Accent | None
    widgetBorderStyle: str = "Thin"  # Thin | Medium | Thick
    widgetCornerPreference: str = "Round"  # Square | Small | Round
    # show/hide animation
    widgetAnimationEffect: str = "SlideFade"
    widgetAnimationSpeed: str = "Standard"
    widgetAnimationSlideDirection: str = "Right"
    widgetAnimationEasingIntensity: str = "Standard"
    # layering / chrome
    widgetLayerMode: str = "Dynamic"  # Dynamic | DesktopPinned | QuickReveal
    keepWidgetsVisibleOnShowDesktop: bool = True
    displayWidgetChromeMode: str = "Overlay"
    interactiveWidgetChromeMode: str = "Standard"
    widgetTitleIconMode: str = "Color"
    showHoverButtons: bool = True
    widgetHoverButtonActions: str = "Add,More"
    # capsule / compact
    widgetCollapseBehavior: str = "Expanded"  # Expanded | Click | Smart
    widgetCompactWidthMode: str = "Aligned"  # Aligned | Independent
    widgetCompactExpansionDirection: str = "Down"  # Auto | Down | Up
    widgetCapsuleArrangementMode: str = "Free"  # Free | Bar
    widgetCapsuleBarSpacing: float = 8.0
    widgetCapsuleBarPlacement: str = "Floating"  # Floating | Top | Bottom | Left | Right
    widgetCapsuleBarDirection: str = "Auto"  # Auto | Horizontal | Vertical
    widgetCapsuleBarOrder: list[str] = dataclasses.field(default_factory=list)
    widgetCompactContentMode: str = "Smart"  # Smart | Minimal | Summary
    widgetCompactHideSensitiveContent: bool = False
    widgetCompactAnimationEffect: str = "Slow"
    widgetCompactAnimationDurationMs: int = 360
    widgetCompactExpandDelayMs: int = 100
    widgetCompactCollapseDelayMs: int = 200
    # snap / density
    resizeSnapEnabled: bool = True
    widgetSnapSpacing: float = 5.0
    focusClickedWidgetOnRaise: bool = False
    iconSize: float = 30.0
    textSize: float = 11.5
    layoutDensity: str = "Standard"  # Compact | Standard | Relaxed | Custom
    layoutDensityScale: float = 0.56
    horizontalSpacingScale: float = 0.40
    verticalSpacingScale: float = 0.60
    # title bar
    titleFontSize: float = 11.5
    titleColor: str = ""  # "" = follow the theme


@dataclasses.dataclass
class FileWidgetSettingsSlice(JsonModel):
    doubleClickToOpen: bool = True
    fileWidgetFolderOpenBehavior: str = "Explorer"  # Explorer | Embedded
    fileItemSystemContextMenuEnabled: bool = False
    hideShortcutArrowOverlay: bool = True
    showImageFilesAsIcons: bool = False
    showListItemDetails: bool = False
    showFileItemPathTooltips: bool = True
    fileStacksEnabled: bool = True
    fileStackAutoStacking: bool = False
    fileStackGroupBy: str = "Kind"  # Kind | DateAdded | DateModified | Custom
    fileStackThreshold: int = 3
    fileStackOrderBy: str = "Widget"  # Widget | Name | DateAdded | DateModified
    fileStackOpenMode: str = "Inline"  # Inline | Popover
    fileStackPopoverLayout: str = "Grid3"  # Adaptive | Grid3 | Grid5
    fileStackPopoverStyle: str = "Neutral"  # FollowMaterial | Neutral
    fileStackCustomRules: list[FileStackCustomRule] = dataclasses.field(default_factory=list)
    fileStackUnmatchedBehavior: str = "KeepLoose"  # KeepLoose | Other
    managedDropAction: str = "Move"  # Move | Copy | FollowSystem
    defaultManagedStorageRootPath: str = ""
    managedStorageDesktopShortcutEnabled: bool = False
    managedStorageDesktopShortcutPath: str = ""
    fileNameWidthScale: float = 0.36
    fileNameLineCount: int = 2  # 1-2; 0 hides labels
    showFileExtensions: bool = False
    hideShortcutExtensionWhenShowingFileExtensions: bool = True

    NESTED = {"fileStackCustomRules": (FileStackCustomRule, "list")}


@dataclasses.dataclass
class PerformanceSettingsSlice(JsonModel):
    performanceMode: str = "ResourceSaver"  # ResourceSaver | Balanced | Custom
    hiddenCacheCleanupDelaySeconds: int = 30
    hiddenCacheCleanupScope: str = "AllRecreatable"  # Warm | AllRecreatable
    visibleIdleCacheCleanupDelaySeconds: int = 300
    idleWorkingSetTrimEnabled: bool = True
    immediateHiddenWorkingSetTrimEnabled: bool = True
    quiescenceWorkingSetTrimEnabled: bool = True
    transientWindowReleaseDelaySeconds: int = 120
    performanceCacheBudget: str = "Small"  # Small | Balanced | Large
    enableContinuousDecorativeAnimations: bool = True
    enableTextMarqueeAnimations: bool = True
    enableVinylRotationAnimations: bool = True
    enableGlanceImageAutoRotation: bool = True
    enableCompactAmbientAnimations: bool = True


@dataclasses.dataclass
class TodoSettingsSlice(JsonModel):
    todoEnabled: bool = False
    todoNewTaskPosition: str = "Top"  # Top | Bottom
    todoTabStyle: str = "Button"  # Pivot | Button
    todoShowTabBar: bool = True
    todoShowAllTab: bool = True
    todoShowActiveTab: bool = False
    todoShowTodayTab: bool = True
    todoShowThisWeekTab: bool = False
    todoShowThisMonthTab: bool = False
    todoShowImportantTab: bool = True
    todoShowCompletedTab: bool = True
    todoDefaultFilter: str = "All"  # All | Active | Today | ThisWeek | ThisMonth | Important | Completed
    todoShowCompletedTasks: bool = False
    todoItemPreviewLineCount: int = 2
    todoListTextSize: float = 0.0  # 0 = follow global
    todoContentTextSize: float = 0.0
    todoEditorEnterBehavior: str = "CtrlEnterSaves"
    todoShowFooterStats: bool = False
    todoShowClearCompletedButton: bool = True
    todoReminderEnabled: bool = True
    todoDefaultReminderOffsetMinutes: int = 5
    todoLayoutMode: str = ""  # "" | Auto | SinglePane | DualPane
    todoAutoSelectFirstInWideLayout: bool = True


@dataclasses.dataclass
class QuickCaptureSettingsSlice(JsonModel):
    quickCaptureEnabled: bool = False
    quickCaptureClipboardEnabled: bool = False
    quickCaptureImageClipboardEnabled: bool = False
    quickCaptureRecentLimit: int = 30  # clamped 10..100
    quickCaptureShowCreatedTime: bool = True
    quickCaptureItemPreviewLineCount: int = 3
    quickCaptureListTextSize: float = 0.0
    quickCaptureContentTextSize: float = 0.0
    quickCaptureEditorEnterBehavior: str = "CtrlEnterSaves"
    quickCaptureDefaultFormat: str = "Markdown"  # Markdown | PlainText
    quickCaptureWideLayout: str = "Auto"  # Auto | SinglePane | DualPane
    quickCaptureWideOpenMode: str = "Reading"  # Reading | Editing
    quickCaptureAllowRemoteImages: bool = False
    attachmentStorageMode: str = "Link"  # Link | Copy (shared with Todo)
    quickCaptureDefaultView: str = "Records"  # Records | Pinned | Recent
    quickCaptureTabStyle: str = "Button"  # Pivot | Button
    quickCaptureShowTabBar: bool = True
    quickCaptureShowRecordsTab: bool = True
    quickCaptureShowPinnedTab: bool = True
    quickCaptureShowRecentTab: bool = True
    lastQuickCaptureFileWidgetId: str = ""


@dataclasses.dataclass
class MusicSettingsSlice(JsonModel):
    musicUseArtworkBackdrop: bool = True
    musicEnableCoverHoverMotion: bool = True
    musicDisplayMode: str = "Auto"  # Auto | Cover | Controls | RecordVertical | RecordHorizontal


@dataclasses.dataclass
class WeatherSettingsSlice(JsonModel):
    weatherAutoLocation: bool = True
    weatherCityName: str = ""
    weatherLatitude: float = 0.0
    weatherLongitude: float = 0.0
    weatherTemperatureUnit: str = "Celsius"  # Celsius | Fahrenheit
    weatherWindSpeedUnit: str = "kmh"  # kmh | ms | mph
    weatherDataSource: str = "OpenMeteo"  # OpenMeteo | MSN (Linux default: Open-Meteo primary)
    weatherDefaultView: str = "Today"  # Today | Week
    weatherSkin: str = "Standard"  # Standard | Rich
    weatherShowForecast: bool = True
    weatherShowSunrise: bool = True
    weatherShowUvIndex: bool = True
    weatherShowPrecipitation: bool = True
    weatherShowHumidity: bool = True
    weatherShowWind: bool = True
    weatherShowPressure: bool = False
    weatherRefreshIntervalMinutes: int = 60  # 15 | 30 | 60 | 180


@dataclasses.dataclass
class SearchSettingsSlice(JsonModel):
    searchHotkeyEnabled: bool = False
    searchHotkeyModifiers: int = 8  # Alt mask
    searchHotkeyKey: int = 0x64  # D
    searchDisplayMode: str = "Spotlight"  # Spotlight | Home | Palette
    searchIncludePaneBoxContent: bool = True
    searchIndexEnabled: bool = True  # Linux port: own file index (replaces Everything consent)
    searchIndexRoots: list[str] = dataclasses.field(default_factory=list)  # empty = defaults (home)
    searchShowRecommendations: bool = True
    searchSaveHistory: bool = True
    searchMaxResults: int = 100
    searchDefaultTab: str = "all"  # all | app | file | panebox
    searchAppIconAnimation: int = 0


@dataclasses.dataclass
class DesktopOrganizationSettingsSlice(JsonModel):
    desktopOrganizationRules: list[DesktopOrganizationRule] = dataclasses.field(default_factory=list)
    desktopAutoOrganizationEnabled: bool = False
    desktopAutoOrganizationBaselineUtc: Optional[str] = omit_none()

    NESTED = {"desktopOrganizationRules": (DesktopOrganizationRule, "list")}


@dataclasses.dataclass
class CloudBackupSettingsSlice(JsonModel):
    cloudBackupEnabled: bool = False
    cloudBackupProvider: str = "none"  # none | webdav
    cloudBackupUrl: str = ""
    cloudBackupRemotePath: str = ""  # normalized default: PaneBox/backups
    cloudBackupUsername: str = ""
    cloudBackupPasswordStored: bool = False  # actual secret in libsecret
    cloudBackupIntervalHours: int = 24
    cloudBackupTodoEnabled: bool = True
    cloudBackupQuickCaptureEnabled: bool = True
    cloudBackupWidgetStyleEnabled: bool = True
    cloudBackupRetainCount: int = 10
    cloudBackupLastRunAt: Optional[str] = omit_none()
    cloudBackupLastResult: str = ""  # "" | ok | error:<msg> | awaiting-confirmation
    cloudBackupLastSuccessUtc: Optional[str] = omit_none()
    cloudBackupLastFailureUtc: Optional[str] = omit_none()
    cloudBackupLastUnverifiedUtc: Optional[str] = omit_none()
