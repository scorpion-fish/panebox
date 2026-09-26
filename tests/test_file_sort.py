"""Sorting + manual order: natural compare, folder-first, ManualOrderBook."""

from __future__ import annotations

from panebox.constants import SortMode
from panebox.models.widget_config import WidgetItemConfig
from panebox.services.file_sort import (
    FileEntry,
    ManualOrderBook,
    SortSession,
    natural_compare,
    sort_entries,
    sync_config_items,
)


def entry(name: str, **kw) -> FileEntry:
    return FileEntry(path=f"/w/{name}", name=name, **kw)


# ---- natural compare ------------------------------------------------------


def test_natural_compare_digit_runs():
    assert natural_compare("file2", "file10") < 0
    assert natural_compare("file10", "file9") > 0
    assert natural_compare("file1", "file1") == 0
    assert natural_compare("a1b2", "a1b2") == 0


def test_natural_compare_case_insensitive():
    assert natural_compare("Beta", "alpha") > 0
    assert natural_compare("BETA", "beta") == 0


def test_natural_compare_prefix_shorter_first():
    assert natural_compare("file", "file1") < 0


# ---- sort_entries ----------------------------------------------------------


def test_folders_first_ascending_and_flipped_descending():
    # C# CompareItems negates the whole comparison under descending, so the
    # folder-first rule flips too: folders land at the back.
    entries = [
        entry("zeta.txt"),
        entry("Alpha", is_folder=True),
        entry("aaa.txt"),
        entry("Zulu", is_folder=True),
    ]
    ascending = sort_entries(entries, SortMode.NAME, False)
    assert [e.name for e in ascending] == ["Alpha", "Zulu", "aaa.txt", "zeta.txt"]
    descending = sort_entries(entries, SortMode.NAME, True)
    assert [e.name for e in descending] == ["zeta.txt", "aaa.txt", "Zulu", "Alpha"]


def test_sort_by_size_and_type():
    entries = [
        entry("big.png", size=100, extension=".png"),
        entry("small.png", size=10, extension=".png"),
        entry("a.txt", size=50, extension=".txt"),
    ]
    by_size = sort_entries(entries, SortMode.SIZE, False)
    assert [e.name for e in by_size] == ["small.png", "a.txt", "big.png"]
    by_type = sort_entries(entries, SortMode.TYPE, False)
    assert [e.name for e in by_type] == ["big.png", "small.png", "a.txt"]  # .png < .txt


def test_sort_by_date_modified():
    entries = [entry("old", last_modified=1.0), entry("new", last_modified=2.0)]
    assert [e.name for e in sort_entries(entries, SortMode.DATE_MODIFIED, False)] == ["old", "new"]


def test_type_ties_fall_back_to_name():
    entries = [entry("b.txt", extension=".txt"), entry("a.txt", extension=".txt")]
    assert [e.name for e in sort_entries(entries, SortMode.TYPE, False)] == ["a.txt", "b.txt"]


# ---- ManualOrderBook ---------------------------------------------------------


def test_manual_order_book_upsert_and_move():
    book = ManualOrderBook()
    book.upsert("a")
    book.upsert("b")
    book.upsert("c")
    assert book.paths == ["a", "b", "c"]

    # re-add without a slot is a no-op
    book.upsert("a")
    assert book.paths == ["a", "b", "c"]

    # drop "c" at slot 1
    book.upsert("c", preferred_index=1)
    assert book.paths == ["a", "c", "b"]

    assert book.move("b", 0)
    assert book.paths == ["b", "a", "c"]
    assert not book.move("b", 0)  # already there
    assert not book.move("missing", 0)

    book.remove("a")
    assert book.paths == ["b", "c"]
    assert book.index_of("a") == -1


def test_manual_order_book_readd_after_lowering_adjusts_index():
    book = ManualOrderBook()
    for name in ("a", "b", "c", "d"):
        book.upsert(name)
    # move "a" to the end via preferred index past its old slot
    book.upsert("a", preferred_index=3)
    assert book.paths == ["b", "c", "a", "d"]


def test_order_for_unknown_paths_sort_last():
    book = ManualOrderBook()
    book.upsert("a")
    assert book.order_for("a") == 0
    assert book.order_for("zzz") == 1  # len(paths)


def test_sort_session_manual_uses_order_book():
    session = SortSession(mode=SortMode.MANUAL)
    session.manual.paths = ["/w/c", "/w/a", "/w/b"]  # book keys are paths
    entries = [entry("a"), entry("b"), entry("c")]
    result = session.apply(entries)
    assert [e.name for e in result] == ["c", "a", "b"]
    assert [e.sort_order for e in result] == [0, 1, 2]


# ---- config sync -------------------------------------------------------------


def test_sync_config_items_only_at_root():
    items = [WidgetItemConfig(path="/w/a", sortOrder=0)]
    assert not sync_config_items(["/w/a"], items, at_root=False)
    assert items[0].sortOrder == 0


def test_sync_config_items_detects_changes():
    items = [WidgetItemConfig(path="/w/a", sortOrder=0)]
    assert not sync_config_items(["/w/a"], items, at_root=True), "identical → no change"

    assert sync_config_items(["/w/b", "/w/a"], items, at_root=True)
    assert [i.path for i in items] == ["/w/b", "/w/a"]
    assert [i.sortOrder for i in items] == [0, 1]
