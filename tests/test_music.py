"""Music widget unit tests: MPRIS service (fake D-Bus connection),
pactl volume parsing, and the pure view model. No GTK/display needed."""

from __future__ import annotations

import pytest  # noqa: F401

from gi.repository import GLib

from panebox.services import music_service as ms
from panebox.services import music_view_model as vm
from panebox.services.music_service import (
    MusicPlaybackMode,
    MusicPlaybackState,
    MusicSessionService,
    MusicVolumeService,
    disambiguate_source_display_names,
    get_source_display_name,
    map_playback_mode,
    map_playback_state,
    parse_sink_inputs,
    parse_volume_percent,
)


# ── display-name mapping ────────────────────────────────────────────────────


def test_source_display_name_table():
    assert get_source_display_name("org.mpris.MediaPlayer2.qqmusic") == "QQ音乐"
    assert get_source_display_name("org.mpris.MediaPlayer2.netease.cloudmusic") == "网易云音乐"
    assert get_source_display_name("org.mpris.MediaPlayer2.firefox.instance812") == "Mozilla Firefox"
    assert get_source_display_name("org.mpris.MediaPlayer2.chrome") == "Google Chrome"
    assert get_source_display_name("org.mpris.MediaPlayer2.spotify") == "Spotify"
    assert get_source_display_name("org.mpris.MediaPlayer2.mpris-proxy") == "Bluetooth"


def test_source_display_name_fallback_tail_and_identity():
    # Unknown player without identity → last dotted segment (digits stripped).
    assert get_source_display_name("org.mpris.MediaPlayer2.someplayer.instance2") == "instance2"
    # Identity wins over the raw tail.
    assert get_source_display_name("org.mpris.MediaPlayer2.someplayer", "Quod Libet") == "Quod Libet"
    assert get_source_display_name("", "X") == "X"


def test_disambiguate_display_names():
    assert disambiguate_source_display_names(["A", "B"]) == ["A", "B"]
    assert disambiguate_source_display_names(["VLC", "vlc", "VLC"]) == [
        "VLC (1)",
        "vlc (2)",
        "VLC (3)",
    ]


def test_playback_state_and_mode_mapping():
    assert map_playback_state("Playing") == MusicPlaybackState.PLAYING
    assert map_playback_state("Paused") == MusicPlaybackState.PAUSED
    assert map_playback_state(None) == MusicPlaybackState.UNKNOWN
    assert map_playback_mode(True, "None") == MusicPlaybackMode.SHUFFLE
    assert map_playback_mode(False, "Playlist") == MusicPlaybackMode.REPEAT
    assert map_playback_mode(False, "Track") == MusicPlaybackMode.REPEAT
    assert map_playback_mode(False, "None") == MusicPlaybackMode.NORMAL


# ── fake D-Bus connection ───────────────────────────────────────────────────


def _v(value, kind):
    return GLib.Variant("v", GLib.Variant(kind, value))


def _player_properties(
    title="Song",
    artist="Artist",
    album="Album",
    status="Playing",
    length_us=200_000_000,
    position_us=60_000_000,
    rate=1.0,
    shuffle=False,
    loop="None",
    can_control=True,
    art="",
):
    return {
        "PlaybackStatus": _v(status, "s"),
        "Rate": _v(rate, "d"),
        "Metadata": _v(
            {
                "mpris:trackid": _v("/dev/panebox/track1", "o"),
                "mpris:length": _v(length_us, "x"),
                "mpris:artUrl": _v(art, "s"),
                "xesam:title": _v(title, "s"),
                "xesam:artist": _v(artist if isinstance(artist, list) else [artist], "as"),
                "xesam:album": _v(album, "s"),
            },
            "a{sv}",
        ),
        "CanControl": _v(can_control, "b"),
        "CanPlay": _v(True, "b"),
        "CanPause": _v(True, "b"),
        "CanGoNext": _v(True, "b"),
        "CanGoPrevious": _v(True, "b"),
        "CanSeek": _v(True, "b"),
        "Shuffle": _v(shuffle, "b"),
        "LoopStatus": _v(loop, "s"),
        "Position": _v(position_us, "x"),
    }


