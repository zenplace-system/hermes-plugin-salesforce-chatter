import httpx
import pytest

from sfchatter import media


@pytest.fixture
def serve(monkeypatch):
    client_type = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(media.httpx, "AsyncClient", lambda **kwargs: client_type(
            transport=httpx.MockTransport(handler), **kwargs
        ))

    return install


async def test_https_redirects_return_image_without_authorization(serve):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert "authorization" not in request.headers
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/next"})
        if request.url.path == "/next":
            return httpx.Response(307, headers={"location": "https://cdn.example/photo.png?token=private"})
        return httpx.Response(200, headers={"content-type": "image/png; charset=binary"}, content=b"image")

    serve(handler)
    assert await media.download_image("https://example.com/start", max_bytes=5) == (b"image", "image/png", "photo.png")
    assert seen == ["https://example.com/start", "https://example.com/next", "https://cdn.example/photo.png?token=private"]


@pytest.mark.parametrize("url", ["http://example.com/image", "ftp://example.com/image", "https://user:secret@example.com/image"])
async def test_rejects_unsafe_initial_urls_without_request(serve, url):
    def handler(request):
        pytest.fail("Unsafe URL was requested")

    serve(handler)
    with pytest.raises(ValueError):
        await media.download_image(url, max_bytes=10)


@pytest.mark.parametrize("target", ["http://example.com/image", "https://user:secret@example.com/image"])
async def test_rejects_unsafe_redirect_before_request(serve, target):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    serve(handler)
    with pytest.raises(ValueError):
        await media.download_image("https://example.com/start", max_bytes=10)
    assert seen == ["https://example.com/start"]


async def test_stops_redirect_loop(serve):
    requests = 0

    def handler(request):
        nonlocal requests
        requests += 1
        assert requests <= 10, "Redirects must be bounded"
        return httpx.Response(302, headers={"location": "/loop"})

    serve(handler)
    with pytest.raises((ValueError, httpx.TooManyRedirects)):
        await media.download_image("https://example.com/loop", max_bytes=10)


async def test_caps_stream_before_reading_remaining_body(serve):
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"123"
            yield b"456"
            pytest.fail("Read beyond the size limit")

        async def aclose(self):
            self.closed = True

    body = Body()
    serve(lambda request: httpx.Response(200, headers={"content-type": "image/png"}, stream=body))
    with pytest.raises(ValueError, match="size|limit|large"):
        await media.download_image("https://example.com/image.png", max_bytes=5)
    assert body.closed


@pytest.mark.parametrize("mime", [None, "text/html", "application/octet-stream"])
async def test_requires_image_content_type(serve, mime):
    headers = {"content-type": mime} if mime else {}
    serve(lambda request: httpx.Response(200, headers=headers, content=b"not an image"))
    with pytest.raises(ValueError, match="image|content"):
        await media.download_image("https://example.com/image.png", max_bytes=100)


async def test_http_failure_propagates(serve):
    serve(lambda request: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        await media.download_image("https://example.com/image.png", max_bytes=10)
