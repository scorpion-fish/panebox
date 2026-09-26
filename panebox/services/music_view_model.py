"""Pure presentation logic for the Music widget (port of the calculable parts
of MusicWidgetViewModel.cs + MusicWidgetContent.xaml.cs's static resolvers).

No GTK imports — unit-testable headless. The surface owns state and threads.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .music_service import (
    MusicPlaybackMode,
    MusicPlaybackState,
)

# MusicWidgetContent.xaml.cs responsive constants.
MINIMUM_RESPONSIVE_WIDTH = 180.0
WIDE_RESPONSIVE_WIDTH = 320.0
MINIMUM_RESPONSIVE_HEIGHT = 180.0
WIDE_RESPONSIVE_HEIGHT = 240.0
WIDE_ALBUM_ART_SIZE = 82.0
MINIMUM_ALBUM_ART_SIZE = 60.0
WIDE_TRANSPORT_BUTTON_SIZE = 32.0
COMPACT_TRANSPORT_BUTTON_SIZE = 28.0
INLINE_VOLUME_PANEL_MAXIMUM_WIDTH = 238.0
INLINE_VOLUME_PANEL_HORIZONTAL_INSET = 6.0

RECORD_VINYL_MINIMUM_SIZE = 64.0
RECORD_VINYL_MAXIMUM_SIZE = 230.0
RECORD_SMALL_SIZE = 110.0
RECORD_RESERVED_BASE = 108.0
RECORD_RESERVED_PADDING = 24.0
RECORD_MODE_BUTTON_MINIMUM_WIDTH = 190.0

# MusicWidgetViewModel.cs timing constants.
INFO_SETTLE_DELAY_MS = 180
VOLUME_SETTLE_DELAY_MS = 260
PLAY_PAUSE_SETTLE_DELAY_MS = 180
SEEK_COMMIT_SETTLE_DELAY_MS = 260
EMPTY_INFO_RETRIES = 4
COVER_RETRIES = 3
REVEAL_FRESHNESS_SECONDS = 30.0
PROGRESS_TIMER_COMPACT_MS = 1000
PROGRESS_TIMER_ACTIVE_MS = 500

# Text glyphs standing in for Segoe MDL2 icons (PlaybackModeGlyphs).
PLAYBACK_MODE_GLYPHS: Dict[str, str] = {
    MusicPlaybackMode.NORMAL: "→",
    MusicPlaybackMode.SHUFFLE: "⇄",
    MusicPlaybackMode.REPEAT: "⟳",
}


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def lerp(first: float, second: float, ratio: float) -> float:
    return first + (second - first) * ratio


def clamp_ratio(value: float, minimum: float, maximum: float) -> float:
    return clamp((value - minimum) / (maximum - minimum), 0.0, 1.0)


def normalize_display_mode(mode: Optional[str]) -> str:
    """SettingsService.NormalizeMusicDisplayMode."""
    return mode if mode in ("Auto", "Cover", "Controls", "RecordVertical", "RecordHorizontal") else "Auto"


def should_use_minimal_layout(width: float, height: float, display_mode: Optional[str]) -> bool:
    """MusicWidgetContent.ShouldUseMinimalLayout(width, height, displayMode)."""
    mode = normalize_display_mode(display_mode)
    if mode == "Cover":
        return True
    if mode == "Controls":
        return False
    return width < MINIMUM_RESPONSIVE_WIDTH or height < MINIMUM_RESPONSIVE_HEIGHT


def resolve_layout(width: float, height: float, display_mode: Optional[str]) -> str:
    """One of Minimal / Controls / RecordVertical / RecordHorizontal."""
    mode = normalize_display_mode(display_mode)
    if mode == "RecordVertical":
        return "RecordVertical"
    if mode == "RecordHorizontal":
        return "RecordHorizontal"
    return "Minimal" if should_use_minimal_layout(width, height, mode) else "Controls"


def resolve_transport_button_size(width: float) -> float:
    return round(
        lerp(
            COMPACT_TRANSPORT_BUTTON_SIZE,
            WIDE_TRANSPORT_BUTTON_SIZE,
            clamp_ratio(width, MINIMUM_RESPONSIVE_WIDTH, WIDE_RESPONSIVE_WIDTH),
        )
    )


def resolve_inline_volume_panel_width(width: float) -> float:
    available = max(0.0, width - INLINE_VOLUME_PANEL_HORIZONTAL_INSET * 2)
    return min(INLINE_VOLUME_PANEL_MAXIMUM_WIDTH, available)


def resolve_content_grid_metrics(width: float, height: float) -> Dict[str, float]:
    """The lerp table from ApplyResponsiveLayout's Controls branch."""
    width_ratio = clamp_ratio(width, MINIMUM_RESPONSIVE_WIDTH, WIDE_RESPONSIVE_WIDTH)
    height_ratio = clamp_ratio(height, MINIMUM_RESPONSIVE_HEIGHT, WIDE_RESPONSIVE_HEIGHT)
    density_ratio = min(width_ratio, height_ratio)
    return {
        "albumSize": round(lerp(MINIMUM_ALBUM_ART_SIZE, WIDE_ALBUM_ART_SIZE, density_ratio)),
        "transportButtonSize": resolve_transport_button_size(width),
        "contentPadding": round(lerp(8, 12, density_ratio)),
        "columnSpacing": round(lerp(8, 12, width_ratio)),
        "rowSpacing": round(lerp(4, 8, height_ratio)),
    }