class FakeConnection:
    """Implements the call_sync/signal_subscribe surface MusicSessionService
    uses. Players register props; tests emit signals by calling handlers."""

    def __init__(self, players=None):
        self.players = players or {}  # bus name → props dict (plain values)
        self.identities = {}
        self.calls = []  # (bus, iface, method, params)
        self.subscriptions = []

    def call_sync(self, bus_name, _path, iface, method, params, *_args, **_kwargs):
        self.calls.append((bus_name, iface, method, params))
        if bus_name == "org.freedesktop.DBus" and method == "ListNames":
            names = list(self.players) + ["org.freedesktop.DBus"]
            return GLib.Variant("(as)", (names,))
        if method == "GetAll":
            props = self.players.get(bus_name) or {}
            return GLib.Variant("(a{sv})", (props,))
        if method == "Get" and params.unpack() == ("org.mpris.MediaPlayer2", "Identity"):
            return GLib.Variant("(v)", (_v(self.identities.get(bus_name, ""), "s"),))
        if method in ("Play", "Pause", "Next", "Previous", "Stop"):
            self.players.setdefault(bus_name, {})
            return GLib.Variant("()", ())
        if method == "SetPosition":
            return GLib.Variant("()", ())
        if method == "Set":
            return GLib.Variant("()", ())
        raise AssertionError(f"unexpected call {bus_name} {iface}.{method}")

    def signal_subscribe(self, _sender, iface, member, *_rest):
        handler = _rest[-1]
        self.subscriptions.append((iface, member, handler))
        return len(self.subscriptions)

    def signal_unsubscribe(self, _subscription_id):
        pass

    # Test helpers — emit as the real bus would.
    def emit_properties_changed(self, bus_name, changed_props):
        for iface, member, handler in self.subscriptions:
            if iface == ms.PROPS_IFACE and member == "PropertiesChanged":
                handler(
                    None,
                    bus_name,
                    ms.MPRIS_OBJECT_PATH,
                    iface,
                    member,
                    GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", changed_props, [])),
                )

    def emit_seeked(self, bus_name, position_us):
        for iface, member, handler in self.subscriptions:
            if iface == ms.PLAYER_IFACE and member == "Seeked":
                handler(
                    None,
                    bus_name,
                    ms.MPRIS_OBJECT_PATH,
                    iface,
                    member,
                    GLib.Variant("(x)", (position_us,)),
                )


def _make_service(players=None, identities=None):
    connection = FakeConnection(players or {})
    connection.identities = identities or {}
    service = MusicSessionService(connection=connection)
    service.initialize()
    return service, connection


def test_service_lists_sessions_and_maps_metadata():
    service, _conn = _make_service(
        {
            "org.mpris.MediaPlayer2.vlc": _player_properties(title="Track A", status="Paused"),
            "org.mpris.MediaPlayer2.qqmusic": _player_properties(title="(trackid)", artist=[]),
        }
    )
    options = service.get_session_options()
    assert [option.sourceDisplayName for option in options] == ["QQ音乐", "VLC"]
    # No signals yet: activity all zero → first by name order is current.
    assert options[0].isSystemCurrent is True
    assert options[1].playbackState == MusicPlaybackState.PAUSED

    info = service.get_current_session_info()
    assert info.sessionId == "org.mpris.MediaPlayer2.qqmusic"
    assert info.title == "(trackid)"
    assert info.artist == ""  # empty artist list → ""
    assert info.duration == 200.0  # µs → seconds
    assert 59.0 <= info.position <= 61.0
    assert info.playbackState == MusicPlaybackState.PLAYING
    assert info.canSeek is True
    assert info.playbackMode == MusicPlaybackMode.NORMAL


