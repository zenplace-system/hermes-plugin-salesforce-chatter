"""Convert Markdown to Chatter messageSegments without Hermes dependencies.

Chatter comments do not interpret Markdown, so replace literal Markdown markers
with supported MarkupBegin/MarkupEnd segments. Use only Connect REST input markup:
Paragraph, Bold, Italic, UnorderedList, OrderedList, ListItem, and Code.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any


Segment = dict[str, Any]

# Salesforce stores rich-text comments as HTML and enforces the comment length
# limit on that HTML (measured 2026-10-09: tags, entity-escaped text, links as
# " url ", mentions as "@Display Name").
_HTML_TAGS = {
    "Paragraph": "p", "Bold": "b", "Italic": "i", "Underline": "u", "Strikethrough": "s",
    "UnorderedList": "ul", "OrderedList": "ol", "ListItem": "li", "Code": "code",
}
_ESCAPED = {"&": 5, "<": 4, ">": 4, '"': 6, "'": 6}
# Mention inputs carry an ID but no display name; reserve room for the name.
_MENTION_RESERVE = 256

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
    return trim_trailing_segments(out)


def _tag_length(segment: Segment) -> int:
    tag = _HTML_TAGS.get(segment.get("markupType"), "span")
    return len(tag) + (2 if segment["type"] == "MarkupBegin" else 3)


def _text_length(text: str) -> int:
    return sum(_ESCAPED.get(ch, 1) for ch in text)


def stored_length(segments: list[Segment]) -> int:
    """Length of the HTML Salesforce stores for these segments (what its limit counts)."""
    length = 0
    for segment in segments:
        kind = segment.get("type")
        if kind == "Mention":
            length += max(_MENTION_RESERVE, len(segment.get("text") or ""))
        elif kind == "Text":
            length += _text_length(segment.get("text") or "")
        elif kind == "Link":
            length += _text_length(segment.get("url") or "") + 2
        elif kind in ("MarkupBegin", "MarkupEnd"):
            length += _tag_length(segment)
    return length


def trim_trailing_segments(segments: list[Segment]) -> list[Segment]:
    """Remove trailing whitespace/empty blocks while retaining balanced markup."""
    for last in range(len(segments) - 1, -1, -1):
        segment = segments[last]
        if segment["type"] == "Mention" or (
            segment["type"] == "Text" and segment["text"].strip()
        ):
            break
    else:
        return []
    out = segments[:last + 1]
    if out[-1]["type"] == "Text":
        out[-1] = {**out[-1], "text": out[-1]["text"].rstrip()}
    stack: list[str] = []
    for segment in out:
        if segment["type"] == "MarkupBegin":
            stack.append(segment["markupType"])
        elif segment["type"] == "MarkupEnd":
            stack.pop()
    out.extend(_end(kind) for kind in reversed(stack))
    return out


def _segment_units(segments: list[Segment]) -> Iterator[list[Segment]]:
    """Yield paragraphs, list items, or code lines as preferred split units."""
    unit: list[Segment] = []
    for segment in segments:
        if segment["type"] == "Text":
            for line in segment["text"].splitlines(keepends=True):
                unit.append(_text(line))
                if line.endswith(("\n", "\r")):
                    yield unit
                    unit = []
        else:
            unit.append(segment)
            if segment["type"] == "MarkupEnd" and segment["markupType"] in {
                "Paragraph", "ListItem", "Code",
            }:
                yield unit
                unit = []
    if unit:
        yield unit


def split_segments(segments: list[Segment], max_length: int) -> list[list[Segment]]:
    """Split converted text, closing/reopening active markup at each boundary.

    Hermes' Markdown splitter preserves code fences, but cannot preserve Chatter
    markup or measure the HTML Salesforce stores. Split the actual segments so
    each chunk's stored length (see ``stored_length``) stays within ``max_length``.
    Boundary whitespace is trimmed; hard splits retain word spacing.
    """
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    stack: list[str] = []
    length = 0
    has_text = False

    def closing() -> int:
        return sum(_tag_length(_end(kind)) for kind in stack)

    def flush() -> None:
        nonlocal current, length, has_text
        finished = trim_trailing_segments(current)
        if finished:
            chunks.append(finished)
        current = [_begin(kind) for kind in stack]
        length = stored_length(current)
        has_text = False

    for unit in _segment_units(segments):
        if has_text and length + stored_length(unit) + closing() > max_length:
            flush()
        for segment in unit:
            kind = segment["type"]
            if kind == "Text":
                value = segment["text"]
                while value:
                    available = max_length - length - closing()
                    end = used = 0
                    for ch in value:
                        cost = _ESCAPED.get(ch, 1)
                        if used + cost > available:
                            break
                        used += cost
                        end += 1
                    if end == 0:
                        if not has_text:
                            raise ValueError("max_length cannot fit text inside its markup")
                        flush()
                        continue
                    if end < len(value):
                        end = len(value[:end].rstrip()) or end
                    part = value[:end]
                    current.append(_text(part))
                    length += _text_length(part)
                    has_text = has_text or bool(part.strip())
                    value = value[end:]
                    if value:
                        flush()
                continue
            cost = stored_length([segment])
            if kind == "Mention" and cost + closing() > max_length:
                raise ValueError("max_length cannot fit a mention")
            reserve = _tag_length(_end(segment["markupType"])) if kind == "MarkupBegin" else 0
            if kind != "MarkupEnd" and has_text and length + cost + reserve + closing() > max_length:
                flush()
            if kind == "MarkupBegin":
                stack.append(segment["markupType"])
            elif kind == "MarkupEnd":
                stack.pop()
            else:
                has_text = True
            length += cost
            current.append(segment)
    flush()
    return chunks
