"""Diagnostics bundle: privacy filter, log tail handling, archive export,
snapshot collection."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone

import pytest

from panebox.services.diagnostics import (
    DiagnosticsBundleService,
    account_values,
    collect_snapshot,
    read_sanitized_log_tail,
    sanitize_log,
)
from panebox.services.settings_service import SettingsService


# ---- privacy filter ------------------------------------------------------------------


def test_sanitize_redacts_sensitive_assignments_but_keeps_bools():
    text = "storagePath = /home/alice/PaneBox count = 3 enabled = True flag=false"
    out = sanitize_log(text)
    assert "storagePath=<REDACTED>" in out
    assert "alice" not in out
    assert "count = 3" in out  # neutral keys untouched
    assert "enabled = True" in out and "flag=false" in out  # bools survive


def test_sanitize_redacts_windows_and_posix_paths():
    text = (
        "open C:\\Users\\alice\\file.txt then '/home/alice/notes.md' "
        "and \\\\server\\share\\doc.docx plus /home/alice/pic.png tail"
    )
    out = sanitize_log(text)
    assert "alice" not in out
    assert "C:\\Users" not in out and "\\\\server" not in out
    assert "<PATH>" in out and "'<PATH>'" in out


def test_sanitize_keeps_urls_intact_and_redacts_emails_sids_users():
    text = (
        "fetched https://example.com/home/feed ok from bob.smith@example.com "
        "owner S-1-5-21-3623811015-3361044348-30300820 user alice"
    )
    out = sanitize_log(text)
    assert "https://example.com/home/feed" in out  # URL host is not a path match
    assert "bob.smith@example.com" not in out and "<EMAIL>" in out
    assert "S-1-5-21" not in out and "<SID>" in out
    # the account candidate list must include the running test user
    assert account_values()
    if "alice" in [value.lower() for value in account_values()]:
        assert "alice" not in out


def test_log_tail_snaps_to_line_boundary_and_caps_size(tmp_path):
    log = tmp_path / "log.txt"
    lines = [f"line-{i} path = /home/u/{i}" for i in range(100)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = read_sanitized_log_tail(log, max_bytes=300)
    assert "line-0 " not in out  # the cut partial line is dropped
    assert out.startswith("line-")
    assert "/home/u/" not in out


def test_missing_log_file_uses_placeholder(tmp_path):
    out = read_sanitized_log_tail(tmp_path / "nope.log")
    assert out == "PaneBox log was not available when the package was created."


# ---- bundle export -------------------------------------------------------------------


def _snapshot() -> dict:
    return {
        "schemaVersion": 5,
        "generatedAtUtc": datetime(2026, 9, 26, 8, 0, 0, tzinfo=timezone.utc).isoformat(),
        "appVersion": "0.3.0",
        "widgetManager": {"loadedSurfaceCount": 0, "hosts": []},
    }


def test_export_writes_three_entries_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "alice")  # deterministic account redaction
    log = tmp_path / "panebox.log"
    log.write_text("opened /home/alice/secret.txt for bob@x.io\n", encoding="utf-8")
    service = DiagnosticsBundleService(log=lambda _m: None)

    archive = service.export(tmp_path / "out", _snapshot(), log)

    # The archive stamp is the snapshot time in LOCAL time (C# ToLocalTime()).
    expected = (
        datetime.fromisoformat(_snapshot()["generatedAtUtc"])
        .astimezone()
        .strftime("PaneBox-Diagnostics-%Y%m%d-%H%M%S.zip")
    )
    assert archive.name == expected
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["diagnostics.json", "README.txt", "PaneBox-sanitized.log"]
        snapshot = json.loads(zf.read("diagnostics.json"))
        assert snapshot["schemaVersion"] == 5
        readme = zf.read("README.txt").decode("utf-8")
        assert "diagnostic package" in readme and "诊断包" in readme
        sanitized = zf.read("PaneBox-sanitized.log").decode("utf-8")
        assert "alice" not in sanitized and "bob@x.io" not in sanitized
    assert not list((tmp_path / "out").glob("*.tmp"))


def test_export_collides_to_numbered_suffix(tmp_path):
    service = DiagnosticsBundleService(log=lambda _m: None)
    first = service.export(tmp_path, _snapshot())
    second = service.export(tmp_path, _snapshot())
    assert first.exists() and second.exists() and first != second
    assert second.stem.endswith("-2")


def test_export_failure_cleans_temporary(tmp_path):
    service = DiagnosticsBundleService(log=lambda _m: None)
    with pytest.raises(Exception):
        service.export("", _snapshot())  # empty destination rejected
    assert not list(tmp_path.glob("*.tmp"))


# ---- snapshot collection ---------------------------------------------------------------


def test_collect_snapshot_headless(tmp_path):
    settings = SettingsService(config_root=tmp_path / "config")
    settings.load()
    snapshot = collect_snapshot(settings, widget_manager=None, log=lambda _m: None)
    assert snapshot["schemaVersion"] == 5
    assert snapshot["distributionChannel"] == "linux-port"
    assert snapshot["settings"]["loadRecoveryState"]
    assert snapshot["widgetManager"]["sessionState"] == "Unavailable"
    assert isinstance(snapshot["displays"], list)
    assert snapshot["hotkeys"]["toggleKey"]  # keysym resolved to a name
