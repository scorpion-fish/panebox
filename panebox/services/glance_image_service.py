"""Glance background image service (port of Services/GlanceImageService.cs).

Sources: Bing daily wallpaper archive, Wikimedia Commons categories, local
files, or a local folder. A shared catalog.json under cache/glance tracks
downloaded images with per-category caps; downloads are incremental (top up
toward 12 per category, max 3 per refresh) and capped at 18 MiB each.

Synchronous requests-based calls — the surface runs them on a worker thread
and marshals results back to the GTK main loop.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import random
import re
import threading
from datetime import datetime, timezone
from typing import Callable, List, Optional

from ..models.glance import (
    GlanceBackgroundSource,
    GlanceImageInfo,
    GlanceOnlineImageCategory,
    GlanceOnlineImageProvider,
    GlanceWidgetData,
)

TARGET_CATALOG_SIZE_PER_CATEGORY = 12
MAXIMUM_CACHE_ITEMS_PER_CATEGORY = 18
MAXIMUM_CACHE_ITEMS_TOTAL = 72
INCREMENTAL_DOWNLOAD_COUNT = 3
CATEGORY_MEMBER_QUERY_LIMIT = 200
REMOTE_CANDIDATE_LIMIT = 80
BING_ARCHIVE_BATCH_SIZE = 8
BING_ARCHIVE_BATCH_COUNT = 3
MAXIMUM_DOWNLOAD_BYTES = 18 * 1024 * 1024

USER_AGENT = "PaneBox-Linux/1.0 (panebox port)"
HTTP_TIMEOUT = 20

SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

_refresh_gate = threading.Lock()


class GlanceImageService:
    def __init__(
        self,
        cache_directory: str,
        session=None,
        can_use_background_network: Optional[Callable[[], bool]] = None,
    ):
        self._cache_directory = cache_directory
        self._image_directory = os.path.join(cache_directory, "images")
        self._catalog_path = os.path.join(cache_directory, "catalog.json")
        self._session = session
        self._can_use_background_network = can_use_background_network or (lambda: True)

    # ---- catalog ------------------------------------------------------------

    @property
    def image_directory(self) -> str:
        return self._image_directory

    @property
    def catalog_path(self) -> str:
        return self._catalog_path

    def _http(self):
        if self._session is not None:
            return self._session
        import requests

        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        return self._session

    def get_available_images(self, settings: GlanceWidgetData) -> List[GlanceImageInfo]:
        if is_online_source(settings.backgroundSource):
            return load_cached_online_images(
                self._load_catalog(),
                get_online_provider(settings.backgroundSource),
                get_online_category(settings.backgroundSource, settings.onlineImageCategory),
            )
        if settings.backgroundSource == GlanceBackgroundSource.LOCAL_FILES:
            return create_local_images(settings.localImagePaths)
        return create_folder_images(settings.localFolderPath)

    def load_cached_online_images(
        self,
        provider: str = GlanceOnlineImageProvider.WIKIMEDIA,
        category: str = GlanceOnlineImageCategory.FEATURED,
    ) -> List[GlanceImageInfo]:
        return load_cached_online_images(self._load_catalog(), provider, category)

    def refresh_online_images(
        self,
        provider: str = GlanceOnlineImageProvider.WIKIMEDIA,
        category: str = GlanceOnlineImageCategory.FEATURED,
    ) -> List[GlanceImageInfo]:
        if not self._can_use_background_network():
            return self.load_cached_online_images(provider, category)

        with _refresh_gate:
            catalog = self._load_catalog()
            by_source = {}
            for image in catalog:
                if matches_online_source(image, provider, category) and (image.sourcePageUrl or "").strip():
                    by_source.setdefault(image.sourcePageUrl.strip().lower(), image)

            remote = self._query_online_pictures(provider, category)
            os.makedirs(self._image_directory, exist_ok=True)

            new_downloads = 0
            usable_count = sum(
                1
                for image in catalog
                if matches_online_source(image, provider, category) and is_usable_file(image.localPath)
            )
            download_limit = min(
                INCREMENTAL_DOWNLOAD_COUNT,
                max(0, TARGET_CATALOG_SIZE_PER_CATEGORY - usable_count),
            )
            for candidate in remote:
                existing = by_source.get((candidate.sourcePageUrl or "").strip().lower())
                if existing is not None and is_usable_file(existing.localPath):
                    continue
                if new_downloads >= download_limit:
                    break
                try:
                    downloaded = self._download(candidate)
                except Exception:
                    continue
                if downloaded is not None:
                    catalog = [image for image in catalog if image.id != downloaded.id]
                    catalog.append(downloaded)
                    new_downloads += 1

            catalog = [image for image in catalog if is_usable_file(image.localPath)]
            catalog.sort(
                key=lambda image: image.cachedAtUtc or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            catalog = self._trim_and_save_catalog(catalog)
            return load_cached_online_images(catalog, provider, category)

    def clear_cache(self) -> None:
        with _refresh_gate:
            if os.path.isdir(self._image_directory):
                for name in os.listdir(self._image_directory):
                    _try_delete(os.path.join(self._image_directory, name))
            _try_delete(self._catalog_path)

    def get_cache_size_bytes(self) -> int:
        total = 0
        for directory, _dirs, files in _walk(self._cache_directory):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(directory, name))
                except OSError:
                    pass
        return total

    # ---- internals -------------------------------------------------------------

    def _load_catalog(self) -> List[GlanceImageInfo]:
        if not os.path.exists(self._catalog_path):
            return []
        try:
            with open(self._catalog_path, encoding="utf-8") as handle:
                documents = json.load(handle)
            if not isinstance(documents, list):
                return []
            return [GlanceImageInfo.from_dict(document) for document in documents if isinstance(document, dict)]
        except (OSError, ValueError):
            return []

    def _query_online_pictures(self, provider: str, category: str) -> List[GlanceImageInfo]:
        if provider == GlanceOnlineImageProvider.BING:
            return query_bing_pictures(self._http())
        return query_category_pictures(self._http(), category)

    def _download(self, candidate: GlanceImageInfo) -> Optional[GlanceImageInfo]:
        if not (candidate.remoteImageUrl or "").strip():
            return None
        response = self._http().get(candidate.remoteImageUrl, stream=True, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > MAXIMUM_DOWNLOAD_BYTES:
            return None

        media_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        extension = {"image/png": ".png", "image/webp": ".webp"}.get(media_type, ".jpg")
        destination = os.path.join(self._image_directory, f"{candidate.id}{extension}")
        temporary = f"{destination}.{os.getpid()}.tmp"
        try:
            total = 0
            with open(temporary, "wb") as handle:
                for chunk in response.iter_content(chunk_size=81920):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAXIMUM_DOWNLOAD_BYTES:
                        raise ValueError("image exceeds the Glance cache item size limit")
                    handle.write(chunk)
            os.replace(temporary, destination)
            result = GlanceImageInfo.from_dict(candidate.to_dict())
            result.localPath = destination
            result.cachedAtUtc = datetime.now(timezone.utc)
            return result
        finally:
            _try_delete(temporary)

    def _trim_and_save_catalog(self, catalog: List[GlanceImageInfo]) -> List[GlanceImageInfo]:
        groups: dict[tuple[str, str], List[GlanceImageInfo]] = {}
        for image in catalog:
            if not is_usable_file(image.localPath):
                continue
            groups.setdefault((image.onlineProvider, image.onlineCategory), []).append(image)
        kept: List[GlanceImageInfo] = []
        for members in groups.values():
            members.sort(
                key=lambda image: image.cachedAtUtc or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            kept.extend(members[:MAXIMUM_CACHE_ITEMS_PER_CATEGORY])
        kept.sort(
            key=lambda image: image.cachedAtUtc or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        kept = kept[:MAXIMUM_CACHE_ITEMS_TOTAL]

        kept_paths = {(image.localPath or "").lower() for image in kept}
        for image in catalog:
            if image not in kept and (image.localPath or "").lower() not in kept_paths:
                _try_delete(image.localPath)

        os.makedirs(self._cache_directory, exist_ok=True)
        payload = json.dumps([image.to_dict() for image in kept], ensure_ascii=False, indent=2)
        temporary = f"{self._catalog_path}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temporary, self._catalog_path)
        finally:
            _try_delete(temporary)
        return kept


# ---- shared helpers -----------------------------------------------------------


def is_online_source(source: str) -> bool:
    return source in (GlanceBackgroundSource.ONLINE, GlanceBackgroundSource.BING)


def get_online_provider(source: str) -> str:
    return (
        GlanceOnlineImageProvider.BING if source == GlanceBackgroundSource.BING else GlanceOnlineImageProvider.WIKIMEDIA
    )


def get_online_category(source: str, category: str) -> str:
    return GlanceOnlineImageCategory.FEATURED if source == GlanceBackgroundSource.BING else category


def matches_online_source(image: GlanceImageInfo, provider: str, category: str) -> bool:
    return image.onlineProvider == provider and image.onlineCategory == category


def load_cached_online_images(
    catalog: List[GlanceImageInfo],
    provider: str = GlanceOnlineImageProvider.WIKIMEDIA,
    category: str = GlanceOnlineImageCategory.FEATURED,
) -> List[GlanceImageInfo]:
    matched = [
        image
        for image in catalog
        if matches_online_source(image, provider, category) and is_usable_file(image.localPath)
    ]
    matched.sort(
        key=lambda image: image.cachedAtUtc or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return matched[:MAXIMUM_CACHE_ITEMS_PER_CATEGORY]


def query_bing_pictures(session) -> List[GlanceImageInfo]:
    results: List[GlanceImageInfo] = []
    for batch in range(BING_ARCHIVE_BATCH_COUNT):
        index = batch * BING_ARCHIVE_BATCH_SIZE
        archive_url = (
            f"https://cn.bing.com/HPImageArchive.aspx?format=js&idx={index}&n={BING_ARCHIVE_BATCH_SIZE}&mkt=zh-CN"
        )
        document = _get_json(session, archive_url)
        for image in (document or {}).get("images") or []:
            if image.get("wp") is False:
                continue
            relative_url = (image.get("url") or "").strip()
            if not relative_url:
                continue
            stable_key = image.get("hsh") or image.get("urlbase") or relative_url
            results.append(
                GlanceImageInfo(
                    id=stable_id(f"bing:{stable_key}"),
                    title=clean_metadata(image.get("title")) or None,
                    author=clean_metadata(image.get("copyright")) or None,
                    license="Bing",
                    licenseUrl="https://www.microsoft.com/zh-cn/bing/bing-wallpaper",
                    sourcePageUrl=to_absolute_bing_url(image.get("copyrightlink")),
                    remoteImageUrl=to_absolute_bing_url(relative_url),
                    pixelWidth=1920,
                    pixelHeight=1080,
                    onlineCategory=GlanceOnlineImageCategory.FEATURED,
                    onlineProvider=GlanceOnlineImageProvider.BING,
                )
            )

    deduped: dict[str, GlanceImageInfo] = {}
    for image in results:
        deduped.setdefault(image.id, image)
    return list(deduped.values())[:REMOTE_CANDIDATE_LIMIT]


def query_category_pictures(session, category: str) -> List[GlanceImageInfo]:
    from datetime import date as _date

    category_title = {
        GlanceOnlineImageCategory.FEATURED: f"Category:Pictures of the day ({_date.today().year})",
        GlanceOnlineImageCategory.LANDSCAPES: "Category:Featured pictures of landscapes",
        GlanceOnlineImageCategory.CITIES: "Category:Quality images of cityscapes",
        GlanceOnlineImageCategory.ARCHITECTURE: "Category:Featured pictures of architecture",
        GlanceOnlineImageCategory.ANIMALS: "Category:Wildlife photography",
        GlanceOnlineImageCategory.PLANTS: "Category:Featured pictures of plants",
        GlanceOnlineImageCategory.ASTRONOMY: "Category:Featured pictures of astronomy",
        GlanceOnlineImageCategory.PEOPLE: "Category:Featured pictures of people",
    }[category]
    category_url = (
        "https://commons.wikimedia.org/w/api.php?action=query&list=categorymembers"
        "&cmtype=file&cmnamespace=6&cmlimit="
        + str(CATEGORY_MEMBER_QUERY_LIMIT)
        + "&format=json&formatversion=2&cmtitle="
        + _quote(category_title)
    )
    document = _get_json(session, category_url)
    file_names: List[str] = []
    for member in ((document or {}).get("query") or {}).get("categorymembers") or []:
        title = (member.get("title") or "").strip()
        if title.lower().startswith("file:"):
            file_names.append(title[5:])
    unique_names = list(dict.fromkeys(file_names))
    random.shuffle(unique_names)
    return query_image_info(session, unique_names[:REMOTE_CANDIDATE_LIMIT], category)


def query_image_info(session, file_names: List[str], category: str) -> List[GlanceImageInfo]:
    results: List[GlanceImageInfo] = []
    for start in range(0, len(file_names), 50):
        chunk = file_names[start : start + 50]
        image_url = (
            "https://commons.wikimedia.org/w/api.php?action=query&prop=imageinfo"
            "&iiprop=url%7Csize%7Cmime%7Cextmetadata"
            "&iiextmetadatafilter=ObjectName%7CImageDescription%7CArtist%7CAttribution%7CLicenseShortName%7CLicenseUrl"
            "&iiurlwidth=1600&format=json&formatversion=2&titles=" + _quote("|".join(f"File:{name}" for name in chunk))
        )
        document = _get_json(session, image_url)
        for page in ((document or {}).get("query") or {}).get("pages") or []:
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            width = info.get("thumbwidth") or info.get("width") or 0
            height = info.get("thumbheight") or info.get("height") or 0
            mime = info.get("mime") or ""
            if (
                width <= 0
                or height <= 0
                or width < height * 1.1
                or mime not in ("image/jpeg", "image/png", "image/webp")
            ):
                continue
            metadata = info.get("extmetadata") or {}
            source_page = info.get("descriptionurl") or ""
            results.append(
                GlanceImageInfo(
                    id=stable_id(source_page),
                    title=clean_metadata(_metadata(metadata, "ObjectName") or _metadata(metadata, "ImageDescription"))
                    or None,
                    author=clean_metadata(_metadata(metadata, "Artist") or _metadata(metadata, "Attribution")) or None,
                    license=clean_metadata(_metadata(metadata, "LicenseShortName")) or None,
                    licenseUrl=_metadata(metadata, "LicenseUrl") or None,
                    sourcePageUrl=source_page,
                    remoteImageUrl=info.get("thumburl") or info.get("url"),
                    pixelWidth=width,
                    pixelHeight=height,
                    onlineCategory=category,
                    onlineProvider=GlanceOnlineImageProvider.WIKIMEDIA,
                )
            )
        if len(results) >= TARGET_CATALOG_SIZE_PER_CATEGORY + 8:
            break
    return results


def create_local_images(paths) -> List[GlanceImageInfo]:
    seen: set[str] = set()
    images: List[GlanceImageInfo] = []
    for path in paths or []:
        if not is_supported_image_path(path) or not os.path.isfile(path):
            continue
        full_path = os.path.abspath(path)
        if full_path.lower() in seen:
            continue
        seen.add(full_path.lower())
        images.append(
            GlanceImageInfo(
                id=stable_id(full_path),
                localPath=full_path,
                title=os.path.splitext(os.path.basename(full_path))[0],
            )
        )
    return images


def create_folder_images(folder_path: Optional[str]) -> List[GlanceImageInfo]:
    if not (folder_path or "").strip() or not os.path.isdir(folder_path):
        return []
    try:
        names = sorted(os.listdir(folder_path))
    except OSError:
        return []
    return create_local_images(os.path.join(folder_path, name) for name in names)


def is_supported_image_path(path: Optional[str]) -> bool:
    if not (path or "").strip():
        return False
    return os.path.splitext(path)[1].lower() in SUPPORTED_IMAGE_EXTENSIONS


def is_usable_file(path: Optional[str]) -> bool:
    return bool((path or "").strip()) and is_supported_image_path(path) and os.path.isfile(path)


def stable_id(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:24]


def clean_metadata(value: Optional[str]) -> str:
    if not (value or "").strip():
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    decoded = html.unescape(without_tags)
    return re.sub(r"\s+", " ", decoded).strip()


def to_absolute_bing_url(value: Optional[str]) -> str:
    if not (value or "").strip():
        return "https://cn.bing.com/"
    value = value.strip()
    if value.startswith(("http://", "https://")):
        return value
    return "https://cn.bing.com/" + value.lstrip("/")


def _get_json(session, url: str) -> Optional[dict]:
    response = session.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _metadata(metadata: dict, name: str) -> Optional[str]:
    item = metadata.get(name) if isinstance(metadata, dict) else None
    if isinstance(item, dict):
        value = item.get("value")
        if value is not None:
            return str(value)
    return None


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _walk(directory: str):
    if not os.path.isdir(directory):
        return
    for directory, _dirs, files in os.walk(directory):
        yield directory, _dirs, files


def _try_delete(path: Optional[str]) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
