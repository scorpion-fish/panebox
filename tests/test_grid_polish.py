"""Grid polish (0.4.4): desktop-like icon pitch + plain-click toggle selection.

1. Icon spacing follows the density settings (icon size × h/v scale) and
   cells keep their natural width at any file count (min_children_per_line 1).
2. A plain click on a selected icon deselects it; a double-click keeps it
   selected (activation opens it); Ctrl/Shift clicks keep multi-select.
   The real-click test drives actual XTest presses on :99.
"""

from __future__ import annotations

import os
import subprocess

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
    import ctypes  # noqa: E402

    import gi  # noqa: E402

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from panebox.models.widget_config import WidgetConfig  # noqa: E402
from panebox.services.file_widget_controller import FileWidgetController  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.views.file_widget import FileSurface  # noqa: E402


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


def _surface(tmp_path, settings: SettingsService) -> FileSurface:
    folder = tmp_path / "folder"
    folder.mkdir()
    for name in ("a.txt", "b.txt"):
        (folder / name).write_text(name)
    config = WidgetConfig(name="Files", widgetKind="File", managedFolderName="folder")
    config.mappedFolderPath = str(folder)
    controller = FileWidgetController(config, storage_root=str(tmp_path), watcher_factory=_NullWatcher)
    return FileSurface(controller, icon_size=30, settings_service=settings)


def _settings(tmp_path) -> SettingsService:
    return SettingsService(config_root=tmp_path / "config")


def test_grid_spacing_follows_settings_and_cells_keep_natural_width(tmp_path):
    settings = _settings(tmp_path)
    shell = settings.settings.widgetShell
    shell.iconSize = 30
    shell.horizontalSpacingScale = 0.40
    shell.verticalSpacingScale = 0.60
    surface = _surface(tmp_path, settings)

    assert surface.flow.get_min_children_per_line() == 1
    assert surface.flow.get_column_spacing() == 12
    assert surface.flow.get_row_spacing() == 18

    shell.horizontalSpacingScale = 0.10
    shell.verticalSpacingScale = 0.20
    surface.icon_size = 40  # what apply_appearance pushes before rebuild
    surface.rebuild()
    assert surface.flow.get_column_spacing() == 4
    assert surface.flow.get_row_spacing() == 8


def test_plain_click_toggles_and_double_click_keeps_selection(tmp_path):
    surface = _surface(tmp_path, _settings(tmp_path))
    child = next(surface._flow_children())

    surface.flow.select_child(child)  # what FlowBox does on press
    surface._handle_item_click(child, 1, 0, was_selected=True)
    assert not child.is_selected()  # second plain click: deselect

    surface._handle_item_click(child, 2, 0, was_selected=False)
    assert child.is_selected()  # double-click: stays selected, activation opens

    surface._handle_item_click(child, 1, 0, was_selected=True)
    surface._handle_item_click(child, 1, Gdk.ModifierType.CONTROL_MASK, was_selected=True)
    assert not child.is_selected()  # ctrl/shift clicks are left to the built-in


def test_real_click_toggles_selection(tmp_path):
    """Real XTest clicks on :99: click selects, click again deselects."""
    libX11 = ctypes.CDLL("libX11.so.6")
    libXtst = ctypes.CDLL("libXtst.so.6")
    libX11.XOpenDisplay.restype = ctypes.c_void_p
    libXtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    libXtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_bool, ctypes.c_ulong]
    xdisplay = libX11.XOpenDisplay(SMOKE_DISPLAY.encode())
    assert xdisplay, "cannot open :99 for XTest"

    surface = _surface(tmp_path, _settings(tmp_path))
    window = Gtk.Window(visible=True)
    window.set_default_size(400, 400)
    window.set_child(surface)
    window.present()

    child = next(surface._flow_children())
    loop = GLib.MainLoop()
    result: dict = {}

    def abs_center() -> tuple[int, int]:
        translated = child.translate_coordinates(window, 0, 0)
        assert translated is not None, "tile not allocated"
        surface_origin = window.get_surface()  # GdkX11.X11Surface on this backend
        xid = surface_origin.get_xid()
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
        assert alloc.width > 0 and alloc.height > 0, "tile not allocated"
        cx = root_x.value + int(translated[0]) + alloc.width // 2
        cy = root_y.value + int(translated[1]) + alloc.height // 2
        return cx, cy

    def fake(x: int, y: int, press: bool) -> bool:
        libXtst.XTestFakeMotionEvent(xdisplay, -1, x, y, 0)
        libXtst.XTestFakeButtonEvent(xdisplay, 1, press, 0)
        libX11.XFlush(xdisplay)
        return False  # every scheduled callback must return False

    def fake_hover(x: int, y: int) -> bool:
        libXtst.XTestFakeMotionEvent(xdisplay, -1, x, y, 0)
        libX11.XFlush(xdisplay)
        return False

    def phase1() -> bool:
        result["center"] = abs_center()
        return False

    def phase2() -> bool:  # after first click
        result["after_first"] = child.is_selected()
        return False

    def phase3() -> bool:  # after second click
        result["after_second"] = child.is_selected()
        loop.quit()
        return False

    def run_clicks() -> bool:
        cx, cy = result["center"]
        GLib.timeout_add(0, lambda: fake_hover(cx - 30, cy))  # approach from the side
        GLib.timeout_add(250, lambda: fake(cx, cy, True))
        GLib.timeout_add(310, lambda: fake(cx, cy, False))
        GLib.timeout_add(900, phase2)
        GLib.timeout_add(1000, lambda: fake_hover(cx - 30, cy))
        GLib.timeout_add(1250, lambda: fake(cx, cy, True))
        GLib.timeout_add(1310, lambda: fake(cx, cy, False))
        GLib.timeout_add(1900, phase3)
        return False

    GLib.timeout_add(300, phase1)
    GLib.timeout_add(400, run_clicks)
    GLib.timeout_add(6000, loop.quit)
    loop.run()

    window.destroy()
    assert result.get("after_first") is True, f"first click did not select: {result}"
    assert result.get("after_second") is False, f"second click did not deselect: {result}"
