"""Cloud-backup settings page under Xvfb: nav page builds, controls land in
the settings slice, snapshot list renders from the transport, and the restore
dialog stages a pending restore through the real DataBackupService."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SMOKE_DISPLAY = os.environ.get("PANEBOX_SMOKE_DISPLAY", ":99")


def _display_reachable(display: str) -> bool:
    try:
        subprocess.run(["xdpyinfo", "-display", display], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


GUI = _display_reachable(SMOKE_DISPLAY) and not os.environ.get("PANEBOX_SKIP_GUI")
if GUI:
    os.environ["DISPLAY"] = SMOKE_DISPLAY
    os.environ.setdefault("GDK_BACKEND", "x11")
    os.environ.setdefault("GSK_RENDERER", "cairo")
    os.environ.setdefault("NO_AT_BRIDGE", "1")

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(not GUI, reason=f"Xvfb {SMOKE_DISPLAY} not available (or PANEBOX_SKIP_GUI set)")

if GUI:
    import gi  # noqa: E402

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk  # noqa: E402

from panebox import i18n  # noqa: E402
from panebox.services import backup_domains as domains  # noqa: E402
from panebox.services.cloud_backup import (  # noqa: E402
    CloudBackupService,
    build_remote_snapshot_name,
    get_options,
)
from panebox.services.data_backup import DataBackupService  # noqa: E402
from panebox.services.settings_service import SettingsService  # noqa: E402
from panebox.services.todo_store import TodoWidgetStore  # noqa: E402
from panebox.services.webdav_transport import RemoteEntry  # noqa: E402
from panebox.models.todo import TodoItem, TodoWidgetData  # noqa: E402
from panebox.views.settings_window import SettingsHooks, SettingsWindow  # noqa: E402

ALL = domains.TODO_DATA | domains.QUICK_CAPTURE_DATA | domains.WIDGET_STYLE


def pump(ms: int = 40) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


class FakeTransport:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    def ensure_directory(self, remote_path: str) -> None:
        pass

    def upload(self, local_path: Path, remote_path: str) -> None:
        self.files[remote_path] = Path(local_path).read_bytes()

    def list(self, remote_path: str) -> list[RemoteEntry]:
        prefix = remote_path.rstrip("/") + "/"
        return [
            RemoteEntry(name=path[len(prefix) :], is_directory=False, size=len(payload))
            for path, payload in sorted(self.files.items())
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]

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


class FakeHooks(SettingsHooks):
    def __init__(self, cloud_backup):
        self.calls = {"save": 0}
        super().__init__(
            save=self._save,
            apply_theme=lambda _t: None,
            apply_language=lambda: None,
            apply_hotkey=lambda: SimpleNamespace(state="registered", detail=""),
            apply_autostart=lambda _on: None,
            apply_appearance=lambda: None,
            cloud_backup=cloud_backup,
        )

    def _save(self):
        self.calls["save"] += 1


def _row_controls(page) -> dict:
    out: dict = {}

    def walk(widget):
        classes = widget.get_css_classes() if isinstance(widget, Gtk.Widget) else []
        if isinstance(widget, Gtk.Box) and "settings-row" in classes:
            texts = widget.get_first_child()
            title_label = texts.get_first_child() if texts is not None else None
            control = texts.get_next_sibling() if texts is not None else None
            if title_label is not None and control is not None:
                out.setdefault(title_label.get_label(), control)
            return
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(page)
    return out


def _wait_for(condition, attempts: int = 60):
    for _ in range(attempts):
        pump(30)
        if condition():
            return True
    return condition()


@pytest.fixture()
def page(tmp_path):
    i18n.set_language("en-US")
    data_dir = tmp_path / "data"
    root = tmp_path / "root"
    data_dir.mkdir()
    root.mkdir()
    settings = SettingsService(config_root=tmp_path / "config")
    settings.load()
    data_backup = DataBackupService(settings_service=settings, data_dir=data_dir, root=root)
    service = CloudBackupService(settings, data_backup=data_backup, credentials=FakeCredentials(), log=lambda _m: None)
    transport = FakeTransport()
    service.create_transport = lambda options: transport

    # One remote snapshot to list + restore.
    from panebox.models.widget_config import WidgetConfig

    settings.layout.widgets.append(WidgetConfig(id="wid1", name="Tasks", widgetKind="Todo"))
    TodoWidgetStore("wid1", widgets_root=data_dir / "widgets").save(TodoWidgetData(items=[TodoItem(id="a", text="a")]))
    archive = root / "snap.zip"
    data_backup.export_scoped_backup(archive, ALL, app_version="0.3.0", source_device_id="deviceAAA")
    options = get_options(settings)
    name = build_remote_snapshot_name("deviceAAA", datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc))
    transport.files[f"{options.remote_path}/{name}"] = archive.read_bytes()

    hooks = FakeHooks(service)
    window = SettingsWindow(None, settings, hooks)
    window.show_section("CloudBackup")
    yield SimpleNamespace(
        window=window,
        settings=settings,
        hooks=hooks,
        service=service,
        data_backup=data_backup,
        transport=transport,
        archive=archive,
        controls=_row_controls(window._stack.get_child_by_name("CloudBackup")),
        snapshot_name=name,
    )
    data_backup.cancel_pending_restore()
    window.destroy()
    i18n.set_language(None)


def test_cloud_backup_page_in_nav_and_builds(page):
    assert "CloudBackup" in page.window._nav_rows
    assert page.window._stack.get_visible_child_name() == "CloudBackup"
    # provider + connection + data-type + schedule rows all rendered
    for title in ("Provider", "Username", "Todo data", "Widget style", "Backup every", "Keep last"):
        assert title in page.controls, title


def test_provider_dropdown_routes_to_slice(page):
    drop = page.controls["Provider"]
    drop.set_selected(1)  # WebDAV
    pump()
    slice_ = page.settings.settings.cloudBackup
    assert slice_.cloudBackupProvider == "webdav"
    assert slice_.cloudBackupEnabled is True
    assert page.hooks.calls["save"] >= 1


def test_data_type_and_schedule_controls_persist(page):
    page.controls["Todo data"].set_active(False)
    page.controls["Quick capture data"].set_active(False)
    pump()
    interval = page.controls["Backup every"]
    interval.set_selected(2)  # 12 hours
    retention = page.controls["Keep last"]
    retention.set_selected(1)  # 5 snapshots
    pump()
    slice_ = page.settings.settings.cloudBackup
    assert slice_.cloudBackupTodoEnabled is False
    assert slice_.cloudBackupQuickCaptureEnabled is False
    assert slice_.cloudBackupIntervalHours == 12
    assert slice_.cloudBackupRetainCount == 5


def test_snapshot_list_renders_from_transport(page):
    rendered = _wait_for(lambda: page.window._cloud_snapshots.get_first_child() is not None)
    assert rendered, "snapshot list never rendered"
    labels = [w.get_label() for w in _walk_type(page.window._cloud_snapshots, Gtk.Label)]
    assert any("deviceAA" in text for text in labels)  # 8-char device suffix


def _walk_type(widget, kind):
    if isinstance(widget, kind):
        yield widget
    child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
    while child is not None:
        yield from _walk_type(child, kind)
        child = child.get_next_sibling()


def test_restore_dialog_stages_pending_restore(page):
    destination = page.archive
    manifest = DataBackupService.peek_manifest(destination)
    assert manifest["kind"] == "cloud-backup"

    before = {id(w) for w in Gtk.Window.list_toplevels()}
    snapshot = SimpleNamespace(
        name=page.snapshot_name,
        size=destination.stat().st_size,
        created_utc=datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc),
        device_id="deviceAA",
    )
    page.window._cloud_show_restore_dialog(snapshot, destination, manifest)
    new_dialogs = [w for w in Gtk.Window.list_toplevels() if id(w) not in before]
    assert new_dialogs, "restore dialog not presented"
    dialog = new_dialogs[0]

    dialog.response(Gtk.ResponseType.OK)
    pump()

    assert page.data_backup.has_pending_restore()
    marker = page.data_backup.load_pending_marker()
    assert marker["domains"] == ["todo-data", "quick-capture-data", "widget-style"]
    assert marker["replaceItemData"] is False  # merge is the default
