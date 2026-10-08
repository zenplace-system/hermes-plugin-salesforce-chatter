"""Find bot mentions and opt-in thread follow-ups through two paths.

To Me feed covers group and record mentions, polling every 60 seconds per Connect
REST guidance. SOQL CollaborationGroupFeed probes allowlisted groups more often.
Comments update the parent post's LastModifiedDate (observed in a production org
on 2026-10-08), allowing comment mentions to be detected. SOQL consumes the org API
quota rather than the Chatter REST hourly limit.

Unmentioned follow-ups require an allowlisted author, a strictly earlier bot
comment, and no mention of another user. Explicit mentions keep their usual
authorization handling in the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .segments import mentions_user, plain_text, same_sf_id
from .settings import Settings

FEED_PATHS = {"to_me": "chatter/feeds/to/me/feed-elements", "news": "chatter/feeds/news/me/feed-elements"}
MAX_FEED_PAGES = 4
MAX_COMMENT_PAGES = 5
CONTEXT_COMMENTS = 10


class ChatterReader(Protocol):
    async def get_json(self, path: str) -> dict[str, Any]: ...
    async def query(self, soql: str) -> list[dict[str, Any]]: ...
    async def fetch_feed_elements(self, ids: list[str]) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Attachment:
    file_id: str
    title: str
    mime_type: str
    download_url: str
    size: int


@dataclass(frozen=True)
class Candidate:
    source_id: str
    kind: str
    feed_element_id: str
    parent_id: str | None
    parent_name: str | None
    requester_id: str
    requester_name: str | None
    created_at: datetime
    text: str
    post_text: str
    thread_context: str
    attachments: tuple[Attachment, ...] = field(default=())
    follow_up_without_mention: bool = False


def parse_sf_datetime(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    if len(value) >= 5 and value[-5] in "+-" and value[-3] != ":":
        value = value[:-2] + ":" + value[-2:]  # SOQL uses the +0000 offset format.
    return datetime.fromisoformat(value)


def _soql_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attachments(obj: dict[str, Any]) -> tuple[Attachment, ...]:
    caps = obj.get("capabilities") or {}
    raw: list[dict[str, Any]] = list(((caps.get("files") or {}).get("items")) or [])
    content = caps.get("content")
    if isinstance(content, dict) and content.get("id"):
        raw.append(content)
    out: list[Attachment] = []
    seen: set[str] = set()
    for f in raw:
        fid = str(f.get("id") or "")
        url = f.get("downloadUrl") or ""
        if not fid or not url or fid in seen:
            continue
        seen.add(fid)
        title = str(f.get("title") or "file")
        ext = f.get("fileExtension")
        if ext and not title.lower().endswith("." + str(ext).lower()):
            title = f"{title}.{ext}"
        size = f.get("contentSize") or f.get("fileSize") or 0
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        out.append(Attachment(fid, title, str(f.get("mimeType") or ""), url, size))
    return tuple(out)


async def _comments(client: ChatterReader, element: dict[str, Any]) -> list[dict[str, Any]]:
    page = ((element.get("capabilities") or {}).get("comments") or {}).get("page") or {}
    items = list(page.get("items") or [])
    if int(page.get("total") or len(items)) <= len(items):
        return items
    items = []
    url: str | None = f"chatter/feed-elements/{element['id']}/capabilities/comments/items?pageSize=100"
    for _ in range(MAX_COMMENT_PAGES):
        if not url:
            break
        data = await client.get_json(url)
        items.extend(data.get("items") or [])
        url = data.get("nextPageUrl")
    return items


def _speaker(comment: dict[str, Any]) -> str:
    text = plain_text(comment.get("body"))
    files = _attachments(comment)
    if files:
        text += " [attachments: " + ", ".join(f.title for f in files) + "]"
    return f"{(comment.get('user') or {}).get('displayName', '')}: {text}"


async def _from_element(client: ChatterReader, s: Settings, el: dict[str, Any], since: datetime) -> list[Candidate]:
    if el.get("feedElementType") != "FeedItem":
        return []
    parent = el.get("parent") or {}
    if s.allowed_parent_ids and not any(same_sf_id(parent.get("id"), p) for p in s.allowed_parent_ids):
        return []
    bot = s.bot_user_id
    post_text = plain_text(el.get("body"), drop_mention_of=bot)
    post_files = _attachments(el)
    actor = el.get("actor") or {}
    out: list[Candidate] = []
    created = parse_sf_datetime(el["createdDate"])
    if created >= since and not same_sf_id(actor.get("id"), bot) and mentions_user(el.get("body"), bot):
        out.append(Candidate(el["id"], "post", el["id"], parent.get("id"), parent.get("name"), actor.get("id", ""),
                             actor.get("displayName"), created, post_text, post_text, "", post_files))
    comments = sorted(await _comments(client, el), key=lambda c: parse_sf_datetime(c["createdDate"]))
    first_bot_comment: datetime | None = None
    for index, cm in enumerate(comments):
        user = cm.get("user") or {}
        cm_created = parse_sf_datetime(cm["createdDate"])
        if same_sf_id(user.get("id"), bot):
            if first_bot_comment is None:
                first_bot_comment = cm_created
            continue
        if cm_created < since:
            continue
        body = cm.get("body") or {}
        follow_up = not mentions_user(body, bot)
        if follow_up:
            if (
                not s.follow_up_without_mention
                or first_bot_comment is None
                or first_bot_comment >= cm_created
                or not any(same_sf_id(user.get("id"), allowed) for allowed in s.allowed_user_ids)
            ):
                continue
            if any(
                seg.get("type") == "Mention"
                and str((seg.get("record") or {}).get("id") or "").startswith("005")
                and not same_sf_id((seg.get("record") or {}).get("id"), bot)
                for seg in body.get("messageSegments") or []
            ):
                continue
        earlier = comments[max(0, index - CONTEXT_COMMENTS):index]
        context = "\n".join(_speaker(c) for c in earlier)
        # Include parent attachments so comments can ask about an image in the post.
        files = _attachments(cm) + tuple(f for f in post_files if f.file_id not in {a.file_id for a in _attachments(cm)})
        out.append(Candidate(cm["id"], "comment", el["id"], parent.get("id"), parent.get("name"), user.get("id", ""),
                             user.get("displayName"), cm_created, plain_text(cm.get("body"), drop_mention_of=bot),
                             post_text, context, files, follow_up_without_mention=follow_up))
    return out


async def scan_feed(client: ChatterReader, settings: Settings, since: datetime) -> list[Candidate]:
    out: list[Candidate] = []
    url: str | None = f"{FEED_PATHS[settings.feed]}?sort=LastModifiedDateDesc&pageSize=25&recentCommentCount=25"
    for _ in range(MAX_FEED_PAGES):
        if not url:
            break
        page = await client.get_json(url)
        stop = False
        for el in page.get("elements") or []:
            if parse_sf_datetime(el["modifiedDate"]) < since:
                stop = True
                break
            out.extend(await _from_element(client, settings, el, since))
        url = None if stop else page.get("nextPageUrl")
    return sorted(out, key=lambda c: c.created_at)


async def probe_groups(client: ChatterReader, settings: Settings, since: datetime) -> list[Candidate]:
    """Find candidates in allowlisted group posts updated since the cursor."""
    groups = settings.group_ids
    if not groups:
        return []
    id_list = ",".join(f"'{g}'" for g in groups)
    records = await client.query(
        "SELECT Id FROM CollaborationGroupFeed "
        f"WHERE ParentId IN ({id_list}) AND LastModifiedDate >= {_soql_datetime(since)} "
        "ORDER BY LastModifiedDate DESC LIMIT 50"
    )
    ids = [str(r["Id"]) for r in records if r.get("Id")]
    if not ids:
        return []
    out: list[Candidate] = []
    for el in await client.fetch_feed_elements(ids):
        out.extend(await _from_element(client, settings, el, since))
    return sorted(out, key=lambda c: c.created_at)
