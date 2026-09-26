"""Organize-desktop window under Xvfb: preview cards, execute, result, undo."""

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

from types import SimpleNamespace  # noqa: E402

from panebox.services.organize_service import OrganizeService  # noqa: E402
from panebox.services.organize_store import HistoryStore, RecoveryStore  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402


def pump(ms: int = 60) -> None:
    if not GUI:
        return
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def _children(box):
    child = box.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _write(path: Path, content: bytes = b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture()
def env(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    storage = tmp_path / "storage"
    config = tmp_path / "config"
    config.mkdir()

    service = SettingsService(config_root=config)
    service.load()
    service.settings.fileWidget.defaultManagedStorageRootPath = str(storage)
    service.save_now()

    organize = OrganizeService(
        service,
        history_store=HistoryStore(tmp_path / "history.json"),
        recovery_store=RecoveryStore(tmp_path / "journal.json"),
    )
    organize._desktop_path = lambda: str(desktop)  # test desktop, not the real one
    return SimpleNamespace(
        desktop=desktop,
        storage=storage,
        config=config,
        service=service,
        organize=organize,
    )


@pytest.fixture()
def window(env):
    from panebox.views.organize_window import OrganizeWindow

    win = OrganizeWindow(None, env.organize)
    win.present()
    pump()
    yield win
    win.destroy()
    pump(20)


def test_preview_shows_targets_and_retained(env, window):
    for name in ("a.pdf", "b.docx", "c.png", "d.png", "note.txt"):
        _write(env.desktop / name)
    (env.desktop / "subfolder").mkdir()
    window._recover_and_scan()

    assert window._stack.get_visible_child_name() == "preview"
    headline = window._headline.get_label()
    assert "5" in headline  # 5 files can be organized
    # one card per target bucket + retained expander for the folder
    cards = list(_children(window._cards))
    assert cards, "target cards rendered"

    opt_in_rows = [c for c in _children(window._retained_box) if isinstance(c, Gtk.CheckButton)]
    assert opt_in_rows and "subfolder" in opt_in_rows[0].get_label()

    # opt the folder in: the plan rebuilds with it as an eligible item
    opt_in_rows[0].set_active(True)
    pump()
    assert "6" in window._headline.get_label()
    assert any(i.is_directory for t in window._plan.targets for i in t.items)


def test_execute_moves_files_and_undo_restores(env, window):
    for name in ("a.pdf", "b.docx", "c.png", "d.png", "e.png"):
        _write(env.desktop / name)
    window._recover_and_scan()

    window._execute_button.emit("clicked")
    pump()

    assert window._stack.get_visible_child_name() == "result"
    assert os.listdir(env.desktop) == []
    assert os.path.isfile(env.storage / "Documents" / "a.pdf")
    assert os.path.isfile(env.storage / "Images" / "c.png")
    # widgets + rules persisted through the real settings service
    assert {w.name for w in env.service.layout.widgets} == {"Documents", "Images"}
    assert len(env.organize.rules()) == 2

    window._undo_button.emit("clicked")
    pump()
    assert sorted(os.listdir(env.desktop)) == ["a.pdf", "b.docx", "c.png", "d.png", "e.png"]
    assert not env.organize.has_pending_recovery


def test_empty_desktop_shows_nothing_page(env, window):
    window._recover_and_scan()
    assert window._stack.get_visible_child_name() == "nothing"
