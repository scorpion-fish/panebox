"""OrganizeTransaction: execute/undo/crash-recovery semantics (journal WAL,
identity receipts, commit order, retention)."""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import pytest

from panebox.models.settings_slices import DesktopOrganizationRule
from panebox.models.widget_config import WidgetConfig
from panebox.services.organize_models import (
    CategoryIds,
    InsufficientSpaceError,
    IncompleteUndoError,
    PendingRecoveryError,
    RecoveryItem,
    RecoveryJournal,
    RetentionReason,
)
from panebox.services import organize_transaction as ot
from panebox.services.organize_planner import (
    DesktopOrganizationScanner,
    create_plan,
)
from panebox.services.organize_store import HistoryStore, RecoveryStore
from panebox.services.organize_transaction import (
    OrganizeStateError,
    OrganizeTransaction,
    capture_identity,
)
from panebox.services.settings_service import SettingsService


def _write(path, content: bytes = b"data"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def make_env(tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir(exist_ok=True)
    storage = tmp_path / "storage"
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)

    service = SettingsService(config_root=config)
    service.load()
    history = HistoryStore(tmp_path / "history.json")
    history.load()
    recovery = RecoveryStore(tmp_path / "journal.json")
    transaction = OrganizeTransaction(service, history, recovery)

    return SimpleNamespace(
        desktop=desktop,
        storage=storage,
        service=service,
        history=history,
        recovery=recovery,
        tx=transaction,
    )


def plan_for(env, rules=None):
    scan = DesktopOrganizationScanner(lambda: str(env.desktop)).scan()
    return create_plan(
        scan,
        str(env.storage),
        list(env.service.layout.widgets),
        rules if rules is not None else env.service.settings.desktopOrganization.desktopOrganizationRules,
        lambda cid: cid,
    )


def seed_desktop(env, files):
    for name in files:
        _write(str(env.desktop / name))


# ---- execute -----------------------------------------------------------------


def test_execute_moves_files_creates_widgets_rules_and_history(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx", "c.png", "d.png", "e.png"])

    result = env.tx.execute(plan_for(env))

    # files moved into per-category storage folders
    assert os.path.isfile(env.storage / "Documents" / "a.pdf")
    assert os.path.isfile(env.storage / "Documents" / "b.docx")
    assert sorted(os.listdir(env.storage / "Images")) == ["c.png", "d.png", "e.png"]
    assert os.listdir(env.desktop) == []

    # widgets + routing rules persisted
    widgets = {w.name: w for w in env.service.layout.widgets}
    assert set(widgets) == {"Documents", "Images"}
    documents = widgets["Documents"]
    assert documents.widgetKind == "File"
    assert documents.managedFolderName == "Documents"
    assert documents.mappedFolderPath == str(env.storage / "Documents")
    rules = env.service.settings.desktopOrganization.desktopOrganizationRules
    assert {r.targetWidgetId for r in rules} == {documents.id, widgets["Images"].id}
    assert any(CategoryIds.DOCUMENTS in r.categoryIds for r in rules)

    # history receipts carry durable identity
    entry = env.history.entries[0]
    assert len(entry.items) == 5 and entry.canUndo and not entry.isUndone
    for item in entry.items:
        assert item.destinationIdentity is not None
        assert item.destinationIdentity.matches(os.stat(item.destinationPath))

    assert not env.recovery.has_pending_journal
    assert len(result.completed_items) == 5 and not result.retained_items

    # persistence round-trip
    reloaded = SettingsService(config_root=tmp_path / "config")
    reloaded.load()
    assert {w.name for w in reloaded.layout.widgets} == {"Documents", "Images"}


def test_execute_retains_source_changed_after_scan(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx", "c.png", "d.png", "e.png"])
    plan = plan_for(env)
    _write(str(env.desktop / "a.pdf"), b"changed in between")  # mtime/size differ

    result = env.tx.execute(plan)

    assert [r.reason for r in result.retained_items] == [RetentionReason.SOURCE_CHANGED]
    assert os.path.isfile(env.desktop / "a.pdf")
    assert len(env.history.entries[0].items) == 4


def test_execute_rejects_invalid_plan_locations(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])

    plan = plan_for(env)
    plan.storage_root_path = str(env.desktop / "Managed")
    plan.targets[0].target_directory_path = str(env.desktop / "Managed" / "Documents")
    with pytest.raises(OrganizeStateError):
        env.tx.execute(plan)

    env2 = make_env(tmp_path)
    seed_desktop(env2, ["a.pdf", "b.docx"])
    plan2 = plan_for(env2)
    plan2.storage_root_path = "/"
    plan2.targets[0].target_directory_path = "/Documents"
    with pytest.raises(OrganizeStateError):
        env2.tx.execute(plan2)


def test_execute_validates_free_space(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])
    monkeypatch.setattr(ot, "compute_required_space_by_device", lambda plan: {str(tmp_path): 10**15})
    with pytest.raises(InsufficientSpaceError):
        env.tx.execute(plan_for(env))
    assert os.path.isfile(env.desktop / "a.pdf")


def test_execute_blocked_while_recovery_pending(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])
    env.recovery.save(RecoveryJournal(transactionId="stale"))
    with pytest.raises(PendingRecoveryError):
        env.tx.execute(plan_for(env))


