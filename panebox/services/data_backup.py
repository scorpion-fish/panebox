"""Scoped data backup + staged restore (port of the scoped paths of
Services/PaneBoxDataBackupService.cs — the cloud-backup feature).

Export: live data-dir files inside the requested domains + a widget-style
projection document + an integrity manifest, zipped atomically.

Restore: two phases. `prepare_scoped_restore` validates the archive, stages
its contents under restore-staging/<guid> and writes a pending-restore marker
(apply happens at next launch, after the user confirms). `apply_pending_restore`
merges (default) or replaces the live stores, applies the widget-style
projection, and only then deletes the marker. A crash mid-apply is retried up
to three times before the restore is abandoned.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import backup_domains as domains
from . import widget_style_projection
from ..constants import DATA_DIR, DATA_ROOT
from ..models.quick_capture import QuickCaptureStoreData
from ..models.todo import TodoWidgetData

BACKUP_KIND = "cloud-backup"
MANIFEST_SCHEMA_VERSION = 2
MAX_SCOPED_RESTORE_APPLY_ATTEMPTS = 3
MAX_SAFETY_BACKUPS = 5


class ScopedBackupError(Exception):
    """Export cannot proceed (nothing in scope)."""


class RestoreValidationError(Exception):
    """The archive is not a usable scoped cloud backup."""


class PendingRestoreError(Exception):
    """A restore is already staged."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


@dataclass
class ScopedExportResult:
    archive_path: Path
    shipped_domains: int
    file_count: int
    total_bytes: int
    created_at_utc: str
    todo_stores: int = 0
    quick_capture: bool = False
    widget_style: bool = False


@dataclass
class TodoRestorePlan:
    remaps: list[tuple[str, str]] = field(default_factory=list)
    merges: list[tuple[str, str]] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    junk: list[str] = field(default_factory=list)


@dataclass
class ScopedRestorePreparation:
    marker: dict
    manifest_domains: int
    applied_domains: int
    backup_created_at_utc: str
    app_version: str
    staged_files: int
    todo_stores: int
    quick_capture: bool
    widget_style: bool
    remapped: int
    merged: int
    unmapped: int
    dropped_source_orphans: int
    attachment_refs: int


@dataclass
class RestoreApplyResult:
    success: bool
    applied_domains: int
    todo_stores: int = 0
    merged_items: int = 0
    quick_capture: bool = False
    widget_style: bool = False
    safety_backup_path: Optional[Path] = None
    error: str = ""
    abandoned: bool = False