def test_service_most_recent_activity_wins():
    service, conn = _make_service(
        {
            "org.mpris.MediaPlayer2.aaa": _player_properties(title="A"),
            "org.mpris.MediaPlayer2.bbb": _player_properties(title="B"),
        }
    )
    conn.emit_properties_changed("org.mpris.MediaPlayer2.bbb", {"PlaybackStatus": _v("Paused", "s")})
    info = service.get_current_session_info()
    assert info.sessionId == "org.mpris.MediaPlayer2.bbb"
    # Preferred session outranks recency.
    info = service.get_current_session_info("org.mpris.MediaPlayer2.aaa")
    assert info.sessionId == "org.mpris.MediaPlayer2.aaa"
    # Vanished preferred falls back to the system-current heuristic.
    info = service.get_current_session_info("org.mpris.MediaPlayer2.gone")
    assert info.sessionId == "org.mpris.MediaPlayer2.bbb"


def test_service_timeline_playback_and_seek():
    service, conn = _make_service(
        {
            "org.mpris.MediaPlayer2.vlc": _player_properties(status="Playing", position_us=10_000_000),
        }
    )
    snapshot = service.get_current_timeline()
    assert 9.0 <= snapshot.position <= 11.0
    assert snapshot.duration == 200.0

    playback = service.get_current_playback()
    assert playback.canPause is True and playback.canSeek is True

    assert service.try_toggle_play_pause() is True
    assert conn.calls[-1][2] == "Pause"  # status is Playing → toggle dispatches Pause

    assert service.try_previous() is True
    assert conn.calls[-1][2] == "Previous"
    assert service.try_next() is True
    assert conn.calls[-1][2] == "Next"

    assert service.try_seek(None, 42.5) is True
    bus, _iface, method, params = conn.calls[-1]
    assert method == "SetPosition"
    track_id, offset = params.unpack()
    assert track_id == "/dev/panebox/track1"
    assert offset == 42_500_000


def test_service_position_extrapolates_with_rate():
    service, conn = _make_service(
        {
            "org.mpris.MediaPlayer2.vlc": _player_properties(status="Playing", position_us=60_000_000, rate=2.0),
        }
    )
    cache = service._players["org.mpris.MediaPlayer2.vlc"]
    cache.position_stamp -= 5.0  # simulate 5s since last stamp
    info = service.get_current_session_info()
    assert 69.5 <= info.position <= 71.5  # 60s + 5s × rate 2

    # Paused players freeze at the last reported position.
    conn.emit_properties_changed("org.mpris.MediaPlayer2.vlc", {"PlaybackStatus": _v("Paused", "s")})
    cache.position_stamp -= 10.0
    info = service.get_current_session_info()
    assert info.position < 62.0

    # Seeked signal jumps the position and re-stamps.
    conn.emit_seeked("org.mpris.MediaPlayer2.vlc", 150_000_000)
    timeline = service.get_current_timeline()
    assert 149.0 <= timeline.position <= 151.5


def test_service_events_fire_callbacks():
    service, conn = _make_service({"org.mpris.MediaPlayer2.vlc": _player_properties()})
    events = []
    service.on_playback_info_changed = lambda: events.append("playback")
    service.on_media_properties_changed = lambda: events.append("media")
    service.on_timeline_properties_changed = lambda: events.append("timeline")
    conn.emit_properties_changed("org.mpris.MediaPlayer2.vlc", {"PlaybackStatus": _v("Paused", "s")})
    assert events == ["playback"]
    conn.emit_properties_changed("org.mpris.MediaPlayer2.vlc", {"Metadata": _player_properties()["Metadata"]})
    assert events[-1] == "media"
    conn.emit_seeked("org.mpris.MediaPlayer2.vlc", 1_000_000)
    assert events[-1] == "timeline"


def test_service_toggle_pauses_when_playing():
    service, _conn = _make_service(
        {
            "org.mpris.MediaPlayer2.vlc": _player_properties(status="Playing"),
        }
    )
    assert service.try_toggle_play_pause() is True
    assert _conn.calls[-1][2] == "Pause"


