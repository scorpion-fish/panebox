"""Stable per-installation device id (port of Services/DeviceIdentity.cs).

Persisted at the data root; regenerated only when the file is lost. Used as
the sync-layer DeviceId stamp on Quick Capture records.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ..constants import DATA_ROOT

_cached: str | None = None


def device_id(root: Path = DATA_ROOT) -> str:
    global _cached
    if _cached:
        return _cached
    marker = Path(root) / "device-id.txt"
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        if marker.exists():
            value = marker.read_text(encoding="utf-8").strip()
            if value:
                _cached = value
                return _cached
        value = uuid.uuid4().hex
        marker.write_text(value, encoding="utf-8")
        _cached = value
        return _cached
    except OSError:
        _cached = _cached or uuid.uuid4().hex
        return _cached
