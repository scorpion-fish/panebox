"""Clipboard write scope (port of Services/PaneBoxClipboardWriteScope.cs).

When PaneBox itself puts text on the clipboard (copy item, export…), the
Quick Capture clipboard monitor must not record its own writes back as
"recent". Writers announce the payload; the monitor asks before capturing.
"""

from __future__ import annotations

import time
from collections import OrderedDict

_TTL_SECONDS = 30.0
_MAX_ENTRIES = 32

_marked: "OrderedDict[str, float]" = OrderedDict()


def _normalize(body) -> str:
    # NormalizeBody(body, PlainText): NULs stripped, then trimmed.
    if body is None:
        return ""
    return body.replace("\0", "").strip()


def mark_text(body) -> None:
    normalized = _normalize(body)
    if not normalized:
        return
    now = time.monotonic()
    _marked[normalized] = now
    _evict(now)


def should_ignore_text(body) -> bool:
    if not _marked:
        return False
    _evict(time.monotonic())
    return _normalize(body) in _marked


def _evict(now: float) -> None:
    stale = [k for k, ts in _marked.items() if now - ts > _TTL_SECONDS]
    for key in stale:
        _marked.pop(key, None)
    while len(_marked) > _MAX_ENTRIES:
        _marked.popitem(last=False)


def reset() -> None:
    _marked.clear()
