"""Glance widget models (port of Models/GlanceWidgetData.cs).

Versioned, portable preferences for one Glance widget. Downloaded images and
their catalog deliberately live outside this document (GlanceImageService
cache). Enums are plain strings — the PaneBox wire format uses string enums.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

from .base import JsonModel

CURRENT_VERSION = 10


class GlanceBackgroundSource:
    ONLINE = "Online"
    LOCAL_FILES = "LocalFiles"
    LOCAL_FOLDER = "LocalFolder"
    BING = "Bing"


class GlanceOnlineImageProvider:
    WIKIMEDIA = "Wikimedia"
    BING = "Bing"


class GlanceOnlineImageCategory:
    FEATURED = "Featured"
    LANDSCAPES = "Landscapes"
    CITIES = "Cities"
    ARCHITECTURE = "Architecture"
    ANIMALS = "Animals"
    PLANTS = "Plants"
    ASTRONOMY = "Astronomy"
    PEOPLE = "People"

    ALL = (
        FEATURED,
        LANDSCAPES,
        CITIES,
        ARCHITECTURE,
        ANIMALS,
        PLANTS,
        ASTRONOMY,
        PEOPLE,
    )


class GlanceLayoutMode:
    IMMERSIVE = "Immersive"
    CENTERED = "Centered"
    EDITORIAL = "Editorial"
    CALENDAR = "Calendar"


class GlanceDisplayElement:
    TIME = "Time"
    DATE = "Date"
    YEAR = "Year"
    WEEKDAY = "Weekday"
    CALENDAR = "Calendar"


class GlanceTimeFormatMode:
    FOLLOW_SYSTEM = "FollowSystem"
    HOUR24 = "Hour24"
    HOUR12 = "Hour12"


class GlanceTransitionMode:
    NONE = "None"
    CROSS_FADE = "CrossFade"
    SLIDE_FADE = "SlideFade"
    ZOOM_FADE = "ZoomFade"


class GlanceTransitionSpeed:
    FAST = "Fast"
    STANDARD = "Standard"
    RELAXED = "Relaxed"


class GlanceReadabilityMode:
    NONE = "None"
    SOFT = "Soft"
    STRONG = "Strong"


class GlanceCalendarMaterialMode:
    FOLLOW_SYSTEM = "FollowSystem"
    FOLLOW_IMAGE = "FollowImage"


class GlanceTraditionalCalendarMode:
    NONE = "None"
    AUTO = "Auto"
    CHINESE_LUNAR = "ChineseLunar"
    UM_AL_QURA = "UmAlQura"
    HIJRI = "Hijri"
    INDIAN_SAKA = "IndianSaka"
    JAPANESE_ERA = "JapaneseEra"
    BANGLA = "Bangla"
    JULIAN = "Julian"
    HEBREW = "Hebrew"
    PERSIAN = "Persian"
    THAI_BUDDHIST = "ThaiBuddhist"


class GlanceImageFitMode:
    FILL = "Fill"
    FIT = "Fit"


class GlanceImageFocus:
    CENTER = "Center"
    TOP = "Top"
    BOTTOM = "Bottom"
    LEFT = "Left"
    RIGHT = "Right"


@dataclass
class GlanceWidgetData(JsonModel):
    version: int = CURRENT_VERSION
    showTime: bool = True
    showDate: bool = True
    showYear: bool = False
    showWeekday: bool = True
    showCalendar: bool = False
    timeFormat: str = GlanceTimeFormatMode.FOLLOW_SYSTEM
    layout: str = GlanceLayoutMode.CENTERED
    backgroundSource: str = GlanceBackgroundSource.BING
    onlineImageCategory: str = GlanceOnlineImageCategory.FEATURED
    localImagePaths: List[str] = field(default_factory=list)
    localFolderPath: Optional[str] = None
    rotationIntervalMinutes: float = 30
    randomOrder: bool = True
    transition: str = GlanceTransitionMode.CROSS_FADE
    transitionSpeed: str = GlanceTransitionSpeed.STANDARD
    readability: str = GlanceReadabilityMode.SOFT
    # Transparency applied only to the background image: zero keeps the fully
    # opaque presentation, one makes the image invisible.
    backgroundImageTransparency: float = 0.0
    showPhotoControls: bool = True
    calendarMaterialMode: str = GlanceCalendarMaterialMode.FOLLOW_SYSTEM
    calendarImageMaterialTransparency: float = 0.32
    traditionalCalendarMode: str = GlanceTraditionalCalendarMode.NONE
    showChineseFestivals: bool = True
    imageFit: str = GlanceImageFitMode.FILL
    imageFocus: str = GlanceImageFocus.CENTER
    timeFontFamily: Optional[str] = None
    timeScale: float = 1.0


@dataclass
class GlanceImageInfo(JsonModel):
    id: str = ""
    localPath: str = ""
    title: Optional[str] = None
    author: Optional[str] = None
    license: Optional[str] = None
    licenseUrl: Optional[str] = None
    sourcePageUrl: Optional[str] = None
    remoteImageUrl: Optional[str] = None
    pixelWidth: int = 0
    pixelHeight: int = 0
    cachedAtUtc: Optional[datetime] = None
    onlineCategory: str = GlanceOnlineImageCategory.FEATURED
    onlineProvider: str = GlanceOnlineImageProvider.WIKIMEDIA

    @property
    def is_online(self) -> bool:
        return bool((self.sourcePageUrl or "").strip())


@dataclass
class GlanceCalendarDay:
    date: date
    dayText: str
    isCurrentMonth: bool
    isToday: bool
    traditionalText: str = ""
    festivalText: str = ""

    @property
    def has_festival(self) -> bool:
        return bool(self.festivalText.strip())

    @property
    def has_secondary_text(self) -> bool:
        return self.has_festival or bool(self.traditionalText.strip())


@dataclass
class GlanceCalendarMonth:
    month: date  # always day 1
    weekdayHeaders: List[str]
    days: List[GlanceCalendarDay]
    traditionalTitle: str = ""

    def clone(self) -> "GlanceCalendarMonth":
        return GlanceCalendarMonth(
            month=self.month,
            weekdayHeaders=list(self.weekdayHeaders),
            days=[
                GlanceCalendarDay(
                    date=d.date,
                    dayText=d.dayText,
                    isCurrentMonth=d.isCurrentMonth,
                    isToday=d.isToday,
                    traditionalText=d.traditionalText,
                    festivalText=d.festivalText,
                )
                for d in self.days
            ],
            traditionalTitle=self.traditionalTitle,
        )

    def weekday_headers(self) -> List[str]:
        return list(self.weekdayHeaders)
