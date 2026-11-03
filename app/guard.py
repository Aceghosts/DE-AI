"""Safeguards for running DE-AI publicly: per-visitor rate limits and an optional password.

All limits are set with environment variables; 0 turns a limit off.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

SCANS_PER_HOUR = int(os.getenv("DEAI_SCANS_PER_HOUR", "0"))
DOWNLOADS_PER_HOUR = int(os.getenv("DEAI_DOWNLOADS_PER_HOUR", "0"))   # quality downloads + ZIPs
FILES_PER_HOUR = int(os.getenv("DEAI_FILES_PER_HOUR", "0"))           # single files and previews
JOBS_AT_ONCE = int(os.getenv("DEAI_JOBS_PER_VISITOR", "0"))           # running downloads per visitor
MAX_FILE_MB = int(os.getenv("DEAI_MAX_FILE_MB", "0"))

PASSWORD = os.getenv("DEAI_PASSWORD", "")
COOKIE = "deai_auth"
_SECRET = os.getenv("DEAI_SECRET") or secrets.token_hex(16)

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit(request: Request, bucket: str, per_hour: int) -> None:
    if per_hour <= 0:
        return
    now = time.time()
    key = (client_ip(request), bucket)
    with _lock:
        q = _hits[key]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= per_hour:
            wait = int(3600 - (now - q[0])) // 60 + 1
            raise HTTPException(429, f"You've hit this server's limit for now. Try again in about {wait} minute{'s' if wait != 1 else ''}.")
        q.append(now)
        if len(_hits) > 50_000:  # keep memory bounded
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600][:25_000]:
                _hits.pop(k, None)


# ---------- password ----------

def _token() -> str:
    return hmac.new(_SECRET.encode(), b"deai:" + PASSWORD.encode(), hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    if not PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())


def check_password(given: str) -> bool:
    return bool(PASSWORD) and hmac.compare_digest(given.encode(), PASSWORD.encode())


def auth_cookie() -> tuple[str, str]:
    return COOKIE, _token()


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>DE-AI — Download Everything</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,800&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css"></head><body>
<header class="bar"><div class="wrap bar-inner">
<span class="brand"><span class="wordmark">DE-AI</span><span class="brand-sub">Download everything</span></span>
<form class="scan" id="f" style="max-width:560px"><label for="p" class="sr-only">Password</label>
<div class="scan-field"><input id="p" type="password" autocomplete="current-password" placeholder="Team password" style="padding-left:18px" autofocus></div>
<button class="btn primary" type="submit">Enter</button>
<p id="e" class="deep" style="color:var(--danger)" hidden></p></form></div></header>
<script>
document.getElementById('f').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: document.getElementById('p').value})});
  if (r.ok) location.reload();
  else { const e = document.getElementById('e'); e.textContent = r.status === 429 ? 'Too many tries. Wait a few minutes.' : 'That password is wrong.'; e.hidden = false; }
});
</script></body></html>"""
