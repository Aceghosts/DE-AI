"""DE-AI — paste a page link, pick the media you want, download it."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import guard, jobs, scanner
from .net import (LIGHT, BlockedURL, async_client, check_url, content_disposition,
                  filename_from_headers, filename_from_url, safe_filename)

app = FastAPI(title="DE-AI — Download Everything", docs_url="/api/docs", openapi_url="/api/openapi.json")
STATIC = Path(__file__).resolve().parent.parent / "public"


@app.exception_handler(BlockedURL)
async def _blocked(_: Request, exc: BlockedURL):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.middleware("http")
async def _password_gate(request: Request, call_next):
    if guard.PASSWORD and not guard.is_authed(request):
        path = request.url.path
        if path.startswith("/api/") and path not in ("/api/login", "/api/health"):
            return JSONResponse({"detail": "Enter the team password first."}, status_code=401)
        if path in ("/", "/index.html"):
            return HTMLResponse(guard.LOGIN_PAGE)
    return await call_next(request)


class LoginIn(BaseModel):
    password: str = Field(..., max_length=200)


@app.post("/api/login")
async def login(body: LoginIn, request: Request):
    guard.limit(request, "login", 10)
    if not guard.check_password(body.password):
        raise HTTPException(401, "That password is wrong.")
    resp = JSONResponse({"ok": True})
    name, value = guard.auth_cookie()
    resp.set_cookie(name, value, max_age=30 * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https")
    return resp


class ScanIn(BaseModel):
    url: str = Field(..., max_length=4000)
    deep: bool = False


@app.post("/api/scan")
async def scan(body: ScanIn, request: Request):
    guard.limit(request, "scan", guard.SCANS_PER_HOUR)
    return await scanner.scan(body.url, deep=body.deep)


@app.get("/api/health")
async def health():
    ok, why = await scanner.browser_available()
    import yt_dlp
    from .ytopts import js_runtime_name
    return {"mode": "light" if LIGHT else "full",
            "ffmpeg": shutil.which("ffmpeg") is not None, "deep_scan": ok, "deep_scan_note": why,
            "yt_dlp": yt_dlp.version.__version__, "js_runtime": js_runtime_name()}


@app.get("/api/file")
async def file(request: Request, url: str, referer: str | None = None, name: str | None = None,
               inline: bool = False):
    """Stream a file through the server (sets the right Referer and a download filename)."""
    guard.limit(request, "file", guard.FILES_PER_HOUR)
    check_url(url)
    client = async_client(timeout=60)
    headers = {"Referer": referer or url, "Accept-Encoding": "identity", "Accept": "*/*"}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    try:
        upstream = await client.send(client.build_request("GET", url, headers=headers), stream=True)
    except (httpx.HTTPError, BlockedURL) as e:
        await client.aclose()
        raise HTTPException(502, f"Couldn't reach the file: {type(e).__name__}") from e
    if upstream.status_code >= 400:
        code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, f"The site refused the download (HTTP {code}).")
    if guard.MAX_FILE_MB:
        total = upstream.headers.get("content-range", "").rsplit("/", 1)[-1]
        total = total if total.isdigit() else upstream.headers.get("content-length", "")
        if total.isdigit() and int(total) > guard.MAX_FILE_MB * 1024 * 1024:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(413, f"This file is bigger than {guard.MAX_FILE_MB} MB, the limit on this server.")

    out = {}
    for h in ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag"):
        if h in upstream.headers:
            out[h] = upstream.headers[h]
    fname = safe_filename(name) if name else (
        filename_from_headers(upstream.headers.get("content-disposition"))
        or filename_from_url(str(upstream.url), upstream.headers.get("content-type")))
    out["Content-Disposition"] = content_disposition("inline" if inline else "attachment", safe_filename(fname))
    # Third-party files are served from our origin: never let them run scripts here.
    out["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src data: 'self'; media-src 'self'; style-src 'unsafe-inline'"
    out["X-Content-Type-Options"] = "nosniff"
    out["Cache-Control"] = "private, max-age=3600"

    async def body():
        try:
            async for chunk in upstream.aiter_raw(256 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=out)


class ZipItem(BaseModel):
    url: str
    referer: str | None = None
    name: str | None = None


class JobIn(BaseModel):
    type: Literal["media", "zip"]
    url: str | None = None
    option: str = "best"
    title: str | None = None
    items: list[ZipItem] = []
    name: str | None = None


@app.post("/api/jobs")
async def create_job(body: JobIn, request: Request):
    if LIGHT:
        raise HTTPException(400, "Quality downloads, MP3 and ZIPs need the full version of DE-AI.")
    who = guard.client_ip(request)
    if guard.JOBS_AT_ONCE and jobs.running_for(who) >= guard.JOBS_AT_ONCE:
        raise HTTPException(429, "Wait for your current downloads to finish before starting another.")
    guard.limit(request, "download", guard.DOWNLOADS_PER_HOUR)
    if body.type == "media":
        if not body.url:
            raise HTTPException(400, "Missing link.")
        job = jobs.start_ytdlp(body.url, body.option, body.title or "", owner=who)
    else:
        job = jobs.start_zip([i.model_dump() for i in body.items], body.name or "media", owner=who)
    return job.public()


@app.get("/api/jobs/{jid}")
async def job_status(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "That download has expired. Start it again.")
    return job.public()


@app.get("/api/jobs/{jid}/file")
async def job_file(jid: str):
    job = jobs.get(jid)
    if not job or job.status != "done" or not job.path or not os.path.exists(job.path):
        raise HTTPException(404, "That file isn't ready or has expired.")
    return FileResponse(job.path, filename=job.filename, media_type="application/octet-stream")


if STATIC.is_dir():  # on Vercel the public/ folder is served by its CDN instead
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
