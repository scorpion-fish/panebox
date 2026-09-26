"""Cloud backup orchestration (port of Services/CloudBackupService.cs).

Builds a scoped snapshot through DataBackupService, uploads it over a
WebDAV transport, verifies the upload by listing the remote folder, prunes old
snapshots, and keeps the credential in the platform keyring. Scheduled runs
gate on the interval plus a 10-minute spacing floor; a pending restore blocks
every upload.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from . import backup_domains as domains
from .data_backup import DataBackupService, PendingRestoreError, ScopedBackupError
from .webdav_transport import CloudBackupTransportException, WebDavBackupTransport
from ..platform.secrets import CredentialStore

PROVIDER_NONE = "none"
PROVIDER_WEBDAV = "webdav"

DEFAULT_REMOTE_PATH = "PaneBox/backups"
SUPPORTED_INTERVAL_HOURS = (1, 6, 12, 24, 168)
DEFAULT_INTERVAL_HOURS = 24
SUPPORTED_RETENTION_COUNTS = (3, 5, 7, 10, 14)
DEFAULT_RETENTION_COUNT = 5

SNAPSHOT_FILE_PREFIX = "PaneBox-CloudBackup-"
SNAPSHOT_SUFFIX = ".zip"
MIN_SCHEDULING_SPACING = timedelta(minutes=10)
VERIFY_RETRY_DELAYS_SECONDS = (0.8, 2.0, 4.0)

# RunBackupNow outcomes (CloudBackupRunResult.Outcome on Windows).
OUTCOME_NOT_CONFIGURED = "NotConfigured"
OUTCOME_NO_SCOPE = "NoScope"
OUTCOME_MISSING_CREDENTIAL = "MissingCredential"
OUTCOME_PENDING_RESTORE = "PendingRestore"
OUTCOME_IN_PROGRESS = "InProgress"
OUTCOME_UPLOADED = "Uploaded"


def normalize_interval_hours(hours: int) -> int:
    return hours if hours in SUPPORTED_INTERVAL_HOURS else DEFAULT_INTERVAL_HOURS


def normalize_retention_count(count: int) -> int:
    return count if count in SUPPORTED_RETENTION_COUNTS else DEFAULT_RETENTION_COUNT


def normalize_remote_path(remote_path: str) -> str:
    """Joined verbatim into request URIs — a ".." segment would escape the
    configured folder on the user's own server, so it falls back to default."""
    trimmed = (remote_path or "").strip().strip("/")
    if not trimmed:
        return DEFAULT_REMOTE_PATH
    segments = [seg for seg in trimmed.split("/") if seg]
    if any(segment in ("..", ".") for segment in segments):
        return DEFAULT_REMOTE_PATH
    return "/".join(segments)


@dataclass
class CloudBackupOptions:
    provider: str
    server_url: str
    remote_path: str
    username: str
    scope: int
    retention_count: int
    interval_hours: int
    last_success_utc: Optional[datetime]

    @property
    def has_endpoint(self) -> bool:
        if self.provider == PROVIDER_NONE or not self.server_url:
            return False
        parts = urlsplit(self.server_url)
        return parts.scheme in ("http", "https") and bool(parts.netloc)

    @property
    def is_configured(self) -> bool:
        return self.has_endpoint and self.scope != 0

    @property
    def uses_plain_http(self) -> bool:
        return urlsplit(self.server_url).scheme == "http"

    def destination_identity(self) -> str:
        """Any change here invalidates the last-success/failure stamps."""
        return "|".join(
            (
                self.provider,
                self.server_url.rstrip("/"),
                self.remote_path,
                self.username,
                str(self.scope),
            )
        )


@dataclass
class CloudBackupRunResult:
    outcome: str
    message: str = ""
    snapshot_name: str = ""
    unverified: bool = False
    pruned: int = 0


@dataclass
class RemoteSnapshot:
    name: str
    size: int
    created_utc: Optional[datetime]
    device_id: str = ""


def get_options(settings) -> CloudBackupOptions:
    slice_ = settings.settings.cloudBackup
    scope = 0
    if slice_.cloudBackupTodoEnabled:
        scope |= domains.TODO_DATA
    if slice_.cloudBackupQuickCaptureEnabled:
        scope |= domains.QUICK_CAPTURE_DATA
    if slice_.cloudBackupWidgetStyleEnabled:
        scope |= domains.WIDGET_STYLE
    provider = (slice_.cloudBackupProvider or "").strip().lower() or PROVIDER_NONE
    last_success = _parse_iso(slice_.cloudBackupLastSuccessUtc)
    return CloudBackupOptions(
        provider=provider,
        server_url=(slice_.cloudBackupUrl or "").strip(),
        remote_path=normalize_remote_path(slice_.cloudBackupRemotePath),
        username=(slice_.cloudBackupUsername or "").strip(),
        scope=scope,
        retention_count=max(1, min(50, int(slice_.cloudBackupRetainCount or 0))),
        interval_hours=normalize_interval_hours(int(slice_.cloudBackupIntervalHours or 0)),
        last_success_utc=last_success,
    )


