import pytest

import sfchatter.segments as segments_module

from sfchatter.segments import mentions_user, plain_text, reply_body, same_sf_id

BOT_15 = "005000000000ABC"
BOT_18 = "005000000000ABCAAA"


def body(*segments):
    return {"messageSegments": list(segments)}


def mention(uid, name="Hermes Bot"):
    return {"type": "Mention", "text": f"@{name}", "record": {"id": uid}}


def text(t):
    return {"type": "Text", "text": t}


def test_mentions_user_matches_15_and_18_char_ids():
    assert mentions_user(body(mention(BOT_18), text(" Question")), BOT_15)
    assert mentions_user(body(mention(BOT_15), text(" Question")), BOT_18)


def test_literal_at_text_is_not_a_mention():
    assert not mentions_user(body(text("@Hermes Bot Question")), BOT_15)


def test_other_user_mention_is_not_bot_mention():
    assert not mentions_user(body(mention("005000000000XYZAAA", "Alex"), text(" Question")), BOT_15)


def test_plain_text_drops_only_bot_mention_and_keeps_paragraphs():
    b = body(
        {"type": "MarkupBegin", "markupType": "Paragraph", "text": ""},
        mention(BOT_18),
        text(" First line"),
        {"type": "MarkupEnd", "markupType": "Paragraph", "text": ""},
        {"type": "MarkupBegin", "markupType": "Paragraph", "text": ""},
        mention("005000000000XYZAAA", "Alex"),
        text(" please confirm"),
        {"type": "MarkupEnd", "markupType": "Paragraph", "text": ""},
    )
    assert plain_text(b, drop_mention_of=BOT_15) == "First line\n@Alex please confirm"


def test_same_sf_id_rejects_empty():
    assert not same_sf_id(None, BOT_15)
    assert not same_sf_id("", BOT_15)


P_BEGIN = {"type": "MarkupBegin", "markupType": "Paragraph"}
P_END = {"type": "MarkupEnd", "markupType": "Paragraph"}


def test_reply_body_mentions_requester_first():
    assert reply_body("005000000000XYZAAA", "Answer") == {
        "body": {
            "messageSegments": [
                {"type": "Mention", "id": "005000000000XYZAAA"},
                {"type": "Text", "text": " "},
                P_BEGIN, {"type": "Text", "text": "Answer"}, P_END,
            ]
        }
    }


def test_reply_body_without_requester():
    assert reply_body(None, "Answer") == {"body": {"messageSegments": [P_BEGIN, {"type": "Text", "text": "Answer"}, P_END]}}


def test_empty_reply_uses_localized_override():
    fallback = "\u7a7a\u306e\u56de\u7b54"
    assert reply_body(None, " \n", empty_reply_text=fallback) == {
        "body": {"messageSegments": [{"type": "Text", "text": fallback}]}
    }


def rendered_runs(payload):
    """Expose text with its active formatting, rejecting malformed markup."""
    stack = []
    runs = []
    for segment in payload["body"]["messageSegments"]:
        if segment["type"] == "MarkupBegin":
            stack.append(segment["markupType"])
        elif segment["type"] == "MarkupEnd":
            assert stack.pop() == segment["markupType"]
        elif segment["type"] == "Text":
            runs.append((segment["text"], tuple(stack)))
    assert stack == []
    return runs


@pytest.mark.parametrize(
    ("markdown", "expected", "formatting"),
    [
        ("Prose " * 4000, ("Prose " * 4000).rstrip(), ("Paragraph",)),
        ("**" + "bold" * 5000 + "**", "bold" * 5000, ("Paragraph", "Bold")),
        ("```\n" + "x" * 20000 + "\n```", "x" * 20000, ("Code",)),
        ("- **" + "item" * 5000 + "**", "item" * 5000, ("UnorderedList", "ListItem", "Bold")),
    ],
    ids=["prose", "bold", "code", "list"],
)
def test_large_replies_preserve_content_and_balanced_formatting(markdown, expected, formatting):
    chunks = segments_module.reply_bodies(BOT_18, markdown)
    assert len(chunks) >= 3
    mentions = [
        (i, segment["id"])
        for i, chunk in enumerate(chunks)
        for segment in chunk["body"]["messageSegments"]
        if segment["type"] == "Mention"
    ]
    assert mentions == [(0, BOT_18)]
    runs = [run for chunk in chunks for run in rendered_runs(chunk)]
    assert "".join(value for value, scope in runs if scope) == expected
    assert all(scope == formatting for value, scope in runs if scope)
    for i, chunk in enumerate(chunks):
        text_length = sum(len(value) for value, scope in rendered_runs(chunk))
        paragraph_breaks = sum(
            segment["type"] == "MarkupEnd" and segment["markupType"] in {"Paragraph", "ListItem"}
            for segment in chunk["body"]["messageSegments"]
        )
        assert text_length + paragraph_breaks + (256 if i == 0 else 0) <= 9000


def test_chunking_prefers_whole_paragraphs_and_code_lines():
    paragraphs = segments_module.reply_bodies(None, "a" * 12 + "\n" + "b" * 12, max_length=20)
    assert [plain_text(chunk["body"]) for chunk in paragraphs] == ["a" * 12, "b" * 12]
    code = segments_module.reply_bodies(None, "```\n123456789\nabcdefghij\nXYZ\n```", max_length=20)
    assert [plain_text(chunk["body"]) for chunk in code] == ["123456789", "abcdefghij\nXYZ"]
    assert all(scope == ("Code",) for chunk in code for value, scope in rendered_runs(chunk))


def test_chunking_reopens_lists_without_splitting_short_items():
    chunks = segments_module.reply_bodies(None, "1. first\n2. second\n3. third", max_length=14)
    assert [plain_text(chunk["body"]) for chunk in chunks] == ["first\nsecond", "third"]
    assert all(scope == ("OrderedList", "ListItem") for chunk in chunks for value, scope in rendered_runs(chunk))


def test_chunking_limits_expanded_links_not_markdown_source():
    chunks = segments_module.reply_bodies(None, "[Docs](https://example.com/long-path)", max_length=10)
    values = ["".join(value for value, scope in rendered_runs(chunk)) for chunk in chunks]
    assert "".join(values) == "Docs https://example.com/long-path"
    assert all(len(value) <= 10 for value in values)


@pytest.mark.parametrize("markdown", ["answer\n \n\t", "```\nanswer\n \t\n```", "answer\n-  \n- \t"])
def test_reply_body_trims_trailing_whitespace_and_empty_blocks(markdown):
    result = reply_body(None, markdown)
    assert "".join(value for value, scope in rendered_runs(result)) == "answer"
    assert len(result["body"]["messageSegments"]) == 3


def test_empty_markup_uses_fallback_and_can_be_chunked():
    chunks = segments_module.reply_bodies(None, "```\n \t\n```", max_length=4, empty_reply_text="No answer")
    assert ["".join(value for value, scope in rendered_runs(chunk)) for chunk in chunks] == ["No a", "nswe", "r"]


@pytest.mark.parametrize("limit", [0, -1, 257, 258])
def test_limit_must_leave_room_for_mention_and_reply(limit):
    with pytest.raises(ValueError):
        segments_module.reply_bodies(BOT_18, "answer", max_length=limit)


def test_many_short_paragraphs_budget_their_line_breaks():
    chunks = segments_module.reply_bodies(None, "\n".join("x" for _ in range(9000)))
    assert [plain_text(chunk["body"]).count("x") for chunk in chunks] == [4500, 4500]
