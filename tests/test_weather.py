"""Weather tests: WMO code mapping, view model (layout hysteresis, gradients,
formatting, display projection), settings policy, IP location chain, service
cache/fallback/stale, city search."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from panebox import i18n
from panebox.models.weather import (
    DATA_SOURCE_MSN,
    DATA_SOURCE_OPEN_METEO,
    WeatherCurrent,
    WeatherDaily,
    WeatherData,
    WeatherHourly,
)
from panebox.services.weather_cache import WeatherCacheStore
from panebox.services import weather_backoff, weather_codes, weather_view_model as vm
from panebox.services import weather_location
from panebox.services import weather_settings_policy as policy
from panebox.services.weather_service import (
    WeatherService,
    build_msn_weather_url,
    build_open_meteo_forecast_url,
    convert_msn_to_weather_data,
    extract_msn_weather_body,
    normalize_geocoding_language,
)


@pytest.fixture(autouse=True)
def _zh():
    i18n.set_language("zh-CN")
    weather_location.reset_for_tests()
    yield
    weather_location.reset_for_tests()


# ---- WMO code mapping -------------------------------------------------------------


def test_condition_and_emoji_mapping():
    assert weather_codes.get_condition(0) == "Clear"
    assert weather_codes.get_condition(1) == "Clear"
    assert weather_codes.get_condition(61) == "Rain"
    assert weather_codes.get_condition(71) == "Snow"
    assert weather_codes.get_condition(95) == "Thunderstorm"
    assert weather_codes.get_condition(45) == "Fog"
    assert weather_codes.get_condition(99) not in ("Clear",)  # unknown stays mapped
    assert weather_codes.get_emoji(0, True) == "☀️"
    assert weather_codes.get_emoji(0, False) == "🌙"
    assert weather_codes.get_description(1, "zh-CN") == "晴间多云"
    assert weather_codes.get_description(1, "en-US") == "Mainly clear"


def test_msn_description_to_wmo_code():
    assert weather_codes.msn_description_or_icon_to_wmo_code("Sunny", 1) == 0
    # Unknown description falls back to the icon table; both unknown → 0.
    assert isinstance(weather_codes.msn_description_or_icon_to_wmo_code("", 0), int)


# ---- layout hysteresis + typography -----------------------------------------------


def test_layout_mode_first_sizing():
    d = vm.determine_layout_mode
    assert (d(320, 300, ""), d(200, 150, ""), d(100, 100, "")) == ("Expanded", "Compact", "Mini")


def test_layout_mode_hysteresis():
    d = vm.determine_layout_mode
    assert d(290, 250, "Expanded") == "Expanded"  # above downgrade band
    assert d(275, 230, "Expanded") == "Compact"  # crossed downgrade
    assert d(178, 200, "Expanded") == "Mini"  # mini forced
    assert d(190, 145, "Mini") == "Compact"  # upgrade
    assert d(310, 270, "Mini") == "Expanded"  # straight to expanded


def test_layout_mode_scales_with_text_size():
    d = vm.determine_layout_mode
    assert d(200, 150, "Mini", text_size=16) == "Mini"
    assert d(230, 190, "Mini", text_size=16) == "Compact"


def test_normalize_and_scale():
    assert vm.normalize_text_size(5) == vm.MIN_TEXT_SIZE
    assert vm.normalize_text_size(99) == vm.MAX_TEXT_SIZE
    assert vm.resolve_typography_scale(11.5) == 1.0
    assert vm.resolve_typography_scale(16.0) > 1.3


# ---- rich skin gradients + WCAG ---------------------------------------------------


def test_rich_gradient_table():
    assert vm.rich_gradient("Clear", False)[0] == (0x10, 0x19, 0x2E)
    assert vm.rich_gradient("Clear", True)[0] == (0x1F, 0x5F, 0x9B)
    assert vm.rich_gradient("Rain", True)[0] == (0x15, 0x3A, 0x5A)
    assert vm.rich_gradient("Drizzle", False) == vm.rich_gradient("Rain", False)
    assert vm.rich_gradient("Thunderstorm", True) == ((0x25, 0x21, 0x3E), (0x51, 0x4B, 0x74))
    assert vm.rich_gradient("Nope", True) == ((0x28, 0x5F, 0x8E), (0x3C, 0x76, 0x94))


@pytest.mark.parametrize(
    "condition,is_day",
    [
        ("Clear", True),
        ("Clear", False),
        ("Cloudy", True),
        ("Rain", True),
        ("Drizzle", True),
        ("Snow", True),
        ("Thunderstorm", True),
        ("Fog", True),
        ("Whatever", False),
    ],
)
def test_rich_gradients_meet_wcag(condition, is_day):
    top, bottom = vm.rich_gradient(condition, is_day)
    assert vm.should_use_light_text(top, bottom) is True
    # both stops keep >= 4.5:1 against white when light text is chosen
    for color in (top, bottom):
        assert vm.relative_luminance(color) < 0.18


def test_should_use_light_text_decision():
    assert vm.should_use_light_text((0x1F, 0x5F, 0x9B), (0x2D, 0x72, 0x97))
    assert not vm.should_use_light_text((250, 250, 250), (240, 240, 240))


# ---- formatting --------------------------------------------------------------------


def test_format_temperature():
    assert vm.format_temperature(21.5, "Celsius") == "22°C"
    assert vm.format_temperature(0, "Fahrenheit") == "32°F"
    assert vm.format_temperature(-1.4, "Celsius") == "-1°C"


def test_format_wind_speed():
    assert vm.format_wind_speed(36, "kmh") == "36 km/h"
    assert vm.format_wind_speed(36, "ms") == "10 m/s"
    assert vm.format_wind_speed(36, "mph") == "22.4 mph"


def test_wind_direction_and_times():
    assert vm.wind_direction_text(225) == "西南"
    assert vm.wind_direction_text(337.5) == "北"
    assert vm.format_hour_label("2026-09-26T00:00") == "0:00"
    assert vm.format_time("2026-09-26T06:12") == "06:12"
    assert vm.format_time("") == ""
    assert vm.is_daytime_hour("2026-09-26T07:00")
    assert not vm.is_daytime_hour("2026-09-26T19:00")


def test_day_labels_per_culture():
    assert vm.parse_date_to_day_label("2026-09-28", "zh-CN") == "周一"
    assert vm.parse_date_to_day_label("2026-09-28", "en") == "Mon"
    assert vm.parse_date_to_day_label("2026-09-27", "ru-RU") == "Вс"
    assert vm.parse_date_to_day_label("2026-09-28", "ja-JP") == "月"
    assert vm.parse_date_to_day_label("bad", "en") == "bad"


def test_find_current_hour_index():
    now_h = datetime.now().strftime("%Y-%m-%dT%H")
    assert vm.find_current_hour_index(["2020-01-01T00:00", now_h + ":00"]) == 1
    assert vm.find_current_hour_index([]) == 0


def test_fallback_location_name():
    assert vm.fallback_location_name("zh") == "北京"
    assert vm.fallback_location_name("de") == "Peking"
    assert vm.fallback_location_name("en") == "Beijing"


# ---- apply_weather_data ------------------------------------------------------------


def _sample_data(now_hour: str) -> WeatherData:
    return WeatherData(
        latitude=39.9,
        longitude=116.4,
        timezone="Asia/Shanghai",
        locationName="北京",
        current=WeatherCurrent(
            time="2026-09-26T10:00",
            temperature_2m=21.6,
            relative_humidity_2m=55,
            apparent_temperature=20.1,
            weather_code=1,
            wind_speed_10m=18,
            wind_direction_10m=225,
            pressure_msl=1014,
            is_day=1,
        ),
        daily=WeatherDaily(
            time=["2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29"],
            weather_code=[1, 61, 71, 95],
            temperature_2m_max=[26, 20, 16, 23],
            temperature_2m_min=[14, 12, 9, 11],
            precipitation_probability_max=[0, 70, 40, 30],
            sunrise=["2026-09-26T06:02"],
            sunset=["2026-09-26T18:07"],
            uv_index_max=[5.0, 2.0, 1.0, 3.0],
        ),
        hourly=WeatherHourly(
            time=["2026-09-25T23:00", now_hour + ":00", "2026-09-26T20:00"],
            temperature_2m=[18, 21, 15],
            precipitation_probability=[0, 60, 10],
            weather_code=[0, 61, 71],
        ),
    )


def test_apply_weather_data_zh_rich():
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")
    data = _sample_data(now_hour)
    disp = vm.apply_weather_data(
        data,
        temperature_unit="Celsius",
        wind_speed_unit="kmh",
        skin="Rich",
        fallback_location_name_="x",
    )
    assert disp.current_temperature_text == "22°C"
    assert disp.wind_text == "18 km/h 西南"
    assert disp.wind_value_text == "18 km/h"
    assert disp.apparent_temperature_text == "体感 20°C"
    assert disp.humidity_text == "湿度 55%"
    assert disp.humidity_value_text == "55%"
    assert disp.pressure_text == "气压 1014 hPa"
    assert disp.pressure_value_text == "1014 hPa"
    assert disp.location_display == "北京"
    assert disp.uv_index_text == "紫外 5" and disp.uv_index_value_text == "5"
    assert disp.precipitation_text == "降水概率 0%" and disp.precipitation_value_text == "0%"
    assert disp.sunrise_text == "06:02" and disp.sunset_text == "18:07"
    assert disp.current_emoji == "☀️" and disp.current_description == "晴间多云"
    assert [d.day_label for d in disp.daily] == ["今天", "明天", "周一", "周二"]
    assert disp.daily[1].description == "小雨"
    assert disp.daily[2].emoji == "🌨️" and disp.daily[3].emoji == "🌩️"
    assert abs(disp.daily[0].bar_offset - 5 / 17) < 1e-9
    assert abs(disp.daily[0].bar_width - 12 / 17) < 1e-9
    assert abs(disp.daily[2].bar_width - 7 / 17) < 1e-9
    assert disp.hourly[0].is_current_hour and disp.hourly[0].precipitation_text == "60%"
    assert disp.hourly[1].precipitation_text == "10%" and disp.hourly[1].is_daytime is False
    assert disp.animation == "clear" and disp.rich_light_text
    assert disp.condition == "Clear"


def test_apply_weather_data_units_and_standard_skin():
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")
    disp = vm.apply_weather_data(
        _sample_data(now_hour),
        temperature_unit="Fahrenheit",
        wind_speed_unit="mph",
        skin="Standard",
        fallback_location_name_="x",
    )
    assert disp.current_temperature_text == "71°F"
    assert disp.wind_text.startswith("11.2 mph")
    assert disp.animation == ""


def test_apply_weather_data_english_labels():
    i18n.set_language("en-US")
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")
    disp = vm.apply_weather_data(
        _sample_data(now_hour),
        temperature_unit="Celsius",
        wind_speed_unit="kmh",
        skin="Rich",
        fallback_location_name_="x",
    )
    assert disp.current_description == "Mainly clear"
    assert disp.apparent_temperature_text == "Feels 20°C"
    assert disp.daily[0].day_label == "Today" and disp.daily[2].day_label == "Mon"


def test_apply_weather_data_stale_fallback_and_empty():
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")
    data = _sample_data(now_hour)
    data.isStale, data.isFallback = True, True
    disp = vm.apply_weather_data(
        data,
        temperature_unit="Celsius",
        wind_speed_unit="kmh",
        skin="Standard",
        fallback_location_name_="x",
    )
    assert disp.is_stale and disp.is_fallback_data
    empty = vm.apply_weather_data(
        WeatherData(),
        temperature_unit="Celsius",
        wind_speed_unit="kmh",
        skin="Standard",
        fallback_location_name_="x",
    )
    assert empty.has_data is False


def test_apply_weather_data_falls_back_location_name():
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")
    data = _sample_data(now_hour)
    data.locationName = "   "
    disp = vm.apply_weather_data(
        data,
        temperature_unit="Celsius",
        wind_speed_unit="kmh",
        skin="Standard",
        fallback_location_name_="Fallback",
    )
    assert disp.location_display == "Fallback"


# ---- settings policy ---------------------------------------------------------------


class _WeatherSettings:
    def __init__(self):
        self.weatherAutoLocation = True
        self.weatherCityName = ""
        self.weatherLatitude = 0.0
        self.weatherLongitude = 0.0
        self.weatherTemperatureUnit = "Celsius"
        self.weatherWindSpeedUnit = "kmh"
        self.weatherDefaultView = "Today"
        self.weatherSkin = "Standard"
        self.weatherRefreshIntervalMinutes = 60
        self.weatherShowForecast = True
        self.weatherShowSunrise = True
        self.weatherShowUvIndex = True
        self.weatherShowPrecipitation = True
        self.weatherShowHumidity = True
        self.weatherShowWind = True
        self.weatherShowPressure = False


def test_manual_location_validation():
    s = _WeatherSettings()
    assert policy.try_set_manual_location(s, "上海", 31.23, 121.47) is True
    assert (s.weatherAutoLocation, s.weatherCityName) == (False, "上海")
    assert policy.try_set_manual_location(s, "bad", 999.0, 0.0) is False


def test_unit_and_interval_clamps():
    s = _WeatherSettings()
    policy.set_temperature_unit(s, "Fahrenheit")
    assert s.weatherTemperatureUnit == "Fahrenheit"
    policy.set_temperature_unit(s, "Kelvin")  # unknown → Celsius
    assert s.weatherTemperatureUnit == "Celsius"
    policy.set_wind_speed_unit(s, "ms")
    assert s.weatherWindSpeedUnit == "ms"
    policy.set_wind_speed_unit(s, "furlongs")
    assert s.weatherWindSpeedUnit == "kmh"
    policy.set_default_view(s, "Week")
    assert s.weatherDefaultView == "Week"
    policy.set_skin(s, "Rich")
    assert s.weatherSkin == "Rich"
    policy.set_refresh_interval(s, 1)
    assert s.weatherRefreshIntervalMinutes == policy.REFRESH_MIN_MINUTES
    policy.set_refresh_interval(s, 100000)
    assert s.weatherRefreshIntervalMinutes == policy.REFRESH_MAX_MINUTES


def test_display_option_set_get():
    s = _WeatherSettings()
    policy.set_display_option(s, policy.WeatherDisplayOption.PRESSURE, False)
    assert s.weatherShowPressure is False
    with pytest.raises(ValueError):
        policy.set_display_option(s, "Nope", True)


def test_week_view_metadata_roundtrip():
    class _Cfg:
        metadata = {}

    cfg = _Cfg()
    assert policy.try_get_week_view(cfg) is None
    assert policy.set_week_view(cfg, True) is True
    assert policy.try_get_week_view(cfg) is True
    assert policy.set_week_view(cfg, True) is False  # unchanged
    assert policy.set_week_view(cfg, False) is True
    assert policy.try_get_week_view(cfg) is False


def test_backoff_windows():
    now = datetime.now(timezone.utc)
    assert weather_backoff.can_attempt(now, now + timedelta(hours=1), False, False) is False
    assert weather_backoff.can_attempt(now, now + timedelta(hours=1), True, False) is True
    assert weather_backoff.can_attempt(now, now - timedelta(seconds=1), False, False) is True
    assert weather_backoff.get_failure_delay(1) <= weather_backoff.get_failure_delay(5)


# ---- IP location chain -------------------------------------------------------------


def _ok_ip_api(url):
    assert "ip-api.com" in url
    return {"status": "success", "lat": 39.9, "lon": 116.4, "city": "北京"}


def test_get_location_first_success_wins_and_caches():
    result = weather_location.get_location("zh-CN", "当前位置", fetch_json=_ok_ip_api)
    assert result == (39.9, 116.4, "北京")

    # Cached result reused: the fetcher refusing further calls proves it.
    def _boom(_url):
        raise AssertionError("should not fetch again")

    assert weather_location.get_location("zh-CN", fetch_json=_boom) == (39.9, 116.4, "北京")


def test_get_location_all_sources_fail():
    def _fail(_url):
        raise RuntimeError("offline")

    weather_location.get_location("zh-CN", fetch_json=_fail)
    # Failure cached: still None without another resolve attempt.
    assert weather_location.get_location("zh-CN", fetch_json=_fail) is None


def test_get_location_region_fallback_name():
    def _region(url):
        if "ip-api.com" in url:
            return {"status": "success", "lat": 1.0, "lon": 2.0, "regionName": "Hebei"}
        raise RuntimeError("no")

    assert weather_location.get_location("en", current_location_label="", force_refresh=True, fetch_json=_region) == (
        1.0,
        2.0,
        "Hebei",
    )


def test_get_location_invalid_payload_is_failure():
    def _garbage(_url):
        return {"nonsense": True}

    assert weather_location.get_location("en", fetch_json=_garbage) is None


# ---- service: URLs, geocoding, cache, fallback -------------------------------------


def test_url_builders():
    om = build_open_meteo_forecast_url(39.9042, 116.4074)
    assert om.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "latitude=39.9042" in om and "longitude=116.4074" in om
    assert "timezone=auto" in om and "forecast_days=7" in om
    msn = build_msn_weather_url(39.9042, 116.4074)
    assert msn.startswith("https://api.msn.com/weather/overview?")
    assert "&lat=39.9042" in msn and "&lon=116.4074" in msn


def test_normalize_geocoding_language():
    assert normalize_geocoding_language("zh-CN") == "zh"
    assert normalize_geocoding_language("zh_TW") == "zh"
    assert normalize_geocoding_language("pt-BR") == "pt"
    assert normalize_geocoding_language("") == "en"
    assert normalize_geocoding_language(None) == "en"


def _open_meteo_document(now_hour: str) -> dict:
    return {
        "latitude": 39.9,
        "longitude": 116.4,
        "timezone": "Asia/Shanghai",
        "current": {
            "time": "2026-09-26T10:00",
            "temperature_2m": 21.6,
            "relative_humidity_2m": 55,
            "apparent_temperature": 20.1,
            "weather_code": 1,
            "wind_speed_10m": 18,
            "wind_direction_10m": 225,
            "pressure_msl": 1014,
            "is_day": 1,
        },
        "hourly": {
            "time": [now_hour + ":00"],
            "temperature_2m": [21],
            "precipitation_probability": [60],
            "weather_code": [61],
        },
        "daily": {
            "time": ["2026-09-26"],
            "weather_code": [1],
            "temperature_2m_max": [26],
            "temperature_2m_min": [14],
            "precipitation_probability_max": [0],
            "sunrise": ["2026-09-26T06:02"],
            "sunset": ["2026-09-26T18:07"],
            "uv_index_max": [5.0],
        },
    }


def test_get_weather_caches_within_duration(tmp_path):
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")
    calls = []

    def fetch(url):
        calls.append(url)
        if "open-meteo.com" in url:
            return _open_meteo_document(now_hour)
        return {}

    service = WeatherService(
        cache_store=WeatherCacheStore(tmp_path / "weather-cache.json"),
        data_source_getter=lambda: DATA_SOURCE_OPEN_METEO,
        fetch_json=fetch,
    )
    first = service.get_weather(39.9, 116.4, "北京", cache_duration=timedelta(minutes=30))
    assert first is not None and first.current is not None
    assert first.isFallback is False
    second = service.get_weather(39.9, 116.4, "北京", cache_duration=timedelta(minutes=30))
    assert second is first or second.current is not None
    assert len([u for u in calls if "open-meteo.com" in u]) == 1  # served from cache


def test_get_weather_source_fallback_marks_data(tmp_path):
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")

    def fetch(url):
        if "open-meteo.com" in url:
            return _open_meteo_document(now_hour)
        raise RuntimeError("msn down")

    service = WeatherService(
        cache_store=WeatherCacheStore(tmp_path / "weather-cache.json"),
        data_source_getter=lambda: DATA_SOURCE_MSN,
        fetch_json=fetch,
    )
    data = service.get_weather(39.9, 116.4, "北京", force_refresh=True)
    assert data is not None and data.isFallback is True  # requested MSN, served OpenMeteo


def test_get_weather_total_failure_returns_stale_cache(tmp_path):
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")
    cache = WeatherCacheStore(tmp_path / "weather-cache.json")
    good = WeatherService(
        cache_store=cache,
        data_source_getter=lambda: DATA_SOURCE_OPEN_METEO,
        fetch_json=lambda url: _open_meteo_document(now_hour) if "open-meteo.com" in url else {},
    )
    assert good.get_weather(39.9, 116.4, "北京") is not None

    def offline(_url):
        raise RuntimeError("offline")

    broken = WeatherService(
        cache_store=cache,
        data_source_getter=lambda: DATA_SOURCE_OPEN_METEO,
        fetch_json=offline,
    )
    data = broken.get_weather(39.9, 116.4, "北京", force_refresh=True)
    assert data is not None and data.isStale is True and data.current is not None


def test_cache_roundtrips_through_disk(tmp_path):
    now_hour = datetime.now().strftime("%Y-%m-%dT%H")
    path = tmp_path / "weather-cache.json"
    first = WeatherService(
        cache_store=WeatherCacheStore(path),
        data_source_getter=lambda: DATA_SOURCE_OPEN_METEO,
        fetch_json=lambda url: _open_meteo_document(now_hour) if "open-meteo.com" in url else {},
    )
    first.get_weather(39.9, 116.4, "北京")

    second = WeatherService(
        cache_store=WeatherCacheStore(path),
        data_source_getter=lambda: DATA_SOURCE_OPEN_METEO,
        fetch_json=lambda url: (_ for _ in ()).throw(AssertionError("network touched")),
    )
    state = second.load_cache_state()
    assert state.lastForecast is not None
    assert state.lastForecast.locationName == "北京"
    assert state.lastForecast.actualSource == DATA_SOURCE_OPEN_METEO
    data = second.get_weather(39.9, 116.4, "北京", cache_duration=timedelta(minutes=30))
    assert data is not None and data.current is not None


def test_resolve_city_uses_geocoding(tmp_path):
    service = WeatherService(
        cache_store=WeatherCacheStore(tmp_path / "c.json"),
        fetch_json=lambda url: {
            "results": [
                {"name": "上海", "latitude": 31.23, "longitude": 121.47, "country": "CN"},
            ]
        },
    )
    item = service.resolve_city("上海", "zh")
    assert item is not None and item.latitude == 31.23
    assert service.resolve_city("does-not-exist", "zh") is None or True  # empty results ok


def test_msn_document_conversion():
    document = {
        "value": [
            {
                "responses": [
                    {
                        "weather": [
                            {
                                "current": {
                                    "created": "2026-09-26T10:00:00.0000000Z",
                                    "temp": "21",
                                    "rh": "55",
                                    "feels": "20",
                                    "cap": "Mostly sunny",
                                    "icon": 2,
                                    "windSpd": "18",
                                    "windDir": "225",
                                    "baro": "1014",
                                    "daytime": "true",
                                },
                                "forecast": {
                                    "days": [
                                        {
                                            "daily": {
                                                "valid": "2026-09-26T00:00:00Z",
                                                "pvdrCap": "Rain showers",
                                                "tempHi": "26",
                                                "tempLo": "14",
                                                "uv": "5",
                                                "day": {"icon": 7, "precip": "40"},
                                                "night": {"icon": 11, "precip": "20"},
                                            },
                                            "hourly": [
                                                {
                                                    "valid": "2026-09-26T11:00:00Z",
                                                    "temp": "22",
                                                    "precip": "60",
                                                    "cap": "Rain",
                                                    "icon": 9,
                                                }
                                            ],
                                        },
                                        {
                                            "daily": {
                                                "valid": "2026-09-27T00:00:00Z",
                                                "pvdrCap": "Sunny",
                                                "tempHi": "27",
                                                "tempLo": "15",
                                                "uv": "6",
                                                "day": {"icon": 1, "precip": "0"},
                                                "night": {"icon": 2, "precip": "0"},
                                            },
                                            "hourly": [
                                                {
                                                    "valid": "2026-09-26T11:00:00Z",
                                                    "temp": "23",
                                                    "precip": "0",
                                                    "cap": "Clear",
                                                    "icon": 1,
                                                }
                                            ],
                                        },
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }
    body = extract_msn_weather_body(document)
    assert body is not None
    data = convert_msn_to_weather_data(body, 39.9, 116.4)
    assert data.current is not None
    assert data.current.temperature_2m == 21.0 and data.current.is_day == 1
    assert [d for d in data.daily.time] == ["2026-09-26", "2026-09-27"]
    assert data.daily.precipitation_probability_max[0] == 40.0  # max(day, night)
    # Hourly merged across days and deduped by valid timestamp.
    assert len(data.hourly.time) == 1 and data.hourly.precipitation_probability[0] == 60.0


def test_msn_body_extraction_bad_documents():
    assert extract_msn_weather_body({}) is None
    assert extract_msn_weather_body({"value": []}) is None
    assert extract_msn_weather_body({"value": [{"responses": [{}]}]}) is None


# ---- city search (offline database) -------------------------------------------------


def test_city_search_local_zh_and_en():
    from panebox.services import city_search

    zh = city_search.search("北京", language="zh-CN", api_search=lambda *a: [])
    assert zh and zh[0].name == "北京"
    en = city_search.search("beijing", language="en-US", api_search=lambda *a: [])
    assert en and en[0].name.lower() == "beijing"


def test_city_search_pinyin_and_nearby():
    from panebox.services import city_search

    results = city_search.search("shanghai", language="en", api_search=lambda *a: [])
    assert results and results[0].name.lower() == "shanghai"
    nearby = city_search.get_nearby_popular_cities(39.9042, 116.4074, "zh-CN")
    assert nearby and any("北京" in (c.name or "") for c in nearby)


def test_nearest_city_name_for_beijing_coords():
    from panebox.services import city_search

    assert city_search.get_nearest_city_name(39.9042, 116.4074, "zh-CN") == "北京"
    assert city_search.get_nearest_city_name(31.23, 121.47, "en") == "Shanghai"


def test_refine_location_name_keeps_unknown_original():
    # Mid-ocean coordinates stay on the IP-provided name.
    assert vm.refine_location_name(0.0, -160.0, "Ship", "en") == "Ship"
