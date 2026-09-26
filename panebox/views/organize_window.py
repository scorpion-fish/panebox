"""Organize-desktop window: preview → progress → result (port of the
DesktopOrganizationWindow flow: plan cards with destination routing,
retained-items opt-in, undo, crash-recovery gate)."""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ..i18n import fmt, t
from ..services.organize_models import (
    DestinationMode,
    ExclusionReason,
    RetentionReason,
    TargetSelection,
)
from ..services.organize_service import OrganizeService

_REASONS_LOCALIZED = {
    RetentionReason.SOURCE_CHANGED: "SourceChanged",
    RetentionReason.IN_USE: "InUse",
    RetentionReason.ACCESS_DENIED: "AccessDenied",
    RetentionReason.UNAVAILABLE: "Unavailable",
    RetentionReason.TRANSFER_FAILED: "TransferFailed",
    RetentionReason.CANCELED: "Canceled",
}

_EXCLUSIONS_LOCALIZED = {
    ExclusionReason.FOLDER: "Folder",
    ExclusionReason.HIDDEN_OR_SYSTEM: "HiddenOrSystem",
    ExclusionReason.REPARSE_POINT: "ReparsePoint",
    ExclusionReason.OFFLINE_PLACEHOLDER: "OfflinePlaceholder",
    ExclusionReason.TEMPORARY_OR_DOWNLOADING: "TemporaryOrDownloading",
    ExclusionReason.PUBLIC_DESKTOP_ITEM: "PublicDesktopItem",
    ExclusionReason.UNAVAILABLE: "Unavailable",
    ExclusionReason.SLOW_ITEM: "SlowItem",
    ExclusionReason.BATCH_LIMIT: "BatchLimit",
    ExclusionReason.USER_CHOICE: "UserChoice",
    ExclusionReason.SOURCE_NOT_SELECTED: "SourceNotSelected",
}


def _exclusion_label(reason: str) -> str:
    suffix = _EXCLUSIONS_LOCALIZED.get(reason)
    return t(f"DesktopOrganization.Exclusion.{suffix}") if suffix else reason


def _retention_label(reason: str) -> str:
    suffix = _REASONS_LOCALIZED.get(reason)
    return t(f"DesktopOrganization.Retention.{suffix}") if suffix else reason


