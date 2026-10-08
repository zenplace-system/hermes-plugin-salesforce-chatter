import io

from PIL import Image, ImageDraw

from sfchatter.preview import crop_to_content


def test_crop_removes_trailing_background_and_keeps_margin():
    img = Image.new("RGB", (200, 1000), "white")
    ImageDraw.Draw(img).rectangle((10, 10, 100, 120), fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    with Image.open(io.BytesIO(crop_to_content(buf.getvalue(), margin=20))) as out:
        assert out.size == (200, 141)


def test_blank_page_is_returned_unchanged():
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), "white").save(buf, format="PNG")
    assert crop_to_content(buf.getvalue()) == buf.getvalue()
