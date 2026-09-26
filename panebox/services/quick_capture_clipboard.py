"""Quick Capture clipboard monitor (port of Services/QuickCaptureClipboardService.cs).

Listens to the Gdk clipboard, records text/links (and optionally images) into
the Quick Capture recent list. Runs only while the Quick Capture feature is
enabled and clipboard recording is on; re-reads on every "changed" and skips
payloads PaneBox itself wrote (clipboard_write_scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..i18n import t
from .quick_capture_service import QuickCaptureService

MAX_CLIPBOARD_TEXT_CHARACTERS = 20000
MAX_CLIPBOARD_IMAGE_BYTES = 12 * 1024 * 1024


@dataclass
class QuickCaptureClipboardDiagnostics:
    is_recording: bool
    is_listening: bool
    last_captured_at: Optional[datetime]
    last_reason: str
    last_reason_at: Optional[datetime]


class QuickCaptureClipboardMonitor:
    def __init__(
        self,
        settings_service,
        quick_capture_service: QuickCaptureService,
        clipboard=None,
        log=lambda _m: None,
    ):
        self.settings = settings_service
        self.service = quick_capture_service
        self._clipboard = clipboard
        self.log = log
        self._is_started = False
        self._is_processing = False
        self._has_pending_capture = False
        self._last_state_log: Optional[str] = None
        self._last_captured_at: Optional[datetime] = None
        self._last_reason = "disabled:initial"
        self._last_reason_at: Optional[datetime] = None
        self.on_diagnostics_changed = None

    # ---- lifecycle -----------------------------------------------------------

    @property
    def clipboard(self):
        if self._clipboard is None:
            import gi

            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            display = Gdk.Display.get_default()
            self._clipboard = display.get_clipboard() if display is not None else None
        return self._clipboard

    def refresh(self) -> None:
        if self.should_capture_clipboard():
            self._set_reason("enabled")
            self.start()
        else:
            self._set_reason(self._build_disabled_reason())
            self.stop()

    def start(self) -> None:
        if self._is_started:
            return
        clipboard = self.clipboard
        if clipboard is None:
            return
        clipboard.connect("changed", self._on_clipboard_changed)
        self._is_started = True
        self._log_state("started")
        self._capture_current()

    def stop(self) -> None:
        if not self._is_started:
            return
        clipboard = self.clipboard
        if clipboard is not None:
            try:
                clipboard.disconnect_by_func(self._on_clipboard_changed)
            except (TypeError, RuntimeError):
                pass
        self._is_started = False
        self._log_state("stopped")

    def get_diagnostics(self) -> QuickCaptureClipboardDiagnostics:
        return QuickCaptureClipboardDiagnostics(
            is_recording=self.should_capture_clipboard(),
            is_listening=self._is_started,
            last_captured_at=self._last_captured_at,
            last_reason=self._last_reason,
            last_reason_at=self._last_reason_at,
        )

    def should_capture_clipboard(self) -> bool:
        settings = self.settings.settings
        feature_states = self.settings.layout.featureWidgetEnabledStates or {}
        return bool(feature_states.get("QuickCapture", False) and settings.quickCapture.quickCaptureClipboardEnabled)

    def _build_disabled_reason(self) -> str:
        feature_states = self.settings.layout.featureWidgetEnabledStates or {}
        if not feature_states.get("QuickCapture", False):
            return "disabled:quick-capture-off"
        if not self.settings.settings.quickCapture.quickCaptureClipboardEnabled:
            return "disabled:clipboard-off"
        return "disabled:unknown"

    def capture_current(self) -> None:
        self._capture_current()

    # ---- capture ---------------------------------------------------------------

    def _on_clipboard_changed(self, *_args) -> None:
        self._capture_current()

    def _capture_current(self) -> None:
        if self._is_processing:
            self._has_pending_capture = True
            return
        self._is_processing = True
        try:
            if not self.should_capture_clipboard():
                self._set_reason(self._build_disabled_reason())
                return
            clipboard = self.clipboard
            if clipboard is None:
                self._set_reason("failed:no-clipboard")
                return
            self._read_and_record(clipboard)
        except Exception as exc:  # a bad payload must never kill the monitor
            self._set_reason("failed:read-or-save")
            self.log(f"[QuickCaptureClipboard] capture failed: {exc}")
            self._is_processing = False
            self._maybe_drain_pending()

    def _read_and_record(self, clipboard) -> None:
        from .quick_capture_service import normalize_recent_limit

        max_items = normalize_recent_limit(self.settings.settings.quickCapture.quickCaptureRecentLimit)
        image_enabled = self.settings.settings.quickCapture.quickCaptureImageClipboardEnabled

        # Snapshot the advertised formats: an async read initiated against one
        # clipboard state can complete after a new write, and GDK then decodes
        # through a degraded fallback target (mojibake). Only record content
        # observed under the state the read was started against.
        state = (max_items, clipboard.get_formats().to_string())

        if image_enabled and clipboard.get_formats().contain_gtype(_texture_gtype()):
            clipboard.read_texture_async(None, self._on_texture_read, state)
            return

        clipboard.read_text_async(None, self._on_text_read, state)

    def _finish_capture(self) -> None:
        self._is_processing = False
        self._maybe_drain_pending()

    def _maybe_drain_pending(self) -> None:
        if self._has_pending_capture:
            self._has_pending_capture = False
            self._capture_current()

    def _is_stale_read(self, clipboard, formats_token: str) -> bool:
        try:
            return clipboard.get_formats().to_string() != formats_token
        except Exception:
            return False

    def _on_text_read(self, clipboard, result, state) -> None:
        max_items, formats_token = state
        try:
            try:
                text = clipboard.read_text_finish(result)
            except Exception:
                self._set_reason("failed:read-text")
                return
            if self._is_stale_read(clipboard, formats_token):
                self._set_reason("ignored:stale-read")
                return
            if text is None or not text.strip():
                self._set_reason("ignored:empty-or-unsupported")
                return
            if len(text) > MAX_CLIPBOARD_TEXT_CHARACTERS:
                self._set_reason("ignored:text-too-large")
                return
            item = self.service.add_recent_clipboard_item(text, max_items)
            if item is None:
                self._set_reason("ignored:duplicate-or-app-write")
            else:
                self._last_captured_at = datetime.now()
                self._set_reason(f"captured:{item.type}")
        finally:
            self._finish_capture()

    def _on_texture_read(self, clipboard, result, state) -> None:
        max_items, formats_token = state
        hand_off = False
        try:
            texture = None
            try:
                texture = clipboard.read_texture_finish(result)
            except Exception:
                pass
            if texture is None:
                # Image-typed clipboards often carry text too; hand off to
                # the text read without clearing the in-flight flag.
                hand_off = True
                clipboard.read_text_async(None, self._on_text_read, max_items)
            else:
                png_bytes = _texture_png_bytes(texture)
                if png_bytes is None or not png_bytes:
                    self._set_reason("ignored:empty-or-unsupported")
                elif len(png_bytes) > MAX_CLIPBOARD_IMAGE_BYTES:
                    self._set_reason("ignored:image-too-large")
                else:
                    item = self.service.add_recent_clipboard_image(png_bytes, max_items)
                    if item is None:
                        self._set_reason("ignored:duplicate-or-app-write")
                    else:
                        self._last_captured_at = datetime.now()
                        self._set_reason("captured:Image")
        except Exception:
            self._set_reason("failed:read-or-save")
        finally:
            if not hand_off:
                self._finish_capture()

    # ---- diagnostics ------------------------------------------------------------

    def _set_reason(self, reason: str) -> None:
        self._last_reason = reason
        self._last_reason_at = datetime.now()
        self._log_state(reason)
        if self.on_diagnostics_changed is not None:
            try:
                self.on_diagnostics_changed()
            except Exception:
                pass

    def _log_state(self, state: str) -> None:
        if self._last_state_log == state:
            return
        self._last_state_log = state
        self.log(f"[QuickCaptureClipboard] state {state}")

    def status_text(self) -> str:
        return recent_status_text(self.settings)


def recent_status_text(settings_service) -> str:
    settings = settings_service.settings.quickCapture
    feature_states = settings_service.layout.featureWidgetEnabledStates or {}
    if not (feature_states.get("QuickCapture", False) and settings.quickCaptureClipboardEnabled):
        return t("QuickCapture.RecentStatus.Off")
    if settings.quickCaptureImageClipboardEnabled:
        return t("QuickCapture.RecentStatus.OnWithImages")
    return t("QuickCapture.RecentStatus.On")


def _texture_gtype():
    import gi

    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk

    return Gdk.Texture.__gtype__


def _texture_png_bytes(texture) -> Optional[bytes]:
    try:
        return bytes(texture.save_to_png_bytes().get_data())
    except Exception:
        return None
