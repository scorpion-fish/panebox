"""AppSettings root: schemaVersion + owned slices (mirrors Models/AppSettings.cs).

Unlike Windows (a flat ~180-property serialization facade), the Linux port
stores slices as nested objects under slice keys — the same wire content,
grouped by owner, which keeps ownership boundaries explicit.
"""

from __future__ import annotations

import dataclasses

from .base import JsonModel
from .settings_slices import (
    CloudBackupSettingsSlice,
    CoreSettingsSlice,
    DesktopOrganizationSettingsSlice,
    FileWidgetSettingsSlice,
    MusicSettingsSlice,
    PerformanceSettingsSlice,
    QuickCaptureSettingsSlice,
    SearchSettingsSlice,
    TodoSettingsSlice,
    WeatherSettingsSlice,
    WidgetShellSettingsSlice,
)

SCHEMA_VERSION = 1


@dataclasses.dataclass
class AppSettings(JsonModel):
    schemaVersion: int = SCHEMA_VERSION
    core: CoreSettingsSlice = dataclasses.field(default_factory=CoreSettingsSlice)
    performance: PerformanceSettingsSlice = dataclasses.field(default_factory=PerformanceSettingsSlice)
    widgetShell: WidgetShellSettingsSlice = dataclasses.field(default_factory=WidgetShellSettingsSlice)
    fileWidget: FileWidgetSettingsSlice = dataclasses.field(default_factory=FileWidgetSettingsSlice)
    todo: TodoSettingsSlice = dataclasses.field(default_factory=TodoSettingsSlice)
    quickCapture: QuickCaptureSettingsSlice = dataclasses.field(default_factory=QuickCaptureSettingsSlice)
    music: MusicSettingsSlice = dataclasses.field(default_factory=MusicSettingsSlice)
    weather: WeatherSettingsSlice = dataclasses.field(default_factory=WeatherSettingsSlice)
    search: SearchSettingsSlice = dataclasses.field(default_factory=SearchSettingsSlice)
    desktopOrganization: DesktopOrganizationSettingsSlice = dataclasses.field(
        default_factory=DesktopOrganizationSettingsSlice
    )
    cloudBackup: CloudBackupSettingsSlice = dataclasses.field(default_factory=CloudBackupSettingsSlice)

    NESTED = {
        "core": (CoreSettingsSlice, "one"),
        "performance": (PerformanceSettingsSlice, "one"),
        "widgetShell": (WidgetShellSettingsSlice, "one"),
        "fileWidget": (FileWidgetSettingsSlice, "one"),
        "todo": (TodoSettingsSlice, "one"),
        "quickCapture": (QuickCaptureSettingsSlice, "one"),
        "music": (MusicSettingsSlice, "one"),
        "weather": (WeatherSettingsSlice, "one"),
        "search": (SearchSettingsSlice, "one"),
        "desktopOrganization": (DesktopOrganizationSettingsSlice, "one"),
        "cloudBackup": (CloudBackupSettingsSlice, "one"),
    }
