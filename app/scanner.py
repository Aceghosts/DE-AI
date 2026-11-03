"""Find every downloadable piece of media on a web page.

Three sources are combined:
  1. The page's HTML (img/picture/video/audio/source tags, links, meta tags,
     CSS backgrounds, JSON-LD, and media URLs buried in scripts).
  2. yt-dlp, which understands 1,800+ video/audio sites (YouTube, Instagram,
     TikTok, X, Facebook, Vimeo, SoundCloud, Reddit ...) and HLS/DASH streams.
  3. Optionally, a real headless browser that renders the page, scrolls it, and
     records every image/video/audio request the page makes ("deep scan").
"""
from __future__ import annotations

import asyncio
import hashlib
import html as htmllib
import json
import re
import shutil
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .net import (LIGHT, BlockedURL, async_client, check_url, filename_from_headers,
                  filename_from_url, host_is_literal_private, normalize_input_url,
                  ALLOW_PRIVATE, USER_AGENT)

EXT_KIND = {}
for _kind, _exts in {
    "image": "jpg jpeg jpe jfif png gif webp avif svg bmp ico tif tiff heic heif apng",
    "video": "mp4 webm mov mkv avi m4v ogv flv wmv 3gp ts",
    "audio": "mp3 wav ogg oga m4a aac flac opus weba wma mid midi amr",
    "document": "pdf doc docx xls xlsx ppt pptx odt ods odp rtf csv epub mobi zip rar 7z gz tar apk exe dmg psd ai eps",
    "stream": "m3u8 mpd",
}.items():
    for _e in _exts.split():
        EXT_KIND[_e] = _kind

EMBED_HOSTS = re.compile(
    r"(youtube\.com|youtube-nocookie\.com|youtu\.be|vimeo\.com|dailymotion\.com|"
    r"soundcloud\.com|twitch\.tv|streamable\.com|facebook\.com/plugins/video|"
    r"instagram\.com/(p|reel)/|tiktok\.com/embed|bandcamp\.com|rumble\.com|"
    r"bilibili\.com|wistia\.(com|net)|jwplayer|brightcove|loom\.com|ted\.com)",
    re.I,
)

MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 400
MAX_PROBES = 220

RAW_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()\\{}|^`]+?\.(?:%s)(?:\?[^\s"'<>()\\{}|^`]*)?(?=["'\s<>()\\,;]|$)"""
    % "|".join(sorted(EXT_KIND, key=len, reverse=True)),
    re.I,
)
CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.I)


def ext_of(url: str) -> str:
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()[:6]


def kind_from_mime(mime: str | None) -> str | None:
    m = (mime or "").split(";")[0].strip().lower()
    if not m:
        return None
    if "mpegurl" in m or m == "application/dash+xml":
        return "stream"
    for k in ("image", "video", "audio"):
        if m.startswith(k + "/"):
            return k
    if m in ("application/pdf", "application/zip", "application/epub+zip", "application/msword",
             "application/x-rar-compressed", "application/x-7z-compressed",
             "application/vnd.android.package-archive", "application/octet-stream") \
            or m.startswith("application/vnd.openxmlformats") or m.startswith("application/vnd.ms-"):
        return "document"
    return None


def _item_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


