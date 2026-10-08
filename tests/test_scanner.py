from datetime import datetime, timezone

import pytest

from sfchatter.scanner import parse_sf_datetime, probe_groups, scan_feed
from sfchatter.settings import Settings

BOT = "005000000000BOTAAA"
U1 = "005000000000U01AAA"
GROUP = "0F9000000000GRPAAA"
SINCE = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)

ENV = {
    "SF_CHATTER_LOGIN_URL": "https://test.salesforce.com",
    "SF_CHATTER_CLIENT_ID": "cid",
    "SF_CHATTER_USERNAME": "bot@example.com",
    "SF_CHATTER_PRIVATE_KEY_PATH": "/tmp/k.key",
    "SF_CHATTER_BOT_USER_ID": BOT[:15],
}


def seg_body(*segs):
    return {"messageSegments": list(segs)}


def m(uid):
    return {"type": "Mention", "text": "@x", "record": {"id": uid}}


def t(s):
    return {"type": "Text", "text": s}


def comment(cid, uid, created, *segs):
    return {"id": cid, "createdDate": created, "user": {"id": uid, "displayName": "U1"}, "body": seg_body(*segs)}


def element(eid, created, modified, actor, body, comments, total=None, parent=GROUP):
    return {
        "id": eid,
        "feedElementType": "FeedItem",
        "createdDate": created,
        "modifiedDate": modified,
        "actor": {"id": actor, "displayName": "U1"},
        "parent": {"id": parent, "name": "Example group"},
        "body": body,
        "capabilities": {"comments": {"page": {"items": comments, "total": total if total is not None else len(comments)}}},
    }


class FakeClient:
    def __init__(self, pages, records=(), elements=()):
        self.pages = pages
        self.records = list(records)
        self.elements = list(elements)
        self.requested = []
        self.queries = []
        self.fetched = []

    async def get_json(self, path):
        self.requested.append(path)
        for prefix, payload in self.pages.items():
            if path.startswith(prefix):
                return payload
        raise AssertionError(path)

    async def query(self, soql):
        self.queries.append(soql)
        return self.records

    async def fetch_feed_elements(self, ids):
        self.fetched.append(list(ids))
        return [e for e in self.elements if e["id"] in ids]


def with_files(obj, *files):
    obj["capabilities"]["files"] = {"items": list(files)}
    return obj


def file_summary(fid, title, mime, ext=None, size=10):
    return {"id": fid, "title": title, "mimeType": mime, "fileExtension": ext,
            "downloadUrl": f"/services/data/v66.0/connect/files/{fid}/content", "contentSize": size}


def settings(**extra):
    return Settings.load(ENV, extra)


async def test_post_mention_is_candidate_and_bot_post_is_not():
    feed = {
        "elements": [
            element("0D5A", "2026-10-08T03:05:00.000Z", "2026-10-08T03:05:00.000Z", U1, seg_body(m(BOT), t(" Question 1")), []),
            element("0D5B", "2026-10-08T03:04:00.000Z", "2026-10-08T03:04:00.000Z", BOT, seg_body(m(BOT), t(" Self")), []),
        ]
    }
    out = await scan_feed(FakeClient({"chatter/feeds/to/me/feed-elements": feed}), settings(), SINCE)
    assert [(c.source_id, c.kind, c.text) for c in out] == [("0D5A", "post", "Question 1")]


async def test_scan_finds_comment_mention_beyond_embedded_page():
    old_post = element(
        "0D5OLD", "2026-09-01T00:00:00.000Z", "2026-10-08T03:10:00.000Z", U1, seg_body(t("Old post")),
        [comment("0D7Z", U1, "2026-09-02T00:00:00.000Z", t("Old comment"))], total=30,
    )
    full = {
        "items": [comment("0D7Y", U1, "2026-09-02T00:00:00.000Z", t("Earlier discussion"))]
        + [comment("0D7NEW", U1, "2026-10-08T03:10:00.000Z", m(BOT), t(" Question 2"))],
        "nextPageUrl": None,
    }
    client = FakeClient({
        "chatter/feeds/to/me/feed-elements": {"elements": [old_post]},
        "chatter/feed-elements/0D5OLD/capabilities/comments/items": full,
    })
    out = await scan_feed(client, settings(), SINCE)
    assert [(c.source_id, c.kind, c.feed_element_id, c.text, c.post_text) for c in out] == [
        ("0D7NEW", "comment", "0D5OLD", "Question 2", "Old post")
    ]
    assert out[0].thread_context == "U1: Earlier discussion"


