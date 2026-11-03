"""Shared HTTP helpers: URL validation (blocks private networks) and HTTP clients."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from urllib.parse import quote, unquote, urlsplit

import httpx

# Set DEAI_ALLOW_PRIVATE=1 to allow links to localhost / LAN addresses
# (handy if you run this only on your own machine and want to grab from your NAS etc.)
ALLOW_PRIVATE = os.getenv("DEAI_ALLOW_PRIVATE", "0") == "1"

# Light mode (used on Vercel): the server only finds media; downloads go straight from the
# original site to the visitor's browser or download manager (e.g. IDM).
LIGHT = os.getenv("DEAI_LIGHT", "0") == "1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}


class BlockedURL(ValueError):
    """Raised when a link can't or shouldn't be fetched."""


def normalize_input_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise BlockedURL("Paste a link first.")
    if raw.startswith("//"):
        raw = "https:" + raw
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    return raw


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def host_is_literal_private(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        return _ip_is_blocked(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return False


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("Only http and https links are supported.")
    host = parts.hostname
    if not host:
        raise BlockedURL("That link has no site name in it.")
    if ALLOW_PRIVATE:
        return url
    if host_is_literal_private(host):
        raise BlockedURL("Links to local or private network addresses are blocked.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Behind some proxies DNS isn't resolvable locally; let the request itself fail later.
        return url
    for info in infos:
        try:
            if _ip_is_blocked(ipaddress.ip_address(info[4][0])):
                raise BlockedURL("Links to local or private network addresses are blocked.")
        except ValueError:
            continue
    return url


async def _async_guard(request: httpx.Request) -> None:
    # Runs for every request, including each redirect hop.
    await asyncio.to_thread(check_url, str(request.url))


def _sync_guard(request: httpx.Request) -> None:
    check_url(str(request.url))


def async_client(timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=8,
        timeout=httpx.Timeout(timeout, connect=10),
        headers=BASE_HEADERS,
        event_hooks={"request": [_async_guard]},
    )


def sync_client(timeout: float = 60) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        max_redirects=8,
        timeout=httpx.Timeout(timeout, connect=15),
        headers=BASE_HEADERS,
        event_hooks={"request": [_sync_guard]},
    )


# ---------- filenames ----------

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

MIME_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
    "image/avif": "avif", "image/svg+xml": "svg", "image/bmp": "bmp", "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico", "image/tiff": "tiff", "image/heic": "heic",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov", "video/x-matroska": "mkv",
    "video/ogg": "ogv", "video/x-msvideo": "avi", "video/x-flv": "flv",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
    "audio/flac": "flac", "audio/webm": "weba", "audio/opus": "opus",
    "application/pdf": "pdf", "application/zip": "zip", "application/epub+zip": "epub",
    "application/vnd.apple.mpegurl": "m3u8", "application/x-mpegurl": "m3u8",
    "application/dash+xml": "mpd",
}


def safe_filename(name: str, fallback: str = "download", max_len: int = 150) -> str:
    name = unquote(name or "").strip()
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 6:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def filename_from_headers(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", content_disposition)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition) or \
        re.search(r"filename\s*=\s*([^;]+)", content_disposition)
    return m.group(1).strip() if m else None


def filename_from_url(url: str, mime: str | None = None, fallback: str = "media") -> str:
    path = urlsplit(url).path
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    ext_from_mime = MIME_EXT.get((mime or "").split(";")[0].strip().lower())
    if not base:
        base = fallback
    if "." not in base and ext_from_mime:
        base = f"{base}.{ext_from_mime}"
    return safe_filename(base, fallback)


def content_disposition(kind: str, filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    ascii_name = ascii_name.replace('"', "")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
