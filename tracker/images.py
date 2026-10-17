"""Product image fetch + disk cache. Micro Center's image CDN rejects plain browser
hotlinks, so images are fetched with the scraper's TLS fingerprint and served locally."""
import hashlib
import logging
from typing import Optional
from urllib.parse import urlparse

from curl_cffi import requests as cffi

from . import config

log = logging.getLogger("images")
ALLOWED_HOSTS = ("microcenter.com", "media-amazon.com", "ssl-images-amazon.com", "images-amazon.com")
MAX_BYTES = 5 * 1024 * 1024


def allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return urlparse(url).scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def get(url: str) -> Optional[tuple[bytes, str]]:
    """Return (bytes, content_type) or None. Blocking; call from a thread."""
    if not url or not allowed(url):
        return None
    cache = config.DATA_DIR / "img"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / hashlib.sha1(url.encode()).hexdigest()
    if path.exists():
        data = path.read_bytes()
        return data, _ctype(data)
    try:
        referer = "https://www.microcenter.com/" if "microcenter" in url else "https://www.amazon.com/"
        r = cffi.get(url, impersonate="safari184", timeout=20, headers={"Referer": referer})
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) > MAX_BYTES:
            return None
        path.write_bytes(r.content)
        return r.content, ctype
    except Exception as e:
        log.debug("image fetch failed %s: %s", url, e)
        return None


def _ctype(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "application/octet-stream"
