"""Recovery journal + history stores and the history retention policy.

Ports DesktopOrganizationRecoveryStore.cs, DesktopOrganizationHistoryStore.cs
and OrganizationHistoryPolicy.cs. Both stores persist through
ResilientJsonStore (tmp+rename, .bak rotation, corrupt quarantine).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..constants import DATA_ROOT
from .organize_models import (
    OrganizationHistoryEntry,
    OrganizationActionType,
    RecoveryJournal,
)
from .resilient_json_store import ResilientJsonStore

# SettingsService.MaxRecentOrganizationHistoryCount
MAX_RECENT_ORGANIZATION_HISTORY_COUNT = 24

# Receipt bounds: batches above the per-entry cap keep only a summary (never
# partially undoable); the global budget downgrades oldest entries first.
MAX_UNDO_RECEIPT_ITEMS_PER_ENTRY = 500
MAX_UNDO_RECEIPT_ITEM_BUDGET = 2500


class RecoveryStore:
    """desktop-organization-recovery.json — the crash-recovery journal (WAL)."""

    def __init__(self, journal_path: Path | None = None):
        path = Path(journal_path) if journal_path else (Path(DATA_ROOT) / "data" / "desktop-organization-recovery.json")
        self.path = path
        self._store = ResilientJsonStore(path)

    @property
    def has_pending_journal(self) -> bool:
        # A surviving .bak is itself a valid recovery source.
        return self.path.exists() or self._store.backup_path.exists()

    def load(self) -> RecoveryJournal | None:
        result = self._store.load()
        if not result.data:
            return None
        return RecoveryJournal.from_dict(result.data)

    def save(self, journal: RecoveryJournal) -> None:
        self._store.save(journal.to_dict())

    def clear(self) -> None:
        # Backup dies BEFORE the primary: a crash between the deletes must
        # leave the most recent journal state as the only recoverable copy.
        backup = self._store.backup_path
        if backup.exists():
            backup.unlink()
        if self.path.exists():
            self.path.unlink()


class HistoryStore:
    """desktop-organization-history.json — undo receipts, newest first."""

    def __init__(self, history_path: Path | None = None):
        path = Path(history_path) if history_path else (Path(DATA_ROOT) / "data" / "desktop-organization-history.json")
        self.path = path
        self._store = ResilientJsonStore(path)
        self.entries: list[OrganizationHistoryEntry] = []

    def load(self, legacy_seed: list[OrganizationHistoryEntry] | None = None) -> bool:
        """True when this store is authoritative (loaded, migrated, or empty)."""
        result = self._store.load()
        if result.data:
            self.entries, changed = _normalize_entries(_entries_from(result.data))
            if changed:
                self.save_checked()
            return True
        self.entries, _changed = _normalize_entries(legacy_seed or [])
        return not self.entries or self.save_checked()

    def save(self) -> None:
        self._store.save({"items": [entry.to_dict() for entry in self.entries]})

    def save_checked(self) -> bool:
        try:
            self.save()
            return True
        except OSError:
            return False


def _entries_from(data: dict) -> list[OrganizationHistoryEntry]:
    raw = data.get("items")
    return [OrganizationHistoryEntry.from_dict(item) for item in raw] if isinstance(raw, list) else []


def _normalize_entries(source: list[OrganizationHistoryEntry]):
    """Sort newest-first and fill field defaults; the cap is the policy's call."""
    changed = False
    entries = sorted(
        (entry for entry in source if entry is not None),
        key=lambda entry: entry.timestampUtc,
        reverse=True,
    )
    for entry in entries:
        if not entry.id:
            import uuid

            entry.id = str(uuid.uuid4())
            changed = True
        if not entry.actionType:
            entry.actionType = OrganizationActionType.MANAGED_DROP
            changed = True
        if entry.transferMode not in ("Move", "Copy"):
            entry.transferMode = "Move"
            changed = True
    return entries, changed


# ---- retention policy -----------------------------------------------------------


def is_undo_lifecycle_active(entry: OrganizationHistoryEntry) -> bool:
    return entry.canUndo and not entry.isUndone and (entry.undoStarted or any(item.isRestored for item in entry.items))


def apply_retention_policy(
    history: list[OrganizationHistoryEntry],
    protected_transaction_id: str | None = None,
) -> bool:
    """Entry cap + per-entry receipt cap + global budget. Returns changed."""

    def protected(entry: OrganizationHistoryEntry) -> bool:
        return is_undo_lifecycle_active(entry) or (
            protected_transaction_id is not None and entry.id == protected_transaction_id
        )

    if not history:
        return False

    changed = _enforce_entry_cap(history, protected)

    for entry in history:
        if protected(entry):
            continue
        if entry.undoReceiptsDiscarded:
            entry.canUndo = False
            if entry.items:
                downgrade_to_summary(entry)
                changed = True
            continue
        if not entry.items:
            continue
        if entry.totalItemCount < len(entry.items):
            entry.totalItemCount = len(entry.items)
            changed = True
        if len(entry.items) > MAX_UNDO_RECEIPT_ITEMS_PER_ENTRY:
            downgrade_to_summary(entry)
            changed = True

    total_items = sum(len(entry.items) for entry in history if not protected(entry) and not entry.undoReceiptsDiscarded)
    for index in range(len(history) - 1, -1, -1):
        if total_items <= MAX_UNDO_RECEIPT_ITEM_BUDGET:
            break
        entry = history[index]
        if protected(entry) or entry.undoReceiptsDiscarded or not entry.items:
            continue
        total_items -= len(entry.items)
        downgrade_to_summary(entry)
        changed = True

    return changed


def merge_retry_history(
    history: OrganizationHistoryEntry,
    previous: OrganizationHistoryEntry,
) -> None:
    history.items[0:0] = previous.items
    if previous.undoReceiptsDiscarded:
        history.undoReceiptsDiscarded = True
        history.canUndo = False
        history.totalItemCount = previous.totalItemCount + len(history.items)


def downgrade_to_summary(entry: OrganizationHistoryEntry) -> None:
    entry.totalItemCount = max(entry.totalItemCount, len(entry.items))
    entry.items.clear()
    entry.canUndo = False
    entry.undoStarted = False
    entry.undoReceiptsDiscarded = True


def cap_entry_receipts(entry: OrganizationHistoryEntry) -> None:
    """Cap one freshly appended entry without touching older history."""
    if not entry.items:
        return
    if entry.totalItemCount < len(entry.items):
        entry.totalItemCount = len(entry.items)
    if len(entry.items) > MAX_UNDO_RECEIPT_ITEMS_PER_ENTRY:
        downgrade_to_summary(entry)


def _enforce_entry_cap(history: list[OrganizationHistoryEntry], protected) -> bool:
    cap = MAX_RECENT_ORGANIZATION_HISTORY_COUNT
    if len(history) <= cap:
        return False
    keep: set[int] = set()
    for index, entry in enumerate(history):
        if protected(entry):
            keep.add(index)
    for index in sorted(
        (i for i in range(len(history)) if i not in keep),
        key=lambda i: history[i].timestampUtc,
        reverse=True,
    )[: max(0, cap - len(keep))]:
        keep.add(index)
    kept = [entry for index, entry in enumerate(history) if index in keep]
    removed = len(history) - len(kept)
    if removed:
        history[:] = kept
    return removed > 0


def is_empty_directory(path: str) -> bool:
    try:
        return not os.path.isdir(path) or not os.listdir(path)
    except OSError:
        return False
