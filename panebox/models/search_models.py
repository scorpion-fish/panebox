"""Search data models (port of Models/SearchModels.cs).

Result kinds, extension-derived file categories, result items/groups/responses
and recommendation items. Everything's IPC states collapse to the Linux file
index provider's states (the index is local; there is no external service to
negotiate with).
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime
from typing import Callable, List, Optional

# Glyph fallbacks: Segoe MDL2  (folder) /  (file) → Unicode.
GLYPH_FOLDER = "\U0001f4c1"
GLYPH_FILE = "\U0001f4c4"
GLYPH_TODO = "☑"  # C# 
GLYPH_NOTE = "✎"  # C# 
GLYPH_SETTINGS = "⚙"  # C# 
GLYPH_WIDGETS = "▦"  # C# 
GLYPH_THEME = "◓"  # C# 


class SearchResultKind:
    FILE = "File"
    TODO = "Todo"
    QUICK_CAPTURE = "QuickCapture"
    ACTION = "Action"
    FOLDER = "Folder"
    HISTORY = "History"
    FAVORITE = "Favorite"


class FileCategory:
    APP = "App"
    DOCUMENT = "Document"
    IMAGE = "Image"
    VIDEO = "Video"
    MUSIC = "Music"
    ARCHIVE = "Archive"
    OTHER = "Other"


# Linux equivalents of the C# extension tables (launcher/script formats replace
# .exe/.lnk/.msi/.bat/.cmd/.ps1).
APP_EXTENSIONS = {
    ".desktop",
    ".appimage",
    ".sh",
    ".bash",
    ".command",
    ".exe",
    ".lnk",
    ".msi",
    ".bat",
    ".cmd",
    ".ps1",
    ".url",
}
DOCUMENT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".pdf",
    ".txt",
    ".md",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".csv",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".ini",
    ".log",
    ".conf",
    ".yaml",
    ".yml",
    ".tex",
}
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".svg",
    ".webp",
    ".ico",
    ".tif",
    ".tiff",
    ".heic",
    ".raw",
    ".avif",
}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".3gp",
}
MUSIC_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".wma",
    ".m4a",
    ".opus",
    ".mid",
    ".midi",
}
ARCHIVE_EXTENSIONS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".iso",
    ".cab",
    ".zst",
}


def categorize_file(file_name: str) -> str:
    """Extension → semantic category (FileCategoryHelper.Categorize)."""
    extension = os.path.splitext(file_name)[1].lower()
    if not extension:
        return FileCategory.OTHER
    if extension in APP_EXTENSIONS:
        return FileCategory.APP
    if extension in DOCUMENT_EXTENSIONS:
        return FileCategory.DOCUMENT
    if extension in IMAGE_EXTENSIONS:
        return FileCategory.IMAGE
    if extension in VIDEO_EXTENSIONS:
        return FileCategory.VIDEO
    if extension in MUSIC_EXTENSIONS:
        return FileCategory.MUSIC
    if extension in ARCHIVE_EXTENSIONS:
        return FileCategory.ARCHIVE
    return FileCategory.OTHER


@dataclasses.dataclass
class SearchResultItem:
    kind: str
    title: str
    subtitle: Optional[str] = None
    detail_path: Optional[str] = None
    glyph: Optional[str] = None
    modified_at: Optional[datetime] = None
    relevance_score: float = 0.0
    # File metadata, populated lazily by the surface (size/date/type displays).
    file_size: Optional[int] = None
    created_at: Optional[datetime] = None
    todo_widget_id: Optional[str] = None
    todo_item_id: Optional[str] = None
    todo_is_completed: bool = False
    quick_capture_item_id: Optional[str] = None
    action_id: Optional[str] = None
    history_query: Optional[str] = None
    # Localized type label for the row's Type column (set once per response).
    type_display: Optional[str] = None

    @property
    def display_glyph(self) -> str:
        if self.glyph:
            return self.glyph
        return GLYPH_FOLDER if self.kind == SearchResultKind.FOLDER else GLYPH_FILE

    @property
    def app_display_name(self) -> str:
        return os.path.splitext(os.path.basename(self.title))[0]


@dataclasses.dataclass
class SearchResultGroup:
    kind: str
    display_name: str
    items: List[SearchResultItem]
    total_count: int = 0


@dataclasses.dataclass
class SearchFileQueryPage:
    """One page from the file index provider (Everything's SearchFileQueryPage)."""

    items: List[SearchResultItem] = dataclasses.field(default_factory=list)
    total_matched_count: int = 0
    next_offset: int = 0

    @staticmethod
    def empty() -> "SearchFileQueryPage":
        return SearchFileQueryPage([], 0, 0)


@dataclasses.dataclass
class SearchResponse:
    query: str
    ranked_items: List[SearchResultItem] = dataclasses.field(default_factory=list)
    groups: List[SearchResultGroup] = dataclasses.field(default_factory=list)
    total_result_count: int = 0
    materialized_file_result_count: int = 0
    total_file_result_count: int = 0
    next_file_result_offset: int = 0
    has_more_results: bool = False
    elapsed_ms: int = 0
    is_complete: bool = True
    file_provider_state: str = "Connected"


@dataclasses.dataclass
class SearchRecommendationItem:
    kind: str
    title: str
    subtitle: Optional[str] = None
    glyph: Optional[str] = None
    detail_path: Optional[str] = None
    action_id: Optional[str] = None
    todo_widget_id: Optional[str] = None
    todo_item_id: Optional[str] = None
    quick_capture_item_id: Optional[str] = None
    history_query: Optional[str] = None


@dataclasses.dataclass
class SearchTabItem:
    """Dynamic result tab with a membership predicate (SearchTabItem)."""

    id: str
    display_name: str
    predicate: Callable[[SearchResultItem], bool]
    glyph: Optional[str] = None
    supports_file_sort: bool = False
    count: int = 0
