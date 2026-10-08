"""Convert Markdown to Chatter messageSegments without Hermes dependencies.

Chatter comments do not interpret Markdown, so replace literal Markdown markers
with supported MarkupBegin/MarkupEnd segments. Use only Connect REST input markup:
Paragraph, Bold, Italic, UnorderedList, OrderedList, ListItem, and Code.
"""

from __future__ import annotations

import re
from typing import Any

Segment = dict[str, Any]

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_INLINE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|`([^`]+)`|\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _begin(kind: str) -> Segment:
    return {"type": "MarkupBegin", "markupType": kind}


def _end(kind: str) -> Segment:
    return {"type": "MarkupEnd", "markupType": kind}


def _text(value: str) -> Segment:
    return {"type": "Text", "text": value}


def inline_segments(line: str) -> list[Segment]:
    """Convert one line of bold, italic, inline code, and links to segments."""
    out: list[Segment] = []
    pos = 0
    for m in _INLINE.finditer(line):
        if m.start() > pos:
            out.append(_text(line[pos:m.start()]))
        bold = m.group(1) or m.group(2)
        italic = m.group(3)
        code = m.group(4)
        if bold:
            out += [_begin("Bold"), _text(bold), _end("Bold")]
        elif italic:
            out += [_begin("Italic"), _text(italic), _end("Italic")]
        elif code:
            out.append(_text(code))
        else:
            out.append(_text(f"{m.group(5)} {m.group(6)}"))
        pos = m.end()
    if pos < len(line):
        out.append(_text(line[pos:]))
    return [s for s in out if s.get("type") != "Text" or s["text"]]


def _paragraph(line: str, *, bold: bool = False) -> list[Segment]:
    inner = inline_segments(line.strip())
    if not inner:
        return []
    if bold:
        inner = [_begin("Bold"), *[s for s in inner if s["type"] == "Text"], _end("Bold")]
    return [_begin("Paragraph"), *inner, _end("Paragraph")]


def _table_row(line: str) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return " / ".join(c for c in cells if c)


def markdown_to_segments(markdown: str) -> list[Segment]:
    """Convert Markdown to Chatter segments; empty input returns an empty list."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[Segment] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            body: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence
            code = "\n".join(body).strip("\n")
            if code:
                out += [_begin("Code"), _text(code), _end("Code")]
            continue
        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if bullet or numbered:
            kind = "UnorderedList" if bullet else "OrderedList"
            pattern = _BULLET if bullet else _NUMBERED
            items: list[str] = []
            while i < len(lines) and pattern.match(lines[i]):
                items.append(pattern.match(lines[i]).group(1))
                i += 1
            out.append(_begin(kind))
            for item in items:
                out += [_begin("ListItem"), *inline_segments(item), _end("ListItem")]
            out.append(_end(kind))
            continue
        if line.strip().startswith("|"):
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _TABLE_RULE.match(lines[i]):
                    out += _paragraph(_table_row(lines[i]))
                i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            out += _paragraph(heading.group(1), bold=True)
        elif line.strip() and not re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            out += _paragraph(line)
        i += 1
    return out


def segments_text_length(segments: list[Segment]) -> int:
    return sum(len(s.get("text") or "") for s in segments)
