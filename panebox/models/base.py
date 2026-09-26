"""Serializable model base: dataclasses whose field names mirror PaneBox wire JSON.

from_dict ignores unknown keys (forward compatibility) and fills defaults for
missing ones (backward compatibility), matching System.Text.Json behavior.
Nested models are declared via the NESTED class map:
    NESTED = {"items": (WidgetItemConfig, "list"), "surfaces": (Profile, "map")}
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any


class JsonModel:
    NESTED: dict[str, tuple[type, str]] = {}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if value is None and f.metadata.get("omit_when_none"):
                continue
            out[f.name] = _encode(value)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Any:
        if not isinstance(data, dict):
            data = {}
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name in data:
                raw = data[f.name]
                nested = cls.NESTED.get(f.name)
                if nested is not None:
                    model, shape = nested
                    if shape == "list":
                        raw = [model.from_dict(item) for item in raw] if isinstance(raw, list) else []
                    elif shape == "map":
                        raw = {str(k): model.from_dict(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
                    else:
                        raw = model.from_dict(raw)
                elif isinstance(raw, str) and "datetime" in str(f.type):
                    try:
                        raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except ValueError:
                        raw = None
                kwargs[f.name] = raw
        return cls(**kwargs)


def _encode(value: Any) -> Any:
    if isinstance(value, JsonModel):
        return value.to_dict()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value