class Collector:
    def __init__(self, base_url: str):
        self.base = base_url
        self.items: dict[str, dict[str, Any]] = {}
        self.embeds: dict[str, str] = {}

    def resolve(self, raw: str | None) -> str | None:
        if not raw:
            return None
        raw = htmllib.unescape(raw.strip()).replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&")
        if not raw or raw.startswith(("data:", "blob:", "javascript:", "about:", "mailto:", "tel:", "#")):
            return None
        if raw.startswith("//"):
            raw = urlsplit(self.base).scheme + ":" + raw
        try:
            url = urldefrag(urljoin(self.base, raw))[0]
        except ValueError:
            return None
        if not url.startswith(("http://", "https://")) or len(url) > 3000:
            return None
        return url

    def add(self, raw: str | None, kind: str | None = None, source: str = "", title: str = "",
            thumb: str | None = None, width: Any = None, height: Any = None, strict: bool = False) -> None:
        url = self.resolve(raw)
        if not url:
            return
        ext = ext_of(url)
        ext_kind = EXT_KIND.get(ext)
        if strict and not ext_kind:
            return
        kind = ext_kind if ext_kind in ("stream",) else (kind or ext_kind)
        if not kind:
            kind = "unknown"
        if url in self.items:
            it = self.items[url]
            if not it.get("title") and title:
                it["title"] = title
            if not it.get("thumb") and thumb:
                it["thumb"] = self.resolve(thumb)
            if it["kind"] == "unknown" and kind != "unknown":
                it["kind"] = kind
            return
        if len(self.items) >= MAX_ITEMS:
            return
        self.items[url] = {
            "id": _item_id(url),
            "url": url,
            "kind": kind,
            "ext": ext,
            "source": source,
            "title": (title or "").strip()[:200],
            "thumb": self.resolve(thumb) if thumb else None,
            "width": _int(width),
            "height": _int(height),
            "mode": "ytdlp" if kind == "stream" else "direct",
        }

    def add_embed(self, raw: str | None, title: str = "") -> None:
        url = self.resolve(raw)
        if url and EMBED_HOSTS.search(url) and url not in self.embeds:
            self.embeds[url] = title


def _int(v: Any) -> int | None:
    try:
        n = int(str(v).strip().rstrip("px"))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def best_from_srcset(value: str | None) -> str | None:
    if not value:
        return None
    best, best_score = None, -1.0
    for part in re.split(r",\s+", value.strip()):
        bits = part.strip().split()
        if not bits:
            continue
        score = 1.0
        if len(bits) > 1:
            d = bits[-1].lower()
            try:
                if d.endswith("w"):
                    score = float(d[:-1])
                elif d.endswith("x"):
                    score = float(d[:-1]) * 1000
            except ValueError:
                pass
        if score > best_score:
            best, best_score = bits[0], score
    return best


IMG_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-lazy", "data-url",
             "data-full", "data-full-src", "data-zoom-src", "data-zoom-image", "data-hi-res-src",
             "data-large", "data-large-file", "data-orig-file", "data-image", "data-bg")
SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset")


