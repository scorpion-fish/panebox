"""Music widget surface (port of MusicWidgetContent.xaml(.cs) + the stateful
half of the MusicWidgetViewModel partials).

Four layouts — Minimal / Controls / RecordVertical / RecordHorizontal — driven
by the musicDisplayMode setting with Auto falling back to widget size. MPRIS
sessions via services.music_service (SMTC → D-Bus mapping documented there);
volume via pactl. Marquee title, rotating vinyl + tonearm, artwork backdrop
with dominant-color ambience, source picker, seekable progress.

GTK divergences from WinUI (documented in README): Segoe MDL2 glyph icons →
Unicode glyphs; InlineVolumePanel flyout → popover per layout; composition
animations → draw-func timers + CSS. vfunc overrides are dead in this
PyGObject build, so no do_* methods anywhere.
"""

from __future__ import annotations

import io
import itertools
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib, Gdk, GdkPixbuf, Gtk  # noqa: E402

try:
    from gi.repository import Pango  # noqa: E402
except ImportError:  # pragma: no cover
    Pango = None

from ..i18n import t
from ..platform.resize_hook import ResizeHook
from ..services.music_service import (
    MusicPlaybackMode,
    MusicPlaybackState,
    MusicSessionInfo,
    MusicSessionService,
    MusicVolumeService,
    get_source_display_name,
)
from ..services.music_view_model import (
    COVER_RETRIES,
    INFO_SETTLE_DELAY_MS,
    PLAY_PAUSE_SETTLE_DELAY_MS,
    PROGRESS_TIMER_ACTIVE_MS,
    PROGRESS_TIMER_COMPACT_MS,
    SEEK_COMMIT_SETTLE_DELAY_MS,
    VOLUME_SETTLE_DELAY_MS,
    compute_dominant_color,
    cover_signature,
    format_percent,
    format_time,
    get_next_playback_mode,
    get_playback_mode_glyph,
    normalize_display_mode,
    normalize_volume,
    resolve_content_grid_metrics,
    resolve_layout,
    resolve_record_metrics,
    resolve_session_list,
    status_text_key,
)

# One shared pool for blocking work (D-Bus reads, cover downloads, pactl).
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="music-w")

# Marquee (TitleMarquee* constants from MusicWidgetContent).
MARQUEE_GAP = 32.0
MARQUEE_START_DELAY_MS = 900
MARQUEE_SPEED_PX_PER_SECOND = 50.0
MARQUEE_OVERFLOW_TOLERANCE = 4.0
MARQUEE_DEFERRED_MEASURE_MS = 120
MARQUEE_TICK_MS = 33

GLYPH_PLAY = "▶"
GLYPH_PAUSE = "⏸"
GLYPH_PREVIOUS = "⏮"
GLYPH_NEXT = "⏭"
GLYPH_VOLUME = "🔊"

_ELLIPSIZE_END = Pango.EllipsizeMode.END if Pango else 0
_ELLIPSIZE_NONE = Pango.EllipsizeMode.NONE if Pango else 0

# Per-instance marquee class suffixes (CSS classes may not start with a digit).
_MARQUEE_SEQ = itertools.count()


