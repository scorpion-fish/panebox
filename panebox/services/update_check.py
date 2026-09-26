"""App update check (port of the check half of Services/AppUpdateService.cs).

Checks the official manifest first, then the GitHub latest-release API,
with the same version semantics (optional v prefix, prerelease/build
suffixes stripped, numeric major.minor.build[.revision] compare).

The Windows download/verify/install pipeline is intentionally NOT ported:
Linux installs arrive through the distribution channel (package manager /
source checkout), so the port reports availability and links the official
download page instead of running an installer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from ..constants import APP_VERSION

DEFAULT_MANIFEST_URL = "https://panebox.fun/update/stable.json"
GITHUB_LATEST_RELEASE_API_URL = "https://api.github.com/repos/Tianyu199509/PaneBox/releases/latest"
MANUAL_DOWNLOAD_URL = "https://panebox.fun/download"
REQUEST_TIMEOUT_SECONDS = 20


class UpdateCheckStatus:
    UPDATE_AVAILABLE = "UpdateAvailable"
    UP_TO_DATE = "UpToDate"
    FAILED = "Failed"
    INVALID_MANIFEST = "InvalidManifest"


@dataclass
class UpdateCheckResult:
    status: str
    current_version: str
    remote_version: str = ""
    release_notes_url: str = ""
    summary: str = ""
    error: str = ""
    checked_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_update_available(self) -> bool:
        return self.status == UpdateCheckStatus.UPDATE_AVAILABLE


def try_parse_version(value) -> Optional[tuple[int, ...]]:
    """'v1.2.3-beta+meta' -> (1, 2, 3). Same rules as the C# TryParseVersion."""
    if not value or not str(value).strip():
        return None
    normalized = str(value).strip()
    if normalized[0] in ("v", "V"):
        normalized = normalized[1:]
    cut = len(normalized)
    for marker in ("-", "+"):
        index = normalized.find(marker)
        if index >= 0:
            cut = min(cut, index)
    normalized = normalized[:cut]
    parts = normalized.split(".")
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        numbers.append(int(part))
    if not numbers or len(numbers) > 4 or any(part == "" for part in parts):
        return None
    while len(numbers) < 4:
        numbers.append(0)
    return tuple(numbers)


def is_remote_version_newer(current_version: str, remote_version: str) -> bool:
    current = try_parse_version(current_version)
    remote = try_parse_version(remote_version)
    if current is None or remote is None:
        return False
    return remote > current


def _manifest_version(document) -> tuple[str, str, str]:
    """(version, summary, notes-url) from the official manifest."""
    if not isinstance(document, dict):
        return "", "", ""
    version = str(document.get("version") or "")
    summary_map = document.get("summary")
    summary = ""
    if isinstance(summary_map, dict):
        summary = str(summary_map.get("en-US") or next(iter(summary_map.values()), ""))
    return version, summary, str(document.get("releaseNotesUrl") or "")


def _release_version(document) -> tuple[str, str, str]:
    """(version, notes-body, notes-url) from a GitHub latest-release reply."""
    if not isinstance(document, dict) or not document.get("tag_name"):
        return "", "", ""
    return (
        str(document["tag_name"]),
        str(document.get("body") or ""),
        str(document.get("html_url") or ""),
    )


class UpdateCheckService:
    def __init__(
        self,
        current_version: str = APP_VERSION,
        manifest_url: str = DEFAULT_MANIFEST_URL,
        github_api_url: str = GITHUB_LATEST_RELEASE_API_URL,
        fetch: Optional[Callable[[str], tuple[int, str]]] = None,
        log: Callable[[str], None] = lambda _message: None,
    ):
        self.current_version = current_version
        self.manifest_url = manifest_url
        self.github_api_url = github_api_url
        self._fetch = fetch or self._fetch_with_requests
        self._log = log
        self.last_check_result: Optional[UpdateCheckResult] = None
        self.last_check_time_utc: Optional[datetime] = None

    @staticmethod
    def _fetch_with_requests(url: str) -> tuple[int, str]:
        import requests

        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": f"PaneBox/{APP_VERSION}"})
        return response.status_code, response.text

    def check(self) -> UpdateCheckResult:
        """Manifest first; only when it yields no verdict does GitHub run."""
        result = self._check_endpoint(self.manifest_url, _manifest_version, "update manifest")
        if result.status in (UpdateCheckStatus.UPDATE_AVAILABLE, UpdateCheckStatus.UP_TO_DATE):
            return self._record(result)
        fallback = self._check_endpoint(self.github_api_url, _release_version, "GitHub release metadata")
        if fallback.status in (UpdateCheckStatus.UPDATE_AVAILABLE, UpdateCheckStatus.UP_TO_DATE):
            return self._record(fallback)
        return self._record(result)

    def _check_endpoint(self, url: str, extract, source_name: str) -> UpdateCheckResult:
        import json

        try:
            status_code, body = self._fetch(url)
            if status_code < 200 or status_code >= 300:
                return UpdateCheckResult(
                    UpdateCheckStatus.FAILED,
                    self.current_version,
                    error=f"{source_name} returned HTTP {status_code}",
                )
            version, summary, notes_url = extract(json.loads(body))
            if not version or try_parse_version(version) is None:
                return UpdateCheckResult(
                    UpdateCheckStatus.INVALID_MANIFEST,
                    self.current_version,
                    error=f"The {source_name} carries no usable version.",
                )
        except Exception as exc:  # network / JSON / timeout — all just Failed
            return UpdateCheckResult(UpdateCheckStatus.FAILED, self.current_version, error=str(exc))
        if is_remote_version_newer(self.current_version, version):
            return UpdateCheckResult(
                UpdateCheckStatus.UPDATE_AVAILABLE,
                self.current_version,
                remote_version=version,
                summary=summary,
                release_notes_url=notes_url,
            )
        return UpdateCheckResult(UpdateCheckStatus.UP_TO_DATE, self.current_version, remote_version=version)

    def _record(self, result: UpdateCheckResult) -> UpdateCheckResult:
        self.last_check_result = result
        self.last_check_time_utc = datetime.now(timezone.utc)
        return result
