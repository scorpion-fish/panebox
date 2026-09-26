"""Desktop-organization pure logic: classifier, scanner, rules, planner,
placement, preview (port of the PaneBox organize pipeline)."""

from __future__ import annotations

import os

from panebox.models.settings_slices import DesktopOrganizationRule
from panebox.models.widget_config import WidgetConfig
from panebox.services import organize_planner as op
from panebox.services.organize_models import (
    CategoryIds,
    DestinationMode,
    ExclusionReason,
    FileSnapshot,
    Plan,
    Rect,
    SourceScope,
    SubtypeIds,
    TargetPlan,
    TargetSelection,
)


# ---- classifier ---------------------------------------------------------------


def test_classify_covers_all_categories():
    cases = {
        "a.pdf": (CategoryIds.DOCUMENTS, SubtypeIds.PDF),
        "b.docx": (CategoryIds.DOCUMENTS, SubtypeIds.WORD),
        "c.xlsx": (CategoryIds.DOCUMENTS, SubtypeIds.EXCEL),
        "d.pptx": (CategoryIds.DOCUMENTS, SubtypeIds.POWERPOINT),
        "e.txt": (CategoryIds.DOCUMENTS, SubtypeIds.TEXT),
        "app.desktop": (CategoryIds.SHORTCUTS, None),
        "link.lnk": (CategoryIds.SHORTCUTS, None),
        "photo.png": (CategoryIds.IMAGES, None),
        "song.mp3": (CategoryIds.MEDIA, SubtypeIds.AUDIO),
        "clip.mp4": (CategoryIds.MEDIA, SubtypeIds.VIDEO),
        "pack.deb": (CategoryIds.PACKAGES, None),
        "setup.exe": (CategoryIds.PACKAGES, None),
        "weird.xyz": (CategoryIds.OTHER, None),
    }
    for name, (category, subtype) in cases.items():
        result = op.classify(name)
        assert result.category_id == category, name
        assert result.subtype_id == subtype, name


def test_normalize_extension():
    assert op.normalize_extension(None) == ""
    assert op.normalize_extension("PDF") == ".PDF"  # case preserved; classify lowercases
    assert op.normalize_extension(".jpg") == ".jpg"


# ---- scanner --------------------------------------------------------------------


