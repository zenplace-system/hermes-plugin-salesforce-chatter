"""Download public images without reusing Salesforce credentials."""

import mimetypes
from pathlib import PurePosixPath

import httpx
from tools.url_safety import create_ssrf_safe_async_client, is_safe_url


class UnsafeImageURL(ValueError):
    """The URL (or a redirect hop) points at a private, internal or metadata address."""


async def download_image(url: str, *, max_bytes: int) -> tuple[bytes, str, str]:
    """Fetch a public HTTPS image, allowing at most five HTTPS redirects.

    The URL comes from the model's reply, so it is untrusted: every hop must pass
    Hermes's SSRF check (``is_safe_url``) before it is requested, and the client
    re-validates the resolved address at connect time. Invalid or unsafe URLs,
    media types, and oversized bodies raise ValueError; network and HTTP failures
    retain their httpx exception types.
    """
    if max_bytes <= 0:
        raise ValueError("Image size limit must be positive")
    current = httpx.URL(url)
    # Keep this client separate from the authenticated Salesforce client.
    async with create_ssrf_safe_async_client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
        for redirects in range(6):
            if current.scheme != "https" or not current.host or current.userinfo:
                raise ValueError("Image URLs must use HTTPS without credentials")
            if not is_safe_url(str(current)):
                raise UnsafeImageURL("Image URL targets a private or internal address")
            async with client.stream("GET", current) as response:
                if response.is_redirect and "location" in response.headers:
                    if redirects == 5:
                        raise ValueError("Image redirect limit exceeded")
                    current = current.join(response.headers["location"])
                    continue
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not mime.startswith("image/") or not mime[6:]:
                    raise ValueError("Image response must have an image content type")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunk) > max_bytes - len(data):
                        raise ValueError("Image exceeds size limit")
                    data.extend(chunk)
                filename = PurePosixPath(current.path).name
                if not filename or filename in {".", ".."}:
                    filename = "image" + (mimetypes.guess_extension(mime) or "")
                return bytes(data), mime, filename
    raise ValueError("Image redirect limit exceeded")
