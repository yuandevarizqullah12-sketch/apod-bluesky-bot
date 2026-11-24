"""Image download and preparation for Bluesky (blob limit 1,000,000 bytes)."""
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import List

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger(__name__)
MAX_DOWNLOAD = 60 * 1024 * 1024
MAX_UPLOAD = 950_000  # headroom under Bluesky's 1,000,000-byte image limit


class MediaError(Exception):
    pass


@dataclass
class PreparedImage:
    data: bytes
    width: int
    height: int


def download_image(session: requests.Session, urls: List[str]) -> bytes:
    last = "no image URLs"
    for url in urls:
        try:
            with session.get(url, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                if not r.headers.get("Content-Type", "").startswith("image/"):
                    last = f"not an image: {r.headers.get('Content-Type')}"
                    log.warning("skipping %s (%s)", url, last)
                    continue
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf.extend(chunk)
                    if len(buf) > MAX_DOWNLOAD:
                        raise MediaError("image too large to download")
                return bytes(buf)
        except (requests.RequestException, MediaError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            log.warning("image download failed for %s: %s", url, last)
    raise MediaError(f"could not download image ({last})")


def prepare_image(data: bytes) -> PreparedImage:
    """Re-encode as JPEG, shrinking until it fits under the upload limit."""
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise MediaError(f"cannot decode image: {exc}") from exc
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (0, 0, 0))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        img = bg
    else:
        img = img.convert("RGB")
    for side in (2000, 1600, 1280, 1024, 800):
        work = img.copy()
        work.thumbnail((side, side), Image.LANCZOS)
        for quality in (88, 80, 72, 64):
            buf = BytesIO()
            work.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
            if buf.tell() <= MAX_UPLOAD:
                return PreparedImage(buf.getvalue(), *work.size)
    raise MediaError("could not compress image under the upload limit")
