"""Cloud backup: domains, WebDAV transport parsing, scoped export/restore
round-trips, orphan remaps, retention, scheduling, credentials."""

from __future__ import annotations

import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from panebox.services import backup_domains as domains
from panebox.services.cloud_backup import (
    CloudBackupOptions,
    CloudBackupService,
    build_remote_snapshot_name,
    credential_key,
    get_options,
    is_safe_snapshot_basename,
    normalize_remote_path,
    parse_snapshot_timestamp,
)
from panebox.services.data_backup import (
    DataBackupService,
    PendingRestoreError,
    RestoreValidationError,
    ScopedBackupError,
)
from panebox.services.quick_capture_store import QuickCaptureStore
from panebox.services.todo_store import TodoWidgetStore
from panebox.services.webdav_transport import (
    CloudBackupTransportException,
    RemoteEntry,
    WebDavBackupTransport,
    parse_multistatus,
)
from panebox.models.quick_capture import QuickCaptureItem, QuickCaptureStoreData
from panebox.models.todo import TodoItem, TodoWidgetData
from panebox.services.settings_service import SettingsService

ALL = domains.TODO_DATA | domains.QUICK_CAPTURE_DATA | domains.WIDGET_STYLE


# ---- domains ---------------------------------------------------------------------


def test_domain_membership_and_manifest_names():
    assert domains.is_in_domain(domains.TODO_DATA, "widgets/abc/todo.json")
    assert not domains.is_in_domain(domains.TODO_DATA, "widgets/abc/todo.json.bak")
    assert not domains.is_in_domain(domains.TODO_DATA, "widgets/abc/attachments/f.png")
    assert domains.is_in_domain(domains.QUICK_CAPTURE_DATA, "quick-capture/quick-capture.json")
    assert not domains.is_in_domain(domains.QUICK_CAPTURE_DATA, "quick-capture/images/a.png")
    assert not domains.is_in_domain(domains.WIDGET_STYLE, "widget-style.json")  # owns no files
    assert domains.is_in_scope(ALL, "widgets/x/todo.json")
    assert domains.is_in_scope(domains.QUICK_CAPTURE_DATA, "widgets/x/todo.json") is False
    assert domains.to_manifest_names(ALL) == ["todo-data", "quick-capture-data", "widget-style"]
    assert domains.try_from_manifest_names(["todo-data", "widget-style"]) == domains.TODO_DATA | domains.WIDGET_STYLE
    assert domains.try_get_todo_widget_id("widgets/ID1/todo.json") == "ID1"
    assert domains.try_get_todo_widget_id("widgets/a/b/todo.json") is None


# ---- WebDAV multistatus --------------------------------------------------------------


MULTISTATUS = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/dav/PaneBox/backups/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/PaneBox/backups/snap1.zip</d:href>
    <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>2048</d:getcontentlength>
      <d:getlastmodified>Mon, 26 Sep 2026 10:00:00 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/PaneBox/backups/evil%2F..%2Fescape.zip</d:href>
    <d:propstat><d:prop><d:displayname>totally-legit.zip</d:displayname></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/PaneBox/backups/locked.zip</d:href>
    <d:propstat><d:prop><d:getcontentlength>1</d:getcontentlength></d:prop>
      <d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat>
  </d:response>
