"""Autostart: ~/.config/autostart/panebox.desktop (the Run-key equivalent).

The first run applies the autoStart default (on, once — autoStartDefaultApplied
guards repeat writes); later calls honor explicit toggles.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

AUTOSTART_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "panebox.desktop"

_DESKTOP_TEMPLATE = """[Desktop Entry]
Type=Application
Name=PaneBox
Comment={comment}
Exec={exec_line}
Terminal=false
Categories=Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""


def _exec_line() -> str:
    main_py = Path(__file__).resolve().parent.parent.parent / "main.py"
    if main_py.exists():
        return f"{_python()} {main_py}"
    return "panebox"


def _python() -> str:
    import sys

    return sys.executable or "python3"


def write_autostart(enabled: bool) -> bool:
    try:
        if enabled:
            AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
            from ..i18n import t

            AUTOSTART_FILE.write_text(
                _DESKTOP_TEMPLATE.format(comment=t("Tray.Tooltip"), exec_line=_exec_line()),
                encoding="utf-8",
            )
            AUTOSTART_FILE.chmod(AUTOSTART_FILE.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            AUTOSTART_FILE.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def is_autostart_enabled() -> bool:
    return AUTOSTART_FILE.exists()


def ensure_autostart_default(auto_start: bool, default_applied: bool) -> bool:
    """Returns the new value of autoStartDefaultApplied."""
    if default_applied:
        return True
    write_autostart(auto_start)
    return True