def resolve_record_metrics(width: float, height: float) -> Dict[str, object]:
    """ApplyRecordLayoutSizing: disc sizing + small-mode typography swap."""
    width_ratio = clamp_ratio(width, MINIMUM_RESPONSIVE_WIDTH, WIDE_RESPONSIVE_WIDTH)
    transport = resolve_transport_button_size(width)
    reserved = RECORD_RESERVED_BASE + transport + RECORD_RESERVED_PADDING
    vinyl = clamp(min(width - 24, height - reserved), RECORD_VINYL_MINIMUM_SIZE, RECORD_VINYL_MAXIMUM_SIZE)
    small = vinyl < RECORD_SMALL_SIZE
    return {
        "transportButtonSize": transport,
        "controlsSpacing": round(lerp(4, 10, width_ratio)),
        "modeButtonVisible": width >= RECORD_MODE_BUTTON_MINIMUM_WIDTH,
        "vinylSize": vinyl,
        "small": small,
        "padding": 8 if small else 12,
        "rowSpacing": 4 if small else 6,
        "titleFontSize": 12.5 if small else 14,
        "artistFontSize": 10.5 if small else 11.5,
        "progressFontSize": 10 if small else 11,
        "tonearmVisible": not small,
    }


# ---- formatting (MusicWidgetViewModel) -----------------------------------------


def format_time(seconds: Optional[float]) -> str:
    total = max(0, int(math.floor(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours >= 1:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def normalize_volume(value: Optional[float]) -> float:
    return clamp(round(value or 0.0, 3), 0.0, 1.0)


def format_percent(value: Optional[float]) -> str:
    return f"{int(round((value or 0.0) * 100))}%"


def get_next_playback_mode(mode: str) -> str:
    return {
        MusicPlaybackMode.NORMAL: MusicPlaybackMode.SHUFFLE,
        MusicPlaybackMode.SHUFFLE: MusicPlaybackMode.REPEAT,
        MusicPlaybackMode.REPEAT: MusicPlaybackMode.NORMAL,
    }.get(mode, MusicPlaybackMode.SHUFFLE)


def get_playback_mode_glyph(mode: str) -> str:
    return PLAYBACK_MODE_GLYPHS.get(mode) or PLAYBACK_MODE_GLYPHS[MusicPlaybackMode.NORMAL]


def status_text_key(playback_state: str, has_session: bool) -> str:
    """Maps service state → i18n key for the status line."""
    if not has_session:
        return "Music.Status.NoSession"
    return {
        MusicPlaybackState.PLAYING: "Music.Status.Playing",
        MusicPlaybackState.PAUSED: "Music.Status.Paused",
        MusicPlaybackState.STOPPED: "Music.Status.Stopped",
    }.get(playback_state, "Music.Status.Ready")


def resolve_session_list(
    options: List,
    preferred_session_id: Optional[str],
) -> Tuple[List[object], Optional[str]]:
    """RefreshSessionList order: preferred → IsSystemCurrent → Playing → name,
    with display-name disambiguation and preferred-drop when it vanished."""
    from .music_service import (
        MusicPlaybackState,
        disambiguate_source_display_names,
    )

    valid = [option for option in options]
    preferred = next((option for option in valid if option.sessionId == preferred_session_id), None)
    ordered = sorted(
        valid,
        key=lambda option: (
            0 if option.sessionId == preferred_session_id else 1,
            0 if option.isSystemCurrent else 1,
            0 if option.playbackState == MusicPlaybackState.PLAYING else 1,
            option.sourceDisplayName.lower(),
        ),
    )
    names = disambiguate_source_display_names([option.sourceDisplayName for option in ordered])
    for option, name in zip(ordered, names):
        option.sourceDisplayName = name
    return ordered, (preferred.sessionId if preferred else None)


def cover_signature(session_id: Optional[str], title: str, artist: str, album: str) -> str:
    """MusicWidgetViewModel.MediaInfo cover cache key."""
    return "\x1e".join((session_id or "", title or "", artist or "", album or ""))


# ---- artwork ambience (dominant color, ported from MediaInfo partial) -----------
# Weight = (0.55 + channelSpread/255) * (alpha/255); pixels with alpha < 24 skipped.


def compute_dominant_color(pixels, width: int, height: int) -> Optional[Tuple[int, int, int]]:
    """pixels: (w, h, 4) RGBA byte array (PIL "RGBA" tobytes order)."""
    if pixels is None or width <= 0 or height <= 0:
        return None
    step_x = max(1, width // 32)
    step_y = max(1, height // 32)
    red_total = green_total = blue_total = weight_total = 0.0
    for y in range(0, height, step_y):
        base = y * width * 4
        for x in range(0, width, step_x):
            offset = base + x * 4
            red = pixels[offset]
            green = pixels[offset + 1]
            blue = pixels[offset + 2]
            alpha = pixels[offset + 3]
            if alpha < 24:
                continue
            spread = max(red, green, blue) - min(red, green, blue)
            weight = (0.55 + spread / 255.0) * (alpha / 255.0)
            red_total += red * weight
            green_total += green * weight
            blue_total += blue * weight
            weight_total += weight
    if weight_total <= 0:
        return None
    return (
        int(red_total / weight_total),
        int(green_total / weight_total),
        int(blue_total / weight_total),
    )


def artwork_backdrop_stops(dominant: Optional[Tuple[int, int, int]]) -> List[Tuple[float, str]]:
    """ArtworkBackdrop gradient: ARGB stops 0x5A/0x20/0x00 at 0/0.48/1."""
    base = dominant or (64, 64, 64)
    return [
        (0.0, f"rgba({base[0]},{base[1]},{base[2]},{0x5A / 255:.3f})"),
        (0.48, f"rgba({base[0]},{base[1]},{base[2]},{0x20 / 255:.3f})"),
        (1.0, f"rgba({base[0]},{base[1]},{base[2]},0.000)"),
    ]