def parse_html(html: str, base_url: str) -> tuple[Collector, dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    base_tag = soup.find("base", href=True)
    base = urljoin(base_url, base_tag["href"]) if base_tag else base_url
    c = Collector(base)

    meta: dict[str, str] = {}
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        val = m.get("content")
        if key and val:
            meta.setdefault(key, val)
    page_title = (meta.get("og:title") or (soup.title.string if soup.title and soup.title.string else "")
                  or "").strip()

    # Social / Open Graph media first: these are usually "the" media of the page.
    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        if key in meta:
            c.add(meta[key], "video", "page preview (og)", page_title)
    for key in ("og:audio:secure_url", "og:audio:url", "og:audio"):
        if key in meta:
            c.add(meta[key], "audio", "page preview (og)", page_title)
    for key in ("og:image:secure_url", "og:image:url", "og:image", "twitter:image", "twitter:image:src",
                "image", "thumbnailurl"):
        if key in meta:
            c.add(meta[key], "image", "page preview (og)", page_title)

    for tag in soup.find_all(["video", "audio"]):
        kind = tag.name
        title = tag.get("title") or tag.get("aria-label") or ""
        poster = tag.get("poster") or tag.get("data-poster")
        for a in ("src", "data-src"):
            c.add(tag.get(a), kind, f"<{kind}> tag", title, thumb=poster)
        for s in tag.find_all("source"):
            c.add(s.get("src") or s.get("data-src"), kind_from_mime(s.get("type")) or kind,
                  f"<{kind}> tag", title, thumb=poster)
        if poster:
            c.add(poster, "image", "video poster", title)

    for pic in soup.find_all("picture"):
        for s in pic.find_all("source"):
            c.add(best_from_srcset(s.get("srcset") or s.get("data-srcset")), "image", "<img> tag")

    for img in soup.find_all("img"):
        title = img.get("alt") or img.get("title") or ""
        w, h = img.get("width"), img.get("height")
        best = None
        for a in SRCSET_ATTRS:
            best = best or best_from_srcset(img.get(a))
        if best:
            c.add(best, "image", "<img> tag", title, width=None, height=None)
        for a in IMG_ATTRS:
            v = img.get(a)
            if a == "src" and best:
                continue  # srcset already gave us the largest version of this image
            if v:
                c.add(v, "image", "<img> tag", title, width=w if a == "src" else None,
                      height=h if a == "src" else None)

    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        as_ = (link.get("as") or "").lower()
        if "image_src" in rel or (rel == "preload" and as_ in ("image", "video", "audio")):
            c.add(link["href"], as_ or "image", "link tag")
        elif "icon" in rel:
            c.add(link["href"], "image", "site icon")
        elif (link.get("type") or "").startswith(("video/", "audio/", "image/")):
            c.add(link["href"], kind_from_mime(link.get("type")), "link tag")

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)[:120] or a.get("title") or ""
        c.add(a["href"], None, "link on page", text, strict=True)
        if EMBED_HOSTS.search(a["href"] or "") and re.search(r"(watch\?v=|youtu\.be/|vimeo\.com/\d+)", a["href"]):
            c.add_embed(a["href"], text)

    for fr in soup.find_all(["iframe", "embed", "object"]):
        src = fr.get("src") or fr.get("data-src") or fr.get("data")
        c.add_embed(src, fr.get("title") or "")
        if fr.name != "iframe":
            c.add(src, None, f"<{fr.name}> tag", strict=True)

    # CSS backgrounds (inline style + <style> blocks)
    for el in soup.find_all(style=True):
        for m in CSS_URL_RE.finditer(el["style"]):
            c.add(m.group(2), "image", "CSS background")
    for st in soup.find_all("style"):
        for m in CSS_URL_RE.finditer(st.get_text() or ""):
            u = m.group(2)
            if EXT_KIND.get(ext_of(u)) == "image":
                c.add(u, "image", "CSS background")

    # JSON-LD structured data
    for sc in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except (ValueError, TypeError):
            continue
        _walk_jsonld(data, c)

    # Anything that looks like a media URL anywhere in the raw source (scripts, JSON blobs...)
    raw = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/").replace("&amp;", "&")
    for m in RAW_URL_RE.finditer(raw):
        c.add(m.group(0), None, "found in page code", strict=True)

    info = {"title": page_title, "site_name": meta.get("og:site_name", "")}
    return c, info


