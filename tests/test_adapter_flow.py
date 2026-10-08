from datetime import datetime, timezone

import pytest

U1 = "005000000000U01AAA"
ENV = {
    "SF_CHATTER_LOGIN_URL": "https://test.salesforce.com",
    "SF_CHATTER_CLIENT_ID": "cid",
    "SF_CHATTER_USERNAME": "bot@example.com",
    "SF_CHATTER_PRIVATE_KEY_PATH": "/tmp/k.key",
    "SF_CHATTER_BOT_USER_ID": "005000000000BOT",
    "SF_CHATTER_ALLOWED_USERS": U1[:15],
}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.files = {}

    async def like_feed_element(self, i):
        self.calls.append(("like_post", i))

    async def like_comment(self, i):
        self.calls.append(("like_comment", i))

    async def post_comment(self, feed_id, body, *, file_id=None):
        self.calls.append(("comment", feed_id, body, file_id))
        return {"id": f"0D7BOT{len(self.calls)}"}

    async def post_feed_item(self, subject_id, body, *, file_id=None):
        self.calls.append(("post", subject_id, body, file_id))
        return {"id": "0D5NEW"}

    async def download(self, url, *, max_bytes):
        self.calls.append(("download", url))
        return self.files[url]

    async def upload_file(self, data, *, filename, mime_type):
        self.calls.append(("upload", filename, mime_type, len(data)))
        return "069UP"


def candidate(text="Question", source_id="0D7A", kind="comment", requester=U1, attachments=()):
    from sfchatter.scanner import Candidate

    return Candidate(source_id, kind, "0D5A", "0F9G", "Example group", requester, "U1",
                     datetime(2026, 10, 8, 3, 5, tzinfo=timezone.utc), text, "Parent post", "U2: Earlier discussion", tuple(attachments))


@pytest.fixture
def adapter(load_plugin, monkeypatch, tmp_path):
    from gateway.config import PlatformConfig

    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = load_plugin()
    a = plugin.adapter.SalesforceChatterAdapter(PlatformConfig(enabled=True, extra={"dry_run": False}))
    a._client = FakeClient()
    a._inbox = plugin.adapter.Inbox(tmp_path / "inbox.sqlite")
    a.dispatched = []

    async def fake_handle(event):
        a.dispatched.append(event)

    a.handle_message = fake_handle
    return a


def texts(body):
    return "".join(s.get("text", "") for s in body["body"]["messageSegments"])


async def test_dispatch_likes_immediately_and_builds_thread_scoped_event(adapter):
    await adapter._dispatch(candidate())
    event = adapter.dispatched[0]
    assert adapter._client.calls[0] == ("like_comment", "0D7A")
    assert (event.source.chat_id, event.source.thread_id, event.message_id) == ("0D5A", "0D5A", "0D7A")
    assert event.text == "Question"
    assert event.reply_to_text == "Parent post"
    assert event.channel_context == "U2: Earlier discussion"


async def test_duplicate_candidate_dispatches_once(adapter):
    await adapter._dispatch(candidate())
    await adapter._dispatch(candidate())
    assert len(adapter.dispatched) == 1


@pytest.mark.parametrize("text", ["Question", "/approve", "/deny"])
async def test_unauthorized_requester_gets_notice_and_no_like(adapter, text):
    await adapter._dispatch(candidate(text=text, requester="005000000000ZZZAAA"))
    assert adapter.dispatched == []
    assert [c[0] for c in adapter._client.calls] == ["comment"]


@pytest.mark.parametrize("text", ["/restart", "/update now", "/"])
async def test_host_level_slash_command_is_not_dispatched(adapter, text):
    await adapter._dispatch(candidate(text=text))
    assert adapter.dispatched == []


@pytest.mark.parametrize("command", ["/new", "/reset", "/stop", "/approve", "/deny"])
async def test_conversation_commands_pass_through(adapter, command):
    await adapter._dispatch(candidate(text=command, source_id="0D7P"))
    assert adapter.dispatched[0].text == command


