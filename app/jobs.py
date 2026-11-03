"""Background jobs: yt-dlp downloads (with quality choice) and ZIP bundles."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .net import (USER_AGENT, BlockedURL, check_url, filename_from_headers,
                  filename_from_url, safe_filename, sync_client)

JOB_TTL = int(os.getenv("DEAI_JOB_TTL", "7200"))           # seconds a finished file is kept
MAX_ZIP_ITEMS = int(os.getenv("DEAI_MAX_ZIP_ITEMS", "300"))
MAX_ZIP_BYTES = int(os.getenv("DEAI_MAX_ZIP_MB", "4096")) * 1024 * 1024
WORK_DIR = os.getenv("DEAI_WORK_DIR") or os.path.join(tempfile.gettempdir(), "de-ai-jobs")
os.makedirs(WORK_DIR, exist_ok=True)

EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("DEAI_WORKERS", "3")))


@dataclass
class Job:
    id: str
    type: str
    title: str = ""
    status: str = "queued"          # queued | running | done | error
    progress: float = 0.0           # 0..1
    message: str = "Waiting to start"
    filename: str | None = None
    path: str | None = None
    size: int | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    owner: str = ""
    tmpdir: str = ""

    def public(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "type", "title", "status", "progress", "message", "filename", "size", "error")}


JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def _cleanup() -> None:
    now = time.time()
    with _LOCK:
        for jid, job in list(JOBS.items()):
            if job.status in ("done", "error") and now - job.created > JOB_TTL:
                shutil.rmtree(job.tmpdir, ignore_errors=True)
                JOBS.pop(jid, None)


def running_for(owner: str) -> int:
    return sum(1 for j in list(JOBS.values()) if j.owner == owner and j.status in ("queued", "running"))


def _new(type_: str, title: str, owner: str = "") -> Job:
    _cleanup()
    jid = uuid.uuid4().hex[:16]
    job = Job(id=jid, type=type_, title=title, owner=owner, tmpdir=tempfile.mkdtemp(prefix=f"{jid}-", dir=WORK_DIR))
    with _LOCK:
        JOBS[jid] = job
    return job


def get(jid: str) -> Job | None:
    return JOBS.get(jid)


# ---------------- yt-dlp ----------------

def _format_for(option: str, ffmpeg: bool) -> tuple[str, dict]:
    extra: dict = {}
    if option == "mp3" and ffmpeg:
        extra["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
        return "ba/b", extra
    if option == "audio":
        return "ba[ext=m4a]/ba/b", extra
    if not ffmpeg:
        # Without ffmpeg we can't merge separate video+audio streams; pick the best single file.
        return "b/bv*+ba", extra
    extra["merge_output_format"] = "mp4"
    extra["format_sort"] = ["res", "ext:mp4:m4a"]
    if option.startswith("h") and option[1:].isdigit():
        h = int(option[1:])
        return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b", extra
    return "bv*+ba/b", extra


def start_ytdlp(url: str, option: str, title: str = "", owner: str = "") -> Job:
    check_url(url)
    job = _new("media", title or url, owner)
    EXECUTOR.submit(_run_ytdlp, job, url, option)
    return job


def _run_ytdlp(job: Job, url: str, option: str) -> None:
    from yt_dlp import YoutubeDL
    ffmpeg = shutil.which("ffmpeg") is not None
    fmt, extra = _format_for(option, ffmpeg)
    job.status, job.message = "running", "Starting download"
    state = {"streams": 0}

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else 0
            # Two streams (video then audio) share the bar: 0-90% video, 90-98% audio.
            if state["streams"] == 0:
                job.progress = min(0.9 * frac, 0.9) if "+" in fmt else min(0.97 * frac, 0.97)
            else:
                job.progress = 0.9 + 0.08 * frac
            speed = d.get("speed")
            sp = f" at {_human(speed)}/s" if speed else ""
            job.message = f"Downloading {_human(done)}" + (f" of {_human(total)}" if total else "") + sp
        elif d.get("status") == "finished":
            state["streams"] += 1

    def pp_hook(d: dict) -> None:
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            job.message = "Converting to MP3" if "ExtractAudio" in name else "Joining video and audio"
            job.progress = max(job.progress, 0.96)

    from .ytopts import base_opts
    opts = {
        **base_opts(),
        "format": fmt,
        "outtmpl": os.path.join(job.tmpdir, "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": True, "windowsfilenames": True,
        "progress_hooks": [hook], "postprocessor_hooks": [pp_hook],
        **extra,
    }
    max_mb = int(os.getenv("DEAI_MAX_FILE_MB", "0"))
    if max_mb:
        opts["max_filesize"] = max_mb * 1024 * 1024
    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
        files = [os.path.join(job.tmpdir, f) for f in os.listdir(job.tmpdir)
                 if not f.endswith((".part", ".ytdl", ".temp")) and not f.startswith(".")]
        if not files:
            raise RuntimeError("The download finished but no file was produced.")
        path = max(files, key=os.path.getsize)
        job.path, job.filename, job.size = path, os.path.basename(path), os.path.getsize(path)
        job.progress, job.status, job.message = 1.0, "done", "Ready"
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("ERROR: ", "").strip()
        job.status, job.error, job.message = "error", _friendly(msg), "Failed"


class _Logger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _friendly(msg: str) -> str:
    low = msg.lower()
    if "sign in" in low or "login" in low or "cookies" in low:
        return "This site requires you to be logged in to download this. (See README: cookies.)"
    if "private" in low:
        return "This media is private."
    if "unsupported url" in low:
        return "No downloadable media was found at that link."
    if "max-filesize" in low or "larger than max" in low or ("file is larger" in low):
        return "This is bigger than the file size limit on this server."
    if "ffmpeg" in low:
        return "ffmpeg is needed for this option. Install ffmpeg and try again."
    if "geo" in low and "restrict" in low:
        return "This media isn't available in your country."
    return msg[:300] or "Download failed."


# ---------------- ZIP ----------------

def start_zip(items: list[dict], name: str, owner: str = "") -> Job:
    if not items:
        raise BlockedURL("Choose at least one item.")
    if len(items) > MAX_ZIP_ITEMS:
        raise BlockedURL(f"Choose at most {MAX_ZIP_ITEMS} items per ZIP.")
    job = _new("zip", name, owner)
    EXECUTOR.submit(_run_zip, job, items, name)
    return job


ZIP_PARALLEL = int(os.getenv("DEAI_ZIP_PARALLEL", "6"))


def _fetch_to_file(client, it: dict, n: int, folder: str, counter: dict) -> tuple[str, str]:
    """Download one item to a temp file. Returns (temp path, filename for the zip)."""
    url = it.get("url", "")
    check_url(url)
    headers = {"Referer": it.get("referer") or url, "Accept-Encoding": "identity"}
    path = os.path.join(folder, f"part-{n}")
    with client.stream("GET", url, headers=headers) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        fname = it.get("name") or filename_from_headers(r.headers.get("content-disposition")) \
            or filename_from_url(url, r.headers.get("content-type"))
        with open(path, "wb") as out:
            for chunk in r.iter_raw(1024 * 1024):
                with _LOCK:
                    counter["bytes"] += len(chunk)
                    if counter["bytes"] > MAX_ZIP_BYTES:
                        raise OverflowError
                out.write(chunk)
    return path, safe_filename(fname, f"item-{n}")


def _run_zip(job: Job, items: list[dict], name: str) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    job.status, job.message = "running", f"Downloading {len(items)} files"
    zip_name = safe_filename(name or "media", "media")
    if not zip_name.lower().endswith(".zip"):
        zip_name += ".zip"
    path = os.path.join(job.tmpdir, zip_name)
    parts = os.path.join(job.tmpdir, "parts")
    os.makedirs(parts, exist_ok=True)
    counter = {"bytes": 0}
    results: dict[int, tuple[str, str]] = {}
    failed: list[str] = []
    try:
        with sync_client() as client, ThreadPoolExecutor(max_workers=ZIP_PARALLEL) as pool:
            futs = {pool.submit(_fetch_to_file, client, it, n, parts, counter): (n, it)
                    for n, it in enumerate(items, 1)}
            done = 0
            for fut in as_completed(futs):
                n, it = futs[fut]
                done += 1
                try:
                    results[n] = fut.result()
                except OverflowError:
                    raise
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{it.get('url', '')}  ({e})")
                job.progress = 0.95 * done / len(items)
                job.message = f"Downloaded {done} of {len(items)} ({_human(counter['bytes'])})"
        job.message = "Packing ZIP"
        used: set[str] = set()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for n in sorted(results):
                tmp, fname = results[n]
                zf.write(tmp, _unique(fname, used))
                os.remove(tmp)
            if failed:
                zf.writestr("_could_not_download.txt", "These items failed:\n\n" + "\n".join(failed))
        shutil.rmtree(parts, ignore_errors=True)
        job.path, job.filename, job.size = path, zip_name, os.path.getsize(path)
        job.status, job.progress = "done", 1.0
        job.message = "Ready" if not failed else f"Ready ({len(failed)} couldn't be downloaded)"
    except OverflowError:
        job.status, job.message = "error", "Failed"
        job.error = f"The ZIP would be bigger than {MAX_ZIP_BYTES // (1024 * 1024)} MB. Choose fewer items."
    except Exception as e:  # noqa: BLE001
        job.status, job.message, job.error = "error", "Failed", str(e)[:300]


def _unique(name: str, used: set[str]) -> str:
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""
    cand, i = name, 2
    while cand.lower() in used:
        cand = f"{base} ({i}).{ext}" if ext else f"{base} ({i})"
        i += 1
    used.add(cand.lower())
    return cand


def _human(n: float | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def site_name(url: str) -> str:
    return (urlsplit(url).hostname or "media").removeprefix("www.")
