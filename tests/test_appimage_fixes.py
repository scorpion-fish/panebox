"""Fixes reported against the 0.4.0 AppImage after 1-2h of real use:

1. Map-folder scan crashed on feature runtimes (controller=None) — the
   tray/…"新建文件夹映射" chooser silently did nothing.
2. Initial setup minted a fresh default folder on every re-arm instead of
   reusing the one already on disk (我的桌面 / My Desktop siblings).
3. Widgets created at runtime ignored the configured opacity/density — the
   boot-restored widgets kept the (dimmed) setting while new ones rendered
   at full opacity, splitting them across "layers".
4. Closing the settings window left a destroyed window referenced by the
   application — reopening presented a zombie that ignored its close button.
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
    import gi  # noqa: E402

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # noqa: E402

from panebox.i18n import default_desktop_names  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.services.widget_manager import FileWidgetPathConflict, WidgetManager  # noqa: E402
from panebox.views.settings_window import SettingsHooks, SettingsWindow  # noqa: E402


def _manager(tmp_path, storage: str):
    settings = SettingsService(config_root=tmp_path / "config")
    settings.settings.fileWidget.defaultManagedStorageRootPath = storage
    return settings, WidgetManager(settings)


# ---- 1: map-folder scan must skip feature runtimes --------------------------------------


def test_map_folder_succeeds_with_feature_widget_open(tmp_path):
    storage = tmp_path / "storage"
    settings, manager = _manager(tmp_path, str(storage))
    manager.create_feature_widget("QuickCapture")  # controller=None runtime
    target = tmp_path / "documents"
    target.mkdir()
    config = manager.create_file_widget(mapped_folder=str(target))
    assert os.path.realpath(config.mappedFolderPath) == os.path.realpath(str(target))
    # mapping the same folder again is still a proper conflict
    with pytest.raises(FileWidgetPathConflict) as exc:
        manager.create_file_widget(mapped_folder=str(target))
    assert exc.value.kind == "ExistingWidget"
    # …and the storage root itself is refused as before
    with pytest.raises(FileWidgetPathConflict) as exc:
        manager.create_file_widget(mapped_folder=str(storage))
    assert exc.value.kind == "StorageRoot"


# ---- 2: initial setup reuses the existing default folder ---------------------------------


def test_default_desktop_names_cover_both_locales():
    names = default_desktop_names()
    assert "我的桌面" in names
    assert "My Desktop" in names


def test_initial_setup_reuses_default_folder_across_rearms(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    default_dir = storage / "My Desktop"  # minted under an English session
    default_dir.mkdir()
    (default_dir / "keep.txt").write_text("user data")

    settings, manager = _manager(tmp_path, str(storage))
    first = manager.ensure_initial_file_widget(interactive=True)
    assert first is not None
    assert os.path.realpath(first.mappedFolderPath) == os.path.realpath(str(default_dir))
    assert (default_dir / "keep.txt").exists()  # reused, not replaced
    assert os.listdir(storage) == ["My Desktop"]  # no " (2)" sibling

    # User closes every widget → setup re-arms on the next evaluate: same folder.
    manager.close_widget(first.id)
    second = manager.ensure_initial_file_widget(interactive=True)
    assert second is not None
    assert os.path.realpath(second.mappedFolderPath) == os.path.realpath(str(default_dir))
    assert sorted(os.listdir(storage)) == ["My Desktop"]


# ---- 3: runtime-created widgets honor the appearance settings ----------------------------


def test_created_widget_applies_configured_opacity(tmp_path):
    storage = tmp_path / "storage"
    settings, manager = _manager(tmp_path, str(storage))
    settings.settings.widgetShell.widgetOpacity = 0.6
    config = manager.create_feature_widget("Todo")
    runtime = manager.runtimes[config.id]
    assert runtime.window.get_opacity() == pytest.approx(0.6)

    settings.settings.widgetShell.widgetOpacity = 0.9
    manager.apply_appearance()
    # X11 stores _NET_WM_WINDOW_OPACITY at 8-bit precision (230/255 ≈ 0.9020).
    assert runtime.window.get_opacity() == pytest.approx(0.9, abs=0.005)


# ---- 4: settings window close detaches the stale reference -------------------------------


def test_settings_close_detaches_reference_and_reopens_fresh(tmp_path):
    class _App(Gtk.Application):
        settings_window = None

    app = _App(application_id="org.panebox.TestSettings")
    settings = SettingsService(config_root=tmp_path / "config")
    hooks = SettingsHooks(
        save=lambda: None,
        apply_theme=lambda _t: None,
        apply_language=lambda: None,
        apply_hotkey=lambda: None,
        apply_autostart=lambda _e: None,
        apply_appearance=lambda: None,
    )
    window = SettingsWindow(app, settings, hooks)
    app.settings_window = window
    window.present()
    window.close()  # close on a mapped window runs the close-request handler
    assert app.settings_window is None  # detached — the next open builds fresh
    assert not window.get_visible()
    # open_settings() builds a fresh window once the reference is gone
    app.settings_window = SettingsWindow(app, settings, hooks) if app.settings_window is None else None
    assert app.settings_window is not None
