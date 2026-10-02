"""Title bar font size / color settings (0.5.1).

set_title_style drives Pango attributes on both the title label and the
rename entry; "" color keeps the theme color. The slice round-trips through
the settings store so the choice survives a restart.
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

pytestmark = pytest.mark.skipif(not GUI, reason=f"Xvfb {SMOKE_DISPLAY} not available")

if GUI:
    import gi  # noqa: E402

    gi.require_version("Gtk", "4.0")


def test_title_style_defaults_and_persistence(tmp_path):
    from panebox.models.settings_slices import WidgetShellSettingsSlice
    from panebox.services.settings_service import SettingsService

    defaults = WidgetShellSettingsSlice()
    assert defaults.titleFontSize == 11.5
    assert defaults.titleColor == ""

    service = SettingsService(config_root=tmp_path / "config")
    shell = service.settings.widgetShell
    shell.titleFontSize = 15.5
    shell.titleColor = "#33AAFF"
    service.save_now()

    reloaded = SettingsService(config_root=tmp_path / "config")
    reloaded.load()  # the constructor alone stays at defaults
    assert reloaded.settings.widgetShell.titleFontSize == 15.5
    assert reloaded.settings.widgetShell.titleColor == "#33AAFF"


def test_set_title_style_applies_pango_attributes():
    from panebox.views.shell import WidgetShell

    shell = WidgetShell(title="Files")
    assert shell.title_label.get_attributes() is None  # theme default, no overrides

    shell.set_title_style(15.0, "#FF1122")
    attrs = shell.title_label.get_attributes()
    assert attrs is not None
    assert len(attrs.get_attributes()) == 2  # size + foreground

    # The rename entry edits with the same styling — no theme-style flash.
    shell.begin_title_edit()
    entry_attributes = shell._edit_entry.get_attributes()
    assert entry_attributes is not None and len(entry_attributes.get_attributes()) == 2
    shell.commit_title_edit()

    # Empty color keeps the theme color; the size override stays.
    shell.set_title_style(12.0, "")
    attrs = shell.title_label.get_attributes()
    assert len(attrs.get_attributes()) == 1

    # A size of 0 disables the override entirely.
    shell.set_title_style(0, "")
    assert len(shell.title_label.get_attributes().get_attributes()) == 0