</d:multistatus>""".encode("utf-8")


def test_parse_multistatus_names_sizes_and_traversal():
    entries = parse_multistatus(MULTISTATUS, "https://example.com/dav/PaneBox/backups")
    by_name = {entry.name: entry for entry in entries}
    assert "snap1.zip" in by_name
    assert by_name["snap1.zip"].size == 2048
    assert by_name["snap1.zip"].is_directory is False
    assert by_name["snap1.zip"].modified_utc == "Mon, 26 Sep 2026 10:00:00 GMT"
    # the requested collection itself is skipped
    assert "backups" not in by_name
    # name comes from the href segment, never displayname; the encoded
    # slashes stay one segment so traversal cannot route a delete elsewhere
    assert any(".." in entry.name for entry in entries) is False
    assert "totally-legit.zip" not in by_name
    # non-200 propstats contribute nothing
    assert "locked.zip" in by_name and by_name["locked.zip"].size == 0


def test_transport_rejects_bad_urls_and_maps_errors():
    with pytest.raises(CloudBackupTransportException):
        WebDavBackupTransport("ftp://example.com/dav")
    transport = WebDavBackupTransport("https://example.com/dav", "u", "p")
    assert transport._url("PaneBox/backups") == "https://example.com/dav/PaneBox/backups"
    assert transport._url("a b/c%2Fd") == "https://example.com/dav/a%20b/c%252Fd"
    response = type("R", (), {"status_code": 401, "text": ""})()
    with pytest.raises(CloudBackupTransportException) as exc:
        transport._raise_for_status(response, "put")
    assert "authentication failed" in str(exc.value).lower()
    assert exc.value.status_code == 401


def test_snapshot_name_helpers():
    name = build_remote_snapshot_name("abcdef1234567890", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert name == "PaneBox-CloudBackup-20260102T030405Z-abcdef12.zip"
    assert parse_snapshot_timestamp(name) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert is_safe_snapshot_basename(name)
    for bad in (
        "../evil.zip",
        "sub/PaneBox-CloudBackup-20260102T030405Z-abcdef12.zip",
        "other.zip",
    ):
        assert not is_safe_snapshot_basename(bad)
    # prefix+ext with no timestamp parses to None but stays a "safe" name
    assert is_safe_snapshot_basename("PaneBox-CloudBackup-x.zip")
    assert parse_snapshot_timestamp("PaneBox-CloudBackup-x.zip") is None
    assert normalize_remote_path("") == "PaneBox/backups"
    assert normalize_remote_path("/a/../b/") == "PaneBox/backups"
    assert normalize_remote_path("Nextcloud/PaneBox") == "Nextcloud/PaneBox"


def test_credential_key_scopes_endpoint_not_remote_path():
    base = dict(
        provider="webdav",
        server_url="https://dav.example.com/remote.php/webdav",
        remote_path="PaneBox/backups",
        username="u@x.com",
        scope=7,
        retention_count=5,
        interval_hours=24,
        last_success_utc=None,
    )
    key = credential_key(CloudBackupOptions(**base))
    assert key == "webdav:u@x.com@https://dav.example.com/remote.php/webdav"
    other_path = {**base, "remote_path": "somewhere/else"}
    assert credential_key(CloudBackupOptions(**other_path)) == key  # folder ≠ auth boundary
    other_user = {**base, "username": "v@x.com"}
    assert credential_key(CloudBackupOptions(**other_user)) != key


# ---- export / restore environment ----------------------------------------------------


class Env:
    def __init__(self, tmp_path: Path):
        self.data_dir = tmp_path / "data"
        self.root = tmp_path / "root"
        self.data_dir.mkdir(parents=True)
        self.root.mkdir(parents=True)
        self.config = tmp_path / "config"
        self.service = SettingsService(config_root=self.config)
        self.service.load()
        self.backup = DataBackupService(settings_service=self.service, data_dir=self.data_dir, root=self.root)

    def add_todo_widget(self, widget_id: str, items: list[TodoItem]) -> None:
        from panebox.models.widget_config import WidgetConfig

        widget = WidgetConfig(id=widget_id, name="Tasks", widgetKind="Todo")
        self.service.layout.widgets.append(widget)
        TodoWidgetStore(widget_id, widgets_root=self.data_dir / "widgets").save(TodoWidgetData(items=items))

    def todo_items(self, widget_id: str) -> list[TodoItem]:
        return TodoWidgetStore(widget_id, widgets_root=self.data_dir / "widgets").load().items

    def qc_items(self) -> list[QuickCaptureItem]:
        return QuickCaptureStore(data_dir=self.data_dir / "quick-capture").load().items


def item(text: str, *, updated: datetime, item_id: str = "", deleted: bool = False) -> TodoItem:
    return TodoItem(
        id=item_id or text,
        text=text,
        updatedAt=updated,
        createdAt=updated,
        isDeleted=deleted,
    )


def qc(body: str, *, updated: datetime, item_id: str = "") -> QuickCaptureItem:
    return QuickCaptureItem(id=item_id or body, body=body, updatedAt=updated, createdAt=updated)


NEWER = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
OLDER = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path):
    return Env(tmp_path)


def test_export_manifest_and_zip_layout(env):
    env.add_todo_widget("wid1", [item("remote-newer", updated=NEWER)])
    QuickCaptureStore(data_dir=env.data_dir / "quick-capture").save(
        QuickCaptureStoreData(items=[qc("note", updated=NEWER)])
    )
    env.service.settings.widgetShell.widgetOpacity = 0.42

    result = env.backup.export_scoped_backup(
        env.root / "snap.zip", ALL, app_version="1.5.5", source_device_id="deviceAAA"
    )

    assert result.todo_stores == 1 and result.quick_capture and result.widget_style
    with zipfile.ZipFile(env.root / "snap.zip") as zf:
        names = zf.namelist()
        assert "data/widgets/wid1/todo.json" in names
        assert "data/quick-capture/quick-capture.json" in names
        assert "widget-style.json" in names
        assert names[-1] == "manifest.json"  # manifest written last
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["kind"] == "cloud-backup" and manifest["schemaVersion"] == 2
        assert manifest["domains"] == ["todo-data", "quick-capture-data", "widget-style"]
        assert manifest["sourceDeviceId"] == "deviceAAA"
        listed = {entry["path"] for entry in manifest["files"]}
        assert listed == {
            "data/widgets/wid1/todo.json",
            "data/quick-capture/quick-capture.json",
            "widget-style.json",
        }
        doc = json.loads(zf.read("widget-style.json"))
        assert doc["shell"]["widgetOpacity"] == 0.42
        assert doc["widgets"]["wid1"]["widgetKind"] == "Todo"


def test_export_nothing_in_scope_errors(env):
    with pytest.raises(ScopedBackupError):
        env.backup.export_scoped_backup(env.root / "snap.zip", domains.TODO_DATA)


def test_merge_restore_round_trip_never_deletes(env):
    env.service.settings.widgetShell.widgetOpacity = 0.55  # snapshot appearance
    env.add_todo_widget(
        "wid1",
        [
            item("shared", updated=OLDER),
            item("snapshot-only", updated=OLDER),
            item("local-only", updated=OLDER),
        ],
    )
    QuickCaptureStore(data_dir=env.data_dir / "quick-capture").save(
        QuickCaptureStoreData(items=[qc("local-qc", updated=OLDER)])
    )
    archive = env.root / "snap.zip"
    env.backup.export_scoped_backup(archive, ALL)

    # After the snapshot, local state evolves on its own.
    TodoWidgetStore("wid1", widgets_root=env.data_dir / "widgets").save(
        TodoWidgetData(items=[item("shared", updated=NEWER), item("local-only", updated=OLDER)])
    )
    QuickCaptureStore(data_dir=env.data_dir / "quick-capture").save(
        QuickCaptureStoreData(items=[qc("local-qc", updated=OLDER), qc("new-local-qc", updated=OLDER)])
    )
    env.service.settings.widgetShell.widgetOpacity = 0.9

    prep = env.backup.prepare_scoped_restore(archive, ALL)
    assert prep.todo_stores == 1 and prep.quick_capture and prep.widget_style
    assert env.backup.has_pending_restore()

    applied = env.backup.apply_pending_restore()
    assert applied is not None and applied.success
    items = {i.id: i for i in env.todo_items("wid1")}
    # local edit is NEWER than the snapshot → local wins
    assert items["shared"].updatedAt == NEWER
    # snapshot item absent locally comes back (merge never deletes)
    assert "snapshot-only" in items and "local-only" in items
    qc_bodies = {i.body for i in env.qc_items()}
    assert qc_bodies == {"local-qc", "new-local-qc"}
    # marker + staging gone
    assert not env.backup.has_pending_restore()
    assert not env.backup.restore_staging_dir.exists() or not any(env.backup.restore_staging_dir.iterdir())
    # widget style restored from the snapshot
    env.service.load()
    assert env.service.settings.widgetShell.widgetOpacity == pytest.approx(0.55)


def test_merge_prefers_newer_remote_but_tombstone_never_wins(env):
    env.add_todo_widget("wid1", [item("shared", updated=OLDER), item("alive", updated=OLDER)])
    archive = env.root / "snap.zip"

    # Build the "remote" snapshot with newer data + a tombstone for 'alive'.
    store = TodoWidgetStore("wid1", widgets_root=env.data_dir / "widgets")
    store.save(
        TodoWidgetData(
            items=[
                item("shared", updated=NEWER),  # remote newer → wins
                item("alive", updated=NEWER, deleted=True),  # tombstone vs live local → loses
            ]
        )
    )
    env.backup.export_scoped_backup(archive, domains.TODO_DATA)
    store.save(TodoWidgetData(items=[item("shared", updated=OLDER), item("alive", updated=OLDER)]))

    env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)
    result = env.backup.apply_pending_restore()
    assert result.success
    items = {i.id: i for i in env.todo_items("wid1")}
    assert items["shared"].updatedAt == NEWER  # strictly newer remote replaced local
    assert items["alive"].isDeleted is False  # remote tombstone cannot delete a live item


def test_replace_mode_wipes_in_domain_data(env):
    env.add_todo_widget("wid1", [item("snapshot-item", updated=OLDER)])
    archive = env.root / "snap.zip"
    env.backup.export_scoped_backup(archive, domains.TODO_DATA)

    store = TodoWidgetStore("wid1", widgets_root=env.data_dir / "widgets")
    store.save(TodoWidgetData(items=[item("local-extra", updated=NEWER)]))

    env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)
    assert env.backup.set_pending_restore_item_replace_mode(True)
    result = env.backup.apply_pending_restore()
    assert result.success
    ids = {i.id for i in env.todo_items("wid1")}
    assert ids == {"snapshot-item"}  # local items outside the snapshot are gone


def test_pending_restore_blocks_second_prepare_and_upload(env):
    env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    archive = env.root / "snap.zip"
    env.backup.export_scoped_backup(archive, domains.TODO_DATA)
    env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)
    with pytest.raises(PendingRestoreError):
        env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)
    assert env.backup.cancel_pending_restore()
    assert not env.backup.has_pending_restore()
    # cancel removed staging
    assert not env.backup.restore_staging_dir.exists() or not any(env.backup.restore_staging_dir.iterdir())


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_corrupted_archive_fails_validation_without_marker(env):
    env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    archive = env.root / "snap.zip"
    env.backup.export_scoped_backup(archive, domains.TODO_DATA)
    with zipfile.ZipFile(archive, "a") as zf:
        # Tamper with the payload after the fact → sha256 mismatch.
        zf.writestr("data/widgets/wid1/todo.json", json.dumps({"version": 3, "items": []}))
    with pytest.raises(RestoreValidationError):
        env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)
    assert not env.backup.has_pending_restore()


def test_requested_domain_absent_from_backup_errors(env):
    env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    archive = env.root / "snap.zip"
    env.backup.export_scoped_backup(archive, domains.TODO_DATA)
    with pytest.raises(RestoreValidationError):
        env.backup.prepare_scoped_restore(archive, domains.QUICK_CAPTURE_DATA)


def test_orphan_remap_moves_snapshot_store_onto_live_widget(env):
    # Device A snapshot: todo store under 'old-id', live widget named there.
    env.add_todo_widget("old-id", [item("from-other-device", updated=OLDER)])
    archive = env.root / "snap.zip"
    env.backup.export_scoped_backup(archive, domains.TODO_DATA)

    # This device's live todo widget has a different id and an empty store.
    from panebox.models.widget_config import WidgetConfig

    env.service.layout.widgets = [WidgetConfig(id="new-id", name="Tasks", widgetKind="Todo")]
    TodoWidgetStore("new-id", widgets_root=env.data_dir / "widgets").save(TodoWidgetData())

    prep = env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)
    assert prep.remapped == 1 and prep.unmapped == 0
    result = env.backup.apply_pending_restore()
    assert result.success
    assert {i.id for i in env.todo_items("new-id")} == {"from-other-device"}
    # the live old-id store (pre-existing local orphan) is left untouched
    assert (env.data_dir / "widgets" / "old-id" / "todo.json").exists()


def test_source_orphan_debris_is_dropped(env):
    """A staged widget dir the archive's widget-style document does NOT name
    is the source device's own orphan debris — dropped from the restore."""
    import hashlib

    env.add_todo_widget("real", [item("real-item", updated=OLDER)])
    archive = env.root / "snap.zip"
    # The style domain must ship too: its document is the source's liveness list.
    env.backup.export_scoped_backup(archive, domains.TODO_DATA | domains.WIDGET_STYLE)

    debris_payload = json.dumps(TodoWidgetData(items=[item("junk", updated=OLDER)]).to_dict()).encode("utf-8")
    debris_entry = "data/widgets/debris/todo.json"
    # Rebuild the archive with the debris entry folded into the manifest so
    # integrity passes and only the liveness check can reject it.
    rebuilt = env.root / "with-debris.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(rebuilt, "w") as zf:
        manifest = json.loads(source.read("manifest.json"))
        for name in source.namelist():
            if name == "manifest.json":
                continue
            zf.writestr(name, source.read(name))
        zf.writestr(debris_entry, debris_payload)
        manifest["files"].append(
            {
                "path": debris_entry,
                "length": len(debris_payload),
                "sha256": hashlib.sha256(debris_payload).hexdigest(),
            }
        )
        zf.writestr("manifest.json", json.dumps(manifest))

    prep = env.backup.prepare_scoped_restore(rebuilt, domains.TODO_DATA)
    assert prep.dropped_source_orphans == 1
    result = env.backup.apply_pending_restore()
    assert result.success
    assert {i.id for i in env.todo_items("real")} == {"real-item"}
    assert not (env.data_dir / "widgets" / "debris").exists()


