"""Todo widget surface (port of the Todo widget's list/detail UI).

Single pane below 720px with push-navigation detail, dual pane at/above it
(680px exit threshold — TodoMasterDetailSettings hysteresis). Item edits hit
the TodoWidgetStore immediately; completing a recurring task generates the
next occurrence in place, like TodoReminderService.CompleteAsync.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..i18n import fmt, t
from ..models.todo import TodoAttachment, TodoItem, TodoStep
from ..platform.resize_hook import ResizeHook
from ..services import feature_widgets, todo_recurrence
from ..services.todo_store import TodoWidgetStore

DUAL_PANE_ENTER_WIDTH = 720
DUAL_PANE_EXIT_WIDTH = 680

RECURRENCE_MODES = ("none", "daily", "weekly", "monthly", "weekdays")
REMINDER_CHOICES = (-1, None, 0, 5, 10, 15, 30, 60, 1440)

EMPTY_KEYS = {
    "All": "Todo.Empty.All",
    "Active": "Todo.Empty.Active",
    "Today": "Todo.Empty.Today",
    "ThisWeek": "Todo.Empty.ThisWeek",
    "ThisMonth": "Todo.Empty.ThisMonth",
    "Important": "Todo.Empty.Important",
    "Completed": "Todo.Empty.Completed",
}


def _local_tz():
    """IANA zone from /etc/localtime when possible (DST-correct), else the
    current offset."""
    try:
        target = os.path.realpath("/etc/localtime")
        prefix = "/usr/share/zoneinfo/"
        if target.startswith(prefix):
            from zoneinfo import ZoneInfo

            return ZoneInfo(target[len(prefix) :])
    except OSError:
        pass
    return datetime.now().astimezone().tzinfo or timezone.utc


class TodoSurface(Gtk.Box):
    def __init__(self, config, settings_service, application=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.config = config
        self.settings_service = settings_service
        self.application = application
        self.store = TodoWidgetStore(config.id)
        self.data = self.store.load()
        self.selected_filter = self._default_filter()
        self.selected_item: Optional[TodoItem] = None
        self._dual = False
        self._refreshing = False

        self._build()
        self.refresh()
        self._resize_hook = ResizeHook(self, self._on_available_size_changed)

    def do_size_allocate(self, width, height, baseline):
        # vfunc overrides are dead in this PyGObject build; ResizeHook drives
        # _on_available_size_changed instead. Kept for direct test invocation.
        Gtk.Box.do_size_allocate(self, width, height, baseline)
        self._on_available_size_changed(width, height)

    def _on_available_size_changed(self, width, height):
        mode = self.settings_service.settings.todo.todoLayoutMode
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
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        add_box.add_css_class("todo-add")
        self.add_entry = Gtk.Entry(visible=True)
        self.add_entry.set_hexpand(True)
        self.add_entry.set_placeholder_text(t("Todo.AddPlaceholder"))
        self.add_entry.connect("activate", self._on_quick_add)
        add_btn = Gtk.Button(label="+", visible=True)
        add_btn.set_tooltip_text(t("Todo.AddPlaceholder"))
        add_btn.connect("clicked", self._on_quick_add)
        add_box.append(self.add_entry)
        add_box.append(add_btn)
        self.append(add_box)

        self.tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, visible=True)
        self.tab_box.add_css_class("todo-tabs")
        self.append(self.tab_box)
        self._rebuild_tabs()

        # The list pane and detail pane are shared widgets: single-pane mode
        # stacks them in a Gtk.Stack, dual-pane mode packs them side by side.
        self.list_scroll = Gtk.ScrolledWindow(visible=True)
        self.list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_scroll.set_hexpand(True)
        self.list_scroll.set_vexpand(True)
        self.item_list = Gtk.ListBox(visible=True)
        self.item_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.item_list.connect("row-selected", self._on_row_selected)
        self.list_scroll.set_child(self.item_list)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.empty_label = Gtk.Label(visible=True)
        self.empty_label.add_css_class("todo-empty")
        self.list_box.append(self.empty_label)
        self.list_box.append(self.list_scroll)

        self.detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.back_button = Gtk.Button(label=t("Todo.Detail.Back"), visible=True)
        self.back_button.add_css_class("todo-back")
        self.back_button.connect("clicked", lambda *_: self.show_detail(False))
        self.detail_box.append(self.back_button)
        self.detail_editor = _DetailEditor(self)
        self.detail_box.append(self.detail_editor)

        # The list and detail panes are shared widgets repacked between hosts:
        # single-pane mode pushes the detail over the list inside one box,
        # dual-pane mode packs list + separator + detail side by side. (A
        # widget can only have one parent, so the two modes are separate
        # containers, never two stack pages sharing children.)
        self.pane_stack = Gtk.Stack(visible=True)
        self.single_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.dual_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, visible=True)
        self.dual_separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, visible=True)
        self.pane_stack.add_named(self.single_box, "single")
        self.pane_stack.add_named(self.dual_box, "dual")
        self.pane_stack.set_visible_child_name("single")
        self.append(self.pane_stack)
        self._detail_pushed = False
        self._repack(self.single_box, [self.list_box])

        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        self.footer.add_css_class("todo-footer")
        self.stats_label = Gtk.Label(xalign=0.0, visible=True)
        self.stats_label.set_hexpand(True)
        self.footer.append(self.stats_label)
        clear_btn = Gtk.Button(label=t("Todo.ClearCompleted"), visible=True)
        clear_btn.connect("clicked", self._on_clear_completed)
        self.footer.append(clear_btn)
        self.append(self.footer)

    def _rebuild_tabs(self) -> None:
        for child in list(self.tab_box):
            self.tab_box.remove(child)
        if not self.settings_service.settings.todo.todoShowTabBar:
            self.tab_box.set_visible(False)
            return
        self.tab_box.set_visible(True)
        for name in feature_widgets.visible_filters(self.settings_service.settings):
            btn = Gtk.ToggleButton(label=t(f"Todo.Filter.{name}"), visible=True)
            btn.set_active(name == self.selected_filter)
            btn.connect("toggled", lambda b, n=name: self._on_filter_toggled(b, n))
            self.tab_box.append(btn)

    # ---- layout mode -----------------------------------------------------------

    def _repack(self, pane: Gtk.Box, widgets) -> None:
        """Empty `pane` and append `widgets` (unparenting them first)."""
        for child in list(pane):
            pane.remove(child)
        for widget in widgets:
            parent = widget.get_parent()
            if parent is not None:
                parent.remove(widget)
            pane.append(widget)

    def _apply_layout_mode(self) -> None:
        self.back_button.set_visible(not self._dual)
        if self._dual:
            self.list_box.set_size_request(280, -1)
            self._repack(self.dual_box, [self.list_box, self.dual_separator, self.detail_box])
            self.pane_stack.set_visible_child_name("dual")
            if self.selected_item is None and self.settings_service.settings.todo.todoAutoSelectFirstInWideLayout:
                row = self.item_list.get_row_at_index(0)
                if row is not None:
                    self.item_list.select_row(row)
        else:
            self.list_box.set_size_request(-1, -1)
            self._repack(self.single_box, [self.detail_box] if self._detail_pushed else [self.list_box])
            self.pane_stack.set_visible_child_name("single")

    def show_detail(self, show: bool) -> None:
        self._detail_pushed = show
        if not self._dual:
            self._repack(self.single_box, [self.detail_box] if show else [self.list_box])

    # ---- data ops ----------------------------------------------------------------

    def _default_filter(self) -> str:
        filters = feature_widgets.visible_filters(self.settings_service.settings)
        default = self.settings_service.settings.todo.todoDefaultFilter
        return default if default in filters else filters[0]

    def _on_filter_toggled(self, button: Gtk.ToggleButton, name: str) -> None:
        if button.get_active():
            self.selected_filter = name
            for child in self.tab_box:
                if isinstance(child, Gtk.ToggleButton) and child is not button:
                    child.set_active(False)
            self.refresh()

    def _on_quick_add(self, *_args) -> None:
        text = self.add_entry.get_text().strip()
        if not text:
            return
        item = TodoItem(text=text)
        if self.settings_service.settings.todo.todoNewTaskPosition == "Bottom":
            self.data.items.append(item)
        else:
            self.data.items.insert(0, item)
        self._commit()
        self.add_entry.set_text("")
        self.refresh()

    def set_completed(self, item: TodoItem, completed: bool) -> None:
        now = datetime.now(timezone.utc)
        item.isCompleted = completed
        item.completedAt = now if completed else None
        item.updatedAt = now
        if completed:
            if item.recurrence is not None and not item.recurrenceSeriesId:
                item.recurrenceSeriesId = uuid.uuid4().hex
            item.snoozedUntil = None
            item.snoozeLastNotifiedAt = None
            next_item = todo_recurrence.try_create_next_occurrence(item, now)
            if next_item is not None and item.generatedNextItemId is None:
                item.generatedNextItemId = next_item.id
                index = self.data.items.index(item)
                self.data.items.insert(index + 1, next_item)
        else:
            # Un-completing drops an untouched generated occurrence.
            if item.generatedNextItemId:
                generated = next((i for i in self.data.items if i.id == item.generatedNextItemId), None)
                if generated is not None and todo_recurrence.should_remove_generated_occurrence(item, generated):
                    self.data.items.remove(generated)
            item.generatedNextItemId = None
        self._commit()
        self.refresh()

    def delete_item(self, item: TodoItem) -> None:
        if item in self.data.items:
            self.data.items.remove(item)
        if self.selected_item is item:
            self.selected_item = None
        self._commit()
        self.refresh()

    def _on_clear_completed(self, *_args) -> None:
        self.data.items = [i for i in self.data.items if not i.isCompleted]
        self._commit()
        self.refresh()

    def _commit(self) -> None:
        for index, item in enumerate(self.data.items):
            item.sortOrder = index
        self.store.save(self.data)
        if self.selected_item is not None and self.selected_item not in self.data.items:
            self.selected_item = None

    def queue_rebuild(self) -> None:  # appearance-refresh hook from the shell
        self._rebuild_tabs()
        self.refresh()

    def refresh(self) -> None:
        keep = self.selected_item
        self._refreshing = True
        try:
            for child in list(self.item_list):
                self.item_list.remove(child)
            settings = self.settings_service.settings
            rows = 0
            for item in self.data.items:
                if not feature_widgets.matches_filter(item, self.selected_filter, settings):
                    continue
                self.item_list.append(_TodoRow(self, item))
                rows += 1
            self.empty_label.set_visible(rows == 0)
            if rows == 0:
                key = EMPTY_KEYS.get(self.selected_filter, "Todo.Empty.Title")
                self.empty_label.set_text(t(key) if _has_key(key) else t("Todo.Empty.Title"))
            if settings.todo.todoShowFooterStats:
                self.stats_label.set_text(fmt("Todo.ItemsLeft", sum(1 for i in self.data.items if not i.isCompleted)))
            self.footer.set_visible(settings.todo.todoShowFooterStats or settings.todo.todoShowClearCompletedButton)
            self.selected_item = keep if any(i is keep for i in self.data.items) else None
        finally:
            self._refreshing = False
        if self.selected_item is not None:
            self.select_item(self.selected_item)
        else:
            self.detail_editor.load(None)
        self._apply_layout_mode()

    # ---- selection -----------------------------------------------------------------

    def _on_row_selected(self, _box, row) -> None:
        if self._refreshing:
            return  # row teardown/rebuild inside refresh(); selection restored there
        item = row.item if row is not None else None
        if item is self.selected_item and item is not None:
            if self.detail_editor.item is not item:
                self.detail_editor.load(item)
            return
        self.selected_item = item
        self.detail_editor.load(item)
        if item is not None and not self._dual:
            self.show_detail(True)

    def select_item(self, item: TodoItem) -> None:
        for row in self.item_list:
            if row.item is item:
                if self.item_list.get_selected_row() is row:
                    if self.detail_editor.item is not item:
                        self.detail_editor.load(item)  # data changed under a kept selection
                else:
                    self.item_list.select_row(row)
                return


class _TodoRow(Gtk.ListBoxRow):
    def __init__(self, surface: TodoSurface, item: TodoItem):
        super().__init__(visible=True)
        self.surface = surface
        self.item = item

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        box.add_css_class("todo-row")
        box.set_margin_top(2)
        box.set_margin_bottom(2)

        self.check = Gtk.CheckButton(visible=True)
        self.check.set_active(item.isCompleted)
        self.check.connect("toggled", self._on_toggled)
        box.append(self.check)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, visible=True)
        text_box.set_hexpand(True)
        self.label = Gtk.Label(label=item.text, xalign=0.0, wrap=True, visible=True)
        self.label.set_lines(surface.settings_service.settings.todo.todoItemPreviewLineCount)
        self.label.set_ellipsize(True)
        self.label.add_css_class("todo-row-text")
        if item.isCompleted:
            self.label.add_css_class("todo-done")
        text_box.append(self.label)
        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        if item.dueDate is not None:
            due = Gtk.Label(label=_format_due_short(item.dueDate), visible=True)
            due.add_css_class("todo-due")
            if not item.isCompleted and item.dueDate <= datetime.now(timezone.utc):
                due.add_css_class("todo-overdue")
            meta.append(due)
        if item.recurrence is not None and item.recurrence.mode != "none":
            rec = Gtk.Label(label="↻", visible=True)
            rec.add_css_class("todo-recurrence-glyph")
            meta.append(rec)
        if item.colorMarker:
            dot = Gtk.Label(label="●", visible=True)
            dot.set_name(f"todo-dot-{item.colorMarker}")
            dot.add_css_class("todo-color-dot")
            meta.append(dot)
        text_box.append(meta)
        box.append(text_box)

        star = Gtk.ToggleButton(visible=True)
        star.set_child(Gtk.Label(label="★" if item.isImportant else "☆", visible=True))
        star.add_css_class("todo-star")
        star.set_active(item.isImportant)
        star.connect("toggled", self._on_star)
        box.append(star)

        self.set_child(box)
        self.set_activatable(True)

    def _on_toggled(self, _check) -> None:
        self.surface.set_completed(self.item, self.check.get_active())

    def _on_star(self, button) -> None:
        self.item.isImportant = button.get_active()
        self.item.updatedAt = datetime.now(timezone.utc)
        self.surface._commit()
        self.surface.refresh()


class _DetailEditor(Gtk.Box):
    """Right-hand / pushed detail editor for one TodoItem."""

    def __init__(self, surface: TodoSurface):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=True)
        self.surface = surface
        self.item: Optional[TodoItem] = None
        self._loading = False
        self._due_popover: Optional[Gtk.Popover] = None

        scroll = Gtk.ScrolledWindow(visible=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=True)
        content.set_margin_top(4)
        scroll.set_child(content)
        self.content = content

        self.title_entry = Gtk.Entry(visible=True)
        self.title_entry.add_css_class("todo-detail-title")
        self.title_entry.connect("changed", self._on_title_changed)
        content.append(self.title_entry)

        steps_label = Gtk.Label(label=t("Todo.Detail.Steps"), xalign=0.0, visible=True)
        steps_label.add_css_class("todo-group-title")
        content.append(steps_label)
        self.steps_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
        content.append(self.steps_list)
        self.step_entry = Gtk.Entry(visible=True)
        self.step_entry.set_placeholder_text(t("Todo.Detail.AddStep"))
        self.step_entry.connect("activate", self._on_add_step)
        content.append(self.step_entry)

        notes_label = Gtk.Label(label=t("Todo.Detail.Notes"), xalign=0.0, visible=True)
        notes_label.add_css_class("todo-group-title")
        content.append(notes_label)
        self.notes_view = Gtk.TextView(visible=True)
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.get_buffer().connect("changed", self._on_notes_changed)
        notes_scroll = Gtk.ScrolledWindow(visible=True)
        notes_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        notes_scroll.set_min_content_height(90)
        notes_scroll.set_child(self.notes_view)
        content.append(notes_scroll)

        sched = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        self.due_button = Gtk.Button(visible=True)
        self.due_button.connect("clicked", self._on_due_clicked)
        sched.append(self.due_button)
        clear_due = Gtk.Button(label=t("Todo.Due.Clear"), visible=True)
        clear_due.connect("clicked", lambda *_: self._set_due(None))
        sched.append(clear_due)
        content.append(sched)

        rec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        rec_label = Gtk.Label(label=t("Todo.Menu.Recurrence"), xalign=0.0, visible=True)
        rec_label.set_hexpand(True)
        rec_row.append(rec_label)
        self.recurrence_drop = _recurrence_dropdown(self._on_recurrence_changed)
        rec_row.append(self.recurrence_drop)
        content.append(rec_row)

        remind_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        remind_label = Gtk.Label(label=t("Todo.Menu.Reminder"), xalign=0.0, visible=True)
        remind_label.set_hexpand(True)
        remind_row.append(remind_label)
        self.reminder_drop = _reminder_dropdown(self._on_reminder_changed)
        remind_row.append(self.reminder_drop)
        content.append(remind_row)

        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        color_row.append(Gtk.Label(label=t("Todo.Menu.ColorMarker"), xalign=0.0, visible=True))
        for color in TodoItem.SUPPORTED_COLOR_MARKERS:
            btn = Gtk.Button(visible=True)
            dot = Gtk.Label(label="●", visible=True)
            dot.set_name(f"todo-dot-{color}")
            btn.set_child(dot)
            btn.set_tooltip_text(t(TodoItem.color_marker_localization_key(color)))
            btn.add_css_class("todo-color-button")
            btn.connect("clicked", lambda _b, c=color: self._set_color(c))
            color_row.append(btn)
        none_btn = Gtk.Button(label="○", visible=True)
        none_btn.set_tooltip_text(t("Todo.Color.None"))
        none_btn.connect("clicked", lambda *_: self._set_color(None))
        color_row.append(none_btn)
        content.append(color_row)

        attach_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=True)
        link_btn = Gtk.Button(label=t("Settings.AttachmentStorageMode.Link"), visible=True)
        link_btn.connect("clicked", lambda *_: self._add_attachment(managed=False))
        attach_buttons.append(link_btn)
        copy_btn = Gtk.Button(label=t("Settings.AttachmentStorageMode.Copy"), visible=True)
        copy_btn.connect("clicked", lambda *_: self._add_attachment(managed=True))
        attach_buttons.append(copy_btn)
        content.append(attach_buttons)
        self.attach_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, visible=True)
        content.append(self.attach_list)

        delete_btn = Gtk.Button(label=t("Common.Delete"), visible=True)
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", self._on_delete)
        content.append(delete_btn)

        self.empty_label = Gtk.Label(label=t("Todo.Detail.EmptyTitle"), visible=True)
        self.empty_label.add_css_class("todo-empty")
        self.stack = Gtk.Stack(visible=True)
        self.stack.add_named(self.empty_label, "empty")
        self.stack.add_named(scroll, "editor")
        self.stack.set_visible_child_name("empty")
        self.append(self.stack)

    # ---- load / save -----------------------------------------------------------

    def load(self, item: Optional[TodoItem]) -> None:
        self._loading = True
        self.item = item
        if item is None:
            self.stack.set_visible_child_name("empty")
            self._loading = False
            return
        self.stack.set_visible_child_name("editor")
        self.title_entry.set_text(item.text)
        buffer = self.notes_view.get_buffer()
        buffer.set_text(item.notes or "")
        self.due_button.set_label(_format_due_long(item.dueDate) if item.dueDate else t("Todo.Due.Custom"))
        mode = item.recurrence.mode if item.recurrence else "none"
        self.recurrence_drop.set_selected(RECURRENCE_MODES.index(mode) if mode in RECURRENCE_MODES else 0)
        self.recurrence_drop.set_sensitive(item.dueDate is not None)
        self._set_reminder_selected(item.reminderOffsetMinutes)
        self._rebuild_steps()
        self._rebuild_attachments()
        self._loading = False

    def _touch(self) -> None:
        if self.item is not None:
            self.item.updatedAt = datetime.now(timezone.utc)
        self.surface._commit()

    def _on_title_changed(self, entry) -> None:
        if self._loading or self.item is None:
            return
        self.item.text = entry.get_text().strip()
        self._touch()
        self.surface.refresh()

    def _on_notes_changed(self, buffer) -> None:
        if self._loading or self.item is None:
            return
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.item.notes = text if text.strip() else None
        self._touch()

    # ---- steps -------------------------------------------------------------------

    def _rebuild_steps(self) -> None:
        for child in list(self.steps_list):
            self.steps_list.remove(child)
        if self.item is None:
            return
        for step in sorted(self.item.steps, key=lambda s: s.sortOrder):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
            check = Gtk.CheckButton(visible=True)
            check.set_active(step.isCompleted)
            check.connect("toggled", lambda _c, s=step: self._toggle_step(s))
            row.append(check)
            label = Gtk.Label(label=step.text, xalign=0.0, visible=True)
            label.set_hexpand(True)
            if step.isCompleted:
                label.add_css_class("todo-done")
            row.append(label)
            remove = Gtk.Button(label="×", visible=True)
            remove.connect("clicked", lambda _b, s=step: self._remove_step(s))
            row.append(remove)
            self.steps_list.append(row)

    def _on_add_step(self, entry) -> None:
        if self.item is None:
            return
        text = entry.get_text().strip()
        if not text:
            return
        self.item.steps.append(TodoStep(text=text))
        entry.set_text("")
        self._touch()
        self._rebuild_steps()

    def _toggle_step(self, step: TodoStep) -> None:
        step.isCompleted = not step.isCompleted
        self._touch()
        self._rebuild_steps()

    def _remove_step(self, step: TodoStep) -> None:
        if step in self.item.steps:
            self.item.steps.remove(step)
        self._touch()
        self._rebuild_steps()

    # ---- scheduling -----------------------------------------------------------------

    def _on_due_clicked(self, *_args) -> None:
        if self.item is None:
            return
        if self._due_popover is None:
            self._due_popover = self._build_due_popover()
        local = (self.item.dueDate or datetime.now(timezone.utc)).astimezone()
        self._calendar.select_day(GLib.DateTime.new_local(local.year, local.month, local.day, 0, 0, 0.0))
        self._hour_spin.set_value(local.hour)
        self._minute_spin.set_value(local.minute)
        self._due_popover.popup()

    def _build_due_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()  # parented before showing; visible=True would realize parentless
        popover.set_autohide(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=True)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        self._calendar = Gtk.Calendar(visible=True)
        box.append(self._calendar)
        time_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=True)
        self._hour_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._minute_spin = Gtk.SpinButton.new_with_range(0, 59, 1)
        apply_btn = Gtk.Button(label=t("Common.Save"), visible=True)
        apply_btn.connect("clicked", self._apply_due_popover)
        time_row.append(self._hour_spin)
        time_row.append(self._minute_spin)
        time_row.append(apply_btn)
        box.append(time_row)
        popover.set_child(box)
        popover.set_parent(self.due_button)
        return popover

    def _apply_due_popover(self, *_args) -> None:
        date = self._calendar.get_date()
        due = datetime(
            date.get_year(),
            date.get_month(),
            date.get_day_of_month(),
            int(self._hour_spin.get_value()),
            int(self._minute_spin.get_value()),
            tzinfo=_local_tz(),
        ).astimezone(timezone.utc)
        self._set_due(due)
        self._due_popover.popdown()

    def _set_due(self, due: Optional[datetime]) -> None:
        if self.item is None:
            return
        self.item.dueDate = due
        if due is None:
            self.item.recurrence = None
            self.item.recurrenceSeriesId = None
            self.item.reminderDismissedForDueDate = None
            self.item.snoozedUntil = None
            self.item.snoozeLastNotifiedAt = None
            self.load(self.item)
        else:
            from ..models.todo import TodoRecurrence

            self.item.recurrence = TodoRecurrence.normalize(self.item.recurrence, due)
            self.load(self.item)
        self._touch()
        self.surface.refresh()

    def _on_recurrence_changed(self, mode: str) -> None:
        if self._loading or self.item is None:
            return
        self.item.recurrence = todo_recurrence.create_recurrence(mode, self.item.dueDate) if mode != "none" else None
        if self.item.recurrence is not None and not self.item.recurrenceSeriesId:
            self.item.recurrenceSeriesId = uuid.uuid4().hex
        self._touch()
        self.surface.refresh()

    def _on_reminder_changed(self, value: Optional[int]) -> None:
        if self._loading or self.item is None:
            return
        self.item.reminderOffsetMinutes = value
        self._touch()

    def _set_reminder_selected(self, value: Optional[int]) -> None:
        index = REMINDER_CHOICES.index(value) if value in REMINDER_CHOICES else 1
        self.reminder_drop.set_selected(index)

    # ---- color / attachments / delete --------------------------------------------

    def _set_color(self, color: Optional[str]) -> None:
        if self.item is None:
            return
        self.item.colorMarker = TodoItem.normalize_color_marker(color)
        self._touch()
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
            remove = Gtk.Button(label="×", visible=True)
            remove.connect("clicked", lambda _b, a=attachment: self._remove_attachment(a))
            row.append(remove)
            self.attach_list.append(row)

    def _add_attachment(self, managed: bool) -> None:
        if self.item is None:
            return
        dialog = Gtk.FileChooserNative.new(t("Todo.Detail.AddFile"), None, Gtk.FileChooserAction.OPEN, None, None)
        dialog.set_modal(True)
        root = self.get_root()
        if isinstance(root, Gtk.Window):
            dialog.set_transient_for(root)

        def respond(_dlg, response: int):
            try:
                if response == Gtk.ResponseType.ACCEPT and self.item is not None:
                    file = dialog.get_file()
                    path = file.get_path() if file is not None else None
                    if path:
                        if managed:
                            os.makedirs(self.surface.store.attachment_directory, exist_ok=True)
                            target = os.path.join(self.surface.store.attachment_directory, os.path.basename(path))
                            shutil.copy2(path, target)
                            path = target
                        self.item.attachments.append(
                            TodoAttachment(
                                filePath=path,
                                displayName=os.path.basename(path),
                                storageMode="managed" if managed else "linked",
                            )
                        )
                        self._touch()
                        self._rebuild_attachments()
            finally:
                dialog.destroy()

        dialog.connect("response", respond)
        dialog.show()

    def _open_attachment(self, attachment: TodoAttachment) -> None:
        try:
            subprocess.Popen(["xdg-open", attachment.filePath])
        except OSError:
            pass

    def _remove_attachment(self, attachment: TodoAttachment) -> None:
        if self.item is not None and attachment in self.item.attachments:
            self.item.attachments.remove(attachment)
        self._touch()
        self._rebuild_attachments()

    def _on_delete(self, *_args) -> None:
        if self.item is not None:
            self.surface.delete_item(self.item)
            self.surface.show_detail(False)


# ---- small helpers ---------------------------------------------------------------


def _has_key(key: str) -> bool:
    return t(key) != key


def _recurrence_dropdown(on_change):
    labels = [t(f"Todo.Recurrence.{m.capitalize()}") for m in RECURRENCE_MODES]
    drop = Gtk.DropDown.new_from_strings(labels)
    drop.set_visible(True)
    drop.connect("notify::selected", lambda w, _p: on_change(RECURRENCE_MODES[w.get_selected()]))
    return drop


def _reminder_dropdown(on_change):
    values = list(REMINDER_CHOICES)
    labels = [_reminder_label(v) for v in values]
    drop = Gtk.DropDown.new_from_strings(labels)
    drop.set_visible(True)
    drop.connect("notify::selected", lambda w, _p: on_change(values[w.get_selected()]))
    return drop


def _reminder_label(value: Optional[int]) -> str:
    if value == -1:
        return t("Todo.Reminder.Off")
    if value is None:
        return t("Todo.Reminder.DefaultShort")
    if value == 0:
        return t("Todo.Reminder.AtDueTime")
    if value == 60:
        return t("Todo.Reminder.OneHourBefore")
    if value == 1440:
        return t("Todo.Reminder.OneDayBefore")
    return fmt("Todo.Reminder.MinutesBefore", value)


def _format_due_short(due: datetime) -> str:
    local = due.astimezone()
    today = datetime.now().astimezone().date()
    if local.date() == today:
        return t("Todo.Due.Today")
    if local.date() == today + timedelta(days=1):
        return t("Todo.Due.Tomorrow")
    return f"{local.month}/{local.day}"


def _format_due_long(due: datetime) -> str:
    local = due.astimezone()
    today = datetime.now().astimezone().date()
    time = local.strftime("%H:%M")
    if local.date() == today:
        return fmt("Todo.Due.TodayAt", time)
    if local.date() == today + timedelta(days=1):
        return fmt("Todo.Due.TomorrowAt", time)
    return f"{local.year}/{local.month}/{local.day} {time}"
