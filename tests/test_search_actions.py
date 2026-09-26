"""Search result action service tests (context-menu operations)."""

from __future__ import annotations

from panebox.models.search_models import SearchResultItem, SearchResultKind
from panebox.models.todo import TodoItem, TodoWidgetData
from panebox.services.search_actions import (
    attach_to_todo,
    copy_path,
    pick_attach_target,
    save_to_note,
)


def _file_item(title="doc.txt", path="/tmp/doc.txt"):
    return SearchResultItem(kind=SearchResultKind.FILE, title=title, detail_path=path, relevance_score=10)


class _TodoStore:
    data_by_widget: dict = {}

    def __init__(self, widget_id: str):
        self.widget_id = widget_id

    def load(self) -> TodoWidgetData:
        return _TodoStore.data_by_widget[self.widget_id]

    def save(self, data: TodoWidgetData) -> None:
        _TodoStore.data_by_widget[self.widget_id] = data


class _QuickCapture:
    def __init__(self):
        self.items = []

    def add_item(self, body: str):
        self.items.append(body)
        return body


def test_attach_to_todo_appends_linked_attachment():
    _TodoStore.data_by_widget = {"w1": TodoWidgetData(items=[TodoItem(id="a", text="x")])}
    key = attach_to_todo(_file_item(), _TodoStore, "w1")
    assert key == "Search.Action.AttachedToTodo"
    item = _TodoStore.data_by_widget["w1"].items[0]
    assert item.attachments[0].filePath == "/tmp/doc.txt"
    assert item.attachments[0].displayName == "doc.txt"


def test_attach_to_todo_fails_without_store_data():
    _TodoStore.data_by_widget = {}
    assert attach_to_todo(_file_item(), _TodoStore, "missing") == "Search.Action.AttachFailed"


def test_save_to_note_records_body():
    service = _QuickCapture()
    assert save_to_note(_file_item(), service) == "Search.Action.SavedToNote"
    assert service.items == ["doc.txt: /tmp/doc.txt"]


def test_copy_path_uses_injected_clipboard():
    captured = []
    assert copy_path(_file_item(), captured.append) == "Search.Action.PathCopied"
    assert captured == ["/tmp/doc.txt"]
    empty = SearchResultItem(kind=SearchResultKind.FILE, title="ghost")
    assert copy_path(empty, captured.append) == "Search.Action.FileOperationFailed"


def test_pick_attach_target_prefers_first_enabled_todo():
    from panebox.models.widget_config import WidgetConfig
    from panebox.services.settings_service import SettingsService

    settings = SettingsService()
    settings.add_widget(WidgetConfig(id="a", name="A", widgetKind="File"))
    settings.add_widget(WidgetConfig(id="b", name="B", widgetKind="Todo"))
    assert pick_attach_target(settings) == "b"
    disabled = settings.widgets()[1]
    disabled.isDisabled = True
    settings.update_widget(disabled)
    assert pick_attach_target(settings) is None