async def test_rate_cap_skips_dispatch(adapter):
    adapter._settings = adapter._settings.__class__(**{**adapter._settings.__dict__, "max_replies_per_hour": 0})
    await adapter._dispatch(candidate())
    assert adapter.dispatched == []


async def test_requester_is_mentioned_only_in_first_reply_comment(adapter):
    await adapter._dispatch(candidate())
    await adapter.send("0D5A", "**Answer** text")
    await adapter.send("0D5A", "Continued")
    first, second = adapter._client.calls[1][2], adapter._client.calls[2][2]
    assert first["body"]["messageSegments"][0] == {"type": "Mention", "id": U1}
    assert all(s["type"] != "Mention" for s in second["body"]["messageSegments"])
    assert texts(first).strip() == "Answer text"


async def test_internal_event_completion_keeps_pending_mention(adapter):
    from gateway.platforms.event import MessageEvent, ProcessingOutcome

    await adapter._dispatch(candidate())
    resumed = MessageEvent(text="", source=adapter.dispatched[0].source, message_id=None)
    await adapter.on_processing_complete(resumed, ProcessingOutcome.SUCCESS)
    await adapter.send("0D5A", "Answer")
    assert adapter._client.calls[-1][2]["body"]["messageSegments"][0] == {"type": "Mention", "id": U1}


async def test_image_attachment_is_downloaded_and_passed_as_photo(adapter):
    from sfchatter.scanner import Attachment

    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    adapter._client.files["/f/069I"] = png
    await adapter._dispatch(candidate(attachments=[Attachment("069I", "photo.png", "image/png", "/f/069I", len(png))]))
    event = adapter.dispatched[0]
    assert event.message_type.value == "photo"
    assert len(event.media_urls) == 1 and event.media_types == ["image/png"]
    assert "photo.png" in event.text


async def test_oversized_attachment_is_skipped_with_note(adapter):
    from sfchatter.scanner import Attachment

    await adapter._dispatch(candidate(attachments=[Attachment("069B", "big.zip", "application/zip", "/f/069B", 10**9)]))
    event = adapter.dispatched[0]
    assert event.media_urls == []
    assert ("download", "/f/069B") not in adapter._client.calls


async def test_html_document_is_followed_by_png_preview_as_latest_comment(adapter, tmp_path, monkeypatch):
    from dataclasses import replace

    adapter._settings = replace(adapter._settings, html_preview=True)
    rendered = []

    async def fake_render(html, **kwargs):
        rendered.append(html)
        return b"PNGDATA"

    monkeypatch.setitem(adapter._send_file.__func__.__globals__, "render_html_preview", fake_render)
    await adapter._dispatch(candidate())
    report = tmp_path / "remote_730ec3aee7ef_report.html"
    report.write_text("<h1>x</h1>")
    result = await adapter.send_document("0D5A", str(report), caption="Report")
    uploads = [c for c in adapter._client.calls if c[0] == "upload"]
    comments = [c for c in adapter._client.calls if c[0] == "comment"]
    assert rendered == [b"<h1>x</h1>"]
    assert [u[1:3] for u in uploads] == [("report.html", "text/html"), ("report-preview.png", "image/png")]
    assert comments[0][2]["body"]["messageSegments"][0] == {"type": "Mention", "id": U1}
    assert "Report" in texts(comments[0][2])
    assert all(s["type"] != "Mention" for s in comments[1][2]["body"]["messageSegments"])
    assert result.success


async def test_html_document_without_preview_still_sends_html(adapter, tmp_path, monkeypatch):
    from dataclasses import replace

    adapter._settings = replace(adapter._settings, html_preview=True)
    async def failed_render(html, **kwargs):
        return None

    monkeypatch.setitem(adapter._send_file.__func__.__globals__, "render_html_preview", failed_render)
    await adapter._dispatch(candidate())
    report = tmp_path / "report.html"
    report.write_text("<h1>x</h1>")
    result = await adapter.send_document("0D5A", str(report), caption="Report")
    uploads = [c for c in adapter._client.calls if c[0] == "upload"]
    assert [u[1] for u in uploads] == ["report.html"]
    assert result.success


