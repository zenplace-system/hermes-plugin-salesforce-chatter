"""Convert Markdown to Chatter messageSegments without Hermes dependencies.

Chatter comments do not interpret Markdown, so replace literal Markdown markers
with supported MarkupBegin/MarkupEnd segments. Use only Connect REST input markup:
Paragraph, Bold, Italic, UnorderedList, OrderedList, ListItem, and Code.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any


_TEXT_BREAKS = {"Paragraph", "ListItem"}
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
    return trim_trailing_segments(out)


def segments_text_length(segments: list[Segment]) -> int:
    """Budget text, paragraph/list newlines, and unresolved mention names.

    Mention inputs have an ID but no display name, so reserve 256 characters
    (or a longer returned mention text). Formatting tags themselves cost zero.
    """
    length = 0
    for segment in segments:
        kind = segment.get("type")
        if kind == "Mention":
            length += max(256, len(segment.get("text") or ""))
        elif kind == "Text":
            length += len(segment.get("text") or "")
        elif kind == "MarkupEnd" and segment.get("markupType") in _TEXT_BREAKS:
            length += 1
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
    markup or measure expanded links and mentions. Split the actual segments
    instead. Boundary whitespace is trimmed; hard splits retain word spacing.
    """
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    stack: list[str] = []
    length = 0
    has_text = False

    def flush() -> None:
        nonlocal current, length, has_text
        finished = trim_trailing_segments(current)
        if finished:
            chunks.append(finished)
        current = [_begin(kind) for kind in stack]
        length = 0
        has_text = False

    for unit in _segment_units(segments):
        if has_text and length + segments_text_length(unit) > max_length:
            flush()
        for segment in unit:
            kind = segment["type"]
            if kind == "MarkupBegin":
                stack.append(segment["markupType"])
            elif kind == "MarkupEnd":
                if stack.pop() in _TEXT_BREAKS:
                    length += 1
            elif kind == "Text":
                value = segment["text"]
                while value:
                    closing_length = sum(markup in _TEXT_BREAKS for markup in stack)
                    available = max_length - length - closing_length
                    if available <= 0:
                        if not has_text:
                            raise ValueError("max_length cannot fit text and its paragraph break")
                        flush()
                        available = max_length - closing_length
                        if available <= 0:
                            raise ValueError("max_length cannot fit text and its paragraph break")
                    end = min(len(value), available)
                    if end < len(value):
                        end = len(value[:end].rstrip()) or end
                    part = value[:end]
                    current.append(_text(part))
                    length += len(part)
                    has_text = has_text or bool(part.strip())
                    value = value[end:]
                    if value:
                        flush()
                continue
            elif kind == "Mention":
                mention_length = segments_text_length([segment])
                if mention_length > max_length:
                    raise ValueError("max_length cannot fit a mention")
                if length + mention_length > max_length:
                    flush()
                length += mention_length
            current.append(segment)
    flush()
    return chunks
