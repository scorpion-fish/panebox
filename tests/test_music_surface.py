"""Music surface GUI tests under Xvfb: layouts, rendering and source picker
against a fake session service (no MPRIS player exists on the test rig).

Skips when display :99 (the test rig's Xvfb) is not reachable — mirrors
test_weather_surface.py. Env pinned at module import, before Gtk.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


if _display_reachable(SMOKE_DISPLAY) and not os.environ.get("PANEBOX_SKIP_GUI"):
    # Must be pinned before the first Gdk.Display is opened.
    os.environ["DISPLAY"] = SMOKE_DISPLAY
    os.environ.setdefault("GDK_BACKEND", "x11")
    os.environ.setdefault("GSK_RENDERER", "cairo")

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    _display_reachable(SMOKE_DISPLAY) is False or os.environ.get("PANEBOX_SKIP_GUI"),
    reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)",
)

from panebox import i18n  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from panebox.models.widget_config import WidgetConfig  # noqa: E402
from panebox.services.music_service import (  # noqa: E402
    MusicPlaybackMode,
    MusicSessionInfo,
    MusicSessionOption,
)
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.views.music_widget import MusicSurface  # noqa: E402


class FakeSessionService:
    """Headless stand-in for MusicSessionService with one mutable session."""

    def __init__(self):
        self.initialized = False
        self.session_id = "org.mpris.MediaPlayer2.vlc"
        self.state = "Playing"
        self.title = "Neon Skyline (Extended Mix)"
        self.artist = "测试歌手"
        self.shuffle = False
        self.loop_status = "None"
        self.commands = []
        self.on_sessions_changed = None
        self.on_current_session_changed = None
        self.on_playback_info_changed = None
        self.on_media_properties_changed = None
        self.on_timeline_properties_changed = None

    def initialize(self):
        self.initialized = True

    def get_session_options(self):
        return [
            MusicSessionOption(
                sessionId=self.session_id,
                sourceAppUserModelId=self.session_id,
                sourceDisplayName="VLC",
                playbackState=self.state,
                isSystemCurrent=True,
            )
        ]

    def _info(self) -> MusicSessionInfo:
        return MusicSessionInfo(
            sessionId=self.session_id,
            sourceAppUserModelId=self.session_id,
            sourceDisplayName="VLC",
            title=self.title,
            artist=self.artist,
            album="Album",
            playbackState=self.state,
            position=63.0,
            duration=200.0,
            canPlay=True,
            canPause=True,
            canGoPrevious=True,
            canGoNext=True,
            canSeek=True,
            canChangeShuffle=True,
            canChangeRepeat=True,
            playbackMode=MusicPlaybackMode.SHUFFLE if self.shuffle else MusicPlaybackMode.NORMAL,
        )

    def get_current_session_info(self, preferred_session_id=None):
        return self._info()

    def get_current_timeline(self, preferred_session_id=None):
        info = self._info()
        return type("Timeline", (), {"position": info.position, "duration": info.duration})()

    def try_toggle_play_pause(self, session_id=None):
        self.commands.append(("toggle", session_id))
        self.state = "Paused" if self.state == "Playing" else "Playing"
        return True

    def try_next(self, session_id=None):
        self.commands.append(("next", session_id))
        return True

    def try_previous(self, session_id=None):
        self.commands.append(("previous", session_id))
        return True

    def try_change_playback_mode(self, session_id=None, mode=None):
        self.commands.append(("mode", session_id, mode))
        self.shuffle = mode == MusicPlaybackMode.SHUFFLE
        return True

    def try_seek(self, session_id=None, position_seconds=0.0):
        self.commands.append(("seek", session_id, position_seconds))
        return True


class FakeVolumeService:
    def __init__(self):
        self.snapshots = []

    def get_volume_snapshot(self, source_id, display_name):
        self.snapshots.append((source_id, display_name))
        return (0.46, 0.65, True)

    def try_set_system_master_volume(self, volume):
        return True

    def try_set_session_volume(self, source_id, display_name, volume):
        return True


def _make_surface(config_root: Path, widget_id: str, settings_mutator=None):
    config_root.mkdir(exist_ok=True)
    settings = SettingsService(config_root=config_root)
    if settings_mutator:
        settings_mutator(settings)
    config = WidgetConfig(id=widget_id, name="Music", widgetKind="Music")
    settings.add_widget(config)
    session = FakeSessionService()
    volume = FakeVolumeService()
    surface = MusicSurface(config, settings, session_service=session, volume_service=volume)
    window = Gtk.Window(default_width=330, default_height=260)
    window.set_child(surface)
    window.present()
    return surface, settings, window, session


def _run_phases(phases, timeout_ms: int = 12000) -> None:
    failures: list = []
    loop = GLib.MainLoop()

    def guard(fn):
        def run():
            try:
                fn()
            except Exception as error:  # noqa: BLE001
                failures.append(error)
                loop.quit()
            return False

        return run

    offset = 0
    for delay, fn in phases:
        offset += delay
        GLib.timeout_add(offset, guard(fn))
    GLib.timeout_add(timeout_ms, loop.quit)
    loop.run()
    if failures:
        raise failures[0]


def test_surface_controls_layout_and_actions(tmp_path):
    i18n.set_language("zh-CN")
    try:
        surface, settings, window, session = _make_surface(tmp_path / "config", "music-1")

        def assert_controls():
            assert surface._layout_name == "Controls"
            assert surface._info is not None and surface._info.sessionId
            # Title / status / source / artist render across the layout set.
            assert surface._title_labels[0]._label.get_text() == "Neon Skyline (Extended Mix)"
            assert surface._status_labels[0].get_text() == "播放中"
            assert surface._source_buttons[0].get_label() == "VLC"
            # Playing → pause glyph; progress carries position/duration.
            assert surface._playpause_buttons[0].get_label() == "⏸"
            # Base position 63s + live extrapolation during the 1.2s phase.
            assert surface._position_labels[0].get_text() in ("01:03", "01:04", "01:05")
            assert surface._duration_labels[0].get_text() == "03:20"
            assert surface._progress_scales[0].get_sensitive() is True

        def click_next():
            surface._next_buttons[0].emit("clicked")

        def assert_next_and_cycle_mode():
            # Transport runs in the worker pool; by the next phase it landed.
            assert session.commands[-1][0] == "next"
            surface._mode_buttons[0].emit("clicked")

        def assert_mode():
            # Normal → Shuffle, both reflected in glyph and service call.
            assert session.commands[-1] == ("mode", None, "Shuffle")
            assert surface._mode_buttons[0].get_label() == "⇄"

        def go_minimal():
            surface._on_available_size_changed(160, 160)

        def assert_minimal():
            assert surface._layout_name == "Minimal"
            assert surface.stack.get_visible_child_name() == "minimal"
            # Shared state: the minimal marquee shows the same title.
            assert surface._title_labels[0]._label.get_text() == "Neon Skyline (Extended Mix)"

        def assert_settled():
            # Settle re-reads applied; play state unchanged since click_next
            # only advanced the track.
            assert surface._playpause_buttons[0].get_label() == "⏸"

        try:
            _run_phases(
                [
                    (1200, assert_controls),
                    (100, click_next),
                    (400, assert_next_and_cycle_mode),
                    (500, assert_mode),
                    (100, go_minimal),
                    (100, assert_minimal),
                    (600, assert_settled),
                ]
            )
        finally:
            # Teardown only after assertions: destroying the window drives the
            # allocation to 0×0 and would flip the layout first.
            surface.dispose()
            window.destroy()
    finally:
        i18n.set_language(None)


def test_surface_record_layout_and_seek(tmp_path):
    i18n.set_language("zh-CN")

    def force_record(settings):
        settings.settings.music.musicDisplayMode = "RecordVertical"

    try:
        surface, _settings, window, session = _make_surface(tmp_path / "config", "music-2", force_record)

        def assert_record():
            assert surface._layout_name == "RecordVertical"
            assert surface.stack.get_visible_child_name() == "recordvertical"
            # 330×260 → vinyl clamped by height; small-mode typography active.
            metrics = surface._vinyls[0].get_size_request()
            assert metrics[0] > 0 and metrics[0] <= 230
            assert surface._title_labels[-1]._label.get_text().startswith("Neon")

        def seek_click():
            # Click at 50% of the visible layout's scale → ~100s of a 200s track.
            scale = next(s for s in surface._progress_scales if s.get_width() > 1)
            gesture = next(
                controller for controller in scale.observe_controllers() if isinstance(controller, Gtk.GestureClick)
            )
            gesture.emit("released", 1, scale.get_width() / 2.0, 5.0)

        def assert_seek_landed():
            assert any(command[0] == "seek" and abs(command[2] - 100.0) < 1.0 for command in session.commands)

        try:
            _run_phases(
                [
                    (1200, assert_record),
                    (200, seek_click),
                    (400, assert_seek_landed),
                ]
            )
        finally:
            surface.dispose()
            window.destroy()
    finally:
        i18n.set_language(None)


def test_surface_preferred_source_persists(tmp_path):
    i18n.set_language("zh-CN")
    try:
        surface, settings, window, session = _make_surface(tmp_path / "config", "music-3")

        def assert_loaded():
            assert surface._info is not None

        def pick_source():
            surface._set_preferred_session(session.session_id)
            settings.flush_pending_save()
            import json

            layout = json.loads((tmp_path / "config" / "widget-layout.json").read_text("utf-8"))
            _widgets = layout.get("widgets") or layout
            raw = json.dumps(layout, ensure_ascii=False)
            assert session.session_id in raw
            assert surface.config.metadata.get("Music.PreferredSource") == session.session_id

        try:
            _run_phases(
                [
                    (1200, assert_loaded),
                    (100, pick_source),
                ]
            )
        finally:
            surface.dispose()
            window.destroy()
    finally:
        i18n.set_language(None)
