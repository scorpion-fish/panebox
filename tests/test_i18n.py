"""Localization contract: key parity across all 12 locales + fallback chain.

Mirrors PaneBox's localization parity tests: every Strings/*.json must carry
the same key set, and lookups fall back to zh-CN (product default) then key.
"""

from __future__ import annotations

import json
from pathlib import Path

from panebox import i18n

STRINGS_DIR = Path(__file__).resolve().parent.parent / "strings"


def _table(culture: str) -> dict[str, str]:
    return json.loads((STRINGS_DIR / f"{culture}.json").read_text(encoding="utf-8"))


def test_all_supported_locales_shipped():
    for culture in i18n.SUPPORTED_LANGUAGES:
        assert (STRINGS_DIR / f"{culture}.json").exists(), f"missing {culture}.json"


def test_key_parity_across_locales():
    reference = set(_table(i18n.DEFAULT_LANGUAGE))
    assert reference, "zh-CN table empty"
    for culture in i18n.SUPPORTED_LANGUAGES:
        assert set(_table(culture)) == reference, (
            f"{culture} key set differs from {i18n.DEFAULT_LANGUAGE}: "
            f"missing={sorted(reference - set(_table(culture)))[:5]} "
            f"extra={sorted(set(_table(culture)) - reference)[:5]}"
        )


def test_fallback_to_default_language():
    i18n.set_language("ja-JP")
    try:
        # A key present everywhere resolves from the current table.
        assert i18n.t("Tray.Exit") == _table("ja-JP")["Tray.Exit"]
        # A key dropped from the current table still resolves via zh-CN.
        monkey_table = {"Tray.Exit": "閉じる"}
        original = i18n._tables.get("ja-JP")
        i18n._tables["ja-JP"] = monkey_table
        try:
            assert i18n.t("Tray.Exit") == monkey_table["Tray.Exit"]
            assert i18n.t("Settings.WindowTitle") == _table("zh-CN")["Settings.WindowTitle"]
        finally:
            if original is None:
                i18n._tables.pop("ja-JP", None)
            else:
                i18n._tables["ja-JP"] = original
    finally:
        i18n.set_language(None)


def test_unknown_key_returns_key():
    i18n.set_language("en-US")
    try:
        assert i18n.t("No.Such.Key.Exists") == "No.Such.Key.Exists"
    finally:
        i18n.set_language(None)


def test_fmt_positional_substitution():
    i18n.set_language("zh-CN")
    try:
        text = i18n.fmt("Widget.Empty.ManagedText", "移动", "我的桌面")
        assert text == "把文件拖到这里，PaneBox 会自动移动到 我的桌面。"
        # Missing args leave the template intact rather than raising.
        assert "{0}" in i18n.fmt("Widget.Empty.ManagedText")
    finally:
        i18n.set_language(None)


def test_set_language_system_resets_to_environment():
    i18n.set_language("de-DE")
    assert i18n.current_language() == "de-DE"
    i18n.set_language("System")
    assert i18n.current_language() == i18n._system_language()
