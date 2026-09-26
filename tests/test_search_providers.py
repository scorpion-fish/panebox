"""Search provider-layer tests: models/categories, ranker (C# quality rules),
history service caps/persistence, and the Everything-replacement file index.
No GTK needed."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from panebox.models.search_models import (
    FileCategory,
    SearchFileQueryPage,
    SearchResultItem,
    SearchResultKind,
    categorize_file,
)
from panebox.services.search_file_index import SearchFileIndex
from panebox.services.search_history import (
    MAX_HISTORY_ENTRIES,
    MAX_RECENT_RESULT_ENTRIES,
    SearchHistoryService,
)
from panebox.services.search_ranker import (
    get_identity_key,
    is_noisy_path,
    merge_and_rank,
)


# ── file categories ─────────────────────────────────────────────────────────


def test_categorize_file_linux_extensions():
    assert categorize_file("app.desktop") == FileCategory.APP
    assert categorize_file("tool.AppImage") == FileCategory.APP
    assert categorize_file("run.sh") == FileCategory.APP
    assert categorize_file("photo.PNG") == FileCategory.IMAGE
    assert categorize_file("report.pdf") == FileCategory.DOCUMENT
    assert categorize_file("song.flac") == FileCategory.MUSIC
    assert categorize_file("clip.mkv") == FileCategory.VIDEO
    assert categorize_file("bundle.tar.zst") == FileCategory.ARCHIVE
    assert categorize_file("Makefile") == FileCategory.OTHER


def test_result_item_display_helpers():
    item = SearchResultItem(kind=SearchResultKind.FOLDER, title="Docs")
    assert item.display_glyph  # fallback glyph when none supplied
    app = SearchResultItem(kind=SearchResultKind.FILE, title="org.gnome.Calc.desktop")
    assert app.app_display_name == "org.gnome.Calc"


# ── ranker ──────────────────────────────────────────────────────────────────


def _file(title, path, score=50, modified=None):
    return SearchResultItem(
        kind=SearchResultKind.FILE,
        title=title,
        detail_path=path,
        relevance_score=score,
        modified_at=modified,
    )


def test_identity_keys_by_kind():
    assert get_identity_key(_file("a", "/tmp/a.txt")).startswith("path:")
    todo = SearchResultItem(kind=SearchResultKind.TODO, title="t", todo_widget_id="w1", todo_item_id="i1")
    assert get_identity_key(todo) == "todo:w1:i1"
    note = SearchResultItem(kind=SearchResultKind.QUICK_CAPTURE, title="n", quick_capture_item_id="q1")
    assert get_identity_key(note) == "note:q1"
    action = SearchResultItem(kind=SearchResultKind.ACTION, title="a", action_id="open-settings")
    assert get_identity_key(action) == "action:open-settings"
    other = SearchResultItem(kind=SearchResultKind.HISTORY, title="q", subtitle=None)
    assert get_identity_key(other) == "History:q:None"


def test_kind_boosts_and_ordering():
    items = [
        _file("plain.txt", "/d/plain.txt", score=50),
        SearchResultItem(kind=SearchResultKind.FOLDER, title="d", detail_path="/d", relevance_score=50),
        SearchResultItem(
            kind=SearchResultKind.QUICK_CAPTURE,
            title="n",
            quick_capture_item_id="1",
            relevance_score=50,
        ),
        SearchResultItem(
            kind=SearchResultKind.TODO,
            title="t",
            todo_widget_id="w",
            todo_item_id="1",
            relevance_score=50,
        ),
        SearchResultItem(kind=SearchResultKind.ACTION, title="a", action_id="x", relevance_score=50),
    ]
    ranked = merge_and_rank(items, "q", 10)
    assert [item.kind for item in ranked] == [
        SearchResultKind.ACTION,
        SearchResultKind.TODO,
        SearchResultKind.QUICK_CAPTURE,
        SearchResultKind.FOLDER,
        SearchResultKind.FILE,
    ]


def test_app_extension_file_beats_plain_file():
    ranked = merge_and_rank(
        [_file("launcher.desktop", "/d/launcher.desktop"), _file("notes.txt", "/d/notes.txt")],
        "q",
        10,
    )
    assert ranked[0].title == "launcher.desktop"  # +3 App boost


def test_noisy_path_detection_and_penalties():
    assert is_noisy_path("/home/u/.cache/x.bin")
    assert is_noisy_path("/home/u/.git/config")
    assert is_noisy_path("/home/u/node_modules/pkg/index.js")
    assert is_noisy_path("/home/u/download.part")
    assert not is_noisy_path("/home/u/Documents/thesis.pdf")
    assert not is_noisy_path(None)

    # Broad query: heavy penalty but the item survives at the tail.
    ranked = merge_and_rank(
        [_file("x.bin", "/home/u/.cache/x.bin", score=100), _file("x.bin", "/d/x.bin", score=50)],
        "unrelated",
        10,
    )
    assert ranked[0].detail_path == "/d/x.bin"
    assert round(ranked[1].relevance_score) == 30  # 100 - 70

    # Exact title match inside a cache path: only −35 (C# intent).
    ranked = merge_and_rank([_file("x.bin", "/home/u/.cache/x.bin", score=100)], "x.bin", 10)
    assert round(ranked[0].relevance_score) == 65


def test_dedupe_prefers_extension_then_mtime():
    older = datetime(2024, 1, 1)
    newer = datetime(2024, 6, 1)
    # Same path identity: higher score wins outright.
    ranked = merge_and_rank(
        [_file("a", "/d/a", score=10), _file("a.txt", "/d/a", score=20, modified=older)],
        "q",
        10,
    )
    assert len(ranked) == 1 and ranked[0].relevance_score == 20
    # Equal scores: title-with-extension wins.
    ranked = merge_and_rank(
        [
            _file("a", "/d/a", score=20, modified=newer),
            _file("a.txt", "/d/a", score=20, modified=older),
        ],
        "q",
        10,
    )
    assert ranked[0].title == "a.txt"
    # Equal scores + same extension shape: newer mtime wins.
    ranked = merge_and_rank(
        [
            _file("a.txt", "/d/a.txt", score=20, modified=older),
            _file("a.txt", "/d/a.txt", score=20, modified=newer),
        ],
        "q",
        10,
    )
    assert ranked[0].modified_at == newer


def test_zero_score_items_are_dropped_and_max_results_caps():
    items = [_file("z", "/d/z", score=0), _file("y", "/d/y", score=5), _file("x", "/d/x", score=6)]
    assert [i.title for i in merge_and_rank(items, "q", 10)] == ["x", "y"]
    assert len(merge_and_rank(items, "q", 1)) == 1


# ── history ─────────────────────────────────────────────────────────────────


def test_history_record_dedupe_and_cap(tmp_path):
    service = SearchHistoryService(tmp_path / "h.json")
    for number in range(MAX_HISTORY_ENTRIES + 5):
        service.record_query(f"q{number}")
    assert len(service.recent_queries) == MAX_HISTORY_ENTRIES
    assert service.recent_queries[0] == f"q{MAX_HISTORY_ENTRIES + 4}"
    service.record_query("Q1")  # case-insensitive dedupe, moved to front
    assert service.recent_queries[0] == "Q1"
    assert "q1" not in service.recent_queries
    service.record_query("   ")  # whitespace ignored
    service.record_query(None)
    assert service.recent_queries[0] == "Q1"


def test_history_favorites_and_clears(tmp_path):
    service = SearchHistoryService(tmp_path / "h.json")
    service.record_query("a")
    service.record_result(SearchResultItem(kind=SearchResultKind.FILE, title="f", detail_path="/f", relevance_score=1))
    assert service.toggle_favorite("a") is True
    assert service.is_favorite("A") is True
    assert service.toggle_favorite("a") is False  # toggled off…
    assert service.toggle_favorite("a") is True  # …and back on

    service.clear_recent_history()  # keeps favorites + recent results
    assert service.recent_queries == []
    assert service.favorite_queries == ["a"]
    assert len(service.recent_results) == 1

    service.record_query("b")
    service.clear_history_and_results()  # keeps favorites only
    assert service.recent_queries == [] and service.recent_results == []
    assert service.favorite_queries == ["a"]

    service.record_query("c")
    service.clear_all_history()
    assert service.favorite_queries == []


def test_history_record_result_rules(tmp_path):
    service = SearchHistoryService(tmp_path / "h.json")
    # Action/History/Favorite results are not recordable.
    for kind, extra in (
        (SearchResultKind.ACTION, {"action_id": "x"}),
        (SearchResultKind.HISTORY, {"history_query": "q"}),
        (SearchResultKind.FAVORITE, {"history_query": "q"}),
    ):
        service.record_result(SearchResultItem(kind=kind, title="t", **extra))
    assert service.recent_results == []
    for number in range(MAX_RECENT_RESULT_ENTRIES + 3):
        service.record_result(
            SearchResultItem(
                kind=SearchResultKind.FILE,
                title=f"t{number}",
                detail_path=f"/t{number}",
                relevance_score=1,
            )
        )
    assert len(service.recent_results) == MAX_RECENT_RESULT_ENTRIES
    assert service.recent_results[0].title == f"t{MAX_RECENT_RESULT_ENTRIES + 2}"
    # Re-recording an item dedupes by identity and moves it to the front.
    service.record_result(
        SearchResultItem(
            kind=SearchResultKind.FILE,
            title="t5",
            detail_path="/t5",
            relevance_score=1,
        )
    )
    assert service.recent_results[0].title == "t5"
    assert len(service.recent_results) == MAX_RECENT_RESULT_ENTRIES


def test_history_persistence_round_trip_and_corruption(tmp_path):
    path = tmp_path / "h.json"
    service = SearchHistoryService(path)
    service.record_query("persisted")
    service.toggle_favorite("pinned")
    service.record_result(
        SearchResultItem(
            kind=SearchResultKind.TODO,
            title="todo",
            todo_widget_id="w",
            todo_item_id="i",
            relevance_score=1,
        )
    )
    reloaded = SearchHistoryService(path)
    assert reloaded.recent_queries == ["persisted"]
    assert reloaded.favorite_queries == ["pinned"]
    assert reloaded.recent_results[0].kind == SearchResultKind.TODO
    assert reloaded.recent_results[0].todo_item_id == "i"

    path.write_text("{not json", encoding="utf-8")
    corrupted = SearchHistoryService(path)
    assert corrupted.recent_queries == []  # fresh start, no raise


# ── file index (Everything replacement) ─────────────────────────────────────


def _make_tree(root: Path):
    (root / "Documents").mkdir()
    (root / "Documents" / "report-2024.pdf").write_bytes(b"x" * 10)
    (root / "Documents" / "report-old.pdf").write_bytes(b"x" * 20)
    (root / "Documents" / "notes.md").write_text("hello")
    (root / "Pictures").mkdir()
    (root / "Pictures" / "cat.png").write_bytes(b"x")
    (root / ".cache").mkdir()
    (root / ".cache" / "noise.bin").write_bytes(b"x")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg").mkdir()
    (root / "node_modules" / "pkg" / "deep.js").write_text("x")


def test_index_scan_skips_noisy_directories(tmp_path):
    _make_tree(tmp_path)
    index = SearchFileIndex(roots=[str(tmp_path)], include_apps=False)
    index.scan_now()
    assert index.query("noise.bin").total_matched_count == 0
    assert index.query("deep.js").total_matched_count == 0
    assert index.query("report").total_matched_count == 2
    assert index.query("cat.png").items[0].detail_path.endswith("Pictures/cat.png")


def test_index_and_terms_and_name_beats_path(tmp_path):
    _make_tree(tmp_path)
    index = SearchFileIndex(roots=[str(tmp_path)], include_apps=False)
    index.scan_now()
    # Space-separated terms AND against the full path.
    assert index.query("report pdf").total_matched_count == 2
    assert index.query("report 2024").total_matched_count == 1
    assert index.query("report cat").total_matched_count == 0
    assert index.query("").total_matched_count == 0
    # A file whose NAME contains the term outranks one only matched by path.
    page = index.query("pictures")
    assert page.items[0].title == "cat.png"


def test_index_paging_contract(tmp_path):
    _make_tree(tmp_path)
    index = SearchFileIndex(roots=[str(tmp_path)], include_apps=False)
    index.scan_now()
    full = index.query("report", 0, 200)
    assert full.total_matched_count == 2 and full.next_offset == 2
    assert not (full.next_offset < full.total_matched_count)

    first = index.query("report", 0, 1)
    assert len(first.items) == 1 and first.next_offset == 1
    second = index.query("report", 1, 1)
    assert len(second.items) == 1 and second.next_offset == 2
    assert {first.items[0].title, second.items[0].title} == {"report-2024.pdf", "report-old.pdf"}
    # Name score: 2024 file == old file (both contain); stable name order.
    assert first.items[0].title == "report-2024.pdf"


def test_index_live_updates(tmp_path):
    _make_tree(tmp_path)
    index = SearchFileIndex(roots=[str(tmp_path)], include_apps=False)
    index.scan_now()
    fresh = tmp_path / "Documents" / "fresh.txt"
    fresh.write_text("x")
    index.apply_filesystem_changes([str(fresh)])
    assert index.query("fresh").total_matched_count == 1
    fresh.unlink()
    index.apply_filesystem_changes([str(fresh)])
    assert index.query("fresh").total_matched_count == 0


def test_index_items_carry_stat_metadata(tmp_path):
    _make_tree(tmp_path)
    index = SearchFileIndex(roots=[str(tmp_path)], include_apps=False)
    index.scan_now()
    item = index.query("notes.md").items[0]
    assert item.kind == SearchResultKind.FILE
    assert item.file_size == len("hello")
    assert abs(item.modified_at - datetime.now()) < timedelta(days=1)
    # "documents" only path-matches; contained files outrank the folder
    # (file-before-folder on path-only hits) but the folder is present.
    page = index.query("documents")
    folder = next(result for result in page.items if result.kind == SearchResultKind.FOLDER)
    assert folder.title == "Documents"
    assert page.items[0].kind == SearchResultKind.FILE


def test_empty_page_contract():
    page = SearchFileQueryPage.empty()
    assert page.items == [] and page.total_matched_count == 0 and page.next_offset == 0