async def test_mention_before_since_and_outside_allowed_group_are_ignored():
    feed = {
        "elements": [
            element("0D5A", "2026-10-08T02:00:00.000Z", "2026-10-08T03:01:00.000Z", U1, seg_body(m(BOT), t(" Old")), []),
            element("0D5B", "2026-10-08T03:05:00.000Z", "2026-10-08T03:05:00.000Z", U1, seg_body(m(BOT), t(" Other group")), [], parent="0F9000000000OTHAAA"),
        ]
    }
    out = await scan_feed(FakeClient({"chatter/feeds/to/me/feed-elements": feed}), settings(allowed_parent_ids=[GROUP[:15]]), SINCE)
    assert out == []


async def test_stops_at_elements_modified_before_since():
    feed = {
        "elements": [element("0D5OLD", "2026-10-07T00:00:00.000Z", "2026-10-07T00:00:00.000Z", U1, seg_body(m(BOT)), [])],
        "nextPageUrl": "/services/data/v66.0/chatter/feeds/to/me/feed-elements?page=2",
    }
    client = FakeClient({"chatter/feeds/to/me/feed-elements": feed})
    assert await scan_feed(client, settings(), SINCE) == []
    assert len(client.requested) == 1


async def test_news_feed_setting_changes_path():
    client = FakeClient({"chatter/feeds/news/me/feed-elements": {"elements": []}})
    await scan_feed(client, settings(feed="news"), SINCE)
    assert client.requested[0].startswith("chatter/feeds/news/me/feed-elements?sort=LastModifiedDateDesc")


async def test_probe_queries_only_allowed_groups_since_cursor_and_finds_comment_mention():
    post = element(
        "0D5P", "2026-09-01T00:00:00.000Z", "2026-10-08T03:10:00.000Z", U1, seg_body(t("Old post")),
        [comment("0D7N", U1, "2026-10-08T03:10:00.000Z", m(BOT), t(" Explain"))],
    )
    client = FakeClient({}, records=[{"Id": "0D5P"}], elements=[post])
    out = await probe_groups(client, settings(allowed_parent_ids=[GROUP, "001000000000ACCAAA"]), SINCE)
    assert [(c.source_id, c.kind, c.text) for c in out] == [("0D7N", "comment", "Explain")]
    assert "ParentId IN ('0F9000000000GRPAAA')" in client.queries[0]
    assert "LastModifiedDate >= 2026-10-08T03:00:00Z" in client.queries[0]
    assert client.fetched == [["0D5P"]]


async def test_probe_without_group_parents_does_nothing():
    client = FakeClient({})
    assert await probe_groups(client, settings(), SINCE) == []
    assert client.queries == []


async def test_comment_mention_sees_its_own_and_parent_post_attachments():
    cm = with_files({**comment("0D7F", U1, "2026-10-08T03:10:00.000Z", m(BOT), t(" What is this?")), "capabilities": {}},
                    file_summary("069C", "photo", "image/png", "png"))
    post = with_files(element("0D5F", "2026-10-08T03:05:00.000Z", "2026-10-08T03:10:00.000Z", U1,
                              seg_body(t("Reference")), [cm]),
                      file_summary("069P", "report.pdf", "application/pdf"))
    out = await scan_feed(FakeClient({"chatter/feeds/to/me/feed-elements": {"elements": [post]}}), settings(), SINCE)
    assert [(a.file_id, a.title) for a in out[0].attachments] == [("069C", "photo.png"), ("069P", "report.pdf")]


def test_parse_sf_datetime_accepts_rest_and_soql_formats():
    assert parse_sf_datetime("2026-10-08T13:45:01.000Z") == parse_sf_datetime("2026-10-08T13:45:01.000+0000")


@pytest.fixture(params=[scan_feed, probe_groups], ids=["feed", "group-probe"])
def scan_thread(request):
    async def scan(comments, **extra):
        post = element(
            "0D5THREAD", "2026-09-01T00:00:00.000Z", "2026-10-08T03:20:00.000Z",
            BOT, seg_body(t("Bot-authored parent")), comments,
        )
        client = FakeClient(
            {"chatter/feeds/to/me/feed-elements": {"elements": [post]}},
            records=[{"Id": "0D5THREAD"}], elements=[post],
        )
        config = {"allowed_parent_ids": [GROUP], "allowed_user_ids": [U1[:15]], **extra}
        return await request.param(client, settings(**config), SINCE)
    return scan


@pytest.mark.parametrize("enabled", [None, False, "false", True, "true"])
async def test_unmentioned_followup_requires_opt_in(scan_thread, enabled):
    comments = [
        comment("0D7BOT", BOT, "2026-10-08T02:55:00.000Z", t("Previous answer")),
        comment("0D7FOLLOW", U1, "2026-10-08T03:05:00.000Z", t("Please explain")),
    ]
    extra = {} if enabled is None else {"follow_up_without_mention": enabled}
    out = await scan_thread(comments, **extra)
    if enabled in (True, "true"):
        assert [(c.source_id, c.text, c.follow_up_without_mention) for c in out] == [
            ("0D7FOLLOW", "Please explain", True)
        ]
        assert out[0].thread_context == "U1: Previous answer"
    else:
        assert out == []


