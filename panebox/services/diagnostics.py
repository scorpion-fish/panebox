"""Diagnostics bundle + privacy filter (port of PaneBoxDiagnosticsBundleService.cs).

Exports a deliberately narrow support package: a runtime snapshot
(diagnostics.json), a bilingual README, and a sanitized tail of the
application log. It never includes settings.json, widget content stores,
file inventories, or user attachment data. Paths, account names, email
addresses and Windows security identifiers are redacted from the log tail
before it enters the archive.
"""

from __future__ import annotations

import json
import os
import platform
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..constants import APP_VERSION, LOG_FILE

MAX_LOG_TAIL_BYTES = 2 * 1024 * 1024
SCHEMA_VERSION = 5

README_TEXT = """PaneBox diagnostic package

This archive contains a runtime snapshot and a sanitized tail of the application log.
It does not contain settings.json, widget contents, file lists, clipboard records, tasks, or attachments.
Paths, account names, email addresses, and Windows security identifiers are automatically redacted.
Please review the archive before sharing it with support.

PaneBox 诊断包

本压缩包只包含运行状态快照和经过脱敏的应用日志末尾，不包含设置文件、格子内容、文件列表、剪贴板记录、待办或附件。
路径、账户名、电子邮箱和 Windows 安全标识符会自动隐藏。发送给支持人员前仍建议快速检查一次。
"""

# key names that smell like a location -> value redacted (bools survive)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?im)\b([a-z0-9_]*(?:path|folder|root|directory|dir|file|exe|commandline)[a-z0-9_]*)"
    r"\s*=\s*('[^'\r\n]*'|\"[^\"\r\n]*\"|[^\r\n]*?)(?=\s+[a-z0-9_]+\s*=|\r?$)"
)
_QUOTED_WINDOWS_PATH = re.compile(r"(?i)(?P<quote>['\"])(?:[a-z]:[\\/]|\\\\)[^\r\n]*?(?P=quote)")
_UNQUOTED_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\s'\",;)\]]+")
_QUOTED_POSIX_PATH = re.compile(r"(?P<quote>['\"])/[^\r\n]*?(?P=quote)")
# Absolute POSIX paths under user-data roots; the lookbehind keeps URL hosts
# (https://example.com/home/...) out of the match.
_UNQUOTED_POSIX_PATH = re.compile(r"(?<![\w.:-])/(?:home|root|Users|media|mnt|srv)/[^\s'\",;)\]]*")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SID = re.compile(r"\bS-1-(?:\d+-){1,14}\d+\b", re.IGNORECASE)


def _is_bool_literal(value: str) -> bool:
    return value.strip().lower() in ("true", "false")


def account_values() -> list[str]:
    """Redaction candidates for account/machine names, longest first."""
    import getpass
    import socket

    candidates = [
        os.environ.get("USER", ""),
        os.environ.get("LOGNAME", ""),
        "",
    ]
    try:
        candidates.append(getpass.getuser())
    except Exception:
        pass
    try:
        candidates.append(socket.gethostname())
    except Exception:
        pass
    seen: set[str] = set()
    unique = []
    for value in candidates:
        if len(value) >= 3 and value.lower() not in seen:
            seen.add(value.lower())
            unique.append(value)
    return sorted(unique, key=len, reverse=True)


def sanitize_log(value: str) -> str:
    """Privacy filter applied to log text before it leaves the machine."""
    if not value:
        return ""

    def redact_assignment(match: re.Match) -> str:
        captured = match.group(2)
        if _is_bool_literal(captured):
            return match.group(0)
        return f"{match.group(1)}=<REDACTED>"

    sanitized = _SENSITIVE_ASSIGNMENT.sub(redact_assignment, value)
    sanitized = _QUOTED_WINDOWS_PATH.sub(lambda _m: "'<PATH>'", sanitized)
    sanitized = _UNQUOTED_WINDOWS_PATH.sub("<PATH>", sanitized)
    sanitized = _QUOTED_POSIX_PATH.sub(lambda _m: "'<PATH>'", sanitized)
    sanitized = _UNQUOTED_POSIX_PATH.sub("<PATH>", sanitized)
    sanitized = _EMAIL.sub("<EMAIL>", sanitized)
    sanitized = _SID.sub("<SID>", sanitized)
    for account in account_values():
        sanitized = re.sub(re.escape(account), "<USER>", sanitized, flags=re.IGNORECASE)
    return sanitized


def read_sanitized_log_tail(log_file_path: Optional[Path], max_bytes: int = MAX_LOG_TAIL_BYTES) -> str:
    if not log_file_path or not Path(log_file_path).is_file():
        return "PaneBox log was not available when the package was created."
    data = Path(log_file_path).read_bytes()
    start = max(0, len(data) - max_bytes)
    tail = data[start:]
    text = tail.decode("utf-8", errors="replace")
    if start > 0:
        first_break = text.find("\n")
        text = text[first_break + 1 :] if first_break >= 0 else ""
    return sanitize_log(text)


# ---- snapshot collection ------------------------------------------------------------


def _rect(x: float, y: float, w: float, h: float) -> dict:
    return {"x": x, "y": y, "width": w, "height": h}


