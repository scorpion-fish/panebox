"""Fixes for the live GNOME/XWayland session:

1. Pointer-driven move/resize (gesture deltas feed back on themselves after
   an XMoveWindow, oscillating the window to half the drag distance).
2. Percent-encoded file URIs from real file-manager drags (a plain
   removeprefix left %20/%xx escapes in the path, so every drop was
   silently discarded).
3. The "…" widget menu carries the PaneBox app entries (no tray icon on
   GNOME/Wayland — this is the primary entry point).
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
from urllib.parse import quote

SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


GUI = _display_reachable(SMOKE_DISPLAY) and not os.environ.get("PANEBOX_SKIP_GUI")
if GUI:
    os.environ["DISPLAY"] = SMOKE_DISPLAY
    os.environ.setdefault("GDK_BACKEND", "x11")
    os.environ.setdefault("GSK_RENDERER", "cairo")
    os.environ.setdefault("NO_AT_BRIDGE", "1")

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not GUI, reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)")

if GUI:
    import gi  # noqa: E402

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # noqa: E402

from panebox.models.widget_config import WidgetConfig  # noqa: E402
from panebox.platform.move_resize import MoveResizeController  # noqa: E402
from panebox.services.file_widget_controller import (  # noqa: E402
    DropIntent,
    FileWidgetController,
    _uri_to_path,
)
from panebox.services.widget_manager_groups import (  # noqa: E402
    WidgetManagerGroupsMixin,
)


# ---- URI decoding ----------------------------------------------------------------------


def test_uri_to_path_decodes_percent_escapes():
    assert _uri_to_path("file:///home/u/my%20file.txt") == "/home/u/my file.txt"
    assert _uri_to_path("file:///home/u/%E6%A1%8C%E9%9D%A2") == "/home/u/桌面"
    assert _uri_to_path("file:///plain/path.txt") == "/plain/path.txt"
    assert _uri_to_path("/already/a/path") == "/already/a/path"
    assert _uri_to_path("") == ""


class _NullWatcher:
    def __init__(self, _path, _callback):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def pause(self):
        pass

    def resume(self):
        pass


def _controller(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    config = WidgetConfig(name="Files", widgetKind="File", managedFolderName="folder")
    config.mappedFolderPath = str(folder)
    return FileWidgetController(config, storage_root=str(tmp_path), watcher_factory=_NullWatcher)


def test_handle_drop_accepts_percent_encoded_uris(tmp_path):
    controller = _controller(tmp_path)
    source = tmp_path / "my file.txt"
    source.write_text("hello", encoding="utf-8")
    other = tmp_path / "桌面 图片.png"
    other.write_bytes(b"png")

    report = controller.handle_drop(
        [f"file://{quote(str(source))}", f"file://{quote(str(other))}"],
        DropIntent.COPY,
    )

    assert len(report.completed) == 2
    assert (tmp_path / "folder" / "my file.txt").exists()
    assert (tmp_path / "folder" / "桌面 图片.png").exists()
    controller.dispose()


# ---- pointer-driven move ---------------------------------------------------------------


class _FakeWindow:
    def __init__(self):
        self.geometry = (100, 100, 300, 400)
        self.moves: list[tuple[int, int]] = []

    def window_geometry(self):
        return self.geometry

    def move_only(self, x, y):
        self.moves.append((int(x), int(y)))
        self.geometry = (int(x), int(y), self.geometry[2], self.geometry[3])

    def resize_to(self, x, y, w, h):
        self.geometry = (int(x), int(y), int(w), int(h))

    def get_scale_factor(self):
        return 1

    def get_width(self):
        return self.geometry[2]

    def get_height(self):
        return self.geometry[3]


def _mover(window):
    return MoveResizeController(
        window,
        snap_enabled=lambda: False,
        snap_spacing=lambda: 0,
        work_area=lambda: None,
        snap_targets=lambda: [],
    )


def test_move_follows_server_pointer_not_gesture_deltas():
    window = _FakeWindow()
    controller = _mover(window)
    controller._pointer_root = lambda: (400, 300)  # pointer at grab time
    controller._on_move_begin(None, 5.0, 5.0)
    assert controller._move_grab_offset == (-300, -200)

    controller._pointer_root = lambda: (460, 330)
    controller._on_move_update(None, 9_999_999.0, -42.0)  # deltas are ignored
    assert window.moves[-1] == (160, 130)  # 100 + pointer delta (60, 30)

    controller._pointer_root = lambda: (460, 330)
    controller._on_move_update(None, 0.0, 0.0)  # stable pointer → stable window
    assert window.moves[-1] == (160, 130)


def test_move_falls_back_to_deltas_without_pointer():
    window = _FakeWindow()
    controller = _mover(window)
    controller._pointer_root = lambda: None  # headless
    controller._on_move_begin(None, 10.0, 10.0)
    controller._on_move_update(None, 25.0, 15.0)
    assert window.moves[-1] == (115, 105)  # origin (90,90) + delta


def test_resize_drives_edges_from_server_pointer():
    window = _FakeWindow()
    controller = _mover(window)
    controller._pointer_root = lambda: (200, 250)
    controller._on_resize_begin(None, float(300 - 1), float(400 - 1))  # bottom-right
    controller._pointer_root = lambda: (240, 290)
    controller._on_resize_update(None, 0.0, 0.0)
    x, y, w, h = controller.window.window_geometry()
    assert (w, h) == (340, 440)  # start size + pointer delta (40, 40)
    assert controller._resize_edge == "bottom-right"


# ---- "…" menu PaneBox entries -----------------------------------------------------------


class _ShellStub:
    def __init__(self):
        self.menu_available = None
        self.group_strip_slot = SimpleNamespace(get_first_child=lambda: None)

    def set_menu_available(self, value):
        self.menu_available = value

    def set_group_strip(self, _widget):
        pass


class _StubManager(WidgetManagerGroupsMixin):
    """Just enough surface for open_widget_menu's ungrouped path."""

    def __init__(self):
        self.application = None
        self.shell_stub = _ShellStub()
        self.runtimes = {
            "w1": SimpleNamespace(
                config=SimpleNamespace(id="w1"),
                shell=self.shell_stub,
            )
        }

    def group_of(self, _widget_id):
        return None

    def join_targets(self, _widget_id):
        return []

    def _member_runtime(self, _widget_id):
        return SimpleNamespace(shell=self.shell_stub)


