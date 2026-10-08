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
