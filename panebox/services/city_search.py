"""Unified city search (port of Services/CitySearchService.cs).

Merges the embedded predefined city list (assets/cities.json) with dynamic
Open-Meteo geocoding results: relevance first (exact > prefix > contains >
pinyin), trusted local matches before API matches, haversine proximity only
as a tie-breaker. Also reverse-geocodes coordinates to the nearest known
city within 80 km.
"""

from __future__ import annotations

import json
import math
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from .chinese_text import is_traditional_chinese_culture, to_simplified, to_traditional

CITIES_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "cities.json"

_GLOBAL_POPULAR_INDICES = (0, 1, 2, 3, 4, 39, 53, 59, 78, 99, 113, 122, 139, 145, 153)


@dataclass
class PredefinedCity:
    zh: str = ""
    en: str = ""
    pinyin: str = ""
    lat: float = 0.0
    lon: float = 0.0
    country_zh: str = ""
    country_en: str = ""
    admin1_zh: str = ""
    admin1_en: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "PredefinedCity":
        return cls(
            zh=data.get("zh") or "",
            en=data.get("en") or "",
            pinyin=data.get("pinyin") or "",
            lat=float(data.get("lat") or 0.0),
            lon=float(data.get("lon") or 0.0),
            country_zh=data.get("country_zh") or "",
            country_en=data.get("country_en") or "",
            admin1_zh=data.get("admin1_zh") or "",
            admin1_en=data.get("admin1_en") or "",
        )


@dataclass
class WeatherCitySearchResult:
    name: str = ""
    displayName: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    country: str = ""
    admin1: str = ""


@dataclass
class _Candidate:
    result: WeatherCitySearchResult
    relevance: int
    is_local: bool
    sequence: int


_predefined: Optional[List[PredefinedCity]] = None
_predefined_lock = threading.Lock()


def _load_predefined() -> List[PredefinedCity]:
    global _predefined
    if _predefined is not None:
        return _predefined
    with _predefined_lock:
        if _predefined is not None:
            return _predefined
        try:
            document = json.loads(CITIES_PATH.read_text(encoding="utf-8"))
            _predefined = [PredefinedCity.from_dict(item) for item in document]
        except (OSError, ValueError):
            _predefined = []
        return _predefined


def predefined_cities() -> List[PredefinedCity]:
    return _load_predefined()


# ─── normalization ───────────────────────────────────────────────────────


def normalize_search_text(value: str | None) -> str:
    """FormD-decompose, drop combining marks and non-letters/digits, lowercase,
    then recompose (C# NormalizeSearchText)."""
    if not value or not value.strip():
        return ""
    decomposed = unicodedata.normalize("NFD", value)
    kept = []
    for character in decomposed:
        if unicodedata.combining(character):
            continue
        if character.isalnum():
            kept.append(character.lower())
    return unicodedata.normalize("NFC", "".join(kept))


def _is_chinese_language(language: str | None) -> bool:
    return bool(language) and language.lower().startswith("zh")


def _localize_chinese_text(value: str | None, use_traditional: bool) -> str:
    if use_traditional:
        return to_traditional(value or "")
    return value or ""


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_valid_coordinate(latitude: float, longitude: float) -> bool:
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    )


def _build_display_name_from_parts(name: str, admin1: str, country: str) -> str:
    parts = [name]
    if admin1 and admin1 != name:
        parts.append(admin1)
    if country:
        parts.append(country)
    return ", ".join(parts)


# ─── scoring ─────────────────────────────────────────────────────────────


def _matches_pinyin_initials(pinyin: str, initials: str) -> bool:
    """Port of the C# heuristic: "hz" matches "hangzhou", "bj" matches "beijing"."""
    if not pinyin or not pinyin.strip() or len(initials) > len(pinyin):
        return False
    if pinyin.lower().startswith(initials):
        return True
    if 2 <= len(initials) <= 4:
        for split_len in range(1, len(pinyin) - 1):
            if len(pinyin) - split_len < len(initials) - 1:
                break
            if pinyin[0].lower() != initials[0]:
                break
            if len(initials) == 2:
                for j in range(split_len, len(pinyin)):
                    if pinyin[j].lower() == initials[1]:
                        return True
    return False


