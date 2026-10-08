import asyncio
from dataclasses import replace

import pytest

from test_adapter_flow import adapter, candidate, texts, U1


def comments(adapter):
    return [call[2] for call in adapter._client.calls if call[0] == "comment"]


def mentions(body):
    return [s["id"] for s in body["body"]["messageSegments"] if s["type"] == "Mention"]


@pytest.mark.parametrize("metadata", [{"_interim_send": True}, {"notify": False}, {"is_approval_prompt": True, "_interim_send": True}])
async def test_nonfinal_send_preserves_requester_for_final(adapter, metadata):
    await adapter._dispatch(candidate())
    await adapter.send("0D5A", "Working", metadata=metadata)
    await adapter.send("0D5A", "Answer", metadata={"notify": True})
    assert [mentions(body) for body in comments(adapter)] == [[], [U1]]


async def test_legacy_metadata_still_mentions_first_reply(adapter):
    await adapter._dispatch(candidate())
    await adapter.send("0D5A", "Answer", metadata={"thread_id": "0D5A"})
    assert mentions(comments(adapter)[0]) == [U1]


async def test_long_final_is_complete_and_mentions_only_first_chunk(adapter):
    await adapter._dispatch(candidate())
    content = "\n\n".join("paragraph " + str(i) + " " + "word " * 800 for i in range(6))
    result = await adapter.send("0D5A", content, metadata={"notify": True})
    bodies = comments(adapter)
    assert result.success
    assert len(bodies) > 1
    assert [mentions(body) for body in bodies] == [[U1]] + [[]] * (len(bodies) - 1)
    from sfchatter.segments import plain_text
    rendered = " ".join(plain_text(body["body"]) for body in bodies)
    assert rendered.split() == content.split()
    assert all(len(texts(body)) <= 9000 for body in bodies)


async def test_exception_path_posts_only_configured_failure(adapter):
    await adapter._dispatch(candidate())
    event = adapter.dispatched[0]
    async def fail(event):
        raise RuntimeError("private exception detail")
    adapter._message_handler = fail
    adapter.config.typing_indicator = False
    await adapter._process_message_background(event, "failure-test")
    bodies = comments(adapter)
    assert len(bodies) == 1
    assert texts(bodies[0]).strip() == adapter._settings.failure_text
    assert "private exception detail" not in texts(bodies[0])


async def test_approval_hint_uses_resolved_name_and_is_customizable(adapter):
    adapter._bot_name = "Example Bot"
    adapter._settings = replace(adapter._settings, approval_hint="Mention @{bot_name} to approve.")
    await adapter.send("0D5A", "Approval needed", metadata={"is_approval_prompt": True, "_interim_send": True})
    assert "Mention @Example Bot to approve." in texts(comments(adapter)[0])


@pytest.mark.parametrize("command", ["/approve", "/approve session", "/approve always", "/deny", "/stop", "2"])
async def test_inline_commands_and_clarify_finish_inbox_without_taking_turn_mention(adapter, command):
    await adapter._dispatch(candidate(source_id="original"))
    await adapter._dispatch(candidate(text=command, source_id="inline"))
    event = adapter.dispatched[-1]
    async def handler(event):
        return "Acknowledged"
    adapter._message_handler = handler
    await adapter._dispatch_inline_reply(event)
    status = adapter._inbox._db.execute("SELECT status FROM mentions WHERE source_id='inline'").fetchone()[0]
    assert status == "handled"
    assert adapter._inbox.answered_in_last_hour() == 0
    assert mentions(comments(adapter)[0]) == []
    await adapter.send("0D5A", "Original answer", metadata={"notify": True})
    assert mentions(comments(adapter)[-1]) == [U1]


async def test_image_url_uploads_file_and_falls_back_to_link_on_fetch_failure(adapter, monkeypatch):
    async def download(url, *, max_bytes):
        return b"png", "image/png", "picture.png"
    monkeypatch.setitem(adapter.send_image.__func__.__globals__, "download_image", download)
    result = await adapter.send_image("0D5A", "https://images.example/picture.png", caption="Picture")
    assert result.success
    assert adapter._client.calls[0] == ("upload", "picture.png", "image/png", 3)
    assert adapter._client.calls[1][3] == "069UP"
    async def failure(url, *, max_bytes):
        raise ValueError("too large")
    monkeypatch.setitem(adapter.send_image.__func__.__globals__, "download_image", failure)
    result = await adapter.send_image("0D5A", "https://images.example/large.png")
    assert result.success
    assert "https://images.example/large.png" in texts(comments(adapter)[-1])


@pytest.mark.parametrize("status,code", [(400, "INVALID_INPUT"), (401, "INVALID_SESSION_ID"), (403, "INSUFFICIENT_ACCESS"), (404, "NOT_FOUND")])
async def test_permanent_error_does_not_plaintext_resend(adapter, status, code):
    ChatterHTTPError = adapter.send.__func__.__globals__["ChatterHTTPError"]
    attempts = []
    async def refuse(*args, **kwargs):
        attempts.append(args)
        raise ChatterHTTPError(status, code)
    adapter._post = refuse
    result = await adapter._send_with_retry("0D5A", "Answer")
    assert not result.success
    assert len(attempts) == 1


@pytest.mark.parametrize("status,code", [(403, "REQUEST_LIMIT_EXCEEDED"), (503, "SERVER_UNAVAILABLE")])
async def test_transient_errors_are_retryable(adapter, status, code):
    ChatterHTTPError = adapter.send.__func__.__globals__["ChatterHTTPError"]
    async def refuse(*args, **kwargs):
        raise ChatterHTTPError(status, code)
    adapter._post = refuse
    result = await adapter.send("0D5A", "Answer")
    assert result.retryable
    assert not adapter._send_retry_is_final(result)


