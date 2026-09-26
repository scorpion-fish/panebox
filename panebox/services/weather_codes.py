"""WMO weather-code mapper (port of Helpers/WeatherCodeMapper.cs, verbatim
tables). MSN descriptions and icon codes fold back into WMO codes so both
data sources share one emoji/condition/description system.
"""

from __future__ import annotations

from .chinese_text import to_traditional


class WeatherCondition:
    CLEAR = "Clear"
    CLOUDY = "Cloudy"
    FOG = "Fog"
    DRIZZLE = "Drizzle"
    RAIN = "Rain"
    SNOW = "Snow"
    THUNDERSTORM = "Thunderstorm"
    UNKNOWN = "Unknown"


# code -> (day emoji, night emoji); single-entry tuples mean day==night.
_EMOJI: dict[int, tuple[str, ...]] = {
    0: ("☀️", "\U0001f319"),  # ☀️ Clear sky day / 🌙 night
    1: ("☀️", "\U0001f319"),  # Mainly clear
    2: ("⛅", "\U0001f319"),  # ⛅ Partly cloudy / 🌙
    3: ("\U0001f325️",),  # 🌥️ Overcast
    45: ("☁️",),  # ☁️ Fog (avoids boxed 🌫️ rendering)
    48: ("☁️",),  # Depositing rime fog
    51: ("\U0001f326️",),  # 🌦️ Light drizzle
    53: ("\U0001f326️",),  # Moderate drizzle
    55: ("\U0001f326️",),  # Dense drizzle
    56: ("\U0001f326️",),  # Light freezing drizzle
    57: ("\U0001f326️",),  # Dense freezing drizzle
    61: ("\U0001f327️",),  # 🌧️ Slight rain
    63: ("\U0001f327️",),  # Moderate rain
    65: ("\U0001f327️",),  # Heavy rain
    66: ("\U0001f327️",),  # Light freezing rain
    67: ("\U0001f327️",),  # Heavy freezing rain
    71: ("\U0001f328️",),  # 🌨️ Slight snow fall
    73: ("\U0001f328️",),  # Moderate snow fall
    75: ("\U0001f328️",),  # Heavy snow fall
    77: ("\U0001f328️",),  # Snow grains
    80: ("\U0001f326️",),  # Slight rain showers
    81: ("\U0001f327️",),  # Moderate rain showers
    82: ("\U0001f327️",),  # Violent rain showers
    85: ("\U0001f328️",),  # Slight snow showers
    86: ("\U0001f328️",),  # Heavy snow showers
    95: ("\U0001f329️",),  # 🌩️ Thunderstorm
    96: ("⛈️",),  # ⛈️ Thunderstorm with slight hail
    99: ("⛈️",),  # ⛈️ Thunderstorm with heavy hail
}


def get_emoji(code: int, is_day: bool = True) -> str:
    entry = _EMOJI.get(code)
    if entry is None:
        return "☀️"  # Unknown → sun
    return entry[bool(is_day) is False and len(entry) > 1]


def get_condition(code: int) -> str:
    if code in (0, 1):
        return WeatherCondition.CLEAR
    if code in (2, 3):
        return WeatherCondition.CLOUDY
    if code in (45, 48):
        return WeatherCondition.FOG
    if 51 <= code <= 57:
        return WeatherCondition.DRIZZLE
    if (61 <= code <= 67) or (80 <= code <= 82):
        return WeatherCondition.RAIN
    if (71 <= code <= 77) or (85 <= code <= 86):
        return WeatherCondition.SNOW
    if 95 <= code <= 99:
        return WeatherCondition.THUNDERSTORM
    return WeatherCondition.UNKNOWN


