"""Weather preference policies (ports of WeatherSettingsPolicy.cs and
WeatherWidgetViewModeSettings.cs). Persisted, local-only preferences — no
network or location resolution here.
"""

from __future__ import annotations

import math

TEMPERATURE_UNIT_CELSIUS = "Celsius"
TEMPERATURE_UNIT_FAHRENHEIT = "Fahrenheit"
WIND_SPEED_UNIT_KMH = "kmh"
WIND_SPEED_UNIT_MS = "ms"
WIND_SPEED_UNIT_MPH = "mph"
DEFAULT_VIEW_TODAY = "Today"
DEFAULT_VIEW_WEEK = "Week"
SKIN_STANDARD = "Standard"
SKIN_RICH = "Rich"

REFRESH_MIN_MINUTES = 15
REFRESH_MAX_MINUTES = 180


class WeatherDisplayOption:
    FORECAST = "Forecast"
    SUNRISE = "Sunrise"
    UV_INDEX = "UvIndex"
    PRECIPITATION = "Precipitation"
    HUMIDITY = "Humidity"
    WIND = "Wind"
    PRESSURE = "Pressure"


def set_auto_location(settings, enabled: bool) -> None:
    settings.weatherAutoLocation = enabled


def try_set_manual_location(settings, city_name: str, latitude: float, longitude: float) -> bool:
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    ):
        return False
    settings.weatherAutoLocation = False
    settings.weatherCityName = city_name
    settings.weatherLatitude = latitude
    settings.weatherLongitude = longitude
    return True


def set_temperature_unit(settings, value: str) -> None:
    settings.weatherTemperatureUnit = (
        TEMPERATURE_UNIT_FAHRENHEIT if value == TEMPERATURE_UNIT_FAHRENHEIT else TEMPERATURE_UNIT_CELSIUS
    )


def set_wind_speed_unit(settings, value: str) -> None:
    settings.weatherWindSpeedUnit = value if value in (WIND_SPEED_UNIT_MS, WIND_SPEED_UNIT_MPH) else WIND_SPEED_UNIT_KMH


def set_default_view(settings, value: str) -> None:
    settings.weatherDefaultView = DEFAULT_VIEW_WEEK if value == DEFAULT_VIEW_WEEK else DEFAULT_VIEW_TODAY


def set_skin(settings, value: str) -> None:
    settings.weatherSkin = SKIN_RICH if value == SKIN_RICH else SKIN_STANDARD


def set_refresh_interval(settings, minutes: int) -> None:
    settings.weatherRefreshIntervalMinutes = min(max(minutes, REFRESH_MIN_MINUTES), REFRESH_MAX_MINUTES)


_DISPLAY_OPTION_FIELDS = {
    WeatherDisplayOption.FORECAST: "weatherShowForecast",
    WeatherDisplayOption.SUNRISE: "weatherShowSunrise",
    WeatherDisplayOption.UV_INDEX: "weatherShowUvIndex",
    WeatherDisplayOption.PRECIPITATION: "weatherShowPrecipitation",
    WeatherDisplayOption.HUMIDITY: "weatherShowHumidity",
    WeatherDisplayOption.WIND: "weatherShowWind",
    WeatherDisplayOption.PRESSURE: "weatherShowPressure",
}


def set_display_option(settings, option: str, enabled: bool) -> None:
    field = _DISPLAY_OPTION_FIELDS.get(option)
    if field is None:
        raise ValueError(f"unknown weather display option: {option}")
    setattr(settings, field, enabled)


# ── per-widget Day/Week view override (WeatherWidgetViewModeSettings) ──

METADATA_KEY = "Weather.ViewMode"
DAY_VALUE = "Day"
WEEK_VALUE = "Week"


def try_get_week_view(config) -> bool | None:
    """True/False when the widget carries an explicit override, else None."""
    if config.metadata.get(METADATA_KEY) in (DAY_VALUE, WEEK_VALUE):
        return config.metadata[METADATA_KEY] == WEEK_VALUE
    return None


def set_week_view(config, use_week_view: bool) -> bool:
    """Returns True when the stored value changed."""
    value = WEEK_VALUE if use_week_view else DAY_VALUE
    if config.metadata.get(METADATA_KEY) == value:
        return False
    config.metadata[METADATA_KEY] = value
    return True
