"""Update check: version parsing/compare, manifest→GitHub fallback, bookkeeping."""

from __future__ import annotations

import json

from panebox.services.update_check import (
    UpdateCheckService,
    UpdateCheckStatus,
    is_remote_version_newer,
    try_parse_version,
)


# ---- version parsing ------------------------------------------------------------------


def test_try_parse_version_accepts_prefix_suffix_and_short_forms():
    assert try_parse_version("v1.2.3-beta") == (1, 2, 3, 0)
    assert try_parse_version("V2.0.0+meta") == (2, 0, 0, 0)
    assert try_parse_version("1.2") == (1, 2, 0, 0)
    assert try_parse_version("  0.3.0  ") == (0, 3, 0, 0)
    assert try_parse_version("1.2.3.4") == (1, 2, 3, 4)


def test_try_parse_version_rejects_garbage():
    for bad in ("abc", "", None, "1.2.3.4.5", "1..2", "1.x.2"):
        assert try_parse_version(bad) is None


def test_is_remote_version_newer_compares_numerically():
    assert is_remote_version_newer("0.3.0", "1.5.5")
    assert is_remote_version_newer("0.3.0", "v0.3.1-beta")
    assert not is_remote_version_newer("0.3.0", "0.3.0")
    assert not is_remote_version_newer("0.3.0", "0.2.9")
    assert not is_remote_version_newer("0.3.0", "not-a-version")  # malformed loses


# ---- service ----------------------------------------------------------------------------


def _manifest(version="1.5.5", summary="Fixes and polish.", notes="https://panebox.fun/notes"):
    return json.dumps({"version": version, "summary": {"en-US": summary}, "releaseNotesUrl": notes})


def _release(tag="v2.0.0"):
    return json.dumps({"tag_name": tag, "body": "Release body", "html_url": "https://github.com/r/v"})


def test_manifest_update_available_wins_without_github_call():
    calls: list[str] = []

    def fetch(url):
        calls.append(url)
        return 200, _manifest()

    service = UpdateCheckService(current_version="0.3.0", fetch=fetch)
    result = service.check()
    assert result.status == UpdateCheckStatus.UPDATE_AVAILABLE
    assert result.is_update_available
    assert result.remote_version == "1.5.5"
    assert result.summary == "Fixes and polish."
    assert result.release_notes_url == "https://panebox.fun/notes"
    assert calls == [service.manifest_url]  # GitHub never consulted
    assert service.last_check_result is result
    assert service.last_check_time_utc is not None


def test_manifest_up_to_date_short_circuits():
    service = UpdateCheckService(current_version="1.5.5", fetch=lambda _u: (200, _manifest("1.5.5")))
    result = service.check()
    assert result.status == UpdateCheckStatus.UP_TO_DATE
    assert not result.is_update_available


def test_manifest_http_error_falls_back_to_github_release():
    def fetch(url):
        if url.endswith("stable.json"):
            return 500, "server exploded"
        return 200, _release("v2.0.0")

    service = UpdateCheckService(current_version="0.3.0", fetch=fetch)
    result = service.check()
    assert result.status == UpdateCheckStatus.UPDATE_AVAILABLE
    assert result.remote_version == "v2.0.0"  # tag_name verbatim, as on Windows


def test_unparseable_bodies_everywhere_report_first_failure():
    def fetch(_url):
        return 200, "not json at all"

    service = UpdateCheckService(current_version="0.3.0", fetch=fetch)
    result = service.check()
    # JSON decode exceptions map to Failed; the manifest verdict wins.
    assert result.status == UpdateCheckStatus.FAILED
    assert not result.is_update_available


def test_network_exception_is_failed_not_crash():
    def fetch(_url):
        raise OSError("dns is a rumor")

    service = UpdateCheckService(current_version="0.3.0", fetch=fetch)
    result = service.check()
    assert result.status == UpdateCheckStatus.FAILED
    assert "dns" in result.error


def test_manifest_without_usable_version_falls_back():
    seen: list[str] = []

    def fetch(url):
        seen.append(url)
        if url.endswith("stable.json"):
            return 200, json.dumps({"version": "tomorrow"})
        return 200, _release("0.3.0")  # GitHub not newer either

    service = UpdateCheckService(current_version="0.3.0", fetch=fetch)
    result = service.check()
    assert result.status == UpdateCheckStatus.UP_TO_DATE  # GitHub verdict wins
    assert set(seen) == {service.manifest_url, service.github_api_url}


def test_checked_at_stamp_present_on_every_result():
    service = UpdateCheckService(current_version="0.3.0", fetch=lambda _u: (200, _manifest()))
    stamp = service.check().checked_at_utc
    assert stamp and "T" in stamp  # ISO-UTC, feeds lastUpdateCheckAt
