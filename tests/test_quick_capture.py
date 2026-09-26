"""Quick Capture service/store/clipboard-scope/markdown-renderer tests
(port of the QuickCaptureStore.Normalize + QuickCaptureService invariants)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panebox.models.quick_capture import (
    QuickCaptureItem,
    QuickCaptureStoreData,
    TextContentFormat,
)
from panebox.services import clipboard_write_scope, markdown_render
from panebox.services.quick_capture_service import (
    MAX_ITEM_BODY_CHARACTERS,
    QuickCaptureService,
    build_image_export_file_name,
    normalize_body,
    normalize_recent_limit,
    try_detect_url,
)
from panebox.services.quick_capture_store import QuickCaptureStore, deduplication_key, normalize


@pytest.fixture()
def store(tmp_path: Path) -> QuickCaptureStore:
    return QuickCaptureStore(tmp_path / "qc")


@pytest.fixture()
def service(store: QuickCaptureStore) -> QuickCaptureService:
    return QuickCaptureService(store)


# ---- body / url / limits ------------------------------------------------------


def test_normalize_body_trims_plain_but_not_markdown():
    assert normalize_body("  hi  ") == "hi"
    assert normalize_body("  hi  ", TextContentFormat.MARKDOWN) == "  hi  "
    assert normalize_body("a\0b") == "ab"


def test_normalize_recent_limit_clamps():
    assert normalize_recent_limit(30) == 30
    assert normalize_recent_limit(5) == 30  # below minimum resets to default
    assert normalize_recent_limit(12) == 12
    assert normalize_recent_limit(500) == 100


def test_try_detect_url_only_http_links():
    assert try_detect_url("https://example.com/a") == "https://example.com/a"
    assert try_detect_url("http://example.com") == "http://example.com"
    assert try_detect_url("ftp://example.com") is None
    assert try_detect_url("see https://x.com here") is None


def test_body_truncation_reports_and_caps():
    from panebox.services.quick_capture_service import truncate_body

    body = "x" * (MAX_ITEM_BODY_CHARACTERS + 10)
    got, flag = truncate_body(body)
    assert flag is True and len(got) == MAX_ITEM_BODY_CHARACTERS


# ---- store normalize -------------------------------------------------------


def test_store_normalize_fills_ids_and_stamps_device():
    data = QuickCaptureStoreData(
        items=[QuickCaptureItem(id="", body="hello")],
        recentItems=[QuickCaptureItem(id="  ", body="clip")],
    )
    result = normalize(data)
    assert all(i.id for i in result.items + result.recentItems)
    assert all(i.deviceId for i in result.items)
    assert result.version == 4
    assert result.items[0].sortOrder == 0


def test_store_normalize_recent_entries_demoted_to_plain_clipboard():
    item = QuickCaptureItem(
        body="clip",
        contentFormat=TextContentFormat.MARKDOWN,
        appearancePreset="Rose",
        sourceKind="Manual",
    )
    result = normalize(QuickCaptureStoreData(recentItems=[item]))
    recent = result.recentItems[0]
    assert recent.isRecent
    assert recent.contentFormat == TextContentFormat.PLAIN_TEXT
    assert recent.appearancePreset == "Default"
    assert recent.sourceKind == "Clipboard"


def test_store_normalize_synthesizes_image_attachment_and_dedupes_ids():
    first = QuickCaptureItem(id="dup", body="a", sortOrder=0)
    second = QuickCaptureItem(id="dup", body="b", sortOrder=1)
    image = QuickCaptureItem(body="", imagePath="/tmp/img.png", sortOrder=2)
    result = normalize(QuickCaptureStoreData(items=[first, second, image]))
    assert [i.body for i in result.items] == ["a", ""]  # second dup dropped
    assert result.items[1].type == "Image"
    assert result.items[1].attachments[0].type == "image"
    assert result.items[1].attachments[0].filePath == "/tmp/img.png"


def test_store_normalize_caps_tombstones():
    items = [QuickCaptureItem(id=f"d{i}", isDeleted=True, body="") for i in range(600)]
    result = normalize(QuickCaptureStoreData(items=items))
    assert sum(1 for i in result.items if i.isDeleted) == 500


def test_deduplication_key_variants():
    assert deduplication_key(QuickCaptureItem(isDeleted=True, id="x")) == "deleted:x"
    assert deduplication_key(QuickCaptureItem(contentHash="abc")) == "abc"
    assert deduplication_key(QuickCaptureItem(type="Image", imagePath="/i.png")) == "image:/i.png"
    assert deduplication_key(QuickCaptureItem(body="plain")) == "plain"


def test_store_roundtrip_through_disk(store: QuickCaptureStore):
    data = QuickCaptureStoreData(items=[QuickCaptureItem(title="t", body="b")])
    store.save(data)
    loaded = store.load()
    assert loaded.items[0].title == "t"
    raw = json.loads(store.store_path.read_text())
    assert raw["version"] == 4


# ---- service records ---------------------------------------------------------


def test_add_items_newest_first_and_detects_links(service: QuickCaptureService):
    first = service.add_detailed_item(None, "plain note")
    second = service.add_detailed_item("A link", "https://example.com/page")
    data = service.get_data()
    assert [i.id for i in data.items] == [second.id, first.id]
    assert data.items[0].type == "Link" and data.items[0].url == "https://example.com/page"
    assert data.items[1].type == "Text"


def test_add_empty_raises(service: QuickCaptureService):
    with pytest.raises(ValueError):
        service.add_detailed_item(None, "   ")


def _pinned_view(service):
    # the Pinned tab sorts by PinnedSortOrder, not list order
    pinned = [i for i in service.get_data().items if i.isPinned]
    return sorted(pinned, key=lambda i: (i.pinnedSortOrder if i.pinnedSortOrder >= 0 else 0, i.sortOrder))


def test_pin_moves_item_to_front_of_pinned_order(service: QuickCaptureService):
    a = service.add_item("a")
    service.add_item("b")
    c = service.add_item("c")
    service.set_pinned(c.id, True)
    service.set_pinned(a.id, True)
    assert [i.id for i in _pinned_view(service)] == [a.id, c.id]
    assert service.move_pinned_item(a.id, 1) is True
    assert [i.id for i in _pinned_view(service)] == [c.id, a.id]


def test_delete_then_restore_resurrects_tombstone(service: QuickCaptureService):
    item = service.add_detailed_item("keep", "body text", "Mint")
    snapshot = service.delete_item(item.id)
    stored = service.get_data().items[0]
    assert stored.isDeleted and stored.body == "" and stored.id == item.id  # content stripped
    assert service.restore_deleted_item(snapshot) is True
    restored = next(i for i in service.get_data().items if i.id == item.id)
    assert not restored.isDeleted and restored.body == "body text" and restored.appearancePreset == "Mint"


def test_update_item_details_rejects_empty_and_keeps_preset(service: QuickCaptureService):
    item = service.add_detailed_item("t", "body")
    assert service.update_item_details(item.id, None, "   ").saved is False
    result = service.update_item_details(item.id, "T2", "new body", "Rose")
    assert result.saved and result.item.title == "T2" and result.item.body == "new body"
    assert result.item.appearancePreset == "Rose"


def test_clear_turns_everything_into_tombstones(service: QuickCaptureService):
    service.add_item("one")
    service.add_recent_clipboard_item("clip", 30)
    service.clear()
    data = service.get_data()
    assert data.items and all(i.isDeleted for i in data.items)
    assert data.recentItems and all(i.isDeleted for i in data.recentItems)
    assert all(not i.body for i in data.items)


# ---- recent list ------------------------------------------------------------


def test_recent_clipboard_dedupe_and_reorder(service: QuickCaptureService):
    assert service.add_recent_clipboard_item("one", 30) is not None
    assert service.add_recent_clipboard_item("two", 30) is not None
    assert service.add_recent_clipboard_item("two", 30) is None  # latest dedupe
    service.add_recent_clipboard_item("one", 30)  # re-capture moves to front
    assert [i.body for i in service.get_data().recentItems][:2] == ["one", "two"]


def test_recent_trim_respects_limit_but_keeps_tombstones(service: QuickCaptureService):
    for index in range(15):
        service.add_recent_clipboard_item(f"clip {index}", 10)
    service.delete_recent_item(service.get_data().recentItems[0].id)
    service.trim_recent_items(10)
    recent = service.get_data().recentItems
    live = [i for i in recent if not i.isDeleted]
    tombstones = [i for i in recent if i.isDeleted]
    assert len(live) <= 10
    assert len(tombstones) == 1  # trim cannot drop delete protection


def test_save_recent_item_to_records(service: QuickCaptureService):
    recent = service.add_recent_clipboard_item("https://example.com", 30)
    saved = service.save_recent_item_to_records(recent.id, pin=True)
    assert saved is not None
    assert saved.body == "https://example.com" and saved.type == "Link"
    assert saved.sourceKind == "Clipboard" and not saved.isRecent
    assert saved.isPinned


# ---- images ------------------------------------------------------------------


def _png(path: Path, size=(400, 200), color=(200, 30, 30)) -> str:
    from PIL import Image

    Image.new("RGB", size, color).save(path)
    return str(path)


def test_image_capture_hash_dedupe_and_thumbnail(
    service: QuickCaptureService, store: QuickCaptureStore, tmp_path: Path
):
    source = _png(tmp_path / "a.png")
    first = service.add_image_file_item(source)
    second = service.add_image_file_item(source)  # same bytes → same hash
    assert Path(second.imagePath) == Path(first.imagePath)
    assert first.contentHash == second.contentHash

    thumb = service.get_or_create_image_thumbnail_path(first.imagePath)
    assert thumb and Path(thumb).exists()
    from PIL import Image

    with Image.open(thumb) as t:
        assert t.size == (180, 90)  # max 180 on the long side

    info = service.get_image_cache_info()
    assert info.total_file_count >= 2  # image + thumbnail


def test_recent_image_dedupe_by_hash(service: QuickCaptureService, tmp_path: Path):
    source = _png(tmp_path / "b.png")
    payload = Path(source).read_bytes()
    first = service.add_recent_clipboard_image(payload, 30)
    assert first is not None and first.type == "Image" and first.body == "Image"
    assert service.add_recent_clipboard_image(payload, 30) is None  # same hash again
    other = _png(tmp_path / "c.png", color=(10, 10, 220))
    second = service.add_recent_clipboard_image(Path(other).read_bytes(), 30)
    assert second is not None


def test_unused_image_cache_cleanup_after_delete(service: QuickCaptureService, tmp_path: Path):
    source = _png(tmp_path / "d.png")
    item = service.add_image_file_item(source)
    assert Path(item.imagePath).exists()
    service.delete_item(item.id)
    result = service.cleanup_unused_image_cache()
    assert result.deleted_file_count >= 1
    assert not Path(item.imagePath).exists()


def test_build_image_export_file_name():
    from datetime import datetime, timezone

    stamp = datetime(2026, 9, 26, 1, 2, 3, tzinfo=timezone.utc)
    name = build_image_export_file_name("截图", stamp, "/x/abc.PNG")
    assert name == f"截图 {stamp.astimezone():%Y-%m-%d %H-%M-%S}.png"
    assert build_image_export_file_name(None, stamp, "/x/abc.png").startswith("Capture ")


# ---- attachments ------------------------------------------------------------


def test_add_item_with_attachments_links_and_copies(service: QuickCaptureService, tmp_path: Path):
    source = tmp_path / "note.txt"
    source.write_text("data", encoding="utf-8")
    linked = service.add_item_with_attachments([str(source)], copy_to_managed_storage=False)
    assert linked.sourceKind == "DragDrop"
    assert linked.attachments[0].filePath == str(source)
    assert linked.attachments[0].storageMode == "linked"

    copied = service.add_item_with_attachments([str(source)], copy_to_managed_storage=True)
    assert copied.attachments[0].filePath != str(source)
    assert Path(copied.attachments[0].filePath).read_text(encoding="utf-8") == "data"
    assert copied.attachments[0].storageMode == "managed"


def test_delete_attachment_promotes_next_image(service: QuickCaptureService, tmp_path: Path):
    one = _png(tmp_path / "one.png")
    two = _png(tmp_path / "two.png", color=(9, 99, 9))
    item = service.add_item_with_attachments([one, two], copy_to_managed_storage=True)
    assert item.type == "Image" and Path(item.imagePath).name == "one.png"
    first_attachment = item.attachments[0]
    updated = service.delete_attachment(item.id, first_attachment.id)
    assert Path(updated.imagePath).name == "two.png"
    assert len(updated.attachments) == 1


# ---- clipboard write scope ----------------------------------------------------


def test_clipboard_write_scope_roundtrip():
    clipboard_write_scope.reset()
    clipboard_write_scope.mark_text("  own write  ")
    assert clipboard_write_scope.should_ignore_text("own write") is True
    assert clipboard_write_scope.should_ignore_text("something else") is False
    clipboard_write_scope.reset()
    assert clipboard_write_scope.should_ignore_text("own write") is False


def test_service_marks_and_monitor_scope_skips(service: QuickCaptureService):
    clipboard_write_scope.reset()
    service.mark_clipboard_text_written_by_panebox("self copy")
    assert service.add_recent_clipboard_item("self copy", 30) is None
    assert service.add_recent_clipboard_item("other", 30) is not None
    clipboard_write_scope.reset()


# ---- markdown renderer -------------------------------------------------------


def test_markdown_renders_core_syntax():
    markup = markdown_render.to_pango_markup("# Head\n\n**bold** *it* `code`\n\n- [x] done\n- [ ] todo")
    assert "weight='bold'" in markup and "Head" in markup
    assert "<b>bold</b>" in markup and "<i>it</i>" in markup
    assert "font_family='monospace'" in markup
    assert "☑ done" in markup and "☐ todo" in markup


def test_markdown_escapes_markup_and_renders_links():
    markup = markdown_render.to_pango_markup('<b> & "q"\n\n[site](https://example.com)')
    assert "&lt;b&gt;" in markup and "&amp;" in markup
    assert "<a href='https://example.com'>site</a>" in markup


def test_markdown_plain_text_strips_syntax():
    assert markdown_render.plain_text("# Title\n**bold** [x](https://e.com)") == "Title\nbold x"