def test_try_change_playback_mode_semantics():
    props = _player_properties(status="Paused", loop="Track")  # repeat active
    service, conn = _make_service({"org.mpris.MediaPlayer2.vlc": props})
    name = "org.mpris.MediaPlayer2.vlc"

    # Shuffle: clears repeat, sets Shuffle=true.
    assert service.try_change_playback_mode(None, MusicPlaybackMode.SHUFFLE) is True
    sets = [(c[2], c[3].unpack()) for c in conn.calls if c[2] == "Set"]
    assert ("Set", ("org.mpris.MediaPlayer2.Player", "LoopStatus", "None")) in sets
    assert ("Set", ("org.mpris.MediaPlayer2.Player", "Shuffle", True)) in sets

    conn.calls.clear()
    # Repeat: clears shuffle, sets LoopStatus=Playlist.
    service._players[name].properties["Shuffle"] = _v(True, "b")
    assert service.try_change_playback_mode(None, MusicPlaybackMode.REPEAT) is True
    sets = [(c[2], c[3].unpack()) for c in conn.calls if c[2] == "Set"]
    assert ("Set", ("org.mpris.MediaPlayer2.Player", "Shuffle", False)) in sets
    assert ("Set", ("org.mpris.MediaPlayer2.Player", "LoopStatus", "Playlist")) in sets

    conn.calls.clear()
    # Normal: both off.
    service._players[name].properties["Shuffle"] = _v(True, "b")
    service._players[name].properties["LoopStatus"] = _v("Track", "s")
    assert service.try_change_playback_mode(None, MusicPlaybackMode.NORMAL) is True
    sets = [(c[2], c[3].unpack()) for c in conn.calls if c[2] == "Set"]
    assert ("Set", ("org.mpris.MediaPlayer2.Player", "Shuffle", False)) in sets
    assert ("Set", ("org.mpris.MediaPlayer2.Player", "LoopStatus", "None")) in sets


def test_service_dispose_blocks_everything():
    service, _conn = _make_service({"org.mpris.MediaPlayer2.vlc": _player_properties()})
    service.dispose()
    assert service.get_session_options() == []
    assert service.get_current_session_info() is None
    assert service.try_next() is False


# ── pactl volume ────────────────────────────────────────────────────────────


PACTL_SINK_VOLUME = """Sink #48
    State: RUNNING
    Volume: front-left: 30148 /  46% / -20.23 dB,   front-right: 30148 /  46% / -20.23 dB
"""


PACTL_SINK_INPUTS = """Sink Input #112
	Driver: protocol-pulse.c
	Application name: "VLC media player"
		application.name = "VLC media player"
		application.process.binary = "vlc"
		module-stream-restore.id = "sink-input-by-media-role:music"
	Volume: front-left: 42367 /  65% / -11.05 dB,   front-right: 42367 /  65% / -11.05 dB
Sink Input #140
	Driver: protocol-pulse.c
	Application name: "Firefox"
		application.name = "Firefox"
		application.process.binary = "firefox"
	Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
"""


def test_parse_volume_percent_real_output():
    assert parse_volume_percent(PACTL_SINK_VOLUME) == 0.46
    assert parse_volume_percent("") is None
    assert parse_volume_percent("no percent here") is None


def test_parse_sink_inputs_extracts_identity_and_volume():
    inputs = parse_sink_inputs(PACTL_SINK_INPUTS)
    assert [entry["index"] for entry in inputs] == [112, 140]
    assert inputs[0]["name"] == "VLC media player"
    assert inputs[0]["binary"] == "vlc"
    assert parse_volume_percent(inputs[0]["volume"]) == 0.65
    assert parse_volume_percent(inputs[1]["volume"]) == 1.0