# MSN "cap" descriptions (zh-CN + English variants) → closest WMO code.
_DESCRIPTION_TO_WMO: dict[str, int] = {
    # Clear / Sunny
    "晴": 0,
    "Sunny": 0,
    "Clear": 0,
    "Clear sky": 0,
    "晴间多云": 1,
    "Mostly sunny": 1,
    "Mainly clear": 1,
    "多云": 2,
    "Partly cloudy": 2,
    "Partly Sunny": 2,
    "阴": 3,
    "Overcast": 3,
    "Cloudy": 3,
    "Mostly cloudy": 3,
    "Mostly Cloudy": 3,
    # Fog
    "雾": 45,
    "Fog": 45,
    "Foggy": 45,
    "冻雾": 48,
    "Freezing fog": 48,
    "薄雾": 45,
    "Mist": 45,
    "Haze": 45,
    # Rain
    "小雨": 51,
    "Light rain": 51,
    "Light drizzle": 51,
    "Drizzle": 51,
    "毛毛雨": 51,
    "中雨": 63,
    "Moderate rain": 63,
    "大雨": 65,
    "Heavy rain": 65,
    "暴雨": 65,
    "Torrential rain": 65,
    "Very heavy rain": 65,
    "阵雨": 80,
    "Rain showers": 80,
    "Showers": 80,
    "Scattered showers": 80,
    "强阵雨": 82,
    "Heavy rain showers": 82,
    "Heavy showers": 82,
    "冻雨": 66,
    "Freezing rain": 66,
    "Ice rain": 66,
    # Snow
    "小雪": 71,
    "Light snow": 71,
    "中雪": 73,
    "Moderate snow": 73,
    "大雪": 75,
    "Heavy snow": 75,
    "阵雪": 85,
    "Snow showers": 85,
    "强阵雪": 86,
    "Heavy snow showers": 86,
    "雨夹雪": 77,
    "Sleet": 77,
    "Rain and snow": 77,
    "米雪": 77,
    "Snow grains": 77,
    # Thunderstorm
    "雷阵雨": 95,
    "Thundershowers": 95,
    "Thunderstorm": 95,
    "Thundershower": 95,
    "雷阵雨伴冰雹": 96,
    "Thunderstorm with hail": 96,
    "雷阵雨伴大冰雹": 99,
    "Thunderstorm with heavy hail": 99,
    "雷暴": 95,
    "Thunder": 95,
    # Dust
    "沙尘暴": 45,
    "Sandstorm": 45,
    "浮尘": 45,
    "Dust": 45,
    "扬沙": 45,
    "Sand": 45,
    # English MSN variants (for non-zh-CN locales)
    "Mostly clear": 1,
    "Partly Cloudy": 2,
    "Scattered clouds": 2,
    "Light Rain": 61,
    "Moderate Rain": 63,
    "Heavy Rain": 65,
    "Light Snow": 71,
    "Moderate Snow": 73,
    "Heavy Snow": 75,
}


def description_to_wmo_code(description: str) -> int:
    if not description or not description.strip():
        return -1
    return _DESCRIPTION_TO_WMO.get(description.strip(), -1)


# MSN icon codes loosely map to WMO: 1=clear, 2-4=cloudy, 5-11=rain, 13-14=snow…
_MSN_ICON_TO_WMO: dict[int, int] = {
    1: 0,  # Sunny
    2: 1,  # Mostly sunny
    3: 2,  # Partly cloudy
    4: 3,  # Cloudy / Overcast
    5: 45,  # Fog
    6: 45,  # Haze / Smoke
    7: 51,  # Light rain
    8: 63,  # Rain
    9: 65,  # Heavy rain
    10: 66,  # Freezing rain
    11: 80,  # Rain showers
    12: 71,  # Light snow
    13: 73,  # Snow
    14: 75,  # Heavy snow
    15: 77,  # Sleet
    16: 85,  # Snow showers
    17: 95,  # Thunderstorm
    18: 96,  # Thunderstorm with hail
    19: 45,  # Blowing snow / dust
    20: 45,  # Dust
    21: 51,  # Mist / drizzle
    22: 45,  # Smoke
    23: 63,  # Windy rain
    24: 3,  # Mostly cloudy
    25: 45,  # Fog
    26: 2,  # Partly cloudy (night)
    27: 0,  # Clear (night)
    28: 1,  # Mostly clear (night)
    29: 29,  # Pass through for night-specific
    30: 2,  # Partly cloudy night
    31: 0,  # Clear night
    32: 1,  # Mostly clear night
    33: 2,  # Partly cloudy night
    34: 3,  # Mostly cloudy night
}


