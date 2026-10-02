"""Central constants: paths, defaults, enums-as-strings shared across modules."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_NAME = "PaneBox"
APP_VERSION = "0.5.1"  # Linux port version; stamped into backup manifests

# Overridable for tests / portable use. The DESKBOX_* names still work as
# legacy aliases from the pre-rename builds.
CONFIG_ROOT = Path(
    os.environ.get("PANEBOX_CONFIG_ROOT")
    or os.environ.get("DESKBOX_CONFIG_ROOT")
    or Path.home() / ".config" / "panebox"
)
DATA_ROOT = Path(
    os.environ.get("PANEBOX_DATA_ROOT")
    or os.environ.get("DESKBOX_DATA_ROOT")
    or Path.home() / ".local" / "share" / "panebox"
)


def migrate_legacy_dirs() -> list[Path]:
    """One-time move from the pre-rename deskbox dirs to the panebox ones.

    Only runs on default (non-overridden) roots, and only when the new dir
    does not exist yet — so repeated calls and test sandboxes are no-ops.
    """
    moved: list[Path] = []
    if os.environ.get("PANEBOX_CONFIG_ROOT") or os.environ.get("PANEBOX_DATA_ROOT"):
        return moved
    legacy_pairs = [
        (Path.home() / ".config" / "deskbox", CONFIG_ROOT),
        (Path.home() / ".local" / "share" / "deskbox", DATA_ROOT),
    ]
    for legacy, current in legacy_pairs:
        try:
            if legacy.is_dir() and not current.exists():
                shutil.copytree(legacy, current)
                moved.append(current)
        except OSError:
            pass  # unreadable/unwritable — fall through to fresh dirs
    # The old autostart entry (deskbox.desktop) would resurrect the dead name.
    old_autostart = Path.home() / ".config" / "autostart" / "deskbox.desktop"
    try:
        old_autostart.unlink(missing_ok=True)
    except OSError:
        pass
    return moved


SETTINGS_FILE = "settings.json"
WIDGET_LAYOUT_FILE = "widget-layout.json"

DATA_DIR = DATA_ROOT / "data"
WIDGETS_DATA_DIR = DATA_DIR / "widgets"
QUICK_CAPTURE_DIR = DATA_DIR / "quick-capture"
GLANCE_DIR = DATA_DIR / "glance"
WEATHER_CACHE_FILE = DATA_DIR / "weather-cache.json"
SEARCH_HISTORY_FILE = DATA_DIR / "search-history.json"
ORG_HISTORY_FILE = DATA_DIR / "desktop-organization-history.json"
RECOVERY_DIR = DATA_ROOT / "recovery"
LOG_FILE = DATA_ROOT / "panebox.log"

DEFAULT_MANAGED_STORAGE_ROOT = Path.home() / "PaneBox"


class WidgetKind:
    FILE = "File"
    QUICK_CAPTURE = "QuickCapture"
    WEATHER = "Weather"
    TODO = "Todo"
    TAGS = "Tags"
    MUSIC = "Music"
    SYSTEM_MONITOR = "SystemMonitor"
    SEARCH = "Search"
    GLANCE = "Glance"
    LEGACY_PRODUCTIVITY = "Productivity"  # migration-only


class ViewMode:
    ICON = "Icon"
    LIST = "List"


class SortMode:
    NAME = "Name"
    SIZE = "Size"
    TYPE = "Type"
    DATE_MODIFIED = "DateModified"
    MANUAL = "Manual"


VALID_SORT_MODES = (
    SortMode.NAME,
    SortMode.SIZE,
    SortMode.TYPE,
    SortMode.DATE_MODIFIED,
    SortMode.MANUAL,
)


class PositionAnchor:
    LEFT_TOP = "LeftTop"
    RIGHT_TOP = "RightTop"
    LEFT_BOTTOM = "LeftBottom"
    RIGHT_BOTTOM = "RightBottom"


# Widget geometry limits (WidgetShellSettingsSlice limits on Windows).
WIDGET_MIN_WIDTH = 50
WIDGET_MIN_HEIGHT = 50
ICON_SIZE_MIN = 24
ICON_SIZE_MAX = 56
ICON_SIZE_STEPS = (24, 28, 32, 36, 40, 48, 56)  # Ctrl+wheel steps
TEXT_SIZE_MIN = 10.0
TEXT_SIZE_MAX = 16.0

# Layout density presets (SettingsService.cs density presets).
DENSITY_PRESETS = {
    "Compact": {
        "icon_size": 26,
        "text_size": 10.5,
        "density": 0.20,
        "h_spacing": 0.20,
        "v_spacing": 0.28,
        "name_width": 0.30,
    },
    "Standard": {
        "icon_size": 30,
        "text_size": 11.5,
        "density": 0.56,
        "h_spacing": 0.40,
        "v_spacing": 0.60,
        "name_width": 0.36,
    },
    "Relaxed": {
        "icon_size": 36,
        "text_size": 13.0,
        "density": 0.84,
        "h_spacing": 0.68,
        "v_spacing": 0.82,
        "name_width": 0.50,
    },
}

# Performance presets (PerformanceSettingsPolicy).
PERFORMANCE_PRESETS = {
    "ResourceSaver": {
        "hidden_cache_cleanup_delay_seconds": 30,
        "visible_idle_cache_cleanup_delay_seconds": 300,
        "transient_window_release_delay_seconds": 120,
        "performance_cache_budget": "Small",
        "hidden_cache_cleanup_scope": "AllRecreatable",
    },
    "Balanced": {
        "hidden_cache_cleanup_delay_seconds": 30,
        "visible_idle_cache_cleanup_delay_seconds": 600,
        "transient_window_release_delay_seconds": 600,
        "performance_cache_budget": "Balanced",
        "hidden_cache_cleanup_scope": "AllRecreatable",
    },
}

# Maximum retained topology layout profiles (WidgetTopologyLayoutService).
MAX_TOPOLOGY_PROFILES = 12

# Debounced settings save interval (SettingsService.SaveDebounced).
SAVE_DEBOUNCE_SECONDS = 1.0

# Interaction constants (WidgetGroupNavigationInteractionPolicy).
WHEEL_STEP = 120
WHEEL_GESTURE_QUIET_PERIOD_MS = 220
GROUP_KEYBOARD_SWITCH_COOLDOWN_MS = 450


# Environment probes
def ensure_dirs() -> None:
    for p in (CONFIG_ROOT, DATA_DIR, WIDGETS_DATA_DIR, QUICK_CAPTURE_DIR, GLANCE_DIR, RECOVERY_DIR):
        p.mkdir(parents=True, exist_ok=True)
