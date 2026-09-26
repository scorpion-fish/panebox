"""File-stack grouping + projection + popover layout unit tests (no GUI)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from panebox.services import stack_grouping as sg
from panebox.services.file_sort import FileEntry


def entry(name: str, *, folder=False, mtime=1000.0) -> FileEntry:
    path = f"/tmp/widget/{name}"
    extension = "" if folder else ("." + name.rsplit(".", 1)[1] if "." in name else "")
    return FileEntry(
        path=path,
        name=name,
        is_folder=folder,
        last_modified=mtime,
        extension=extension,
    )


# ---- categories -------------------------------------------------------------------


def test_kind_categories_match_extension_tables():
    cases = {
        "a.txt": sg.DOCUMENTS,
        "photo.PNG": sg.IMAGES,
        "clip.mp4": sg.VIDEOS,
        "song.flac": sg.AUDIO,
        "bundle.tar": sg.ARCHIVES,
        "app.desktop": sg.APPLICATIONS,
        "tool.AppImage": sg.APPLICATIONS,
        "setup.exe": sg.APPLICATIONS,
        "weird.xyz": sg.OTHER,
    }
    for name, expected in cases.items():
        assert sg.resolve_category(entry(name), "Kind") == expected, name


def test_folders_and_shortcuts_sort_first():
    assert sg.resolve_category(entry("stuff", folder=True), "Kind") == sg.FOLDERS
    shortcut = entry("link.desktop")
    shortcut.is_shortcut = True
    assert sg.resolve_category(shortcut, "Kind") == sg.APPLICATIONS


def test_date_modified_buckets():
    now = datetime(2026, 9, 26, 12, 0)
    today = entry("a.txt", mtime=now.timestamp())
    yesterday = entry("b.txt", mtime=(now - timedelta(days=1, hours=1)).timestamp())
    last_week = entry("c.txt", mtime=(now - timedelta(days=5)).timestamp())
    last_month = entry("d.txt", mtime=(now - timedelta(days=20)).timestamp())
    old = entry("e.txt", mtime=(now - timedelta(days=200)).timestamp())
    missing = entry("f.txt", mtime=0.0)

    buckets = (
        (today, sg.TODAY),
        (yesterday, sg.YESTERDAY),
        (last_week, sg.PREVIOUS_SEVEN_DAYS),
        (last_month, sg.PREVIOUS_THIRTY_DAYS),
        (old, sg.EARLIER),
        (missing, sg.EARLIER),
    )
    for item, expected in buckets:
        assert sg.resolve_category(item, "DateModified", now=now) == expected, item.name


def test_date_added_uses_tracking_map():
    now = datetime(2026, 9, 26, 12, 0)
    a = entry("a.txt")
    b = entry("b.txt")
    added = {
        a.path: (now - timedelta(days=3)).isoformat(),
    }
    assert sg.resolve_category(a, "DateAdded", now=now, added_at_by_path=added) == sg.PREVIOUS_SEVEN_DAYS
    assert sg.resolve_category(b, "DateAdded", now=now, added_at_by_path=added) == sg.EARLIER


# ---- group() ------------------------------------------------------------------------


def test_groups_follow_category_order():
    groups = sg.group(
        [entry("z.zip"), entry("a.txt"), entry("dir", folder=True), entry("x.mp3")],
        group_by="Kind",
    )
    assert [g.category for g in groups] == [sg.FOLDERS, sg.DOCUMENTS, sg.AUDIO, sg.ARCHIVES]
    assert all(g.stack_key is None and g.can_stack for g in groups)


def test_member_ordering_modes():
    items = [entry("b.txt", mtime=5.0), entry("A.txt", mtime=9.0), entry("c.txt", mtime=7.0)]
    by_widget = sg.group(items, "Kind")
    assert [e.name for e in by_widget[0].items] == ["b.txt", "A.txt", "c.txt"]

    by_name = sg.group(items, "Kind", order_by="Name")
    assert [e.name for e in by_name[0].items] == ["A.txt", "b.txt", "c.txt"]

    by_modified = sg.group(items, "Kind", order_by="DateModified")
    assert [e.name for e in by_modified[0].items] == ["A.txt", "c.txt", "b.txt"]

    added = {
        items[0].path: "2026-01-03T00:00:00",
        items[1].path: "2026-01-01T00:00:00",
        items[2].path: "2026-01-02T00:00:00",
    }
    by_added = sg.group(items, "Kind", order_by="DateAdded", added_at_by_path=added)
    assert [e.name for e in by_added[0].items] == ["b.txt", "c.txt", "A.txt"]


def test_custom_rules_first_match_with_loose_unmatched():
    rules = [
        {"id": "r1", "name": "Media", "extensions": ["mp4", "*.mkv"]},
        {"id": "r2", "name": "", "extensions": [".txt"]},
    ]
    items = [entry("a.mp4"), entry("b.mkv"), entry("c.txt"), entry("d.pdf")]
    groups = sg.group(items, "Custom", custom_rules=rules, unmatched_behavior="KeepLoose")
    assert [g.stack_key for g in groups][:2] == ["Custom:r1", "Custom:r2"]
    assert groups[0].display_name == "Media"
    assert groups[1].display_name == ".txt"  # blank name → extension list
    # KeepLoose: each unmatched item becomes its own unstackable group.
    loose = [g for g in groups if not g.can_stack]
    assert len(loose) == 1 and loose[0].items[0].name == "d.pdf"

    merged = sg.group(items, "Custom", custom_rules=rules, unmatched_behavior="Other")
    assert merged[-1].stack_key == "Custom:Other"
    assert merged[-1].items[0].name == "d.pdf"


def test_date_group_ordering_in_group_result():
    now = datetime(2026, 9, 26, 12, 0)
    items = [
        entry("old.txt", mtime=(now - timedelta(days=90)).timestamp()),
        entry("new.txt", mtime=now.timestamp()),
        entry("week.txt", mtime=(now - timedelta(days=4)).timestamp()),
    ]
    groups = sg.group(items, "DateModified", now=now)
    assert [g.category for g in groups] == [sg.TODAY, sg.PREVIOUS_SEVEN_DAYS, sg.EARLIER]


# ---- normalizers ---------------------------------------------------------------------


def test_normalize_group_by_domains():
    assert sg.normalize_group_by("datecreated") == "DateAdded"
    assert sg.normalize_group_by("DATEMODIFIED") == "DateModified"
    assert sg.normalize_group_by("custom") == "Custom"
    assert sg.normalize_group_by("bogus") == "Kind"
    assert sg.normalize_group_by(None) == "Kind"


def test_normalize_threshold_only_allows_235():
    assert sg.normalize_threshold(2) == 2
    assert sg.normalize_threshold(5) == 5
    assert sg.normalize_threshold(4) == 3
    assert sg.normalize_threshold("nope") == 3
    assert sg.normalize_threshold(None) == 3


def test_normalize_open_mode_and_layouts():
    assert sg.normalize_open_mode("popover") == "Popover"
    assert sg.normalize_open_mode("Popover") == "Popover"
    assert sg.normalize_open_mode("inline") == "Inline"
    assert sg.normalize_open_mode("zzz") == "Inline"
    assert sg.normalize_popover_layout("Grid5") == "Grid5"
    assert sg.normalize_popover_layout("grid3") == "Adaptive"  # case-sensitive like C#
    assert sg.normalize_unmatched_behavior("other") == "Other"
    assert sg.normalize_unmatched_behavior("keeploose") == "KeepLoose"


def test_normalize_extensions_strips_globs_and_dedupes():
    assert sg.normalize_extensions(["mp3", "*.MP3", ".txt", "x", "", "*.a*b"]) == [
        ".mp3",
        ".txt",
        ".x",
    ]


# ---- projection ----------------------------------------------------------------------


def _projection_entries():
    return [entry(f"n{i}.txt") for i in range(4)] + [entry("pic.png"), entry("clip.mp4")]


def test_threshold_collapses_stacks():
    items = _projection_entries()  # 4 documents, 1 image, 1 video
    projection = sg.project(items, stacks_enabled=True, auto_stacking=True, group_by="Kind", threshold=3)
    keys = [u.order_key for u in projection.units]
    assert sg.DOCUMENTS in keys  # 4 docs ≥ 3 → stack tile
    assert sg.IMAGES not in keys  # single image stays loose
    stack = next(u for u in projection.units if u.is_stack)
    assert len(stack.stack.items) == 4
    loose = [u for u in projection.units if not u.is_stack]
    assert len(loose) == 2


def test_disabled_groups_project_loose():
    items = _projection_entries()
    customizations = sg.StackCustomizations(disabled={sg.DOCUMENTS})
    projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3, customizations=customizations)
    assert all(not u.is_stack for u in projection.units)


def test_expanded_stack_interleaves_members():
    items = _projection_entries()
    projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3, expanded_key=sg.DOCUMENTS)
    assert projection.expanded_key == sg.DOCUMENTS
    visible = projection.visible_units
    stack_index = next(i for i, u in enumerate(visible) if u.is_stack)
    children = [u for u in visible if u.child_of == sg.DOCUMENTS]
    assert len(children) == 4
    # Children immediately follow the tile.
    assert all(visible[stack_index + 1 + i].child_of == sg.DOCUMENTS for i in range(4))
    assert projection.current_order() == [u.order_key for u in projection.units]


def test_stale_expanded_key_clears():
    projection = sg.project(
        [entry("only.txt")],
        stacks_enabled=True,
        group_by="Kind",
        threshold=3,
        expanded_key=sg.DOCUMENTS,
    )
    assert projection.expanded_key is None


def test_persisted_order_reorders_units():
    items = [entry("a.txt"), entry("b.png"), entry("c.mp4")]
    customizations = sg.StackCustomizations(order=[sg.AUDIO, "nope"])
    projection = sg.project(items, stacks_enabled=True, group_by="Kind", customizations=customizations)
    # Unknown keys dropped; known ones first, the rest keep group order.
    keys = [u.order_key for u in projection.units]
    assert keys[0] != sg.AUDIO  # audio group doesn't exist here
    assert len(keys) == 3


def test_stacks_disabled_projects_all_loose():
    items = _projection_entries()
    projection = sg.project(items, stacks_enabled=False)
    assert all(not u.is_stack for u in projection.units)
    assert projection.expanded_key is None


def test_auto_stacking_off_keeps_loose_but_manuals_survive():
    items = _projection_entries()
    members = [items[0].path, items[1].path]
    customizations = sg.StackCustomizations(member_overrides={"Manual:abc": members}, order=["Manual:abc"])
    projection = sg.project(items, stacks_enabled=True, auto_stacking=False, customizations=customizations)
    manual = next(u for u in projection.units if u.is_stack)
    assert manual.is_manual and len(manual.stack.items) == 2  # force-stacked ≥2


# ---- manual operations ---------------------------------------------------------------


def _items_in_tmp_dir():
    with TemporaryDirectory() as tmp:
        root = str(Path(tmp).resolve())
        names = ["a.txt", "b.txt", "c.txt", "d.png"]
        items = []
        for index, name in enumerate(names):
            path = str(Path(root) / name)
            items.append(FileEntry(path=path, name=name, extension="." + name.rsplit(".", 1)[1], sort_order=index))
        yield items, root


def test_create_manual_stack_inserts_at_first_member():
    for items, root in _items_in_tmp_dir():
        projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3)
        customizations = sg.StackCustomizations()
        key = sg.create_manual_stack(items, [items[1].path, items[2].path], customizations, projection)
        assert key and key.startswith("Manual:")
        assert customizations.member_overrides[key] == [items[1].path, items[2].path]
        # Members lived inside the Documents stack (slot 0) → inserted right after it;
        # the other loose units (d.png) keep their slots.
        assert customizations.order[:2] == [sg.DOCUMENTS, key]
        assert customizations.order[-1].startswith("Item:")

        projection2 = sg.project(
            items, stacks_enabled=True, group_by="Kind", threshold=3, customizations=customizations
        )
        manual = next(u for u in projection2.units if u.order_key == key)
        assert manual.is_manual and len(manual.stack.items) == 2
        # The remaining single document falls below the threshold → loose again.
        assert sg.DOCUMENTS not in [u.order_key for u in projection2.units if u.is_stack]


def test_create_manual_stack_requires_two():
    for items, _root in _items_in_tmp_dir():
        projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3)
        assert sg.create_manual_stack(items, [items[0].path], sg.StackCustomizations(), projection) is None


def test_convert_to_manual_keeps_name_and_expansion():
    docs = [entry(f"d{i}.txt") for i in range(3)]
    items = docs + [entry("x.png")]
    projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3)
    customizations = sg.StackCustomizations(name_overrides={sg.DOCUMENTS: "My Papers"})
    key = sg.convert_to_manual(sg.DOCUMENTS, items, customizations, projection, expanded=True)
    assert key and key.startswith("Manual:")
    assert customizations.name_overrides[key] == "My Papers"

    projection2 = sg.project(
        items,
        stacks_enabled=True,
        group_by="Kind",
        threshold=3,
        customizations=customizations,
        expanded_key=key,
    )
    manual = next(u for u in projection2.units if u.order_key == key)
    assert manual.stack_name == "My Papers" and manual.expanded


def test_add_items_converts_auto_stack_to_manual():
    items = [entry(f"d{i}.txt") for i in range(3)] + [entry("x.png")]
    projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3)
    customizations = sg.StackCustomizations()
    assert sg.add_items_to_stack(sg.DOCUMENTS, items, customizations, projection, [items[3].path])
    manuals = [k for k in customizations.member_overrides if k.startswith("Manual:")]
    assert len(manuals) == 1
    assert len(customizations.member_overrides[manuals[0]]) == 4


def test_remove_items_dissolves_small_stack():
    items = [entry(f"d{i}.txt") for i in range(3)]
    projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3)
    customizations = sg.StackCustomizations()
    # Remove 2 of 3 → under two members → group disabled (auto stack).
    assert sg.remove_items_from_stack(sg.DOCUMENTS, items, customizations, projection, [items[0].path, items[1].path])
    assert sg.DOCUMENTS in customizations.disabled

    # Remove 1 of 3 → converts to manual with 2 members.
    customizations = sg.StackCustomizations()
    assert sg.remove_items_from_stack(sg.DOCUMENTS, items, customizations, projection, [items[0].path])
    assert any(len(m) == 2 for m in customizations.member_overrides.values())


def test_dissolve_manual_drops_customization():
    items = [entry(f"d{i}.txt") for i in range(3)]
    projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3)
    customizations = sg.StackCustomizations()
    key = sg.create_manual_stack(items, [items[0].path, items[1].path], customizations, projection)
    assert key
    with_manual = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=3, customizations=customizations)
    assert sg.dissolve_stack(key, customizations, with_manual)
    assert not customizations.member_overrides and key not in customizations.order


def test_move_stack_up_down():
    items = [
        entry("a1.txt"),
        entry("a2.txt"),
        entry("b1.png"),
        entry("b2.png"),
        entry("c1.mp3"),
        entry("c2.mp3"),
    ]
    projection = sg.project(items, stacks_enabled=True, group_by="Kind", threshold=2)
    customizations = sg.StackCustomizations(order=projection.current_order())
    audio_index = customizations.order.index(sg.AUDIO)
    assert audio_index > 0
    assert sg.move_stack(sg.AUDIO, -1, customizations, projection)
    assert customizations.order.index(sg.AUDIO) == audio_index - 1
    assert not sg.move_stack(sg.AUDIO, -99, customizations, projection)


# ---- popover layout --------------------------------------------------------------------


def test_grid3_does_not_pad_empty_rows():
    from panebox.services import stack_popover as sp

    three = sp.calculate_layout(
        is_list_mode=False,
        item_count=3,
        widget_width=300,
        work_area_width=1920,
        work_area_height=1040,
        item_width=96,
        item_height=112,
        layout_mode="Grid3",
    )
    assert three.columns == 3 and three.visible_rows == 1  # 3×1, not 3×3
    assert not three.has_vertical_overflow

    ten = sp.calculate_layout(
        is_list_mode=False,
        item_count=10,
        widget_width=300,
        work_area_width=1920,
        work_area_height=1040,
        item_width=96,
        item_height=112,
        layout_mode="Grid3",
    )
    assert ten.columns == 3 and ten.visible_rows == 3
    assert ten.has_vertical_overflow  # 4 total rows

    grid5 = sp.calculate_layout(
        is_list_mode=False,
        item_count=30,
        widget_width=300,
        work_area_width=1920,
        work_area_height=1040,
        item_width=96,
        item_height=112,
        layout_mode="Grid5",
    )
    assert grid5.columns == 5


def test_adaptive_column_breakpoints():
    from panebox.services import stack_popover as sp

    def columns_for(count):
        return sp.calculate_layout(
            is_list_mode=False,
            item_count=count,
            widget_width=300,
            work_area_width=1920,
            work_area_height=1040,
            item_width=96,
            item_height=112,
            layout_mode="Adaptive",
        ).columns

    assert columns_for(2) == 2
    assert columns_for(3) == 3
    assert columns_for(4) == 2
    assert columns_for(9) == 3
    assert columns_for(16) == 4
    assert columns_for(25) == 5
    assert columns_for(26) == 6


def test_popover_capped_by_work_area():
    from panebox.services import stack_popover as sp

    layout = sp.calculate_layout(
        is_list_mode=False,
        item_count=400,
        widget_width=300,
        work_area_width=1920,
        work_area_height=1040,
        item_width=96,
        item_height=112,
    )
    assert layout.width <= 720 and layout.height <= 720
    assert layout.has_vertical_overflow

    tiny = sp.calculate_layout(
        is_list_mode=False,
        item_count=9,
        widget_width=300,
        work_area_width=300,
        work_area_height=260,
        item_width=96,
        item_height=112,
    )
    assert tiny.width >= 180 and tiny.height >= 160
    assert tiny.visible_rows >= 1


def test_list_mode_widths_and_rows():
    from panebox.services import stack_popover as sp

    few = sp.calculate_layout(
        is_list_mode=True,
        item_count=3,
        widget_width=320,
        work_area_width=1920,
        work_area_height=1040,
        item_width=0,
        item_height=48,
    )
    assert few.columns == 1 and few.visible_rows == 3 and not few.has_vertical_overflow
    assert 280 <= few.width <= 560

    many = sp.calculate_layout(
        is_list_mode=True,
        item_count=50,
        widget_width=320,
        work_area_width=1920,
        work_area_height=1040,
        item_width=0,
        item_height=48,
    )
    assert many.visible_rows == 8 and many.has_vertical_overflow


def test_position_clamps_only_at_edges():
    from panebox.services import stack_popover as sp

    centered = sp.calculate_position(960, 400, 400, 300, 0, 0, 1920, 1040)
    assert centered.left == 960 - 200 and centered.top == 400 - 150
    assert not centered.is_horizontally_clamped and not centered.is_vertically_clamped

    clamped = sp.calculate_position(30, 20, 400, 300, 0, 0, 1920, 1040)
    assert clamped.left == 8 and clamped.top == 8  # work-area margin
    assert clamped.is_horizontally_clamped and clamped.is_vertically_clamped

    tiny_area = sp.calculate_position(50, 50, 400, 300, 0, 0, 100, 100)
    assert tiny_area.left == (100 - 400) / 2  # centered fallback