class OrganizeWindow(Gtk.ApplicationWindow):
    def __init__(self, application, service: OrganizeService, on_committed=None):
        super().__init__(application=application)
        self.service = service
        self._on_committed = on_committed  # app hook: materialize created widgets
        self.set_title(t("DesktopOrganization.Window.Title"))
        self.set_default_size(620, 680)

        self._scan = None
        self._plan = None
        self._opt_in: set[str] = set()
        self._selections: dict[str, TargetSelection] = {}
        self._undo_entry_id: Optional[str] = None

        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_preview_page(), "preview")
        self._stack.add_named(self._build_progress_page(), "progress")
        self._stack.add_named(self._build_result_page(), "result")
        self._stack.add_named(self._build_nothing_page(), "nothing")
        self.set_child(self._stack)

        self._recover_and_scan()

    # ---- pages -----------------------------------------------------------------

    def _build_preview_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        self._recovery_banner = Gtk.Revealer(reveal_child=False)
        self._recovery_label = Gtk.Label(wrap=True)
        self._recovery_label.add_css_class("dim-label")
        self._recovery_banner.set_child(self._recovery_label)
        box.append(self._recovery_banner)

        self._headline = Gtk.Label()
        self._headline.set_halign(Gtk.Align.START)
        self._headline.add_css_class("title-2")
        box.append(self._headline)

        self._summary = Gtk.Label(wrap=True, halign=Gtk.Align.START)
        self._summary.add_css_class("dim-label")
        box.append(self._summary)

        scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scrolled.set_child(self._cards)
        box.append(scrolled)

        self._retained = Gtk.Expander()
        self._retained_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._retained.set_child(self._retained_box)
        box.append(self._retained)

        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        rescan = Gtk.Button(label=t("DesktopOrganization.Preview.Refresh"))
        rescan.connect("clicked", lambda *_: self._recover_and_scan())
        actions.append(rescan)
        self._execute_button = Gtk.Button()
        self._execute_button.add_css_class("suggested-action")
        self._execute_button.connect("clicked", lambda *_: self._on_execute())
        actions.append(self._execute_button)
        box.append(actions)
        return box

    def _build_progress_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_label = Gtk.Label(wrap=True)
        box.append(self._progress_bar)
        box.append(self._progress_label)
        return box

    def _build_result_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        self._result_title = Gtk.Label(halign=Gtk.Align.START)
        self._result_title.add_css_class("title-2")
        self._result_body = Gtk.Label(wrap=True, halign=Gtk.Align.START)
        self._result_body.add_css_class("dim-label")
        self._result_retained = Gtk.Expander()
        self._result_retained_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._result_retained.set_child(self._result_retained_box)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END, vexpand=False, valign=Gtk.Align.END)
        actions.set_valign(Gtk.Align.END)
        self._undo_button = Gtk.Button(label=t("DesktopOrganization.Layout.Undo"))
        self._undo_button.connect("clicked", lambda *_: self._on_undo())
        done = Gtk.Button(label=t("DesktopOrganization.Window.Done"))
        done.add_css_class("suggested-action")
        done.connect("clicked", lambda *_: self.destroy())
        actions.append(self._undo_button)
        actions.append(done)
        box.append(self._result_title)
        box.append(self._result_body)
        box.append(self._result_retained)
        box.append(actions)
        return box

    def _build_nothing_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=t("DesktopOrganization.Preview.NothingTitle"))
        title.add_css_class("title-2")
        body = Gtk.Label(label=t("DesktopOrganization.Preview.NothingBody"), wrap=True, max_width_chars=48)
        body.add_css_class("dim-label")
        rescan = Gtk.Button(label=t("DesktopOrganization.Preview.Refresh"), halign=Gtk.Align.CENTER)
        rescan.connect("clicked", lambda *_: self._recover_and_scan())
        box.append(title)
        box.append(body)
        box.append(rescan)
        return box

    # ---- preview ------------------------------------------------------------------

    def _recover_and_scan(self) -> None:
        restored = 0
        if self.service.has_pending_recovery:
            restored = self.service.recover_pending()
        self._recovery_banner.set_reveal_child(restored > 0)
        if restored:
            self._recovery_label.set_text(fmt("DesktopOrganization.Public.SourceResult", restored))
        self._scan = self.service.scan()
        self._opt_in = {p for p in self._opt_in if p.lower() in {i.source_path.lower() for i in self._scan.items}}
        self._rebuild_plan()

    def _rebuild_plan(self) -> None:
        self._plan = self.service.create_plan(self._scan, opt_in_paths=self._opt_in)
        self._selections = {}
        self._render_preview()

    def _render_preview(self) -> None:
        plan = self._plan
        assert plan is not None

        while child := self._cards.get_first_child():
            self._cards.remove(child)
        while child := self._retained_box.get_first_child():
            self._retained_box.remove(child)

        widgets = [w for w in self.service.settings.layout.widgets if w.widgetKind == "File" and w.mappedFolderPath]
        for target in plan.targets:
            self._cards.append(self._build_target_card(target, widgets))

        eligible = plan.eligible_item_count
        retained_total = sum(
            1 for item in plan.excluded_items if plan.include_personal_desktop or plan.include_public_desktop
        )
        opt_inable = [i for i in plan.excluded_items if i.can_opt_in]
        protected = [i for i in plan.excluded_items if not i.can_opt_in]

        if eligible == 0:
            self._stack.set_visible_child_name("nothing")
            return

        self._stack.set_visible_child_name("preview")
        self._headline.set_text(fmt("DesktopOrganization.Preview.Headline", eligible, len(plan.targets)))
        self._summary.set_text(
            fmt(
                "DesktopOrganization.Preview.TotalSummary",
                eligible + retained_total,
                retained_total,
                plan.new_widget_count,
            )
        )
        self._execute_button.set_label(fmt("DesktopOrganization.Layout.Execute", eligible))
        self._execute_button.set_sensitive(True)

        self._retained.set_visible(retained_total > 0)
        self._retained.set_label(fmt("DesktopOrganization.Preview.RetainedTitle", retained_total))
        description = Gtk.Label(wrap=True, xalign=0)
        description.set_text(
            fmt(
                "DesktopOrganization.Preview.RetainedDescription",
                len(opt_inable),
                len(self._opt_in),
                len(protected),
                0,
            )
        )
        description.add_css_class("dim-label")
        self._retained_box.append(description)
        for item in opt_inable:
            check = Gtk.CheckButton(label=f"{item.name} — {_exclusion_label(item.exclusion_reason)}")
            check.set_active(item.source_path.lower() in {p.lower() for p in self._opt_in})
            check.connect(
                "toggled",
                lambda toggle, path=item.source_path: self._on_opt_in(toggle, path),
            )
            self._retained_box.append(check)
        if protected:
            header = Gtk.Label(xalign=0)
            header.set_text(fmt("DesktopOrganization.Preview.ExcludedHeader", len(protected)))
            self._retained_box.append(header)
            for item in protected[:8]:
                row = Gtk.Label(
                    label=f"{item.name} — {_exclusion_label(item.exclusion_reason)}",
                    xalign=0,
                )
                self._retained_box.append(row)
            if len(protected) > 8:
                more = Gtk.Label(
                    label=fmt("DesktopOrganization.Window.ShowMore", len(protected) - 8),
                    xalign=0,
                )
                more.add_css_class("dim-label")
                self._retained_box.append(more)

    def _build_target_card(self, target, widgets) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.set_margin_start(6)
        card.set_margin_end(6)

        header = Gtk.Box(spacing=8)
        check = Gtk.CheckButton(label=target.suggested_display_name, active=True)
        check.connect(
            "toggled",
            lambda toggle: self._on_toggle_target(target, toggle.get_active()),
        )
        check.set_hexpand(True)
        check.set_halign(Gtk.Align.START)
        header.append(check)

        count = Gtk.Label(label=fmt("DesktopOrganization.Preview.ItemCount", len(target.items)))
        count.add_css_class("dim-label")
        header.append(count)
        card.append(header)

        routing = Gtk.Box(spacing=8)
        routing.append(Gtk.Label(label=t("DesktopOrganization.Preview.DestinationLabel")))
        options = [t("DesktopOrganization.Preview.NewWidget")] + [w.name for w in widgets]
        dropdown = Gtk.DropDown.new_from_strings(options)
        dropdown.set_selected(0 if target.creates_widget else min(1, len(options) - 1))
        dropdown.set_sensitive(bool(widgets))
        dropdown.connect(
            "notify::selected",
            lambda drop, _ps: self._on_destination(target, drop, widgets),
        )
        routing.append(dropdown)
        card.append(routing)
        return card

    def _on_toggle_target(self, target, selected: bool) -> None:
        selection = self._selections.get(target.source_bucket_id) or TargetSelection(
            source_bucket_id=target.source_bucket_id
        )
        selection.is_selected = selected
        self._selections[target.source_bucket_id] = selection
        self._refresh_execute_label()

    def _on_destination(self, target, dropdown, widgets) -> None:
        index = dropdown.get_selected()
        selection = self._selections.get(target.source_bucket_id) or TargetSelection(
            source_bucket_id=target.source_bucket_id
        )
        if index == 0:
            selection.destination_mode = DestinationMode.DYNAMIC
            selection.existing_widget_id = None
        else:
            widget = widgets[index - 1]
            selection.destination_mode = DestinationMode.EXISTING_WIDGET
            selection.existing_widget_id = widget.id
        self._selections[target.source_bucket_id] = selection

    def _on_opt_in(self, toggle: Gtk.CheckButton, path: str) -> None:
        lowered = path.lower()
        if toggle.get_active():
            self._opt_in.add(path)
        else:
            self._opt_in = {p for p in self._opt_in if p.lower() != lowered}
        self._rebuild_plan()

    def _refresh_execute_label(self) -> None:
        if self._plan is None:
            return
        selected = sum(
            len(target.items)
            for target in self._plan.targets
            if self._selections.get(target.source_bucket_id) is None
            or self._selections[target.source_bucket_id].is_selected
        )
        self._execute_button.set_label(fmt("DesktopOrganization.Layout.Execute", selected))
        self._execute_button.set_sensitive(selected > 0)

    # ---- execute --------------------------------------------------------------------

    def _on_execute(self) -> None:
        assert self._plan is not None
        self._apply_selections_to_plan()
        self._progress_bar.set_fraction(0.0)
        self._progress_label.set_text(t("DesktopOrganization.Preview.Preparing"))
        self._stack.set_visible_child_name("progress")

        def progress(completed: int, total: int, widget_id: str, name: str) -> None:
            self._progress_bar.set_fraction(completed / max(1, total))
            self._progress_label.set_text(fmt("DesktopOrganization.Preview.Progress", completed, total, name))

        try:
            result = self.service.execute(self._plan, progress=progress)
        except Exception as exc:  # surfaced on the result page, never a crash
            self._show_result(
                title=t("DesktopOrganization.Result.FailedTitle"),
                body=f"{t('DesktopOrganization.Result.FailedBody')}\n{exc}",
                retained=[],
                undo_entry_id=None,
            )
            return
        if self._on_committed is not None:
            try:
                self._on_committed()
            except Exception:
                pass
        self._undo_entry_id = result.history.id if result.history.items else None
        retained = result.retained_items
        if not result.history.items:
            self._show_result(
                title=t("DesktopOrganization.Result.FailedTitle"),
                body=t("DesktopOrganization.Result.FailedBody"),
                retained=retained,
                undo_entry_id=None,
            )
        elif retained:
            self._show_result(
                title=t("DesktopOrganization.Result.PartialTitle"),
                body=fmt(
                    "DesktopOrganization.Result.PartialBody",
                    len(result.history.items),
                    len(retained),
                ),
                retained=retained,
                undo_entry_id=self._undo_entry_id,
            )
        else:
            self._show_result(
                title=t("DesktopOrganization.Result.SuccessTitle"),
                body=fmt(
                    "DesktopOrganization.Result.SuccessBody",
                    len(result.history.items),
                    len(result.history.targets),
                ),
                retained=retained,
                undo_entry_id=self._undo_entry_id,
            )

    def _apply_selections_to_plan(self) -> None:
        assert self._plan is not None
        for target in self._plan.targets:
            selection = self._selections.get(target.source_bucket_id)
            if selection is None:
                continue
            if not selection.is_selected:
                target.items = []
                continue
            if selection.destination_mode == DestinationMode.EXISTING_WIDGET and selection.existing_widget_id:
                widget = next(
                    (w for w in self.service.settings.layout.widgets if w.id == selection.existing_widget_id),
                    None,
                )
                if widget is not None and widget.mappedFolderPath:
                    target.target_widget_id = widget.id
                    target.suggested_display_name = widget.name
                    target.target_directory_path = widget.mappedFolderPath
                    target.creates_widget = False
        self._plan.targets = [t_ for t_ in self._plan.targets if t_.items]

    # ---- result / undo -----------------------------------------------------------------

    def _show_result(self, title: str, body: str, retained, undo_entry_id) -> None:
        self._result_title.set_text(title)
        self._result_body.set_text(body)
        while child := self._result_retained_box.get_first_child():
            self._result_retained_box.remove(child)
        self._result_retained.set_visible(bool(retained))
        if retained:
            self._result_retained.set_label(t("DesktopOrganization.Result.RetainedDuringRun"))
            for item in retained[:10]:
                row = Gtk.Label(
                    label=f"{item.name} — {_retention_label(item.reason)}",
                    xalign=0,
                    wrap=True,
                )
                self._result_retained_box.append(row)
        self._undo_button.set_visible(undo_entry_id is not None)
        self._stack.set_visible_child_name("result")

    def _on_undo(self) -> None:
        if self._undo_entry_id is None:
            return
        try:
            self.service.transaction.undo(self._undo_entry_id)
            self._undo_button.set_visible(False)
            self._result_body.set_text(t("DesktopOrganization.Undo.Success"))
        except Exception as exc:
            self._result_body.set_text(f"{t('DesktopOrganization.Undo.Failed')}\n{exc}")
