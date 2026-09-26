"""Glance widget tests: lunar calendar, festivals, store, calendar grid,
image service (fake HTTP), view formatting and settings policy."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from panebox.models.glance import (
    GlanceBackgroundSource,
    GlanceLayoutMode,
    GlanceWidgetData,
)
from panebox.services import chinese_calendar, glance_calendar
from panebox.services.glance_festival import get_chinese_festival
from panebox.services.glance_image_service import GlanceImageService
from panebox.services.glance_store import GlanceWidgetStore, normalize
from panebox.services.glance_traditional import (
    format_day,
    format_title,
    resolve_mode,
)
from panebox.services.glance_widget_settings_policy import (
    clear_local_source,
    set_display_element,
    set_layout,
)


# ---- chinese lunar calendar -----------------------------------------------------


def test_lunar_table_known_dates():
    assert chinese_calendar.chinese_date(date(1900, 1, 31)) == chinese_calendar.ChineseDate(
        year=1900, month=1, day=1, is_leap_month=False, leap_month=8, sexagenary_year=37
    )
    checks = [
        (date(2024, 2, 10), (2024, 1, 1, False), "甲辰"),
        (date(2025, 1, 29), (2025, 1, 1, False), "乙巳"),
        (date(2023, 3, 22), (2023, 2, 1, True), "癸卯"),  # 闰二月初一
        (date(2025, 7, 25), (2025, 6, 1, True), "乙巳"),  # 闰六月初一
        (date(2024, 9, 17), (2024, 8, 15, False), "甲辰"),  # 中秋
        (date(2024, 2, 9), (2023, 12, 30, False), "癸卯"),  # 除夕 = 腊月三十
    ]
    for value, (year, month, day, leap), stem_branch in checks:
        got = chinese_calendar.chinese_date(value)
        assert (got.year, got.month, got.day, got.is_leap_month) == (year, month, day, leap), value
        assert chinese_calendar.sexagenary_name(got.sexagenary_year) == stem_branch


def test_lunar_continuity_walks_year_boundary():
    # every consecutive day advances the lunar day by one (modulo month lengths)
    previous = chinese_calendar.chinese_date(date(2024, 1, 1))
    for offset in range(1, 420):
        current = chinese_calendar.chinese_date(date(2024, 1, 1).__add__(_days(offset)))
        same_month = (current.month, current.is_leap_month) == (
            previous.month,
            previous.is_leap_month,
        )
        assert (not same_month) or current.day == previous.day + 1
        previous = current


def _days(n):
    from datetime import timedelta

    return timedelta(days=n)


def test_qingming_formula_matches_recent_years():
    assert chinese_calendar.qingming_day(2000) == 4
    assert chinese_calendar.qingming_day(2024) == 4
    assert chinese_calendar.qingming_day(2025) == 4
    assert chinese_calendar.qingming_day(1996) == 4


def test_lunar_out_of_range_raises():
    with pytest.raises(ValueError):
        chinese_calendar.chinese_date(date(1899, 12, 31))
    with pytest.raises(ValueError):
        chinese_calendar.chinese_date(date(2101, 1, 1))


# ---- traditional calendar layer ---------------------------------------------------


def test_resolve_mode_auto_follows_language():
    assert resolve_mode("Auto", "zh-CN") == "ChineseLunar"
    assert resolve_mode("Auto", "zh_TW") == "ChineseLunar"
    assert resolve_mode("Auto", "en-US") == "None"
    assert resolve_mode("None", "zh-CN") == "None"
    assert resolve_mode("ChineseLunar", "en-US") == "ChineseLunar"


def test_traditional_formatting_chinese():
    assert format_day(date(2024, 2, 10), "ChineseLunar") == "正月"
    assert format_day(date(2024, 2, 11), "ChineseLunar") == "初二"
    assert format_day(date(2024, 2, 10), "None") == ""
    assert format_day(date(2025, 7, 25), "ChineseLunar") == "闰六月"
    title = format_title(date(2024, 2, 10), "ChineseLunar")
    assert title == "甲辰年 正月初一"


# ---- festivals ----------------------------------------------------------------------


def test_festivals_known_dates():
    assert get_chinese_festival(date(2024, 2, 10)) == "春节"
    assert get_chinese_festival(date(2024, 2, 24)) == "元宵"
    assert get_chinese_festival(date(2024, 9, 17)) == "中秋"
    assert get_chinese_festival(date(2024, 4, 4)) == "清明"
    assert get_chinese_festival(date(2024, 2, 9)) == "除夕"
    assert get_chinese_festival(date(2024, 6, 10)) == "端午"
    assert get_chinese_festival(date(2024, 3, 1)) == ""


def test_festivals_skip_leap_month_occurrences():
    # 2023-03-22 starts 闰二月; its 15th day is NOT 元宵 (that was 2023-02-05).
    assert get_chinese_festival(date(2023, 4, 6)) == ""  # 闰二月十六, no festival
    assert get_chinese_festival(date(2023, 2, 5)) == "元宵"


# ---- store ---------------------------------------------------------------------------


def test_store_normalize_defaults_and_clamps(tmp_path: Path):
    raw = GlanceWidgetData(
        backgroundSource="Bogus",
        rotationIntervalMinutes=7,
        timeScale=9,
        localImagePaths=["", "  /a.png  ", "/a.png", "/A.PNG"],
        showDate=False,
        showYear=True,
        backgroundImageTransparency=2.0,
        layout="Nope",
    )
    result = normalize(raw)
    assert result.backgroundSource == "Bing"
    assert result.rotationIntervalMinutes == 30
    assert result.timeScale == 1.35
    assert result.localImagePaths == ["/a.png"]  # trimmed + case-insensitive dedupe
    assert result.showYear is False  # year cannot outlive date
    assert result.backgroundImageTransparency == 1.0
    assert result.layout == "Centered"
    assert result.version == 10


def test_store_roundtrip_and_update(tmp_path: Path):
    store = GlanceWidgetStore(str(tmp_path / "widgets" / "w1.json"))
    store.save(GlanceWidgetData(layout="Calendar"))
    assert json.loads((tmp_path / "widgets" / "w1.json").read_text())["layout"] == "Calendar"
    store.update(lambda settings: setattr(settings, "rotationIntervalMinutes", 5))
    assert store.load().rotationIntervalMinutes == 5


def test_store_change_listener_fires(tmp_path: Path):
    store = GlanceWidgetStore(str(tmp_path / "w.json"))
    events = []
    store.add_changed_listener(lambda: events.append(1))
    store.save(GlanceWidgetData())
    assert events == [1]


def test_store_legacy_migration(tmp_path: Path):
    legacy = tmp_path / "glance.json"
    legacy.write_text(json.dumps({"layout": "Editorial", "version": 9}), encoding="utf-8")
    store = GlanceWidgetStore(str(tmp_path / "widgets" / "w9.json"), legacy_store_path=str(legacy))
    data = store.load()
    assert data.layout == "Editorial" and data.version == 10
    assert not legacy.exists()
    assert (tmp_path / "widgets" / "w9.json").exists()


# ---- calendar grid --------------------------------------------------------------------


def test_build_month_grid_shape_and_flags():
    month = glance_calendar.build_month(date(2026, 9, 1), date(2026, 9, 15), first_day_of_week=0)
    assert len(month.days) == 42
    assert len(month.weekdayHeaders) == 7
    assert month.weekdayHeaders[0] == "Mon"
    assert month.days[0].date == date(2026, 8, 31)  # Sept 1 2026 is a Tuesday
    in_month = [d for d in month.days if d.isCurrentMonth]
    assert len(in_month) == 30
    assert sum(1 for d in month.days if d.isToday) == 1


def test_weekday_headers_follow_app_language_not_process_locale():
    # Headers must track the in-app language; importing GTK flips the process
    # locale (LC_TIME) on some systems and must not leak into the calendar.
    from panebox import i18n

    i18n.set_language("zh-CN")
    try:
        assert glance_calendar.weekday_headers(0)[0] == "周一"
        assert glance_calendar.weekday_headers(6)[0] == "周日"  # Sunday-first rotation
    finally:
        i18n.set_language(None)
    assert glance_calendar.weekday_headers(0) == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def test_build_month_decorates_lunar_and_festivals():
    month = glance_calendar.build_month(date(2024, 2, 1), date(2024, 2, 10), first_day_of_week=0)
    glance_calendar.decorate_month(month, "ChineseLunar", "zh-CN", True, date(2024, 2, 10))
    assert month.traditionalTitle == "甲辰年 正月初一"
    spring = next(d for d in month.days if d.date == date(2024, 2, 10))
    assert spring.festivalText == "春节"
    new_year_eve = next(d for d in month.days if d.date == date(2024, 2, 9))
    assert new_year_eve.festivalText == "除夕"


def test_navigation_resolver_clamps_and_normalizes():
    # wheel down (delta>0) goes to the previous month, like the C# resolver.
    assert glance_calendar.resolve_wheel_target(date(2026, 9, 15), 1, date(2020, 1, 1), date(2027, 12, 1)) == date(
        2026, 8, 1
    )
    assert glance_calendar.resolve_wheel_target(date(2027, 12, 1), -1, date(2020, 1, 1), date(2027, 12, 1)) == date(
        2027, 12, 1
    )  # clamped at max
    assert glance_calendar.resolve_wheel_target(date(2020, 2, 1), 1, date(2020, 1, 1), date(2027, 12, 1)) == date(
        2020, 1, 1
    )  # clamped at min
    assert glance_calendar.resolve_wheel_target(date(2026, 9, 1), 0, date(2020, 1, 1), date(2027, 12, 1)) == date(
        2026, 9, 1
    )


def test_displayed_month_resolution_majority():
    dates = [date(2026, 8, 31)] + [date(2026, 9, d) for d in range(1, 31)]
    assert glance_calendar.resolve_displayed_month(dates, date(2026, 9, 1)) == date(2026, 9, 1)
    assert glance_calendar.resolve_displayed_month([], date(2026, 9, 1)) == date(2026, 9, 1)


def test_layout_calculator_matches_cs_values():
    assert glance_calendar.is_compact(319) and not glance_calendar.is_compact(320)
    assert glance_calendar.calculate_panel_height(400, compact=False) == 298
    assert glance_calendar.calculate_panel_height(300, compact=True) == 260
    assert glance_calendar.calculate_panel_height(1000, compact=False) == 310
    assert glance_calendar.calculate_day_height(298, compact=False) == 40  # (298-58)/6
    assert glance_calendar.should_show_traditional_details(300, 28, False, True)
    assert not glance_calendar.should_show_traditional_details(300, 28, True, True)


# ---- image service (fake transport) ------------------------------------------------------


class FakeResponse:
    def __init__(self, payload=None, content=b"", headers=None):
        self._payload = payload
        self._content = content
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        if self._content:
            yield self._content


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for prefix, response in self.routes:
            if url.startswith(prefix):
                return response
        raise AssertionError(f"unexpected url {url}")


def _bing_payload(count=2):
    return {
        "images": [
            {
                "url": f"/th?id=OHR.{index}&w=1920",
                "hsh": f"hash{index}",
                "title": f"Title {index}",
                "copyright": f"(© Author {index})",
                "copyrightlink": f"/copyrightlink/{index}",
                "wp": True,
            }
            for index in range(count)
        ]
    }


def test_bing_query_builds_candidates():
    service = GlanceImageService("/tmp/unused-glance-cache")
    session = FakeSession([("https://cn.bing.com/", FakeResponse(payload=_bing_payload(2)))])
    service._session = session
    images = service._query_online_pictures("Bing", "Featured")
    assert len(images) == 2
    assert images[0].remoteImageUrl.startswith("https://cn.bing.com/")
    assert images[0].onlineProvider == "Bing"
    assert images[0].license == "Bing"


def test_refresh_downloads_and_trims_catalog(tmp_path: Path):
    cache = tmp_path / "cache" / "glance"
    service = GlanceImageService(str(cache))
    png = b"\x89PNG-not-really-but-fine"
    session = FakeSession(
        [
            ("https://cn.bing.com/HPImageArchive", FakeResponse(payload=_bing_payload(2))),
            (
                "https://cn.bing.com/",
                FakeResponse(content=png, headers={"Content-Type": "image/jpeg"}),
            ),
        ]
    )
    service._session = session
    images = service.refresh_online_images("Bing", "Featured")
    assert 1 <= len(images) <= 2
    for image in images:
        assert os.path.isfile(image.localPath)
        assert image.onlineProvider == "Bing"
    catalog = json.loads((cache / "catalog.json").read_text())
    assert catalog, "catalog persisted"
    assert service.get_cache_size_bytes() > 0


def test_refresh_reuses_existing_download(tmp_path: Path):
    cache = tmp_path / "cache" / "glance"
    service = GlanceImageService(str(cache))
    payload = _bing_payload(1)
    session = FakeSession(
        [
            ("https://cn.bing.com/HPImageArchive", FakeResponse(payload=payload)),
            (
                "https://cn.bing.com/",
                FakeResponse(content=b"first", headers={"Content-Type": "image/jpeg"}),
            ),
        ]
    )
    service._session = session
    first = service.refresh_online_images("Bing", "Featured")
    assert len(first) == 1

    downloads_before = sum(1 for url in session.calls if "HPImageArchive" not in url)
    again = service.refresh_online_images("Bing", "Featured")
    assert len(again) == 1
    downloads_after = sum(1 for url in session.calls if "HPImageArchive" not in url)
    assert downloads_after == downloads_before  # no re-download of a usable file
    assert again[0].localPath == first[0].localPath


def test_local_files_and_folder_sources(tmp_path: Path):
    service = GlanceImageService(str(tmp_path / "cache"))
    one = tmp_path / "one.png"
    one.write_bytes(b"1")
    two = tmp_path / "two.jpg"
    two.write_bytes(b"2")
    three = tmp_path / "three.txt"
    three.write_text("nope")

    images = service.get_available_images(
        GlanceWidgetData(
            backgroundSource=GlanceBackgroundSource.LOCAL_FILES,
            localImagePaths=[str(one), str(two), str(three), str(one)],
        )
    )
    assert [Path(i.localPath).name for i in images] == ["one.png", "two.jpg"]

    folder_images = service.get_available_images(
        GlanceWidgetData(
            backgroundSource=GlanceBackgroundSource.LOCAL_FOLDER,
            localFolderPath=str(tmp_path),
        )
    )
    assert {Path(i.localPath).name for i in folder_images} == {"one.png", "two.jpg"}


def test_clear_cache_removes_everything(tmp_path: Path):
    cache = tmp_path / "cache" / "glance"
    service = GlanceImageService(str(cache))
    session = FakeSession(
        [
            ("https://cn.bing.com/HPImageArchive", FakeResponse(payload=_bing_payload(1))),
            (
                "https://cn.bing.com/",
                FakeResponse(content=b"img", headers={"Content-Type": "image/jpeg"}),
            ),
        ]
    )
    service._session = session
    assert service.refresh_online_images("Bing", "Featured")
    service.clear_cache()
    assert service.get_cache_size_bytes() == 0
    assert not (cache / "catalog.json").exists()


# ---- view formatting helpers ---------------------------------------------------------------


def test_format_time_text_modes():
    from panebox.models.glance import GlanceTimeFormatMode
    from panebox.views.glance_widget import format_time_text

    value = datetime(2026, 9, 26, 15, 5)
    assert format_time_text(value, GlanceTimeFormatMode.HOUR24) == "15:05"
    assert format_time_text(value, GlanceTimeFormatMode.HOUR12) == "3:05"
    midnight = datetime(2026, 9, 26, 0, 5)
    assert format_time_text(midnight, GlanceTimeFormatMode.HOUR12) == "12:05"


def test_format_date_weekday_and_month_title():
    from panebox.views.glance_widget import (
        format_compact_calendar_date_text,
        format_date_text,
        format_month_title,
        format_weekday_text,
    )

    value = datetime(2026, 9, 26, 10, 0)  # Saturday
    assert format_date_text(value, include_year=True, language="zh") == "2026年9月26日"
    assert format_date_text(value, include_year=False, language="zh") == "9月26日"
    assert format_weekday_text(value, language="zh") == "星期六"
    assert format_compact_calendar_date_text(value, language="zh") == "26日"
    assert format_month_title(date(2026, 9, 1), language="zh") == "2026年九月"


# ---- settings policy ---------------------------------------------------------------------------


def test_settings_policy_display_and_layout_coupling():
    settings = GlanceWidgetData()
    set_display_element(settings, "Calendar", True)
    assert settings.layout == "Calendar" and settings.showCalendar
    set_display_element(settings, "Calendar", False)
    assert settings.layout == "Centered" and not settings.showCalendar

    set_display_element(settings, "Year", True)
    assert settings.showYear and settings.showDate  # year implies date
    set_display_element(settings, "Date", False)
    assert not settings.showYear

    set_layout(settings, GlanceLayoutMode.IMMERSIVE)
    assert settings.layout == "Immersive" and not settings.showCalendar


def test_settings_policy_local_sources():
    settings = GlanceWidgetData()
    set_display_element(settings, "Calendar", True)  # arbitrary prior state
    from panebox.services.glance_widget_settings_policy import set_local_image_files

    set_local_image_files(settings, ["/a.png", "/a.png", " /b.png ", ""])
    assert settings.backgroundSource == "LocalFiles"
    assert settings.localImagePaths == ["/a.png", "/b.png"]

    settings.backgroundSource = "LocalFolder"
    clear_local_source(settings)
    assert settings.localFolderPath is None
    settings.backgroundSource = "LocalFiles"
    clear_local_source(settings)
    assert settings.localImagePaths == []
