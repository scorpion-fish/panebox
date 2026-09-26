"""Weather service (port of Services/WeatherService.cs).

Open-Meteo forecast + geocoding, MSN Weather secondary with automatic
fallback between sources, a 30-minute in-memory + on-disk cache keyed by
"F4,F4" coordinates and data source, and stale-cache-on-total-failure.
Blocking calls — run from worker threads and marshal results to the UI.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from urllib.parse import quote

from ..models.weather import (
    DATA_SOURCE_MSN,
    DATA_SOURCE_OPEN_METEO,
    WeatherCacheState,
    WeatherCachedForecast,
    WeatherCurrent,
    WeatherDaily,
    WeatherData,
    WeatherGeocodingItem,
    WeatherHourly,
)
from .weather_cache import WeatherCacheStore
from .weather_codes import msn_description_or_icon_to_wmo_code

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_BASE_URL = "https://geocoding-api.open-meteo.com/v1/search"
MSN_WEATHER_URL = "https://api.msn.com/weather/overview"
MSN_API_KEY = "UhJ4G66OjyLbn9mXARgajXLiLw6V75sHnfpU60aJBB"

DEFAULT_CACHE_DURATION = timedelta(minutes=30)
REQUEST_TIMEOUT_SECONDS = 6.0  # per-source ceiling, as on Windows


def normalize_geocoding_language(language: str | None) -> str:
    if not language or not language.strip():
        return "en"
    if language.lower().startswith("zh"):
        # Open-Meteo returns English for zh-TW/zh-Hant while plain zh returns
        # Chinese; traditional conversion happens in CitySearchService.
        return "zh"
    language = language.strip()
    for separator in ("-", "_"):
        if separator in language:
            return language.split(separator)[0].lower()
    return language.lower()


def build_open_meteo_forecast_url(latitude: float, longitude: float) -> str:
    return (
        f"{OPEN_METEO_FORECAST_URL}"
        f"?latitude={latitude:.4f}"
        f"&longitude={longitude:.4f}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,pressure_msl,is_day"
        "&hourly=temperature_2m,precipitation_probability,weather_code"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,uv_index_max"
        "&timezone=auto"
        "&forecast_days=7"
        "&wind_speed_unit=kmh"
        "&temperature_unit=celsius"
        "&precipitation_unit=mm"
    )


def build_msn_weather_url(latitude: float, longitude: float) -> str:
    return f"{MSN_WEATHER_URL}?apikey={MSN_API_KEY}&lat={latitude:.4f}&lon={longitude:.4f}&units=C"


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def extract_msn_weather_body(document: dict) -> Optional[dict]:
    """value[0].responses[0].weather[0] — the single body the C# service uses."""
    try:
        return document["value"][0]["responses"][0]["weather"][0]
    except (KeyError, IndexError, TypeError):
        return None


