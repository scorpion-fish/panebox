"""Search result actions (port of Services/SearchResultActionService.cs).

Operations behind the result context menu: open, reveal location, copy path,
attach to a todo, save to a note. Each call returns the localization key of
the outcome so the view can surface a toast/notification the same way the C#
service raised SearchResultActionEventArgs.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from ..models.search_models import SearchResultItem, SearchResultKind
from .search_engine import truncate_text

NOTE_BODY_LIMIT = 120


def open_result(item: SearchResultItem) -> bool:
    """Open with the default application (launcher equivalent of Shell.Execute)."""
    if item.kind not in (SearchResultKind.FILE, SearchResultKind.FOLDER):
        return False
    path = item.detail_path or ""
    if not path:
        return False
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(os.path.abspath(path)).get_uri(), None)
        return True
    except Exception:
        return False


def reveal_result(item: SearchResultItem) -> bool:
    """Select the item in the file manager (Explorer reveal)."""
    if item.kind not in (SearchResultKind.FILE, SearchResultKind.FOLDER):
        return False
    path = item.detail_path or ""
    if not path:
        return False
    from . import file_ops

    try:
        file_ops.reveal_in_file_manager([os.path.abspath(path)])
        return True
    except Exception:
        return False


def copy_path(item: SearchResultItem, set_text: Callable[[str], None]) -> str:
    """Copy the absolute path; returns the outcome key."""
    path = os.path.abspath(item.detail_path) if item.detail_path else ""
    if not path:
        return "Search.Action.FileOperationFailed"
    set_text(path)
    return "Search.Action.PathCopied"


def attach_to_todo(
    item: SearchResultItem,
    todo_store_factory: Callable[[str], object],
    widget_id: str,
) -> str:
    """Append a linked file attachment to the given Todo widget's first item."""
    path = item.detail_path or ""
    if not path or item.kind not in (SearchResultKind.FILE, SearchResultKind.FOLDER):
        return "Search.Action.AttachFailed"
    try:
        from ..models.todo import TodoAttachment

        store = todo_store_factory(widget_id)
        data = store.load()
        target = next(
            (entry for entry in data.items if not entry.isCompleted),
            data.items[0] if data.items else None,
        )
        if target is None:
            return "Search.Action.AttachFailed"
        target.attachments.append(
            TodoAttachment(
                filePath=os.path.abspath(path),
                displayName=item.title,
                type="file",
            )
        )
        store.save(data)
        return "Search.Action.AttachedToTodo"
    except Exception:
        return "Search.Action.AttachFailed"


def save_to_note(item: SearchResultItem, quick_capture_service) -> str:
    """Append a Quick Capture note referencing the result (link body)."""
    path = item.detail_path or ""
    if not path:
        return "Search.Action.SaveFailed"
    try:
        quick_capture_service.add_item(truncate_text(f"{item.title}: {os.path.abspath(path)}", NOTE_BODY_LIMIT))
        return "Search.Action.SavedToNote"
    except Exception:
        return "Search.Action.SaveFailed"


def pick_attach_target(settings_service) -> Optional[str]:
    """First enabled Todo widget id (the C# picker defaults to it)."""
    for widget in settings_service.widgets():
        if widget.widgetKind == "Todo" and not widget.isDisabled:
            return widget.id
    return None
