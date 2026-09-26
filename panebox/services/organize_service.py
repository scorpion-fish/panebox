"""OrganizeService — the window-facing facade over scanner/planner/transaction
(the usable subset of PaneBox's OrganizerService; the auto-organization
watcher is intentionally not ported — see README)."""

from __future__ import annotations

from typing import Callable, Optional

from ..models.settings_slices import DesktopOrganizationRule
from .organize_models import (
    ExecutionResult,
    ExclusionReason,
    OrganizationHistoryEntry,
    Plan,
    Rect,
    ScanResult,
)
from .organize_planner import (
    DesktopOrganizationScanner,
    assign_planned_bounds,
    create_plan,
)
from .organize_store import HistoryStore, RecoveryStore
from .organize_transaction import (
    OrganizeStateError,
    OrganizeTransaction,
    ProgressCallback,
)


class OrganizeService:
    def __init__(
        self,
        settings_service,
        history_store: Optional[HistoryStore] = None,
        recovery_store: Optional[RecoveryStore] = None,
    ):
        self.settings = settings_service
        self.history = history_store or HistoryStore()
        self.history.load()
        self.recovery = recovery_store or RecoveryStore()
        self.transaction = OrganizeTransaction(settings_service, self.history, self.recovery)

    # ---- recovery / undo ------------------------------------------------------

    def recover_pending(self) -> int:
        return self.transaction.recover_pending()

    @property
    def has_pending_recovery(self) -> bool:
        return self.transaction.has_pending_recovery

    def latest_undoable(self) -> Optional[OrganizationHistoryEntry]:
        return next(
            (entry for entry in self.history.entries if entry.canUndo and not entry.isUndone and entry.items),
            None,
        )

    def undo_latest(self) -> OrganizationHistoryEntry:
        latest = self.latest_undoable()
        if latest is None:
            raise OrganizeStateError("Nothing to undo.")
        self.transaction.undo(latest.id)
        return latest

    # ---- scan / plan ------------------------------------------------------------

    def scan(self, include_slow_items: bool = False) -> ScanResult:
        return DesktopOrganizationScanner(desktop_path_provider=self._desktop_path).scan(
            include_slow_items=include_slow_items
        )

    def create_plan(
        self,
        scan: ScanResult,
        include_slow_items: bool = False,
        opt_in_paths: Optional[set[str]] = None,
    ) -> Plan:
        """Plan with the user's opt-ins applied (folders / slow / batch items)."""
        opt_in = {p.lower() for p in (opt_in_paths or set())}
        items = [
            _with_exclusion(item, reason=ExclusionReason.NONE)
            if item.can_opt_in and item.source_path.lower() in opt_in
            else item
            for item in scan.items
        ]
        adjusted = ScanResult(
            desktop_path=scan.desktop_path,
            public_desktop_path=scan.public_desktop_path,
            public_desktop_unavailable=scan.public_desktop_unavailable,
            items=items,
        )
        plan = create_plan(
            adjusted,
            self.storage_root_path(),
            list(self.settings.layout.widgets),
            self.rules(),
            self._category_name,
        )
        self._assign_bounds(plan)
        return plan

    def rules(self) -> list[DesktopOrganizationRule]:
        return self.settings.settings.desktopOrganization.desktopOrganizationRules

    def storage_root_path(self) -> str:
        root = self.settings.settings.fileWidget.defaultManagedStorageRootPath
        import os

        return os.path.abspath(os.path.expanduser(root or "~/PaneBox"))

    # ---- execute -------------------------------------------------------------------

    def execute(
        self,
        plan: Plan,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> ExecutionResult:
        return self.transaction.execute(plan, progress=progress, cancel=cancel)

    # ---- internals -----------------------------------------------------------------

    def _desktop_path(self) -> str:
        from .file_ops import desktop_directory

        return desktop_directory()

    def _category_name(self, category_id: str) -> str:
        from ..i18n import t

        return t(f"DesktopOrganization.Category.{category_id}")

    def _assign_bounds(self, plan: Plan) -> None:
        try:
            from ..platform.workarea import list_monitors

            monitors = list_monitors()
        except Exception:
            return
        primary = next((m for m in monitors if m.is_primary), monitors[0] if monitors else None)
        if primary is None:
            return
        work = Rect(
            float(primary.work_x),
            float(primary.work_y),
            float(primary.work_width),
            float(primary.work_height),
        )
        shell = self.settings.settings.widgetShell
        occupied = [Rect(float(w.x), float(w.y), float(w.width), float(w.height)) for w in self.settings.layout.widgets]
        assign_planned_bounds(
            plan,
            work,
            occupied,
            float(shell.defaultWidgetWidth),
            float(shell.defaultWidgetHeight),
        )


def _with_exclusion(item, reason: str):
    clone = type(item)(**{**item.__dict__})
    clone.exclusion_reason = reason
    return clone
