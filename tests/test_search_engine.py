"""Search engine tests: unified search over the file index + PaneBox content
+ actions, groups, recommendations, and the recommendation helpers. Pure
services — the engine is called synchronously like a worker would."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


from panebox import i18n
from panebox.models.search_models import (
    SearchResultKind,
    SearchRecommendationItem,
)
from panebox.models.todo import TodoItem, TodoWidgetData
from panebox.services.quick_capture_service import QuickCaptureService
from panebox.services.quick_capture_store import QuickCaptureStore
from panebox.services.search_engine import SearchEngineService
from panebox.services.search_file_index import SearchFileIndex
from panebox.services.settings_service import SettingsService


class _FakeTodoStore:
    data_by_widget: dict = {}

    def __init__(self, widget_id: str):
        self.widget_id = widget_id

    def load(self) -> TodoWidgetData:
        return _FakeTodoStore.data_by_widget.get(self.widget_id, TodoWidgetData())


def _make_engine(tmp_path: Path, roots: Path):
    settings = SettingsService(config_root=tmp_path / "config")
    store_dir = tmp_path / "qc"
    store_dir.mkdir()
    quick_capture = QuickCaptureService(store=QuickCaptureStore(data_dir=store_dir))
    _FakeTodoStore.data_by_widget = {}
    engine = SearchEngineService(
        settings_service=settings,
        file_index=SearchFileIndex(roots=[str(roots)], include_apps=False),
        quick_capture_service=quick_capture,
        todo_store_factory=_FakeTodoStore,
    )
    return engine, settings, quick_capture


def test_engine_unified_search_actions_and_groups(tmp_path):
    i18n.set_language("en-US")
    try:
        roots = tmp_path / "files"
        roots.mkdir()
        (roots / "todo-list.txt").write_text("x")
        engine, _settings, _qc = _make_engine(tmp_path, roots)

        response = engine.search("todo")
        kinds = {item.kind for item in response.ranked_items}
        assert SearchResultKind.FILE in kinds
        assert SearchResultKind.ACTION in kinds  # "New Todo" contains 'todo'
        action = next(item for item in response.ranked_items if item.kind == SearchResultKind.ACTION)
        assert action.action_id == "new-todo"
        # Actions group exists and is named from the localized key.
        assert any(group.kind == SearchResultKind.ACTION for group in response.groups)

        # Query matching no action name excludes actions.
        response = engine.search("zzz-nothing")
        assert all(item.kind != SearchResultKind.ACTION for item in response.ranked_items)
    finally:
        i18n.set_language(None)


def test_engine_panebox_content_search_and_refresh(tmp_path):
    i18n.set_language("en-US")
    try:
        roots = tmp_path / "files"
        roots.mkdir()
        engine, settings, quick_capture = _make_engine(tmp_path, roots)

        _item = quick_capture.add_item("buy oat milk note")
        todo_widget_id = "todo-1"
        settings.add_widget(_widget(todo_widget_id, "Todo"))
        _FakeTodoStore.data_by_widget[todo_widget_id] = TodoWidgetData(
            items=[
                TodoItem(id="t1", text="renew passport"),
                TodoItem(id="t2", text="oat milk todo done", isCompleted=True),
                TodoItem(id="t3", text="oat milk active"),  # same prefix, not completed
            ]
        )

        response = engine.search("oat")
        titles = {item.title for item in response.ranked_items}
        assert any("oat milk" in title for title in titles)
        assert "oat milk todo done" in titles
        # Same prefix-match shape: the active todo (+10) outranks the
        # completed one (−20) — the completion penalty is observable.
        ordered = [i.title for i in response.ranked_items]
        assert ordered.index("oat milk active") < ordered.index("oat milk todo done")
        assert "buy oat milk note" in ordered
        todo_result = next(i for i in response.ranked_items if i.todo_item_id == "t3")
        assert todo_result.subtitle == "Todo widget"  # no due date → widget name

        # Content disabled → notes/todos vanish.
        settings.settings.search.searchIncludePaneBoxContent = False
        engine.set_panebox_content_enabled(False)
        response = engine.search("oat")
        assert all(i.kind not in (SearchResultKind.TODO, SearchResultKind.QUICK_CAPTURE) for i in response.ranked_items)
    finally:
        i18n.set_language(None)


def test_engine_recommendations_widget_shortcuts_first(tmp_path, monkeypatch):
    i18n.set_language("en-US")
    try:
        roots = tmp_path / "files"
        roots.mkdir()
        engine, settings, _qc = _make_engine(tmp_path, roots)

        # A file widget with a mapped folder containing launchers.
        mapped = tmp_path / "mapped"
        mapped.mkdir()
        (mapped / "z-widget.desktop").write_text("[Desktop Entry]\nType=Application\n")
        widget = _widget("fw1", "File", mapped_folder=str(mapped))
        settings.add_widget(widget)

        # App directories: fake XDG set with two launchers.
        app_dir = tmp_path / "apps"
        app_dir.mkdir()
        (app_dir / "a-app.desktop").write_text("[Desktop Entry]\n")
        (app_dir / "m-app.desktop").write_text("[Desktop Entry]\n")
        monkeypatch.setattr(
            "panebox.services.search_engine.application_directories",
            lambda: [str(app_dir)],
        )

        recommendations = engine.recommendations()
        titles = [r.title for r in recommendations]
        assert titles[0] == "z-widget.desktop"  # widget curation wins
        assert "a-app.desktop" in titles and "m-app.desktop" in titles
        assert recommendations[0].subtitle == "File widget" == widget.name
        assert recommendations[1].subtitle == i18n.t("Search.Recommend.StartMenu")
        # Non-launchers in the widget folder are ignored.
        (mapped / "readme.txt").write_text("x")
        titles = [r.title for r in engine.recommendations()]
        assert "readme.txt" not in titles
    finally:
        i18n.set_language(None)


def test_engine_recent_notes_and_upcoming_todos(tmp_path):
    i18n.set_language("en-US")
    try:
        roots = tmp_path / "files"
        roots.mkdir()
        engine, settings, quick_capture = _make_engine(tmp_path, roots)

        quick_capture.add_item("note one")
        quick_capture.add_item("note two")
        notes = engine.recent_notes()
        assert len(notes) == 2 and notes[0].kind == SearchResultKind.QUICK_CAPTURE

        due = datetime.now() + timedelta(days=2)
        overdue = datetime.now() - timedelta(days=1)
        widget_id = "todo-9"
        settings.add_widget(_widget(widget_id, "Todo"))
        _FakeTodoStore.data_by_widget[widget_id] = TodoWidgetData(
            items=[
                TodoItem(id="a", text="soon", dueDate=due),
                TodoItem(id="b", text="past", dueDate=overdue),  # outside window
                TodoItem(id="c", text="done", dueDate=due, isCompleted=True),
                TodoItem(id="d", text="far", dueDate=datetime.now() + timedelta(days=30)),
            ]
        )
        upcoming = engine.upcoming_todos()
        assert [item.todo_item_id for item in upcoming] == ["a"]
        assert upcoming[0].subtitle.startswith(f"{i18n.t('Search.Todo.Due')}:")
    finally:
        i18n.set_language(None)


def test_engine_paging_contract_via_index(tmp_path):
    roots = tmp_path / "files"
    roots.mkdir()
    for number in range(5):
        (roots / f"page-{number}.txt").write_text("x")
    engine, _settings, _qc = _make_engine(tmp_path, roots)

    first = engine.search_page("page", 0, 2)
    assert first.total_file_result_count == 5
    assert len([i for i in first.ranked_items if i.kind == SearchResultKind.FILE]) == 2
    assert first.has_more_results and first.next_file_result_offset == 2

    second = engine.search_page("page", 2, 2)
    assert second.next_file_result_offset == 4
    third = engine.search_page("page", 4, 2)
    assert not third.has_more_results and third.next_file_result_offset == 5


def test_recommendation_item_shape_passthrough():
    # Fields survive the model round-trip (used by history recentResults).
    item = SearchRecommendationItem(
        kind=SearchResultKind.TODO,
        title="t",
        subtitle="s",
        todo_widget_id="w",
        todo_item_id="i",
    )
    assert (item.kind, item.todo_item_id) == (SearchResultKind.TODO, "i")


def _widget(widget_id: str, kind: str, mapped_folder: str = None):
    from panebox.models.widget_config import WidgetConfig

    config = WidgetConfig(id=widget_id, name=f"{kind} widget", widgetKind=kind)
    if mapped_folder:
        config.mappedFolderPath = mapped_folder
    return config