def test_volume_service_snapshot_and_set():
    outputs = {
        ("get-sink-volume", "@DEFAULT_SINK@"): PACTL_SINK_VOLUME,
        ("list", "sink-inputs"): PACTL_SINK_INPUTS,
    }
    commands = []

    def runner(arguments):
        commands.append(tuple(arguments))
        return outputs.get(tuple(arguments), "")

    service = MusicVolumeService(runner=runner)
    system, session, has_session = service.get_volume_snapshot("org.mpris.MediaPlayer2.vlc", "VLC")
    assert system == 0.46
    assert session == 0.65
    assert has_session is True

    assert service.try_set_session_volume("org.mpris.MediaPlayer2.vlc", "VLC", 0.8) is True
    assert commands[-1] == ("set-sink-input-volume", "112", "80%")

    assert service.try_set_system_master_volume(0.25) is True
    assert commands[-1] == ("set-sink-volume", "@DEFAULT_SINK@", "25%")


def test_volume_service_without_matching_stream():
    def runner(arguments):
        return PACTL_SINK_VOLUME if tuple(arguments) == ("get-sink-volume", "@DEFAULT_SINK@") else ""

    service = MusicVolumeService(runner=runner)
    system, session, has_session = service.get_volume_snapshot("org.mpris.MediaPlayer2.nope", "Nothing")
    assert system == 0.46
    assert has_session is False
    assert service.try_set_session_volume("org.mpris.MediaPlayer2.nope", "Nothing", 0.5) is False


# ── view model ──────────────────────────────────────────────────────────────


def test_layout_resolution_modes():
    # Cover forces minimal regardless of size; Controls forbids it.
    assert vm.resolve_layout(400, 400, "Cover") == "Minimal"
    assert vm.resolve_layout(150, 150, "Controls") == "Controls"
    # Record layouts ignore size entirely.
    assert vm.resolve_layout(150, 150, "RecordVertical") == "RecordVertical"
    assert vm.resolve_layout(500, 500, "RecordHorizontal") == "RecordHorizontal"
    # Auto: below 180×180 → minimal.
    assert vm.resolve_layout(320, 240, "Auto") == "Controls"
    assert vm.resolve_layout(179, 240, "Auto") == "Minimal"
    assert vm.resolve_layout(320, 179, "Auto") == "Minimal"
    assert vm.resolve_layout(200, 200, "Garbage") == "Controls"  # normalized to Auto


def test_transport_and_panel_sizes():
    assert vm.resolve_transport_button_size(180) == 28
    assert vm.resolve_transport_button_size(320) == 32
    assert vm.resolve_transport_button_size(250) == 30
    assert vm.resolve_transport_button_size(100) == 28  # clamped
    assert vm.resolve_inline_volume_panel_width(320) == 238
    assert vm.resolve_inline_volume_panel_width(100) == 88


def test_content_grid_metrics_lerp():
    metrics = vm.resolve_content_grid_metrics(180, 180)
    assert metrics == {
        "albumSize": 60,
        "transportButtonSize": 28,
        "contentPadding": 8,
        "columnSpacing": 8,
        "rowSpacing": 4,
    }
    metrics = vm.resolve_content_grid_metrics(320, 240)
    assert metrics == {
        "albumSize": 82,
        "transportButtonSize": 32,
        "contentPadding": 12,
        "columnSpacing": 12,
        "rowSpacing": 8,
    }


def test_record_metrics_clamps_and_small_swap():
    # Tight grid: clamped to 64 minimum, small typography.
    tiny = vm.resolve_record_metrics(120, 120)
    assert tiny["vinylSize"] == 64
    assert tiny["small"] is True
    assert tiny["modeButtonVisible"] is False  # width < 190
    assert tiny["tonearmVisible"] is False
    # Wide grid: 230 maximum, regular typography, mode button visible.
    wide = vm.resolve_record_metrics(400, 700)
    assert wide["vinylSize"] == 230
    assert wide["small"] is False
    assert wide["modeButtonVisible"] is True
    assert wide["titleFontSize"] == 14
    # Boundary: small at vinyl < 110.
    edge = vm.resolve_record_metrics(150, 210)
    assert edge["vinylSize"] < 110 and edge["small"] is True