def _get_search_relevance(city: PredefinedCity, normalized_query: str) -> int:
    zh = normalize_search_text(city.zh)
    en = normalize_search_text(city.en)
    pinyin = normalize_search_text(city.pinyin)
    if zh == normalized_query or en == normalized_query:
        return 500
    if pinyin == normalized_query:
        return 450
    if zh.startswith(normalized_query) or en.startswith(normalized_query) or pinyin.startswith(normalized_query):
        return 350
    if normalized_query in zh or normalized_query in en or normalized_query in pinyin:
        return 250
    return 100


def _get_result_relevance(result: WeatherCitySearchResult, query: str) -> int:
    normalized_query = normalize_search_text(query)
    name = normalize_search_text(result.name)
    display = normalize_search_text(result.displayName)
    if name == normalized_query:
        return 500
    if name.startswith(normalized_query):
        return 350
    if normalized_query in name:
        return 250
    return 100 if normalized_query in display else 0


def _get_local_result_relevance(result: WeatherCitySearchResult, query: str) -> int:
    for candidate in _load_predefined():
        if abs(candidate.lat - result.latitude) < 0.0001 and abs(candidate.lon - result.longitude) < 0.0001:
            return _get_search_relevance(candidate, normalize_search_text(query))
    return _get_result_relevance(result, query)


def _to_search_result(city: PredefinedCity, is_en: bool, use_traditional: bool) -> WeatherCitySearchResult:
    if is_en:
        name, admin1, country = city.en, city.admin1_en, city.country_en
    else:
        name = _localize_chinese_text(city.zh, use_traditional)
        admin1 = _localize_chinese_text(city.admin1_zh, use_traditional)
        country = _localize_chinese_text(city.country_zh, use_traditional)
    return WeatherCitySearchResult(
        name=name,
        displayName=_build_display_name_from_parts(name, admin1, country),
        latitude=city.lat,
        longitude=city.lon,
        country=country,
        admin1=admin1,
    )


# ─── public API ──────────────────────────────────────────────────────────


def get_nearest_city_name(lat: float, lon: float, language: str = "zh", max_distance_km: float = 80.0) -> Optional[str]:
    use_chinese = _is_chinese_language(language)
    use_traditional = is_traditional_chinese_culture(language)
    best: Optional[PredefinedCity] = None
    best_dist = float("inf")
    for city in _load_predefined():
        distance = _haversine_distance(lat, lon, city.lat, city.lon)
        if distance < best_dist:
            best_dist = distance
            best = city
    if best is None or best_dist > max_distance_km:
        return None
    if not use_chinese:
        return best.en
    return _localize_chinese_text(best.zh, use_traditional)


def search_local(query: str, is_en: bool, use_traditional: bool = False) -> List[WeatherCitySearchResult]:
    lower = query.lower()
    matching_query = to_simplified(query) if use_traditional else query
    normalized_query = normalize_search_text(matching_query)
    is_pinyin_initials = len(lower) >= 2 and all("a" <= c <= "z" for c in lower)

    matches: List[tuple[int, int, PredefinedCity]] = []
    for index, city in enumerate(_load_predefined()):
        if (
            normalized_query in normalize_search_text(city.zh)
            or normalized_query in normalize_search_text(city.en)
            or normalized_query in normalize_search_text(city.pinyin)
            or normalized_query in normalize_search_text(city.country_zh)
            or normalized_query in normalize_search_text(city.country_en)
            or normalized_query in normalize_search_text(city.admin1_zh)
            or normalized_query in normalize_search_text(city.admin1_en)
            or (is_pinyin_initials and _matches_pinyin_initials(city.pinyin, lower))
        ):
            matches.append((_get_search_relevance(city, normalized_query), -index, city))
    matches.sort(key=lambda entry: (-entry[0], entry[1]))
    return [_to_search_result(city, is_en, use_traditional) for _score, _index, city in matches[:8]]


