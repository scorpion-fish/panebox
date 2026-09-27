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
    from gi.repository import GLib, Gtk  # noqa: E402

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


def test_plain_click_verdicts(tmp_path):
    surface = _surface(tmp_path, _settings(tmp_path))
    opened: list[str] = []
    surface.controller.open = lambda e: opened.append(e.name)  # type: ignore[method-assign]
    first, second = list(surface._flow_children())

    # Plain click on an unselected item: it becomes THE selection.
    surface._handle_item_click(first, 1, was_selected=False)
    assert first.is_selected() and not second.is_selected()

    # Click another item: selection MOVES (A1 deselects, C2 selects).
    surface._handle_item_click(second, 1, was_selected=False)
    assert second.is_selected() and not first.is_selected()

    # Plain second click on the selected item: deselect.
    surface._handle_item_click(second, 1, was_selected=True)
    assert not second.is_selected() and not opened

    # Double-click an unselected item: it is selected and opened, once.
    surface._handle_item_click(second, 2, was_selected=False)
    assert second.is_selected() and not first.is_selected()
    assert opened == ["b.txt"]

    # Double-click inside a multi-selection: every selected item opens.
    opened.clear()
    surface.flow.select_child(first)
    surface._handle_item_click(second, 2, was_selected=True)
    assert sorted(opened) == ["a.txt", "b.txt"]


def test_real_clicks_match_desktop_semantics(tmp_path):
    """Real XTest clicks on :99, reproducing the reported bug:

    select a.txt, then quickly click b.txt (cross-tile, inside the
    double-click time — what the built-in handler misread as a double
    click, opening the range). Expected: selection moves to b.txt, nothing
    opens. Then a real double-click on b.txt: b.txt opens, once.
    """
    libX11 = ctypes.CDLL("libX11.so.6")
    libXtst = ctypes.CDLL("libXtst.so.6")
    libX11.XOpenDisplay.restype = ctypes.c_void_p
    libXtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    libXtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_bool, ctypes.c_ulong]
    xdisplay = libX11.XOpenDisplay(SMOKE_DISPLAY.encode())
    assert xdisplay, "cannot open :99 for XTest"

    surface = _surface(tmp_path, _settings(tmp_path))
    opened: list[str] = []
    surface.controller.open = lambda e: opened.append(e.name)  # type: ignore[method-assign]
    window = Gtk.Window(visible=True)
    window.set_default_size(400, 400)
    window.set_child(surface)
    window.present()

    children = list(surface._flow_children())
    first, second = children[0], children[1]
    loop = GLib.MainLoop()
    result: dict = {}

    def abs_center(child) -> tuple[int, int]:
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

    def press(x: int, y: int, down: bool) -> bool:
        # XTest presses at the CURRENT pointer position — move to the target
        # first, or the click lands wherever the last approach-motion left it.
        libXtst.XTestFakeMotionEvent(xdisplay, -1, x, y, 0)
        libXtst.XTestFakeButtonEvent(xdisplay, 1, down, 0)
        libX11.XFlush(xdisplay)
        return False  # every scheduled callback must return False

    def move(x: int, y: int) -> bool:
        libXtst.XTestFakeMotionEvent(xdisplay, -1, x, y, 0)
        libX11.XFlush(xdisplay)
        return False

    def snapshot(key: str) -> bool:
        result[key] = {
            "a": first.is_selected(),
            "b": second.is_selected(),
            "opened": list(opened),
        }
        return False

    def finish() -> bool:
        loop.quit()
        return False

    def run_clicks() -> bool:
        a1, a2 = abs_center(first)
        b1, b2 = abs_center(second)
        # single click on a.txt (press 250 / release 310)
        GLib.timeout_add(0, lambda: move(a1 - 30, a2))
        GLib.timeout_add(250, lambda: press(a1, a2, True))
        GLib.timeout_add(310, lambda: press(a1, a2, False))
        # fast cross-tile single click on b.txt 90ms later — inside the
        # double-click window: must move the selection, NOT open anything
        GLib.timeout_add(400, lambda: move(b1 - 30, b2))
        GLib.timeout_add(650, lambda: press(b1, b2, True))
        GLib.timeout_add(710, lambda: press(b1, b2, False))
        GLib.timeout_add(1300, lambda: snapshot("after_cross_tile"))
        # genuine double-click on b.txt (already selected by the click above)
        GLib.timeout_add(1600, lambda: press(b1, b2, True))
        GLib.timeout_add(1660, lambda: press(b1, b2, False))
        # between the two clicks: the toggle-off must NOT have fired — that
        # unselected→selected flash is exactly the reported flicker
        GLib.timeout_add(1750, lambda: snapshot("mid_double"))
        GLib.timeout_add(1800, lambda: press(b1, b2, True))
        GLib.timeout_add(1860, lambda: press(b1, b2, False))
        GLib.timeout_add(2500, lambda: snapshot("after_double"))
        GLib.timeout_add(2600, finish)
        return False

    GLib.timeout_add(400, run_clicks)
    GLib.timeout_add(8000, loop.quit)
    loop.run()

    window.destroy()
    cross = result.get("after_cross_tile")
    assert cross, f"no cross-tile snapshot: {result}"
    assert cross["b"] is True and cross["a"] is False, f"selection must move to b.txt: {cross}"
    assert cross["opened"] == [], f"a fast cross-tile click must not open anything: {cross}"

    dbl = result.get("after_double")
    assert dbl, f"no double-click snapshot: {result}"
    assert dbl["opened"] == ["b.txt"], f"double-click must open b.txt once: {dbl}"
    assert dbl["b"] is True and dbl["a"] is False, f"double-click leaves b.txt selected: {dbl}"

    mid = result.get("mid_double")
    assert mid, f"no mid-double-click snapshot: {result}"
    assert mid["b"] is True, f"selection must not flash off between the two clicks: {mid}"
