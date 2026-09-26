"""Widget-style backup projection (port of Services/WidgetStyleBackupProjection.cs).

Serializes the appearance-relevant subset of settings into a portable JSON
document shipped inside scoped cloud backups, and applies such a document back
onto live settings. Keys outside the whitelists are ignored on apply — a
foreign or newer document can never smuggle arbitrary settings fields in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Exact whitelist from WidgetStyleBackupProjection.cs — cross-version stable.
SHELL_KEYS = (
    "widgetOpacity",
    "widgetMaterialType",
    "widgetMaterialIntensity",
    "widgetForegroundMode",
    "widgetForegroundColor",
    "widgetBorderColorMode",
    "widgetBorderStyle",
    "widgetCornerPreference",
    "widgetAnimationEffect",
    "widgetAnimationSpeed",
    "widgetAnimationSlideDirection",
    "widgetAnimationEasingIntensity",
    "displayWidgetChromeMode",
    "interactiveWidgetChromeMode",
    "widgetTitleIconMode",
    "showHoverButtons",
    "widgetHoverButtonActions",
    "widgetCollapsedStyle",
    "widgetCompactContentMode",
    "widgetCompactHideSensitiveContent",
    "widgetCompactMediaCornerMode",
    "widgetCompactAnimationEffect",
    "widgetCompactAnimationDurationMs",
)
WIDGET_KEYS = ("name", "isDefaultTitle", "viewMode", "sortMode", "sortDescending")
# widgetKind travels in the document for identity verification only.


def serialize(settings_service, source_device_id: str) -> dict[str, Any]:
    """Live settings → widget-style document ({} when there is nothing to say)."""
    shell = settings_service.settings.widgetShell
    shell_doc = {key: getattr(shell, key) for key in SHELL_KEYS if hasattr(shell, key)}
    widgets_doc: dict[str, Any] = {}
    for widget in settings_service.layout.widgets:
        entry: dict[str, Any] = {"widgetKind": widget.widgetKind}
        entry.update({key: getattr(widget, key) for key in WIDGET_KEYS if hasattr(widget, key)})
        widgets_doc[widget.id] = entry
    if not shell_doc and not widgets_doc:
        return {}
    return {
        "schemaVersion": 1,
        "kind": "widget-style",
        "createdAtUtc": datetime.now(timezone.utc).isoformat(),
        "sourceDeviceId": source_device_id,
        "shell": shell_doc,
        "widgets": widgets_doc,
    }


def apply(document: Any, settings_service) -> int:
    """Apply a widget-style document onto live settings; returns patched keys.

    Widget entries match by id and are kind-verified — a widget that changed
    kind since the snapshot keeps its current style. Unknown ids are skipped:
    the widget no longer exists here.
    """
    if not isinstance(document, dict):
        raise ValueError("widget-style document is not an object")
    if document.get("kind") != "widget-style":
        raise ValueError("document is not a widget-style projection")
    schema = document.get("schemaVersion")
    if not isinstance(schema, int) or schema != 1:
        raise ValueError(f"unsupported widget-style schema version: {schema!r}")

    settings = settings_service.settings
    patched = 0
    shell_doc = document.get("shell")
    if isinstance(shell_doc, dict):
        shell = settings.widgetShell
        patched += _patch_object(shell, shell_doc, SHELL_KEYS)

    widgets_doc = document.get("widgets")
    if isinstance(widgets_doc, dict):
        by_id = {w.id: w for w in settings_service.layout.widgets}
        for widget_id, entry in widgets_doc.items():
            widget = by_id.get(str(widget_id))
            if widget is None or not isinstance(entry, dict):
                continue
            kind = entry.get("widgetKind")
            if kind and str(kind).lower() != (widget.widgetKind or "").lower():
                continue  # same id, different feature — not this widget's style
            patched += _patch_object(widget, entry, WIDGET_KEYS)
    return patched


def _patch_object(target, values: dict, allowed: tuple) -> int:
    patched = 0
    for key in allowed:
        if key not in values or not hasattr(target, key):
            continue
        setattr(target, key, _coerce(getattr(target, key), values[key]))
        patched += 1
    return patched


def _coerce(current: Any, value: Any) -> Any:
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    return value if isinstance(value, str) else current