@pytest.mark.parametrize("bot_time", [
    None,
    "2026-10-08T03:05:00.000Z",
    "2026-10-08T03:06:00.000Z",
    "2026-10-08T03:04:00.000Z",
    "2026-10-08T04:04:00.000+0100",
])
async def test_followup_requires_strictly_earlier_bot_comment(scan_thread, bot_time):
    comments = [comment("0D7FOLLOW", U1, "2026-10-08T03:05:00.000Z", t("Continue"))]
    if bot_time:
        comments.insert(0, comment("0D7BOT", BOT[:15], bot_time, t("Answer")))
    out = await scan_thread(comments, follow_up_without_mention=True)
    expected = ["0D7FOLLOW"] if bot_time in (
        "2026-10-08T03:04:00.000Z", "2026-10-08T04:04:00.000+0100",
    ) else []
    assert [c.source_id for c in out] == expected


@pytest.mark.parametrize("allowed_users", [[], ["005000000000U02AAA"], [U1[:15]]])
async def test_followups_filter_unauthorized_authors_but_keep_explicit_mentions(scan_thread, allowed_users):
    comments = [
        comment("0D7BOT", BOT, "2026-10-08T02:55:00.000Z", t("Answer")),
        comment("0D7FOLLOW", U1, "2026-10-08T03:05:00.000Z", t("Continue")),
        comment("0D7MENTION", U1, "2026-10-08T03:06:00.000Z", m(BOT), t("Question")),
    ]
    out = await scan_thread(comments, follow_up_without_mention=True, allowed_user_ids=allowed_users)
    expected = [("0D7MENTION", False)]
    if allowed_users == [U1[:15]]:
        expected.insert(0, ("0D7FOLLOW", True))
    assert [(c.source_id, c.follow_up_without_mention) for c in out] == expected


async def test_followups_ignore_other_user_mentions_and_bot_comments(scan_thread):
    comments = [
        comment("0D7BOT", BOT, "2026-10-08T02:55:00.000Z", t("Answer")),
        comment("0D7OTHER", U1, "2026-10-08T03:04:00.000Z", m("005000000000U02AAA"), t("Over to you")),
        comment("0D7SELF", U1, "2026-10-08T03:05:00.000Z", m(U1), t("My update")),
        comment("0D7BOT2", BOT, "2026-10-08T03:06:00.000Z", t("Another answer")),
        comment("0D7MENTION", U1, "2026-10-08T03:07:00.000Z", m(BOT), m(U1), t("Question")),
    ]
    out = await scan_thread(comments, follow_up_without_mention=True, allowed_user_ids=[U1, BOT])
    assert [(c.source_id, c.follow_up_without_mention) for c in out] == [("0D7MENTION", False)]


async def test_followup_cursor_filters_old_comments_and_includes_boundary(scan_thread):
    comments = [
        comment("0D7BOT", BOT, "2026-10-08T02:55:00.000Z", t("Answer")),
        comment("0D7OLD", U1, "2026-10-08T02:59:59.000Z", t("Old followup")),
        comment("0D7BOUNDARY", U1, "2026-10-08T03:00:00.000Z", t("New followup")),
    ]
    out = await scan_thread(comments, follow_up_without_mention=True)
    assert [(c.source_id, c.kind, c.feed_element_id) for c in out] == [
        ("0D7BOUNDARY", "comment", "0D5THREAD")
    ]


@pytest.mark.parametrize("scan", [scan_feed, probe_groups], ids=["feed", "group-probe"])
async def test_followup_finds_earlier_bot_comment_on_full_comment_pages(scan):
    followup = comment("0D7FOLLOW", U1, "2026-10-08T03:05:00.000Z", t("Continue"))
    post = element(
        "0D5PAGED", "2026-09-01T00:00:00.000Z", "2026-10-08T03:05:00.000Z",
        U1, seg_body(t("Original question")), [followup], total=2,
    )
    client = FakeClient({
        "chatter/feeds/to/me/feed-elements": {"elements": [post]},
        "chatter/feed-elements/0D5PAGED/capabilities/comments/items": {
            "items": [comment("0D7BOT", BOT, "2026-09-01T00:10:00.000Z", t("Previous answer"))],
            "nextPageUrl": "/comments/page2",
        },
        "/comments/page2": {"items": [followup]},
    }, records=[{"Id": "0D5PAGED"}], elements=[post])
    out = await scan(client, settings(
        follow_up_without_mention=True, allowed_user_ids=[U1], allowed_parent_ids=[GROUP],
    ), SINCE)
    assert [(c.source_id, c.follow_up_without_mention) for c in out] == [("0D7FOLLOW", True)]