def test_ungrouped_widget_keeps_menu_button_visible():
    """_refresh_group_strips must not hide the "…" button for ungrouped
    widgets — on GNOME/Wayland (no tray) it is the only app entry point."""
    stub = _StubManager()
    stub.shell_stub.menu_available = False
    stub._refresh_group_strips()
    assert stub.shell_stub.menu_available is True


def test_widget_menu_offers_app_entries_without_tray():
    from panebox import i18n

    i18n.set_language("en-US")
    stub = _StubManager()
    button = Gtk.Button(visible=True)
    window = Gtk.Window(visible=True)
    window.set_child(button)
    window.present()

    stub.open_widget_menu("w1", button)

    assert stub.shell_stub.menu_available is True  # was False when ungrouped
    popover = button.get_first_child()  # popover was parented to the button
    assert isinstance(popover, Gtk.PopoverMenu)
    labels: list[str] = []

    def walk(model):
        for i in range(model.get_n_items()):
            value = model.get_item_attribute_value(i, "label", None)
            if value is not None:
                labels.append(value.get_string())
            for link in ("section", "submenu"):
                sub = model.get_item_link(i, link)
                if sub is not None:
                    walk(sub)

    walk(popover.get_menu_model())
    assert any("New Widget" in label for label in labels)
    assert any("Settings" in label for label in labels)
    window.destroy()


# ---- tile layout (single file must not stretch to full height) -------------------------


def test_file_tile_child_is_top_aligned(tmp_path):
    """A FlowBox cell fills the box vertically; without valign=START a lone
    file stretches into one giant full-height column (invisible content)."""
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # noqa: E402

    from panebox.views.file_widget import FileSurface  # noqa: E402

    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "a.txt").write_text("x", encoding="utf-8")
    config = WidgetConfig(name="T", widgetKind="File", managedFolderName="f")
    config.mappedFolderPath = str(folder)
    controller = FileWidgetController(config, storage_root=str(tmp_path), watcher_factory=_NullWatcher)
    surface = FileSurface(controller)
    window = Gtk.Window(visible=True)
    window.set_default_size(280, 400)
    window.set_child(surface)
    window.present()

    child = surface.flow.get_child_at_index(0)
    assert child is not None
    assert child.get_valign() == Gtk.Align.START
    box = child.get_child()
    image = box.get_first_child()
    assert image.get_halign() == Gtk.Align.CENTER
    assert image.get_valign() == Gtk.Align.START
    window.destroy()
    controller.dispose()


# ---- rotating application log ----------------------------------------------------------


def test_setup_logging_writes_rotating_file(tmp_path, monkeypatch):
    import importlib
    import logging

    monkeypatch.setenv("PANEBOX_DATA_ROOT", str(tmp_path))
    from panebox import app_log, constants

    importlib.reload(constants)
    importlib.reload(app_log)
    try:
        log_file = app_log.setup_logging()
        assert log_file == constants.LOG_FILE
        app_log.get_logger("test").info("hello-log-line")
        logging.getLogger("panebox.test").handlers.clear()
        for handler in logging.getLogger("panebox").handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
        assert "hello-log-line" in text
        assert "session start" in text
    finally:
        for handler in logging.getLogger("panebox").handlers[:]:
            handler.close()
            logging.getLogger("panebox").removeHandler(handler)
