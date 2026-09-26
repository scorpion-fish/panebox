"""ResilientJsonStore: atomic saves, .bak rotation, corrupt-file quarantine."""

from __future__ import annotations

import json
from pathlib import Path

from panebox.services.resilient_json_store import LoadState, ResilientJsonStore


def make_store(tmp_path: Path) -> ResilientJsonStore:
    return ResilientJsonStore(tmp_path / "settings.json")


def test_missing_file_loads_defaults(tmp_path):
    store = make_store(tmp_path)
    result = store.load()
    assert result.data == {}
    assert result.state == LoadState.DEFAULTS_FOR_MISSING_FILE


def test_save_writes_primary_and_seeds_backup(tmp_path):
    store = make_store(tmp_path)
    store.save({"core": {"theme": "Dark"}})
    assert json.loads(store.path.read_text())["core"]["theme"] == "Dark"
    # First save seeds the backup so recovery never lacks a .bak.
    assert json.loads(store.backup_path.read_text())["core"]["theme"] == "Dark"


def test_second_save_rotates_backup(tmp_path):
    store = make_store(tmp_path)
    store.save({"v": 1})
    store.save({"v": 2})
    assert json.loads(store.path.read_text())["v"] == 2
    assert json.loads(store.backup_path.read_text())["v"] == 1


def test_corrupt_primary_recovers_from_backup(tmp_path):
    store = make_store(tmp_path)
    store.save({"v": 1})
    store.path.write_text("{ this is not json", encoding="utf-8")

    result = store.load()
    assert result.data == {"v": 1}
    assert result.state == LoadState.RECOVERED_FROM_BACKUP
    # Corrupt file quarantined, primary restored from the backup.
    quarantined = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(quarantined) == 1
    assert json.loads(store.path.read_text()) == {"v": 1}


def test_non_object_root_is_treated_as_corrupt(tmp_path):
    store = make_store(tmp_path)
    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    result = store.load()
    # File existed but was unusable → failure state (not "missing").
    assert result.state == LoadState.DEFAULTS_AFTER_FAILURE
    assert list(tmp_path.glob("settings.json.corrupt-*")), "array root must quarantine"


def test_both_files_corrupt_reports_failure(tmp_path):
    store = make_store(tmp_path)
    store.save({"v": 1})
    store.path.write_text("nope", encoding="utf-8")
    store.backup_path.write_text("also nope", encoding="utf-8")
    result = store.load()
    assert result.state == LoadState.DEFAULTS_AFTER_FAILURE
    assert result.data == {}