def convert_msn_to_weather_data(msn: dict, latitude: float, longitude: float) -> WeatherData:
    current = msn.get("current")
    current_wmo = 0
    if current is not None:
        current_wmo = msn_description_or_icon_to_wmo_code(current.get("cap") or "", int(current.get("icon") or 0))

    data = WeatherData(
        latitude=latitude,
        longitude=longitude,
        timezone="",  # MSN doesn't return timezone; UI uses local time
        current=None
        if current is None
        else WeatherCurrent(
            time=current.get("created") or "",
            temperature_2m=float(current.get("temp") or 0.0),
            relative_humidity_2m=float(current.get("rh") or 0.0),
            apparent_temperature=float(current.get("feels") or 0.0),
            weather_code=current_wmo,
            wind_speed_10m=float(current.get("windSpd") or 0.0),
            wind_direction_10m=float(current.get("windDir") or 0.0),
            pressure_msl=float(current.get("baro") or 0.0),
            is_day=1 if current.get("daytime") else 0,
        ),
    )

    forecast_days = ((msn.get("forecast") or {}).get("days")) or []
    if forecast_days:
        daily = WeatherDaily()
        for day in forecast_days:
            summary = day.get("daily")
            if not summary:
                continue
            parsed = _parse_iso(summary.get("valid") or "")
            daily.time.append(parsed.strftime("%Y-%m-%d") if parsed else "")

            day_period = summary.get("day") or {}
            daily.weather_code.append(
                msn_description_or_icon_to_wmo_code(summary.get("pvdrCap") or "", int(day_period.get("icon") or 0))
            )
            daily.temperature_2m_max.append(float(summary.get("tempHi") or 0.0))
            daily.temperature_2m_min.append(float(summary.get("tempLo") or 0.0))
            # Precipitation probability: max of day and night halves.
            daily.precipitation_probability_max.append(
                max(
                    float(day_period.get("precip") or 0.0),
                    float((summary.get("night") or {}).get("precip") or 0.0),
                )
            )
            daily.uv_index_max.append(float(summary.get("uv") or 0.0))

            almanac = day.get("almanac") or {}
            sunrise = _parse_iso(almanac.get("sunrise") or "")
            sunset = _parse_iso(almanac.get("sunset") or "")
            daily.sunrise.append(sunrise.strftime("%Y-%m-%dT%H:%M") if sunrise else "")
            daily.sunset.append(sunset.strftime("%Y-%m-%dT%H:%M") if sunset else "")
        if daily.time:
            data.daily = daily

    # Merge hourly rows across all days (dedupe by valid, chronological) so
    # the Today view never goes empty around midnight when MSN drops the
    # current day's rows.
    grouped: dict[str, dict] = {}
    for day in forecast_days:
        for hour in day.get("hourly") or []:
            valid = hour.get("valid") or ""
            if valid and valid not in grouped:
                grouped[valid] = hour
    ordered = sorted(
        grouped.values(),
        key=lambda h: _parse_iso(h.get("valid") or "") or datetime.max.replace(tzinfo=timezone.utc),
    )
    if ordered:
        hourly = WeatherHourly()
        for hour in ordered:
            parsed = _parse_iso(hour.get("valid") or "")
            hourly.time.append(parsed.strftime("%Y-%m-%dT%H:%M") if parsed else hour.get("valid") or "")
            hourly.temperature_2m.append(float(hour.get("temp") or 0.0))
            hourly.precipitation_probability.append(float(hour.get("precip") or 0.0))
            hourly.weather_code.append(
                msn_description_or_icon_to_wmo_code(hour.get("cap") or "", int(hour.get("icon") or 0))
            )
        if hourly.time:
            data.hourly = hourly

    return data


def _location_key(latitude: float, longitude: float) -> str:
    return f"{latitude:.4f},{longitude:.4f}"