def _walk_jsonld(node: Any, c: Collector, depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(node, list):
        for n in node[:50]:
            _walk_jsonld(n, c, depth + 1)
        return
    if not isinstance(node, dict):
        return
    typ = str(node.get("@type", "")).lower()
    name = str(node.get("name") or node.get("headline") or "")
    kind = "video" if "video" in typ else "audio" if "audio" in typ or "music" in typ else None
    for key in ("contentUrl", "contentURL"):
        v = node.get(key)
        if isinstance(v, str):
            c.add(v, kind, "structured data", name)
    v = node.get("embedUrl")
    if isinstance(v, str):
        c.add_embed(v, name)
    for key in ("thumbnailUrl", "image", "logo"):
        v = node.get(key)
        vals = v if isinstance(v, list) else [v]
        for x in vals[:10]:
            if isinstance(x, dict):
                x = x.get("url") or x.get("contentUrl")
            if isinstance(x, str):
                c.add(x, "image", "structured data", name)
    for k, v in node.items():
        if isinstance(v, (dict, list)) and k not in ("image", "thumbnailUrl", "logo"):
            _walk_jsonld(v, c, depth + 1)


# ---------------- probing (size / type) ----------------

async def probe(client: httpx.AsyncClient, item: dict, referer: str, sem: asyncio.Semaphore) -> None:
    async with sem:
        headers = {"Referer": referer, "Accept-Encoding": "identity"}
        resp = None
        try:
            resp = await client.head(item["url"], headers=headers, timeout=8)
            if resp.status_code in (403, 405, 501) or resp.status_code >= 500 \
                    or "content-length" not in resp.headers:
                async with client.stream("GET", item["url"], timeout=8,
                                         headers={**headers, "Range": "bytes=0-0"}) as r:
                    resp = r
        except (httpx.HTTPError, BlockedURL):
            item["reachable"] = False
            return
        except Exception:
            return
        h = resp.headers
        item["status"] = resp.status_code
        item["reachable"] = resp.status_code < 400
        mime = (h.get("content-type") or "").split(";")[0].strip().lower()
        if mime:
            item["mime"] = mime
        size = None
        cr = h.get("content-range")
        if cr and "/" in cr:
            tot = cr.rsplit("/", 1)[-1]
            size = int(tot) if tot.isdigit() else None
        elif h.get("content-length", "").isdigit() and resp.status_code == 200:
            size = int(h["content-length"])
        if size:
            item["size"] = size
        name = filename_from_headers(h.get("content-disposition"))
        if name:
            item["name"] = name
        mk = kind_from_mime(mime)
        if mk and (item["kind"] in ("unknown", "document") or mk != item["kind"]) and mime != "application/octet-stream":
            item["kind"] = mk
            if mk == "stream":
                item["mode"] = "ytdlp"
        if mime.startswith("text/html") and item["source"] in ("link on page", "found in page code"):
            item["drop"] = True


# ---------------- yt-dlp ----------------

HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _options(has_video: bool, heights: list[int]) -> list[dict]:
    ff = ffmpeg_available()
    opts: list[dict] = []
    if has_video:
        top = max(heights) if heights else None
        opts.append({"id": "best", "label": f"Best quality ({top}p)" if top else "Best quality"})
        if ff:
            for h in HEIGHTS:
                if top and h < top and any(x >= h for x in heights):
                    opts.append({"id": f"h{h}", "label": f"{h}p"})
                elif not top and h in (1080, 720, 480):
                    opts.append({"id": f"h{h}", "label": f"Up to {h}p"})
        opts.append({"id": "audio", "label": "Audio only (original format)"})
    else:
        opts.append({"id": "audio", "label": "Best audio (original format)"})
    if ff:
        opts.append({"id": "mp3", "label": "Audio only as MP3"})
    return opts


def _thumb(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    for t in reversed(thumbs):
        if t.get("url"):
            return t["url"]
    return None


class QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def ytdlp_probe(url: str) -> dict | None:
    from yt_dlp import YoutubeDL
    from .ytopts import base_opts
    opts = {**base_opts(), "skip_download": True, "extract_flat": "in_playlist", "playlistend": 60}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def _direct_format(formats: list[dict], has_video: bool) -> dict | None:
    """Best single file that already has both video and audio (or best audio), fetchable over plain HTTP."""
    cands = []
    for f in formats:
        if not f.get("url") or not str(f.get("protocol", "https")).startswith("http"):
            continue
        v, a = f.get("vcodec"), f.get("acodec")
        if has_video and (v in (None, "none") or a == "none"):
            continue
        if not has_video and a in (None, "none"):
            continue
        cands.append(f)
    if not cands:
        return None
    best = max(cands, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))
    return {"url": best["url"], "ext": best.get("ext"), "height": best.get("height"),
            "size": best.get("filesize") or best.get("filesize_approx")}


def items_from_ytdlp(info: dict, page_url: str) -> list[dict]:
    out: list[dict] = []
    extractor = info.get("extractor_key") or info.get("ie_key") or ""
    generic = extractor.lower() == "generic"
    if info.get("_type") in ("playlist", "multi_video") and info.get("entries"):
        for e in info["entries"]:
            if not e:
                continue
            # Generic-extractor entries point at the media file itself; site playlists point at pages.
            eurl = (e.get("url") or e.get("webpage_url")) if generic else (e.get("webpage_url") or e.get("url"))
            if not eurl:
                continue
            if not eurl.startswith("http"):
                eurl = urljoin(page_url, eurl)
            out.append({
                "direct_url": eurl if generic else None,
                "id": _item_id("yt:" + eurl), "url": eurl, "kind": "video", "mode": "ytdlp",
                "title": e.get("title") or "Untitled", "thumb": _thumb(e),
                "duration": e.get("duration"), "uploader": e.get("uploader") or e.get("channel"),
                "source": "media player on page" if generic else f"{extractor or 'site'} playlist",
                "options": _options(True, []),
                "playlist_title": info.get("title"),
            })
        return out

    formats = info.get("formats") or ([info] if info.get("url") else [])
    has_video = any((f.get("vcodec") not in ("none",)) and (f.get("height") or f.get("vcodec"))
                    for f in formats) or (info.get("vcodec") not in (None, "none"))
    if formats and all(f.get("vcodec") == "none" for f in formats):
        has_video = False
    heights = sorted({int(f["height"]) for f in formats if f.get("height") and f.get("vcodec") != "none"})
    size = info.get("filesize") or info.get("filesize_approx")
    direct = _direct_format(formats, has_video) if LIGHT else None
    out.append({
        "id": _item_id("yt:" + (info.get("webpage_url") or page_url) + str(info.get("id"))),
        "url": info.get("webpage_url") or page_url,
        "kind": "video" if has_video else "audio",
        "mode": "ytdlp",
        "title": info.get("title") or "Untitled",
        "thumb": _thumb(info),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "width": info.get("width"), "height": max(heights) if heights else info.get("height"),
        "size": size,
        "source": "media player on page" if generic else extractor,
        "options": _options(has_video, heights),
        "direct_url": info.get("url") if generic else None,
        "direct": direct,
    })
    if not generic and _thumb(info):
        out.append({
            "id": _item_id(_thumb(info)), "url": _thumb(info), "kind": "image", "mode": "direct",
            "ext": ext_of(_thumb(info)), "title": f"Thumbnail: {info.get('title') or ''}".strip(),
            "source": "thumbnail", "name": None,
        })
    return out


# ---------------- headless browser (deep scan) ----------------

_browser_state: dict[str, Any] = {"checked": False, "ok": False, "why": ""}


async def browser_available() -> tuple[bool, str]:
    if LIGHT:
        return False, "Scanning in a browser isn't available in the light version."
    if _browser_state["checked"]:
        return _browser_state["ok"], _browser_state["why"]
    _browser_state["checked"] = True
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        _browser_state["why"] = "Install Playwright to enable deep scan (see README)."
        return False, _browser_state["why"]
    try:
        b, pw = await _launch()
        await b.close()
        await pw.stop()
        _browser_state["ok"] = True
    except Exception as e:  # noqa: BLE001
        _browser_state["why"] = "No headless browser found. Run: python -m playwright install chromium"
        _browser_state["detail"] = str(e)[:300]
    return _browser_state["ok"], _browser_state["why"]


async def _launch():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    last = None
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            return await pw.chromium.launch(headless=True, **kwargs), pw
        except Exception as e:  # noqa: BLE001
            last = e
    await pw.stop()
    raise last  # type: ignore[misc]


async def render_page(url: str, timeout_s: int = 30) -> tuple[str, str, list[tuple[str, str, str]]]:
    browser, pw = await _launch()
    seen: list[tuple[str, str, str]] = []
    try:
        ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()

        if not ALLOW_PRIVATE:
            async def guard(route):
                host = urlsplit(route.request.url).hostname or ""
                if host_is_literal_private(host):
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", guard)

        def on_response(resp):
            try:
                rt = resp.request.resource_type
                ct = (resp.headers.get("content-type") or "").lower()
                u = resp.url
                if rt in ("image", "media") or ct.startswith(("image/", "video/", "audio/")) \
                        or "mpegurl" in ct or "dash+xml" in ct or ext_of(u) in ("m3u8", "mpd"):
                    seen.append((u, ct, rt))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="networkidle", timeout=timeout_s * 1000)
        except Exception:  # noqa: BLE001  (timeouts are fine; we take what loaded)
            pass
        for _ in range(8):  # scroll to trigger lazy-loading
            try:
                await page.mouse.wheel(0, 1400)
                await page.wait_for_timeout(450)
            except Exception:  # noqa: BLE001
                break
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001
            pass
        html = await page.content()
        final = page.url
    finally:
        await browser.close()
        await pw.stop()
    return html, final, seen


