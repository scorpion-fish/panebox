"""Weather cache store (port of Services/WeatherCacheStore.cs).

Persists the last resolved location and last forecast into
``data/weather-cache.json`` via the resilient JSON store, with the same
schema-version normalization and 0.01° IsSameLocation tolerance.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..constants import DATA_ROOT
from ..models.weather import WeatherCacheState, WeatherCachedForecast, WeatherCachedLocation
from .resilient_json_store import ResilientJsonStore

FILE_NAME = "weather-cache.json"
CURRENT_SCHEMA_VERSION = 1
COORDINATE_TOLERANCE = 0.01

# Path → store registry (the C# per-path semaphore gates concurrent saves).
_stores: dict[str, "WeatherCacheStore"] = {}
_stores_lock = threading.Lock()


def is_same_location(
    first_latitude: float,
    first_longitude: float,
    second_latitude: float,
    second_longitude: float,
) -> bool:
    return (
        abs(first_latitude - second_latitude) <= COORDINATE_TOLERANCE
        and abs(first_longitude - second_longitude) <= COORDINATE_TOLERANCE
    )


class WeatherCacheStore:
    def __init__(self, store_path: str | Path | None = None):
        self.store_path = str(Path(store_path or Path(DATA_ROOT) / "data" / FILE_NAME))
        self._store = ResilientJsonStore(Path(self.store_path))
        self._lock = threading.Lock()

    @classmethod
    def for_path(cls, store_path: str | Path) -> "WeatherCacheStore":
        key = str(Path(store_path))
        with _stores_lock:
            store = _stores.get(key)
            if store is None:
                store = _stores[key] = cls(key)
            return store

    # ---- load / save -----------------------------------------------------------

    def load(self) -> WeatherCacheState:
        with self._lock:
            try:
                result = self._store.load()
                state = WeatherCacheState.from_dict(result.data)
            except Exception:
                return WeatherCacheState()
            return self._normalize(state)

    def save_location(self, location: WeatherCachedLocation) -> bool:
        if not location.is_valid:
            return False
        return self._update(lambda state: setattr(state, "lastLocation", location))

    def save_forecast(self, forecast: WeatherCachedForecast) -> bool:
        if not forecast.is_valid:
            return False
        return self._update(lambda state: setattr(state, "lastForecast", forecast))

    def _update(self, update) -> bool:
        with self._lock:
            try:
                state = self._load_core()
                update(state)
                state.schemaVersion = CURRENT_SCHEMA_VERSION
                self._store.save(state.to_dict())
                return True
            except Exception:
                return False

    def _load_core(self) -> WeatherCacheState:
        result = self._store.load()
        return WeatherCacheState.from_dict(result.data)

    @staticmethod
    def _normalize(state: WeatherCacheState) -> WeatherCacheState:
        if not 0 < state.schemaVersion <= CURRENT_SCHEMA_VERSION:
            return WeatherCacheState()
        if state.lastLocation is not None and not state.lastLocation.is_valid:
            state.lastLocation = None
        if state.lastForecast is not None and not state.lastForecast.is_valid:
            state.lastForecast = None
        state.schemaVersion = CURRENT_SCHEMA_VERSION
        return state