def test_format_time_and_volume():
    assert vm.format_time(0) == "00:00"
    assert vm.format_time(65.4) == "01:05"
    assert vm.format_time(3599) == "59:59"
    assert vm.format_time(3600) == "1:00:00"
    assert vm.format_time(None) == "00:00"
    assert vm.format_time(-5) == "00:00"
    assert vm.normalize_volume(0.46789) == 0.468
    assert vm.normalize_volume(2.0) == 1.0
    assert vm.normalize_volume(None) == 0.0
    assert vm.format_percent(0.46789) == "47%"
    assert vm.format_percent(None) == "0%"


def test_playback_mode_cycle_and_glyphs():
    assert vm.get_next_playback_mode("Normal") == "Shuffle"
    assert vm.get_next_playback_mode("Shuffle") == "Repeat"
    assert vm.get_next_playback_mode("Repeat") == "Normal"
    assert vm.get_playback_mode_glyph("Shuffle") == "⇄"
    assert vm.get_playback_mode_glyph("Repeat") == "⟳"
    assert vm.get_playback_mode_glyph("Normal") == "→"


def test_status_text_keys():
    assert vm.status_text_key("Playing", True) == "Music.Status.Playing"
    assert vm.status_text_key("Paused", True) == "Music.Status.Paused"
    assert vm.status_text_key("Stopped", True) == "Music.Status.Stopped"
    assert vm.status_text_key("Unknown", True) == "Music.Status.Ready"
    assert vm.status_text_key("Playing", False) == "Music.Status.NoSession"


class _Option:
    def __init__(self, session_id, name, state, is_current=False):
        self.sessionId = session_id
        self.sourceAppUserModelId = session_id
        self.sourceDisplayName = name
        self.playbackState = state
        self.isSystemCurrent = is_current


def test_session_list_ordering_and_disambiguation():
    options = [
        _Option("a", "Zeta", "Paused"),
        _Option("b", "Alpha", "Paused", is_current=True),
        _Option("c", "Alpha", "Playing"),
        _Option("d", "Alpha", "Paused"),
    ]
    ordered, kept = vm.resolve_session_list(options, "a")
    assert [option.sessionId for option in ordered] == ["a", "b", "c", "d"]
    assert kept == "a"
    # Duplicate names disambiguated (preferred first stays unsuffixed only if
    # unique — here Alpha repeats, so ordinals apply).
    names = [option.sourceDisplayName for option in ordered]
    assert names[1] == "Alpha (1)" and names[2] == "Alpha (2)" and names[3] == "Alpha (3)"

    # Vanished preferred: kept is None, ordering falls back to system-current.
    ordered, kept = vm.resolve_session_list(options[1:], "missing")
    assert kept is None
    assert ordered[0].sessionId == "b"


def test_cover_signature_and_dominant_color():
    assert vm.cover_signature("s1", "T", "A", "L") == "s1\x1eT\x1eA\x1eL"
    # 2×2 solid red RGBA image → dominant (255,0,0).
    pixels = bytes([255, 0, 0, 255]) * 4
    assert vm.compute_dominant_color(pixels, 2, 2) == (255, 0, 0)
    # Fully transparent → None.
    assert vm.compute_dominant_color(bytes([0, 0, 0, 0]) * 4, 2, 2) is None
    # Alpha 24+ counts; weight favors saturated pixels over gray.
    mixed = bytes([255, 0, 0, 255] + [128, 128, 128, 255])
    red, green, blue = vm.compute_dominant_color(mixed, 2, 1)
    assert red > green == blue


def test_artwork_backdrop_stops():
    stops = vm.artwork_backdrop_stops((255, 0, 0))
    assert stops[0][0] == 0.0 and stops[-1][0] == 1.0
    assert stops[0][1].startswith("rgba(255,0,0,0.35")  # 0x5A/255
    assert stops[-1][1].endswith("0.000)")
    assert vm.artwork_backdrop_stops(None)[0][1].startswith("rgba(64,64,64,")
