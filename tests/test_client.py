import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sfchatter.client import ChatterClient, ChatterHTTPError
from sfchatter.settings import Settings


@pytest.fixture
def settings(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    key_path = tmp_path / "k.key"
    key_path.write_bytes(pem)
    env = {
        "SF_CHATTER_LOGIN_URL": "https://test.salesforce.com",
        "SF_CHATTER_CLIENT_ID": "cid",
        "SF_CHATTER_USERNAME": "bot@example.com",
        "SF_CHATTER_PRIVATE_KEY_PATH": str(key_path),
        "SF_CHATTER_BOT_USER_ID": "005000000000ABC",
    }
    return Settings.load(env, {})


async def test_reauthenticates_once_on_401(settings):
    calls = {"token": 0, "feed": 0}

    def handler(request: httpx.Request):
        if request.url.path == "/services/oauth2/token":
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"t{calls['token']}", "instance_url": "https://org.my.salesforce.com"})
        calls["feed"] += 1
        if request.headers["Authorization"] == "Bearer t1":
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "expired"}])
        return httpx.Response(200, json={"elements": []})

    client = ChatterClient(settings, transport=httpx.MockTransport(handler))
    await client.authenticate()
    assert await client.get_json("chatter/feeds/to/me/feed-elements") == {"elements": []}
    assert calls == {"token": 2, "feed": 2}
    await client.aclose()


async def test_error_exposes_status_and_code_only(settings):
    def handler(request: httpx.Request):
        if request.url.path == "/services/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "instance_url": "https://org.my.salesforce.com"})
        return httpx.Response(403, json=[{"errorCode": "INSUFFICIENT_ACCESS", "message": "Sensitive response body"}])

    client = ChatterClient(settings, transport=httpx.MockTransport(handler))
    await client.authenticate()
    with pytest.raises(ChatterHTTPError) as err:
        await client.post_comment("0D5A", {"body": {"messageSegments": []}})
    assert (err.value.status, err.value.code) == (403, "INSUFFICIENT_ACCESS")
    assert "Sensitive response body" not in str(err.value)
    await client.aclose()


async def test_absolute_next_page_url_is_used_as_is(settings):
    seen = []

    def handler(request: httpx.Request):
        if request.url.path == "/services/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "instance_url": "https://org.my.salesforce.com"})
        seen.append(str(request.url))
        return httpx.Response(200, content=json.dumps({}).encode())

    client = ChatterClient(settings, transport=httpx.MockTransport(handler))
    await client.authenticate()
    await client.get_json("/services/data/v66.0/chatter/feeds/to/me/feed-elements?page=2")
    await client.get_json("chatter/comments/0D7A/likes")
    assert seen == [
        "https://org.my.salesforce.com/services/data/v66.0/chatter/feeds/to/me/feed-elements?page=2",
        "https://org.my.salesforce.com/services/data/v66.0/chatter/comments/0D7A/likes",
    ]
    await client.aclose()
