"""Copy-to-clipboard text shaping (port of QuickCaptureClipboardFormatter).

Content first, then an attachment manifest (display name + path) — the same
layout PaneBox puts on the Windows clipboard.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Sequence


def format_item_copy_text(item) -> str:
    """Formatter.FormatSingle for one QuickCaptureItem."""
    content = ((item.title + "\n\n" if item.title else "") + (item.body or "")).strip()
    return format_content(content, item.attachments, include_content_label=bool(item.attachments))


def format_content(content: str, attachments: Iterable, include_content_label: bool = True) -> str:
    normalized = (content or "").strip()
    attachment_list = [a for a in attachments if a is not None and (a.filePath or "").strip()]
    if not attachment_list:
        return normalized

    from ..i18n import t

    sections: List[str] = []
    if normalized:
        sections.append(f"{t('Clipboard.ContentLabel')}\n{normalized}" if include_content_label else normalized)
    sections.append(_format_attachments(attachment_list))
    return "\n\n".join(sections)


def format_batch(items: Sequence) -> str:
    from ..i18n import fmt

    return "\n\n---\n\n".join(
        "\n".join(
            [
                fmt("QuickCapture.Copy.ItemHeader", index + 1),
                format_item_copy_text(item),
            ]
        )
        for index, item in enumerate(items)
    )


def _format_attachments(attachments: Sequence) -> str:
    from ..i18n import fmt

    lines = [fmt("Clipboard.Attachments", len(attachments))]
    for attachment in attachments:
        display_name = (attachment.displayName or "").strip() or os.path.basename(attachment.filePath)
        lines.append(f"- {display_name}")
        lines.append(f"  {fmt('Clipboard.Path', attachment.filePath)}")
    return "\n".join(lines)
