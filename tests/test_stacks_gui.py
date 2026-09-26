"""File-stack surface tests under Xvfb: controller projection + FileSurface
tiles (auto stacks, inline expand, disable/restore, manual stacks, popover)."""

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
    from gi.repository import GLib, Gtk  # noqa: E402


def pump(ms: int = 60) -> None:
    """Run the main loop for `ms` so realize/map/idle callbacks settle."""
    if not GUI:
        return
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


class _NullWatcher:
    """No inotify: tests drive refresh() explicitly."""

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


from panebox import i18n  # noqa: E402
from panebox.models.widget_config import WidgetConfig  # noqa: E402
from panebox.services.file_widget_controller import FileWidgetController  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.views.file_widget import FileSurface  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    i18n.set_language("en-US")
    settings = SettingsService(config_root=tmp_path / "config")
    settings.load()
    settings.settings.fileWidget.fileStackAutoStacking = True
    folder = tmp_path / "folder"
    folder.mkdir()
    config = WidgetConfig(name="Files", widgetKind="File", managedFolderName="folder")
    config.mappedFolderPath = str(folder)
    controller = FileWidgetController(
        config,
        storage_root=str(tmp_path),
        settings_service=settings,
        watcher_factory=_NullWatcher,
    )
    surface = FileSurface(controller, settings_service=settings)
    # Popovers pop up only inside a mapped toplevel.
    window = Gtk.Window()
    window.set_default_size(480, 420)
    window.set_child(surface)
    window.present()
    pump()
    yield controller, surface, folder, settings
    controller.dispose()
    pump()
    window.set_child(None)
    window.destroy()
    pump()
    i18n.set_language(None)


def _mk(folder: Path, name: str):
    (folder / name).write_text("x")


def _stack_tiles(surface) -> list:
    out = []
    child = surface.flow.get_first_child()
    while child is not None:
        if getattr(child, "stack_key", None):
            out.append(child)
        child = child.get_next_sibling()
    return out


def _tile_labels(surface) -> list:
    labels = []
    child = surface.flow.get_first_child()
    while child is not None:
        box = child.get_child()
        label = box.get_first_child() if box is not None else None
        # stack tiles: overlay then label; entry tiles: image then label
        while label is not None and not isinstance(label, Gtk.Label):
            label = label.get_next_sibling()
        labels.append(label.get_text() if label is not None else "")
        child = child.get_next_sibling()
    return labels


def test_auto_stack_tile_appears_and_expands_inline(env):
    controller, surface, folder, _settings = env
    for index in range(3):
        _mk(folder, f"doc{index}.txt")
    _mk(folder, "photo.png")
    controller.refresh()
    surface.rebuild()

    tiles = _stack_tiles(surface)
    assert len(tiles) == 1
    assert tiles[0].stack_key == "Documents"
    assert _tile_labels(surface) == ["Documents", "photo.png"]

    # Inline open mode (default): activation expands members below the tile.
    surface._on_activate_flow(surface.flow, tiles[0])
    surface.rebuild()
    controller._invalidate_projection(notify=False)
    surface.rebuild()
    visible = [getattr(c, "stack_key", None) or c.entry.name for c in list(surface._flow_children())]
    assert visible == ["Documents", "doc0.txt", "doc1.txt", "doc2.txt", "photo.png"]


def test_disable_group_and_restore(env):
    controller, surface, folder, _settings = env
    for index in range(3):
        _mk(folder, f"doc{index}.txt")
    controller.refresh()
    surface.rebuild()
    assert len(_stack_tiles(surface)) == 1

    assert controller.disable_stack_group("Documents")
    controller._invalidate_projection(notify=False)
    surface.rebuild()
    assert _stack_tiles(surface) == []
    assert controller.has_disabled_groups()

    assert controller.restore_disabled_groups()
    controller._invalidate_projection(notify=False)
    surface.rebuild()
    assert len(_stack_tiles(surface)) == 1


def test_manual_stack_from_selection_and_dissolve(env):
    controller, surface, folder, _settings = env
    _mk(folder, "a.txt")
    _mk(folder, "b.png")
    controller.refresh()
    surface.rebuild()
    assert _stack_tiles(surface) == []  # below threshold, mixed kinds

    assert controller.create_manual_stack_from([str(folder / "a.txt"), str(folder / "b.png")])
    controller._invalidate_projection(notify=False)
    surface.rebuild()
    tiles = _stack_tiles(surface)
    assert len(tiles) == 1 and tiles[0].stack_key.startswith("Manual:")
    assert tiles[0].stack_key in controller.stack_projection().units[0].order_key

    assert controller.dissolve_stack(tiles[0].stack_key)
    controller._invalidate_projection(notify=False)
    surface.rebuild()
    assert _stack_tiles(surface) == []


def test_per_widget_override_disables_stacks(env):
    controller, surface, folder, _settings = env
    for index in range(3):
        _mk(folder, f"doc{index}.txt")
    controller.refresh()
    surface.rebuild()
    assert len(_stack_tiles(surface)) == 1

    controller.set_widget_stacks_enabled(False)
    surface.rebuild()
    assert _stack_tiles(surface) == []
    assert len(list(surface._flow_children())) == 3

    controller.follow_global_stack_defaults()
    surface.rebuild()
    assert len(_stack_tiles(surface)) == 1


def test_popover_open_mode_lists_members(env):
    controller, surface, folder, settings = env
    for index in range(4):
        _mk(folder, f"doc{index}.txt")
    settings.settings.fileWidget.fileStackOpenMode = "Popover"
    controller.refresh()
    surface.rebuild()
    tiles = _stack_tiles(surface)
    assert len(tiles) == 1

    surface._activate_stack(controller.stack_unit("Documents"), tiles[0])
    assert surface._stack_popover is not None  # popover mode → surface popover
    pump()
    surface._stack_popover.popdown()
    pump()
    surface._stack_popover = None
    # Inline mode still works when the global default flips back.
    settings.settings.fileWidget.fileStackOpenMode = "Inline"
    controller.invalidate_stack_projection()
    assert controller.toggle_stack("Documents")
    controller._invalidate_projection(notify=False)
    surface.rebuild()
    visible = [getattr(c, "stack_key", None) or c.entry.name for c in list(surface._flow_children())]
    assert visible[0] == "Documents" and len(visible) == 5


def test_date_modified_grouping(env):
    controller, surface, folder, settings = env
    import time

    now = time.time()
    (folder / "new.txt").write_text("n")
    os.utime(folder / "new.txt", (now, now))
    old = now - 60 * 60 * 24 * 90
    for index in range(3):  # 3 old files → Earlier bucket reaches the threshold
        (folder / f"old{index}.txt").write_text("o")
        os.utime(folder / f"old{index}.txt", (old, old))
    settings.settings.fileWidget.fileStackGroupBy = "DateModified"
    controller.refresh()
    surface.rebuild()
    keys = [tile.stack_key for tile in _stack_tiles(surface)]
    assert "Earlier" in keys
    assert keys.count("Today") == 0  # single new file → no Today stack
