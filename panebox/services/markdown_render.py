"""Tiny Markdown → Pango markup renderer for Quick Capture preview mode.

Supports the subset the PaneBox notes actually use (headings, bold/italic,
inline code, links, bullet/task lists, fenced code, blockquotes, rules).
Escapes everything first; Pango spans carry the emphasis. Unknown syntax
falls through as plain text — a note must render, never crash.
"""

from __future__ import annotations

import re
from html import escape

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__")
_ITALIC = re.compile(r"\*([^*\n]+)\*|_([^_\n]+)_")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)\)")


def to_pango_markup(text: str) -> str:
    lines_out: list[str] = []
    in_code_block = False
    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            lines_out.append(
                f"<span font_family='monospace' background='alpha(currentColor,0.08)'>{escape(raw_line)}</span>"
            )
            continue

        if not stripped:
            lines_out.append("")
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            lines_out.append("<span alpha='50%'>──────────────</span>")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            size = max(8, 18 - 2 * level) / 10.0
            lines_out.append(f"<span size='{size * 1000:.0f}' weight='bold'>{_inline(heading.group(2))}</span>")
            continue

        task = re.match(r"^[-*]\s+\[( |x|X)\]\s+(.*)$", stripped)
        if task:
            mark = "☑" if task.group(1).lower() == "x" else "☐"
            lines_out.append(f"{mark} {_inline(task.group(2))}")
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            lines_out.append(f"• {_inline(bullet.group(1))}")
            continue

        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            index = re.match(r"^(\d+)", stripped).group(1)
            lines_out.append(f"{index}. {_inline(numbered.group(1))}")
            continue

        quote = re.match(r"^&gt;\s*(.*)$", escape(stripped))
        if quote:
            lines_out.append(f"<span alpha='70%' style='italic'>› {_inline(_unescape(quote.group(1)))}</span>")
            continue

        lines_out.append(_inline(stripped))

    return "\n".join(lines_out)


def plain_text(text: str) -> str:
    """Strip markdown syntax for one-line previews."""
    value = text or ""
    value = _IMAGE.sub(lambda m: m.group(1) or "🖼", value)
    value = _LINK.sub(lambda m: m.group(1), value)
    value = _CODE_SPAN.sub(lambda m: m.group(1), value)
    value = _BOLD.sub(lambda m: m.group(1) or m.group(2), value)
    value = _ITALIC.sub(lambda m: m.group(1) or m.group(2), value)
    value = re.sub(r"^#{1,6}\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^[-*+]\s+\[( |x|X)\]\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^[-*+]\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\d+[.)]\s+", "", value, flags=re.MULTILINE)
    return value.strip()


def _unescape(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#x27;", "'")
    )


def _inline(segment: str) -> str:
    escaped = escape(segment)
    escaped = _IMAGE.sub(lambda m: f"[{escape(m.group(1)) or '🖼'}]", escaped)
    escaped = _LINK.sub(lambda m: f"<a href='{m.group(2)}'>{m.group(1)}</a>", escaped)
    escaped = _CODE_SPAN.sub(
        lambda m: f"<span font_family='monospace' background='alpha(currentColor,0.08)'>{m.group(1)}</span>",
        escaped,
    )
    escaped = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", escaped)
    escaped = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", escaped)
    return escaped