async def test_partial_chunk_delivery_is_not_replayed(adapter):
    ChatterHTTPError = adapter.send.__func__.__globals__["ChatterHTTPError"]
    attempts = []
    async def partial(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 2:
            raise ChatterHTTPError(503, "SERVER_UNAVAILABLE")
        return {"id": "first"}
    adapter._post = partial
    result = await adapter._send_with_retry("0D5A", "word " * 4000)
    assert not result.success
    assert result.raw_response["partial_overflow"]
    assert len(attempts) == 2


async def test_trailing_blank_lines_are_not_posted(adapter):
    await adapter.send("0D5A", "Answer\n\n   \n\n")
    assert texts(comments(adapter)[0]) == "Answer"


@pytest.mark.parametrize("word", ["yes", "approve"])
async def test_busy_core_approval_words_resolve_and_finish_inbox(adapter, monkeypatch, word):
    from gateway.run_busy import GatewayBusySessionMixin
    from tools import approval

    monkeypatch.setattr(approval, "has_blocking_approval", lambda key: True)
    routed = []
    class Runner(GatewayBusySessionMixin):
        async def _handle_approve_command(self, event):
            routed.append(event.get_command())
            return "Approved"

        _handle_deny_command = _handle_approve_command

        def _delivery_adapter_for(self, source):
            return adapter

        _adapter_for_source = _delivery_adapter_for

        async def _send_busy_reply(self, event, target, content, **kwargs):
            await target.send(event.source.chat_id, content, metadata={"thread_id": event.source.thread_id})

    runner = Runner()
    await adapter._dispatch(candidate(source_id="original"))
    async def busy(event):
        assert await runner._route_plaintext_approval_while_busy(event, "approval-test")
    adapter.handle_message = busy
    await adapter._dispatch(candidate(text=word, source_id="approval"))
    assert routed == ["approve"]
    assert adapter._inbox._db.execute("SELECT status FROM mentions WHERE source_id='approval'").fetchone()[0] == "handled"
    assert mentions(comments(adapter)[0]) == []
    await adapter.send("0D5A", "Final", metadata={"notify": True})
    assert mentions(comments(adapter)[-1]) == [U1]


async def test_like_only_does_not_consume_hourly_reply_budget(adapter):
    adapter._settings = replace(adapter._settings, max_replies_per_hour=1)
    await adapter._dispatch(candidate(source_id="first"))
    await adapter._dispatch(candidate(source_id="second"))
    assert [event.message_id for event in adapter.dispatched] == ["first", "second"]
    adapter._inbox.set_status("first", "answered")
    await adapter._dispatch(candidate(source_id="third"))
    assert [event.message_id for event in adapter.dispatched] == ["first", "second"]


async def test_core_stream_setup_does_not_feed_partial_deltas_to_chatter(adapter):
    from types import SimpleNamespace
    from gateway.config import StreamingConfig
    from gateway.run_turn_runner import TurnRunner
    from gateway.run_turn import GatewayTurnMixin

    await adapter._dispatch(candidate())
    source = adapter.dispatched[0].source
    runner = SimpleNamespace(
        config=SimpleNamespace(streaming=StreamingConfig(enabled=True)),
        _delivery_adapter_for=lambda source: adapter,
        _adapter_for_source=lambda source: adapter,
        _build_stream_consumer_config=lambda *args, **kwargs: GatewayTurnMixin._build_stream_consumer_config(None, *args, **kwargs),
    )
    ctx = SimpleNamespace(
        mute_notification_reply=False, streaming_tts_consumer_holder=[None],
        resolve_display_setting=lambda *args: True, user_config={},
        scheduled_heartbeat=False, interim_assistant_messages_enabled=False,
        source=source, _status_thread_metadata={"thread_id": source.thread_id},
        progress_queue=None, event_message_id="original", _run_still_current=lambda: True,
        stream_consumer_holder=[None],
    )
    turn = object.__new__(TurnRunner)
    turn._runner, turn._ctx = runner, ctx
    consumer, delta_callback, _, _ = turn._setup_stream_consumer("salesforce_chatter")
    assert consumer is None
    assert delta_callback is None
    assert comments(adapter) == []


async def test_empty_success_is_handled_not_counted_as_reply(adapter):
    from gateway.platforms.event import ProcessingOutcome
    await adapter._dispatch(candidate())
    await adapter.on_processing_complete(adapter.dispatched[0], ProcessingOutcome.SUCCESS)
    assert adapter._inbox.answered_in_last_hour() == 0
    assert adapter._inbox.pending_requester("0D5A") is None


async def test_connect_resolves_bot_name_once_for_approval_prompts(adapter, monkeypatch):
    from test_adapter_flow import FakeClient
    lookups = []
    class Client(FakeClient):
        async def authenticate(self):
            pass

        async def get_json(self, path):
            lookups.append(path)
            return {"displayName": "Approval Bot"}

        async def aclose(self):
            pass

    client = Client()
    monkeypatch.setitem(adapter.connect.__func__.__globals__, "ChatterClient", lambda settings: client)
    async def poll():
        await asyncio.Event().wait()
    adapter._poll_loop = poll
    adapter._inbox.close()
    assert await adapter.connect()
    try:
        for _ in range(2):
            await adapter.send("0D5A", "Approve?", metadata={"is_approval_prompt": True})
        assert lookups == ["chatter/users/me"]
        assert all("@Approval Bot approve" in texts(body) for body in comments(adapter))
    finally:
        await adapter.disconnect()
