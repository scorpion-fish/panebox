"""Closing the last window must end the process (0.4.2 behavior).

Reported against 0.4.1: closing every widget (✕) left a headless background
service. The driver boots the real application as an isolated subprocess on
the smoke display, closes windows with genuine XTest clicks, and asserts the
process exits only after the last one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")
DRIVER = Path(__file__).parent / "drivers" / "exit_on_close_driver.py"
ROOT = Path(__file__).resolve().parent.parent


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


GUI = _display_reachable(SMOKE_DISPLAY) and not os.environ.get("PANEBOX_SKIP_GUI")

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not GUI, reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)")


def test_app_exits_after_last_window_closed(tmp_path):
    env = dict(
        os.environ,
        DISPLAY=SMOKE_DISPLAY,
        HOME=str(tmp_path / "home"),
        PANEBOX_CONFIG_ROOT=str(tmp_path / "config"),
        PANEBOX_DATA_ROOT=str(tmp_path / "data"),
    )
    # A live instance (user's desktop) owns org.panebox.PaneBox on the session
    # bus; without a bus the app registers locally and stays self-contained.
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    (tmp_path / "home").mkdir()
    process = subprocess.Popen(
        ["python3", "-u", str(DRIVER)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        out, _ = process.communicate(timeout=40)
    except subprocess.TimeoutExpired:
        process.kill()
        out, _ = process.communicate()
        raise AssertionError(f"driver hung (app never exited after last close):\n{out}")
    assert "CLICKED-FILE-CLOSE" in out, out
    assert "SURVIVED-ONE-WINDOW" in out, f"process died with a window still open:\n{out}"
    assert "CLICKED-LAST-CLOSE" in out, out
    assert "EXIT-ON-EMPTY: PASS" in out, out
    assert process.returncode == 0, f"driver exit {process.returncode}:\n{out}"
