"""GUI smoke under Xvfb: boot, EWMH desktop-layer state, graceful shutdown.

Runs the real `main.py` as a subprocess on display :99 (started by the test
rig). Skips when :99 is not reachable, so the unit suite stays runnable
anywhere.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    _display_reachable(SMOKE_DISPLAY) is False or os.environ.get("PANEBOX_SKIP_GUI"),
    reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)",
)


class App:
    """Subprocess wrapper: stderr drains on a thread so polls never block."""

    def __init__(self, config_root: Path, data_root: Path):
        env = dict(
            os.environ,
            DISPLAY=SMOKE_DISPLAY,
            GDK_BACKEND="x11",
            GSK_RENDERER="cairo",
            NO_AT_BRIDGE="1",
            PANEBOX_TEST_MARKERS="1",
            PANEBOX_CONFIG_ROOT=str(config_root),
            PANEBOX_DATA_ROOT=str(data_root),
        )
        self.process = subprocess.Popen(
            ["python3", str(ROOT / "main.py")],
            cwd=ROOT,
            env=env,
            stderr=subprocess.STDOUT,  # merge: XID markers go to stdout
            stdout=subprocess.PIPE,
            text=True,
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line)

    @property
    def log(self) -> str:
        return "".join(self.lines)

    def wait_for(self, needle: str, timeout: float = 25.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"app exited early ({self.process.returncode}):\n{self.log}")
            if any(needle in line for line in self.lines):
                # give follow-up logs (xprop-visible state) a moment
                time.sleep(0.5)
                return
            time.sleep(0.1)
        raise AssertionError(f"timeout waiting for {needle!r}; log so far:\n{self.log}")

    def stop(self, timeout: float = 10.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise AssertionError(f"app ignored SIGTERM for {timeout}s:\n{self.log}")


def test_boot_ewmh_shutdown_roundtrip(tmp_path):
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    config_root.mkdir()
    data_root.mkdir()

    app = App(config_root, data_root)
    try:
        app.wait_for("DESKBOX-XID")
        app.wait_for("[Hotkey] registered")
        assert "Traceback" not in app.log

        marker_line = next(line for line in app.lines if "DESKBOX-XID" in line)
        xid = marker_line.split()[3]  # "DESKBOX-XID <title words...> <hex-xid>"
        props = subprocess.run(
            [
                "xprop",
                "-display",
                SMOKE_DISPLAY,
                "-id",
                xid,
                "_NET_WM_WINDOW_TYPE",
                "_NET_WM_STATE",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        assert "_NET_WM_WINDOW_TYPE_DESKTOP" in props, props
        assert "_NET_WM_STATE_STICKY" in props, props
        assert "_NET_WM_STATE_SKIP_TASKBAR" in props, props
    finally:
        app.stop()

    # Graceful shutdown must have flushed both stores.
    settings = json.loads((config_root / "settings.json").read_text())
    assert settings["core"]["language"]
    layout_files = [p for p in config_root.glob("widget-layout*.json") if not p.name.endswith(".bak")]
    assert len(layout_files) == 1
    layout = json.loads(layout_files[0].read_text())
    widgets = layout["layout"]["widgets"]
    assert len(widgets) == 1, "first-run widget must persist"
    assert widgets[0]["widgetKind"] == "File"


def test_second_boot_restores_widget(tmp_path):
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    config_root.mkdir()
    data_root.mkdir()

    first = App(config_root, data_root)
    first.wait_for("DESKBOX-XID")
    first.stop()
    assert "Traceback" not in first.log

    second = App(config_root, data_root)
    try:
        second.wait_for("DESKBOX-XID")
        assert "Traceback" not in second.log
    finally:
        second.stop()

    layout = json.loads((config_root / "widget-layout.json").read_text())
    widgets = layout["layout"]["widgets"]
    assert len(widgets) == 1, "second boot restores the same widget, no extra first-run mint"


def test_search_feature_widget_boots(tmp_path):
    """Seed a Search feature widget between boots; second boot must realize
    its surface (launcher + recent-queries widget on the desktop layer)."""
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    config_root.mkdir()
    data_root.mkdir()

    first = App(config_root, data_root)
    first.wait_for("DESKBOX-XID")
    first.stop()
    assert "Traceback" not in first.log

    layout_path = config_root / "widget-layout.json"
    layout = json.loads(layout_path.read_text())
    file_widget = layout["layout"]["widgets"][0]
    search_widget = dict(file_widget)
    search_widget.update({"id": "search-widget-smoke", "name": "Search", "widgetKind": "Search"})
    layout["layout"]["widgets"].append(search_widget)
    layout["layout"]["featureWidgetEnabledStates"] = {"Search": True}
    layout_path.write_text(json.dumps(layout, indent=2))

    second = App(config_root, data_root)
    try:
        second.wait_for("DESKBOX-XID Search")
        assert "Traceback" not in second.log
    finally:
        second.stop()