class WeatherService:
    """Blocking weather client with source fallback + cache (run off the UI thread)."""

    def __init__(
        self,
        cache_store: Optional[WeatherCacheStore] = None,
        data_source_getter: Optional[Callable[[], str]] = None,
        fetch_json: Optional[Callable[[str], dict]] = None,
    ):
        self._cache_store = cache_store or WeatherCacheStore()
        self._data_source_getter = data_source_getter
        self._fetch_json = fetch_json or self._http_get_json
        self._lock = threading.Lock()
        self._cache_loaded = False
        self._cached_data: Optional[WeatherData] = None
        self._cache_timestamp = datetime.min.replace(tzinfo=timezone.utc)
        self._cache_location_key = ""
        self._cache_source_key = ""

    # ---- HTTP -------------------------------------------------------------------

    @staticmethod
    def _http_get_json(url: str) -> dict:
        import requests

        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()

    def _current_data_source(self) -> str:
        getter = self._data_source_getter
        if getter is None:
            return DATA_SOURCE_MSN
        try:
            return getter() or DATA_SOURCE_MSN
        except Exception:
            return DATA_SOURCE_MSN

    # ---- geocoding -----------------------------------------------------------------

    def search_city(self, query: str, language: str = "zh") -> List[WeatherGeocodingItem]:
        if not query or not query.strip():
            return []
        try:
            api_language = normalize_geocoding_language(language)
            url = f"{GEOCODING_BASE_URL}?name={quote(query)}&count=10&language={api_language}&format=json"
            document = self._fetch_json(url)
            results = document.get("results") or []
            return [WeatherGeocodingItem.from_dict(item) for item in results if isinstance(item, dict)]
        except Exception:
            return []

    def resolve_city(self, city_name: str, language: str = "zh") -> Optional[WeatherGeocodingItem]:
        results = self.search_city(city_name, language)
        return results[0] if results else None

    # ---- forecast ------------------------------------------------------------------

    def load_cache_state(self) -> WeatherCacheState:
        with self._lock:
            state = self._cache_store.load()
            if not self._cache_loaded:
                self._cache_loaded = True
                forecast = state.lastForecast
                if forecast is not None and forecast.is_valid and forecast.data is not None:
                    cached = forecast.data
                    cached.locationName = forecast.locationName
                    cached.isStale = False
                    cached.isFallback = forecast.requestedSource != forecast.actualSource
                    self._cached_data = cached
                    self._cache_timestamp = _parse_iso(forecast.fetchedAtUtc) or datetime.min.replace(
                        tzinfo=timezone.utc
                    )
                    self._cache_location_key = _location_key(forecast.latitude, forecast.longitude)
                    self._cache_source_key = forecast.requestedSource
            return state

    def save_resolved_location(self, latitude: float, longitude: float, name: str, resolved_at_utc: datetime) -> bool:
        from ..models.weather import WeatherCachedLocation

        return self._cache_store.save_location(
            WeatherCachedLocation(
                latitude=latitude,
                longitude=longitude,
                name=name,
                resolvedAtUtc=resolved_at_utc.astimezone(timezone.utc).isoformat(),
            )
        )

    def get_weather(
        self,
        latitude: float,
        longitude: float,
        location_name: str = "",
        force_refresh: bool = False,
        cache_duration: Optional[timedelta] = None,
        data_source: Optional[str] = None,
    ) -> Optional[WeatherData]:
        self.load_cache_state()

        cache_key = _location_key(latitude, longitude)
        source_key = data_source or self._current_data_source()
        effective_duration = cache_duration if cache_duration is not None else DEFAULT_CACHE_DURATION
        if effective_duration < timedelta(0):
            effective_duration = timedelta(0)

        with self._lock:
            if (
                not force_refresh
                and self._cached_data is not None
                and self._cache_location_key == cache_key
                and self._cache_source_key == source_key
                and datetime.now(timezone.utc) - self._cache_timestamp < effective_duration
            ):
                self._cached_data.locationName = location_name
                return self._cached_data

        actual_source = source_key
        data = self._fetch_from_source(source_key, latitude, longitude)
        if data is None:
            fallback_source = DATA_SOURCE_OPEN_METEO if source_key == DATA_SOURCE_MSN else DATA_SOURCE_MSN
            actual_source = fallback_source
            data = self._fetch_from_source(fallback_source, latitude, longitude)
            if data is not None:
                data.isFallback = True

        with self._lock:
            if data is not None:
                data.locationName = location_name
                data.isStale = False
                now = datetime.now(timezone.utc)
                self._cached_data = data
                self._cache_timestamp = now
                self._cache_location_key = cache_key
                self._cache_source_key = source_key
                self._cache_store.save_forecast(
                    WeatherCachedForecast(
                        latitude=latitude,
                        longitude=longitude,
                        locationName=location_name,
                        requestedSource=source_key,
                        actualSource=actual_source,
                        fetchedAtUtc=now.isoformat(),
                        data=data,
                    )
                )
            elif self._cache_location_key == cache_key and self._cached_data is not None:
                # All sources failed: stale cache for the same location.
                self._cached_data.isStale = True
                return self._cached_data
        return data

    # ---- source dispatch ------------------------------------------------------------

    def _fetch_from_source(self, source: str, latitude: float, longitude: float) -> Optional[WeatherData]:
        try:
            if source == DATA_SOURCE_MSN:
                return self._fetch_msn_weather(latitude, longitude)
            return self._fetch_open_meteo_weather(latitude, longitude)
        except Exception:
            return None

    def _fetch_open_meteo_weather(self, latitude: float, longitude: float) -> Optional[WeatherData]:
        document = self._fetch_json(build_open_meteo_forecast_url(latitude, longitude))
        if not isinstance(document, dict):
            return None
        return WeatherData.from_dict(document)

    def _fetch_msn_weather(self, latitude: float, longitude: float) -> Optional[WeatherData]:
        document = self._fetch_json(build_msn_weather_url(latitude, longitude))
        if not isinstance(document, dict):
            return None
        body = extract_msn_weather_body(document)
        if body is None:
            return None
        return convert_msn_to_weather_data(body, latitude, longitude)