class _Marquee(Gtk.Box):
    """Ellipsized label that becomes a two-copy scroller on overflow.

    Labels get size_request(0,-1) so the box minimum stays zero (full-text
    natural width would otherwise inflate the widget's minimum size); the box
    clips overflowing drawing via overflow=HIDDEN. Ticks shift the copies with
    CSS transforms — paint-only, so scrolling never re-runs size negotiation.
    (Animating via negative margins instead re-measured at every tick and
    flooded stderr with "adjusted size ... must not decrease" warnings.)
    """

    def __init__(self, css_class: str = "music-title"):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, visible=True, hexpand=True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._css_class = css_class
        self._text = ""
        self._timer: Optional[int] = None
        self._started_at = 0.0
        self._distance = 0.0
        self._label_width = 0.0

        suffix = next(_MARQUEE_SEQ)
        self._cls_a = f"mq{suffix}-a"
        self._cls_b = f"mq{suffix}-b"
        self._provider = Gtk.CssProvider.new()

        self._label = self._make_label(self._cls_a)
        self._clone = self._make_label(self._cls_b)
        self._clone.set_visible(False)
        self.append(self._label)
        self.append(self._clone)
        for label in (self._label, self._clone):
            label.get_style_context().add_provider(self._provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.connect("unmap", self._on_unmapped)

    def _make_label(self, extra_class: str) -> Gtk.Label:
        label = Gtk.Label(visible=True, hexpand=False, halign=Gtk.Align.START, ellipsize=_ELLIPSIZE_END)
        label.set_size_request(0, -1)
        label.set_css_classes([self._css_class, extra_class])
        return label

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text or ""
        self._label.set_text(self._text)
        self._clone.set_text(self._text)
        self._stop()
        # Deferred measure (the C# defers 120ms): labels need an allocation
        # before their natural width is meaningful.
        GLib.timeout_add(MARQUEE_DEFERRED_MEASURE_MS, self._evaluate)

    def set_css_extra(self, extra_class: str) -> None:
        for label, unique in ((self._label, self._cls_a), (self._clone, self._cls_b)):
            label.set_css_classes([self._css_class, unique, extra_class])

    def clear_css_extra(self) -> None:
        for label, unique in ((self._label, self._cls_a), (self._clone, self._cls_b)):
            label.set_css_classes([self._css_class, unique])

    def _label_natural_width(self) -> float:
        _minimum, natural, _, _ = self._label.measure(Gtk.Orientation.HORIZONTAL, -1)
        return float(natural)

    def _evaluate(self) -> bool:
        self._stop()
        if not self._text or not self.get_mapped():
            return False
        viewport = float(self.get_width())
        label_width = self._label_natural_width()
        if viewport <= 0 or label_width <= 0:
            return False
        if label_width - viewport <= MARQUEE_OVERFLOW_TOLERANCE:
            return False
        self._distance = label_width + MARQUEE_GAP
        self._label_width = label_width
        for label in (self._label, self._clone):
            label.set_ellipsize(_ELLIPSIZE_NONE)
        self._clone.set_visible(True)
        self._apply_offset(0.0)
        self._started_at = GLib.get_monotonic_time() / 1000.0
        self._timer = GLib.timeout_add(MARQUEE_TICK_MS, self._tick)
        return False

    def _tick(self) -> bool:
        elapsed_ms = GLib.get_monotonic_time() / 1000.0 - self._started_at
        moving = max(0.0, elapsed_ms - MARQUEE_START_DELAY_MS) / 1000.0 * MARQUEE_SPEED_PX_PER_SECOND
        self._apply_offset(moving % self._distance)
        return True

    def _apply_offset(self, offset: float) -> None:
        # Copy A paints at -offset; copy B sits naturally at label_width, so
        # GAP - offset keeps a constant GAP between the two while they scroll.
        css = (
            f".{self._cls_a}{{transform:translate({-offset:.1f}px,0px);}}"
            f".{self._cls_b}{{transform:translate({MARQUEE_GAP - offset:.1f}px,0px);}}"
        )
        self._provider.load_from_string(css)

    def _stop(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        self._label.set_ellipsize(_ELLIPSIZE_END)
        self._clone.set_visible(False)
        self._provider.load_from_string("")

    def stop(self) -> None:
        self._stop()

    def _on_unmapped(self, *_args) -> None:
        self._stop()


class _Vinyl(Gtk.DrawingArea):
    """Rotating record: grooves, label in the dominant color, spindle, and
    (vertical layout) a tonearm resting at 0° / swinging to 22° while playing.

    360° per 5s (RecordVinylRotationDuration), drawn via a 100ms timer.
    """

    ROTATION_STEP_DEGREES = 7.2

    def __init__(self, tonearm: bool):
        super().__init__(visible=True, hexpand=True, vexpand=True)
        self._angle = 0.0
        self._timer: Optional[int] = None
        self._playing = False
        self._label_color = None  # (r,g,b) floats
        self._tonearm = tonearm
        self.set_draw_func(self._draw)
        self.connect("unmap", self._on_unmapped)

    def set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        if playing:
            self._start()
        else:
            self._stop()
            self.queue_draw()

    def set_label_color(self, color) -> None:
        self._label_color = color
        self.queue_draw()

    def _start(self) -> None:
        self._stop()
        self._timer = GLib.timeout_add(100, self._advance)

    def _advance(self) -> bool:
        self._angle = (self._angle + self.ROTATION_STEP_DEGREES) % 360.0
        self.queue_draw()
        return True

    def _stop(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def stop(self) -> None:
        self._stop()

    def _on_unmapped(self, *_args) -> None:
        self._stop()

    def _draw(self, _area, cr, width, height) -> None:
        size = min(width, height)
        if size <= 4:
            return
        full = 2.0 * math.pi
        cx, cy = width / 2.0, height / 2.0
        radius = size / 2.0

        # Disc body + grooves (RecordGroove margins 3..35 of an 80-unit disc).
        cr.set_source_rgba(0.08, 0.08, 0.09, 1.0)
        cr.arc(cx, cy, radius, 0.0, full)
        cr.fill()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.06)
        cr.set_line_width(max(0.8, size / 160.0))
        inner = radius * 3.0 / 80.0
        outer = radius * 35.0 / 80.0
        for index in range(9):
            ring = inner + (outer - inner) * index / 8.0
            cr.arc(cx, cy, ring, 0.0, full)
            cr.stroke()

        # Rotating label + spindle + a visibility notch.
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(math.radians(self._angle))
        label_radius = radius * 32.0 / 80.0
        color = self._label_color or (0.45, 0.45, 0.48)
        cr.set_source_rgba(color[0], color[1], color[2], 1.0)
        cr.arc(0.0, 0.0, label_radius, 0.0, full)
        cr.fill()
        cr.set_source_rgba(0.05, 0.05, 0.05, 1.0)
        cr.arc(0.0, 0.0, max(1.5, radius * 0.045), 0.0, full)
        cr.fill()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.55)
        cr.rectangle(0.0, -label_radius, max(1.0, radius / 48.0), label_radius)
        cr.fill()
        cr.restore()

        if self._tonearm:
            # Pivot at the disc's top-right (tonearm 67×80, pivot (50,20)).
            pivot_x = cx + radius * 0.62
            pivot_y = cy - radius * 0.66
            if self._playing:
                end_x = pivot_x - radius * 0.42
                end_y = pivot_y + radius * 0.52
            else:
                end_x = pivot_x - radius * 0.30
                end_y = pivot_y + radius * 0.70
            cr.set_source_rgba(0.85, 0.85, 0.88, 0.9)
            cr.set_line_width(max(1.5, size / 52.0))
            cr.move_to(pivot_x, pivot_y)
            cr.line_to(end_x, end_y)
            cr.stroke()
            cr.arc(pivot_x, pivot_y, max(2.0, size / 40.0), 0.0, full)
            cr.fill()


class MusicSurface(Gtk.Overlay):
    def __init__(self, config, settings_service, session_service=None, volume_service=None, application=None):
        super().__init__(visible=True)
        self.config = config
        self.settings_service = settings_service
        self.application = application

        self.service = session_service or MusicSessionService()
        self.volume_service = volume_service or MusicVolumeService()

        # ── state (MusicWidgetViewModel fields) ──
        self._preferred_session_id: Optional[str] = config.metadata.get("Music.PreferredSource") or None
        self._info: Optional[MusicSessionInfo] = None
        self._session_options: List = []
        self._layout_name = ""
        self._last_width = 0.0
        self._last_height = 0.0
        self._seeking = False
        self._is_refreshing = False
        self._refresh_pending = False
        self._generation = 0
        self._volume_generation = 0
        self._syncing_volume_ui = False
        self._disposed = False
        self._revealed = False
        self._empty_info_retries = 0
        self._empty_info_timer: Optional[int] = None
        self._settle_timer: Optional[int] = None
        self._progress_timer: Optional[int] = None
        self._last_tick_monotonic = 0.0

        # Cover / ambience.
        self._cover_signature = ""
        self._cover_retry_count = 0
        self._dominant_color = None  # (r,g,b) floats for vinyl/backdrop
        self._cover_texture = None

        # Volume.
        self._volume_commit_timer: Optional[int] = None
        self._system_volume_scales: List[Gtk.Scale] = []
        self._system_volume_labels: List[Gtk.Label] = []
        self._app_volume_scales: List[Gtk.Scale] = []
        self._app_volume_labels: List[Gtk.Label] = []
        self._app_volume_rows: List[Gtk.Widget] = []
        self._volume_unavailable_labels: List[Gtk.Label] = []

        # Shared widget registries (each layout owns its own instances).
        self._title_labels: List[_Marquee] = []
        self._artist_labels: List[Gtk.Label] = []
        self._status_labels: List[Gtk.Label] = []
        self._playpause_buttons: List[Gtk.Button] = []
        self._mode_buttons: List[Gtk.Button] = []
        self._prev_buttons: List[Gtk.Button] = []
        self._next_buttons: List[Gtk.Button] = []
        self._source_buttons: List[Gtk.MenuButton] = []
        self._position_labels: List[Gtk.Label] = []
        self._duration_labels: List[Gtk.Label] = []
        self._progress_scales: List[Gtk.Scale] = []
        self._album_frames: List[Gtk.AspectFrame] = []
        self._album_pictures: List[Gtk.Picture] = []
        self._vinyls: List[_Vinyl] = []

        self._css_prefix = f"msc{id(self) & 0xFFFFFF:x}"
        self._css_provider = Gtk.CssProvider.new()

        self._build()
        self._apply_appearance()
        self._apply_layout(300, 210)  # default metrics until first allocation

        self._resize_hook = ResizeHook(self, self._on_available_size_changed)
        self.connect("map", self._on_mapped)
        self.connect("unmap", self._on_unmapped)
        self.service.on_sessions_changed = self._queue_refresh
        self.service.on_current_session_changed = self._queue_refresh
        self.service.on_playback_info_changed = self._queue_refresh
        self.service.on_media_properties_changed = self._queue_refresh
        self.service.on_timeline_properties_changed = self._queue_refresh
        GLib.idle_add(self._initialize)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _on_available_size_changed(self, width: float, height: float) -> None:
        if not (math.isfinite(width) and math.isfinite(height)):
            return
        self._last_width = width
        self._last_height = height
        self._apply_layout(width, height)

    def _apply_layout(self, width: float, height: float) -> None:
        if width <= 0 or height <= 0:
            return
        mode = normalize_display_mode(self.settings_service.settings.music.musicDisplayMode)
        new_layout = resolve_layout(width, height, mode)
        if new_layout != self._layout_name:
            self._layout_name = new_layout
            self.stack.set_visible_child_name(new_layout.lower())
        if new_layout in ("Controls", "Minimal"):
            metrics = resolve_content_grid_metrics(width, height)
            size = int(metrics["albumSize"])
            for frame in self._album_frames:
                frame.set_size_request(size, size)
            for vinyl in self._vinyls:
                vinyl.stop()
        else:
            record = resolve_record_metrics(width, height)
            self._apply_record_sizing(record)
            if new_layout == "RecordHorizontal":
                self._apply_record_sizing(record)

    def _apply_record_sizing(self, metrics) -> None:
        vinyl_size = int(metrics["vinylSize"])
        for vinyl in self._vinyls:
            vinyl.set_size_request(vinyl_size, vinyl_size)
        for marquee in self._title_labels:
            if metrics["small"]:
                marquee.set_css_extra("music-title-small")
            else:
                marquee.clear_css_extra()
        self._update_vinyl_play_state()

    def _on_mapped(self, *_args) -> None:
        if self._disposed or self._revealed:
            return
        self._revealed = True
        self._start_progress_timer()

    def _on_unmapped(self, *_args) -> None:
        # OnWindowVisibilityChanged(false): bump generations so in-flight
        # workers from the hidden window never apply.
        self._revealed = False
        self._generation += 1
        self._is_refreshing = False
        self._refresh_pending = False
        self._stop_progress_timer()
        for vinyl in self._vinyls:
            vinyl.stop()

    def dispose(self) -> None:
        self._disposed = True
        self._generation += 1
        self._volume_generation += 1
        self._stop_progress_timer()
        for vinyl in self._vinyls:
            vinyl.stop()
        for timer_attr in ("_empty_info_timer", "_settle_timer", "_volume_commit_timer"):
            timer_id = getattr(self, timer_attr)
            if timer_id is not None:
                GLib.source_remove(timer_id)
                setattr(self, timer_attr, None)
        self.service.on_sessions_changed = None
        self.service.on_current_session_changed = None
        self.service.on_playback_info_changed = None
        self.service.on_media_properties_changed = None
        self.service.on_timeline_properties_changed = None

    def _initialize(self) -> bool:
        if self._disposed:
            return False
        try:
            self.service.initialize()
        except Exception:
            pass
        self.refresh()
        return False

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.set_css_classes(["music-surface"])
        self.backdrop = Gtk.Box(visible=True)
        self.backdrop.set_css_classes(["music-backdrop"])
        self.add_overlay(self.backdrop)
        self.set_clip_overlay(self.backdrop, True)

        self.stack = Gtk.Stack(visible=True, hexpand=True, vexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_minimal(), "minimal")
        self.stack.add_named(self._build_controls(), "controls")
        self.stack.add_named(self._build_record(vertical=True), "recordvertical")
        self.stack.add_named(self._build_record(vertical=False), "recordhorizontal")
        self.set_child(self.stack)

    # Minimal: full-bleed cover + bottom mini panel (playpause/next, marquee, source).
    def _build_minimal(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        picture = Gtk.Picture(visible=True, hexpand=True, vexpand=True)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        aspect = Gtk.AspectFrame(visible=True, ratio=1.0)
        aspect.set_child(picture)
        root.append(aspect)

        panel = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True, spacing=6)
        panel.set_css_classes(["music-mini-panel"])
        playpause = self._make_playpause()
        nxt = self._make_next()
        title = _Marquee()
        source = self._make_source_button()
        panel.append(playpause)
        panel.append(nxt)
        panel.append(title)
        panel.append(source)
        root.append(panel)

        self._title_labels.append(title)
        self._playpause_buttons.append(playpause)
        self._next_buttons.append(nxt)
        self._source_buttons.append(source)
        self._album_frames.append(aspect)
        self._album_pictures.append(picture)
        return root

    # Controls: album + info + progress + transport.
    def _build_controls(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True, spacing=8)
        root.set_css_classes(["music-content-root"])
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True, spacing=12)
        content.set_valign(Gtk.Align.START)

        picture = Gtk.Picture(visible=True)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        aspect = Gtk.AspectFrame(visible=True, ratio=1.0)
        aspect.set_overflow(Gtk.Overflow.HIDDEN)  # clip to border-radius
        aspect.set_css_classes(["music-art"])
        aspect.set_child(picture)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True, spacing=2)
        info.set_valign(Gtk.Align.CENTER)
        info.set_hexpand(True)
        title = _Marquee()
        artist = Gtk.Label(visible=True, halign=Gtk.Align.START, ellipsize=_ELLIPSIZE_END)
        artist.set_css_classes(["music-artist"])
        meta_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True, spacing=6)
        source = self._make_source_button()
        status = Gtk.Label(visible=True, halign=Gtk.Align.START)
        status.set_css_classes(["music-status"])
        meta_line.append(source)
        meta_line.append(status)
        info.append(title)
        info.append(artist)
        info.append(meta_line)
        content.append(aspect)
        content.append(info)

        progress_row, position_label, duration_label, scale = self._make_progress_row()
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True, spacing=12)
        controls.set_css_classes(["music-controls"])
        controls.set_valign(Gtk.Align.END)
        mode = self._make_mode_button()
        prev = self._make_previous()
        playpause = self._make_playpause(large=True)
        nxt = self._make_next()
        volume = self._make_volume_button()
        controls.append(mode)
        controls.append(prev)
        controls.append(playpause)
        controls.append(nxt)
        controls.append(volume)

        root.append(content)
        root.append(progress_row)
        root.append(controls)

        self._title_labels.append(title)
        self._artist_labels.append(artist)
        self._status_labels.append(status)
        self._mode_buttons.append(mode)
        self._prev_buttons.append(prev)
        self._playpause_buttons.append(playpause)
        self._next_buttons.append(nxt)
        self._source_buttons.append(source)
        self._position_labels.append(position_label)
        self._duration_labels.append(duration_label)
        self._progress_scales.append(scale)
        self._album_frames.append(aspect)
        self._album_pictures.append(picture)
        return root

    # Record layouts: vinyl + info + transport.
    def _build_record(self, vertical: bool) -> Gtk.Widget:
        root = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
            visible=True,
            spacing=8,
        )
        vinyl = _Vinyl(tonearm=vertical)
        vinyl.set_halign(Gtk.Align.CENTER)
        vinyl.set_valign(Gtk.Align.CENTER)
        self._vinyls.append(vinyl)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True, spacing=2)
        info.set_valign(Gtk.Align.CENTER)
        info.set_hexpand(not vertical)
        title = _Marquee()
        artist = Gtk.Label(visible=True, halign=Gtk.Align.START, ellipsize=_ELLIPSIZE_END)
        artist.set_css_classes(["music-artist"])
        source = self._make_source_button()
        info.append(title)
        info.append(artist)
        info.append(source)

        progress_row, position_label, duration_label, scale = self._make_progress_row(compact=True)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True, spacing=10)
        mode = self._make_mode_button()
        prev = self._make_previous()
        playpause = self._make_playpause(large=True)
        nxt = self._make_next()
        volume = self._make_volume_button()
        controls.append(mode)
        controls.append(prev)
        controls.append(playpause)
        controls.append(nxt)
        controls.append(volume)

        if vertical:
            root.append(vinyl)
            root.append(info)
            root.append(progress_row)
            root.append(controls)
        else:
            disc_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                visible=True,
                spacing=6,
                halign=Gtk.Align.CENTER,
            )
            disc_box.append(vinyl)
            root.append(disc_box)
            side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True, spacing=6, hexpand=True)
            side.append(info)
            side.append(progress_row)
            side.append(controls)
            root.append(side)

        self._title_labels.append(title)
        self._artist_labels.append(artist)
        self._mode_buttons.append(mode)
        self._prev_buttons.append(prev)
        self._playpause_buttons.append(playpause)
        self._next_buttons.append(nxt)
        self._source_buttons.append(source)
        self._position_labels.append(position_label)
        self._duration_labels.append(duration_label)
        self._progress_scales.append(scale)
        return root

    def _make_playpause(self, large: bool = False) -> Gtk.Button:
        button = Gtk.Button(visible=True)
        self._style_button(
            button,
            GLYPH_PLAY,
            ["music-playpause"] + (["music-btn-large"] if large else []),
            "Music.Control.Play",
        )
        button.connect("clicked", self._on_playpause_clicked)
        return button

    def _make_previous(self) -> Gtk.Button:
        button = Gtk.Button(visible=True)
        self._style_button(button, GLYPH_PREVIOUS, ["music-btn"], "Music.Control.Previous")
        button.connect("clicked", lambda *_: self._dispatch_transport("try_previous"))
        return button

    def _make_next(self) -> Gtk.Button:
        button = Gtk.Button(visible=True)
        self._style_button(button, GLYPH_NEXT, ["music-btn"], "Music.Control.Next")
        button.connect("clicked", lambda *_: self._dispatch_transport("try_next"))
        return button

    def _make_mode_button(self) -> Gtk.Button:
        button = Gtk.Button(visible=True)
        self._style_button(
            button,
            get_playback_mode_glyph(MusicPlaybackMode.NORMAL),
            ["music-btn", "music-mode"],
            "Music.Control.Mode.Normal",
        )
        button.connect("clicked", self._on_mode_clicked)
        return button

    def _make_volume_button(self) -> Gtk.Widget:
        # A popover per layout (one Gtk.Popover cannot be shared between
        # parents); all instances are driven through the registries below.
        button = Gtk.MenuButton(visible=True, label=GLYPH_VOLUME)
        button.set_css_classes(["music-btn", "music-volume"])
        button.set_tooltip_text(t("Music.Control.Volume"))
        button.set_has_frame(False)

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        system_title = Gtk.Label(visible=True, halign=Gtk.Align.START, label=t("Music.Volume.System"))
        system_title.set_css_classes(["music-volume-title"])
        system_scale = self._make_volume_scale("system")
        system_label = Gtk.Label(visible=True, label=format_percent(0.0))
        system_label.set_css_classes(["music-volume-text"])
        system_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True, spacing=8)
        system_row.append(system_title)
        system_row.append(system_scale)
        system_row.append(system_label)

        app_title = Gtk.Label(visible=True, halign=Gtk.Align.START, label=t("Music.Volume.App"))
        app_title.set_css_classes(["music-volume-title"])
        app_scale = self._make_volume_scale("app")
        app_label = Gtk.Label(visible=True, label=format_percent(0.0))
        app_label.set_css_classes(["music-volume-text"])
        app_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True, spacing=8)
        app_row.append(app_title)
        app_row.append(app_scale)
        app_row.append(app_label)

        unavailable = Gtk.Label(visible=True, label=t("Music.Volume.Unavailable"))
        unavailable.set_css_classes(["music-volume-unavailable"])

        box.append(system_row)
        box.append(app_row)
        box.append(unavailable)
        popover.set_child(box)
        button.set_popover(popover)
        popover.connect("map", lambda *_: self._refresh_volume_snapshot())

        self._system_volume_scales.append(system_scale)
        self._system_volume_labels.append(system_label)
        self._app_volume_scales.append(app_scale)
        self._app_volume_labels.append(app_label)
        self._app_volume_rows.append(app_row)
        self._volume_unavailable_labels.append(unavailable)
        return button

    def _make_volume_scale(self, kind: str) -> Gtk.Scale:
        adjustment = Gtk.Adjustment(value=0.0, lower=0.0, upper=1.0, step_increment=0.01)
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=adjustment,
            visible=True,
            hexpand=True,
        )
        scale.set_size_request(140, -1)
        scale.set_draw_value(False)
        scale.set_css_classes(["music-volume-scale"])
        scale.connect("value-changed", self._on_volume_scale_changed, kind)
        return scale

    def _make_source_button(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton(visible=True, label=t("Music.SourceUnknown"))
        button.set_css_classes(["music-source"])
        button.set_tooltip_text(t("Music.SwitchSource"))
        button.set_create_popup_func(self._populate_source_popover)
        return button

    def _make_progress_row(self, compact: bool = False) -> tuple:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True, spacing=6)
        position_label = Gtk.Label(visible=True, label="0:00")
        position_label.set_css_classes(["music-progress-text"])
        duration_label = Gtk.Label(visible=True, label="0:00")
        duration_label.set_css_classes(["music-progress-text"])
        adjustment = Gtk.Adjustment(value=0.0, lower=0.0, upper=1.0, step_increment=1.0)
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=adjustment,
            visible=True,
            hexpand=True,
        )
        scale.set_draw_value(False)
        scale.set_css_classes(["music-progress"] + (["music-progress-compact"] if compact else []))
        scale.set_sensitive(False)
        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_progress_drag_begin)
        drag.connect("drag-end", self._on_progress_drag_end)
        scale.add_controller(drag)
        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_progress_click)
        scale.add_controller(click)
        row.append(position_label)
        row.append(scale)
        row.append(duration_label)
        return row, position_label, duration_label, scale

    @staticmethod
    def _style_button(button: Gtk.Button, glyph: str, css_classes: List[str], tooltip_key: str) -> None:
        button.set_css_classes(["music-btn", *css_classes])
        button.set_label(glyph)
        button.set_tooltip_text(t(tooltip_key))
        button.set_has_frame(False)

    # ── appearance / CSS ───────────────────────────────────────────────────────

    def _apply_appearance(self) -> None:
        use_backdrop = self.settings_service.settings.music.musicUseArtworkBackdrop
        self.backdrop.set_visible(use_backdrop)
        self._inject_backdrop_css()

    def _inject_backdrop_css(self) -> None:
        from ..services.music_view_model import artwork_backdrop_stops

        stops = artwork_backdrop_stops(
            tuple(int(channel * 255) for channel in self._dominant_color) if self._dominant_color else None
        )
        stop_text = ", ".join(f"{position:.2f} {color}" for position, color in stops)
        css = f".{self._css_prefix}-backdrop {{ background: linear-gradient(to bottom, {stop_text}); }}"
        self._css_provider.load_from_string(css)
        self.backdrop.set_css_classes([f"{self._css_prefix}-backdrop", "music-backdrop"])
        context = self.backdrop.get_style_context()
        context.add_provider(self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── refresh pipeline (RefreshAsync merge loop) ────────────────────────────

    def _queue_refresh(self) -> None:
        if self._disposed:
            return
        self.refresh()

    def refresh(self) -> None:
        if self._disposed:
            return
        if self._is_refreshing:
            self._refresh_pending = True
            return
        self._is_refreshing = True
        self._generation += 1
        generation = self._generation

        def work():
            preferred = self._preferred_session_id
            options = self.service.get_session_options()
            info = self.service.get_current_session_info(preferred)
            ordered, kept_preferred = resolve_session_list(options, preferred)
            return options, ordered, kept_preferred, info

        def applied(future) -> bool:
            if self._disposed:
                return False
            try:
                _options, ordered, kept_preferred, info = future.result()
            except Exception:
                ordered, kept_preferred, info = [], None, None
            if generation != self._generation:
                return False  # superseded (hidden, disposed, or newer refresh)
            self._is_refreshing = False
            self._session_options = ordered
            if self._preferred_session_id and not kept_preferred:
                self._set_preferred_session(None)  # preferred session vanished
            # settle=False: a settle re-read here would arm another refresh,
            # which applies info again and re-arms settle — an infinite 180ms
            # poll that also churns _generation and starves cover loads.
            # Settle re-reads are armed by the interactive paths instead
            # (play/pause, mode, seek).
            self._apply_info(info, settle=False)
            if self._refresh_pending:
                self._refresh_pending = False
                self.refresh()
            return False

        future = _executor.submit(work)
        future.add_done_callback(lambda f: GLib.idle_add(applied, f))

    def _apply_info(self, info: Optional[MusicSessionInfo], settle: bool = False) -> None:
        self._info = info
        # Empty-info deferral (ApplyInfoAsync): fresh sessions often report
        # empty titles for a few hundred ms — retry 4× at 80+n*60ms.
        if info is not None and info.sessionId and not (info.title or info.artist):
            if self._empty_info_retries < 4:
                self._empty_info_retries += 1
                delay = 80 + (self._empty_info_retries - 1) * 60
                self._arm_timer(delay, self._empty_info_retry_tick, "_empty_info_timer")
                return
        else:
            self._empty_info_retries = 0

        self._render_session(info)
        self._update_cover(info)
        self._start_progress_timer()
        if settle and info is not None and info.sessionId:
            # Info settle re-read (180ms) catches late metadata updates.
            self._arm_timer(INFO_SETTLE_DELAY_MS, self._settle_tick, "_settle_timer")

    def _arm_timer(self, delay_ms: int, callback, attr: str) -> None:
        existing = getattr(self, attr)
        if existing is not None:
            GLib.source_remove(existing)
            setattr(self, attr, None)
        setattr(self, attr, GLib.timeout_add(delay_ms, callback))

    def _empty_info_retry_tick(self) -> bool:
        self._empty_info_timer = None
        if self._disposed:
            return False
        self.refresh()
        return False

    def _settle_tick(self) -> bool:
        self._settle_timer = None
        if self._disposed:
            return False
        self.refresh()
        return False

    # ── rendering ─────────────────────────────────────────────────────────────

    def _display_source_name(self) -> str:
        if not self._info or not self._info.sessionId:
            return t("Music.SourceUnknown")
        for option in self._session_options:
            if option.sessionId == self._info.sessionId:
                return option.sourceDisplayName
        return get_source_display_name(self._info.sessionId)

    def _render_session(self, info: Optional[MusicSessionInfo]) -> None:
        has_session = bool(info and info.sessionId)
        title = (info.title if info.title else t("Music.EmptyTitle")) if has_session else t("Music.EmptyTitle")
        artist = info.artist if has_session and info.artist else ""
        for marquee in self._title_labels:
            marquee.set_text(title)
        for label in self._artist_labels:
            label.set_text(artist)
            label.set_visible(bool(artist))
        status_key = status_text_key(info.playbackState if info else MusicPlaybackState.UNKNOWN, has_session)
        for label in self._status_labels:
            label.set_text(t(status_key))
        source_name = self._display_source_name()
        for button in self._source_buttons:
            button.set_label(source_name)
        self._render_playback_state(info)

    def _render_playback_state(self, info: Optional[MusicSessionInfo]) -> None:
        playing = bool(info) and info.playbackState == MusicPlaybackState.PLAYING
        glyph = GLYPH_PAUSE if playing else GLYPH_PLAY
        tooltip = t("Music.Control.Pause" if playing else "Music.Control.Play")
        for button in self._playpause_buttons:
            button.set_label(glyph)
            button.set_tooltip_text(tooltip)
            button.set_sensitive(bool(info))
        mode = info.playbackMode if info else MusicPlaybackMode.NORMAL
        mode_tooltip_key = {
            MusicPlaybackMode.NORMAL: "Music.Control.Mode.Normal",
            MusicPlaybackMode.SHUFFLE: "Music.Control.Mode.Shuffle",
            MusicPlaybackMode.REPEAT: "Music.Control.Mode.Repeat",
        }[mode]
        for button in self._mode_buttons:
            button.set_label(get_playback_mode_glyph(mode))
            button.set_tooltip_text(t(mode_tooltip_key))
        can_seek = bool(info) and info.canSeek and info.duration > 0
        for scale in self._progress_scales:
            scale.set_sensitive(can_seek and not self._seeking)
        self._render_timeline(force=True)
        self._update_vinyl_play_state()

    def _render_timeline(self, force: bool = False) -> None:
        if self._seeking and not force:
            return
        position = self._info.position if self._info else 0.0
        duration = self._info.duration if self._info else 0.0
        for label in self._position_labels:
            label.set_text(format_time(position))
        for label in self._duration_labels:
            label.set_text(format_time(duration))
        for scale in self._progress_scales:
            adjustment = scale.get_adjustment()
            adjustment.set_upper(max(1.0, duration))
            if not self._seeking:
                adjustment.set_value(min(position, max(0.0, duration)))
        self._last_tick_monotonic = GLib.get_monotonic_time() / 1000.0

    def _update_vinyl_play_state(self) -> None:
        playing = bool(self._info) and self._info.playbackState == MusicPlaybackState.PLAYING
        for vinyl in self._vinyls:
            vinyl.set_playing(playing)
            vinyl.set_label_color(self._dominant_color)

    # ── progress timer (cadence 500/1000ms by playback state) ────────────────

    def _start_progress_timer(self) -> None:
        self._stop_progress_timer()
        if self._disposed or not self._revealed:
            return
        playing = bool(self._info) and self._info.playbackState == MusicPlaybackState.PLAYING
        interval = PROGRESS_TIMER_ACTIVE_MS if playing else PROGRESS_TIMER_COMPACT_MS
        self._progress_timer = GLib.timeout_add(interval, self._progress_tick)

    def _stop_progress_timer(self) -> None:
        if self._progress_timer is not None:
            GLib.source_remove(self._progress_timer)
            self._progress_timer = None

    def _progress_tick(self) -> bool:
        if self._disposed:
            return False
        if self._info and not self._seeking:
            if self._info.playbackState == MusicPlaybackState.PLAYING:
                now = GLib.get_monotonic_time() / 1000.0
                advanced = (now - self._last_tick_monotonic) / 1000.0
                duration = self._info.duration
                if duration > 0:
                    self._info.position = min(max(0.0, self._info.position + advanced), duration)
                else:
                    self._info.position += advanced
            self._render_timeline()
        return True

    # ── transport actions ─────────────────────────────────────────────────────

    def _submit(self, work, applied) -> None:
        future = _executor.submit(work)
        future.add_done_callback(lambda f: GLib.idle_add(applied, f))

    def _dispatch_transport(self, method_name: str) -> None:
        preferred = self._preferred_session_id
        method = getattr(self.service, method_name)

        def work():
            return bool(method(preferred))

        def applied(future) -> bool:
            if self._disposed:
                return False
            try:
                future.result()
            except Exception:
                pass
            self.refresh()
            return False

        self._submit(work, applied)

    def _on_playpause_clicked(self, *_args) -> None:
        preferred = self._preferred_session_id

        def work():
            return bool(self.service.try_toggle_play_pause(preferred))

        def applied(future) -> bool:
            if self._disposed:
                return False
            try:
                future.result()
            except Exception:
                pass
            # TogglePlayPause: immediate refresh + 180ms settle re-read
            # (players flip PlaybackStatus asynchronously).
            self.refresh()
            GLib.timeout_add(PLAY_PAUSE_SETTLE_DELAY_MS, self._settle_tick)
            return False

        self._submit(work, applied)

    def _on_mode_clicked(self, *_args) -> None:
        current = self._info.playbackMode if self._info else MusicPlaybackMode.NORMAL
        target = get_next_playback_mode(current)
        preferred = self._preferred_session_id
        generation = self._generation

        def work():
            return bool(self.service.try_change_playback_mode(preferred, target))

        def applied(future) -> bool:
            if self._disposed or generation != self._generation:
                return False
            try:
                future.result()
            except Exception:
                pass
            self.refresh()
            # CyclePlaybackModeAsync does a second refresh shortly after.
            GLib.timeout_add(200, self._settle_tick)
            return False

        self._submit(work, applied)

    # ── seek (BeginSeek / CommitSeekAsync) ────────────────────────────────────

    def _scale_from_gesture(self, gesture) -> Optional[Gtk.Scale]:
        widget = gesture.get_widget()
        return widget if isinstance(widget, Gtk.Scale) else None

    def _seek_target(self, scale: Gtk.Scale, x: float) -> float:
        width = scale.get_width()
        if width <= 1:
            return 0.0
        ratio = min(1.0, max(0.0, x / width))
        return ratio * (self._info.duration if self._info else 0.0)

    def _on_progress_drag_begin(self, gesture, start_x: float, _start_y: float) -> None:
        scale = self._scale_from_gesture(gesture)
        if scale is None or not scale.get_sensitive():
            return
        self._seeking = True
        for other in self._progress_scales:
            other.set_sensitive(False)
        self._preview_seek(scale, start_x)

    def _on_progress_drag_end(self, gesture, offset_x: float, _offset_y: float) -> None:
        scale = self._scale_from_gesture(gesture)
        if scale is None or not self._seeking:
            return
        start_x = gesture.get_start_point()[1]
        start_x = start_x if isinstance(start_x, (int, float)) else 0.0
        self._commit_seek(self._seek_target(scale, start_x + offset_x))

    def _on_progress_click(self, gesture, n_press: int, x: float, _y: float) -> None:
        if n_press != 1:
            return
        scale = self._scale_from_gesture(gesture)
        if scale is None or not scale.get_sensitive():
            return
        self._seeking = True
        self._commit_seek(self._seek_target(scale, x))

    def _preview_seek(self, scale: Gtk.Scale, x: float) -> None:
        target = self._seek_target(scale, x)
        duration = self._info.duration if self._info else 0.0
        scale.get_adjustment().set_value(min(target, max(1.0, duration)))
        for label in self._position_labels:
            label.set_text(format_time(target))

    def _commit_seek(self, target: float) -> None:
        self._seeking = False
        can_seek = bool(self._info) and self._info.canSeek
        for scale in self._progress_scales:
            scale.set_sensitive(can_seek)
        if not can_seek:
            self.refresh()
            return
        preferred = self._preferred_session_id
        generation = self._generation

        def work():
            return bool(self.service.try_seek(preferred, target))

        def applied(future) -> bool:
            if self._disposed or generation != self._generation:
                return False
            try:
                succeeded = future.result()
            except Exception:
                succeeded = False
            if succeeded and self._info:
                self._info.position = target  # optimistic; Seeked corrects it
                self._render_timeline(force=True)
                GLib.timeout_add(SEEK_COMMIT_SETTLE_DELAY_MS, self._settle_tick)
            else:
                self.refresh()
            return False

        self._submit(work, applied)

    # ── cover + ambience (MediaInfo partial) ─────────────────────────────────

    def _update_cover(self, info: Optional[MusicSessionInfo]) -> None:
        signature = cover_signature(
            info.sessionId if info else None,
            info.title if info else "",
            info.artist if info else "",
            info.album if info else "",
        )
        if signature == self._cover_signature:
            return
        self._cover_signature = signature
        self._cover_retry_count = 0
        self._load_cover(info.artUrl if info else "")

    def _load_cover(self, art_url: str) -> None:
        self._set_cover(None, None)
        if not art_url:
            return
        generation = self._generation

        def work():
            return _fetch_cover_texture(art_url)

        def applied(future) -> bool:
            if self._disposed or generation != self._generation:
                return False
            try:
                texture, dominant = future.result()
            except Exception:
                texture, dominant = None, None
            if texture is None:
                # Cover retry 3× at 250*n ms (transient artUrl gaps).
                if self._cover_retry_count < COVER_RETRIES:
                    self._cover_retry_count += 1
                    GLib.timeout_add(250 * self._cover_retry_count, self._cover_retry_tick, art_url)
                return False
            self._cover_retry_count = 0
            self._set_cover(texture, dominant)
            return False

        self._submit(work, applied)

    def _cover_retry_tick(self, art_url: str) -> bool:
        if self._disposed:
            return False
        self._load_cover(art_url)
        return False

    def _set_cover(self, texture, dominant) -> None:
        self._cover_texture = texture
        self._dominant_color = dominant
        for picture in self._album_pictures:
            picture.set_paintable(texture)
        self._update_vinyl_play_state()
        self._inject_backdrop_css()

    # ── volume (MusicVolumeService snapshot contract + pending coalesce) ──────

    def _refresh_volume_snapshot(self) -> None:
        self._volume_generation += 1
        generation = self._volume_generation
        source_id = self._info.sessionId if self._info else ""
        display_name = get_source_display_name(source_id) if source_id else ""

        def work():
            return self.volume_service.get_volume_snapshot(source_id, display_name)

        def applied(future) -> bool:
            if self._disposed or generation != self._volume_generation:
                return False
            try:
                system_volume, session_volume, has_session = future.result()
            except Exception:
                system_volume, session_volume, has_session = 0.0, 0.0, False
            self._syncing_volume_ui = True
            try:
                for scale, label in zip(self._system_volume_scales, self._system_volume_labels):
                    scale.set_value(normalize_volume(system_volume))
                    label.set_text(format_percent(system_volume))
                for scale, label in zip(self._app_volume_scales, self._app_volume_labels):
                    scale.set_value(normalize_volume(session_volume))
                    label.set_text(format_percent(session_volume))
            finally:
                self._syncing_volume_ui = False
            for row in self._app_volume_rows:
                row.set_visible(has_session)
            for label in self._volume_unavailable_labels:
                label.set_visible(not has_session)
            return False

        self._submit(work, applied)

    def _on_volume_scale_changed(self, scale: Gtk.Scale, kind: str) -> None:
        if self._syncing_volume_ui:
            return
        value = normalize_volume(scale.get_value())
        labels = self._system_volume_labels if kind == "system" else self._app_volume_labels
        for label in labels:
            label.set_text(format_percent(value))
        # Pending-coalesce (C# loop): keep resetting a 260ms commit timer.
        if self._volume_commit_timer is not None:
            GLib.source_remove(self._volume_commit_timer)
        self._volume_commit_timer = GLib.timeout_add(VOLUME_SETTLE_DELAY_MS, self._commit_volume, kind, value)

    def _commit_volume(self, kind: str, value: float) -> bool:
        self._volume_commit_timer = None
        if self._disposed:
            return False
        self._volume_generation += 1
        source_id = self._info.sessionId if self._info else ""
        display_name = get_source_display_name(source_id) if source_id else ""

        def work():
            if kind == "system":
                return self.volume_service.try_set_system_master_volume(value)
            return self.volume_service.try_set_session_volume(source_id, display_name, value)

        def applied(future) -> bool:
            if self._disposed:
                return False
            try:
                future.result()
            except Exception:
                pass
            GLib.timeout_add(100, self._refresh_volume_snapshot)
            return False

        self._submit(work, applied)
        return False

    # ── source picker (source flyout) ────────────────────────────────────────

    def _populate_source_popover(self, _button, popover) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        preferred = self._preferred_session_id

        group: Optional[Gtk.CheckButton] = None
        if not self._session_options:
            empty = Gtk.CheckButton(visible=True, label=t("Music.NoAvailableSources"))
            empty.set_active(True)
            empty.set_sensitive(False)
            box.append(empty)
            popover.set_child(box)
            return

        follow = Gtk.CheckButton(visible=True, label=t("Music.FollowSystemSource"))
        follow.set_active(preferred is None)
        follow.connect("toggled", self._on_source_row_toggled, None)
        box.append(follow)
        group = follow

        for option in self._session_options:
            row = Gtk.CheckButton(visible=True, label=option.sourceDisplayName)
            row.set_group(group)
            row.set_active(option.sessionId == preferred)
            row.connect("toggled", self._on_source_row_toggled, option.sessionId)
            box.append(row)
        popover.set_child(box)

    def _on_source_row_toggled(self, check: Gtk.CheckButton, session_id: Optional[str]) -> None:
        if not check.get_active():
            return
        self._set_preferred_session(session_id)
        self.refresh()

    def _set_preferred_session(self, session_id: Optional[str]) -> None:
        # SelectSessionAsync: an invalid pick falls back to following system.
        self._preferred_session_id = session_id
        if session_id is None:
            self.config.metadata.pop("Music.PreferredSource", None)
        else:
            self.config.metadata["Music.PreferredSource"] = session_id
        self.settings_service.save_debounced()


def _fetch_cover_texture(art_url: str):
    """file:// or http(s) URL → (Gdk.Texture, dominant rgb floats). Worker."""
    from PIL import Image

    if art_url.startswith("file://"):
        image = Image.open(urllib.request.url2pathname(art_url[7:]))
    elif art_url.startswith("/"):
        image = Image.open(art_url)
    else:
        with urllib.request.urlopen(art_url, timeout=8) as response:
            payload = response.read()
        image = Image.open(io.BytesIO(payload))
    image = image.convert("RGB")
    image.thumbnail((192, 192))  # DecodePixelWidth 192
    dominant = compute_dominant_color(image.convert("RGBA").tobytes(), image.width, image.height)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    loader = GdkPixbuf.PixbufLoader()
    loader.write(buffer.getvalue())
    loader.close()
    texture = Gdk.Texture.new_for_pixbuf(loader.get_pixbuf())
    dominant_normalized = None
    if dominant:
        dominant_normalized = (dominant[0] / 255.0, dominant[1] / 255.0, dominant[2] / 255.0)
    return texture, dominant_normalized