def _write(path: str, content: bytes = b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def test_scan_excludes_hidden_temp_links_folders_and_slow(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    _write(str(desktop / "a.txt"))
    _write(str(desktop / ".secret"))
    _write(str(desktop / "desktop.ini"))
    _write(str(desktop / "big.iso"), b"y")
    os.symlink(str(desktop / "a.txt"), str(desktop / "alias.txt"))
    (desktop / "folder").mkdir()
    monkeypatch.setattr(op, "SLOW_ITEM_THRESHOLD_BYTES", 0)  # every file is slow

    result = op.DesktopOrganizationScanner(lambda: str(desktop)).scan()

    reasons = {item.name: item.exclusion_reason for item in result.items}
    assert reasons["a.txt"] == ExclusionReason.SLOW_ITEM
    assert reasons[".secret"] == ExclusionReason.HIDDEN_OR_SYSTEM
    assert reasons["desktop.ini"] == ExclusionReason.HIDDEN_OR_SYSTEM
    assert reasons["alias.txt"] == ExclusionReason.REPARSE_POINT
    assert reasons["folder"] == ExclusionReason.FOLDER
    assert reasons["big.iso"] == ExclusionReason.SLOW_ITEM
    assert result.public_desktop_unavailable  # no public desktop on Linux
    assert result.desktop_path == str(desktop)


def test_scan_quick_batch_limit_smallest_first(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(op, "QUICK_BATCH_SIZE_LIMIT_BYTES", 4)
    monkeypatch.setattr(op, "QUICK_BATCH_ITEM_LIMIT", 200)
    for name, size in (("a.txt", 1), ("b.txt", 2), ("c.txt", 4)):
        _write(str(desktop / name), b"z" * size)

    result = op.DesktopOrganizationScanner(lambda: str(desktop)).scan()

    reasons = {item.name: item.exclusion_reason for item in result.items}
    assert reasons["a.txt"] == ExclusionReason.NONE
    assert reasons["b.txt"] == ExclusionReason.NONE
    assert reasons["c.txt"] == ExclusionReason.BATCH_LIMIT  # would exceed 4 bytes


def test_scan_eligible_snapshot_has_exact_identity_anchors(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    target = desktop / "a.txt"
    _write(str(target), b"hello")

    result = op.DesktopOrganizationScanner(lambda: str(desktop)).scan()

    item = next(i for i in result.items if i.name == "a.txt")
    assert item.size == 5
    assert item.mtime_ns == os.stat(target).st_mtime_ns
    assert item.source_scope == SourceScope.PERSONAL


# ---- rule resolver -----------------------------------------------------------------


def _widget(widget_id: str, folder: str) -> WidgetConfig:
    return WidgetConfig(id=widget_id, name=widget_id, widgetKind="File", mappedFolderPath=folder)


def test_resolve_rule_ranks_extension_over_subtype_over_category(tmp_path):
    widgets = [
        _widget("ext", str(tmp_path / "e")),
        _widget("sub", str(tmp_path / "s")),
        _widget("cat", str(tmp_path / "c")),
    ]
    rules = [
        DesktopOrganizationRule(id="r1", targetWidgetId="cat", categoryIds=[CategoryIds.DOCUMENTS]),
        DesktopOrganizationRule(id="r2", targetWidgetId="sub", subtypeIds=[SubtypeIds.PDF]),
        DesktopOrganizationRule(id="r3", targetWidgetId="ext", extensions=[".pdf"]),
    ]
    item = type(
        "Item",
        (),
        {
            "extension": ".pdf",
            "subtype_id": SubtypeIds.PDF,
            "category_id": CategoryIds.DOCUMENTS,
        },
    )()

    assert op.resolve_rule(item, rules, widgets).id == "r3"
    rules = [r for r in rules if r.id != "r3"]
    assert op.resolve_rule(item, rules, widgets).id == "r2"
    rules = [r for r in rules if r.id != "r2"]
    assert op.resolve_rule(item, rules, widgets).id == "r1"


def test_resolve_rule_skips_disabled_excluded_and_widgetless():
    widgets = [_widget("w1", "/tmp/x")]
    item = type("Item", (), {"extension": ".pdf", "subtype_id": None, "category_id": CategoryIds.DOCUMENTS})()
    disabled = DesktopOrganizationRule(
        id="d", targetWidgetId="w1", categoryIds=[CategoryIds.DOCUMENTS], isEnabled=False
    )
    assert op.resolve_rule(item, [disabled], widgets) is None

    blocked = DesktopOrganizationRule(
        id="b",
        targetWidgetId="w1",
        categoryIds=[CategoryIds.DOCUMENTS],
        excludedExtensions=[".pdf"],
    )
    assert op.resolve_rule(item, [blocked], widgets) is None

    orphan = DesktopOrganizationRule(id="o", targetWidgetId="gone", categoryIds=[CategoryIds.DOCUMENTS])
    no_folder = [_widget("nf", "")]
    assert op.resolve_rule(item, [orphan], widgets) is None
    assert (
        op.resolve_rule(
            item,
            [DesktopOrganizationRule(id="nf", targetWidgetId="nf", categoryIds=[CategoryIds.DOCUMENTS])],
            no_folder,
        )
        is None
    )


def test_find_rule_conflicts_and_exclusive_assignment():
    rules = [
        DesktopOrganizationRule(id="a", targetWidgetId="w1", extensions=[".pdf"], categoryIds=[CategoryIds.DOCUMENTS]),
        DesktopOrganizationRule(id="b", targetWidgetId="w2", extensions=[".pdf"]),
    ]
    conflicts = op.find_rule_conflicts(rules)
    assert ("Extension", ".pdf", ["w1", "w2"]) in conflicts

    op.assign_extension_exclusively(rules, "w1", ".pdf")
    assert op.find_rule_conflicts(rules) == []
    kept = next(r for r in rules if r.id == "b")
    assert kept.extensions == []
    owner = next(r for r in rules if r.id == "a")
    assert ".pdf" in owner.extensions


# ---- planner -------------------------------------------------------------------------


def _scan_with(desktop, **scan_kwargs):
    return op.DesktopOrganizationScanner(lambda: str(desktop)).scan(**scan_kwargs)


def test_plan_merges_small_categories_into_other(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    storage = tmp_path / "storage"
    _write(str(desktop / "a.txt"))  # Documents (1)
    _write(str(desktop / "b.pdf"))  # Documents (2)
    _write(str(desktop / "one.png"))  # Images (1) -> merged into Other

    plan = op.create_plan(_scan_with(desktop), str(storage), [], [], lambda cid: cid)

    categories = {t.category_id for t in plan.targets}
    assert categories == {CategoryIds.DOCUMENTS, CategoryIds.OTHER}
    other = next(t for t in plan.targets if t.category_id == CategoryIds.OTHER)
    assert {i.name for i in other.items} == {"one.png"}
    assert other.creates_widget
    assert other.source_bucket_id == f"category:{CategoryIds.OTHER}"
    assert other.target_directory_path.startswith(str(storage))


def test_plan_caps_new_widget_count(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    storage = tmp_path / "storage"
    names = ["a.txt", "b.txt", "one.png", "two.png", "song.mp3", "clip.mp4", "p.deb", "q.rpm"]
    for name in names:
        _write(str(desktop / name))

    plan = op.create_plan(_scan_with(desktop), str(storage), [], [], lambda cid: cid)

    # 5 content categories exist but only MAX_NEW_WIDGET_COUNT widgets appear.
    assert plan.new_widget_count <= op.MAX_NEW_WIDGET_COUNT == 4
    assert sum(len(t.items) for t in plan.targets) == len(names)


def test_plan_routes_rules_to_existing_widget_with_reserved_directory(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    storage = tmp_path / "storage"
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    _write(str(desktop / "a.txt"))
    _write(str(desktop / "c.pdf"))
    widgets = [_widget("w1", str(existing_dir))]
    rules = [DesktopOrganizationRule(id="r", targetWidgetId="w1", categoryIds=[CategoryIds.DOCUMENTS])]

    plan = op.create_plan(_scan_with(desktop), str(storage), widgets, rules, lambda cid: cid)

    assert len(plan.targets) == 1
    target = plan.targets[0]
    assert target.target_widget_id == "w1"
    assert not target.creates_widget
    assert target.target_directory_path == str(existing_dir)
    assert {i.name for i in target.items} == {"a.txt", "c.pdf"}


def test_create_retry_plan_drops_live_widgets_and_filters_items(tmp_path):
    desktop = tmp_path / "Desktop"
    _scan = op.ScanResult(desktop_path=str(desktop))
    from datetime import datetime, timezone

    def snap(name):
        return FileSnapshot(
            source_path=str(desktop / name),
            name=name,
            extension=".txt",
            size=1,
            last_write_time_utc=datetime.now(timezone.utc),
            category_id=CategoryIds.DOCUMENTS,
            subtype_id=None,
        )

    plan = Plan(
        desktop_path=str(desktop),
        storage_root_path=str(tmp_path),
        targets=[
            TargetPlan(
                source_bucket_id="category:Documents",
                category_id=CategoryIds.DOCUMENTS,
                target_widget_id="new1",
                suggested_display_name="Documents",
                target_directory_path=str(tmp_path / "Documents"),
                creates_widget=True,
                items=[snap("a.txt"), snap("b.txt")],
            )
        ],
    )

    retry = op.create_retry_plan(plan, {str(desktop / "b.txt")}, [])  # widget "new1" not live
    assert retry.targets[0].creates_widget  # not live -> still creates
    assert [i.name for i in retry.targets[0].items] == ["b.txt"]

    retry = op.create_retry_plan(plan, {str(desktop / "b.txt")}, [_widget("new1", str(tmp_path))])
    assert not retry.targets[0].creates_widget  # live -> route into it


# ---- placement ------------------------------------------------------------------------


def test_assign_planned_bounds_slots_and_falls_back():
    def target(name):
        return TargetPlan(
            source_bucket_id=f"category:{name}",
            target_widget_id=name,
            creates_widget=True,
        )

    plan = Plan(targets=[target("a"), target("b")])
    work = Rect(0.0, 0.0, 1280.0, 800.0)
    occupied = [Rect(0.0, 0.0, 280.0, 400.0)]

    assert op.assign_planned_bounds(plan, work, occupied, 280.0, 400.0)
    first, second = plan.targets[0].planned_bounds, plan.targets[1].planned_bounds
    # row-major scan: the occupied 0..280 column is skipped within row y=16
    assert (first.x, first.y) == (308.0, 16.0)
    assert (second.x, second.y) == (600.0, 16.0)


def test_assign_planned_bounds_unplaceable_clears_all():
    plan = Plan(
        targets=[
            TargetPlan(
                source_bucket_id="a",
                target_widget_id="a",
                creates_widget=True,
                planned_bounds=Rect(1, 1, 1, 1),
            )
        ]
    )
    tiny = Rect(0.0, 0.0, 50.0, 50.0)
    assert not op.assign_planned_bounds(plan, tiny, [], 280.0, 400.0)
    assert plan.targets[0].planned_bounds is None


# ---- preview ----------------------------------------------------------------------------


def test_preview_summary_counts_and_destination_keys(tmp_path):
    desktop = tmp_path / "Desktop"
    _scan = op.ScanResult(desktop_path=str(desktop), items=[])
    from datetime import datetime, timezone

    def snap(name, scope=SourceScope.PERSONAL):
        return FileSnapshot(
            source_path=str(desktop / name),
            name=name,
            extension=".txt",
            size=1,
            last_write_time_utc=datetime.now(timezone.utc),
            category_id=CategoryIds.DOCUMENTS,
            subtype_id=None,
            source_scope=scope,
        )

    items = [snap("a.txt"), snap("b.txt")]
    plan = Plan(
        desktop_path=str(desktop),
        source_items=items,
        targets=[
            TargetPlan(
                source_bucket_id="category:Documents",
                target_widget_id="n1",
                creates_widget=True,
                items=items,
            )
        ],
    )
    selections = {"category:Documents": TargetSelection(source_bucket_id="category:Documents")}

    total, selected, retained, destinations, new = op.preview_summary(plan, selections, set())
    assert (total, selected, retained, destinations, new) == (2, 2, 0, 1, 1)

    total, selected, retained, destinations, new = op.preview_summary(plan, selections, {str(desktop / "b.txt")})
    assert (selected, retained, new) == (1, 1, 1)

    rerouted = {
        "category:Documents": TargetSelection(
            source_bucket_id="category:Documents",
            destination_mode=DestinationMode.EXISTING_WIDGET,
            existing_widget_id="w9",
        )
    }
    total, selected, retained, destinations, new = op.preview_summary(plan, rerouted, set())
    assert (destinations, new) == (1, 0)

    deselected = {"category:Documents": TargetSelection(source_bucket_id="category:Documents", is_selected=False)}
    total, selected, retained, destinations, new = op.preview_summary(plan, deselected, set())
    assert (selected, retained, destinations) == (0, 2, 0)


def test_reconcile_preview_never_auto_selects_new_files(tmp_path):
    desktop = tmp_path / "Desktop"

    def snap(name, reason=ExclusionReason.NONE):
        return FileSnapshot(
            source_path=str(desktop / name),
            name=name,
            extension=".txt",
            size=1,
            last_write_time_utc=None,
            category_id=CategoryIds.DOCUMENTS,
            subtype_id=None,
            exclusion_reason=reason,
        )

    previous = [snap("a.txt"), snap("folder", ExclusionReason.FOLDER)]
    current = [
        snap("a.txt"),
        snap("folder", ExclusionReason.FOLDER),
        snap("new.txt"),
        snap("slow.bin", ExclusionReason.SLOW_ITEM),
    ]

    excluded, optional, marked_new, added, removed = op.reconcile_preview(
        previous, current, {str(desktop / "folder")}, {str(desktop / "slow.bin")}, set()
    )

    assert added == 2 and removed == 0  # new.txt AND slow.bin are discoveries
    assert str(desktop / "new.txt").lower() in excluded  # silently excluded
    assert str(desktop / "new.txt").lower() in marked_new  # and flagged as new
    assert str(desktop / "slow.bin").lower() in optional  # opt-in retained
    assert str(desktop / "a.txt").lower() not in excluded