def test_execute_canceled_items_retained(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])
    plan = plan_for(env)
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1  # let the first item through, cancel the rest

    result = env.tx.execute(plan, cancel=cancel)
    assert all(r.reason == RetentionReason.CANCELED for r in result.retained_items)
    assert len(env.history.entries[0].items) == 1


# ---- undo ----------------------------------------------------------------------


def test_undo_restores_files_by_identity(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx", "c.png", "d.png", "e.png"])
    result = env.tx.execute(plan_for(env))

    env.tx.undo(result.history.id)

    assert sorted(os.listdir(env.desktop)) == ["a.pdf", "b.docx", "c.png", "d.png", "e.png"]
    entry = env.history.entries[0]
    assert entry.isUndone and not entry.canUndo
    assert all(item.isRestored for item in entry.items)
    assert not env.recovery.has_pending_journal
    # widgets stay (matching the C# — undo empties the folder, not the widget)
    assert len(env.service.layout.widgets) == 2


def test_undo_refuses_to_clobber_replaced_file(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])
    result = env.tx.execute(plan_for(env))

    destination = env.history.entries[0].items[0].destinationPath
    os.remove(destination)
    _write(destination, b"a different object entirely")

    with pytest.raises(IncompleteUndoError):
        env.tx.undo(result.history.id)

    entry = env.history.entries[0]
    restored = [i for i in entry.items if i.isRestored]
    assert len(restored) == 1  # only the untouched item came back
    assert os.path.isfile(env.desktop / next(iter({i.name for i in restored})))
    with open(destination, "rb") as handle:
        assert handle.read() == b"a different object entirely"  # decoy untouched


# ---- crash recovery ----------------------------------------------------------------


def test_forward_journal_recovery_restores_uncommitted_moves(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])
    env.history.save_checked = lambda: False  # crash between moves and history commit

    with pytest.raises(OSError):
        env.tx.execute(plan_for(env))

    assert env.recovery.has_pending_journal  # receipts survived
    assert not os.listdir(env.desktop)

    env.history.save_checked = lambda: HistoryStore.save_checked(env.history)  # disk heals
    restored = env.tx.recover_pending()
    assert restored == 2
    assert sorted(os.listdir(env.desktop)) == ["a.pdf", "b.docx"]
    assert not os.listdir(env.storage / "Documents")  # files pulled back out
    assert not env.recovery.has_pending_journal
    assert env.history.entries == []


def test_recovery_ignores_items_already_committed(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])
    result = env.tx.execute(plan_for(env))
    assert not env.recovery.has_pending_journal

    # stale journal (e.g. .bak resurrected) duplicating committed receipts
    env.recovery.save(
        RecoveryJournal(
            transactionId=result.history.id,
            items=[
                RecoveryItem(
                    sourcePath=i.sourcePath,
                    destinationPath=i.destinationPath,
                    targetWidgetId=i.targetWidgetId,
                    completed=True,
                    destinationIdentity=i.destinationIdentity,
                )
                for i in result.history.items
            ],
        )
    )

    restored = env.tx.recover_pending()
    assert restored == 0
    assert os.path.isfile(env.storage / "Documents" / "a.pdf")  # committed stays put
    assert os.listdir(env.desktop) == []
    assert not env.recovery.has_pending_journal


