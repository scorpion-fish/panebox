"""Cloud backup domains (port of Services/CloudBackupDomains.cs).

Bit flags over the three user-visible data types. Attachments and images are
deliberately OUT of every domain: they can dwarf the JSON stores and ride the
same sync as content the user actually asked to sync.
"""

from __future__ import annotations

import re
from typing import Optional

TODO_DATA = 1
QUICK_CAPTURE_DATA = 2
WIDGET_STYLE = 4

FILE_DOMAINS = (TODO_DATA, QUICK_CAPTURE_DATA)
ALL_DOMAINS = (TODO_DATA, QUICK_CAPTURE_DATA, WIDGET_STYLE)

WIDGET_STYLE_ENTRY_NAME = "widget-style.json"

_QUICK_CAPTURE_PATH = "quick-capture/quick-capture.json"
_TODO_PATH = re.compile(r"^widgets/[^/]+/todo\.json$")


def is_todo_data_path(relative_path: str) -> bool:
    return bool(_TODO_PATH.match(relative_path.replace("\\", "/")))


def is_quick_capture_data_path(relative_path: str) -> bool:
    return relative_path.replace("\\", "/") == _QUICK_CAPTURE_PATH


def is_in_domain(domain: int, relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    if domain == TODO_DATA:
        return is_todo_data_path(normalized)
    if domain == QUICK_CAPTURE_DATA:
        return is_quick_capture_data_path(normalized)
    # WidgetStyle owns no data-directory files — it is a projection document.
    return False


def is_in_scope(scope: int, relative_path: str) -> bool:
    """Export-time filter: the path belongs to ANY enabled domain."""
    return any(scope & domain and is_in_domain(domain, relative_path) for domain in FILE_DOMAINS)


def to_manifest_name(domain: int) -> Optional[str]:
    return {
        TODO_DATA: "todo-data",
        QUICK_CAPTURE_DATA: "quick-capture-data",
        WIDGET_STYLE: "widget-style",
    }.get(domain)


def try_from_manifest_name(name: str) -> Optional[int]:
    return {
        "todo-data": TODO_DATA,
        "quick-capture-data": QUICK_CAPTURE_DATA,
        "widget-style": WIDGET_STYLE,
    }.get(name)


def to_manifest_names(scope: int) -> list[str]:
    names: list[str] = []
    for domain in ALL_DOMAINS:
        if scope & domain:
            name = to_manifest_name(domain)
            if name:
                names.append(name)
    return names


def try_from_manifest_names(names) -> int:
    scope = 0
    for name in names or ():
        domain = try_from_manifest_name(str(name))
        if domain is None:
            raise ValueError(f"Unknown backup domain name: {name!r}")
        scope |= domain
    return scope


def try_get_todo_widget_id(relative_path: str) -> Optional[str]:
    """widgets/<id>/todo.json → <id>, else None (ids never contain '/')."""
    normalized = relative_path.replace("\\", "/")
    if not normalized.startswith("widgets/") or not normalized.endswith("/todo.json"):
        return None
    widget_id = normalized[len("widgets/") : -len("/todo.json")]
    return widget_id if widget_id and "/" not in widget_id else None