async def test_failure_posts_failure_text_and_marks_failed(adapter):
    from gateway.platforms.event import ProcessingOutcome

    await adapter._dispatch(candidate(kind="post", source_id="0D5A"))
    event = adapter.dispatched[0]
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
    assert adapter._client.calls[-1][0] == "comment"
    assert adapter._inbox.pending_requester("0D5A") is None


async def test_dry_run_does_not_call_salesforce(adapter):
    adapter._settings = adapter._settings.__class__(**{**adapter._settings.__dict__, "dry_run": True})
    await adapter._dispatch(candidate())
    result = await adapter.send("0D5A", "Answer")
    assert adapter._client.calls == []
    assert result.success



async def test_send_to_group_home_channel_creates_new_post(adapter):
    result = await adapter.send("0F9GROUP", "Scheduled report")
    assert adapter._client.calls[0][:2] == ("post", "0F9GROUP")
    assert result.message_id == "0D5NEW"


async def test_default_html_send_does_not_launch_preview_renderer(adapter, tmp_path, monkeypatch):
    async def unexpected_render(*args, **kwargs):
        pytest.fail("HTML previews must be explicitly enabled")

    monkeypatch.setitem(adapter._send_file.__func__.__globals__, "render_html_preview", unexpected_render)
    report = tmp_path / "report.html"
    report.write_text("<h1>Report</h1>")
    result = await adapter.send_document("0D5A", str(report))
    assert result.success
    assert [call[1] for call in adapter._client.calls if call[0] == "upload"] == ["report.html"]


@pytest.mark.parametrize("mode,field", [
    ("large", "attachment_too_large_note"),
    ("failed", "attachment_failed_note"),
    ("unreadable", "attachment_unreadable_note"),
])
async def test_attachment_note_overrides_include_title(adapter, monkeypatch, mode, field):
    from sfchatter.scanner import Attachment

    template = "\u6dfb\u4ed8: {title}"
    adapter._settings = adapter._settings.__class__.load(ENV, {"dry_run": False, field: template})

    async def download(url, *, max_bytes):
        if mode == "failed":
            raise ValueError("Download exceeds byte limit")
        return b"invalid image"

    adapter._client.download = download
    monkeypatch.setitem(adapter._download_attachments.__func__.__globals__, "cache_media_bytes", lambda *args, **kwargs: None)
    attachment = Attachment("069B", "file{1}.png", "image/png", "/f/069B", 10**9 if mode == "large" else 10)
    await adapter._dispatch(candidate(attachments=[attachment]))
    assert adapter.dispatched[0].text == "Question\n\n" + template.format(title=attachment.title)
    assert adapter.dispatched[0].media_urls == []


async def test_configured_localized_text_reaches_events_and_replies(adapter):
    from gateway.platforms.event import ProcessingOutcome

    extra = {
        "dry_run": False,
        "empty_post_text": "\u672c\u6587\u306a\u3057",
        "empty_reply_text": "\u7a7a\u306e\u56de\u7b54",
        "failure_text": "\u5931\u6557",
        "unauthorized_text": "\u8a31\u53ef\u306a\u3057",
        "commands_text": "\u30b3\u30de\u30f3\u30c9",
    }
    adapter._settings = adapter._settings.__class__.load(ENV, extra)
    await adapter._dispatch(candidate(text=""))
    assert adapter.dispatched[0].text == extra["empty_post_text"]
    await adapter.send("0D5A", "")
    assert texts(adapter._client.calls[-1][2]).strip() == extra["empty_reply_text"]
    await adapter.on_processing_complete(adapter.dispatched[0], ProcessingOutcome.FAILURE)
    assert texts(adapter._client.calls[-1][2]).strip() == extra["failure_text"]
    await adapter._dispatch(candidate(source_id="0D7U", requester="005000000000ZZZAAA"))
    assert texts(adapter._client.calls[-1][2]).strip() == extra["unauthorized_text"]
    await adapter._dispatch(candidate(source_id="0D7C", text="/restart"))
    assert texts(adapter._client.calls[-1][2]).strip() == extra["commands_text"]