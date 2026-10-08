"""Render HTML as PNG without depending on Hermes.

Chatter cannot preview HTML attachments, so include an image with the original file.
Use Chromium from the Hermes Docker sandbox image instead of opening generated HTML
in a host browser, which can also hang under launchd on macOS.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "nousresearch/hermes-sandbox:desktop"
_DOCKER_FALLBACKS = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker", "~/.orbstack/bin/docker")
# Locate Chromium inside the image's versioned Playwright directory.
_CHROME_SCRIPT = (
    'chrome=$(ls /opt/playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1); '
    '[ -n "$chrome" ] || chrome=$(command -v chromium || command -v chromium-browser); '
    'exec "$chrome" --headless=new --no-sandbox --disable-gpu --hide-scrollbars --no-first-run '
    '--user-data-dir=/tmp/chromium '
    '--virtual-time-budget=5000 --window-size="$W,$H" --screenshot=/r/out.png file:///r/in.html'
)


def find_docker() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in _DOCKER_FALLBACKS:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def crop_to_content(png: bytes, *, margin: int = 32) -> bytes:
    """Trim trailing background rows, keeping the margin; leave blank images unchanged."""
    from PIL import Image, ImageChops

    with Image.open(io.BytesIO(png)) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        background = Image.new("RGB", rgb.size, rgb.getpixel((0, height - 1)))
        bbox = ImageChops.difference(rgb, background).getbbox()
        if not bbox:
            return png
        bottom = min(height, bbox[3] + margin)
        out = io.BytesIO()
        rgb.crop((0, 0, width, bottom)).save(out, format="PNG", optimize=True)
        return out.getvalue()


async def render_html_preview(
    html: bytes,
    *,
    work_dir: Path,
    image: str = DEFAULT_IMAGE,
    width: int = 1280,
    max_height: int = 6000,
    timeout: float = 60.0,
) -> bytes | None:
    """Return a PNG, or None if rendering fails so callers can send only the HTML."""
    docker = find_docker()
    if not docker:
        logger.warning("html preview skipped: docker not found")
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        root = Path(tmp)
        (root / "in.html").write_bytes(html)
        # Chromium's profile and crash reporter need a writable home on the tmpfs.
        proc = await asyncio.create_subprocess_exec(
            docker, "run", "--rm", "--network", "none",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "--memory", "1g", "--cpus", "1", "--pids-limit", "256",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
            "-e", "HOME=/tmp", "-e", f"W={width}", "-e", f"H={max_height}",
            "-v", f"{root}:/r", "--entrypoint", "sh", image, "-c", _CHROME_SCRIPT,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("html preview timed out after %ss", timeout)
            return None
        out = root / "out.png"
        if proc.returncode != 0 or not out.is_file():
            logger.warning("html preview failed rc=%s", proc.returncode)
            return None
        png = out.read_bytes()
    return await asyncio.to_thread(crop_to_content, png)