def test_apply_retries_then_abandons(env, monkeypatch):
    env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    archive = env.root / "snap.zip"
    env.backup.export_scoped_backup(archive, domains.TODO_DATA)
    env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)

    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise OSError("disk on fire")

    monkeypatch.setattr(env.backup, "_apply_scoped_restore_core", boom)
    for _ in range(2):  # attempts 1..2 keep the marker
        result = env.backup.apply_pending_restore()
        assert result.success is False and result.abandoned is False
        assert env.backup.has_pending_restore()
    result = env.backup.apply_pending_restore()  # attempt 3 → abandoned
    assert result.abandoned and not env.backup.has_pending_restore()
    assert calls["n"] == 3


def test_safety_backup_written_and_capped(env):
    env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    archive = env.root / "snap.zip"
    env.backup.export_scoped_backup(archive, domains.TODO_DATA)
    env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)
    env.backup.safety_backup_dir.mkdir(parents=True, exist_ok=True)
    for i in range(7):  # simulate prior restores
        (env.backup.safety_backup_dir / f"pre-restore-2026010{i}T000000Z-00000{i}.zip").write_bytes(b"old")
    result = env.backup.apply_pending_restore()
    assert result.success and result.safety_backup_path is not None
    assert result.safety_backup_path.is_file()
    remaining = list(env.backup.safety_backup_dir.glob("pre-restore-*.zip"))
    assert len(remaining) == 5  # capped


