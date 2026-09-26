"""Music session + volume services (port of MusicSessionService.cs and
MusicVolumeService.cs).

Linux mapping (documented divergence from Windows):
- SMTC (GlobalSystemMediaTransportControlsSessionManager) → MPRIS2 over the
  session D-Bus: sessions are bus names "org.mpris.MediaPlayer2.*", events are
  NameOwnerChanged + org.freedesktop.DBus.Properties.PropertiesChanged + Seeked.
- "System current session" does not exist in MPRIS; the service tracks the
  most recently signalled player as current (ties → first listed).
- Session volume (per-app) uses PulseAudio sink-inputs via pactl, matched by
  application.name / process binary against the player identity.
- Position is extrapolated from the last reported Position/Rate using the
  monotonic clock (players only emit Seeked on discontinuities).

All D-Bus access goes through the injected connection so tests can pass a
fake. Callbacks fire on the GLib main loop thread (D-Bus signal dispatch) —
plain callables, no GTK dependency.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from gi.repository import Gio, GLib


# MusicSessionService.cs enums (string values persist nowhere; identity only)
class MusicPlaybackState:
    UNKNOWN = "Unknown"
    STOPPED = "Stopped"
    PLAYING = "Playing"
    PAUSED = "Paused"


class MusicPlaybackMode:
    NORMAL = "Normal"
    SHUFFLE = "Shuffle"
    REPEAT = "Repeat"


MPRIS_PREFIX = "org.mpris.MediaPlayer2."
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"

_MICROSECONDS = 1_000_000


@dataclass
class MusicSessionInfo:
    sessionId: str
    sourceAppUserModelId: str
    sourceDisplayName: str
    title: str
    artist: str
    album: str
    playbackState: str
    position: float  # seconds
    duration: float  # seconds
    canPlay: bool
    canPause: bool
    canGoPrevious: bool
    canGoNext: bool
    canSeek: bool
    canChangeShuffle: bool
    canChangeRepeat: bool
    playbackMode: str
    artUrl: str = ""


@dataclass
class MusicSessionOption:
    sessionId: str
    sourceAppUserModelId: str
    sourceDisplayName: str
    playbackState: str
    isSystemCurrent: bool


@dataclass
class MusicTimelineSnapshot:
    position: float
    duration: float


@dataclass
class MusicPlaybackSnapshot:
    playbackState: str
    canPlay: bool
    canPause: bool
    canGoPrevious: bool
    canGoNext: bool
    canSeek: bool
    canChangeShuffle: bool
    canChangeRepeat: bool
    playbackMode: str


def get_source_display_name(source_id: str, identity: str = "") -> str:
    """Port of MusicSessionService.GetSourceDisplayName for MPRIS names."""
    normalized = (source_id or "").strip()
    if not normalized:
        return (identity or "").strip()
    lower = normalized.lower()
    checks = (
        ("qqmusic", "QQ音乐"),
        ("cloudmusic", "网易云音乐"),
        ("netease", "网易云音乐"),
        ("feelingsofclosing", "网易云音乐"),
        ("spotify", "Spotify"),
        ("youtube", "YouTube"),
        ("vlc", "VLC"),
        ("rhythmbox", "Rhythmbox"),
        ("audacious", "Audacious"),
        ("elisa", "Elisa"),
        ("lollypop", "Lollypop"),
        ("amberol", "Amberol"),
        ("msedge", "Microsoft Edge"),
        ("chromium", "Chromium"),
        ("chrome", "Google Chrome"),
        ("firefox", "Mozilla Firefox"),
        ("mpv", "mpv"),
        ("mpris-proxy", "Bluetooth"),
        ("celluloid", "Celluloid"),
        ("totem", "Videos"),
    )
    for needle, name in checks:
        if needle in lower:
            return name
    # org.mpris.MediaPlayer2.firefox.instance123 → Firefox-ish tail segment
    tail = normalized.rsplit(".", 1)[-1] if "." in normalized else normalized
    tail = re.sub(r"^\d+$", "", tail) or tail
    if identity and identity.strip():
        return identity.strip()
    return tail or normalized


def disambiguate_source_display_names(display_names: List[str]) -> List[str]:
    """Port of DisambiguateSourceDisplayNames: append " (n)" to duplicates."""
    totals: Dict[str, int] = {}
    for name in display_names:
        totals[name.lower()] = totals.get(name.lower(), 0) + 1
    ordinals: Dict[str, int] = {}
    result: List[str] = []
    for name in display_names:
        if totals[name.lower()] <= 1:
            result.append(name)
            continue
        key = name.lower()
        ordinal = ordinals.get(key, 0) + 1
        ordinals[key] = ordinal
        result.append(f"{name} ({ordinal})")
    return result


def map_playback_state(status: Optional[str]) -> str:
    return {
        "Playing": MusicPlaybackState.PLAYING,
        "Paused": MusicPlaybackState.PAUSED,
        "Stopped": MusicPlaybackState.STOPPED,
    }.get(status or "", MusicPlaybackState.UNKNOWN)


def map_playback_mode(shuffle: bool, loop_status: Optional[str]) -> str:
    if shuffle:
        return MusicPlaybackMode.SHUFFLE
    return MusicPlaybackMode.REPEAT if loop_status in ("Track", "Playlist") else MusicPlaybackMode.NORMAL


# GLib.Variant constructors (plain wrappers so tests can monkeypatch nothing).


def _variant_s(value: str):
    return GLib.Variant("(s)", (value,))


def _variant_ss(first: str, second: str):
    return GLib.Variant("(ss)", (first, second))


def _variant_ssv(iface: str, name: str, variant):
    return GLib.Variant("(ssv)", (iface, name, variant))


def _variant_string(value: str):
    return GLib.Variant("s", value)


def _variant_boolean(value: bool):
    return GLib.Variant("b", value)


@dataclass
class _PlayerCache:
    properties: Dict[str, object] = field(default_factory=dict)
    identity: str = ""
    # Position extrapolation inputs (monotonic seconds).
    position_seconds: float = 0.0
    position_stamp: float = 0.0
    rate: float = 1.0
    activity: int = 0


class MusicSessionService:
    """MPRIS2 media session aggregator with the C# service's surface."""

    def __init__(self, connection=None):
        self._connection = connection
        self._initialized = False
        self._disposed = False
        self._players: Dict[str, _PlayerCache] = {}
        self._activity_counter = 0
        self._subscription_ids: List[int] = []
        # Callbacks (the surface marshals to the UI thread as needed — D-Bus
        # signals already arrive on the GLib main loop).
        self.on_sessions_changed: Optional[Callable[[], None]] = None
        self.on_current_session_changed: Optional[Callable[[], None]] = None
        self.on_playback_info_changed: Optional[Callable[[], None]] = None
        self.on_media_properties_changed: Optional[Callable[[], None]] = None
        self.on_timeline_properties_changed: Optional[Callable[[], None]] = None

    # ---- lifecycle -----------------------------------------------------------

    def initialize(self) -> None:
        if self._disposed or self._initialized:
            return
        connection = self._connection
        if connection is None:
            connection = self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._initialized = True
        self._subscription_ids.append(
            connection.signal_subscribe(
                None,
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_name_owner_changed,
            )
        )
        self._subscription_ids.append(
            connection.signal_subscribe(
                None,
                PROPS_IFACE,
                "PropertiesChanged",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_properties_changed,
            )
        )
        self._subscription_ids.append(
            connection.signal_subscribe(
                None,
                PLAYER_IFACE,
                "Seeked",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_seeked,
            )
        )
        self._refresh_players()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._connection is not None:
            for subscription_id in self._subscription_ids:
                try:
                    self._connection.signal_unsubscribe(subscription_id)
                except Exception:
                    pass
        self._subscription_ids.clear()
        self._players.clear()

    # ---- D-Bus plumbing --------------------------------------------------------

    def _list_player_names(self) -> List[str]:
        try:
            result = self._connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ListNames",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            names = result.unpack()[0]
        except Exception:
            return []
        return sorted(name for name in names if name.startswith(MPRIS_PREFIX))

    def _call_player(self, bus_name: str, method: str, parameters=None):
        return self._connection.call_sync(
            bus_name,
            MPRIS_OBJECT_PATH,
            PLAYER_IFACE,
            method,
            parameters,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

    def _get_all_properties(self, bus_name: str) -> Dict[str, object]:
        result = self._connection.call_sync(
            bus_name,
            MPRIS_OBJECT_PATH,
            PROPS_IFACE,
            "GetAll",
            _variant_s(PLAYER_IFACE),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return dict(result.unpack()[0]) if result else {}

    def _get_root_identity(self, bus_name: str) -> str:
        try:
            result = self._connection.call_sync(
                bus_name,
                MPRIS_OBJECT_PATH,
                PROPS_IFACE,
                "Get",
                _variant_ss(ROOT_IFACE, "Identity"),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            return str(result.unpack()[0])
        except Exception:
            return ""

    def _set_player_property(self, bus_name: str, name: str, variant) -> bool:
        try:
            self._connection.call_sync(
                bus_name,
                MPRIS_OBJECT_PATH,
                PROPS_IFACE,
                "Set",
                _variant_ssv(PLAYER_IFACE, name, variant),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            return True
        except Exception:
            return False

    def _refresh_players(self) -> None:
        names = self._list_player_names()
        self._players = {name: self._players.get(name, _PlayerCache()) for name in names}
        for name in list(self._players):
            if name not in names:
                del self._players[name]
        for name, cache in self._players.items():
            if not cache.properties:
                self._reload_player(name, cache)

    def _reload_player(self, bus_name: str, cache: _PlayerCache) -> None:
        properties = self._get_all_properties(bus_name)
        if not properties:
            return
        cache.properties = properties
        if not cache.identity:
            cache.identity = self._get_root_identity(bus_name)
        self._stamp_position(cache, properties)

    def _stamp_position(self, cache: _PlayerCache, properties: Dict[str, object]) -> None:
        position = properties.get("Position")
        cache.position_seconds = (position / _MICROSECONDS) if isinstance(position, int) else cache.position_seconds
        cache.position_stamp = time.monotonic()
        rate = properties.get("Rate")
        cache.rate = float(rate) if isinstance(rate, (int, float)) else 1.0

    # ---- signal handlers ---------------------------------------------------------

    def _on_name_owner_changed(self, *_args) -> None:
        if self._disposed:
            return
        self._refresh_players()
        self._emit(self.on_sessions_changed)
        self._emit(self.on_current_session_changed)

    def _on_properties_changed(self, _connection, sender, _path, _iface, _signal, parameters) -> None:
        if self._disposed or not sender or not sender.startswith(MPRIS_PREFIX):
            return
        cache = self._players.get(sender)
        if cache is None:
            return
        try:
            _iface_name, changed, _invalidated = parameters.unpack()
        except Exception:
            return
        if not isinstance(changed, dict):
            return
        cache.properties.update(changed)
        self._activity_counter += 1
        cache.activity = self._activity_counter
        if "Position" in changed:
            self._stamp_position(cache, cache.properties)
        if "Metadata" in changed:
            self._emit(self.on_media_properties_changed)
            self._emit(self.on_current_session_changed)
        if "PlaybackStatus" in changed:
            self._stamp_position(cache, cache.properties)
            self._emit(self.on_playback_info_changed)
            self._emit(self.on_current_session_changed)
        elif {
            "Shuffle",
            "LoopStatus",
            "CanPlay",
            "CanPause",
            "CanGoNext",
            "CanGoPrevious",
            "CanSeek",
            "Rate",
        } & set(changed):
            self._emit(self.on_playback_info_changed)

    def _on_seeked(self, _connection, sender, _path, _iface, _signal, parameters) -> None:
        if self._disposed or not sender or not sender.startswith(MPRIS_PREFIX):
            return
        cache = self._players.get(sender)
        if cache is None:
            return
        try:
            position_microseconds = parameters.unpack()[0]
        except Exception:
            return
        cache.position_seconds = position_microseconds / _MICROSECONDS
        cache.position_stamp = time.monotonic()
        self._emit(self.on_timeline_properties_changed)

    @staticmethod
    def _emit(callback: Optional[Callable[[], None]]) -> None:
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    # ---- session resolution --------------------------------------------------------

    def _system_current_name(self) -> Optional[str]:
        if not self._players:
            return None
        # Most recently signalled player wins; ties → first by name order.
        best_name = None
        best_activity = -1
        for name in sorted(self._players):
            activity = self._players[name].activity
            if activity > best_activity:
                best_activity = activity
                best_name = name
        return best_name

    def _resolve_name(self, preferred_session_id: Optional[str]) -> Optional[str]:
        if preferred_session_id:
            if preferred_session_id in self._players:
                return preferred_session_id
        return self._system_current_name()

    def _ensure_initialized(self) -> None:
        if not self._initialized and not self._disposed:
            self.initialize()

    # ---- public surface (port of MusicSessionService) ---------------------------------

    def get_session_options(self) -> List[MusicSessionOption]:
        self._ensure_initialized()
        if self._disposed:
            return []
        system_current = self._system_current_name()
        options: List[MusicSessionOption] = []
        for name, cache in sorted(self._players.items()):
            properties = cache.properties
            if not properties:
                continue
            options.append(
                MusicSessionOption(
                    sessionId=name,
                    sourceAppUserModelId=name,
                    sourceDisplayName=get_source_display_name(name, cache.identity),
                    playbackState=map_playback_state(properties.get("PlaybackStatus")),
                    isSystemCurrent=name == system_current,
                )
            )
        return options

    def _live_position(self, cache: _PlayerCache) -> float:
        state = map_playback_state(cache.properties.get("PlaybackStatus"))
        if state != MusicPlaybackState.PLAYING:
            return cache.position_seconds
        elapsed = time.monotonic() - cache.position_stamp
        return cache.position_seconds + max(0.0, elapsed) * cache.rate

    def _info_for(self, bus_name: str) -> Optional[MusicSessionInfo]:
        cache = self._players.get(bus_name)
        if cache is None:
            return None
        properties = cache.properties
        if not properties:
            self._reload_player(bus_name, cache)
            properties = cache.properties
            if not properties:
                return None

        metadata = properties.get("Metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        duration_microseconds = metadata.get("mpris:length")
        duration = duration_microseconds / _MICROSECONDS if isinstance(duration_microseconds, (int, float)) else 0.0
        can_control = bool(properties.get("CanControl", False))

        def text(key: str) -> str:
            value = metadata.get(key, "")
            if isinstance(value, list):
                value = ", ".join(str(part) for part in value)
            return str(value or "")

        return MusicSessionInfo(
            sessionId=bus_name,
            sourceAppUserModelId=bus_name,
            sourceDisplayName=get_source_display_name(bus_name, cache.identity),
            title=text("xesam:title"),
            artist=text("xesam:artist"),
            album=text("xesam:album"),
            playbackState=map_playback_state(properties.get("PlaybackStatus")),
            position=self._live_position(cache),
            duration=duration,
            canPlay=can_control and bool(properties.get("CanPlay", False)),
            canPause=can_control and bool(properties.get("CanPause", False)),
            canGoPrevious=can_control and bool(properties.get("CanGoPrevious", False)),
            canGoNext=can_control and bool(properties.get("CanGoNext", False)),
            canSeek=can_control and bool(properties.get("CanSeek", False)),
            canChangeShuffle=can_control and "Shuffle" in properties,
            canChangeRepeat=can_control and "LoopStatus" in properties,
            playbackMode=map_playback_mode(bool(properties.get("Shuffle", False)), properties.get("LoopStatus")),
            artUrl=text("mpris:artUrl"),
        )

    def get_current_session_info(self, preferred_session_id: Optional[str] = None) -> Optional[MusicSessionInfo]:
        self._ensure_initialized()
        if self._disposed:
            return None
        name = self._resolve_name(preferred_session_id)
        return self._info_for(name) if name else None

    def get_current_timeline(self, preferred_session_id: Optional[str] = None) -> Optional[MusicTimelineSnapshot]:
        self._ensure_initialized()
        if self._disposed:
            return None
        name = self._resolve_name(preferred_session_id)
        if name is None:
            return None
        info = self._info_for(name)
        if info is None:
            return None
        return MusicTimelineSnapshot(position=info.position, duration=info.duration)

    def get_current_playback(self, preferred_session_id: Optional[str] = None) -> Optional[MusicPlaybackSnapshot]:
        self._ensure_initialized()
        if self._disposed:
            return None
        name = self._resolve_name(preferred_session_id)
        cache = self._players.get(name) if name else None
        if cache is None or not cache.properties:
            return None
        properties = cache.properties
        can_control = bool(properties.get("CanControl", False))
        return MusicPlaybackSnapshot(
            playbackState=map_playback_state(properties.get("PlaybackStatus")),
            canPlay=can_control and bool(properties.get("CanPlay", False)),
            canPause=can_control and bool(properties.get("CanPause", False)),
            canGoPrevious=can_control and bool(properties.get("CanGoPrevious", False)),
            canGoNext=can_control and bool(properties.get("CanGoNext", False)),
            canSeek=can_control and bool(properties.get("CanSeek", False)),
            canChangeShuffle=can_control and "Shuffle" in properties,
            canChangeRepeat=can_control and "LoopStatus" in properties,
            playbackMode=map_playback_mode(bool(properties.get("Shuffle", False)), properties.get("LoopStatus")),
        )

    def try_set_preferred_session(self, session_id: str) -> bool:
        self._ensure_initialized()
        return not self._disposed and session_id in self._players

    def _dispatch(self, session_id: Optional[str], method: str) -> bool:
        self._ensure_initialized()
        name = self._resolve_name(session_id)
        if name is None:
            return False
        try:
            self._call_player(name, method)
            return True
        except Exception:
            return False

    def try_toggle_play_pause(self, session_id: Optional[str] = None) -> bool:
        self._ensure_initialized()
        name = self._resolve_name(session_id)
        cache = self._players.get(name) if name else None
        if cache is None:
            return False
        method = (
            "Pause"
            if map_playback_state(cache.properties.get("PlaybackStatus")) == MusicPlaybackState.PLAYING
            else "Play"
        )
        return self._dispatch(name, method)

    def try_play(self, session_id: Optional[str] = None) -> bool:
        return self._dispatch(session_id, "Play")

    def try_pause(self, session_id: Optional[str] = None) -> bool:
        return self._dispatch(session_id, "Pause")

    def try_previous(self, session_id: Optional[str] = None) -> bool:
        return self._dispatch(session_id, "Previous")

    def try_next(self, session_id: Optional[str] = None) -> bool:
        return self._dispatch(session_id, "Next")

    def try_seek(self, session_id: Optional[str], position_seconds: float) -> bool:
        self._ensure_initialized()
        name = self._resolve_name(session_id)
        if name is None:
            return False
        try:
            track_id = (self._players[name].properties.get("Metadata") or {}).get("mpris:trackid")
            parameters = GLib.Variant("(ox)", (track_id or MPRIS_OBJECT_PATH, int(position_seconds * _MICROSECONDS)))
            self._connection.call_sync(
                name,
                MPRIS_OBJECT_PATH,
                PLAYER_IFACE,
                "SetPosition",
                parameters,
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            return True
        except Exception:
            return False

    def try_change_playback_mode(self, session_id: Optional[str], playback_mode: str) -> bool:
        """Port of TryChangePlaybackModeAsync via Shuffle + LoopStatus sets."""
        self._ensure_initialized()
        name = self._resolve_name(session_id)
        cache = self._players.get(name) if name else None
        if cache is None:
            return False
        properties = cache.properties
        can_shuffle = "Shuffle" in properties
        can_repeat = "LoopStatus" in properties
        did_change = False

        if playback_mode == MusicPlaybackMode.SHUFFLE:
            if not can_shuffle:
                return False
            if can_repeat and properties.get("LoopStatus") != "None":
                did_change |= self._set_player_property(name, "LoopStatus", _variant_string("None"))
            did_change |= self._set_player_property(name, "Shuffle", _variant_boolean(True))
            return did_change

        if playback_mode == MusicPlaybackMode.REPEAT:
            if not can_repeat:
                return False
            if can_shuffle and properties.get("Shuffle"):
                did_change |= self._set_player_property(name, "Shuffle", _variant_boolean(False))
            did_change |= self._set_player_property(name, "LoopStatus", _variant_string("Playlist"))
            return did_change

        if can_shuffle and properties.get("Shuffle"):
            did_change |= self._set_player_property(name, "Shuffle", _variant_boolean(False))
        if can_repeat and properties.get("LoopStatus") != "None":
            did_change |= self._set_player_property(name, "LoopStatus", _variant_string("None"))
        return did_change


# GLib.Variant construction helpers (isolated for testability and clarity).


def _variant_s(value: str):
    from gi.repository import GLib

    return GLib.Variant("(s)", (value,))


def _variant_ss(iface: str, name: str):
    from gi.repository import GLib

    return GLib.Variant("(ss)", (iface, name))


def _variant_ssv(iface: str, name: str, variant):
    from gi.repository import GLib

    return GLib.Variant("(ssv)", (iface, name, variant))


def _variant_string(value: str):
    from gi.repository import GLib

    return GLib.Variant("s", value)


def _variant_boolean(value: bool):
    from gi.repository import GLib

    return GLib.Variant("b", value)


# ── volume (MusicVolumeService → pactl) ────────────────────────────────────────

DEFAULT_SINK = "@DEFAULT_SINK@"
_VOLUME_PERCENT = re.compile(r"/\s*(\d+)%")
_SINK_INPUT_HEADER = re.compile(r"^Sink Input #(\d+)")
_APPLICATION_NAME = re.compile(r'application.name\s*=\s*"([^"]*)"')
_PROCESS_BINARY = re.compile(r'application.process.binary\s*=\s*"([^"]*)"')


def parse_volume_percent(pactl_output: str) -> Optional[float]:
    match = _VOLUME_PERCENT.search(pactl_output or "")
    if match is None:
        return None
    return int(match.group(1)) / 100.0


def parse_sink_inputs(pactl_output: str) -> List[dict]:
    """Split `pactl list sink-inputs` into {index, name, binary, volume}."""
    inputs: List[dict] = []
    current: Optional[dict] = None
    for line in (pactl_output or "").splitlines():
        header = _SINK_INPUT_HEADER.match(line.strip())
        if header:
            if current:
                inputs.append(current)
            current = {"index": int(header.group(1)), "name": "", "binary": "", "volume": ""}
            continue
        if current is None:
            continue
        stripped = line.strip()
        name = _APPLICATION_NAME.search(stripped)
        if name:
            current["name"] = name.group(1)
            continue
        binary = _PROCESS_BINARY.search(stripped)
        if binary:
            current["binary"] = binary.group(1)
            continue
        if "Volume:" in stripped:
            current["volume"] += stripped
    if current:
        inputs.append(current)
    return inputs


def _matches_source(candidate: str, source_id: str, display_name: str) -> bool:
    candidate = (candidate or "").lower()
    if not candidate:
        return False
    identity = (source_id or "").lower().replace("org.mpris.mediaplayer2.", "")
    display = (display_name or "").lower()
    return (
        candidate in identity or identity in candidate or (display and (candidate in display or display in candidate))
    )


class MusicVolumeService:
    """System + per-app volume through pactl (blocking; call off the UI thread)."""

    def __init__(self, runner: Optional[Callable[[List[str]], str]] = None):
        self._run = runner or self._run_pactl

    @staticmethod
    def _run_pactl(arguments: List[str]) -> str:
        result = subprocess.run(["pactl", *arguments], capture_output=True, text=True, timeout=3)
        return result.stdout or ""

    def get_system_master_volume(self) -> float:
        try:
            value = parse_volume_percent(self._run(["get-sink-volume", DEFAULT_SINK]))
        except Exception:
            return 0.0
        return value if value is not None else 0.0

    def try_set_system_master_volume(self, volume: float) -> bool:
        try:
            self._run(["set-sink-volume", DEFAULT_SINK, f"{int(round(volume * 100))}%"])
            return True
        except Exception:
            return False

    def _find_sink_input(self, source_id: str, display_name: str) -> Optional[dict]:
        try:
            listing = parse_sink_inputs(self._run(["list", "sink-inputs"]))
        except Exception:
            return None
        for entry in listing:
            if _matches_source(entry["name"], source_id, display_name) or _matches_source(
                entry["binary"], source_id, display_name
            ):
                return entry
        return None

    def get_volume_snapshot(self, source_id: str, display_name: str) -> tuple:
        """Returns (system_volume, session_volume, has_session_volume)."""
        system_volume = self.get_system_master_volume()
        entry = self._find_sink_input(source_id, display_name)
        if entry is None:
            return (system_volume, 0.0, False)
        session_volume = parse_volume_percent(entry.get("volume", ""))
        if session_volume is None:
            return (system_volume, 0.0, False)
        return (system_volume, session_volume, True)

    def try_set_session_volume(self, source_id: str, display_name: str, volume: float) -> bool:
        entry = self._find_sink_input(source_id, display_name)
        if entry is None:
            return False
        try:
            self._run(["set-sink-input-volume", str(entry["index"]), f"{int(round(volume * 100))}%"])
            return True
        except Exception:
            return False
