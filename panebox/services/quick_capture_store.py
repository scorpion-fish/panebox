"""Quick Capture store (port of Services/QuickCaptureStore.cs).

One shared store for the whole app (data/quick-capture/quick-capture.json),
unlike the per-widget Todo store. Normalize mirrors the C# pass: ids, body
trimming per format, image attachment synthesis, recent-entry demotion,
tombstone caps, dedupe keys and sort orders.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import List, Optional

from ..constants import QUICK_CAPTURE_DIR
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
from .device_identity import device_id
from .resilient_json_store import ResilientJsonStore

CURRENT_VERSION = 4

# Tombstones are the only records allowed to outlive their content —
# cap them so delete-heavy stores do not accumulate stubs forever.
MAX_RETAINED_TOMBSTONES = 500

# MaxRecentLimit lives on the service in C#; the store needs it too for the
# recent-window trim during Normalize.
MAX_RECENT_LIMIT = 100


class QuickCaptureStore:
    def __init__(self, data_dir: Path = QUICK_CAPTURE_DIR):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = ResilientJsonStore(self.data_dir / "quick-capture.json")

    @property
    def store_path(self) -> Path:
        return self.store.path

    @property
    def image_directory(self) -> Path:
        return self.data_dir / "images"

    @property
    def thumbnail_directory(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def export_directory(self) -> Path:
        return self.data_dir / "exports"

    @property
    def attachment_directory(self) -> Path:
        return self.data_dir / "attachments"

    # ---- load / save -----------------------------------------------------------

    def load(self) -> QuickCaptureStoreData:
        result = self.store.load()
        return normalize(QuickCaptureStoreData.from_dict(result.data))

    def save(self, data: QuickCaptureStoreData) -> None:
        data = normalize(data)
        self.store.save(data.to_dict())


def normalize(data: Optional[QuickCaptureStoreData]) -> QuickCaptureStoreData:
    data = data or QuickCaptureStoreData()
    data.version = CURRENT_VERSION
    data.currentView = QuickCaptureViewMode.normalize(data.currentView)

    _normalize_items(data.items, is_recent=False)
    _normalize_items(data.recentItems, is_recent=True)

    items = [item for item in data.items if _is_valid_item(item)]
    # Dedupe by id, keep first occurrence.
    seen_ids: set[str] = set()
    deduped: List[QuickCaptureItem] = []
    for item in items:
        if item.id in seen_ids:
            continue
        seen_ids.add(item.id)
        deduped.append(item)
    deduped.sort(key=lambda i: (i.sortOrder, -_epoch(i.updatedAt)))
    data.items = _prune_tombstones(deduped)
    normalize_pinned_sort_orders(data.items)

    # Live entries fill the recent window on their own; tombstones ride
    # outside it (deduped by id, newest UpdatedAt wins) so loading cannot
    # squeeze delete protection out of the list.
    live_recent: List[QuickCaptureItem] = []
    seen_keys: set[str] = set()
    for item in sorted(data.recentItems, key=lambda i: (i.sortOrder, -_epoch(i.updatedAt))):
        if _is_valid_item(item) and not item.isDeleted:
            key = deduplication_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            live_recent.append(item)
        if len(live_recent) >= MAX_RECENT_LIMIT:
            break
    tombstoned_by_id: dict[str, QuickCaptureItem] = {}
    for item in data.recentItems:
        if item.isDeleted and (
            item.id not in tombstoned_by_id or _epoch(item.updatedAt) > _epoch(tombstoned_by_id[item.id].updatedAt)
        ):
            tombstoned_by_id[item.id] = item
    data.recentItems = _prune_tombstones(live_recent + list(tombstoned_by_id.values()))

    normalize_sort_orders(data.items)
    normalize_sort_orders(data.recentItems)
    return data


def _epoch(value) -> float:
    return value.timestamp() if value is not None else 0.0


def _normalize_items(items: List[QuickCaptureItem], is_recent: bool) -> None:
    sort_order = 0
    for item in items:
        if item is None:
            continue
        if not (item.id or "").strip():
            item.id = uuid.uuid4().hex

        # Sync-layer field: local records all originate from this device.
        item.deviceId = item.deviceId or device_id()

        item.contentFormat = TextContentFormat.normalize(item.contentFormat)
        body = item.body or ""
        if item.contentFormat == TextContentFormat.MARKDOWN:
            item.body = body.replace("\0", "")
        else:
            item.body = body.strip()
        item.title = ((item.title or "").strip() or None) if (item.title or "").strip() else None
        item.url = ((item.url or "").strip() or None) if (item.url or "").strip() else None
        item.imagePath = ((item.imagePath or "").strip() or None) if (item.imagePath or "").strip() else None
        item.contentHash = ((item.contentHash or "").strip() or None) if (item.contentHash or "").strip() else None
        item.attachments = item.attachments or []
        if item.imagePath and not any((a.filePath or "").lower() == item.imagePath.lower() for a in item.attachments):
            item.attachments.insert(
                0,
                TodoAttachment(
                    filePath=item.imagePath,
                    displayName=os.path.basename(item.imagePath),
                    type="image",
                    storageMode=TodoAttachmentStorage.MANAGED,
                    addedAt=item.createdAt if item.createdAt is not None else None,
                ),
            )
        _normalize_attachments(item.attachments)
        item.imagePath = item.imagePath or next(
            (
                (a.filePath or "").strip()
                for a in item.attachments
                if (a.type or "").lower() == "image" and (a.filePath or "").strip()
            ),
            None,
        )
        if item.imagePath:
            item.type = QuickCaptureItemType.IMAGE
        item.isRecent = is_recent
        original_tags = item.tags or []
        item.tags = []
        for tag in original_tags:
            tag = (tag or "").strip()
            if tag and tag.lower() not in [t.lower() for t in item.tags]:
                item.tags.append(tag)
        if is_recent:
            item.contentFormat = TextContentFormat.PLAIN_TEXT
            item.appearancePreset = QuickCaptureAppearancePreset.DEFAULT
            item.sourceKind = QuickCaptureSourceKind.CLIPBOARD
        if item.createdAt is None:
            item.createdAt = _now()
        if item.updatedAt is None:
            item.updatedAt = item.createdAt
        if item.sortOrder < 0:
            item.sortOrder = sort_order
        if not item.isPinned:
            item.pinnedSortOrder = -1
        sort_order += 1


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _is_valid_item(item: Optional[QuickCaptureItem]) -> bool:
    return bool(
        item is not None
        and (
            item.isDeleted
            or (item.title or "").strip()
            or (item.body or "").strip()
            or len(item.attachments) > 0
            or (item.type == QuickCaptureItemType.IMAGE and (item.imagePath or "").strip())
        )
    )


def _prune_tombstones(items: List[QuickCaptureItem]) -> List[QuickCaptureItem]:
    tombstones = [i for i in items if i.isDeleted]
    if len(tombstones) <= MAX_RETAINED_TOMBSTONES:
        return items
    retained = {i.id for i in sorted(tombstones, key=lambda i: -_epoch(i.updatedAt))[:MAX_RETAINED_TOMBSTONES]}
    return [i for i in items if not i.isDeleted or i.id in retained]


def _normalize_attachments(attachments: List[TodoAttachment]) -> None:
    for attachment in list(attachments):
        if attachment is None:
            attachments.remove(attachment)
            continue
        attachment.id = (attachment.id or "").strip() or uuid.uuid4().hex
        attachment.filePath = (attachment.filePath or "").strip()
        attachment.displayName = (attachment.displayName or "").strip() or os.path.basename(attachment.filePath)
        attachment.type = (attachment.type or "").strip() or "file"
        attachment.storageMode = TodoAttachmentStorage.normalize(attachment.storageMode)
        if attachment.addedAt is None:
            attachment.addedAt = _now()
    for attachment in list(attachments):
        if not (attachment.filePath or "").strip():
            attachments.remove(attachment)


def deduplication_key(item: QuickCaptureItem) -> str:
    # Stripped tombstones share empty content keys — dedupe them by id
    # so distinct deletes never collapse into one record.
    if item.isDeleted:
        return f"deleted:{item.id}"
    if (item.contentHash or "").strip():
        return item.contentHash
    if item.type == QuickCaptureItemType.IMAGE and (item.imagePath or "").strip():
        return f"image:{item.imagePath}"
    return item.body


def normalize_sort_orders(items: List[QuickCaptureItem]) -> None:
    for index, item in enumerate(items):
        item.sortOrder = index


def normalize_pinned_sort_orders(items: List[QuickCaptureItem]) -> None:
    pinned = sorted(
        (i for i in items if i is not None and i.isPinned),
        key=lambda i: (
            i.pinnedSortOrder if i.pinnedSortOrder >= 0 else 2**31,
            i.sortOrder,
            -_epoch(i.updatedAt),
        ),
    )
    for index, item in enumerate(pinned):
        item.pinnedSortOrder = index
    for item in items:
        if item is not None and not item.isPinned:
            item.pinnedSortOrder = -1