# ---- cloud service orchestration ----------------------------------------------------


class FakeTransport:
    """In-memory WebDAV server: dict path → bytes."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.collections: set[str] = set()
        self.never_lists = False  # simulates a server whose listing lags

    def ensure_directory(self, remote_path: str) -> None:
        parts = remote_path.split("/")
        for i in range(1, len(parts) + 1):
            self.collections.add("/".join(parts[:i]))

    def upload(self, local_path: Path, remote_path: str) -> None:
        self.files[remote_path] = Path(local_path).read_bytes()

    def list(self, remote_path: str) -> list[RemoteEntry]:
        if self.never_lists:
            return []
        prefix = remote_path.rstrip("/") + "/"
        entries = []
        for path, payload in sorted(self.files.items()):
            if path.startswith(prefix) and "/" not in path[len(prefix) :]:
                entries.append(RemoteEntry(name=path[len(prefix) :], is_directory=False, size=len(payload)))
        return entries

    def download(self, remote_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[remote_path])

    def delete(self, remote_path: str) -> None:
        self.files.pop(remote_path, None)


class FakeCredentials:
    def __init__(self):
        self.passwords: dict[str, str] = {}

    def get_password(self, key):
        return self.passwords.get(key)

    def set_password(self, key, value, label="PaneBox"):
        self.passwords[key] = value
        return True

    def delete_password(self, key):
        return self.passwords.pop(key, None) is not None

    def list_keys(self):
        return sorted(self.passwords)

    def delete_keys_with_prefix(self, prefix):
        stale = [k for k in self.passwords if k.startswith(prefix)]
        for key in stale:
            self.passwords.pop(key)
        return len(stale)


class CloudEnv:
    def __init__(self, tmp_path: Path):
        self.env = Env(tmp_path)
        self.transport = FakeTransport()
        self.credentials = FakeCredentials()
        slice_ = self.env.service.settings.cloudBackup
        slice_.cloudBackupEnabled = True
        slice_.cloudBackupProvider = "webdav"
        slice_.cloudBackupUrl = "https://dav.example.com/remote.php/webdav"
        slice_.cloudBackupRemotePath = "PaneBox/backups"
        slice_.cloudBackupUsername = "user@example.com"
        self.env.service.save_now()
        self.service = CloudBackupService(
            self.env.service,
            data_backup=self.env.backup,
            credentials=self.credentials,
            sleeper=lambda _s: None,
        )
        self.service.create_transport = lambda options: self.transport

    def set_credential(self):
        options = get_options(self.env.service)
        self.credentials.set_password(credential_key(options), "app-token")


@pytest.fixture()
def cloud(tmp_path):
    return CloudEnv(tmp_path)


def test_run_backup_now_happy_path_and_retention(cloud):
    cloud.env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    cloud.set_credential()
    cloud.env.service.settings.cloudBackup.cloudBackupRetainCount = 5
    options = get_options(cloud.env.service)
    yesterday = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)

    for hour in (10, 11, 12, 13, 14, 15, 16, 17):
        name = build_remote_snapshot_name("deviceAAA", yesterday.replace(hour=hour))
        cloud.transport.files[f"{options.remote_path}/{name}"] = b"x" * 16

    result = cloud.service.run_backup_now(app_version="1.5.5", device_id="deviceAAA")
    assert result.outcome == "Uploaded"
    assert result.unverified is False
    assert is_safe_snapshot_basename(result.snapshot_name)
    assert f"{options.remote_path}/{result.snapshot_name}" in cloud.transport.files
    # 8 seeded + the fresh upload, pruned down to the retention count
    assert result.pruned == 4
    assert len(cloud.transport.files) == 5
    slice_ = cloud.env.service.settings.cloudBackup
    assert slice_.cloudBackupLastSuccessUtc
    assert slice_.cloudBackupLastResult == "ok"
    # local staging cleaned up
    assert not any(cloud.env.backup.backup_staging_dir.iterdir())


def test_run_backup_now_gates(cloud):
    cloud.env.service.settings.cloudBackup.cloudBackupUrl = ""
    assert cloud.service.run_backup_now().outcome == "NotConfigured"

    cloud.env.service.settings.cloudBackup.cloudBackupUrl = "https://dav.example.com/dav"
    cloud.env.service.settings.cloudBackup.cloudBackupTodoEnabled = False
    cloud.env.service.settings.cloudBackup.cloudBackupQuickCaptureEnabled = False
    cloud.env.service.settings.cloudBackup.cloudBackupWidgetStyleEnabled = False
    assert cloud.service.run_backup_now().outcome == "NoScope"

    cloud.env.service.settings.cloudBackup.cloudBackupTodoEnabled = True
    assert cloud.service.run_backup_now().outcome == "MissingCredential"
    cloud.set_credential()

    # pending restore blocks uploads
    cloud.env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    archive = cloud.env.root / "snap.zip"
    cloud.env.backup.export_scoped_backup(archive, domains.TODO_DATA)
    cloud.env.backup.prepare_scoped_restore(archive, domains.TODO_DATA)
    assert cloud.service.run_backup_now().outcome == "PendingRestore"
    cloud.env.backup.cancel_pending_restore()


def test_upload_verification_degrades_to_unverified(cloud):
    cloud.env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    cloud.set_credential()
    cloud.transport.never_lists = True
    result = cloud.service.run_backup_now(device_id="deviceAAA")
    assert result.outcome == "Uploaded" and result.unverified is True
    slice_ = cloud.env.service.settings.cloudBackup
    assert slice_.cloudBackupLastUnverifiedUtc and not slice_.cloudBackupLastSuccessUtc


def test_destination_identity_change_resets_stamps(cloud):
    cloud.env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    cloud.set_credential()
    assert cloud.service.run_backup_now(device_id="deviceAAA").outcome == "Uploaded"
    slice_ = cloud.env.service.settings.cloudBackup
    assert slice_.cloudBackupLastSuccessUtc

    # First observation after startup keeps the persisted stamp (C# semantics).
    cloud.service.refresh_options()
    assert cloud.env.service.settings.cloudBackup.cloudBackupLastSuccessUtc

    # A destination switch wipes the old destination's stamps…
    cloud.env.service.settings.cloudBackup.cloudBackupRemotePath = "other/folder"
    cloud.service.refresh_options()
    assert cloud.env.service.settings.cloudBackup.cloudBackupLastSuccessUtc is None

    # …so the new destination backs up immediately instead of waiting out the
    # old interval (spacing floor aside).
    assert cloud.service.is_due()
    cloud.service._last_attempt_monotonic = time.monotonic() - 700
    assert cloud.service.tick_scheduled().outcome == "Uploaded"
    slice_ = cloud.env.service.settings.cloudBackup
    assert slice_.cloudBackupLastSuccessUtc and slice_.cloudBackupLastResult == "ok"


def test_scheduled_tick_respects_interval_and_spacing(cloud):
    cloud.env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    cloud.set_credential()
    slice_ = cloud.env.service.settings.cloudBackup

    assert cloud.service.tick_scheduled().outcome == "Uploaded"
    assert cloud.service._last_attempt_monotonic > 0

    # immediately due again by interval (no last success? there is one now)
    slice_.cloudBackupIntervalHours = 24
    assert cloud.service.is_due() is False

    # backdate the success stamp → due again, but the 10-minute spacing floor
    slice_.cloudBackupLastSuccessUtc = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    cloud.service._last_attempt_monotonic = time.monotonic()
    assert cloud.service.is_due()
    assert cloud.service.tick_scheduled() is None  # spacing floor holds it

    cloud.service._last_attempt_monotonic = time.monotonic() - 700  # >10 min ago
    assert cloud.service.tick_scheduled().outcome == "Uploaded"

    slice_.cloudBackupEnabled = False
    assert cloud.service.tick_scheduled() is None and cloud.service.is_due() is False


def test_store_credential_sweeps_stale_keys(cloud):
    options = get_options(cloud.env.service)
    cloud.credentials.set_password("webdav:old@example.com@https://dav.example.com/old", "stale")
    assert cloud.service.store_credential(options, "app-token")
    assert cloud.credentials.get_password(credential_key(options)) == "app-token"
    assert "webdav:old@example.com@https://dav.example.com/old" not in cloud.credentials.passwords
    assert cloud.env.service.settings.cloudBackup.cloudBackupPasswordStored is True


def test_download_and_delete_snapshot_round_trip(cloud):
    cloud.env.add_todo_widget("wid1", [item("a", updated=OLDER)])
    cloud.set_credential()
    result = cloud.service.run_backup_now(device_id="deviceAAA")
    snapshots = cloud.service.list_remote_snapshots()
    assert [s.name for s in snapshots] == [result.snapshot_name]
    assert snapshots[0].device_id == "deviceAA"  # 8-char device suffix from the name

    destination = cloud.env.root / "downloaded.zip"
    cloud.service.download_snapshot(result.snapshot_name, destination)
    assert destination.read_bytes() == cloud.transport.files[f"PaneBox/backups/{result.snapshot_name}"]

    cloud.service.delete_snapshot(result.snapshot_name)
    assert cloud.service.list_remote_snapshots() == []
