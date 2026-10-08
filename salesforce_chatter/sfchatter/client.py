"""Connect REST using bot JWT authentication; never log tokens or response bodies."""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from .settings import Settings


class ChatterHTTPError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"Salesforce HTTP {status} {code}")
        self.status = status
        self.code = code


def _error_code(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return "UNKNOWN"
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return str(data[0].get("errorCode") or "UNKNOWN")
    if isinstance(data, dict):
        return str(data.get("error") or "UNKNOWN")
    return "UNKNOWN"


class ChatterClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._s = settings
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)
        self._token: str | None = None
        self._instance_url: str | None = None

    async def authenticate(self) -> None:
        key = self._s.private_key_path.read_text()
        assertion = jwt.encode(
            {"iss": self._s.client_id, "sub": self._s.username, "aud": self._s.login_url, "exp": int(time.time()) + 180},
            key,
            algorithm="RS256",
        )
        response = await self._http.post(
            f"{self._s.login_url}/services/oauth2/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
        if response.status_code != 200:
            raise ChatterHTTPError(response.status_code, _error_code(response))
        data = response.json()
        self._token = data["access_token"]
        self._instance_url = data["instance_url"].rstrip("/")

    def _url(self, path: str) -> str:
        if path.startswith("/"):
            return f"{self._instance_url}{path}"
        return f"{self._instance_url}/services/data/{self._s.api_version}/{path}"

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in (1, 2):
            response = await self._http.request(
                method, self._url(path), headers={"Authorization": f"Bearer {self._token}"}, **kwargs
            )
            if response.status_code == 401 and attempt == 1:
                await self.authenticate()
                continue
            if response.status_code >= 400:
                raise ChatterHTTPError(response.status_code, _error_code(response))
            return response
        raise ChatterHTTPError(401, "INVALID_SESSION_ID")

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._send(method, path, json=json)
        return response.json() if response.content else {}

    async def get_json(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path)

    async def query(self, soql: str) -> list[dict[str, Any]]:
        response = await self._send("GET", "query", params={"q": soql})
        return list(response.json().get("records") or [])

    async def fetch_feed_elements(self, ids: list[str]) -> list[dict[str, Any]]:
        """Fetch feed elements in batches (API limit: 500); omit inaccessible items."""
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), 100):
            chunk = ",".join(ids[start:start + 100])
            data = await self.get_json(f"chatter/feed-elements/batch/{chunk}")
            for item in data.get("results") or []:
                if item.get("statusCode") == 200 and isinstance(item.get("result"), dict):
                    out.append(item["result"])
        return out

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """Download attachment bytes, aborting with ValueError above the size cap."""
        for attempt in (1, 2):
            async with self._http.stream(
                "GET", self._url(url), headers={"Authorization": f"Bearer {self._token}"}, follow_redirects=True
            ) as response:
                if response.status_code == 401 and attempt == 1:
                    await self.authenticate()
                    continue
                if response.status_code >= 400:
                    raise ChatterHTTPError(response.status_code, "DOWNLOAD_FAILED")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("attachment too large")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise ChatterHTTPError(401, "INVALID_SESSION_ID")

    async def upload_file(self, data: bytes, *, filename: str, mime_type: str) -> str:
        """Upload to the bot's Files and return the ContentDocument ID (069 prefix)."""
        response = await self._send(
            "POST", "connect/files/users/me",
            files={"fileData": (filename, data, mime_type)}, data={"title": filename},
        )
        return str(response.json()["id"])

    async def like_feed_element(self, feed_element_id: str) -> None:
        await self._request("POST", f"chatter/feed-elements/{feed_element_id}/capabilities/chatter-likes/items")

    async def like_comment(self, comment_id: str) -> None:
        await self._request("POST", f"chatter/comments/{comment_id}/likes")

    async def post_comment(self, feed_element_id: str, body: dict[str, Any], *, file_id: str | None = None) -> dict[str, Any]:
        payload = dict(body)
        if file_id:
            payload["capabilities"] = {"content": {"contentDocumentId": file_id}}
        return await self._request("POST", f"chatter/feed-elements/{feed_element_id}/capabilities/comments/items", json=payload)

    async def post_feed_item(self, subject_id: str, body: dict[str, Any], *, file_id: str | None = None) -> dict[str, Any]:
        """Create a group or record feed post for scheduled home-channel delivery."""
        payload = {"feedElementType": "FeedItem", "subjectId": subject_id, **body}
        if file_id:
            payload["capabilities"] = {"content": {"contentDocumentId": file_id}}
        return await self._request("POST", "chatter/feed-elements", json=payload)

    async def aclose(self) -> None:
        await self._http.aclose()
