"""Localization: flat dot-key string tables reused verbatim from PaneBox.

Resolution mirrors PaneBox LocalizationService: current culture -> zh-CN
fallback (the product default language) -> raw key.
"""

from __future__ import annotations

import json
import locale
import threading
from pathlib import Path

SUPPORTED_LANGUAGES = (
    "en-US",
    "zh-CN",
    "zh-TW",
    "ja-JP",
    "de-DE",
    "pt-BR",
    "hi-IN",
    "es-ES",
    "fr-FR",
    "ar-SA",
    "bn-BD",
    "ru-RU",
)
DEFAULT_LANGUAGE = "zh-CN"

_lock = threading.Lock()
_tables: dict[str, dict[str, str]] = {}
_current: str | None = None  # None = follow system


def _strings_dir() -> Path:
    override = None
    import os

    override = os.environ.get("PANEBOX_STRINGS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "strings"


def _load(culture: str) -> dict[str, str]:
    with _lock:
        table = _tables.get(culture)
        if table is not None:
            return table
    path = _strings_dir() / f"{culture}.json"
    data: dict[str, str] = {}
    if path.exists():
        try:
            data = {str(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError):
            data = {}
    with _lock:
        _tables[culture] = data
    return data


def _system_language() -> str:
    codes = []
    import os

    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            codes.extend(part for part in value.split(":") if part)
    try:
        codes.append(locale.getdefaultlocale()[0] or "")
    except Exception:
        pass
    for code in codes:
        code = code.strip().replace("-", "-")
        if not code or code in ("C", "POSIX"):
            continue
        if code in SUPPORTED_LANGUAGES:
            return code
        base = code.split("_")[0].split("-")[0].lower()
        for cand in SUPPORTED_LANGUAGES:
            if cand.split("-")[0].lower() == base:
                return cand
    return "en-US"


def set_language(culture: str | None) -> None:
    """Set explicit culture ('System' or None resets to system-following)."""
    global _current
    if not culture or culture == "System":
        _current = None
    else:
        _current = culture if culture in SUPPORTED_LANGUAGES else _system_language()


def current_language() -> str:
    return _current if _current else _system_language()


def t(key: str) -> str:
    table = _load(current_language())
    value = table.get(key)
    if value is not None:
        return value
    if current_language() != DEFAULT_LANGUAGE:
        fallback = _load(DEFAULT_LANGUAGE).get(key)
        if fallback is not None:
            return fallback
    return key


def fmt(key: str, *args, **kwargs) -> str:
    text = t(key)
    try:
        return text.format(*args, **kwargs) if (args or kwargs) else text
    except (IndexError, KeyError):
        return text


def api_language_code() -> str:
    """Two-letter language code for external APIs (weather etc.)."""
    return current_language().split("-")[0].lower()


def available_tables() -> dict[str, int]:
    return {culture: len(_load(culture)) for culture in SUPPORTED_LANGUAGES}
