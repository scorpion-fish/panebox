"""Quick Capture domain models (port of Models/QuickCaptureItem.cs + QuickCaptureStoreData.cs).

Enums are plain strings with the C# names so store JSON matches PaneBox
("Text"/"Link"/"Image", "PlainText"/"Markdown", "Default"/"Paper"/...,
"Manual"/"Clipboard"/"Image"/"DragDrop", "Records"/"Pinned"/"Recent").
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from .base import JsonModel
from .todo import TodoAttachment


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class QuickCaptureItemType:
    TEXT = "Text"
    LINK = "Link"
    IMAGE = "Image"
    TODO = "Todo"


class TextContentFormat:
    PLAIN_TEXT = "PlainText"
    MARKDOWN = "Markdown"

    @staticmethod
    def normalize(value: Optional[str]) -> str:
        return (
            TextContentFormat.MARKDOWN
            if (value or "").strip() == TextContentFormat.MARKDOWN
            else TextContentFormat.PLAIN_TEXT
        )


class QuickCaptureAppearancePreset:
    VALUES = ("Default", "Paper", "StickyYellow", "Rose", "Mint", "MistBlue")
    DEFAULT = "Default"

    @staticmethod
    def normalize(value: Optional[str]) -> str:
        return value if value in QuickCaptureAppearancePreset.VALUES else QuickCaptureAppearancePreset.DEFAULT

    @staticmethod
    def localization_key(value: str) -> str:
        names = {
            "Default": "Default",
            "Paper": "Paper",
            "StickyYellow": "Yellow",
            "Rose": "Rose",
            "Mint": "Mint",
            "MistBlue": "Blue",
        }
        return f"QuickCapture.Material.{names.get(value, 'Default')}"

    @staticmethod
    def resolve_list_preset(preset: str, is_recent: bool) -> str:
        """Clipboard/recent entries never show a paper material (C# AppearancePolicy)."""
        return QuickCaptureAppearancePreset.DEFAULT if is_recent else preset


class QuickCaptureSourceKind:
    MANUAL = "Manual"
    CLIPBOARD = "Clipboard"
    IMAGE = "Image"
    DRAG_DROP = "DragDrop"


class QuickCaptureViewMode:
    RECORDS = "Records"
    PINNED = "Pinned"
    RECENT = "Recent"

    @staticmethod
    def normalize(value: Optional[str]) -> str:
        return (
            value
            if value
            in (
                QuickCaptureViewMode.RECORDS,
                QuickCaptureViewMode.PINNED,
                QuickCaptureViewMode.RECENT,
            )
            else QuickCaptureViewMode.RECORDS
        )


@dataclasses.dataclass
class QuickCaptureItem(JsonModel):
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    type: str = QuickCaptureItemType.TEXT
    body: str = ""
    contentFormat: str = TextContentFormat.PLAIN_TEXT
    title: Optional[str] = None
    url: Optional[str] = None
    imagePath: Optional[str] = None
    contentHash: Optional[str] = None
    attachments: List[TodoAttachment] = dataclasses.field(default_factory=list)
    isPinned: bool = False
    isRecent: bool = False
    isDeleted: bool = False
    appearancePreset: str = QuickCaptureAppearancePreset.DEFAULT
    sourceKind: str = QuickCaptureSourceKind.MANUAL
    tags: List[str] = dataclasses.field(default_factory=list)
    archivedAt: Optional[datetime] = None
    sortOrder: int = 0
    pinnedSortOrder: int = -1
    createdAt: datetime = dataclasses.field(default_factory=_now_utc)
    updatedAt: datetime = dataclasses.field(default_factory=_now_utc)
    deviceId: Optional[str] = None

    NESTED = {"attachments": (TodoAttachment, "list")}

    def clone(self) -> "QuickCaptureItem":
        return QuickCaptureItem.from_dict(self.to_dict())  # deep copy, like C# Clone()


@dataclasses.dataclass
class QuickCaptureStoreData(JsonModel):
    version: int = 4
    currentView: str = QuickCaptureViewMode.RECORDS
    items: List[QuickCaptureItem] = dataclasses.field(default_factory=list)
    recentItems: List[QuickCaptureItem] = dataclasses.field(default_factory=list)

    NESTED = {
        "items": (QuickCaptureItem, "list"),
        "recentItems": (QuickCaptureItem, "list"),
    }
