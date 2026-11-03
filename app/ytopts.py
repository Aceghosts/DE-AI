"""yt-dlp settings shared by scanning and downloading.

YouTube now requires a JavaScript runtime to unlock full-speed, full-quality
streams. Without one, yt-dlp falls back to throttled or limited formats, which
is the usual cause of very slow downloads. We use the Deno binary installed by
the `deno` pip package (see requirements.txt), or any deno/node/bun on PATH.
"""
from __future__ import annotations

import functools
import os
import shutil

from .net import USER_AGENT

CHUNK_MB = int(os.getenv("DEAI_CHUNK_MB", "10"))
FRAGMENTS = int(os.getenv("DEAI_FRAGMENTS", "8"))


@functools.lru_cache(maxsize=1)
def js_runtimes() -> dict:
    found: dict = {}
    try:
        import deno  # pip package that ships the deno binary
        found["deno"] = {"path": deno.find_deno_bin()}
    except Exception:  # noqa: BLE001
        if shutil.which("deno"):
            found["deno"] = {}
    for name in ("node", "bun"):
        if shutil.which(name):
            found[name] = {}
    return found


def js_runtime_name() -> str | None:
    rt = js_runtimes()
    return next(iter(rt), None)


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def base_opts() -> dict:
    opts = {
        "logger": _QuietLogger(),
        "quiet": True, "no_warnings": True, "noprogress": True,
        "http_headers": {"User-Agent": USER_AGENT},
        "socket_timeout": 20,
        "retries": 10, "fragment_retries": 10,
        # Download in 10 MB ranged requests: sidesteps YouTube's per-connection throttling.
        "http_chunk_size": CHUNK_MB * 1024 * 1024,
        # Fetch several pieces of HLS/DASH streams at once.
        "concurrent_fragment_downloads": FRAGMENTS,
        # Fetch yt-dlp's YouTube solver script if the bundled one is missing or outdated.
        "remote_components": ["ejs:github"],
    }
    rt = js_runtimes()
    if rt:
        opts["js_runtimes"] = rt
    return opts
