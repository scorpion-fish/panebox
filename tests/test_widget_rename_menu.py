"""Widget "…" menu rename entry (0.4.3).

The title bar was already double-click-to-edit, but nothing advertised it.
The "…" menu now leads with a per-widget "Rename" section that opens the
same in-place editor; the popover unmapping must not cancel the edit.
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
    from gi.repository import Gio, GLib, Gtk  # noqa: E402

from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.services.widget_manager import WidgetManager  # noqa: E402


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def _menu_actions(menu) -> set[str]:
    """Every action target reachable in a Gio.Menu, incl. sections/submenus."""
    actions: set[str] = set()
    for i in range(menu.get_n_items()):
        action = menu.get_item_attribute_value(i, "action", None)
        if action is not None and action.get_string():
            actions.add(action.get_string())
        for link in (Gio.MENU_LINK_SUBMENU, Gio.MENU_LINK_SECTION):
            nested = menu.get_item_link(i, link)
            if nested is not None:
                actions |= _menu_actions(nested)
    return actions


def _manager(tmp_path):
    settings = SettingsService(config_root=tmp_path / "config")
    settings.settings.fileWidget.defaultManagedStorageRootPath = str(tmp_path / "storage")
    return settings, WidgetManager(settings)


def _run(phases, timeout_ms: int = 4000) -> None:
    loop = GLib.MainLoop()
    failures: list[Exception] = []

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


def test_menu_rename_edits_title_in_place(tmp_path):
    settings, manager = _manager(tmp_path)
    config = manager.create_file_widget(name="My Desktop")
    runtime = manager.runtimes[config.id]
    shell = runtime.shell

    def open_menu():
        manager.open_widget_menu(config.id, shell.menu_button)
        # Assert the menu MODEL offers rename (the popover is parented to the
        # button), then trigger it exactly as the menu item would.
        popover = next(w for w in _descendants(shell.menu_button) if isinstance(w, Gtk.PopoverMenu))
        actions = _menu_actions(popover.get_menu_model())
        assert "wg.rename" in actions, f"… menu actions: {actions}"
        shell.menu_button.activate_action("wg.rename")

    def still_editing_after_popover_closed():
        # The popover's focus restore must not commit the empty edit away.
        assert shell._editing is True, "editor was cancelled immediately"
        entry = shell._edit_entry
        assert entry is not None
        entry.set_text("工作桌面")
        shell.commit_title_edit()

    def renamed():
        assert shell.get_title() == "工作桌面"
        assert runtime.window.get_title() == "工作桌面"
        stored = next(w for w in settings.layout.widgets if w.id == config.id)
        assert stored.name == "工作桌面"
        assert stored.isDefaultTitle is False

    _run(
        [
            (100, open_menu),
            (600, still_editing_after_popover_closed),
            (100, renamed),
        ]
    )
    manager.shutdown()