def _display_diagnostics(log=print) -> list[dict]:
    try:
        from ..platform.workarea import list_monitors

        return [
            {
                "number": index + 1,
                "isPrimary": monitor.is_primary,
                "dpiScale": monitor.scale,
                "connector": monitor.connector,
                "monitorBounds": _rect(monitor.x, monitor.y, monitor.width, monitor.height),
                "workAreaBounds": _rect(monitor.work_x, monitor.work_y, monitor.work_width, monitor.work_height),
            }
            for index, monitor in enumerate(list_monitors())
        ]
    except Exception as exc:
        log(f"[Diagnostics] display enumeration failed: {exc}")
        return []


def _hotkey_diagnostic(settings_service, hotkey, search_hotkey) -> dict:
    """Settings-derived hotkey state (the controllers don't retain status)."""
    core = settings_service.settings.core
    search = settings_service.settings.search

    def key_name(keysym: int) -> str:
        try:
            import gi

            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            return Gdk.keyval_name(keysym) or str(keysym)
        except Exception:
            return str(keysym)

    return {
        "toggleEnabled": core.globalHotkeyEnabled,
        "toggleActivationKind": core.globalHotkeyActivationKind,
        "toggleModifiers": core.globalHotkeyModifiers,
        "toggleKey": key_name(core.globalHotkeyKey),
        "searchEnabled": search.searchHotkeyEnabled,
        "searchModifiers": search.searchHotkeyModifiers,
        "searchKey": key_name(search.searchHotkeyKey),
    }


def _widget_manager_diagnostic(widget_manager) -> dict:
    if widget_manager is None or not getattr(widget_manager, "runtimes", None):
        return {
            "widgetsRaisedFromTray": False,
            "sessionState": "Unavailable",
            "loadedSurfaceCount": 0,
            "visibleSurfaceCount": 0,
            "hosts": [],
        }
    runtimes = list(widget_manager.runtimes.values())
    hosts = []
    for index, runtime in enumerate(sorted(runtimes, key=lambda rt: rt.config.id)):
        config = runtime.config
        window = getattr(runtime, "window", None)
        hosts.append(
            {
                "number": index + 1,
                "widgetKind": config.widgetKind,
                "isGroupSurface": bool(getattr(config, "groupId", "")),
                "visible": bool(window and window.get_visible()),
                "collapsed": bool(config.isCollapsed),
                "positionLocked": bool(config.isPositionLocked),
                "bounds": _rect(config.x, config.y, config.width, config.height),
            }
        )
    raised = False
    try:
        raised = bool(widget_manager.raise_toggle.widgets_raised)
    except Exception:
        raised = any(rt.window.get_visible() for rt in runtimes if getattr(rt, "window", None))
    return {
        "widgetsRaisedFromTray": raised,
        "sessionState": "Running",
        "loadedSurfaceCount": len(runtimes),
        "visibleSurfaceCount": sum(1 for rt in runtimes if getattr(rt, "window", None) and rt.window.get_visible()),
        "hosts": hosts,
    }


def collect_snapshot(
    settings_service,
    widget_manager=None,
    hotkey=None,
    search_hotkey=None,
    log=print,
) -> dict:
    from .. import i18n

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "appVersion": APP_VERSION,
        "distributionChannel": "linux-port",
        "isPackaged": False,
        "operatingSystem": platform.platform(),
        "processArchitecture": platform.machine(),
        "uiCulture": i18n.current_language(),
        "hotkeys": _hotkey_diagnostic(settings_service, hotkey, search_hotkey),
        "settings": {
            "loadRecoveryState": str(getattr(settings_service, "load_state", "")),
            "layoutLoadRecoveryState": str(getattr(settings_service, "layout_load_state", "")),
            "hasPendingSave": bool(getattr(settings_service, "_pending_save", False)),
            "lastPersistenceError": getattr(settings_service, "last_persistence_error", "") or None,
        },
        "widgetManager": _widget_manager_diagnostic(widget_manager),
        "displays": _display_diagnostics(log=log),
    }


# ---- bundle export -------------------------------------------------------------------


class DiagnosticsBundleService:
    def __init__(self, log=print):
        self._log = log

    def export(self, destination_directory: Path, snapshot: dict, log_file_path: Optional[Path] = None) -> Path:
        if not str(destination_directory).strip():
            raise ValueError("destination directory is empty")
        destination = Path(destination_directory)
        destination.mkdir(parents=True, exist_ok=True)
        archive_path = self._available_archive_path(destination, snapshot.get("generatedAtUtc"))
        temporary = archive_path.with_name(archive_path.name + ".tmp")
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(
                    "diagnostics.json",
                    json.dumps(snapshot, ensure_ascii=False, indent=2),
                )
                zf.writestr("README.txt", README_TEXT)
                zf.writestr(
                    "PaneBox-sanitized.log",
                    read_sanitized_log_tail(log_file_path or LOG_FILE),
                )
            os.replace(temporary, archive_path)
            return archive_path
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _available_archive_path(destination: Path, generated_at_utc) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            parsed = datetime.fromisoformat(str(generated_at_utc).replace("Z", "+00:00"))
            stamp = parsed.astimezone().strftime("%Y%m%d-%H%M%S")
        except (TypeError, ValueError):
            pass
        stem = f"PaneBox-Diagnostics-{stamp}"
        path = destination / f"{stem}.zip"
        suffix = 2
        while path.exists() or path.with_name(path.name + ".tmp").exists():
            path = destination / f"{stem}-{suffix}.zip"
            suffix += 1
        return path
