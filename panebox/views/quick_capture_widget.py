"""Quick Capture widget surface (port of QuickCaptureSurfaceContent + the
QuickCaptureWidgetWindow detail flows).

Three views over one shared service: Records (all saved notes), Pinned
(pinned subset in pinned order), Recent (read-only clipboard history).
Single-pane push detail below 720px, dual pane above it (680 exit,
MasterDetailLayoutPolicy). Paper presets tint the cards; Markdown notes get
a rendered preview via services.markdown_render.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from ..i18n import fmt, t
from ..models.quick_capture import (
    QuickCaptureAppearancePreset,
    QuickCaptureItem,
    QuickCaptureItemType,
    QuickCaptureViewMode,
    TextContentFormat,
)
from ..platform.resize_hook import ResizeHook
from ..services import markdown_render
from ..services.quick_capture_service import QuickCaptureService

DUAL_PANE_ENTER_WIDTH = 720
DUAL_PANE_EXIT_WIDTH = 680

VIEW_TAB_KEYS = {
    QuickCaptureViewMode.RECORDS: "QuickCapture.Tab.Records",
    QuickCaptureViewMode.PINNED: "QuickCapture.Tab.Pinned",
    QuickCaptureViewMode.RECENT: "QuickCapture.Tab.Recent",
}

EMPTY_TITLE_KEYS = {
    QuickCaptureViewMode.RECORDS: "QuickCapture.Empty.RecordsTitle",
    QuickCaptureViewMode.PINNED: "QuickCapture.Empty.PinnedTitle",
    QuickCaptureViewMode.RECENT: "QuickCapture.Empty.RecentTitle",
}
EMPTY_TEXT_KEYS = {
    QuickCaptureViewMode.RECORDS: "QuickCapture.Empty.RecordsText",
    QuickCaptureViewMode.PINNED: "QuickCapture.Empty.PinnedText",
    QuickCaptureViewMode.RECENT: "QuickCapture.Empty.RecentText",
}

PRESET_CSS = {
    "Paper": "qc-paper",
    "StickyYellow": "qc-paper-yellow",
    "Rose": "qc-paper-rose",
    "Mint": "qc-paper-mint",
    "MistBlue": "qc-paper-blue",
}
MATERIALS = ("Paper", "StickyYellow", "Rose", "Mint", "MistBlue")


class QuickCaptureSurface(Gtk.Box):
    def __init__(self, config, settings_service, service: QuickCaptureService, application=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.config = config
        self.settings_service = settings_service
        self.service = service
        self.application = application
        self.current_view = QuickCaptureViewMode.normalize(
            settings_service.settings.quickCapture.quickCaptureDefaultView
        )
        self.search_text = ""
        self.selected_item: Optional[QuickCaptureItem] = None
        self._dual = False
        self._refreshing = False

        self._build()
        self.refresh()

        self.service.add_changed_listener(self._on_service_changed)  # shared service: all surfaces refresh
        self._resize_hook = ResizeHook(self, self._on_available_size_changed)

    def do_size_allocate(self, width, height, baseline):
        # vfunc overrides are dead in this PyGObject build; ResizeHook drives
        # _on_available_size_changed instead. Kept for direct test invocation.
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        self._on_available_size_changed(width, height)

    def _on_available_size_changed(self, width, height):
        mode = self.settings_service.settings.quickCapture.quickCaptureWideLayout
        if mode == "SinglePane":
            dual = False
        elif mode == "DualPane":
            dual = True
        elif self._dual:
            dual = width >= DUAL_PANE_EXIT_WIDTH
        else:
            dual = width >= DUAL_PANE_ENTER_WIDTH
        if dual != self._dual:
            self._dual = dual
            self._apply_layout_mode()

    # ---- structure -----------------------------------------------------------

    def _build(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        header.add_css_class("qc-add")
        self.add_entry = Gtk.Entry(visible=True)
        self.add_entry.set_hexpand(True)
        self.add_entry.set_placeholder_text(t("QuickCapture.InputPlaceholder"))
        self.add_entry.connect("activate", self._on_quick_add)
        add_btn = Gtk.Button(label="+", visible=True)
        add_btn.set_tooltip_text(t("QuickCapture.AddNote"))
        add_btn.connect("clicked", self._on_quick_add)
        header.append(self.add_entry)
        header.append(add_btn)
        search_btn = Gtk.ToggleButton(label="🔍", visible=True)
        search_btn.set_tooltip_text(t("QuickCapture.SearchPlaceholder"))
        search_btn.connect("toggled", lambda b: self.search_row.set_visible(b.get_active()))
        header.append(search_btn)
        self.append(header)

        self.search_row = Gtk.SearchEntry(visible=False)
        self.search_row.set_placeholder_text(t("QuickCapture.SearchPlaceholder"))
        self.search_row.connect("search-changed", self._on_search_changed)
        self.append(self.search_row)

        self.tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, visible=True)
        self.tab_box.add_css_class("qc-tabs")
        self.append(self.tab_box)
        self._rebuild_tabs()

        self.list_scroll = Gtk.ScrolledWindow(visible=True)
        self.list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_scroll.set_hexpand(True)
        self.list_scroll.set_vexpand(True)
        self.item_list = Gtk.ListBox(visible=True)
        self.item_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.item_list.connect("row-selected", self._on_row_selected)
        self.list_scroll.set_child(self.item_list)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.empty_title = Gtk.Label(visible=True)
        self.empty_title.add_css_class("qc-empty-title")
        self.empty_text = Gtk.Label(visible=True)
        self.empty_text.add_css_class("qc-empty-text")
        self.empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        self.empty_box.append(self.empty_title)
        self.empty_box.append(self.empty_text)
        self.list_box.append(self.empty_box)
        self.list_box.append(self.list_scroll)

        self.detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        back_btn = Gtk.Button(label=t("QuickCapture.Detail.Back"), visible=True)
        back_btn.add_css_class("qc-back")
        back_btn.connect("clicked", lambda *_: self.show_detail(False))
        self.detail_box.append(back_btn)
        self.detail_editor = _DetailEditor(self)
        self.detail_box.append(self.detail_editor)

        self.pane_stack = Gtk.Stack(visible=True)
        self.pane_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.pane_stack.add_named(self.list_box, "list")
        self.dual_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True)
        self.dual_box.append(self.list_box)
        self.dual_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, visible=True))
        self.dual_box.append(self.detail_box)
        self.pane_stack.add_named(self.dual_box, "dual")
        self.pane_stack.set_visible_child_name("list")
        self.append(self.pane_stack)

        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        self.footer.add_css_class("qc-footer")
        self.footer_label = Gtk.Label(xalign=0.0, visible=True)
        self.footer_label.set_hexpand(True)
        self.footer_label.set_ellipsize(True)
        self.footer.append(self.footer_label)
        self.clear_btn = Gtk.Button(visible=True)
        self.clear_btn.connect("clicked", self._on_clear)
        self.footer.append(self.clear_btn)
        self.append(self.footer)

    def _rebuild_tabs(self) -> None:
        qc = self.settings_service.settings.quickCapture
        for child in list(self.tab_box):
            self.tab_box.remove(child)
        if not qc.quickCaptureShowTabBar:
            self.tab_box.set_visible(False)
            return
        self.tab_box.set_visible(True)
        for view, key in VIEW_TAB_KEYS.items():
            flag = {
                QuickCaptureViewMode.RECORDS: qc.quickCaptureShowRecordsTab,
                QuickCaptureViewMode.PINNED: qc.quickCaptureShowPinnedTab,
                QuickCaptureViewMode.RECENT: qc.quickCaptureShowRecentTab,
            }[view]
            if not flag:
                continue
            count = len(self._visible_items(view))
            btn = Gtk.ToggleButton(label=fmt(key, count), visible=True)
            btn.set_active(view == self.current_view)
            btn.connect("toggled", lambda b, v=view: self._on_view_toggled(b, v))
            self.tab_box.append(btn)

    # ---- layout mode -----------------------------------------------------------

    def _apply_layout_mode(self) -> None:
        if self._dual:
            self.list_box.set_size_request(240, -1)
            self.pane_stack.set_visible_child_name("dual")
            if self.selected_item is None:
                row = self.item_list.get_row_at_index(0)
                if row is not None:
                    self.item_list.select_row(row)
        else:
            self.list_box.set_size_request(-1, -1)
            self.pane_stack.set_visible_child_name("list")

    def show_detail(self, show: bool) -> None:
        if not self._dual:
            self.pane_stack.set_visible_child_name("detail" if show else "list")

    # ---- item selection -----------------------------------------------------------

    def _visible_items(self, view: str) -> List[QuickCaptureItem]:
        data = self.service.data
        if view == QuickCaptureViewMode.RECENT:
            items = [i for i in data.recentItems if not i.isDeleted]
        elif view == QuickCaptureViewMode.PINNED:
            items = [i for i in data.items if not i.isDeleted and i.isPinned]
            items.sort(key=lambda i: (i.pinnedSortOrder if i.pinnedSortOrder >= 0 else 0, i.sortOrder))
        else:
            items = [i for i in data.items if not i.isDeleted]
        if self.search_text:
            needle = self.search_text.lower()
            items = [
                i
                for i in items
                if needle in (i.title or "").lower()
                or needle in (i.body or "").lower()
                or needle in (i.url or "").lower()
            ]
        return items

    def _on_search_changed(self, entry) -> None:
        self.search_text = entry.get_text().strip()
        self.refresh()

    def _on_view_toggled(self, button: Gtk.ToggleButton, view: str) -> None:
        if not button.get_active() or view == self.current_view:
            return
        self.current_view = view
        for child in self.tab_box:
            if isinstance(child, Gtk.ToggleButton) and child is not button:
                child.set_active(False)
        self.selected_item = None
        self.service.set_current_view(view)
        self.refresh()

    def _on_quick_add(self, *_args) -> None:
        text = self.add_entry.get_text()
        if not text.strip():
            return
        qc = self.settings_service.settings.quickCapture
        try:
            self.service.add_detailed_item(
                None, text, QuickCaptureAppearancePreset.DEFAULT, qc.quickCaptureDefaultFormat
            )
        except ValueError:
            return
        self.add_entry.set_text("")
        if self.current_view == QuickCaptureViewMode.RECENT:
            self._switch_view(QuickCaptureViewMode.RECORDS)
        self.refresh()

    def _switch_view(self, view: str) -> None:
        self.current_view = view
        self.service.set_current_view(view)
        self.selected_item = None
        self.refresh()

    # ---- refresh ---------------------------------------------------------------

    def refresh(self) -> None:
        keep = self.selected_item
        self._refreshing = True
        try:
            for child in list(self.item_list):
                self.item_list.remove(child)
            items = self._visible_items(self.current_view)
            for item in items:
                self.item_list.append(_QCCard(self, item))
            has_rows = bool(items)
            self.empty_box.set_visible(not has_rows)
            if not has_rows:
                if self.search_text:
                    self.empty_title.set_text(t("QuickCapture.Empty.SearchTitle"))
                    self.empty_text.set_text(t("QuickCapture.Empty.SearchText"))
                else:
                    self.empty_title.set_text(t(EMPTY_TITLE_KEYS[self.current_view]))
                    self.empty_text.set_text(t(EMPTY_TEXT_KEYS[self.current_view]))
            self._rebuild_tabs()
            self._refresh_footer()
            if keep is not None:
                self.selected_item = (
                    keep
                    if any(
                        (i.id == keep.id and i.isRecent == keep.isRecent)
                        for i in (self.service.data.items + self.service.data.recentItems)
                    )
                    else None
                )
        finally:
            self._refreshing = False
        if self.selected_item is not None:
            self.select_item(self.selected_item)
        else:
            self.detail_editor.load(None)
        self._apply_layout_mode()

    def _refresh_footer(self) -> None:
        if self.current_view == QuickCaptureViewMode.RECENT:
            from ..services.quick_capture_clipboard import recent_status_text

            self.footer_label.set_text(recent_status_text(self.settings_service))
            self.clear_btn.set_label(t("QuickCapture.ClearRecent"))
            self.clear_btn.set_visible(True)
        else:
            data = self.service.data
            self.footer_label.set_text(
                fmt(
                    "QuickCapture.ClearDataDescriptionWithCount",
                    sum(1 for i in data.items if not i.isDeleted),
                    sum(1 for i in data.recentItems if not i.isDeleted),
                )
            )
            self.clear_btn.set_label(t("QuickCapture.ClearData"))
            self.clear_btn.set_visible(self.current_view == QuickCaptureViewMode.RECORDS)

    def queue_rebuild(self) -> None:  # appearance/language refresh hook
        self._rebuild_tabs()
        self.refresh()

    def dispose(self) -> None:  # called by the widget manager on close
        self.service.remove_changed_listener(self._on_service_changed)

    def _on_service_changed(self) -> None:
        # Store changed elsewhere (clipboard monitor, another widget).
        self.refresh()

    def _on_row_selected(self, _box, row) -> None:
        if self._refreshing:
            return
        item = row.item if row is not None else None
        if item is self.selected_item and item is not None:
            if self.detail_editor.item is not item:
                self.detail_editor.load(item)
            return
        self.selected_item = item
        self.detail_editor.load(item)
        if item is not None and not self._dual:
            self.show_detail(True)

    def select_item(self, item: QuickCaptureItem) -> None:
        for row in self.item_list:
            if row.item is item:
                if self.item_list.get_selected_row() is row:
                    if self.detail_editor.item is not item:
                        self.detail_editor.load(item)
                else:
                    self.item_list.select_row(row)
                return

    # ---- actions -------------------------------------------------------------

    def _on_clear(self, *_args) -> None:
        if self.current_view == QuickCaptureViewMode.RECENT:
            self.service.clear_recent()
        else:
            self.service.clear()
        self.selected_item = None
        self.refresh()

    def pin_item(self, item: QuickCaptureItem, pinned: bool) -> None:
        if item.isRecent:
            self.service.save_recent_item_to_records(item.id, pin=pinned)
        else:
            self.service.set_pinned(item.id, pinned)
        self.refresh()

    def delete_item(self, item: QuickCaptureItem) -> None:
        if item.isRecent:
            self.service.delete_recent_item(item.id)
        else:
            self.service.delete_item(item.id)
        if self.selected_item is item:
            self.selected_item = None
        self.refresh()
        if not self._dual:
            self.show_detail(False)


class _QCCard(Gtk.ListBoxRow):
    def __init__(self, surface: QuickCaptureSurface, item: QuickCaptureItem):
        super().__init__(visible=True)
        self.surface = surface
        self.item = item

        qc = surface.settings_service.settings.quickCapture
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        box.add_css_class("qc-card")
        preset = QuickCaptureAppearancePreset.resolve_list_preset(item.appearancePreset, item.isRecent)
        css = PRESET_CSS.get(preset)
        if css:
            box.add_css_class(css)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, visible=True)
        content.set_hexpand(True)
        title_text = (item.title or "").strip()
        if not title_text and item.type == QuickCaptureItemType.IMAGE:
            title_text = t("QuickCapture.ImageItem")
        if not title_text:
            first_line = next((line for line in (item.body or "").splitlines() if line.strip()), "")
            title_text = markdown_render.plain_text(first_line)[:120]
        title = Gtk.Label(label=title_text, xalign=0.0, visible=True)
        title.set_ellipsize(True)
        title.add_css_class("qc-card-title")
        content.append(title)

        if item.type == QuickCaptureItemType.IMAGE and (item.imagePath or "").strip():
            thumb = surface.service.get_or_create_image_thumbnail_path(item.imagePath)
            if thumb and os.path.isfile(thumb):
                picture = Gtk.Picture.new_for_filename(thumb)
                picture.set_size_request(-1, 72)
                picture.set_can_shrink(True)
                picture.set_visible(True)
                picture.add_css_class("qc-thumb")
                content.append(picture)
        elif (item.body or "").strip() and item.type != QuickCaptureItemType.IMAGE:
            preview = markdown_render.plain_text(item.body)
            preview_label = Gtk.Label(
                label=preview,
                xalign=0.0,
                wrap=True,
                visible=True,
            )
            preview_label.set_lines(qc.quickCaptureItemPreviewLineCount)
            preview_label.set_ellipsize(True)
            preview_label.add_css_class("qc-card-preview")
            content.append(preview_label)

        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        if qc.quickCaptureShowCreatedTime and item.createdAt is not None:
            created = Gtk.Label(label=_format_created(item.createdAt), visible=True)
            created.add_css_class("qc-card-time")
            meta.append(created)
        if item.url:
            link = Gtk.Label(label="🔗", visible=True)
            meta.append(link)
        if meta.get_first_child() is not None:
            content.append(meta)
        box.append(content)

        if not item.isRecent:
            star = Gtk.ToggleButton(visible=True)
            star.set_child(Gtk.Label(label="📌" if item.isPinned else "☆", visible=True))
            star.set_active(item.isPinned)
            star.set_tooltip_text(t("QuickCapture.Unpin" if item.isPinned else "QuickCapture.Pin"))
            star.connect("toggled", lambda b: surface.pin_item(item, b.get_active()))
            star.add_css_class("qc-star")
            box.append(star)

        self.set_child(box)
        self.set_activatable(True)


class _DetailEditor(Gtk.Box):
    """Detail pane: read-only for Recent entries, editor for Records."""

    def __init__(self, surface: QuickCaptureSurface):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=True)
        self.surface = surface
        self.item: Optional[QuickCaptureItem] = None
        self._loading = False

        scroll = Gtk.ScrolledWindow(visible=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=True)
        scroll.set_child(content)
        self.content = content

        self.title_entry = Gtk.Entry(visible=True)
        self.title_entry.set_placeholder_text(t("QuickCapture.Detail.TitlePlaceholder"))
        self.title_entry.add_css_class("qc-detail-title")
        self.title_entry.connect("changed", self._on_title_changed)
        content.append(self.title_entry)

        self.format_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        edit_btn = Gtk.ToggleButton(label=t("QuickCapture.Detail.Edit"), visible=True)
        edit_btn.connect("toggled", lambda b: self._set_editing(b.get_active()))
        self.edit_btn = edit_btn
        self.format_row.append(edit_btn)
        convert_btn = Gtk.Button(label=t("QuickCapture.Detail.ConvertMarkdown"), visible=True)
        convert_btn.connect("clicked", self._on_convert_markdown)
        self.convert_btn = convert_btn
        self.format_row.append(convert_btn)
        content.append(self.format_row)

        self.editor_stack = Gtk.Stack(visible=True)
        self.body_view = Gtk.TextView(visible=True)
        self.body_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.body_view.get_buffer().connect("changed", self._on_body_changed)
        self.editor_stack.add_named(self.body_view, "edit")
        self.preview_label = Gtk.Label(visible=True)
        self.preview_label.set_xalign(0.0)
        self.preview_label.set_valign(Gtk.Align.START)
        self.preview_label.set_selectable(True)
        self.preview_label.set_use_markup(True)
        self.preview_label.set_wrap(True)
        preview_scroll = Gtk.ScrolledWindow(visible=True)
        preview_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        preview_scroll.set_child(self.preview_label)
        self.editor_stack.add_named(preview_scroll, "preview")
        self.editor_stack.set_visible_child_name("edit")
        content.append(self.editor_stack)

        material_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        material_row.append(Gtk.Label(label=t("QuickCapture.Detail.Appearance"), xalign=0.0, visible=True))
        for material in MATERIALS:
            btn = Gtk.Button(visible=True)
            swatch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True)
            swatch.set_size_request(18, 12)
            swatch.add_css_class(f"qc-swatch-{material.lower()}")
            btn.set_child(swatch)
            btn.set_tooltip_text(t(QuickCaptureAppearancePreset.localization_key(material)))
            btn.connect("clicked", lambda _b, m=material: self._set_material(m))
            material_row.append(btn)
        content.append(material_row)

        stamps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
        self.created_label = Gtk.Label(xalign=0.0, visible=True)
        self.updated_label = Gtk.Label(xalign=0.0, visible=True)
        for label in (self.created_label, self.updated_label):
            label.add_css_class("qc-card-time")
            stamps.append(label)
        content.append(stamps)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        copy_btn = Gtk.Button(label=t("QuickCapture.Detail.Copy"), visible=True)
        copy_btn.connect("clicked", lambda *_: self._copy_item())
        action_row.append(copy_btn)
        self.save_recent_btn = Gtk.Button(label=t("QuickCapture.SaveToRecords"), visible=True)
        self.save_recent_btn.connect("clicked", lambda *_: self._save_recent(pin=False))
        action_row.append(self.save_recent_btn)
        pin_recent_btn = Gtk.Button(label=t("QuickCapture.PinToRecords"), visible=True)
        pin_recent_btn.connect("clicked", lambda *_: self._save_recent(pin=True))
        action_row.append(pin_recent_btn)
        content.append(action_row)

        attach_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        add_file = Gtk.Button(label=t("QuickCapture.AddFile"), visible=True)
        add_file.connect("clicked", lambda *_: self._add_attachments(copy=False))
        attach_row.append(add_file)
        import_copy = Gtk.Button(label=t("Settings.AttachmentStorageMode.Copy"), visible=True)
        import_copy.connect("clicked", lambda *_: self._add_attachments(copy=True))
        attach_row.append(import_copy)
        self.attach_row = attach_row
        content.append(attach_row)
        self.attach_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
        content.append(self.attach_list)

        delete_btn = Gtk.Button(label=t("QuickCapture.Detail.Delete"), visible=True)
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", self._on_delete)
        self.delete_btn = delete_btn
        content.append(delete_btn)

        self.readonly_banner = Gtk.Label(label=t("QuickCapture.Detail.ReadOnly"), visible=True)
        self.readonly_banner.add_css_class("qc-readonly")

        self.empty_label = Gtk.Label(label=t("QuickCapture.Detail.NoSelection"), visible=True)
        self.empty_label.add_css_class("qc-empty-text")
        self.stack = Gtk.Stack(visible=True)
        self.stack.add_named(self.empty_label, "empty")
        editor_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, visible=True)
        editor_holder.append(self.readonly_banner)
        editor_holder.append(scroll)
        self.stack.add_named(editor_holder, "editor")
        self.stack.set_visible_child_name("empty")
        self.append(self.stack)

    # ---- load -----------------------------------------------------------------

    def load(self, item: Optional[QuickCaptureItem]) -> None:
        self._loading = True
        try:
            self.item = item
            if item is None:
                self.stack.set_visible_child_name("empty")
                return
            self.stack.set_visible_child_name("editor")
            readonly = item.isRecent
            self.readonly_banner.set_visible(readonly)
            for widget in (
                self.title_entry,
                self.edit_btn,
                self.convert_btn,
                self.attach_row,
                self.delete_btn,
            ):
                widget.set_sensitive(not readonly)
            self.save_recent_btn.set_visible(readonly)
            self.body_view.set_editable(not readonly)
            self.title_entry.set_text(item.title or "")
            buffer = self.body_view.get_buffer()
            buffer.set_text(item.body or "")
            self._refresh_preview()
            qc = self.surface.settings_service.settings.quickCapture
            wide_mode = qc.quickCaptureWideOpenMode if self.surface._dual else "Editing"
            markdown = item.contentFormat == TextContentFormat.MARKDOWN
            self._set_editing(wide_mode == "Editing" or not markdown)
            self.created_label.set_text(
                fmt("QuickCapture.Detail.Created", _format_created(item.createdAt)) if item.createdAt else ""
            )
            self.updated_label.set_text(
                fmt("QuickCapture.Detail.Updated", _format_created(item.updatedAt)) if item.updatedAt else ""
            )
            self._rebuild_attachments()
        finally:
            self._loading = False

    def _refresh_preview(self) -> None:
        item = self.item
        if item is None:
            return
        if item.contentFormat == TextContentFormat.MARKDOWN:
            try:
                self.preview_label.set_markup(markdown_render.to_pango_markup(item.body or ""))
            except Exception:
                self.preview_label.set_text(item.body or "")
        else:
            self.preview_label.set_text(item.body or "")

    def _set_editing(self, editing: bool) -> None:
        if self.edit_btn.get_active() != editing:
            self.edit_btn.set_active(editing)
        self.editor_stack.set_visible_child_name("edit" if editing else "preview")

    def _on_title_changed(self, entry) -> None:
        if self._loading or self.item is None or self.item.isRecent:
            return
        self._commit(title=entry.get_text())

    def _on_body_changed(self, buffer) -> None:
        if self._loading or self.item is None or self.item.isRecent or not self.body_view.get_editable():
            return
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.item.body = text
        self._refresh_preview()
        self._commit()

    def _commit(self, title: Optional[str] = None) -> None:
        if self.item is None or self.item.isRecent:
            return
        self.surface.service.update_item_details(
            self.item.id,
            self.item.title if title is None else title,
            self.item.body or "",
            self.item.appearancePreset,
        )
        self.surface.refresh()

    # ---- actions -----------------------------------------------------------------

    def _on_convert_markdown(self, *_args) -> None:
        if self.item is None or self.item.isRecent:
            return
        self.surface.service.update_item_details(
            self.item.id,
            self.item.title,
            self.item.body or "",
            self.item.appearancePreset,
            TextContentFormat.MARKDOWN,
        )
        self.item = next((i for i in self.surface.service.data.items if i.id == self.item.id), self.item)
        self.load(self.item)
        self.surface.refresh()

    def _set_material(self, material: str) -> None:
        if self.item is None or self.item.isRecent:
            return
        self.surface.service.set_appearance([self.item.id], material)
        self.item = next((i for i in self.surface.service.data.items if i.id == self.item.id), self.item)
        self.surface.refresh()

    def _copy_item(self) -> None:
        item = self.item
        if item is None:
            return
        from ..services import clipboard_io

        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()
        if (
            item.type == QuickCaptureItemType.IMAGE
            and (item.imagePath or "").strip()
            and os.path.isfile(item.imagePath)
        ):
            text = (item.title + "\n\n" if item.title else "") + (item.body or "")
            for attachment in item.attachments:
                text += f"\n{attachment.filePath}"
            if clipboard_io.write_image_and_mark(clipboard, item.imagePath, text):
                return
            return
        from ..services.quick_capture_clipboard_formatter import format_item_copy_text

        clipboard_io.write_text_and_mark(clipboard, format_item_copy_text(item))

    def _save_recent(self, pin: bool) -> None:
        if self.item is None or not self.item.isRecent:
            return
        self.surface.service.save_recent_item_to_records(self.item.id, pin=pin)
        self.surface._switch_view(QuickCaptureViewMode.RECORDS)
        self.surface.refresh()

    def _rebuild_attachments(self) -> None:
        for child in list(self.attach_list):
            self.attach_list.remove(child)
        if self.item is None:
            return
        for attachment in self.item.attachments:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
            label = Gtk.Label(label=attachment.displayName or attachment.filePath, xalign=0.0, visible=True)
            label.set_hexpand(True)
            label.set_ellipsize(True)
            row.append(label)
            open_btn = Gtk.Button(label=t("Common.Open"), visible=True)
            open_btn.connect("clicked", lambda _b, a=attachment: self._open_attachment(a))
            row.append(open_btn)
            if not self.item.isRecent:
                remove = Gtk.Button(label=t("QuickCapture.Detail.RemoveAttachment"), visible=True)
                remove.connect("clicked", lambda _b, a=attachment: self._remove_attachment(a))
                row.append(remove)
            self.attach_list.append(row)

    def _add_attachments(self, copy: bool) -> None:
        if self.item is None or self.item.isRecent:
            return
        dialog = Gtk.FileChooserNative.new(t("QuickCapture.AddFile"), None, Gtk.FileChooserAction.OPEN, None, None)
        dialog.set_select_multiple(True)
        dialog.set_modal(True)
        root = self.get_root()
        if isinstance(root, Gtk.Window):
            dialog.set_transient_for(root)

        def respond(_dlg, response: int):
            try:
                if response == Gtk.ResponseType.ACCEPT and self.item is not None:
                    files = dialog.get_files()
                    paths = [f.get_path() for f in files if f.get_path()]
                    if paths:
                        self.surface.service.add_attachments(self.item.id, paths, copy)
                        self.item = next(
                            (i for i in self.surface.service.data.items if i.id == self.item.id),
                            self.item,
                        )
                        self.load(self.item)
                        self.surface.refresh()
            finally:
                dialog.destroy()

        dialog.connect("response", respond)
        dialog.show()

    def _open_attachment(self, attachment) -> None:
        try:
            subprocess.Popen(["xdg-open", attachment.filePath])
        except OSError:
            pass

    def _remove_attachment(self, attachment) -> None:
        if self.item is None:
            return
        self.surface.service.delete_attachment(self.item.id, attachment.id)
        self.item = next((i for i in self.surface.service.data.items if i.id == self.item.id), self.item)
        self.load(self.item)
        self.surface.refresh()

    def _on_delete(self, *_args) -> None:
        if self.item is not None:
            self.surface.delete_item(self.item)


def _format_created(value: datetime) -> str:
    local = value.astimezone()
    return local.strftime("%Y-%m-%d %H:%M")
