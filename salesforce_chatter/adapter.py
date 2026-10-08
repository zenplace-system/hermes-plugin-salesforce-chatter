"""Hermes gateway integration; Salesforce operations live in sfchatter/.

Use only documented Hermes APIs covered by the plugin compatibility contract.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import mimetypes
import re
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path


import httpx
from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult, cache_media_bytes
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.status import acquire_scoped_lock, release_scoped_lock
from plugins.plugin_storage import plugin_data_dir

from .sfchatter.client import ChatterClient, ChatterHTTPError
from .sfchatter.inbox import Claim, Inbox, utc_now
from .sfchatter.preview import render_html_preview
from .sfchatter.scanner import Candidate, probe_groups, scan_feed
from .sfchatter.segments import reply_body, reply_bodies, same_sf_id
from .sfchatter.media import download_image
from .sfchatter.settings import Settings

logger = logging.getLogger(__name__)

PLATFORM_NAME = "salesforce_chatter"
PLUGIN_ID = "salesforce-chatter"
SECRET_NAMES = (
    "SF_CHATTER_LOGIN_URL",
    "SF_CHATTER_CLIENT_ID",
    "SF_CHATTER_USERNAME",
    "SF_CHATTER_PRIVATE_KEY_PATH",
    "SF_CHATTER_BOT_USER_ID",
    "SF_CHATTER_ALLOWED_PARENT_IDS",
    "SF_CHATTER_ALLOWED_USERS",
)
# Allow conversation boundaries, cancellation, and approval replies, not host commands.
ALLOWED_COMMANDS = frozenset({"new", "reset", "stop", "approve", "deny"})
SCAN_OVERLAP = timedelta(minutes=5)
_REMOTE_PREFIX = re.compile(r"^remote_[0-9a-f]{8,}_")
PROBE_OVERLAP = timedelta(minutes=2)
TO_ME_CURSOR = "scan_since"
PROBE_CURSOR = "probe_since"
PLATFORM_HINT = (
    "You are replying inside a Salesforce Chatter thread that every member of the group can read. "
    "Your reply is posted as a comment in the same thread. "
    "Chatter renders paragraphs, bold, italic, bullet and numbered lists, and code blocks; tables and "
    "headings are flattened, so prefer short paragraphs and lists. Keep answers concise. "
    "Earlier comments of the thread are given as context; use them when the question refers to them. "
    "Images and files attached to the post or comment are provided as local files. "
    "To attach a file (HTML, image, PDF, CSV, etc.), first save it to a file, then write "
    "MEDIA:/absolute/path/to/file on its own line in your reply; it is uploaded to Chatter and attached "
    "to a comment in this thread. Never paste a whole HTML document into the reply text. "
    "Do not include personal data that is not already in the question."
)


def _settings(config: PlatformConfig) -> Settings:
    # Read profile-scoped secrets to avoid another profile's environment in multiplex mode.
    env = {name: get_scoped_secret(name, "") or "" for name in SECRET_NAMES}
    return Settings.load(env, config.extra or {})


def check_requirements() -> bool:
    try:
        import httpx  # noqa: F401
        import jwt  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config: PlatformConfig) -> bool:
    try:
        _settings(config)
    except ValueError:
        return False
    return True


class SalesforceChatterAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 9000
    splits_long_messages = True
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform(PLATFORM_NAME))
        self._settings = _settings(config)
        self._client: ChatterClient | None = None
        self._inbox: Inbox | None = None
        self._poll_task: asyncio.Task | None = None
        # Mention the requester only in the first comment to avoid duplicate notifications.
        # chat_id -> (accepted post/comment ID, requester ID)
        self._mention_once: dict[str, tuple[str, str]] = {}
        self._bot_name = "{bot}"
        self._inline_reply = ContextVar("chatter_inline_reply", default=False)
        self._replied_sources: set[str] = set()

    # ---- Connection ---------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        s = self._settings
        acquired, _existing = acquire_scoped_lock(PLATFORM_NAME, s.bot_user_id[:15], metadata={"platform": PLATFORM_NAME})
        if not acquired:
            logger.error("salesforce_chatter: bot user already in use by another gateway")
            return False
        self._inbox = Inbox(plugin_data_dir(PLUGIN_ID) / "inbox.sqlite")
        self._client = ChatterClient(s)
        try:
            await self._client.authenticate()
        except (ChatterHTTPError, OSError, KeyError) as exc:
            logger.error("salesforce_chatter: authentication failed (%s)", type(exc).__name__)
            self._set_fatal_error("auth_failed", "Salesforce authentication failed", retryable=True)
            await self._client.aclose()
            release_scoped_lock(PLATFORM_NAME, s.bot_user_id[:15])
            return False
        try:
            me = await self._client.get_json("chatter/users/me")
            self._bot_name = str(me.get("displayName") or me.get("name") or "{bot}")
        except (ChatterHTTPError, httpx.HTTPError, ValueError):
            logger.warning("salesforce_chatter: bot display name unavailable; approval hint uses placeholder")
        self._mark_connected()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "salesforce_chatter: connected feed=%s probe_groups=%d probe=%ss full_scan=%ss dry_run=%s",
            s.feed, len(s.group_ids), s.probe_interval_seconds, s.poll_interval_seconds, s.dry_run,
        )
        return True

    async def disconnect(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        if self._inbox:
            self._inbox.close()
        release_scoped_lock(PLATFORM_NAME, self._settings.bot_user_id[:15])
        self._mark_disconnected()

    # ---- Receiving ----------------------------------------------------------

    async def _poll_loop(self) -> None:
        s = self._settings
        next_full_scan = 0.0
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                if s.group_ids:
                    await self._probe_once()
                if loop.time() >= next_full_scan:
                    await self._poll_once()
                    next_full_scan = loop.time() + s.poll_interval_seconds
            except asyncio.CancelledError:
                raise
            except ChatterHTTPError as exc:
                logger.warning("salesforce_chatter: poll failed status=%s code=%s", exc.status, exc.code)
            except Exception as exc:
                logger.error("salesforce_chatter: poll failed (%s)", type(exc).__name__)
            await asyncio.sleep(s.probe_interval_seconds if s.group_ids else s.poll_interval_seconds)

    async def _probe_once(self) -> None:
        started = utc_now()
        since = self._inbox.scan_since(timedelta(hours=self._settings.max_catchup_hours), cursor=PROBE_CURSOR)
        for c in await probe_groups(self._client, self._settings, since):
            await self._dispatch(c)
        self._inbox.advance_scan(started, PROBE_OVERLAP, cursor=PROBE_CURSOR)

    async def _poll_once(self) -> None:
        started = utc_now()
        since = self._inbox.scan_since(timedelta(hours=self._settings.max_catchup_hours), cursor=TO_ME_CURSOR)
        for c in await scan_feed(self._client, self._settings, since):
            await self._dispatch(c)
        self._inbox.advance_scan(started, SCAN_OVERLAP, cursor=TO_ME_CURSOR)

    def _is_allowed_user(self, user_id: str) -> bool:
        allowed = self._settings.allowed_user_ids
        return bool(allowed) and any(same_sf_id(user_id, u) for u in allowed)

    async def _dispatch(self, c: Candidate) -> None:
        if not self._inbox.claim(Claim(c.source_id, c.feed_element_id, c.kind, c.requester_id)):
            return
        if not self._is_allowed_user(c.requester_id):
            self._inbox.set_status(c.source_id, "skipped_unauthorized")
            logger.info("salesforce_chatter: unauthorized requester source=%s user=%s", c.source_id, c.requester_id)
            if self._settings.unauthorized_reply and not c.follow_up_without_mention:
                await self._post_notice(c.feed_element_id, c.requester_id, self._settings.unauthorized_text)
            return
        command = c.text.lstrip()
        if command.startswith("/") and (command[1:].split(maxsplit=1) or [""])[0].lower() not in ALLOWED_COMMANDS:
            self._inbox.set_status(c.source_id, "skipped_command")
            logger.info("salesforce_chatter: skipped slash command source=%s", c.source_id)
            await self._post_notice(c.feed_element_id, c.requester_id, self._settings.commands_text)
            return
        if self._inbox.answered_in_last_hour() >= self._settings.max_replies_per_hour:
            self._inbox.set_status(c.source_id, "skipped_rate")
            logger.warning("salesforce_chatter: hourly cap reached source=%s", c.source_id)
            return
        # Acknowledge immediately, before the request enters the generation queue.
        await self._like(c.source_id, c.kind)
        self._inbox.set_status(c.source_id, "liked")
        self._mention_once.setdefault(c.feed_element_id, (c.source_id, c.requester_id))

        media_urls, media_types, notes = await self._download_attachments(c)
        text = c.text or self._settings.empty_post_text
        if notes:
            text = text + "\n\n" + "\n".join(notes)
        message_type = MessageType.TEXT
        if media_types:
            message_type = MessageType.PHOTO if all(t.startswith("image/") for t in media_types) else MessageType.DOCUMENT
        source = self.build_source(
            chat_id=c.feed_element_id,
            chat_name=c.parent_name,
            chat_type="group",
            user_id=c.requester_id,
            user_name=c.requester_name,
            thread_id=c.feed_element_id,
            message_id=c.source_id,
        )
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            message_id=c.source_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=c.feed_element_id if c.kind == "comment" else None,
            reply_to_text=c.post_text if c.kind == "comment" else None,
            channel_context=c.thread_context or None,
            metadata={"sf_kind": c.kind},
        )
        logger.info("salesforce_chatter: dispatch source=%s kind=%s thread=%s files=%d follow_up=%s",
                    c.source_id, c.kind, c.feed_element_id, len(media_urls), c.follow_up_without_mention)
        token = self._inline_reply.set(asyncio.current_task())
        try:
            await self.handle_message(event)
        finally:
            self._inline_reply.reset(token)
        # run_busy rewrites accepted bare approval words into slash commands.
        if event.text != text and event.get_command() in {"approve", "deny"}:
            self._inbox.set_status(c.source_id, "handled")

    async def _download_attachments(self, c: Candidate) -> tuple[list[str], list[str], list[str]]:
        urls: list[str] = []
        types: list[str] = []
        notes: list[str] = []
        for a in c.attachments:
            if a.size and a.size > self._settings.max_attachment_bytes:
                notes.append(self._settings.attachment_too_large_note.format(title=a.title))
                continue
            try:
                data = await self._client.download(a.download_url, max_bytes=self._settings.max_attachment_bytes)
            except (ChatterHTTPError, ValueError) as exc:
                logger.warning("salesforce_chatter: download failed file=%s (%s)", a.file_id, type(exc).__name__)
                notes.append(self._settings.attachment_failed_note.format(title=a.title))
                continue
            cached = await asyncio.to_thread(cache_media_bytes, data, filename=a.title, mime_type=a.mime_type)
            if cached is None:
                notes.append(self._settings.attachment_unreadable_note.format(title=a.title))
                continue
            urls.append(cached.path)
            types.append(cached.media_type)
            notes.append(cached.context_note())
        return urls, types, notes

    async def _like(self, source_id: str, kind: str | None) -> None:
        if self._settings.dry_run:
            logger.info("salesforce_chatter: [dry-run] like %s=%s", kind, source_id)
            return
        try:
            if kind == "comment":
                await self._client.like_comment(source_id)
            else:
                await self._client.like_feed_element(source_id)
        except ChatterHTTPError as exc:
            logger.warning("salesforce_chatter: like failed source=%s status=%s code=%s", source_id, exc.status, exc.code)

    async def _post_notice(self, chat_id: str, requester_id: str, text: str) -> None:
        if self._settings.dry_run:
            logger.info("salesforce_chatter: [dry-run] notice thread=%s", chat_id)
            return
        try:
            await self._client.post_comment(chat_id, reply_body(requester_id, text, empty_reply_text=self._settings.empty_reply_text))
        except ChatterHTTPError as exc:
            logger.warning("salesforce_chatter: notice failed thread=%s status=%s code=%s", chat_id, exc.status, exc.code)

    async def on_processing_start(self, event: MessageEvent) -> None:
        if event.message_id and event.source.user_id:
            self._mention_once[event.source.chat_id] = (event.message_id, event.source.user_id)

    async def _dispatch_inline_reply(self, event: MessageEvent, *, log_cmd=None) -> None:
        token = self._inline_reply.set(asyncio.current_task())
        try:
            await super()._dispatch_inline_reply(event, log_cmd=log_cmd)
        except BaseException:
            self._inbox.set_status(event.message_id, "failed")
            raise
        else:
            self._inbox.set_status(event.message_id, "handled")
        finally:
            self._inline_reply.reset(token)

    async def _notify_turn_error(self, event: MessageEvent, e: BaseException):
        # Both supported cores invoke on_processing_complete(FAILURE) first.
        # That hook owns the configurable user-facing notice; never leak a second
        # core-generated exception message (or suppress unrelated later notices).
        return None

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        logger.info("salesforce_chatter: processing complete source=%s outcome=%s", event.message_id, outcome.value)
        if not event.message_id:
            # Internal resume events have no Chatter post; leave the inbox and pending mention unchanged.
            return
        status = {
            ProcessingOutcome.SUCCESS: "answered" if event.message_id in self._replied_sources else "handled",
            ProcessingOutcome.FAILURE: "failed",
            ProcessingOutcome.CANCELLED: "cancelled",
        }[outcome]
        if outcome is ProcessingOutcome.FAILURE:
            await self.send(event.source.chat_id, self._settings.failure_text, metadata={"notify": True})
        pending = self._mention_once.get(event.source.chat_id)
        if pending and pending[0] == event.message_id:
            del self._mention_once[event.source.chat_id]
        self._inbox.set_status(event.message_id, status)
        self._replied_sources.discard(event.message_id)

    # ---- Sending ------------------------------------------------------------

    def _take_requester(self, chat_id: str, metadata=None) -> tuple[str, str] | None:
        if self._inline_reply.get() is asyncio.current_task():
            return None
        if metadata and not metadata.get("notify") and any(
                key in metadata for key in ("notify", "_interim_send", "is_approval_prompt")):
            return None
        return self._mention_once.pop(chat_id, None)

    async def _post(self, chat_id: str, body: dict, *, file_id: str | None = None) -> dict:
        # Conversations belong to feed elements (0D5...), so respond with comments.
        # Other IDs, such as group home channels, receive new posts for cron delivery.
        if chat_id.startswith("0D5"):
            return await self._client.post_comment(chat_id, body, file_id=file_id)
        return await self._client.post_feed_item(chat_id, body, file_id=file_id)

    def _send_retry_is_final(self, result: SendResult) -> bool:
        if (result.raw_response or {}).get("partial_overflow"):
            return True
        status, _, code = (result.error or "").partition(" ")
        return status in {"400", "401", "403", "404"} and code != "REQUEST_LIMIT_EXCEEDED"

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if metadata and metadata.get("is_approval_prompt") and self._settings.approval_hint:
            hint = self._settings.approval_hint.replace("{bot_name}", self._bot_name)
            content = f"{content.rstrip()}\n\n{hint}"
        pending = self._take_requester(chat_id, metadata)
        bodies = reply_bodies(pending[1] if pending else None, content,
                              max_length=self.MAX_MESSAGE_LENGTH,
                              empty_reply_text=self._settings.empty_reply_text)
        if self._settings.dry_run:
            logger.info("salesforce_chatter: [dry-run] comment thread=%s chunks=%d", chat_id, len(bodies))
            return SendResult(success=True, message_id=f"dry-run:{chat_id}")
        sent = []
        for body in bodies:
            try:
                data = await self._post(chat_id, body)
            except ChatterHTTPError as exc:
                if pending and not sent:
                    self._mention_once.setdefault(chat_id, pending)
                logger.warning("salesforce_chatter: comment failed thread=%s status=%s code=%s", chat_id, exc.status, exc.code)
                return SendResult(success=False, error=f"{exc.status} {exc.code}",
                                  retryable=not sent and (exc.status >= 500 or exc.code == "REQUEST_LIMIT_EXCEEDED"),
                                  raw_response={"partial_overflow": bool(sent), "message_ids": sent})
            sent.append(data.get("id"))
            if pending:
                self._replied_sources.add(pending[0])
        logger.info("salesforce_chatter: comment sent thread=%s chunks=%d mention=%s", chat_id, len(sent), bool(pending))
        return SendResult(success=True, message_id=sent[-1], raw_response={"message_ids": sent})

    async def _send_file(self, chat_id: str, path: str, caption: str | None, file_name: str | None, metadata=None) -> SendResult:
        file_path = Path(path)
        # Remove the sandbox cache's remote_<hash>_ prefix from the display name.
        name = _REMOTE_PREFIX.sub("", file_name or file_path.name)
        if self._settings.dry_run:
            logger.info("salesforce_chatter: [dry-run] file thread=%s name=%s", chat_id, name)
            return SendResult(success=True, message_id=f"dry-run:{chat_id}")
        try:
            data = await asyncio.to_thread(file_path.read_bytes)
        except OSError as exc:
            return SendResult(success=False, error=f"read failed: {type(exc).__name__}")
        if len(data) > self._settings.max_attachment_bytes:
            return SendResult(success=False, error="file too large")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        preview = None
        if mime == "text/html" and self._settings.html_preview:
            preview = await render_html_preview(
                data, work_dir=plugin_data_dir(PLUGIN_ID) / "preview", image=self._settings.preview_image)
        pending = self._take_requester(chat_id, metadata)
        requester = pending[1] if pending else None
        try:
            file_id = await self._client.upload_file(data, filename=name, mime_type=mime)
            html_caption = self._settings.html_download_caption.format(name=name) if preview else name
            result = await self._post(chat_id, reply_body(requester, caption or html_caption, empty_reply_text=self._settings.empty_reply_text), file_id=file_id)
            if pending:
                self._replied_sources.add(pending[0])
            if preview:
                # Chatter cannot preview HTML. Post the image last so the feed's latest
                # comment shows the content without requiring the file to be opened.
                image_id = await self._client.upload_file(
                    preview, filename=f"{Path(name).stem}-preview.png", mime_type="image/png")
                result = await self._post(chat_id, reply_body(None, self._settings.html_preview_caption.format(name=name), empty_reply_text=self._settings.empty_reply_text), file_id=image_id)
        except ChatterHTTPError as exc:
            if pending:
                self._mention_once.setdefault(chat_id, pending)
            logger.warning("salesforce_chatter: file send failed thread=%s status=%s code=%s", chat_id, exc.status, exc.code)
            return SendResult(success=False, error=f"{exc.status} {exc.code}", retryable=exc.status >= 500)
        logger.info("salesforce_chatter: file sent thread=%s file=%s preview=%s", chat_id, file_id, bool(preview))
        return SendResult(success=True, message_id=result.get("id"))

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_file(chat_id, file_path, caption, file_name, metadata)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_file(chat_id, image_path, caption, None, metadata)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_file(chat_id, video_path, caption, None, metadata)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_file(chat_id, audio_path, caption, None, metadata)

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult:
        if self._settings.dry_run:
            return SendResult(success=True, message_id=f"dry-run:{chat_id}")
        pending = None
        try:
            data, mime, name = await download_image(image_url, max_bytes=self._settings.max_attachment_bytes)
            file_id = await self._client.upload_file(data, filename=name, mime_type=mime)
            pending = self._take_requester(chat_id, metadata)
            result = await self._post(chat_id, reply_body(
                pending[1] if pending else None, caption or name,
                empty_reply_text=self._settings.empty_reply_text), file_id=file_id)
            if pending:
                self._replied_sources.add(pending[0])
            return SendResult(success=True, message_id=result.get("id"))
        except (httpx.HTTPError, ChatterHTTPError, ValueError, OSError) as exc:
            if pending:
                self._mention_once.setdefault(chat_id, pending)
            logger.warning("salesforce_chatter: image URL delivery failed (%s); posting link", type(exc).__name__)
            return await self.send(chat_id, f"{caption}\n{image_url}" if caption else image_url,
                                   reply_to=reply_to, metadata=metadata)

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "group"}


def register(ctx) -> None:
    kwargs = dict(
        name=PLATFORM_NAME,
        label="Salesforce Chatter",
        adapter_factory=SalesforceChatterAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=list(SECRET_NAMES[:5]),
        install_hint="Run hermes plugins enable salesforce-chatter to install httpx and PyJWT",
        allowed_users_env="SF_CHATTER_ALLOWED_USERS",
        allow_all_env="SF_CHATTER_ALLOW_ALL_USERS",
        max_message_length=9000,
        pii_safe=True,
        allow_update_command=False,
        platform_hint=PLATFORM_HINT,
        emoji="☁️",
        # A group ID (0F9...) as the cron home channel receives new posts.
        cron_deliver_env_var="SF_CHATTER_HOME_CHANNEL",
    )
    # display_tier was added in v0.21; omit it when older cores reject the keyword.
    if "display_tier" in {f.name for f in dataclasses.fields(PlatformEntry)}:
        kwargs["display_tier"] = "minimal"
    ctx.register_platform(**kwargs)
