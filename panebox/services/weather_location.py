"""IP-based location resolution (port of Helpers/WindowsLocationHelper.cs).

Linux has no equivalent of the Windows Geolocation API, so only the IP
fallback chain runs (documented divergence in the README): three concurrent
sources — ip-api.com (supports localized city names), ipapi.co, api.ip.sb —
first successful result wins. Successful results are reused for 6 hours,
failures for 10 minutes, and concurrent callers share one in-flight resolve.
"""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from .weather_service import REQUEST_TIMEOUT_SECONDS, normalize_geocoding_language

SUCCESSFUL_RESULT_REUSE_DURATION = timedelta(hours=6)
FAILED_RESULT_REUSE_DURATION = timedelta(minutes=10)

Location = Tuple[float, float, str]

_gate = threading.Lock()
_cached_location: Optional[Location] = None
_cached_location_at = datetime.min.replace(tzinfo=timezone.utc)
_last_failure_at = datetime.min.replace(tzinfo=timezone.utc)
_in_flight: Optional["Future"] = None
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="wx-loc")


def _http_get_json(url: str) -> dict:
    import requests

    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _fallback_name(current_location_label: str) -> str:
    return current_location_label or "Current Location"


# ─── sources ────────────────────────────────────────────────────────────


def _try_ip_api_com(fetch_json: Callable[[str], dict], api_language: str, label: str) -> Optional[Location]:
    try:
        root = fetch_json(f"http://ip-api.com/json/?fields=status,lat,lon,city,regionName,country&lang={api_language}")
        if root.get("status") not in (None, "success"):
            return None
        if "lat" in root and "lon" in root:
            lat, lon = float(root["lat"]), float(root["lon"])
            name = ""
            if (root.get("city") or "").strip():
                name = root["city"]
            elif (root.get("regionName") or "").strip():
                name = root["regionName"]
            return (lat, lon, name or _fallback_name(label))
    except Exception:
        pass
    return None


def _try_ip_api_co(fetch_json: Callable[[str], dict], is_english: bool, label: str) -> Optional[Location]:
    try:
        root = fetch_json("https://ipapi.co/json/")
        if "latitude" in root and "longitude" in root:
            lat, lon = float(root["latitude"]), float(root["longitude"])
            name = ""
            if is_english and (root.get("city") or "").strip():
                name = root["city"]
            elif (root.get("region") or "").strip():
                name = root["region"]
            return (lat, lon, name or _fallback_name(label))
    except Exception:
        pass
    return None


def _try_ip_sb(fetch_json: Callable[[str], dict], is_english: bool, label: str) -> Optional[Location]:
    try:
        root = fetch_json("https://api.ip.sb/geoip")
        if "latitude" in root and "longitude" in root:
            lat, lon = float(root["latitude"]), float(root["longitude"])
            name = ""
            if (root.get("city") or "").strip():
                name = root["city"]
            elif (root.get("region") or "").strip():
                name = root["region"]
            return (lat, lon, name or _fallback_name(label))
    except Exception:
        pass
    return None


def _resolve(fetch_json: Callable[[str], dict], api_language: str, is_english: bool, label: str) -> Optional[Location]:
    pending = {
        _executor.submit(_try_ip_api_com, fetch_json, api_language, label),
        _executor.submit(_try_ip_api_co, fetch_json, is_english, label),
        _executor.submit(_try_ip_sb, fetch_json, is_english, label),
    }
    try:
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                if result is not None:
                    return result
    finally:
        for future in pending:
            future.cancel()
    return None


# ─── public API ──────────────────────────────────────────────────────────


def get_location(
    language: str = "zh-CN",
    current_location_label: str = "",
    force_refresh: bool = False,
    fetch_json: Optional[Callable[[str], dict]] = None,
) -> Optional[Location]:
    """Resolve (lat, lon, display name) by IP. Blocking; call off the UI thread."""
    global _cached_location, _cached_location_at, _last_failure_at, _in_flight

    now = datetime.now(timezone.utc)
    with _gate:
        if not force_refresh and _cached_location is not None:
            if now - _cached_location_at <= SUCCESSFUL_RESULT_REUSE_DURATION:
                return _cached_location
        if not force_refresh and _last_failure_at != datetime.min.replace(tzinfo=timezone.utc):
            if now - _last_failure_at <= FAILED_RESULT_REUSE_DURATION:
                return None
        if _in_flight is not None:
            in_flight = _in_flight
        else:
            fetch = fetch_json or _http_get_json
            api_language = normalize_geocoding_language(language)
            is_english = api_language == "en"
            label = current_location_label
            _in_flight = in_flight = _executor.submit(_resolve_and_cache, fetch, api_language, is_english, label)

    try:
        return in_flight.result()
    finally:
        with _gate:
            if _in_flight is in_flight:
                _in_flight = None


def _resolve_and_cache(
    fetch_json: Callable[[str], dict], api_language: str, is_english: bool, label: str
) -> Optional[Location]:
    global _cached_location, _cached_location_at, _last_failure_at
    result = _resolve(fetch_json, api_language, is_english, label)
    with _gate:
        if result is not None:
            _cached_location = result
            _cached_location_at = datetime.now(timezone.utc)
            _last_failure_at = datetime.min.replace(tzinfo=timezone.utc)
        else:
            _last_failure_at = datetime.now(timezone.utc)
    return result


def reset_for_tests() -> None:
    global _cached_location, _cached_location_at, _last_failure_at, _in_flight
    with _gate:
        _cached_location = None
        _cached_location_at = datetime.min.replace(tzinfo=timezone.utc)
        _last_failure_at = datetime.min.replace(tzinfo=timezone.utc)
        _in_flight = None
