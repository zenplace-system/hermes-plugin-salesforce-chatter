import pytest

from sfchatter.settings import Settings

ENV = {
    "SF_CHATTER_LOGIN_URL": "https://test.salesforce.com",
    "SF_CHATTER_CLIENT_ID": "cid",
    "SF_CHATTER_USERNAME": "bot@example.com",
    "SF_CHATTER_PRIVATE_KEY_PATH": "/tmp/k.key",
    "SF_CHATTER_BOT_USER_ID": "005000000000ABC",
}


def test_poll_interval_is_raised_to_60_and_dry_run_defaults_true():
    s = Settings.load(ENV, {"poll_interval_seconds": 15})
    assert s.poll_interval_seconds == 60
    assert s.dry_run is True


def test_extra_parses_lists_and_bools():
    s = Settings.load(ENV, {"allowed_parent_ids": ["0F9A", "0F9B"], "dry_run": "false", "feed": "news"})
    assert s.allowed_parent_ids == ("0F9A", "0F9B")
    assert s.dry_run is False
    assert s.feed == "news"


def test_missing_required_env_raises():
    env = dict(ENV)
    del env["SF_CHATTER_BOT_USER_ID"]
    with pytest.raises(ValueError, match="SF_CHATTER_BOT_USER_ID"):
        Settings.load(env, {})


def test_unknown_feed_raises():
    with pytest.raises(ValueError, match="feed"):
        Settings.load(ENV, {"feed": "company"})


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False), (True, True), (False, False)])
def test_followup_setting_parses_explicit_opt_in(value, expected):
    assert Settings.load(ENV, {"follow_up_without_mention": value}).follow_up_without_mention is expected


def test_approval_hint_can_be_localized_or_disabled():
    localized = Settings.load(ENV, {"approval_hint": "Mention @{bot_name} to respond."})
    assert localized.approval_hint.format(bot_name="Assistant") == "Mention @Assistant to respond."
    assert Settings.load(ENV, {"approval_hint": ""}).approval_hint == ""
