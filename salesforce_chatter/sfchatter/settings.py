from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .preview import DEFAULT_IMAGE

FEEDS = ("to_me", "news")
_REQUIRED = {
    "login_url": "SF_CHATTER_LOGIN_URL",
    "client_id": "SF_CHATTER_CLIENT_ID",
    "username": "SF_CHATTER_USERNAME",
    "private_key_path": "SF_CHATTER_PRIVATE_KEY_PATH",
    "bot_user_id": "SF_CHATTER_BOT_USER_ID",
}


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _ids(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return tuple(str(v).strip() for v in items if str(v).strip())


@dataclass(frozen=True)
class Settings:
    login_url: str
    client_id: str
    username: str
    private_key_path: Path
    bot_user_id: str
    api_version: str = "v66.0"
    feed: str = "to_me"
    # To Me includes mentions anywhere; Connect REST guidance requires at least 60 seconds.
    poll_interval_seconds: int = 60
    # Group change detection uses SOQL rather than the hourly Chatter REST allowance.
    probe_interval_seconds: int = 5
    allowed_parent_ids: tuple[str, ...] = ()
    allowed_user_ids: tuple[str, ...] = ()
    # Only allowlisted authors can continue after a bot comment without mentioning it.
    follow_up_without_mention: bool = False
    dry_run: bool = True
    max_replies_per_hour: int = 20
    max_catchup_hours: int = 24
    max_attachment_bytes: int = 25 * 1024 * 1024
    unauthorized_reply: bool = True
    # Optionally attach a PNG preview because Chatter cannot preview HTML.
    html_preview: bool = False
    preview_image: str = DEFAULT_IMAGE
    # The preview is one image, 1280 px wide; taller pages are cut at this height.
    html_preview_max_height: int = 8000
    failure_text: str = "Failed to generate a reply. Please wait and mention me again."
    unauthorized_text: str = "Only authorized users can use this assistant."
    commands_text: str = "Available Chatter commands: /new /reset /stop /approve /deny. Write questions in plain text."
    approval_hint: str = 'Reply with a comment that mentions me: "@{bot_name} approve" or "@{bot_name} deny".'
    empty_post_text: str = "(No message text)"
    empty_reply_text: str = "(Empty reply)"
    attachment_too_large_note: str = "[Attachment '{title}' was not loaded because it is too large]"
    attachment_failed_note: str = "[Attachment '{title}' could not be loaded]"
    attachment_unreadable_note: str = "[Attachment '{title}' could not be loaded as an image]"
    html_download_caption: str = "{name} (download and open in a browser)"
    html_preview_caption: str = "{name} preview (image)"
    html_preview_truncated_note: str = "The preview stops here; download {name} for the rest."

    @property
    def group_ids(self) -> tuple[str, ...]:
        return tuple(p for p in self.allowed_parent_ids if p.startswith("0F9"))

    @classmethod
    def load(cls, env: Mapping[str, str], extra: Mapping[str, Any]) -> "Settings":
        values: dict[str, Any] = {}
        for field_name, env_name in _REQUIRED.items():
            value = (env.get(env_name) or str(extra.get(field_name) or "")).strip()
            if not value:
                raise ValueError(f"{env_name} is required")
            values[field_name] = value
        feed = str(extra.get("feed", "to_me"))
        if feed not in FEEDS:
            raise ValueError(f"feed must be one of {FEEDS}")
        return cls(
            login_url=values["login_url"].rstrip("/"),
            client_id=values["client_id"],
            username=values["username"],
            private_key_path=Path(values["private_key_path"]).expanduser(),
            bot_user_id=values["bot_user_id"],
            api_version=str(extra.get("api_version", "v66.0")),
            feed=feed,
            poll_interval_seconds=max(60, int(extra.get("poll_interval_seconds", 60))),
            probe_interval_seconds=max(3, int(extra.get("probe_interval_seconds", 5))),
            allowed_parent_ids=_ids(env.get("SF_CHATTER_ALLOWED_PARENT_IDS") or extra.get("allowed_parent_ids")),
            allowed_user_ids=_ids(env.get("SF_CHATTER_ALLOWED_USERS") or extra.get("allowed_user_ids")),
            follow_up_without_mention=_bool(extra.get("follow_up_without_mention"), False),
            dry_run=_bool(extra.get("dry_run"), True),
            max_replies_per_hour=int(extra.get("max_replies_per_hour", 20)),
            max_catchup_hours=int(extra.get("max_catchup_hours", 24)),
            max_attachment_bytes=int(extra.get("max_attachment_mb", 25)) * 1024 * 1024,
            unauthorized_reply=_bool(extra.get("unauthorized_reply"), True),
            html_preview=_bool(extra.get("html_preview"), False),
            preview_image=str(extra.get("preview_image") or DEFAULT_IMAGE),
            html_preview_max_height=max(400, min(30000, int(extra.get("html_preview_max_height", cls.html_preview_max_height)))),
            failure_text=str(extra.get("failure_text", cls.failure_text)),
            unauthorized_text=str(extra.get("unauthorized_text", cls.unauthorized_text)),
            commands_text=str(extra.get("commands_text", cls.commands_text)),
            approval_hint=str(extra.get("approval_hint", cls.approval_hint)),
            empty_post_text=str(extra.get("empty_post_text", cls.empty_post_text)),
            empty_reply_text=str(extra.get("empty_reply_text", cls.empty_reply_text)),
            attachment_too_large_note=str(extra.get("attachment_too_large_note", cls.attachment_too_large_note)),
            attachment_failed_note=str(extra.get("attachment_failed_note", cls.attachment_failed_note)),
            attachment_unreadable_note=str(extra.get("attachment_unreadable_note", cls.attachment_unreadable_note)),
            html_download_caption=str(extra.get("html_download_caption", cls.html_download_caption)),
            html_preview_caption=str(extra.get("html_preview_caption", cls.html_preview_caption)),
            html_preview_truncated_note=str(extra.get("html_preview_truncated_note", cls.html_preview_truncated_note)),
        )