def search(
    query: str,
    language: str = "zh",
    user_lat: Optional[float] = None,
    user_lon: Optional[float] = None,
    api_search: Optional[Callable[[str, str], List]] = None,
) -> List[WeatherCitySearchResult]:
    """Merged local + API search. api_search(query, language) returns geocoding
    items (objects with name/admin1/country/latitude/longitude or dicts); pass a
    stub in tests. Defaults to WeatherService.search_city."""
    if not query or not query.strip():
        return []
    query = query.strip()
    if not normalize_search_text(query):
        return []

    # Single CJK characters allowed ("京" → 北京); Latin needs ≥2.
    has_cjk = any("一" <= c <= "鿿" for c in query)
    if not has_cjk and len(query) < 2:
        return []

    is_en = not _is_chinese_language(language)
    use_traditional = is_traditional_chinese_culture(language)

    local_results = search_local(query, is_en, use_traditional)

    api_items: List = []
    fetch = api_search
    if fetch is None:
        from .weather_service import WeatherService

        fetch = WeatherService().search_city
    try:
        api_items = fetch(query, language) or []
    except Exception:
        api_items = []

    merged: List[_Candidate] = []
    seen: set[str] = set()
    sequence = 0

    for result in local_results:
        key = f"{result.latitude:.2f},{result.longitude:.2f}"
        if key not in seen:
            seen.add(key)
            merged.append(_Candidate(result, _get_local_result_relevance(result, query), True, sequence))
            sequence += 1

    for item in api_items:
        if isinstance(item, dict):
            name = item.get("name") or ""
            admin1 = item.get("admin1") or ""
            country = item.get("country") or ""
            latitude = float(item.get("latitude") or 0.0)
            longitude = float(item.get("longitude") or 0.0)
        else:
            name, admin1, country = item.name, item.admin1, item.country
            latitude, longitude = item.latitude, item.longitude
        if not _is_valid_coordinate(latitude, longitude):
            continue
        key = f"{latitude:.2f},{longitude:.2f}"
        if key in seen:
            continue
        seen.add(key)
        name = _localize_chinese_text(name, use_traditional)
        admin1 = _localize_chinese_text(admin1, use_traditional)
        country = _localize_chinese_text(country, use_traditional)
        result = WeatherCitySearchResult(
            name=name,
            displayName=_build_display_name_from_parts(name, admin1, country),
            latitude=latitude,
            longitude=longitude,
            country=country,
            admin1=admin1,
        )
        merged.append(_Candidate(result, _get_result_relevance(result, query), False, sequence))
        sequence += 1

    def distance_key(candidate: _Candidate) -> float:
        if user_lat is not None and user_lon is not None:
            return _haversine_distance(user_lat, user_lon, candidate.result.latitude, candidate.result.longitude)
        return float("inf")

    merged.sort(key=lambda c: (-c.relevance, not c.is_local, distance_key(c), c.sequence))
    return [candidate.result for candidate in merged[:10]]


def get_nearby_popular_cities(
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    language: str = "zh",
    max_count: int = 8,
) -> List[WeatherCitySearchResult]:
    is_en = not _is_chinese_language(language)
    use_traditional = is_traditional_chinese_culture(language)
    cities = list(_load_predefined())
    if lat is not None and lon is not None:
        cities.sort(key=lambda c: _haversine_distance(lat, lon, c.lat, c.lon))
    return [_to_search_result(c, is_en, use_traditional) for c in cities[:max_count]]


def get_global_popular_cities(language: str = "zh", max_count: int = 8) -> List[WeatherCitySearchResult]:
    is_en = not _is_chinese_language(language)
    use_traditional = is_traditional_chinese_culture(language)
    cities = _load_predefined()
    picked = [cities[i] for i in _GLOBAL_POPULAR_INDICES if i < len(cities)]
    return [_to_search_result(c, is_en, use_traditional) for c in picked[:max_count]]