def msn_description_or_icon_to_wmo_code(description: str, msn_icon: int) -> int:
    from_desc = description_to_wmo_code(description)
    if from_desc >= 0:
        return from_desc
    return _MSN_ICON_TO_WMO.get(msn_icon, -1)


# ── Legacy glyph support (kept for parity; the Linux view uses emoji) ──

_GLYPH: dict[int, tuple[str, ...]] = {
    0: ("", ""),
    1: ("", ""),
    2: ("", ""),
    3: ("",),
    45: ("",),
    48: ("",),
    51: ("",),
    53: ("",),
    55: ("",),
    56: ("",),
    57: ("",),
    61: ("",),
    63: ("",),
    65: ("",),
    66: ("",),
    67: ("",),
    71: ("",),
    73: ("",),
    75: ("",),
    77: ("",),
    80: ("",),
    81: ("",),
    82: ("",),
    85: ("",),
    86: ("",),
    95: ("",),
    96: ("",),
    99: ("",),
}


def get_glyph(code: int, is_day: bool = True) -> str:
    entry = _GLYPH.get(code)
    if entry is None:
        return ""  # Sun (unknown fallback)
    return entry[bool(is_day) is False and len(entry) > 1]


# ── Localized descriptions ──────────────────────────────────────────────

_DESCRIPTIONS: dict[str, dict[int, str]] = {
    "zh-CN": {
        0: "晴",
        1: "晴间多云",
        2: "多云",
        3: "阴",
        45: "雾",
        48: "冻雾",
        51: "小雨",
        53: "小雨",
        55: "中雨",
        56: "冻雨",
        57: "冻雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        66: "冻雨",
        67: "冻雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        77: "米雪",
        80: "阵雨",
        81: "阵雨",
        82: "强阵雨",
        85: "阵雪",
        86: "强阵雪",
        95: "雷阵雨",
        96: "雷阵雨伴冰雹",
        99: "雷阵雨伴大冰雹",
    },
    "en": {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Rime fog",
        51: "Light rain",
        53: "Light rain",
        55: "Moderate rain",
        56: "Freezing rain",
        57: "Freezing rain",
        61: "Light rain",
        63: "Moderate rain",
        65: "Heavy rain",
        66: "Freezing rain",
        67: "Freezing rain",
        71: "Light snow",
        73: "Moderate snow",
        75: "Heavy snow",
        77: "Snow grains",
        80: "Rain showers",
        81: "Rain showers",
        82: "Heavy rain showers",
        85: "Snow showers",
        86: "Heavy snow showers",
        95: "Thundershowers",
        96: "Thundershowers with hail",
        99: "Thundershowers with heavy hail",
    },
    "ja-JP": {
        0: "晴天",
        1: "ほぼ晴れ",
        2: "曇りがち",
        3: "曇り",
        45: "霧",
        48: "着氷霧",
        51: "弱い雨",
        53: "弱い雨",
        55: "雨",
        56: "着氷雨",
        57: "着氷雨",
        61: "弱い雨",
        63: "雨",
        65: "強い雨",
        66: "着氷雨",
        67: "着氷雨",
        71: "弱い雪",
        73: "雪",
        75: "強い雪",
        77: "霧雪",
        80: "にわか雨",
        81: "にわか雨",
        82: "強いにわか雨",
        85: "にわか雪",
        86: "強いにわか雪",
        95: "雷雨",
        96: "雹を伴う雷雨",
        99: "激しい雹を伴う雷雨",
    },
    "de-DE": {
        0: "Klar",
        1: "Überwiegend klar",
        2: "Teilweise bewölkt",
        3: "Bedeckt",
        45: "Nebel",
        48: "Reifnebel",
        51: "Leichter Regen",
        53: "Leichter Regen",
        55: "Mäßiger Regen",
        56: "Gefrierender Regen",
        57: "Gefrierender Regen",
        61: "Leichter Regen",
        63: "Mäßiger Regen",
        65: "Starker Regen",
        66: "Gefrierender Regen",
        67: "Gefrierender Regen",
        71: "Leichter Schnee",
        73: "Mäßiger Schnee",
        75: "Starker Schnee",
        77: "Schneegriesel",
        80: "Regenschauer",
        81: "Regenschauer",
        82: "Starke Regenschauer",
        85: "Schneeschauer",
        86: "Starke Schneeschauer",
        95: "Gewitter",
        96: "Gewitter mit Hagel",
        99: "Gewitter mit starkem Hagel",
    },
    "pt-BR": {
        0: "Céu limpo",
        1: "Predominantemente limpo",
        2: "Parcialmente nublado",
        3: "Nublado",
        45: "Nevoeiro",
        48: "Nevoeiro com geada",
        51: "Chuva fraca",
        53: "Chuva fraca",
        55: "Chuva moderada",
        56: "Chuva congelante",
        57: "Chuva congelante",
        61: "Chuva fraca",
        63: "Chuva moderada",
        65: "Chuva forte",
        66: "Chuva congelante",
        67: "Chuva congelante",
        71: "Neve fraca",
        73: "Neve moderada",
        75: "Neve forte",
        77: "Grãos de neve",
        80: "Pancadas de chuva",
        81: "Pancadas de chuva",
        82: "Pancadas de chuva fortes",
        85: "Pancadas de neve",
        86: "Pancadas de neve fortes",
        95: "Trovoada",
        96: "Trovoada com granizo",
        99: "Trovoada com granizo forte",
    },
    "hi-IN": {
        0: "साफ आसमान",
        1: "अधिकतर साफ",
        2: "आंशिक बादल",
        3: "बादल छाए",
        45: "कोहरा",
        48: "पाला कोहरा",
        51: "हल्की बारिश",
        53: "हल्की बारिश",
        55: "मध्यम बारिश",
        61: "हल्की बारिश",
        63: "मध्यम बारिश",
        65: "तेज़ बारिश",
        56: "जमने वाली बारिश",
        57: "जमने वाली बारिश",
        66: "जमने वाली बारिश",
        67: "जमने वाली बारिश",
        71: "हल्की बर्फ",
        73: "मध्यम बर्फ",
        75: "तेज़ बर्फ",
        77: "बर्फ के कण",
        80: "बारिश की बौछारें",
        81: "बारिश की बौछारें",
        82: "तेज़ बारिश की बौछारें",
        85: "बर्फीली बौछारें",
        86: "तेज़ बर्फीली बौछारें",
        95: "गरज के साथ बारिश",
        96: "ओलों के साथ गरज",
        99: "भारी ओलों के साथ गरज",
    },
    "es-ES": {
        0: "Cielo despejado",
        1: "Principalmente despejado",
        2: "Parcialmente nublado",
        3: "Cubierto",
        45: "Niebla",
        48: "Niebla helada",
        51: "Lluvia ligera",
        53: "Lluvia ligera",
        55: "Lluvia moderada",
        61: "Lluvia ligera",
        63: "Lluvia moderada",
        65: "Lluvia intensa",
        56: "Lluvia helada",
        57: "Lluvia helada",
        66: "Lluvia helada",
        67: "Lluvia helada",
        71: "Nieve ligera",
        73: "Nieve moderada",
        75: "Nieve intensa",
        77: "Granos de nieve",
        80: "Chubascos",
        81: "Chubascos",
        82: "Chubascos intensos",
        85: "Chubascos de nieve",
        86: "Chubascos de nieve intensos",
        95: "Tormenta",
        96: "Tormenta con granizo",
        99: "Tormenta con granizo intenso",
    },
    "fr-FR": {
        0: "Ciel dégagé",
        1: "Globalement dégagé",
        2: "Partiellement nuageux",
        3: "Couvert",
        45: "Brouillard",
        48: "Brouillard givrant",
        51: "Pluie légère",
        53: "Pluie légère",
        55: "Pluie modérée",
        61: "Pluie légère",
        63: "Pluie modérée",
        65: "Forte pluie",
        56: "Pluie verglaçante",
        57: "Pluie verglaçante",
        66: "Pluie verglaçante",
        67: "Pluie verglaçante",
        71: "Neige légère",
        73: "Neige modérée",
        75: "Forte neige",
        77: "Neige en grains",
        80: "Averses",
        81: "Averses",
        82: "Fortes averses",
        85: "Averses de neige",
        86: "Fortes averses de neige",
        95: "Orage",
        96: "Orage avec grêle",
        99: "Orage avec forte grêle",
    },
    "ar-SA": {
        0: "سماء صافية",
        1: "صحو غالبًا",
        2: "غائم جزئيًا",
        3: "غائم",
        45: "ضباب",
        48: "ضباب متجمد",
        51: "أمطار خفيفة",
        53: "أمطار خفيفة",
        55: "أمطار متوسطة",
        61: "أمطار خفيفة",
        63: "أمطار متوسطة",
        65: "أمطار غزيرة",
        56: "أمطار متجمدة",
        57: "أمطار متجمدة",
        66: "أمطار متجمدة",
        67: "أمطار متجمدة",
        71: "ثلوج خفيفة",
        73: "ثلوج متوسطة",
        75: "ثلوج غزيرة",
        77: "حبيبات ثلج",
        80: "زخات مطر",
        81: "زخات مطر",
        82: "زخات مطر غزيرة",
        85: "زخات ثلج",
        86: "زخات ثلج غزيرة",
        95: "عواصف رعدية",
        96: "عواصف رعدية مع بَرَد",
        99: "عواصف رعدية مع بَرَد شديد",
    },
    "bn-BD": {
        0: "পরিষ্কার আকাশ",
        1: "প্রধানত পরিষ্কার",
        2: "আংশিক মেঘলা",
        3: "মেঘাচ্ছন্ন",
        45: "কুয়াশা",
        48: "জমাট কুয়াশা",
        51: "হালকা বৃষ্টি",
        53: "হালকা বৃষ্টি",
        55: "মাঝারি বৃষ্টি",
        61: "হালকা বৃষ্টি",
        63: "মাঝারি বৃষ্টি",
        65: "ভারী বৃষ্টি",
        56: "বরফ জমা বৃষ্টি",
        57: "বরফ জমা বৃষ্টি",
        66: "বরফ জমা বৃষ্টি",
        67: "বরফ জমা বৃষ্টি",
        71: "হালকা তুষার",
        73: "মাঝারি তুষার",
        75: "ভারী তুষার",
        77: "তুষারকণা",
        80: "বৃষ্টির ঝাপটা",
        81: "বৃষ্টির ঝাপটা",
        82: "ভারী বৃষ্টির ঝাপটা",
        85: "তুষারের ঝাপটা",
        86: "ভারী তুষারের ঝাপটা",
        95: "বজ্রঝড়",
        96: "শিলাসহ বজ্রঝড়",
        99: "ভারী শিলাসহ বজ্রঝড়",
    },
    "ru-RU": {
        0: "Ясное небо",
        1: "Преимущественно ясно",
        2: "Переменная облачность",
        3: "Пасмурно",
        45: "Туман",
        48: "Изморозь",
        51: "Небольшой дождь",
        53: "Небольшой дождь",
        55: "Умеренный дождь",
        61: "Небольшой дождь",
        63: "Умеренный дождь",
        65: "Сильный дождь",
        56: "Ледяной дождь",
        57: "Ледяной дождь",
        66: "Ледяной дождь",
        67: "Ледяной дождь",
        71: "Небольшой снег",
        73: "Умеренный снег",
        75: "Сильный снег",
        77: "Снежная крупа",
        80: "Ливневый дождь",
        81: "Ливневый дождь",
        82: "Сильный ливень",
        85: "Снегопад",
        86: "Сильный снегопад",
        95: "Гроза",
        96: "Гроза с градом",
        99: "Гроза с сильным градом",
    },
}

_UNKNOWN_DESCRIPTIONS = {
    "zh-CN": "未知",
    "en": "Unknown",
    "ja-JP": "不明",
    "de-DE": "Unbekannt",
    "pt-BR": "Desconhecido",
    "hi-IN": "अज्ञात",
    "es-ES": "Desconocido",
    "fr-FR": "Inconnu",
    "ar-SA": "غير معروف",
    "bn-BD": "অজানা",
    "ru-RU": "Неизвестно",
}


def get_description(code: int, language: str) -> str:
    if language == "zh-TW":
        return to_traditional(get_description(code, "zh-CN"))
    table = _DESCRIPTIONS.get(language, _DESCRIPTIONS["en"])
    return table.get(code) or _UNKNOWN_DESCRIPTIONS.get(language, "Unknown")
