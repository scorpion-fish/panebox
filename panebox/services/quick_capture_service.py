"""Quick Capture service (port of Services/QuickCaptureService.cs).

Single shared instance for the app (all Quick Capture widgets show the same
data). Synchronous where the C# is async — same invariants: newest-first
inserts, pinned order normalization, content-stripped tombstones, recent
window trim, sha256 image dedupe + PIL thumbnails (max 180px).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from ..models.quick_capture import (
    QuickCaptureAppearancePreset,
    QuickCaptureItem,
    QuickCaptureItemType,
    QuickCaptureSourceKind,
    QuickCaptureStoreData,
    QuickCaptureViewMode,
    TextContentFormat,
)
from ..models.todo import TodoAttachment, TodoAttachmentStorage
from . import clipboard_write_scope
from .file_ops import get_available_path, sanitize_file_system_name
from .quick_capture_store import (
    QuickCaptureStore,
    normalize_pinned_sort_orders,
    normalize_sort_orders,
)

DEFAULT_RECENT_LIMIT = 30
MIN_RECENT_LIMIT = 10
MAX_RECENT_LIMIT = 100
MAX_ITEM_BODY_CHARACTERS = 256 * 1024  # MarkdownDocumentService.MaxCharacters
THUMBNAIL_MAX_PIXEL_SIZE = 180
EXPORT_CLEANUP_AGE = timedelta(days=1)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


@dataclass
class QuickCaptureWriteResult:
    saved: bool
    was_truncated: bool
    item: Optional[QuickCaptureItem]


@dataclass
class QuickCaptureDeletedItemSnapshot:
    item: QuickCaptureItem
    is_recent: bool


@dataclass
class QuickCaptureImageCacheInfo:
    total_file_count: int = 0
    total_bytes: int = 0
    unused_file_count: int = 0
    unused_bytes: int = 0


@dataclass
class QuickCaptureImageCacheCleanupResult:
    deleted_file_count: int = 0
    deleted_bytes: int = 0


def normalize_recent_limit(value: int) -> int:
    if value < MIN_RECENT_LIMIT:
        return DEFAULT_RECENT_LIMIT
    return max(MIN_RECENT_LIMIT, min(MAX_RECENT_LIMIT, value))


def normalize_body(body: Optional[str], content_format: str = TextContentFormat.PLAIN_TEXT) -> str:
    if body is None or not body.strip():
        return ""
    value = body.replace("\0", "")
    return value if content_format == TextContentFormat.MARKDOWN else value.strip()


def truncate_body(body: str) -> tuple[str, bool]:
    was_truncated = len(body) > MAX_ITEM_BODY_CHARACTERS
    if not was_truncated:
        return body, False
    return body[:MAX_ITEM_BODY_CHARACTERS], True


def normalize_optional_text(value: Optional[str]) -> Optional[str]:
    return (value or "").strip() or None


def try_detect_url(body: str) -> Optional[str]:
    candidate = (body or "").strip()
    if candidate and _URL_RE.match(candidate):
        return candidate
    return None


def compute_content_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def is_image_file(path: Optional[str]) -> bool:
    return os.path.splitext(path or "")[1].lower() in IMAGE_EXTENSIONS


def normalize_image_extension(path: Optional[str]) -> str:
    extension = os.path.splitext(path or "")[1]
    return extension.lower() if is_image_file(path) else ".png"


def build_image_export_file_name(file_name_prefix: Optional[str], timestamp: datetime, source_image_path: str) -> str:
    prefix = sanitize_file_system_name(file_name_prefix or "").strip() or "Capture"
    extension = normalize_image_extension(source_image_path)
    local = timestamp.astimezone()
    return f"{prefix} {local:%Y-%m-%d %H-%M-%S}{extension}"


class QuickCaptureService:
    def __init__(self, store: Optional[QuickCaptureStore] = None):
        self.store = store or QuickCaptureStore()
        self._data: Optional[QuickCaptureStoreData] = None
        self._lock = threading.RLock()
        self._listeners: List[Callable[[], None]] = []

    def add_changed_listener(self, listener: Callable[[], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_changed_listener(self, listener: Callable[[], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    # ---- data access -----------------------------------------------------------

    def get_data(self) -> QuickCaptureStoreData:
        with self._lock:
            self._ensure_loaded()
            return _clone_data(self._data)

    @property
    def data(self) -> QuickCaptureStoreData:
        with self._lock:
            self._ensure_loaded()
            return self._data

    def _ensure_loaded(self) -> None:
        if self._data is not None:
            return
        self._data = self.store.load()

    def _save(self, notify: bool = True) -> None:
        self.store.save(self._data)
        if notify:
            for listener in list(self._listeners):
                try:
                    listener()
                except Exception:
                    pass

    # ---- records -----------------------------------------------------------

    def add_item(self, body: str) -> QuickCaptureItem:
        return self.add_detailed_item(None, body, QuickCaptureAppearancePreset.DEFAULT)

    def add_detailed_item(
        self,
        title: Optional[str],
        body: str,
        appearance_preset: str = QuickCaptureAppearancePreset.DEFAULT,
        content_format: str = TextContentFormat.PLAIN_TEXT,
        pin: bool = False,
    ) -> QuickCaptureItem:
        result = self.add_detailed_item_with_result(title, body, appearance_preset, content_format, pin)
        return result.item  # type: ignore[return-value]

    def add_detailed_item_with_result(
        self,
        title: Optional[str],
        body: str,
        appearance_preset: str = QuickCaptureAppearancePreset.DEFAULT,
        content_format: str = TextContentFormat.PLAIN_TEXT,
        pin: bool = False,
    ) -> QuickCaptureWriteResult:
        content_format = TextContentFormat.normalize(content_format)
        normalized_body = normalize_body(body, content_format)
        normalized_title = normalize_optional_text(title)
        if not (normalized_title or "").strip() and not normalized_body.strip():
            raise ValueError("Quick Capture title and body cannot both be empty.")

        now = _utcnow()
        item = QuickCaptureItem(
            id=uuid.uuid4().hex,
            body=normalized_body,
            contentFormat=content_format,
            title=normalized_title,
            type=QuickCaptureItemType.LINK if try_detect_url(normalized_body) else QuickCaptureItemType.TEXT,
            url=try_detect_url(normalized_body),
            appearancePreset=QuickCaptureAppearancePreset.normalize(appearance_preset),
            sourceKind=QuickCaptureSourceKind.MANUAL,
            isPinned=pin,
            sortOrder=0,
            pinnedSortOrder=0 if pin else -1,
            createdAt=now,
            updatedAt=now,
        )
        with self._lock:
            self._ensure_loaded()
            body_final, was_truncated = truncate_body(item.body)
            item.body = body_final
            self._insert_item(item, pin=pin)
            self._save()
            return QuickCaptureWriteResult(True, was_truncated, item.clone())

    def update_item_details(
        self,
        item_id: Optional[str],
        title: Optional[str],
        body: str,
        appearance_preset: str = QuickCaptureAppearancePreset.DEFAULT,
        content_format: Optional[str] = None,
    ) -> QuickCaptureWriteResult:
        if not (item_id or "").strip():
            return QuickCaptureWriteResult(False, False, None)
        with self._lock:
            self._ensure_loaded()
            item = next((i for i in self._data.items if i.id == item_id and not i.isDeleted), None)
            if item is None:
                return QuickCaptureWriteResult(False, False, None)

            effective_format = TextContentFormat.normalize(content_format or item.contentFormat)
            normalized_body = normalize_body(body, effective_format)
            normalized_title = normalize_optional_text(title)
            if item.type != QuickCaptureItemType.IMAGE and not normalized_title and not normalized_body:
                return QuickCaptureWriteResult(False, False, None)

            normalized_body, was_truncated = truncate_body(normalized_body)
            item.title = normalized_title
            item.body = normalized_body
            item.contentFormat = effective_format
            if item.type != QuickCaptureItemType.IMAGE:
                item.type = QuickCaptureItemType.LINK if try_detect_url(normalized_body) else QuickCaptureItemType.TEXT
                item.url = try_detect_url(normalized_body)
            item.appearancePreset = QuickCaptureAppearancePreset.normalize(appearance_preset)
            item.updatedAt = _utcnow()
            self._save()
            return QuickCaptureWriteResult(True, was_truncated, item.clone())

    def _insert_item(self, item: QuickCaptureItem, pin: bool) -> None:
        for existing in self._data.items:
            existing.sortOrder += 1
            if pin and existing.isPinned:
                existing.pinnedSortOrder += 1
        self._data.items.insert(0, item)
        normalize_pinned_sort_orders(self._data.items)

    # ---- recent (clipboard) -------------------------------------------------------

    def add_recent_clipboard_item(self, body: str, max_recent_items: int) -> Optional[QuickCaptureItem]:
        normalized_body = normalize_body(body)
        if not normalized_body or clipboard_write_scope.should_ignore_text(normalized_body):
            return None
        with self._lock:
            self._ensure_loaded()
            live = [i for i in self._data.recentItems if not i.isDeleted]
            if live and live[0].body == normalized_body:
                return None

            self._data.recentItems = [i for i in self._data.recentItems if i.body != normalized_body]
            now = _utcnow()
            item = QuickCaptureItem(
                body=normalized_body,
                contentFormat=TextContentFormat.PLAIN_TEXT,
                type=QuickCaptureItemType.LINK if try_detect_url(normalized_body) else QuickCaptureItemType.TEXT,
                url=try_detect_url(normalized_body),
                sourceKind=QuickCaptureSourceKind.CLIPBOARD,
                isRecent=True,
                sortOrder=0,
                createdAt=now,
                updatedAt=now,
            )
            self._insert_recent(item)
            self._trim_recent_items(normalize_recent_limit(max_recent_items))
            self._save()
            self.cleanup_unused_image_cache()
            return item.clone()

    def add_recent_clipboard_image(self, image_png_bytes: bytes, max_recent_items: int) -> Optional[QuickCaptureItem]:
        if not image_png_bytes:
            return None
        content_hash = compute_content_hash(image_png_bytes)
        with self._lock:
            self._ensure_loaded()
            live = [i for i in self._data.recentItems if not i.isDeleted]
            if live and live[0].contentHash == content_hash:
                return None

            image_path = self._save_image_bytes(image_png_bytes, content_hash)
            self._data.recentItems = [i for i in self._data.recentItems if i.contentHash != content_hash]
            now = _utcnow()
            item = QuickCaptureItem(
                body="Image",
                contentFormat=TextContentFormat.PLAIN_TEXT,
                type=QuickCaptureItemType.IMAGE,
                imagePath=image_path,
                contentHash=content_hash,
                attachments=[_image_attachment(image_path, now)],
                sourceKind=QuickCaptureSourceKind.CLIPBOARD,
                isRecent=True,
                sortOrder=0,
                createdAt=now,
                updatedAt=now,
            )
            self._insert_recent(item)
            self._trim_recent_items(normalize_recent_limit(max_recent_items))
            self._save()
            self.cleanup_unused_image_cache()
            return item.clone()

    def _insert_recent(self, item: QuickCaptureItem) -> None:
        for existing in self._data.recentItems:
            existing.sortOrder += 1
        self._data.recentItems.insert(0, item)

    def _trim_recent_items(self, max_recent_items: int) -> None:
        # Only live entries compete for the recent window; tombstones are
        # kept outside it so a trim can never drop delete protection.
        live = [
            i
            for i in self._data.recentItems
            if not i.isDeleted
            and ((i.body or "").strip() or (i.type == QuickCaptureItemType.IMAGE and (i.imagePath or "").strip()))
        ]
        live.sort(key=lambda i: (i.sortOrder, -_epoch(i.updatedAt)))
        live = live[:max_recent_items]
        tombstones: dict[str, QuickCaptureItem] = {}
        for i in self._data.recentItems:
            if i.isDeleted and (i.id not in tombstones or _epoch(i.updatedAt) > _epoch(tombstones[i.id].updatedAt)):
                tombstones[i.id] = i
        self._data.recentItems = live + list(tombstones.values())
        normalize_sort_orders(self._data.recentItems)

    def trim_recent_items(self, max_recent_items: int) -> None:
        with self._lock:
            self._ensure_loaded()
            before = len(self._data.recentItems)
            self._trim_recent_items(normalize_recent_limit(max_recent_items))
            if len(self._data.recentItems) != before:
                self._save()
                self.cleanup_unused_image_cache()

    def save_recent_item_to_records(
        self, recent_item_id: Optional[str], pin: bool = False
    ) -> Optional[QuickCaptureItem]:
        if not (recent_item_id or "").strip():
            return None
        with self._lock:
            self._ensure_loaded()
            recent_item = next(
                (i for i in self._data.recentItems if i.id == recent_item_id and not i.isDeleted),
                None,
            )
            if recent_item is None or not ((recent_item.body or "").strip() or (recent_item.imagePath or "").strip()):
                return None

            now = _utcnow()
            item = QuickCaptureItem(
                body=recent_item.body,
                contentFormat=TextContentFormat.PLAIN_TEXT,
                title=recent_item.title,
                type=recent_item.type,
                url=recent_item.url,
                imagePath=recent_item.imagePath,
                contentHash=recent_item.contentHash,
                attachments=[a.clone() for a in recent_item.attachments],
                sourceKind=QuickCaptureSourceKind.CLIPBOARD,
                isPinned=pin,
                sortOrder=0,
                pinnedSortOrder=0 if pin else -1,
                createdAt=now,
                updatedAt=now,
            )
            self._insert_item(item, pin=pin)
            self._save()
            return item.clone()

    # ---- images -------------------------------------------------------------

    def add_image_file_item(self, image_path: Optional[str], pin: bool = False) -> Optional[QuickCaptureItem]:
        if not (image_path or "").strip() or not os.path.isfile(image_path) or not is_image_file(image_path):
            return None
        with self._lock:
            self._ensure_loaded()
            cached_path = self._save_image_file(image_path)
            content_hash = os.path.splitext(os.path.basename(cached_path))[0]
            now = _utcnow()
            item = QuickCaptureItem(
                body="Image",
                contentFormat=TextContentFormat.PLAIN_TEXT,
                type=QuickCaptureItemType.IMAGE,
                imagePath=cached_path,
                contentHash=content_hash,
                attachments=[_image_attachment(cached_path, now)],
                sourceKind=QuickCaptureSourceKind.IMAGE,
                isPinned=pin,
                sortOrder=0,
                pinnedSortOrder=0 if pin else -1,
                createdAt=now,
                updatedAt=now,
            )
            self._insert_item(item, pin=pin)
            self._save()
            self.cleanup_unused_image_cache()
            return item.clone()

    def replace_item_image(self, item_id: Optional[str], image_path: str) -> Optional[QuickCaptureItem]:
        if (
            not (item_id or "").strip()
            or not (image_path or "").strip()
            or not os.path.isfile(image_path)
            or not is_image_file(image_path)
        ):
            return None
        with self._lock:
            self._ensure_loaded()
            item = next((i for i in self._data.items if i.id == item_id and not i.isDeleted), None)
            if item is None:
                return None

            cached_path = self._save_image_file(image_path)
            content_hash = os.path.splitext(os.path.basename(cached_path))[0]
            primary = next(
                (a for a in item.attachments if (a.filePath or "").lower() == (item.imagePath or "").lower()),
                None,
            )
            if primary is None:
                item.attachments.insert(0, _image_attachment(cached_path, _utcnow()))
            else:
                primary.filePath = cached_path
                primary.displayName = os.path.basename(cached_path)
                primary.type = "image"
                primary.storageMode = TodoAttachmentStorage.MANAGED

            item.type = QuickCaptureItemType.IMAGE
            item.imagePath = cached_path
            item.contentHash = content_hash
            item.sourceKind = QuickCaptureSourceKind.IMAGE
            item.url = None
            item.updatedAt = _utcnow()
            self._save()
            self.cleanup_unused_image_cache()
            return item.clone()

    def create_image_export_file(self, item: QuickCaptureItem, file_name_prefix: Optional[str] = None) -> Optional[str]:
        if (
            item.type != QuickCaptureItemType.IMAGE
            or not (item.imagePath or "").strip()
            or not os.path.isfile(item.imagePath)
        ):
            return None
        os.makedirs(self.store.export_directory, exist_ok=True)
        self._cleanup_old_export_files()
        timestamp = item.updatedAt or item.createdAt or _utcnow()
        export_name = build_image_export_file_name(file_name_prefix, timestamp, item.imagePath)
        export_path = get_available_path(str(self.store.export_directory / export_name))
        shutil.copy2(item.imagePath, export_path)
        return export_path

    def _save_image_bytes(self, image_png_bytes: bytes, content_hash: str) -> str:
        os.makedirs(self.store.image_directory, exist_ok=True)
        image_path = str(self.store.image_directory / f"{content_hash}.png")
        if not os.path.exists(image_path):
            Path(image_path).write_bytes(image_png_bytes)
        self._create_image_thumbnail(image_path)
        return image_path

    def _save_image_file(self, source_image_path: str) -> str:
        payload = Path(source_image_path).read_bytes()
        content_hash = compute_content_hash(payload)
        extension = normalize_image_extension(source_image_path)
        os.makedirs(self.store.image_directory, exist_ok=True)
        image_path = str(self.store.image_directory / f"{content_hash}{extension}")
        if not os.path.exists(image_path):
            Path(image_path).write_bytes(payload)
        self._create_image_thumbnail(image_path)
        return image_path

    def get_or_create_image_thumbnail_path(self, image_path: Optional[str]) -> Optional[str]:
        if not (image_path or "").strip():
            return None
        normalized_path = os.path.abspath(image_path)
        if not os.path.isfile(normalized_path) or not is_image_file(normalized_path):
            return None
        thumbnail_path = self.thumbnail_path_for(normalized_path)
        if os.path.exists(thumbnail_path):
            return thumbnail_path
        return self._create_image_thumbnail(normalized_path)

    def thumbnail_path_for(self, image_path: str) -> str:
        stem = os.path.splitext(os.path.basename(image_path))[0]
        if not stem:
            stem = compute_content_hash(Path(image_path).read_bytes())
        return str(self.store.thumbnail_directory / f"{stem}.png")

    def _create_image_thumbnail(self, image_path: Optional[str]) -> Optional[str]:
        if not (image_path or "").strip() or not os.path.isfile(image_path) or not is_image_file(image_path):
            return None
        thumbnail_path = self.thumbnail_path_for(image_path)
        if os.path.exists(thumbnail_path):
            return thumbnail_path
        try:
            from PIL import Image, ImageOps

            os.makedirs(self.store.thumbnail_directory, exist_ok=True)
            with Image.open(image_path) as image:
                image = ImageOps.exif_transpose(image)
                width, height = image.size
                if width <= 0 or height <= 0:
                    return None
                if width >= height:
                    new_width = min(width, THUMBNAIL_MAX_PIXEL_SIZE)
                    new_height = max(1, round(height * new_width / width))
                else:
                    new_height = min(height, THUMBNAIL_MAX_PIXEL_SIZE)
                    new_width = max(1, round(width * new_height / height))
                thumbnail = image.convert("RGBA").resize((new_width, new_height), Image.LANCZOS)
                tmp = f"{thumbnail_path}.{uuid.uuid4().hex}.tmp"
                try:
                    thumbnail.save(tmp, format="PNG")
                    if os.path.exists(thumbnail_path):
                        os.remove(tmp)
                    else:
                        os.replace(tmp, thumbnail_path)
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            return thumbnail_path
        except Exception:
            return thumbnail_path if os.path.exists(thumbnail_path) else None

    def _cleanup_old_export_files(self) -> None:
        export_dir = self.store.export_directory
        if not os.path.isdir(export_dir):
            return
        cutoff = datetime.now(timezone.utc) - EXPORT_CLEANUP_AGE
        for name in os.listdir(export_dir):
            path = os.path.join(export_dir, name)
            try:
                if os.path.isfile(path) and datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    # ---- image cache ---------------------------------------------------------------

    def _referenced_image_paths(self) -> set[str]:
        paths: set[str] = set()
        for item in list(self._data.items) + list(self._data.recentItems):
            if (
                item is not None
                and not item.isDeleted
                and item.type == QuickCaptureItemType.IMAGE
                and (item.imagePath or "").strip()
            ):
                paths.add(_normalize_fs_path(item.imagePath))
        return paths

    def _referenced_thumbnail_paths(self, referenced_image_paths: set[str]) -> set[str]:
        return {self.thumbnail_path_for(p) for p in referenced_image_paths if p}

    def get_image_cache_info(self) -> QuickCaptureImageCacheInfo:
        with self._lock:
            self._ensure_loaded()
            return self._image_cache_info(self._referenced_image_paths())

    def cleanup_unused_image_cache(self) -> QuickCaptureImageCacheCleanupResult:
        with self._lock:
            self._ensure_loaded()
            return self._cleanup_unused_image_cache(self._referenced_image_paths())

    def _image_cache_info(self, referenced_image_paths: set[str]) -> QuickCaptureImageCacheInfo:
        info = QuickCaptureImageCacheInfo()
        if not os.path.isdir(self.store.image_directory) and not os.path.isdir(self.store.thumbnail_directory):
            return info
        referenced_thumbnails = self._referenced_thumbnail_paths(referenced_image_paths)
        for path in _enumerate_cache_files(self.store.image_directory) + _enumerate_cache_files(
            self.store.thumbnail_directory
        ):
            size = os.path.getsize(path)
            info.total_file_count += 1
            info.total_bytes += size
            if (
                _normalize_fs_path(path) not in referenced_image_paths
                and _normalize_fs_path(path) not in referenced_thumbnails
            ):
                info.unused_file_count += 1
                info.unused_bytes += size
        return info

    def _cleanup_unused_image_cache(self, referenced_image_paths: set[str]) -> QuickCaptureImageCacheCleanupResult:
        result = QuickCaptureImageCacheCleanupResult()
        if not os.path.isdir(self.store.image_directory) and not os.path.isdir(self.store.thumbnail_directory):
            return result
        referenced_thumbnails = self._referenced_thumbnail_paths(referenced_image_paths)
        for path in _enumerate_cache_files(self.store.image_directory) + _enumerate_cache_files(
            self.store.thumbnail_directory
        ):
            normalized = _normalize_fs_path(path)
            if normalized in referenced_image_paths or normalized in referenced_thumbnails:
                continue
            try:
                result.deleted_bytes += os.path.getsize(path)
                os.remove(path)
                result.deleted_file_count += 1
            except OSError:
                pass
        return result

    # ---- attachments ------------------------------------------------------------

    def add_item_with_attachments(
        self, file_paths: Iterable[str], copy_to_managed_storage: bool, pin: bool = False
    ) -> Optional[QuickCaptureItem]:
        paths = _clean_existing_paths(file_paths)
        if not paths:
            return None
        item_id = uuid.uuid4().hex
        attachments: List[TodoAttachment] = []
        for path in paths:
            attachment = _import_attachment(
                path, str(self.store.attachment_directory / item_id), copy_to_managed_storage
            )
            if attachment is not None:
                attachments.append(attachment)
        if not attachments:
            return None
        now = _utcnow()
        primary_image = next((a.filePath for a in attachments if (a.type or "").lower() == "image"), None)
        item = QuickCaptureItem(
            id=item_id,
            body=", ".join(a.displayName or "" for a in attachments),
            contentFormat=TextContentFormat.PLAIN_TEXT,
            type=QuickCaptureItemType.IMAGE if primary_image else QuickCaptureItemType.TEXT,
            imagePath=primary_image,
            attachments=attachments,
            sourceKind=QuickCaptureSourceKind.DRAG_DROP,
            isPinned=pin,
            sortOrder=0,
            pinnedSortOrder=0 if pin else -1,
            createdAt=now,
            updatedAt=now,
        )
        with self._lock:
            self._ensure_loaded()
            self._insert_item(item, pin=pin)
            self._save()
            return item.clone()

    def add_attachments(
        self, item_id: Optional[str], file_paths: Iterable[str], copy_to_managed_storage: bool
    ) -> Optional[QuickCaptureItem]:
        paths = _clean_existing_paths(file_paths)
        if not (item_id or "").strip() or not paths:
            return None
        imported = [
            attachment
            for path in paths
            if (
                attachment := _import_attachment(
                    path, str(self.store.attachment_directory / item_id), copy_to_managed_storage
                )
            )
            is not None
        ]
        with self._lock:
            self._ensure_loaded()
            item = next((i for i in self._data.items if i.id == item_id and not i.isDeleted), None)
            if item is None:
                return None
            for attachment in imported:
                if not any((a.filePath or "").lower() == (attachment.filePath or "").lower() for a in item.attachments):
                    item.attachments.append(attachment)
            item.imagePath = item.imagePath or next(
                (a.filePath for a in item.attachments if (a.type or "").lower() == "image"), None
            )
            if (item.imagePath or "").strip():
                item.type = QuickCaptureItemType.IMAGE
            item.updatedAt = _utcnow()
            self._save()
            return item.clone()

    def delete_attachment(self, item_id: Optional[str], attachment_id: Optional[str]) -> Optional[QuickCaptureItem]:
        if not (item_id or "").strip() or not (attachment_id or "").strip():
            return None
        with self._lock:
            self._ensure_loaded()
            item = next((i for i in self._data.items if i.id == item_id and not i.isDeleted), None)
            if item is None:
                return None
            removed = next((a for a in item.attachments if a.id == attachment_id), None)
            if removed is None:
                return item.clone()

            item.attachments.remove(removed)
            if (item.imagePath or "").lower() == (removed.filePath or "").lower():
                item.imagePath = next(
                    (a.filePath for a in item.attachments if (a.type or "").lower() == "image"),
                    None,
                )
                item.contentHash = None
                if not (item.imagePath or "").strip():
                    item.type = QuickCaptureItemType.LINK if try_detect_url(item.body) else QuickCaptureItemType.TEXT
                    item.url = try_detect_url(item.body)
            item.updatedAt = _utcnow()
            self._save()
            should_delete_file = (
                removed.is_managed_copy
                and os.path.isfile(removed.filePath or "")
                and not _is_path_inside_directory(removed.filePath or "", str(self.store.image_directory))
            )
        if should_delete_file:
            try:
                os.remove(removed.filePath)
            except OSError:
                pass
        return item.clone()

    # ---- pinning / ordering -----------------------------------------------------------

    def set_pinned(self, item_id: Optional[str], is_pinned: bool) -> bool:
        if not (item_id or "").strip():
            return False
        with self._lock:
            self._ensure_loaded()
            item = next((i for i in self._data.items if i.id == item_id), None)
            if item is None or item.isDeleted or item.isPinned == is_pinned:
                return False
            item.isPinned = is_pinned
            if is_pinned:
                for existing in self._data.items:
                    if existing.isPinned and existing is not item:
                        existing.pinnedSortOrder += 1
                item.pinnedSortOrder = 0
            else:
                item.pinnedSortOrder = -1
            normalize_pinned_sort_orders(self._data.items)
            item.updatedAt = _utcnow()
            self._save()
            return True

    def set_pinned_many(self, item_ids: Iterable[str], is_pinned: bool) -> int:
        selected = {i for i in item_ids if (i or "").strip()}
        if not selected:
            return 0
        with self._lock:
            self._ensure_loaded()
            targets = [
                i
                for i in sorted(self._data.items, key=lambda i: i.sortOrder)
                if not i.isDeleted and i.id in selected and i.isPinned != is_pinned
            ]
            if not targets:
                return 0
            now = _utcnow()
            for item in targets:
                item.isPinned = is_pinned
                item.pinnedSortOrder = 0 if is_pinned else -1
                item.updatedAt = now
            if is_pinned:
                target_ids = {i.id for i in targets}
                pinned_order = targets + sorted(
                    (i for i in self._data.items if i.isPinned and i.id not in target_ids),
                    key=lambda i: i.pinnedSortOrder,
                )
                for index, item in enumerate(pinned_order):
                    item.pinnedSortOrder = index
            normalize_pinned_sort_orders(self._data.items)
            self._save()
            return len(targets)

    def set_appearance(self, item_ids: Iterable[str], appearance_preset: str) -> int:
        selected = {i for i in item_ids if (i or "").strip()}
        if not selected:
            return 0
        preset = QuickCaptureAppearancePreset.normalize(appearance_preset)
        with self._lock:
            self._ensure_loaded()
            targets = [
                i for i in self._data.items if not i.isDeleted and i.id in selected and i.appearancePreset != preset
            ]
            if not targets:
                return 0
            now = _utcnow()
            for item in targets:
                item.appearancePreset = preset
                item.updatedAt = now
            self._save()
            return len(targets)

    def move_pinned_item(self, item_id: Optional[str], direction: int) -> bool:
        if not (item_id or "").strip() or direction == 0:
            return False
        with self._lock:
            self._ensure_loaded()
            normalize_pinned_sort_orders(self._data.items)
            pinned = _pinned_ordered(self._data.items)
            current_index = next((idx for idx, i in enumerate(pinned) if i.id == item_id), -1)
            if current_index < 0:
                return False
            target_index = current_index + (-1 if direction < 0 else 1)
            if target_index < 0 or target_index >= len(pinned):
                return False
            pinned[current_index].pinnedSortOrder, pinned[target_index].pinnedSortOrder = (
                pinned[target_index].pinnedSortOrder,
                pinned[current_index].pinnedSortOrder,
            )
            normalize_pinned_sort_orders(self._data.items)
            self._save()
            return True

    def move_pinned_item_to_index(self, item_id: Optional[str], target_index: int) -> bool:
        if not (item_id or "").strip():
            return False
        with self._lock:
            self._ensure_loaded()
            normalize_pinned_sort_orders(self._data.items)
            pinned = _pinned_ordered(self._data.items)
            current_index = next((idx for idx, i in enumerate(pinned) if i.id == item_id), -1)
            if current_index < 0:
                return False
            moved = pinned.pop(current_index)
            pinned.insert(max(0, min(target_index, len(pinned))), moved)
            for index, item in enumerate(pinned):
                item.pinnedSortOrder = index
            self._save()
            return True

    def move_item(self, item_id: Optional[str], target_index: int) -> bool:
        if not (item_id or "").strip():
            return False
        with self._lock:
            self._ensure_loaded()
            items = sorted(
                (i for i in self._data.items if not i.isDeleted),
                key=lambda i: (i.sortOrder, -_epoch(i.updatedAt)),
            )
            current_index = next((idx for idx, i in enumerate(items) if i.id == item_id), -1)
            if current_index < 0:
                return False
            moved = items.pop(current_index)
            items.insert(max(0, min(target_index, len(items))), moved)
            normalize_sort_orders(items)
            normalize_pinned_sort_orders(self._data.items)
            self._save()
            return True

    # ---- delete / restore -----------------------------------------------------------

    def delete_item(self, item_id: Optional[str]) -> Optional[QuickCaptureDeletedItemSnapshot]:
        if not (item_id or "").strip():
            return None
        with self._lock:
            self._ensure_loaded()
            index = next((idx for idx, i in enumerate(self._data.items) if i.id == item_id), -1)
            if index < 0:
                return None
            snapshot = QuickCaptureDeletedItemSnapshot(self._data.items[index].clone(), False)
            # Tombstone, not physical removal: a merge restore treats an
            # absent id as "unknown to this device" and would resurrect the
            # item. Undo restores from the in-memory snapshot.
            self._data.items[index] = _tombstone_stub(self._data.items[index], _utcnow(), False)
            normalize_sort_orders(self._data.items)
            normalize_pinned_sort_orders(self._data.items)
            self._save()
            return snapshot

    def delete_items(self, item_ids: Iterable[str], is_recent: bool) -> List[QuickCaptureDeletedItemSnapshot]:
        selected = {i for i in item_ids if (i or "").strip()}
        if not selected:
            return []
        with self._lock:
            self._ensure_loaded()
            source = self._data.recentItems if is_recent else self._data.items
            targets = [i for i in source if i.id in selected]
            if not targets:
                return []
            snapshots = [QuickCaptureDeletedItemSnapshot(i.clone(), is_recent) for i in targets]
            deleted_at = _utcnow()
            for item in targets:
                index = next((idx for idx, i in enumerate(source) if i is item), -1)
                if index >= 0:
                    source[index] = _tombstone_stub(item, deleted_at, is_recent)
            normalize_sort_orders(source)
            if not is_recent:
                normalize_pinned_sort_orders(self._data.items)
            self._save()
            return snapshots

    def delete_recent_item(self, item_id: Optional[str]) -> Optional[QuickCaptureDeletedItemSnapshot]:
        if not (item_id or "").strip():
            return None
        with self._lock:
            self._ensure_loaded()
            index = next((idx for idx, i in enumerate(self._data.recentItems) if i.id == item_id), -1)
            if index < 0:
                return None
            snapshot = QuickCaptureDeletedItemSnapshot(self._data.recentItems[index].clone(), True)
            self._data.recentItems[index] = _tombstone_stub(self._data.recentItems[index], _utcnow(), True)
            normalize_sort_orders(self._data.recentItems)
            self._save()
            return snapshot

    def restore_deleted_item(self, snapshot: Optional[QuickCaptureDeletedItemSnapshot]) -> bool:
        if snapshot is None:
            return False
        with self._lock:
            self._ensure_loaded()
            target = self._data.recentItems if snapshot.is_recent else self._data.items
            existing = next((i for i in target if i.id == snapshot.item.id), None)
            if existing is not None:
                if not existing.isDeleted:
                    return False
                # Undo resurrects the tombstone; the refreshed timestamp
                # also wins any later merge conflict.
                resurrected = snapshot.item.clone()
                resurrected.isDeleted = False
                resurrected.updatedAt = _utcnow()
                index = next((idx for idx, i in enumerate(target) if i is existing), -1)
                target[index] = resurrected
            else:
                item = snapshot.item.clone()
                target.insert(max(0, min(item.sortOrder, len(target))), item)
            normalize_sort_orders(target)
            if not snapshot.is_recent:
                normalize_pinned_sort_orders(self._data.items)
            self._save()
            return True

    def clear(self) -> None:
        with self._lock:
            self._ensure_loaded()
            cleared_at = _utcnow()
            self._data.items = [i if i.isDeleted else _tombstone_stub(i, cleared_at, False) for i in self._data.items]
            self._data.recentItems = [
                i if i.isDeleted else _tombstone_stub(i, cleared_at, True) for i in self._data.recentItems
            ]
            self._save()
            self.cleanup_unused_image_cache()
            _try_delete_directory(self.store.attachment_directory)

    def clear_recent(self) -> None:
        with self._lock:
            self._ensure_loaded()
            cleared_at = _utcnow()
            self._data.recentItems = [
                i if i.isDeleted else _tombstone_stub(i, cleared_at, True) for i in self._data.recentItems
            ]
            self._save()
            self.cleanup_unused_image_cache()

    # ---- view state ---------------------------------------------------------------

    def set_current_view(self, view_mode: str) -> None:
        view_mode = QuickCaptureViewMode.normalize(view_mode)
        with self._lock:
            self._ensure_loaded()
            if self._data.currentView == view_mode:
                return
            self._data.currentView = view_mode
            self._save(notify=False)

    # ---- clipboard write scope ------------------------------------------------

    def mark_clipboard_text_written_by_panebox(self, body: Optional[str]) -> None:
        clipboard_write_scope.mark_text(normalize_body(body))


# ---- helpers ---------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(value) -> float:
    return value.timestamp() if value is not None else 0.0


def _clone_data(data: QuickCaptureStoreData) -> QuickCaptureStoreData:
    return QuickCaptureStoreData(
        version=data.version,
        currentView=data.currentView,
        items=[i.clone() for i in data.items],
        recentItems=[i.clone() for i in data.recentItems],
    )


def _tombstone_stub(item: QuickCaptureItem, deleted_at: datetime, is_recent: bool) -> QuickCaptureItem:
    """Content-stripped tombstone: keeps id + timestamps so a merge restore
    cannot resurrect a deleted item, while the payload leaves the store."""
    return QuickCaptureItem(
        id=item.id,
        isDeleted=True,
        isRecent=is_recent,
        sortOrder=item.sortOrder,
        createdAt=item.createdAt,
        updatedAt=deleted_at,
    )


def _pinned_ordered(items: List[QuickCaptureItem]) -> List[QuickCaptureItem]:
    return sorted(
        (i for i in items if not i.isDeleted and i.isPinned),
        key=lambda i: (
            i.pinnedSortOrder if i.pinnedSortOrder >= 0 else 2**31,
            i.sortOrder,
            -_epoch(i.updatedAt),
        ),
    )


def _image_attachment(image_path: str, added_at: datetime) -> TodoAttachment:
    return TodoAttachment(
        filePath=image_path,
        displayName=os.path.basename(image_path),
        type="image",
        storageMode=TodoAttachmentStorage.MANAGED,
        addedAt=added_at,
    )


def _clean_existing_paths(file_paths: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for path in file_paths:
        if not (path or "").strip() or not os.path.isfile(path):
            continue
        full = os.path.normpath(os.path.abspath(path))
        if full.lower() in seen:
            continue
        seen.add(full.lower())
        result.append(full)
    return result


def _import_attachment(
    source_path: str, managed_directory: str, copy_to_managed_storage: bool
) -> Optional[TodoAttachment]:
    """AttachmentStorageService.ImportPathAsync."""
    if not os.path.isfile(source_path):
        return None
    source_path = os.path.abspath(source_path)
    extension = os.path.splitext(source_path)[1].lower()
    attachment_type = (
        "image"
        if extension in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".heic", ".heif"}
        else "file"
    )
    if not copy_to_managed_storage:
        return TodoAttachment(
            filePath=source_path,
            displayName=os.path.basename(source_path),
            type=attachment_type,
            storageMode=TodoAttachmentStorage.LINKED,
        )
    os.makedirs(managed_directory, exist_ok=True)
    destination = get_available_path(os.path.join(managed_directory, os.path.basename(source_path)))
    shutil.copy2(source_path, destination)
    return TodoAttachment(
        filePath=destination,
        displayName=os.path.basename(destination),
        type=attachment_type,
        storageMode=TodoAttachmentStorage.MANAGED,
    )


def _normalize_fs_path(path: Optional[str]) -> str:
    if not (path or "").strip():
        return ""
    trimmed = path.strip()
    try:
        return os.path.abspath(trimmed)
    except OSError:
        return trimmed


def _is_path_inside_directory(path: str, directory: str) -> bool:
    normalized_dir = os.path.abspath(directory).rstrip(os.sep) + os.sep
    return os.path.abspath(path).startswith(normalized_dir)


def _enumerate_cache_files(directory) -> List[str]:
    if not os.path.isdir(directory):
        return []
    return [
        os.path.join(directory, name) for name in os.listdir(directory) if os.path.isfile(os.path.join(directory, name))
    ]


def _try_delete_directory(directory) -> None:
    try:
        if os.path.isdir(directory):
            shutil.rmtree(directory)
    except OSError:
        pass