class DataBackupService:
    def __init__(self, settings_service=None, data_dir: Path = DATA_DIR, root: Path = DATA_ROOT, log=None):
        self.settings_service = settings_service
        self.data_dir = Path(data_dir)
        self.root = Path(root)
        self._log = log or (lambda message: None)

    # ---- paths ---------------------------------------------------------------

    @property
    def restore_staging_dir(self) -> Path:
        return self.root / "restore-staging"

    @property
    def backup_staging_dir(self) -> Path:
        return self.root / "backup-staging"

    @property
    def pending_restore_marker_path(self) -> Path:
        return self.root / "restore-pending.json"

    @property
    def safety_backup_dir(self) -> Path:
        return self.root / "safety-backups"

    # ---- export ----------------------------------------------------------------

    def export_scoped_backup(
        self,
        archive_path: Path,
        scope: int,
        app_version: str = "",
        source_device_id: str = "",
    ) -> ScopedExportResult:
        """Build a scoped cloud-backup ZIP atomically (tmp file + rename)."""
        live_todo_ids = self._live_todo_widget_ids()
        shipped = scope
        entries: list[dict] = []
        archive_path = Path(archive_path)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = archive_path.with_name(archive_path.name + f".tmp-{uuid.uuid4().hex[:8]}")
        style_document: Optional[dict] = None
        todo_stores = 0
        quick_capture = False
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel in sorted(self._iter_scope_files(scope, live_todo_ids)):
                    source = self.data_dir / rel
                    digest, size = self._write_zip_entry(zf, f"data/{rel}", source)
                    entries.append({"path": f"data/{rel}", "length": size, "sha256": digest})
                    if domains.is_todo_data_path(rel):
                        todo_stores += 1
                    elif domains.is_quick_capture_data_path(rel):
                        quick_capture = True

                if scope & domains.WIDGET_STYLE:
                    style_document = self._build_style_document(source_device_id)
                if style_document is None:
                    # No document → the manifest must not claim the domain.
                    shipped &= ~domains.WIDGET_STYLE
                else:
                    payload = json.dumps(style_document, ensure_ascii=False, indent=2).encode("utf-8")
                    zf.writestr(domains.WIDGET_STYLE_ENTRY_NAME, payload)
                    entries.append(
                        {
                            "path": domains.WIDGET_STYLE_ENTRY_NAME,
                            "length": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    )

                if not entries:
                    raise ScopedBackupError("None of the selected data types has anything to back up yet.")

                manifest = {
                    "schemaVersion": MANIFEST_SCHEMA_VERSION,
                    "kind": BACKUP_KIND,
                    "createdAtUtc": _now_iso(),
                    "appVersion": app_version,
                    "sourceDataPath": str(self.data_dir),
                    "sourceDeviceId": source_device_id,
                    "files": entries,
                    "domains": domains.to_manifest_names(shipped),
                    "widgetStyleFile": domains.WIDGET_STYLE_ENTRY_NAME if style_document else None,
                }
                # manifest.json is written LAST: its presence implies a complete archive.
                zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            os.replace(tmp, archive_path)
        finally:
            tmp.unlink(missing_ok=True)
        return ScopedExportResult(
            archive_path=archive_path,
            shipped_domains=shipped,
            file_count=len(entries),
            total_bytes=sum(entry["length"] for entry in entries),
            created_at_utc=manifest["createdAtUtc"],
            todo_stores=todo_stores,
            quick_capture=quick_capture,
            widget_style=bool(style_document),
        )

    def _iter_scope_files(self, scope: int, live_todo_ids: set[str]) -> list[str]:
        files: list[str] = []
        if scope & domains.TODO_DATA:
            widgets_dir = self.data_dir / "widgets"
            if widgets_dir.is_dir():
                for widget_dir in sorted(p for p in widgets_dir.iterdir() if p.is_dir()):
                    rel = f"widgets/{widget_dir.name}/todo.json"
                    # Orphan stores of deleted widgets are skipped; an empty
                    # live set (fresh data dir read from files) keeps everything.
                    if live_todo_ids and widget_dir.name not in live_todo_ids:
                        continue
                    if (widget_dir / "todo.json").is_file():
                        files.append(rel)
        if scope & domains.QUICK_CAPTURE_DATA:
            qc = self.data_dir / "quick-capture" / "quick-capture.json"
            if qc.is_file():
                files.append("quick-capture/quick-capture.json")
        return files

    def _build_style_document(self, source_device_id: str) -> Optional[dict]:
        if self.settings_service is None:
            return None
        return widget_style_projection.serialize(self.settings_service, source_device_id) or None

    @staticmethod
    def _write_zip_entry(zf: zipfile.ZipFile, entry_name: str, source: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with open(source, "rb") as handle:
            while chunk := handle.read(256 * 1024):
                digest.update(chunk)
                size += len(chunk)
            handle.seek(0)
            zf.write(source, entry_name)
        return digest.hexdigest(), size

    # ---- live widget ids -----------------------------------------------------------

    @staticmethod
    def peek_manifest(archive_path: Path) -> dict:
        """Read-only manifest peek for the restore-confirm dialog (domains,
        creation time, version). Raises RestoreValidationError when unusable."""
        archive_path = Path(archive_path)
        if not archive_path.is_file():
            raise RestoreValidationError("The snapshot file does not exist.")
        try:
            with zipfile.ZipFile(archive_path) as zf:
                if "manifest.json" not in zf.namelist():
                    raise RestoreValidationError("The snapshot has no manifest.")
                manifest = json.loads(zf.read("manifest.json"))
        except (OSError, zipfile.BadZipFile) as exc:
            raise RestoreValidationError(f"The snapshot is not a readable ZIP archive: {exc}") from exc
        except ValueError as exc:
            raise RestoreValidationError(f"The snapshot manifest is unreadable: {exc}") from exc
        if not isinstance(manifest, dict):
            raise RestoreValidationError("The snapshot manifest is malformed.")
        return manifest

    def _live_todo_widget_ids(self) -> set[str]:
        if self.settings_service is not None:
            return {w.id for w in self.settings_service.layout.widgets if (w.widgetKind or "").lower() == "todo"}
        # No settings service (tests / headless restore): read the layout file
        # first, then settings.json — a wiped device has neither, and the empty
        # set keeps every staged store a candidate.
        ids: set[str] = set()
        layout_path = self._config_dir() / "widget-layout.json"
        settings_path = self._config_dir() / "settings.json"
        source = layout_path if layout_path.exists() else settings_path
        try:
            if source.exists():
                document = json.loads(source.read_text(encoding="utf-8"))
                widgets = (
                    document.get("layout", {}).get("widgets") if source == layout_path else document.get("widgets")
                )
                for element in widgets or ():
                    if not isinstance(element, dict):
                        continue
                    if str(element.get("widgetKind", "")).lower() == "todo" and element.get("id"):
                        ids.add(str(element["id"]))
        except (OSError, ValueError) as exc:
            self._log(f"[DataBackup] could not enumerate live todo widgets: {exc}")
        return ids

    def _config_dir(self) -> Path:
        from ..constants import CONFIG_ROOT

        return CONFIG_ROOT

    # ---- prepare restore ------------------------------------------------------------

    def has_pending_restore(self) -> bool:
        return self.pending_restore_marker_path.exists()

    def load_pending_marker(self) -> Optional[dict]:
        if not self.pending_restore_marker_path.exists():
            return None
        try:
            marker = json.loads(self.pending_restore_marker_path.read_text(encoding="utf-8"))
            return marker if isinstance(marker, dict) else None
        except (OSError, ValueError) as exc:
            self._log(f"[DataBackup] pending-restore marker unreadable: {exc}")
            return None

    def _write_marker(self, marker: dict) -> None:
        path = self.pending_restore_marker_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _delete_marker(self) -> None:
        self.pending_restore_marker_path.unlink(missing_ok=True)
        self.pending_restore_marker_path.with_name(self.pending_restore_marker_path.name + ".bak").unlink(
            missing_ok=True
        )

    def cancel_pending_restore(self) -> bool:
        """Abandon a staged restore: marker + staging go, live data untouched."""
        marker = self.load_pending_marker()
        if marker is None:
            self._delete_marker()
            return False
        staging = marker.get("stagingRoot")
        if staging:
            shutil.rmtree(Path(staging), ignore_errors=True)
        self._delete_marker()
        return True

    def set_pending_restore_item_replace_mode(self, replace: bool) -> bool:
        marker = self.load_pending_marker()
        if marker is None:
            return False
        marker["replaceItemData"] = bool(replace)
        self._write_marker(marker)
        return True

    def prepare_scoped_restore(
        self,
        archive_path: Path,
        requested_domains: int,
        allow_missing_settings: bool = False,
    ) -> ScopedRestorePreparation:
        archive_path = Path(archive_path)
        if not archive_path.is_file():
            raise RestoreValidationError("The snapshot file does not exist.")

        existing = self.load_pending_marker()
        if existing is not None:
            staging = existing.get("stagingRoot")
            if staging and Path(staging).exists():
                raise PendingRestoreError("A restore is already staged for the next launch.")
            self._log("[DataBackup] dropping stale pending-restore marker.")
            self.cancel_pending_restore()

        staging_root = self.restore_staging_dir / uuid.uuid4().hex
        staged_data = staging_root / "data"
        staged_data.mkdir(parents=True, exist_ok=True)

        try:
            info = self._extract_and_validate_manifest(archive_path, staging_root, staged_data, requested_domains)
        except Exception:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise

        plan = self._plan_orphaned_todo_widget_remaps(staging_root, staged_data)
        self._apply_todo_restore_plan(staged_data, plan)
        remaps = dict(plan.remaps + plan.merges)
        attachment_refs = self._rebase_managed_attachment_paths(staging_root, info["source_data_path"], remaps)
        self._validate_staged_stores(staging_root, info["applied_domains"])

        marker = {
            "schemaVersion": 1,
            "stagingRoot": str(staging_root),
            "archivePath": str(archive_path),
            "preparedAtUtc": _now_iso(),
            "backupCreatedAtUtc": info["created_at_utc"],
            "appVersion": info["app_version"],
            "domains": domains.to_manifest_names(info["applied_domains"]),
            "allowMissingSettings": bool(allow_missing_settings),
            "safetyBackupPath": None,
            # Absent/false = additive merge; true = replace in-domain items.
            "replaceItemData": False,
            "applyAttemptCount": 0,
        }
        self._write_marker(marker)
        return ScopedRestorePreparation(
            marker=marker,
            manifest_domains=info["manifest_domains"],
            applied_domains=info["applied_domains"],
            backup_created_at_utc=info["created_at_utc"],
            app_version=info["app_version"],
            staged_files=info["staged_files"],
            todo_stores=info["todo_stores"],
            quick_capture=info["quick_capture"],
            widget_style=info["widget_style"],
            remapped=len(plan.remaps),
            merged=len(plan.merges),
            unmapped=len(plan.unmapped),
            dropped_source_orphans=len(plan.junk),
            attachment_refs=attachment_refs,
        )

    def _extract_and_validate_manifest(
        self, archive_path: Path, staging_root: Path, staged_data: Path, requested_domains: int
    ) -> dict:
        try:
            zf = zipfile.ZipFile(archive_path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise RestoreValidationError(f"The snapshot is not a readable ZIP archive: {exc}") from exc
        with zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                raise RestoreValidationError("The snapshot has no manifest.")
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except (OSError, ValueError) as exc:
                raise RestoreValidationError(f"The snapshot manifest is unreadable: {exc}") from exc
            if not isinstance(manifest, dict):
                raise RestoreValidationError("The snapshot manifest is malformed.")

            schema = manifest.get("schemaVersion")
            if not isinstance(schema, int) or not 1 <= schema <= MANIFEST_SCHEMA_VERSION:
                raise RestoreValidationError(f"Unsupported backup format version: {schema!r}")
            kind = manifest.get("kind")
            if kind is not None and kind != BACKUP_KIND:
                raise RestoreValidationError("This file is not a PaneBox cloud backup.")

            for name in names:
                if name.endswith("/"):
                    continue
                if "\\" in name or name.startswith("/"):
                    raise RestoreValidationError(f"Unsafe entry in archive: {name}")
                if ".." in name.split("/"):
                    raise RestoreValidationError(f"Unsafe entry in archive: {name}")
                if name not in (
                    "manifest.json",
                    domains.WIDGET_STYLE_ENTRY_NAME,
                ) and not name.startswith("data/"):
                    raise RestoreValidationError(f"Unexpected entry in archive: {name}")

            try:
                manifest_domains = domains.try_from_manifest_names(manifest.get("domains") or [])
            except ValueError as exc:
                raise RestoreValidationError(str(exc)) from exc
            if manifest_domains == 0:
                raise RestoreValidationError("This backup is not a scoped cloud backup.")
            applied = manifest_domains & requested_domains
            if applied == 0:
                raise RestoreValidationError("The selected data types contain nothing from this backup.")

            files = manifest.get("files") or []
            by_path = {
                entry.get("path"): entry
                for entry in files
                if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            }

            staged_files = 0
            todo_stores = 0
            quick_capture = False
            widget_style = False
            for name in names:
                if name == "manifest.json" or name.endswith("/"):
                    continue
                if name == domains.WIDGET_STYLE_ENTRY_NAME:
                    if not manifest_domains & domains.WIDGET_STYLE:
                        raise RestoreValidationError(
                            "The archive contains data outside its declared scope: widget-style.json"
                        )
                    # Staged even when the user didn't pick the style domain —
                    # the remap planner reads it as the source's liveness list.
                    self._extract_verified(zf, name, staging_root / name, by_path.get(name))
                    staged_files += 1
                    if applied & domains.WIDGET_STYLE:
                        widget_style = True
                    continue
                rel = name[len("data/") :]
                if not domains.is_in_scope(manifest_domains, rel):
                    raise RestoreValidationError(f"The archive contains data outside its declared scope: {rel}")
                if not (applied & self._domain_of(rel)):
                    continue
                self._extract_verified(zf, name, staged_data / rel, by_path.get(name))
                staged_files += 1
                if domains.is_todo_data_path(rel):
                    todo_stores += 1
                elif domains.is_quick_capture_data_path(rel):
                    quick_capture = True

            if not staged_files:
                raise RestoreValidationError("The backup contains no files for the selected data types.")

        return {
            "manifest_domains": manifest_domains,
            "applied_domains": applied,
            "created_at_utc": str(manifest.get("createdAtUtc") or ""),
            "app_version": str(manifest.get("appVersion") or ""),
            "source_data_path": manifest.get("sourceDataPath"),
            "staged_files": staged_files,
            "todo_stores": todo_stores,
            "quick_capture": quick_capture,
            "widget_style": widget_style,
        }

    @staticmethod
    def _extract_verified(zf: zipfile.ZipFile, name: str, destination: Path, manifest_entry: Optional[dict]) -> None:
        if manifest_entry is None:
            raise RestoreValidationError(f"A file is missing from the manifest: {name}")
        payload = zf.read(name)
        expected_length = manifest_entry.get("length")
        if isinstance(expected_length, int) and expected_length != len(payload):
            raise RestoreValidationError(f"Size mismatch for {name} — the backup is corrupted.")
        expected_sha = manifest_entry.get("sha256")
        if isinstance(expected_sha, str) and expected_sha:
            if hashlib.sha256(payload).hexdigest() != expected_sha:
                raise RestoreValidationError(f"Checksum mismatch for {name} — the backup is corrupted.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    @staticmethod
    def _domain_of(rel: str) -> int:
        if domains.is_todo_data_path(rel):
            return domains.TODO_DATA
        if domains.is_quick_capture_data_path(rel):
            return domains.QUICK_CAPTURE_DATA
        return 0

    # ---- orphan remap planning ------------------------------------------------------

    def _plan_orphaned_todo_widget_remaps(self, staging_root: Path, staged_data: Path) -> TodoRestorePlan:
        staged_widgets = staged_data / "widgets"
        if not staged_widgets.is_dir():
            return TodoRestorePlan()

        staged_ids = [
            widget_dir.name
            for widget_dir in sorted(staged_widgets.iterdir())
            if widget_dir.is_dir() and (widget_dir / "todo.json").is_file()
        ]
        if not staged_ids:
            return TodoRestorePlan()

        # The widget-style document names the SOURCE device's live widgets —
        # staged dirs it does not list are the source's own orphan debris.
        authoritative = set(staged_ids)
        source_live = self._read_source_live_todo_widget_ids(staging_root)
        if source_live is not None:
            named = [i for i in staged_ids if i in source_live]
            if named:
                authoritative &= set(named)
        junk = [i for i in staged_ids if i not in authoritative]

        live_ids = self._live_todo_widget_ids()
        orphans = sorted(i for i in staged_ids if i in authoritative and i not in live_ids)
        free_targets = sorted(i for i in live_ids if i not in authoritative)

        plan = TodoRestorePlan()
        pairs = min(len(orphans), len(free_targets))
        plan.remaps = [(orphans[i], free_targets[i]) for i in range(pairs)]
        if len(orphans) > pairs:
            merge_target = (
                plan.remaps[0][1] if plan.remaps else next((i for i in sorted(live_ids) if i in authoritative), None)
            )
            for leftover in orphans[pairs:]:
                if merge_target is None:
                    plan.unmapped.append(leftover)
                else:
                    plan.merges.append((leftover, merge_target))
        plan.junk = junk
        return plan

    @staticmethod
    def _read_source_live_todo_widget_ids(staging_root: Path) -> Optional[set[str]]:
        document_path = staging_root / domains.WIDGET_STYLE_ENTRY_NAME
        if not document_path.is_file():
            return None
        try:
            document = json.loads(document_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("kind") != "widget-style":
                return None
            widgets = document.get("widgets")
            if not isinstance(widgets, dict):
                return None
            return {
                str(widget_id)
                for widget_id, style in widgets.items()
                if isinstance(style, dict) and str(style.get("widgetKind", "")).lower() == "todo"
            }
        except (OSError, ValueError):
            return None

    def _apply_todo_restore_plan(self, staged_data: Path, plan: TodoRestorePlan) -> None:
        staged_widgets = staged_data / "widgets"
        for junk_id in plan.junk:
            shutil.rmtree(staged_widgets / junk_id, ignore_errors=True)
            self._log(f"[DataBackup] scoped restore dropped source-orphan todo store '{junk_id}'.")
        for source_id, target_id in plan.remaps:
            source_dir = staged_widgets / source_id
            target_dir = staged_widgets / target_id
            if source_dir.is_dir():
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(target_dir, ignore_errors=True)
                shutil.move(str(source_dir), str(target_dir))
                self._log(f"[DataBackup] scoped restore remapped todo store '{source_id}' -> '{target_id}'.")
        by_target: dict[str, list[str]] = {}
        for source_id, target_id in plan.merges:
            by_target.setdefault(target_id, []).append(source_id)
        for target_id, source_ids in by_target.items():
            target_data = self._load_todo_doc(staged_widgets / target_id / "todo.json") or TodoWidgetData()
            seen = {item.id for item in target_data.items}
            next_sort = max((item.sortOrder for item in target_data.items), default=-1) + 1
            for source_id in source_ids:
                orphan = self._load_todo_doc(staged_widgets / source_id / "todo.json")
                if orphan is None:
                    continue
                appended = 0
                for item in orphan.items:
                    if not (item.text or "").strip() or item.id in seen:
                        continue
                    seen.add(item.id)
                    item.sortOrder = next_sort
                    next_sort += 1
                    target_data.items.append(item)
                    appended += 1
                shutil.rmtree(staged_widgets / source_id, ignore_errors=True)
                self._log(
                    f"[DataBackup] scoped restore merged {appended} todo item(s) from '{source_id}' into '{target_id}'."
                )
            self._write_json_doc(staged_widgets / target_id / "todo.json", target_data.to_dict())

    @staticmethod
    def _load_todo_doc(path: Path) -> Optional[TodoWidgetData]:
        try:
            return TodoWidgetData.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json_doc(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- attachment path rebase -----------------------------------------------------

    def _rebase_managed_attachment_paths(
        self, staging_root: Path, source_data_path: Optional[str], remaps: dict[str, str]
    ) -> int:
        """Rewrite managed-copy attachment/image paths embedded in staged stores
        to point into THIS device's data dir. Attachments are outside every
        backup domain, so in practice staged payload files do not exist and the
        existence check keeps the original path — the faithful C# behavior."""
        staged_data = staging_root / "data"
        rebased = 0

        qc_path = staged_data / "quick-capture" / "quick-capture.json"
        for path in (qc_path, qc_path.with_name(qc_path.name + ".bak")):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            changed = False
            for item in list(data.get("items") or []) + list(data.get("recentItems") or []):
                if not isinstance(item, dict):
                    continue
                for attachment in item.get("attachments") or []:
                    if not isinstance(attachment, dict) or attachment.get("storageMode") != "managed":
                        continue
                    new_path = self._try_rebase_managed_path(
                        attachment.get("filePath"),
                        source_data_path,
                        staged_data,
                        ["quick-capture"],
                        {},
                    )
                    if new_path:
                        attachment["filePath"] = new_path
                        rebased += 1
                        changed = True
                image_path = item.get("imagePath")
                if isinstance(image_path, str) and image_path:
                    new_path = self._try_rebase_managed_path(
                        image_path, source_data_path, staged_data, ["quick-capture"], {}
                    )
                    if new_path:
                        item["imagePath"] = new_path
                        rebased += 1
                        changed = True
            if changed:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        staged_widgets = staged_data / "widgets"
        if staged_widgets.is_dir():
            for todo_path in staged_widgets.glob("*/todo.json"):
                for path in (todo_path, todo_path.with_name(todo_path.name + ".bak")):
                    if not path.is_file():
                        continue
                    rebased += self._rebase_todo_file(path, staged_data, source_data_path, remaps)
        return rebased

    def _rebase_todo_file(self, path: Path, staged_data: Path, source_data_path: Optional[str], remaps: dict) -> int:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        current_widget_id = path.parent.name
        fallbacks = [f"widgets/{current_widget_id}"]
        for source_id, target_id in remaps.items():
            if target_id == current_widget_id and f"widgets/{source_id}" not in fallbacks:
                fallbacks.append(f"widgets/{source_id}")
        changed = 0
        for item in data.get("items") or []:
            for attachment in (item or {}).get("attachments") or []:
                if not isinstance(attachment, dict) or attachment.get("storageMode") != "managed":
                    continue
                new_path = self._try_rebase_managed_path(
                    attachment.get("filePath"), source_data_path, staged_data, fallbacks, remaps
                )
                if new_path:
                    attachment["filePath"] = new_path
                    changed += 1
        if changed:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed

    def _try_rebase_managed_path(
        self,
        original_path: Optional[str],
        source_data_path: Optional[str],
        staged_data: Path,
        fallback_store_relative: list[str],
        remaps: dict[str, str],
    ) -> Optional[str]:
        if not original_path or not original_path.strip():
            return None
        normalized = original_path.replace("\\", "/")
        relative: Optional[str] = None
        if source_data_path:
            prefix = str(source_data_path).replace("\\", "/").rstrip("/") + "/"
            if normalized.lower().startswith(prefix.lower()):
                relative = normalized[len(prefix) :]
        for fallback in fallback_store_relative:
            if relative is not None:
                break
            marker = f"/{fallback.strip('/')}/"
            index = normalized.lower().find(marker.lower())
            if index >= 0:
                relative = normalized[index + 1 :]
        if relative is None:
            return None
        if remaps and relative.startswith("widgets/"):
            segments = relative.split("/", 2)
            if len(segments) == 3 and segments[1] in remaps:
                relative = f"widgets/{remaps[segments[1]]}/{segments[2]}"
        staged_file = (staged_data / relative).resolve()
        try:
            staged_file.relative_to(staged_data.resolve())
        except ValueError:
            return None
        if not staged_file.is_file():
            return None
        return str((self.data_dir / relative).resolve())

    # ---- staged store validation ---------------------------------------------------

    def _validate_staged_stores(self, staging_root: Path, applied_domains: int) -> None:
        staged_data = staging_root / "data"
        if applied_domains & domains.TODO_DATA and staged_data.is_dir():
            for todo_path in (staged_data / "widgets").glob("*/todo.json"):
                if self._load_todo_doc(todo_path) is None:
                    raise RestoreValidationError(f"The staged todo store is invalid: {todo_path.name}")
        if applied_domains & domains.QUICK_CAPTURE_DATA:
            qc = staged_data / "quick-capture" / "quick-capture.json"
            if qc.is_file():
                try:
                    QuickCaptureStoreData.from_dict(json.loads(qc.read_text(encoding="utf-8")))
                except (OSError, ValueError) as exc:
                    raise RestoreValidationError("The staged Quick Capture store is invalid.") from exc
        if applied_domains & domains.WIDGET_STYLE:
            doc_path = staging_root / domains.WIDGET_STYLE_ENTRY_NAME
            if doc_path.is_file():
                try:
                    document = json.loads(doc_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RestoreValidationError("The widget-style document is invalid.") from exc
                if not isinstance(document, dict) or document.get("kind") != "widget-style":
                    raise RestoreValidationError("The widget-style document is invalid.")

    # ---- apply ----------------------------------------------------------------------

    def apply_pending_restore(self) -> Optional[RestoreApplyResult]:
        marker = self.load_pending_marker()
        if marker is None:
            self._delete_marker()
            return None
        staging = Path(marker.get("stagingRoot") or "")
        if not staging.is_dir():
            self._log("[DataBackup] pending-restore staging is gone — dropping marker.")
            self._delete_marker()
            return None
        try:
            applied_domains = domains.try_from_manifest_names(marker.get("domains") or [])
        except ValueError:
            applied_domains = 0
        replace = bool(marker.get("replaceItemData", False))

        try:
            marker = self._ensure_pre_restore_safety_backup(marker, applied_domains)
            counts = self._apply_scoped_restore_core(staging, applied_domains, replace)
        except Exception as exc:
            attempts = int(marker.get("applyAttemptCount") or 0) + 1
            self._log(f"[DataBackup] scoped restore apply failed (attempt {attempts}): {exc}")
            if attempts >= MAX_SCOPED_RESTORE_APPLY_ATTEMPTS:
                self._abandon_pending_restore(staging)
                return RestoreApplyResult(
                    success=False,
                    applied_domains=applied_domains,
                    error=str(exc),
                    abandoned=True,
                )
            marker["applyAttemptCount"] = attempts
            self._write_marker(marker)
            return RestoreApplyResult(success=False, applied_domains=applied_domains, error=str(exc))

        self._delete_marker()
        shutil.rmtree(staging, ignore_errors=True)
        if counts["widget_style"] and self.settings_service is not None:
            try:
                self.settings_service.save_now()
            except Exception as exc:
                self._log(f"[DataBackup] settings save after restore: {exc}")
        return RestoreApplyResult(
            success=True,
            applied_domains=applied_domains,
            todo_stores=counts["todo_stores"],
            merged_items=counts["merged_items"],
            quick_capture=counts["quick_capture"],
            widget_style=counts["widget_style"],
            safety_backup_path=Path(marker["safetyBackupPath"]) if marker.get("safetyBackupPath") else None,
        )

    def _abandon_pending_restore(self, staging: Path) -> None:
        """Give up on a restore that keeps failing: drop marker + staging."""
        self._log("[DataBackup] scoped restore abandoned after repeated apply failures.")
        shutil.rmtree(staging, ignore_errors=True)
        self._delete_marker()

    def _ensure_pre_restore_safety_backup(self, marker: dict, applied_domains: int) -> dict:
        if marker.get("safetyBackupPath"):
            return marker
        if not applied_domains:
            return marker
        try:
            self.safety_backup_dir.mkdir(parents=True, exist_ok=True)
            destination = (
                self.safety_backup_dir
                / f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.zip"
            )
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel in sorted(self._iter_scope_files(applied_domains, self._live_todo_widget_ids())):
                    source = self.data_dir / rel
                    if source.is_file():
                        zf.write(source, f"data/{rel}")
                if applied_domains & domains.WIDGET_STYLE and self.settings_service is not None:
                    document = self._build_style_document("")
                    if document:
                        zf.writestr(
                            domains.WIDGET_STYLE_ENTRY_NAME,
                            json.dumps(document, ensure_ascii=False, indent=2),
                        )
            marker = {**marker, "safetyBackupPath": str(destination)}
            self._write_marker(marker)
            self._prune_safety_backups()
        except Exception as exc:
            self._log(f"[DataBackup] pre-restore safety backup failed (continuing): {exc}")
        return marker

    def _prune_safety_backups(self) -> None:
        backups = sorted(
            (p for p in self.safety_backup_dir.glob("pre-restore-*.zip") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[MAX_SAFETY_BACKUPS:]:
            stale.unlink(missing_ok=True)

    def _apply_scoped_restore_core(self, staging: Path, applied_domains: int, replace: bool) -> dict:
        staged_data = staging / "data"
        todo_stores = 0
        merged_items = 0
        quick_capture = False

        if staged_data.is_dir():
            staged_files = sorted(p for p in staged_data.rglob("*") if p.is_file() and p.name != "todo.json.bak")
            for staged_file in staged_files:
                rel = staged_file.relative_to(staged_data).as_posix()
                domain = self._domain_of(rel)
                if not domain or not (domain & applied_domains):
                    continue
                target = self.data_dir / rel
                if domain == domains.TODO_DATA:
                    if replace:
                        self._replace_store_file(staged_file, target)
                        todo_stores += 1
                    else:
                        merged_items += self._merge_staged_todo_store(staged_file, target)
                        todo_stores += 1
                elif domain == domains.QUICK_CAPTURE_DATA:
                    if replace:
                        self._replace_store_file(staged_file, target)
                    else:
                        self._merge_staged_quick_capture_store(staged_file, target)
                    quick_capture = True

        widget_style = False
        style_path = staging / domains.WIDGET_STYLE_ENTRY_NAME
        if (applied_domains & domains.WIDGET_STYLE) and style_path.is_file():
            if self.settings_service is None:
                if not self._marker_allows_missing_settings():
                    raise RestoreValidationError("Settings are not loaded — the widget-style domain cannot be applied.")
            else:
                document = json.loads(style_path.read_text(encoding="utf-8"))
                widget_style_projection.apply(document, self.settings_service)
                widget_style = True
        return {
            "todo_stores": todo_stores,
            "merged_items": merged_items,
            "quick_capture": quick_capture,
            "widget_style": widget_style,
        }

    def _marker_allows_missing_settings(self) -> bool:
        marker = self.load_pending_marker() or {}
        return bool(marker.get("allowMissingSettings"))

    @staticmethod
    def _replace_store_file(staged_file: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        target.with_name(target.name + ".bak").unlink(missing_ok=True)
        shutil.copyfile(staged_file, target)

    def _merge_staged_todo_store(self, staged_file: Path, target: Path) -> int:
        """Additive merge by item id; a remote item never deletes a live one."""
        remote = self._load_todo_doc(staged_file)
        if remote is None:
            return 0
        widget_id = domains.try_get_todo_widget_id(target.relative_to(self.data_dir).as_posix())
        if not widget_id:
            return 0
        from .todo_store import TodoWidgetStore

        store = TodoWidgetStore(widget_id, widgets_root=self.data_dir / "widgets")
        local = store.load()
        by_id = {item.id: item for item in local.items}
        merged = 0
        for item in remote.items:
            existing = by_id.get(item.id)
            if existing is None:
                local.items.append(item)
                merged += 1
            elif _should_remote_item_win(item, existing):
                local.items[local.items.index(existing)] = item
                merged += 1
        store.save(local)
        return merged

    def _merge_staged_quick_capture_store(self, staged_file: Path, target: Path) -> None:
        try:
            remote = QuickCaptureStoreData.from_dict(json.loads(staged_file.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise RestoreValidationError("The staged Quick Capture store is invalid.") from exc
        from .quick_capture_store import QuickCaptureStore

        store = QuickCaptureStore(data_dir=self.data_dir / "quick-capture")
        local = store.load()
        by_id = {item.id: item for item in local.items}
        for item in remote.items:
            existing = by_id.get(item.id)
            if existing is None:
                local.items.append(item)
            elif _should_remote_item_win(item, existing):
                local.items[local.items.index(existing)] = item
        local_recent_ids = {item.id for item in local.recentItems}
        for item in remote.recentItems:
            if item.id not in local_recent_ids:
                local.recentItems.append(item)
        local.version = max(local.version, remote.version)
        store.save(local)


def _should_remote_item_win(remote, local) -> bool:
    """Additive-merge precedence: remote wins only when strictly newer, and a
    remote tombstone never wins against a live local item."""
    if remote.isDeleted and not local.isDeleted:
        return False
    remote_updated = _parse_iso(remote.updatedAt)
    local_updated = _parse_iso(local.updatedAt)
    if remote_updated is None or local_updated is None:
        return False
    return remote_updated > local_updated