def test_abandon_pending_recovery_removes_uncommitted_widgets(tmp_path):
    env = make_env(tmp_path)
    os.makedirs(env.storage / "Documents", exist_ok=True)  # empty created dir
    widget = WidgetConfig(
        id="w-pending",
        name="Documents",
        widgetKind="File",
        mappedFolderPath=str(env.storage / "Documents"),
    )
    env.service.layout.widgets.append(widget)
    env.service.settings.desktopOrganization.desktopOrganizationRules.append(
        DesktopOrganizationRule(targetWidgetId="w-pending", categoryIds=["Documents"])
    )
    env.service.save_now()
    env.recovery.save(RecoveryJournal(transactionId="t1", createdWidgetIds=["w-pending"], items=[]))

    env.tx.abandon_pending_recovery()

    assert not env.recovery.has_pending_journal
    assert all(w.id != "w-pending" for w in env.service.layout.widgets)
    assert env.service.settings.desktopOrganization.desktopOrganizationRules == []


def test_undo_journal_recovery_applies_receipts_then_undo_completes(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])
    env.tx.execute(plan_for(env))
    entry = env.history.entries[0]

    # crash mid-undo: one file already restored, receipt journaled
    first = entry.items[0]
    shutil.move(first.destinationPath, first.sourcePath)
    env.recovery.save(
        RecoveryJournal(
            transactionId=entry.id,
            isUndo=True,
            items=[
                RecoveryItem(
                    sourcePath=first.sourcePath,
                    destinationPath=first.destinationPath,
                    restorePath=first.sourcePath,
                    completed=True,
                    destinationIdentity=capture_identity(first.sourcePath),
                )
            ],
        )
    )

    restored = env.tx.recover_pending()
    assert restored == 1
    assert not env.recovery.has_pending_journal
    assert first.isRestored and not entry.isUndone and entry.canUndo

    env.tx.undo(entry.id)  # retry finishes the undo
    assert entry.isUndone
    assert sorted(os.listdir(env.desktop)) == ["a.pdf", "b.docx"]


def test_abandon_undo_marks_history_terminal(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx"])
    result = env.tx.execute(plan_for(env))

    entry = env.tx.abandon_undo(result.history.id)

    assert entry is not None and not entry.canUndo
    assert os.path.isfile(env.storage / "Documents" / "a.pdf")
    with pytest.raises(OrganizeStateError):
        env.tx.undo(result.history.id)


# ---- retry + retention ---------------------------------------------------------------


def test_retry_plan_merges_into_same_history_entry(tmp_path):
    env = make_env(tmp_path)
    seed_desktop(env, ["a.pdf", "b.docx", "c.png", "d.png", "e.png"])
    plan = plan_for(env)
    _write(str(env.desktop / "a.pdf"), b"changed")  # a.pdf retained
    env.tx.execute(plan)
    assert len(env.history.entries) == 1 and len(env.history.entries[0].items) == 4

    _write(str(env.desktop / "z.txt"))

    # the window rescans and re-plans, keeping the previous plan id so the
    # retry merges into the same history entry
    scan = DesktopOrganizationScanner(lambda: str(env.desktop)).scan()
    retry = create_plan(
        scan,
        str(env.storage),
        list(env.service.layout.widgets),
        env.service.settings.desktopOrganization.desktopOrganizationRules,
        lambda cid: cid,
    )
    retry.id = plan.id
    env.tx.execute(retry)

    assert len(env.history.entries) == 1  # merged, not appended
    entry = env.history.entries[0]
    assert len(entry.items) == 6  # 4 original + a.pdf + z.txt
    assert os.path.isfile(env.storage / "Documents" / "a.pdf")


def test_history_entry_cap(tmp_path):
    env = make_env(tmp_path)
    for index in range(26):
        seed_desktop(env, [f"f{index}.txt"])
        env.tx.execute(plan_for(env))

    assert len(env.history.entries) <= 24
    assert all(entry.canUndo or entry.isUndone for entry in env.history.entries)
