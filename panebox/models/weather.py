"""Weather payload models (port of Models/WeatherData.cs + the cache models
from WeatherCacheStore.cs). Wire format matches the C# JSON names exactly so
Open-Meteo responses and the on-disk cache are interchangeable with PaneBox.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import List, Optional

from .base import JsonModel

DATA_SOURCE_MSN = "MSN"
DATA_SOURCE_OPEN_METEO = "OpenMeteo"


@dataclasses.dataclass
class WeatherCurrent(JsonModel):
    time: str = ""
    temperature_2m: float = 0.0
    relative_humidity_2m: float = 0.0
    apparent_temperature: float = 0.0
    weather_code: int = 0
    wind_speed_10m: float = 0.0
    wind_direction_10m: float = 0.0
    pressure_msl: float = 0.0
    is_day: int = 1

    @property
    def temperature(self) -> float:
        return self.temperature_2m

    @property
    def humidity(self) -> float:
        return self.relative_humidity_2m

    @property
    def wind_speed(self) -> float:
        return self.wind_speed_10m

    @property
    def wind_direction(self) -> float:
        return self.wind_direction_10m

    @property
    def pressure(self) -> float:
        return self.pressure_msl


@dataclasses.dataclass
class WeatherDaily(JsonModel):
    time: List[str] = dataclasses.field(default_factory=list)
    weather_code: List[int] = dataclasses.field(default_factory=list)
    temperature_2m_max: List[float] = dataclasses.field(default_factory=list)
    temperature_2m_min: List[float] = dataclasses.field(default_factory=list)
    precipitation_probability_max: List[float] = dataclasses.field(default_factory=list)
    sunrise: List[str] = dataclasses.field(default_factory=list)
    sunset: List[str] = dataclasses.field(default_factory=list)
    uv_index_max: List[float] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class WeatherHourly(JsonModel):
    time: List[str] = dataclasses.field(default_factory=list)
    temperature_2m: List[float] = dataclasses.field(default_factory=list)
    precipitation_probability: List[float] = dataclasses.field(default_factory=list)
    weather_code: List[int] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class WeatherData(JsonModel):
    latitude: float = 0.0
    longitude: float = 0.0
    timezone: str = ""
    current: Optional[WeatherCurrent] = None
    daily: Optional[WeatherDaily] = None
    hourly: Optional[WeatherHourly] = None

    # Runtime-only (JsonIgnore in C#), never serialized to the API shape.
    locationName: str = ""
    isStale: bool = False
    isFallback: bool = False

    NESTED = {
        "current": (WeatherCurrent, "obj"),
        "daily": (WeatherDaily, "obj"),
        "hourly": (WeatherHourly, "obj"),
    }
    RUNTIME_FIELDS = ("locationName", "isStale", "isFallback")

    def to_dict(self) -> dict:
        document = super().to_dict()
        for field in self.RUNTIME_FIELDS:  # JsonIgnore equivalents
            document.pop(field, None)
        return document

    def to_cache_dict(self) -> dict:
        document = self.to_dict()
        document["locationName"] = self.locationName
        return document

    @classmethod
    def from_cache_dict(cls, document: dict) -> "WeatherData":
        data = cls.from_dict(document)
        data.locationName = document.get("locationName") or ""
        return data


@dataclasses.dataclass
class WeatherGeocodingItem(JsonModel):
    id: int = 0
    name: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    country: str = ""
    admin1: str = ""

    @property
    def displayName(self) -> str:
        if not (self.admin1 or "").strip():
            return f"{self.name}, {self.country}"
        return f"{self.name}, {self.admin1}, {self.country}"


@dataclasses.dataclass
class WeatherCachedLocation(JsonModel):
    latitude: float = 0.0
    longitude: float = 0.0
    name: str = ""
    resolvedAtUtc: str = ""

    @property
    def is_valid(self) -> bool:
        try:
            resolved = datetime.fromisoformat(self.resolvedAtUtc)
        except ValueError:
            return False
        return (
            -90.0 <= self.latitude <= 90.0
            and -180.0 <= self.longitude <= 180.0
            and resolved != datetime.min.replace(tzinfo=timezone.utc)
        )


@dataclasses.dataclass
class WeatherCachedForecast(JsonModel):
    latitude: float = 0.0
    longitude: float = 0.0
    locationName: str = ""
    requestedSource: str = DATA_SOURCE_MSN
    actualSource: str = DATA_SOURCE_MSN
    fetchedAtUtc: str = ""
    data: Optional[WeatherData] = None

    NESTED = {"data": (WeatherData, "obj")}

    @property
    def is_valid(self) -> bool:
        try:
            fetched = datetime.fromisoformat(self.fetchedAtUtc)
        except ValueError:
            return False
        return (
            -90.0 <= self.latitude <= 90.0
            and -180.0 <= self.longitude <= 180.0
            and fetched != datetime.min.replace(tzinfo=timezone.utc)
            and self.data is not None
            and self.data.current is not None
        )


@dataclasses.dataclass
class WeatherCacheState(JsonModel):
    schemaVersion: int = 1
    lastLocation: Optional[WeatherCachedLocation] = None
    lastForecast: Optional[WeatherCachedForecast] = None

    NESTED = {
        "lastLocation": (WeatherCachedLocation, "obj"),
        "lastForecast": (WeatherCachedForecast, "obj"),
    }

    @property
    def normalized(self) -> "WeatherCacheState":
        if not 0 < self.schemaVersion <= 1:
            return WeatherCacheState()
        if self.lastLocation is not None and not self.lastLocation.is_valid:
            self.lastLocation = None
        if self.lastForecast is not None and not self.lastForecast.is_valid:
            self.lastForecast = None
        self.schemaVersion = 1
        return self