# ---------------- orchestration ----------------

async def fetch_page(client: httpx.AsyncClient, url: str) -> dict:
    async with client.stream("GET", url, headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}) as r:
        final = str(r.url)
        mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if r.status_code >= 400:
            return {"status": r.status_code, "final": final, "mime": mime, "html": None}
        if mime and not ("html" in mime or "xml" in mime or mime.startswith("text/")):
            size = r.headers.get("content-length")
            return {"status": r.status_code, "final": final, "mime": mime, "html": None, "direct": True,
                    "size": int(size) if size and size.isdigit() else None,
                    "name": filename_from_headers(r.headers.get("content-disposition"))}
        chunks, total = [], 0
        async for chunk in r.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PAGE_BYTES:
                break
        body = b"".join(chunks)
        enc = r.encoding or "utf-8"
        try:
            text = body.decode(enc, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return {"status": r.status_code, "final": final, "mime": mime, "html": text}


KIND_ORDER = {"video": 0, "audio": 1, "stream": 2, "image": 3, "document": 4, "unknown": 5}


async def scan(raw_url: str, deep: bool = False) -> dict:
    url = normalize_input_url(raw_url)
    typed_scheme = "://" in (raw_url or "")
    await asyncio.to_thread(check_url, url)
    notes: list[str] = []
    items: list[dict] = []
    page_info = {"url": url, "final_url": url, "title": "", "site": urlsplit(url).hostname}

    async with async_client() as client:
        yt_task = asyncio.create_task(asyncio.wait_for(asyncio.to_thread(ytdlp_probe, url), 45))
        try:
            page = await fetch_page(client, url)
        except BlockedURL:
            raise
        except httpx.HTTPError as e:
            page = {"error": f"Couldn't open the page ({type(e).__name__})."}
            if not typed_scheme and url.startswith("https://"):
                # No scheme was typed and https failed: the site may be http-only.
                url = "http://" + url[len("https://"):]
                page_info.update(url=url, final_url=url)
                yt_task.cancel()
                yt_task = asyncio.create_task(asyncio.wait_for(asyncio.to_thread(ytdlp_probe, url), 45))
                try:
                    page = await fetch_page(client, url)
                except httpx.HTTPError as e2:
                    page = {"error": f"Couldn't open the page ({type(e2).__name__})."}

        # The link itself is a media file.
        if page.get("direct"):
            yt_task.cancel()
            kind = kind_from_mime(page["mime"]) or EXT_KIND.get(ext_of(page["final"]), "unknown")
            it = {"id": _item_id(page["final"]), "url": page["final"], "kind": kind,
                  "mode": "ytdlp" if kind == "stream" else "direct", "ext": ext_of(page["final"]),
                  "mime": page["mime"], "size": page.get("size"), "source": "the link itself",
                  "name": page.get("name") or filename_from_url(page["final"], page["mime"]),
                  "title": "", "reachable": True}
            if kind == "stream":
                it["options"] = _options(True, [])
            page_info["final_url"] = page["final"]
            return {"page": page_info, "items": [it], "notes": notes}

        collector = None
        if page.get("html"):
            page_info["final_url"] = page["final"]
            collector, meta = parse_html(page["html"], page["final"])
            page_info["title"] = meta["title"]
        elif page.get("status"):
            notes.append(f"The site answered with HTTP {page['status']}"
                         + (" — it may block automated visitors. Try deep scan." if page["status"] in (401, 403, 429, 503) else "."))
        elif page.get("error"):
            notes.append(page["error"])

        # Deep scan: render with a real browser and merge what it sees.
        if deep:
            ok, why = await browser_available()
            if not ok:
                notes.append(why)
            else:
                try:
                    html, final, seen = await render_page(page_info["final_url"])
                    c2, meta2 = parse_html(html, final)
                    if collector is None:
                        collector = c2
                    else:
                        for u, it in c2.items.items():
                            if u not in collector.items:
                                collector.items[u] = it
                        collector.embeds.update(c2.embeds)
                    page_info["title"] = page_info["title"] or meta2["title"]
                    for u, ct, rt in seen:
                        k = kind_from_mime(ct) or ("video" if rt == "media" else "image" if rt == "image" else None)
                        collector.add(u, k, "loaded by page")
                except Exception as e:  # noqa: BLE001
                    notes.append(f"Deep scan failed: {str(e)[:160]}")

        # yt-dlp result
        yt_items: list[dict] = []
        try:
            info = await yt_task
            if info:
                yt_items = items_from_ytdlp(info, page_info["final_url"])
                page_info["title"] = page_info["title"] or info.get("title") or ""
        except asyncio.TimeoutError:
            notes.append("The video-site check took too long and was skipped.")
        except Exception:  # noqa: BLE001  - "Unsupported URL" etc. is normal for ordinary pages
            pass

        # Embedded players (YouTube/Vimeo iframes ...) -> resolve each with yt-dlp
        if collector and collector.embeds:
            known = {i["url"] for i in yt_items}
            embeds = [u for u in collector.embeds if u not in known][:8]

            async def resolve(u):
                try:
                    inf = await asyncio.wait_for(asyncio.to_thread(ytdlp_probe, u), 30)
                    return items_from_ytdlp(inf, u) if inf else []
                except Exception:  # noqa: BLE001
                    return []
            for res in await asyncio.gather(*(resolve(u) for u in embeds)):
                for it in res:
                    it["source"] = f"embedded {it.get('source') or 'player'}"
                    yt_items.append(it)

        direct = list(collector.items.values()) if collector else []
        direct_urls = {i["url"] for i in direct}
        # Drop generic-player hits that are just the same file we already found directly.
        yt_items = [i for i in yt_items if not (i.get("direct_url") and i["direct_url"] in direct_urls)]

        # Probe sizes / real types for direct files.
        sem = asyncio.Semaphore(14)
        to_probe = [i for i in direct if i["mode"] == "direct"][:MAX_PROBES]
        yt_direct = [i for i in yt_items if i["mode"] == "direct"]
        await asyncio.gather(*(probe(client, i, page_info["final_url"], sem) for i in to_probe + yt_direct))

    for i in direct:
        if i.get("drop") or i["kind"] == "unknown":
            continue
        if i["kind"] == "stream":
            i["options"] = _options(True, [])
        items.append(i)
    seen_ids = {i["id"] for i in items}
    for i in yt_items:
        if i["id"] not in seen_ids:
            items.append(i)
            seen_ids.add(i["id"])

    for i in items:
        i.setdefault("name", None)
        if not i.get("name") and i["mode"] == "direct":
            i["name"] = filename_from_url(i["url"], i.get("mime"), fallback=i["kind"])
        i["referer"] = page_info["final_url"]

    items.sort(key=lambda i: (0 if i["mode"] == "ytdlp" and i["source"] != "thumbnail" else 1,
                              KIND_ORDER.get(i["kind"], 9),
                              -(i.get("size") or 0) if i["kind"] in ("image",) else 0))
    if not items and not notes:
        notes.append("No media found on this page. If the site loads content with JavaScript, try deep scan.")
    return {"page": page_info, "items": items, "notes": notes}
