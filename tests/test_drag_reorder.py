"""Drag-to-reorder inside the file widget (0.5.1).

Controller semantics: the first drag switches the widget to manual sort with
the on-screen order as the baseline; dragged paths land consecutively at the
target index, everything else shifts after them; below the root reorder is
refused (the manual order only persists there). The GUI test drives a REAL
XTest press-move-release drag on :99 and asserts the icons actually move.
"""

from __future__ import annotations

import os
import subprocess

from panebox.constants import SortMode
from panebox.models.widget_config import WidgetConfig
from panebox.services.file_widget_controller import FileWidgetController

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

pytestmark_gui = pytest.mark.skipif(not GUI, reason=f"Xvfb {SMOKE_DISPLAY} not available")

if GUI:
    import ctypes  # noqa: E402

    import gi  # noqa: E402

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk  # noqa: E402


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


def _controller(tmp_path, names=("a.txt", "b.txt", "c.txt")):
    folder = tmp_path / "folder"
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_text(name)
    config = WidgetConfig(name="Files", widgetKind="File", managedFolderName="folder")
    config.mappedFolderPath = str(folder)
    controller = FileWidgetController(config, storage_root=str(tmp_path), watcher_factory=_NullWatcher)
    controller.start()
    return controller, folder


def _names(controller) -> list[str]:
    return [e.name for e in controller.entries]


def test_first_drag_switches_to_manual_and_inserts_before_target(tmp_path):
    controller, folder = _controller(tmp_path)
    assert _names(controller) == ["a.txt", "b.txt", "c.txt"]

    # Drag c onto a's left half: insert at 0, the rest shifts after.
    assert controller.reorder([str(folder / "c.txt")], 0) is True
    assert controller.config.sortMode == SortMode.MANUAL
    assert _names(controller) == ["c.txt", "a.txt", "b.txt"]
    # Persisted for the next boot.
    assert [i.path for i in controller.config.items] == [
        str(folder / "c.txt"),
        str(folder / "a.txt"),
        str(folder / "b.txt"),
    ]


def test_multi_drag_moves_the_group_consecutively(tmp_path):
    controller, folder = _controller(tmp_path)

    # Drag a+b to the end (target index clamps to the list length).
    assert controller.reorder([str(folder / "a.txt"), str(folder / "b.txt")], 99) is True
    assert _names(controller) == ["c.txt", "a.txt", "b.txt"]

    # Drag c to index 1 (after a): [a, c, b].
    assert controller.reorder([str(folder / "c.txt")], 1) is True
    assert _names(controller) == ["a.txt", "c.txt", "b.txt"]


def test_reorder_refused_below_root_and_for_foreign_paths(tmp_path):
    controller, folder = _controller(tmp_path)
    sub = folder / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("x")
    assert controller.navigate(str(sub)) is True
    assert controller.is_at_root is False
    assert controller.reorder([str(sub / "inner.txt")], 0) is False

    root_controller, _root_folder = _controller(tmp_path / "other")
    assert root_controller.reorder([str(folder / "a.txt")], 0) is False  # not our item


@pytestmark_gui
def test_real_drag_reorders_icons(tmp_path):
    """Real XTest drag on :99: press on c.txt, drag onto a.txt, release.
    Expected: c lands BEFORE a — [c, a, b] — and the widget flips to manual
    sort. Also snapshots the drop-slot marker while hovering."""
    libX11 = ctypes.CDLL("libX11.so.6")
    libXtst = ctypes.CDLL("libXtst.so.6")
    libX11.XOpenDisplay.restype = ctypes.c_void_p
    libXtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    libXtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_bool, ctypes.c_ulong]
    xdisplay = libX11.XOpenDisplay(SMOKE_DISPLAY.encode())
    assert xdisplay, "cannot open :99 for XTest"

    from panebox.services.settings_service import SettingsService
    from panebox.views.file_widget import FileSurface

    settings = SettingsService(config_root=tmp_path / "config")
    controller, _folder = _controller(tmp_path)
    surface = FileSurface(controller, icon_size=30, settings_service=settings)
    window = Gtk.Window(visible=True)
    window.set_default_size(420, 300)
    window.set_child(surface)
    window.present()

    loop = GLib.MainLoop()
    result: dict = {}

    def abs_center(child) -> tuple[int, int]:
        translated = child.translate_coordinates(window, 0, 0)
        assert translated is not None, "tile not allocated"
        xid = window.get_surface().get_xid()
        root_x, root_y = ctypes.c_int(), ctypes.c_int()
        child_win = ctypes.c_void_p()
        ok = libX11.XTranslateCoordinates(
            xdisplay,
            xid,
            libX11.XDefaultRootWindow(xdisplay),
            0,
            0,
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(child_win),
        )
        assert ok, "XTranslateCoordinates failed"
        alloc = child.get_allocation()
        return (
            root_x.value + int(translated[0]) + alloc.width // 2,
            root_y.value + int(translated[1]) + alloc.height // 2,
        )

    def motion(x: int, y: int) -> bool:
        libXtst.XTestFakeMotionEvent(xdisplay, -1, x, y, 0)
        libX11.XFlush(xdisplay)
        return False

    def button(down: bool) -> bool:
        libXtst.XTestFakeButtonEvent(xdisplay, 1, down, 0)
        libX11.XFlush(xdisplay)
        return False

    def snapshot(key: str) -> bool:
        result[key] = {
            "order": [c.entry.name for c in surface._flow_children()],
            "sort": controller.config.sortMode,
            "markers": sum(
                1 for c in surface._flow_children() if c.has_css_class("drop-before") or c.has_css_class("drop-after")
            ),
        }
        return False

    def finish() -> bool:
        loop.quit()
        return False

    def run_drag() -> bool:
        children = list(surface._flow_children())
        by_name = {c.entry.name: c for c in children}
        a_x, a_y = abs_center(by_name["a.txt"])
        c_x, c_y = abs_center(by_name["c.txt"])
        steps = 12
        GLib.timeout_add(0, lambda: motion(c_x - 25, c_y))
        GLib.timeout_add(200, lambda: motion(c_x, c_y))
        GLib.timeout_add(260, lambda: button(True))  # press on c
        for i in range(1, steps + 1):  # glide toward a's center
            t = 300 + i * 40
            x = c_x + (a_x - c_x) * i // steps
            y = c_y + (a_y - c_y) * i // steps
            GLib.timeout_add(t, lambda x=x, y=y: motion(x, y))
        GLib.timeout_add(900, lambda: snapshot("hover"))
        GLib.timeout_add(1000, lambda: button(False))  # release over a
        GLib.timeout_add(1700, lambda: snapshot("after"))
        GLib.timeout_add(1800, finish)
        return False

    GLib.timeout_add(400, run_drag)
    GLib.timeout_add(9000, loop.quit)
    loop.run()

    window.destroy()

    hover = result.get("hover")
    assert hover, f"no hover snapshot: {result}"
    assert hover["order"] == ["a.txt", "b.txt", "c.txt"], f"order must not change before release: {hover}"
    assert hover["markers"] == 1, f"exactly one drop-slot marker while hovering: {hover}"

    after = result.get("after")
    assert after, f"no post-drop snapshot: {result}"
    assert after["order"] == ["c.txt", "a.txt", "b.txt"], f"drag must move c before a: {after}"
    assert after["sort"] == SortMode.MANUAL, f"drag must switch to manual sort: {after}"
