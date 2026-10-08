"""Chatter messageSegments helpers without Hermes dependencies."""

from __future__ import annotations

import re
from typing import Any

from .richtext import markdown_to_segments, split_segments, stored_length

_BLOCK_END = {"Paragraph", "ListItem"}


def same_sf_id(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a[:15] == b[:15]


def _segments(body: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not body:
        return []
    segs = body.get("messageSegments")
    return segs if isinstance(segs, list) else []


def mentions_user(body: dict[str, Any] | None, user_id: str) -> bool:
    for seg in _segments(body):
        if seg.get("type") == "Mention" and same_sf_id((seg.get("record") or {}).get("id"), user_id):
            return True
    return False


def plain_text(body: dict[str, Any] | None, *, drop_mention_of: str | None = None) -> str:
    parts: list[str] = []
    for seg in _segments(body):
        kind = seg.get("type")
        if kind == "MarkupEnd":
            if seg.get("markupType") in _BLOCK_END:
                parts.append("\n")
            continue
        if kind == "MarkupBegin":
            continue
        if kind == "Mention" and drop_mention_of and same_sf_id((seg.get("record") or {}).get("id"), drop_mention_of):
            continue
        parts.append(seg.get("text") or "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(parts).split("\n")]
    return "\n".join(line for line in lines if line)


def reply_body(requester_id: str | None, markdown: str, *, empty_reply_text: str = "(Empty reply)") -> dict[str, Any]:
    """Mention the requester first and convert Markdown to Chatter formatting."""
    segments: list[dict[str, Any]] = []
    if requester_id:
        segments += [{"type": "Mention", "id": requester_id}, {"type": "Text", "text": " "}]
    body = markdown_to_segments(markdown)
    segments += body or [{"type": "Text", "text": empty_reply_text.rstrip()}]
    return {"body": {"messageSegments": segments}}


def reply_bodies(
    requester_id: str | None,
    text: str,
    *,
    max_length: int = 9000,
    empty_reply_text: str = "(Empty reply)",
) -> list[dict[str, Any]]:
    """Build bounded, balanced reply bodies with the requester mentioned once.

    Lengths are measured as the HTML Salesforce stores (its comment limit counts
    that). The first chunk reserves room for the unresolved mention name plus its
    separating space. Subsequent chunks contain only reply content.
    """
    prefix_length = stored_length([{"type": "Mention", "id": requester_id}]) + 1 if requester_id else 0
    if max_length <= prefix_length:
        raise ValueError("max_length must leave room for reply text after the mention")
    segments = reply_body(requester_id, text, empty_reply_text=empty_reply_text)["body"]["messageSegments"]
    return [{"body": {"messageSegments": chunk}} for chunk in split_segments(segments, max_length)]