def credential_key(options: CloudBackupOptions) -> str:
    """Vault key scoped by provider + endpoint origin + username: one host can
    reverse-proxy several DAV tenants; the wrong tenant must never receive a
    stored secret. The remote path stays out — folder choice, not auth boundary."""
    parts = urlsplit(options.server_url)
    if parts.scheme in ("http", "https") and parts.netloc:
        origin = (
            f"{parts.scheme}://{parts.hostname}"
            if parts.port is None
            else f"{parts.scheme}://{parts.hostname}:{parts.port}"
        )
        base_path = parts.path.rstrip("/")
        return f"{options.provider}:{options.username}@{origin.lower()}{base_path}"
    return f"{options.provider}:{options.username}@invalid"


def build_remote_snapshot_name(device_id: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{SNAPSHOT_FILE_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}-{device_id[:8]}{SNAPSHOT_SUFFIX}"


def parse_snapshot_timestamp(name: str) -> Optional[datetime]:
    if not is_safe_snapshot_basename(name):
        return None
    stem = name[len(SNAPSHOT_FILE_PREFIX) : -len(SNAPSHOT_SUFFIX)]
    timestamp = stem.split("-", 1)[0]
    try:
        return datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_safe_snapshot_basename(name: str) -> bool:
    return (
        isinstance(name, str)
        and name.startswith(SNAPSHOT_FILE_PREFIX)
        and name.endswith(SNAPSHOT_SUFFIX)
        and "/" not in name
        and "\\" not in name
        and ".." not in name
        and len(name) > len(SNAPSHOT_FILE_PREFIX) + len(SNAPSHOT_SUFFIX)
    )


class CloudBackupService:
    def __init__(
        self,
        settings_service,
        data_backup: Optional[DataBackupService] = None,
        credentials: Optional[CredentialStore] = None,
        log: Optional[Callable[[str], None]] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings_service
        self.data_backup = data_backup or DataBackupService(settings_service)
        self.credentials = credentials or CredentialStore()
        self._log = log or (lambda message: None)
        self._sleep = sleeper
        self._in_progress = False
        self._last_attempt_monotonic = 0.0
        self._last_identity: Optional[str] = None

    # ---- transport / credentials ------------------------------------------------

    def create_transport(self, options: CloudBackupOptions) -> WebDavBackupTransport:
        password = self.credentials.get_password(credential_key(options)) or ""
        return WebDavBackupTransport(options.server_url, options.username, password)

    def store_credential(self, options: CloudBackupOptions, password: str, label: str = "PaneBox cloud backup") -> bool:
        """Save the provider secret and sweep stale provider-prefixed keys."""
        try:
            self.credentials.set_password(credential_key(options), password, label)
        except Exception as exc:
            self._log(f"[CloudBackup] credential store failed: {exc}")
            return False
        self._sweep_stale_credentials(options)
        self.settings.settings.cloudBackup.cloudBackupPasswordStored = True
        self.settings.save_debounced()
        return True

    def clear_credential(self, options: CloudBackupOptions) -> None:
        try:
            self.credentials.delete_password(credential_key(options))
        except Exception as exc:
            self._log(f"[CloudBackup] credential delete failed: {exc}")
        self.settings.settings.cloudBackup.cloudBackupPasswordStored = False
        self.settings.save_debounced()

    def _sweep_stale_credentials(self, options: CloudBackupOptions) -> None:
        prefix = f"{options.provider}:"
        current = credential_key(options)
        try:
            keys = self.credentials.list_keys()
        except Exception:
            keys = []
        for key in keys:
            if key.startswith(prefix) and key != current:
                self.credentials.delete_password(key)
                self._log(f"[CloudBackup] swept stale credential key: {key}")

    def test_connection(self) -> tuple[bool, str]:
        options = get_options(self.settings)
        if not options.has_endpoint:
            return False, "Server URL is not configured."
        try:
            transport = self.create_transport(options)
            transport.probe()
            return True, ""
        except CloudBackupTransportException as exc:
            return False, str(exc)
        except Exception as exc:  # network stack unavailable etc.
            return False, str(exc)

    # ---- run ---------------------------------------------------------------------

    def run_backup_now(self, app_version: str = "", device_id: str = "") -> CloudBackupRunResult:
        if self._in_progress:
            return CloudBackupRunResult(OUTCOME_IN_PROGRESS, "Another backup is already running.")
        options = get_options(self.settings)
        if not options.has_endpoint:
            return CloudBackupRunResult(OUTCOME_NOT_CONFIGURED, "Cloud backup is not configured yet.")
        if options.scope == 0:
            return CloudBackupRunResult(OUTCOME_NO_SCOPE, "No data types selected.")
        if self.credentials.get_password(credential_key(options)) in (None, ""):
            return CloudBackupRunResult(OUTCOME_MISSING_CREDENTIAL, "No password stored for this account.")
        if self.data_backup.has_pending_restore():
            return CloudBackupRunResult(
                OUTCOME_PENDING_RESTORE,
                "A restore is staged for the next launch — restart PaneBox first.",
            )

        self._invalidate_stamps_on_identity_change(options)
        self._in_progress = True
        self._last_attempt_monotonic = time.monotonic()
        try:
            return self._run_core(options, app_version, device_id)
        finally:
            self._in_progress = False

    def _run_core(self, options: CloudBackupOptions, app_version: str, device_id: str) -> CloudBackupRunResult:
        slice_ = self.settings.settings.cloudBackup
        from .device_identity import device_id as resolve_device_id

        device_id = device_id or resolve_device_id()
        staging_dir = self.data_backup.backup_staging_dir
        name = build_remote_snapshot_name(device_id)
        archive_path = staging_dir / name
        try:
            self.data_backup.export_scoped_backup(
                archive_path, options.scope, app_version=app_version, source_device_id=device_id
            )
        except ScopedBackupError as exc:
            slice_.cloudBackupLastResult = f"error:{exc}"
            self.settings.save_debounced()
            return CloudBackupRunResult("Failed", str(exc))

        uploaded_size = archive_path.stat().st_size
        try:
            transport = self.create_transport(options)
            transport.ensure_directory(options.remote_path)
            transport.upload(archive_path, f"{options.remote_path}/{name}")
        except CloudBackupTransportException as exc:
            slice_.cloudBackupLastFailureUtc = _utc_now_iso()
            slice_.cloudBackupLastResult = f"error:{exc}"
            self.settings.save_debounced()
            return CloudBackupRunResult("Failed", str(exc))

        # Verify by listing: the remote entry must appear with the uploaded size.
        unverified = not self._verify_upload(transport, options, name, uploaded_size)
        archive_path.unlink(missing_ok=True)
        now_iso = _utc_now_iso()
        if unverified:
            slice_.cloudBackupLastUnverifiedUtc = now_iso
        else:
            slice_.cloudBackupLastSuccessUtc = now_iso
            slice_.cloudBackupLastFailureUtc = None
            slice_.cloudBackupLastUnverifiedUtc = None
        slice_.cloudBackupLastRunAt = now_iso
        slice_.cloudBackupLastResult = "ok"

        pruned = self._prune_old_snapshots(transport, options)
        self.settings.save_debounced()
        message = name
        if unverified:
            message += " (uploaded, but the server's file list has not confirmed it yet)"
        return CloudBackupRunResult(OUTCOME_UPLOADED, message, snapshot_name=name, unverified=unverified, pruned=pruned)

    def _verify_upload(
        self, transport: WebDavBackupTransport, options: CloudBackupOptions, name: str, size: int
    ) -> bool:
        """List the remote folder until the snapshot appears with the right
        size. An unconfirmed upload degrades to success-with-flag, never failure."""
        for delay in VERIFY_RETRY_DELAYS_SECONDS:
            try:
                entries = transport.list(options.remote_path)
                match = next(
                    (entry for entry in entries if entry.name == name and not entry.is_directory),
                    None,
                )
                if match is not None and match.size == size:
                    return True
            except CloudBackupTransportException as exc:
                self._log(f"[CloudBackup] verification listing failed: {exc}")
            self._sleep(delay)
        return False

    def _prune_old_snapshots(self, transport: WebDavBackupTransport, options: CloudBackupOptions) -> int:
        """Best-effort retention: keep max(1, retention) newest, ignore errors."""
        try:
            snapshots = self._list_snapshots(transport, options.remote_path)
            keep = max(1, options.retention_count)
            pruned = 0
            for stale in snapshots[keep:]:
                try:
                    transport.delete(f"{options.remote_path}/{stale.name}")
                    pruned += 1
                except CloudBackupTransportException as exc:
                    self._log(f"[CloudBackup] retention delete failed for {stale.name}: {exc}")
            return pruned
        except CloudBackupTransportException as exc:
            self._log(f"[CloudBackup] retention listing failed: {exc}")
            return 0

    @staticmethod
    def _list_snapshots(transport: WebDavBackupTransport, remote_path: str) -> list[RemoteSnapshot]:
        entries = [entry for entry in transport.list(remote_path) if not entry.is_directory]
        snapshots: list[RemoteSnapshot] = []
        for entry in entries:
            if not is_safe_snapshot_basename(entry.name):
                continue
            embedded = parse_snapshot_timestamp(entry.name)
            modified = _parse_http_date(entry.modified_utc)
            created = embedded or modified
            device = ""
            stem = entry.name[len(SNAPSHOT_FILE_PREFIX) : -len(SNAPSHOT_SUFFIX)]
            if "-" in stem:
                device = stem.split("-", 1)[1]
            snapshots.append(RemoteSnapshot(entry.name, entry.size, created, device))
        snapshots.sort(
            key=lambda snap: snap.created_utc or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return snapshots

    # ---- remote operations (settings UI) --------------------------------------------

    def list_remote_snapshots(self) -> list[RemoteSnapshot]:
        options = get_options(self.settings)
        transport = self.create_transport(options)
        return self._list_snapshots(transport, options.remote_path)

    def download_snapshot(self, name: str, destination: Path) -> Path:
        if not is_safe_snapshot_basename(name):
            raise CloudBackupTransportException(f"Unsafe snapshot name: {name!r}")
        options = get_options(self.settings)
        transport = self.create_transport(options)
        transport.download(f"{options.remote_path}/{name}", Path(destination))
        return Path(destination)

    def delete_snapshot(self, name: str) -> None:
        if not is_safe_snapshot_basename(name):
            raise CloudBackupTransportException(f"Unsafe snapshot name: {name!r}")
        options = get_options(self.settings)
        transport = self.create_transport(options)
        transport.delete(f"{options.remote_path}/{name}")

    # ---- scheduling -------------------------------------------------------------------

    def tick_scheduled(self, app_version: str = "") -> Optional[CloudBackupRunResult]:
        """Called periodically by the app; runs when due. The 10-minute
        spacing floor keeps a flapping schedule from hammering the server."""
        slice_ = self.settings.settings.cloudBackup
        if not slice_.cloudBackupEnabled:
            return None
        options = get_options(self.settings)
        if not options.is_configured:
            return None
        self._invalidate_stamps_on_identity_change(options)
        now = datetime.now(timezone.utc)
        last_success = options.last_success_utc
        due_at = (
            last_success + timedelta(hours=options.interval_hours)
            if last_success is not None
            else datetime.min.replace(tzinfo=timezone.utc)
        )
        if now < due_at:
            return None
        if time.monotonic() - self._last_attempt_monotonic < MIN_SCHEDULING_SPACING.total_seconds():
            return None
        result = self.run_backup_now(app_version=app_version)
        self._last_attempt_monotonic = time.monotonic()
        return result

    def is_due(self, now: Optional[datetime] = None) -> bool:
        slice_ = self.settings.settings.cloudBackup
        if not slice_.cloudBackupEnabled:
            return False
        options = get_options(self.settings)
        if not options.is_configured:
            return False
        now = now or datetime.now(timezone.utc)
        last_success = options.last_success_utc
        due_at = last_success + timedelta(hours=options.interval_hours) if last_success is not None else None
        return due_at is None or now >= due_at

    # ---- stamp invalidation ------------------------------------------------------------

    def refresh_options(self) -> CloudBackupOptions:
        """Settings-changed entry point (C# UpdateOptions): recompute options
        and invalidate destination-scoped stamps when the destination moved."""
        options = get_options(self.settings)
        self._invalidate_stamps_on_identity_change(options)
        return options

    def _invalidate_stamps_on_identity_change(self, options: CloudBackupOptions) -> None:
        identity = options.destination_identity()
        if self._last_identity is None:
            self._last_identity = identity  # first observation after startup
            return
        if identity == self._last_identity:
            return
        self._last_identity = identity
        slice_ = self.settings.settings.cloudBackup
        if slice_.cloudBackupLastSuccessUtc or slice_.cloudBackupLastFailureUtc or slice_.cloudBackupLastUnverifiedUtc:
            self._log("[CloudBackup] destination changed — resetting backup stamps.")
        slice_.cloudBackupLastSuccessUtc = None
        slice_.cloudBackupLastFailureUtc = None
        slice_.cloudBackupLastUnverifiedUtc = None
        self.settings.save_debounced()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_http_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


# Re-exported for callers that reason about restore staging.
__all__ = [
    "CloudBackupService",
    "CloudBackupOptions",
    "CloudBackupRunResult",
    "RemoteSnapshot",
    "PendingRestoreError",
    "get_options",
    "credential_key",
    "build_remote_snapshot_name",
    "parse_snapshot_timestamp",
    "is_safe_snapshot_basename",
    "normalize_remote_path",
]
