"""Pure weather presentation logic (port of the computed halves of
WeatherWidgetViewModel + RefreshAndLayout.DetermineLayoutMode +
DataProcessing formatting). No GTK imports — unit-tested directly, consumed
by views.weather_widget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from ..i18n import current_language, fmt, t
from ..models.weather import WeatherData
from .weather_codes import WeatherCondition, get_condition, get_description, get_emoji
from .weather_settings_policy import (
    TEMPERATURE_UNIT_FAHRENHEIT,
    WIND_SPEED_UNIT_MS,
    WIND_SPEED_UNIT_MPH,
    SKIN_RICH,
)

DEFAULT_TEXT_SIZE = 11.5
MIN_TEXT_SIZE = 10.0
MAX_TEXT_SIZE = 16.0
MIN_SYSTEM_TEXT_SCALE = 1.0

Rgb = Tuple[int, int, int]

# Rich gradient stops (top, bottom); every pair keeps ≥4.5:1 against white.
_RICH_GRADIENTS = {
    ("Clear", True): ((0x1F, 0x5F, 0x9B), (0x2D, 0x72, 0x97)),
    ("Clear", False): ((0x10, 0x19, 0x2E), (0x29, 0x3D, 0x69)),
    ("Cloudy", None): ((0x3C, 0x52, 0x66), (0x52, 0x68, 0x78)),
    ("Rain", None): ((0x15, 0x3A, 0x5A), (0x35, 0x6B, 0x88)),
    ("Drizzle", None): ((0x15, 0x3A, 0x5A), (0x35, 0x6B, 0x88)),
    ("Snow", None): ((0x4B, 0x69, 0x7A), (0x5C, 0x72, 0x7E)),
    ("Thunderstorm", None): ((0x25, 0x21, 0x3E), (0x51, 0x4B, 0x74)),
    ("Fog", None): ((0x53, 0x62, 0x6F), (0x60, 0x71, 0x7E)),
}
_RICH_GRADIENT_DEFAULT: Tuple[Rgb, Rgb] = ((0x28, 0x5F, 0x8E), (0x3C, 0x76, 0x94))

# "ddd" abbreviations per culture (ParseDateToDayLabel), Monday-first.
WEEKDAY_ABBREVIATIONS = {
    "zh-CN": ("周一", "周二", "周三", "周四", "周五", "周六", "周日"),
    "zh-TW": ("週一", "週二", "週三", "週四", "週五", "週六", "週日"),
    "ja-JP": ("月", "火", "水", "木", "金", "土", "日"),
    "de-DE": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"),
    "pt-BR": ("seg", "ter", "qua", "qui", "sex", "sáb", "dom"),
    "hi-IN": ("सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि", "रवि"),
    "es-ES": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
    "fr-FR": ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."),
    "ar-SA": ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"),
    "bn-BD": ("সোম", "মঙ্গল", "বুধ", "বৃহঃ", "শুক্র", "শনি", "রবি"),
    "ru-RU": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}


# ── layout mode (RefreshAndLayout.DetermineLayoutMode) ────────────────────


def normalize_text_size(value: float) -> float:
    return MIN_TEXT_SIZE if value < MIN_TEXT_SIZE else (MAX_TEXT_SIZE if value > MAX_TEXT_SIZE else value)


def resolve_typography_scale(text_size: float, system_text_scale: float = MIN_SYSTEM_TEXT_SCALE) -> float:
    appearance_scale = max(1.0, normalize_text_size(text_size) / DEFAULT_TEXT_SIZE)
    return system_text_scale * appearance_scale


def determine_layout_mode(
    width: float,
    height: float,
    current_layout: str,
    text_size: float = DEFAULT_TEXT_SIZE,
    system_text_scale: float = MIN_SYSTEM_TEXT_SCALE,
) -> str:
    """Mini / Compact / Expanded with hysteresis (breakpoints scale with type)."""
    typography_delta = resolve_typography_scale(text_size, system_text_scale) - 1.0
    mini_upgrade_w = 190 + 42 * typography_delta
    mini_upgrade_h = 145 + 58 * typography_delta
    mini_downgrade_w = 178 + 38 * typography_delta
    mini_downgrade_h = 134 + 52 * typography_delta

    expanded_upgrade_w = 300 + 110 * typography_delta
    expanded_upgrade_h = 260 + 150 * typography_delta
    expanded_downgrade_w = 280 + 96 * typography_delta
    expanded_downgrade_h = 240 + 132 * typography_delta

    # Mini is always forced for very small sizes regardless of hysteresis.
    if width <= mini_downgrade_w or height <= mini_downgrade_h:
        return "Mini"

    if current_layout == "Mini":
        if width >= expanded_upgrade_w and height >= expanded_upgrade_h:
            return "Expanded"
        if width >= mini_upgrade_w and height >= mini_upgrade_h:
            return "Compact"
        return "Mini"

    if current_layout == "Compact":
        if width >= expanded_upgrade_w and height >= expanded_upgrade_h:
            return "Expanded"
        if width <= mini_downgrade_w or height <= mini_downgrade_h:
            return "Mini"
        return "Compact"

    if current_layout == "Expanded":
        if width <= expanded_downgrade_w or height <= expanded_downgrade_h:
            if width <= mini_downgrade_w or height <= mini_downgrade_h:
                return "Mini"
            return "Compact"
        return "Expanded"

    # First-time default: use upgrade thresholds.
    if width >= expanded_upgrade_w and height >= expanded_upgrade_h:
        return "Expanded"
    if width >= mini_upgrade_w and height >= mini_upgrade_h:
        return "Compact"
    return "Mini"


# ── rich skin colors ─────────────────────────────────────────────────────


def _to_linear(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(color: Rgb) -> float:
    return 0.2126 * _to_linear(color[0]) + 0.7152 * _to_linear(color[1]) + 0.0722 * _to_linear(color[2])


def should_use_light_text(top: Rgb, bottom: Rgb) -> bool:
    top_luminance = relative_luminance(top)
    bottom_luminance = relative_luminance(bottom)
    minimum_light_contrast = min(1.05 / (top_luminance + 0.05), 1.05 / (bottom_luminance + 0.05))
    minimum_dark_contrast = min((top_luminance + 0.05) / 0.05, (bottom_luminance + 0.05) / 0.05)
    return minimum_light_contrast >= minimum_dark_contrast


def rich_gradient(condition: str, is_day: bool) -> Tuple[Rgb, Rgb]:
    if condition == WeatherCondition.CLEAR:
        return _RICH_GRADIENTS[(WeatherCondition.CLEAR, bool(is_day))]
    return _RICH_GRADIENTS.get((condition, None), _RICH_GRADIENT_DEFAULT)


def css_color(color: Rgb) -> str:
    return f"rgb({color[0]},{color[1]},{color[2]})"


# ── formatting ───────────────────────────────────────────────────────────


def format_temperature(celsius: float, unit: str) -> str:
    if unit == TEMPERATURE_UNIT_FAHRENHEIT:
        value = celsius * 9 / 5 + 32
        unit_glyph = "°F"
    else:
        value = celsius
        unit_glyph = "°C"
    return f"{round(value)}{unit_glyph}"


def format_wind_speed(kmh: float, unit: str) -> str:
    if unit == WIND_SPEED_UNIT_MS:
        value, suffix = kmh / 3.6, "m/s"
    elif unit == WIND_SPEED_UNIT_MPH:
        value, suffix = kmh / 1.609, "mph"
    else:
        value, suffix = kmh, "km/h"
    # .NET double formatting drops the trailing ".0" (10.0 → "10"); :g matches.
    return f"{round(value, 1):g} {suffix}"


_WIND_KEYS = (
    "Weather.Wind.N",
    "Weather.Wind.NE",
    "Weather.Wind.E",
    "Weather.Wind.SE",
    "Weather.Wind.S",
    "Weather.Wind.SW",
    "Weather.Wind.W",
    "Weather.Wind.NW",
)


def wind_direction_text(direction: float) -> str:
    return t(_WIND_KEYS[round(direction / 45) % 8])


def _parse_iso(iso_time: str) -> Optional[datetime]:
    if not iso_time:
        return None
    text = iso_time.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed


def parse_date_to_day_label(date_str: str, language: str) -> str:
    parsed = _parse_iso(date_str)
    if parsed is None:
        return date_str
    table = WEEKDAY_ABBREVIATIONS.get(language)
    if table is not None:
        return table[parsed.weekday()]
    return WEEKDAY_ABBREVIATIONS["en"][parsed.weekday()]


def format_hour_label(iso_time: str) -> str:
    parsed = _parse_iso(iso_time)
    if parsed is None:
        return ""
    return "0:00" if parsed.hour == 0 else f"{parsed.hour}:00"


def format_time(iso_time: str) -> str:
    parsed = _parse_iso(iso_time)
    return parsed.strftime("%H:%M") if parsed is not None else ""


def is_daytime_hour(iso_time: str) -> bool:
    parsed = _parse_iso(iso_time)
    return True if parsed is None else 6 <= parsed.hour < 19


def find_current_hour_index(times: List[str]) -> int:
    if not times:
        return 0
    now = datetime.now().strftime("%Y-%m-%dT%H")
    for index, time in enumerate(times):
        if time.lower().startswith(now.lower()):
            return index
    return 0


def refine_location_name(lat: float, lon: float, original_name: str, language: str) -> str:
    """Normalize IP city names against the local database (≤80 km)."""
    try:
        from .city_search import get_nearest_city_name

        nearest = get_nearest_city_name(lat, lon, language)
        if nearest:
            return nearest
    except Exception:
        pass
    return original_name


FALLBACK_LOCATION = (39.9042, 116.4074)


def fallback_location_name(api_language: str) -> str:
    return {"zh": "北京", "ja": "北京", "de": "Peking", "pt": "Pequim"}.get(api_language, "Beijing")


# ── display model ────────────────────────────────────────────────────────


@dataclass
class WeatherDayRow:
    day_label: str
    emoji: str
    description: str
    temp_max_text: str
    temp_min_text: str
    precipitation_text: str
    bar_offset: float
    bar_width: float


@dataclass
class WeatherHourRow:
    hour_label: str
    temperature_text: str
    precipitation_text: str
    emoji: str
    is_daytime: bool
    is_current_hour: bool


@dataclass
class WeatherDisplay:
    has_data: bool = False
    is_day: bool = True
    current_emoji: str = ""
    current_description: str = ""
    current_temperature_text: str = ""
    apparent_temperature_text: str = ""
    humidity_text: str = ""
    humidity_value_text: str = ""
    wind_text: str = ""
    wind_value_text: str = ""
    pressure_text: str = ""
    pressure_value_text: str = ""
    uv_index_text: str = ""
    uv_index_value_text: str = ""
    precipitation_text: str = ""
    precipitation_value_text: str = ""
    sunrise_text: str = ""
    sunset_text: str = ""
    location_display: str = ""
    is_stale: bool = False
    is_fallback_data: bool = False
    condition: str = WeatherCondition.UNKNOWN
    daily: List[WeatherDayRow] = field(default_factory=list)
    hourly: List[WeatherHourRow] = field(default_factory=list)
    rich_top: Rgb = _RICH_GRADIENT_DEFAULT[0]
    rich_bottom: Rgb = _RICH_GRADIENT_DEFAULT[1]
    rich_light_text: bool = True
    animation: str = ""  # rain | snow | thunder | clear | ""


def apply_weather_data(
    data: WeatherData,
    *,
    temperature_unit: str,
    wind_speed_unit: str,
    skin: str,
    fallback_location_name_: str,
    language: Optional[str] = None,
    now: Optional[datetime] = None,
) -> WeatherDisplay:
    """Port of ApplyWeatherData + UpdateRichSkinColors (pure computation)."""
    display = WeatherDisplay()
    if data.current is None:
        return display

    language = language or current_language() or "zh-CN"
    current = data.current

    display.has_data = True
    display.is_day = current.is_day == 1
    display.condition = get_condition(current.weather_code)
    display.current_emoji = get_emoji(current.weather_code, display.is_day)
    display.current_description = get_description(current.weather_code, language)
    display.current_temperature_text = format_temperature(current.temperature, temperature_unit)
    display.apparent_temperature_text = fmt(
        "Weather.FeelsLike", format_temperature(current.apparent_temperature, temperature_unit)
    )
    display.humidity_value_text = f"{int(current.humidity)}%"
    display.humidity_text = fmt("Weather.HumidityLabel", display.humidity_value_text)
    display.wind_value_text = format_wind_speed(current.wind_speed, wind_speed_unit)
    display.wind_text = f"{display.wind_value_text} {wind_direction_text(current.wind_direction)}"
    display.pressure_value_text = f"{int(current.pressure)} hPa"
    display.pressure_text = fmt("Weather.PressureLabel", display.pressure_value_text)
    display.location_display = data.locationName.strip() or fallback_location_name_
    display.is_stale = data.isStale
    display.is_fallback_data = data.isFallback

    if data.daily is not None:
        daily = data.daily
        count = min(len(daily.time), 7)
        week_min = float("inf")
        week_max = float("-inf")
        for i in range(count):
            t_max = daily.temperature_2m_max[i] if i < len(daily.temperature_2m_max) else 0.0
            t_min = daily.temperature_2m_min[i] if i < len(daily.temperature_2m_min) else 0.0
            week_max = max(week_max, t_max)
            week_min = min(week_min, t_min)
        week_range = max(week_max - week_min, 1.0)

        for i in range(count):
            date_str = daily.time[i] if i < len(daily.time) else ""
            wmo_code = daily.weather_code[i] if i < len(daily.weather_code) else 0
            temp_max = daily.temperature_2m_max[i] if i < len(daily.temperature_2m_max) else 0.0
            temp_min = daily.temperature_2m_min[i] if i < len(daily.temperature_2m_min) else 0.0
            precip = daily.precipitation_probability_max[i] if i < len(daily.precipitation_probability_max) else 0.0
            if i == 0:
                day_label = t("Weather.Today")
            elif i == 1:
                day_label = t("Weather.Tomorrow")
            else:
                day_label = parse_date_to_day_label(date_str, language)

            bar_offset = min(max((temp_min - week_min) / week_range, 0.0), 1.0)
            bar_width = min(max((temp_max - temp_min) / week_range, 0.04), 1.0)
            display.daily.append(
                WeatherDayRow(
                    day_label=day_label,
                    emoji=get_emoji(wmo_code, is_day=True),
                    description=get_description(wmo_code, language),
                    temp_max_text=format_temperature(temp_max, temperature_unit),
                    temp_min_text=format_temperature(temp_min, temperature_unit),
                    precipitation_text=f"{int(precip)}%",
                    bar_offset=bar_offset,
                    bar_width=bar_width,
                )
            )

        if daily.uv_index_max:
            display.uv_index_value_text = f"{daily.uv_index_max[0]:g}"
            display.uv_index_text = fmt("Weather.UVLabel", display.uv_index_value_text)
        if daily.precipitation_probability_max:
            display.precipitation_value_text = f"{int(daily.precipitation_probability_max[0])}%"
            display.precipitation_text = fmt("Weather.PrecipChance", display.precipitation_value_text)
        if daily.sunrise:
            display.sunrise_text = format_time(daily.sunrise[0])
        if daily.sunset:
            display.sunset_text = format_time(daily.sunset[0])

    if data.hourly is not None:
        hourly = data.hourly
        start_index = find_current_hour_index(hourly.time)
        count = min(24, len(hourly.time) - start_index)
        for i in range(count):
            index = start_index + i
            if index >= len(hourly.time):
                break
            time_str = hourly.time[index]
            temp = hourly.temperature_2m[index] if index < len(hourly.temperature_2m) else 0.0
            precip = hourly.precipitation_probability[index] if index < len(hourly.precipitation_probability) else 0.0
            wmo_code = hourly.weather_code[index] if index < len(hourly.weather_code) else 0
            daytime = is_daytime_hour(time_str)
            display.hourly.append(
                WeatherHourRow(
                    hour_label=format_hour_label(time_str),
                    temperature_text=format_temperature(temp, temperature_unit),
                    precipitation_text=f"{int(precip)}%" if precip > 0 else "",
                    emoji=get_emoji(wmo_code, daytime),
                    is_daytime=daytime,
                    is_current_hour=i == 0,
                )
            )

    top, bottom = rich_gradient(display.condition, display.is_day)
    display.rich_top = top
    display.rich_bottom = bottom
    display.rich_light_text = should_use_light_text(top, bottom)

    if skin == SKIN_RICH:
        if display.condition in (WeatherCondition.RAIN, WeatherCondition.DRIZZLE):
            display.animation = "rain"
        elif display.condition == WeatherCondition.SNOW:
            display.animation = "snow"
        elif display.condition == WeatherCondition.THUNDERSTORM:
            display.animation = "thunder"
        elif display.condition == WeatherCondition.CLEAR:
            display.animation = "clear"

    return display
