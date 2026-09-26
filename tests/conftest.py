"""Test bootstrap: isolate config/data roots BEFORE panebox imports.

constants.py reads PANEBOX_CONFIG_ROOT / PANEBOX_DATA_ROOT at import time,
so the env is pinned here (conftest imports before any test module).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="panebox-tests-"))
os.environ["PANEBOX_CONFIG_ROOT"] = str(_SESSION_TMP / "config")
os.environ["PANEBOX_DATA_ROOT"] = str(_SESSION_TMP / "data")
os.environ.setdefault("NO_AT_BRIDGE", "1")

sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture()
def fresh_config_root(tmp_path: Path) -> Path:
    """Per-test isolated config directory for a SettingsService."""
    root = tmp_path / "config"
    root.mkdir()
    return root
