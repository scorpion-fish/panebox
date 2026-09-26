"""Chinese lunisolar calendar (replacement for the BCL
ChineseLunisolarCalendar used by PaneBox on Windows).

Compact table algorithm covering 1900-01-31 .. 2100-02-08 (the same span the
BCL calendar serves). Each 17-bit year word packs: bits 0-3 the leap month
number (0 = none), bit 16 the leap-month length (1 = 30 days, 0 = 29), and
bits 4-15 the twelve month lengths read as ``0x10000 >> month`` (set = 30
days). A leap month follows its same-numbered regular month.

Cyclical (sexagenary) year numbering matches the BCL: 1984 = 甲子 = 61.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MIN_SUPPORTED_DATE = date(1900, 1, 31)
MAX_SUPPORTED_DATE = date(2100, 2, 8)

# 1900..2100. Data follows the published lunisolar tables used across
# Windows/ICU-compatible implementations; bit 0..11 = months 1..12 length,
# bits 12..14 = leap month length (0 when there is no leap month that year),
# bits 16..19 = leap month number.
_LUNAR_YEAR_WORDS = [
    0x04BD8,
    0x04AE0,
    0x0A570,
    0x054D5,
    0x0D260,
    0x0D950,
    0x16554,
    0x056A0,
    0x09AD0,
    0x055D2,
    0x04AE0,
    0x0A5B6,
    0x0A4D0,
    0x0D250,
    0x1D255,
    0x0B540,
    0x0D6A0,
    0x0ADA2,
    0x095B0,
    0x14977,
    0x04970,
    0x0A4B0,
    0x0B4B5,
    0x06A50,
    0x06D40,
    0x1AB54,
    0x02B60,
    0x09570,
    0x052F2,
    0x04970,
    0x06566,
    0x0D4A0,
    0x0EA50,
    0x06E95,
    0x05AD0,
    0x02B60,
    0x186E3,
    0x092E0,
    0x1C8D7,
    0x0C950,
    0x0D4A0,
    0x1D8A6,
    0x0B550,
    0x056A0,
    0x1A5B4,
    0x025D0,
    0x092D0,
    0x0D2B2,
    0x0A950,
    0x0B557,
    0x06CA0,
    0x0B550,
    0x15355,
    0x04DA0,
    0x0A5B0,
    0x14573,
    0x052B0,
    0x0A9A8,
    0x0E950,
    0x06AA0,
    0x0AEA6,
    0x0AB50,
    0x04B60,
    0x0AAE4,
    0x0A570,
    0x05260,
    0x0F263,
    0x0D950,
    0x05B57,
    0x056A0,
    0x096D0,
    0x04DD5,
    0x04AD0,
    0x0A4D0,
    0x0D4D4,
    0x0D250,
    0x0D558,
    0x0B540,
    0x0B6A0,
    0x195A6,
    0x095B0,
    0x049B0,
    0x0A974,
    0x0A4B0,
    0x0B27A,
    0x06A50,
    0x06D40,
    0x0AF46,
    0x0AB60,
    0x09570,
    0x04AF5,
    0x04970,
    0x064B0,
    0x074A3,
    0x0EA50,
    0x06B58,
    0x055C0,
    0x0AB60,
    0x096D5,
    0x092E0,
    0x0C960,
    0x0D954,
    0x0D4A0,
    0x0DA50,
    0x07552,
    0x056A0,
    0x0ABB7,
    0x025D0,
    0x092D0,
    0x0CAB5,
    0x0A950,
    0x0B4A0,
    0x0BAA4,
    0x0AD50,
    0x055D9,
    0x04BA0,
    0x0A5B0,
    0x15176,
    0x052B0,
    0x0A930,
    0x07954,
    0x06AA0,
    0x0AD50,
    0x05B52,
    0x04B60,
    0x0A6E6,
    0x0A4E0,
    0x0D260,
    0x0EA65,
    0x0D530,
    0x05AA0,
    0x076A3,
    0x096D0,
    0x04AFB,
    0x04AD0,
    0x0A4D0,
    0x1D0B6,
    0x0D250,
    0x0D520,
    0x0DD45,
    0x0B5A0,
    0x056D0,
    0x055B2,
    0x049B0,
    0x0A577,
    0x0A4B0,
    0x0AA50,
    0x1B255,
    0x06D20,
    0x0ADA0,
    0x14B63,
    0x09370,
    0x049F8,
    0x04970,
    0x064B0,
    0x168A6,
    0x0EA50,
    0x06B20,
    0x1A6C4,
    0x0AAE0,
    0x0A2E0,
    0x0D2E3,
    0x0C960,
    0x0D557,
    0x0D4A0,
    0x0DA50,
    0x05D55,
    0x056A0,
    0x0A6D0,
    0x055D4,
    0x052D0,
    0x0A9B8,
    0x0A950,
    0x0B4A0,
    0x0B6A6,
    0x0AD50,
    0x055A0,
    0x0ABA4,
    0x0A5B0,
    0x052B0,
    0x0B273,
    0x06930,
    0x07337,
    0x06AA0,
    0x0AD50,
    0x14B55,
    0x04B60,
    0x0A570,
    0x054E4,
    0x0D160,
    0x0E968,
    0x0D520,
    0x0DAA0,
    0x16AA6,
    0x056D0,
    0x04AE0,
    0x0A9D4,
    0x0A2D0,
    0x0D150,
    0x0F252,
    0x0D520,
]

_BASE_DATE = date(1900, 1, 31)  # 1900 正月初一
_BASE_SEXAGENARY_YEAR = 1900 - 1864 + 1  # 1900 = 庚子 = 37


@dataclass(frozen=True)
class ChineseDate:
    year: int  # lunar year number (Gregorian year of 正月初一)
    month: int  # 1..12, leap month excluded from numbering
    day: int  # 1..30
    is_leap_month: bool
    leap_month: int  # 0 when the year has no leap month
    sexagenary_year: int  # 1..60


def _year_word(lunar_year: int) -> int:
    return _LUNAR_YEAR_WORDS[lunar_year - 1900]


def _month_days(word: int, month: int) -> int:
    return 30 if word & (0x10000 >> month) else 29


def _leap_month_of(word: int) -> int:
    return word & 0xF


def _leap_month_days(word: int) -> int:
    return 30 if word & 0x10000 else 29


def _days_in_lunar_year(word: int) -> int:
    total = sum(_month_days(word, m) for m in range(1, 13))
    if _leap_month_of(word):
        total += _leap_month_days(word)
    return total


_OFFSETS: list[int] | None = None


def _year_offsets() -> list[int]:
    # Days from _BASE_DATE to 正月初一 of each lunar year 1900..2101.
    global _OFFSETS
    if _OFFSETS is None:
        offsets = [0]
        for year in range(1900, 2101):
            offsets.append(offsets[-1] + _days_in_lunar_year(_year_word(year)))
        _OFFSETS = offsets
    return _OFFSETS


def chinese_date(value: date) -> ChineseDate:
    """Lunar date for a Gregorian date. Raises ValueError outside 1900-2100."""
    if value < _BASE_DATE or value > MAX_SUPPORTED_DATE:
        raise ValueError(f"Chinese lunar calendar supports {_BASE_DATE}..{MAX_SUPPORTED_DATE}, got {value}")

    offset = (value - _BASE_DATE).days
    offsets = _year_offsets()
    # Find the lunar year containing this offset (offsets[i] = start of 1900+i).
    lunar_year = 0
    for year in range(2100, 1899, -1):
        if offsets[year - 1900] <= offset:
            lunar_year = year
            break
    offset -= offsets[lunar_year - 1900]

    word = _year_word(lunar_year)
    leap_month = _leap_month_of(word)

    month = 1
    is_leap = False
    while month <= 12:
        days = _month_days(word, month)
        take_leap = leap_month == month and not is_leap
        if offset < days:
            break
        offset -= days
        if take_leap:
            # The leap copy of this month follows the regular one.
            leap_days = _leap_month_days(word)
            if offset < leap_days:
                is_leap = True
                break
            offset -= leap_days
        month += 1

    return ChineseDate(
        year=lunar_year,
        month=month,
        day=offset + 1,
        is_leap_month=is_leap,
        leap_month=leap_month,
        sexagenary_year=(_BASE_SEXAGENARY_YEAR - 1 + (lunar_year - 1900)) % 60 + 1,
    )


def qingming_day(year: int) -> int:
    """Day of April for 清明 in a Gregorian year (the C# formula, verbatim)."""
    short_year = year % 100
    century_constant = 5.59 if year < 2000 else 4.81
    import math

    return int(math.floor(short_year * 0.2422 + century_constant)) - (short_year // 4)


# Display names (simplified; the view converts for zh-TW style locales).

MONTH_NAMES = (
    "",
    "正月",
    "二月",
    "三月",
    "四月",
    "五月",
    "六月",
    "七月",
    "八月",
    "九月",
    "十月",
    "冬月",
    "腊月",
)
DAY_NAMES = (
    "",
    "初一",
    "初二",
    "初三",
    "初四",
    "初五",
    "初六",
    "初七",
    "初八",
    "初九",
    "初十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
    "十八",
    "十九",
    "二十",
    "廿一",
    "廿二",
    "廿三",
    "廿四",
    "廿五",
    "廿六",
    "廿七",
    "廿八",
    "廿九",
    "三十",
)
HEAVENLY_STEMS = ("甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸")
EARTHLY_BRANCHES = ("子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥")


def sexagenary_name(sexagenary_year: int) -> str:
    return f"{HEAVENLY_STEMS[(sexagenary_year - 1) % 10]}{EARTHLY_BRANCHES[(sexagenary_year - 1) % 12]}"


def lunar_month_day_name(value: ChineseDate) -> str:
    """Day display text: month name on the first day, else the day name."""
    if value.day == 1:
        return ("闰" if value.is_leap_month else "") + MONTH_NAMES[value.month]
    return DAY_NAMES[value.day]


def today() -> date:
    return date.today()


__all__ = [
    "MAX_SUPPORTED_DATE",
    "MIN_SUPPORTED_DATE",
    "ChineseDate",
    "chinese_date",
    "lunar_month_day_name",
    "qingming_day",
    "sexagenary_name",
    "today",
    "timedelta",
]
