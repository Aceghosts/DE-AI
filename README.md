# DE-AI — Download Everything

Paste a link to any web page. DE-AI finds every image, video, audio file and document on it, shows them as a contact sheet, and lets you pick what to download — one at a time, or several at once as a ZIP. On video sites (YouTube, Instagram, TikTok, X, Facebook, Vimeo, SoundCloud, Reddit and 1,800+ others) it also lets you choose the quality, or grab just the audio as MP3.

## Two versions, one codebase

| | **Light** (Vercel) | **Full** (your PC, or any server) |
|---|---|---|
| Finds every image, video, audio file and document on a page | ✓ | ✓ |
| Downloads | Straight from the original site to your browser or IDM | Through DE-AI (works around hotlink protection) |
| Many files at once | Save the links, then import into IDM | One ZIP |
| Video sites: pick quality, MP3 | Only when the site offers a single direct file | ✓ (yt-dlp + ffmpeg) |
| "Open the page in a browser first" | — | ✓ |
| Hosting | Free on Vercel, no server to look after | Needs a machine that stays on |

The light version is what runs on Vercel: Vercel functions can't send responses over 4.5 MB or run background jobs, so DE-AI only finds the media there and lets your browser or download manager fetch it directly.

## Deploy the light version to Vercel

1. Push this repository to GitHub.
2. On vercel.com choose **Add New → Project**, import the repository, keep the defaults (no framework, no build command) and click **Deploy**.
3. Every push to the main branch redeploys automatically.

How Vercel runs it: `pyproject.toml` points Vercel at `vercel_app.py`, which starts the app in light mode (`DEAI_LIGHT=1`). The page is served from `public/` by Vercel's CDN, and Vercel installs the small `requirements.txt` list.

Optional settings in Vercel → Project → Settings → Environment Variables:

| Variable | Meaning |
|---|---|
| `DEAI_PASSWORD` | Ask for a team password before anyone can use it. |
| `DEAI_SECRET` | Any long random text; keeps people signed in across deploys when a password is set. |
| `DEAI_SCANS_PER_HOUR` | Page scans allowed per visitor per hour (default on Vercel: 120; `0` = no limit). |

**Downloading many files with IDM:** mark the files, click **Save links for IDM**, then in IDM choose **Tasks → Import → From text file** and pick the saved file.

## Run the full version

You need **Python 3.10+**. **ffmpeg** is strongly recommended (needed for HD video and MP3).

**Windows** — double-click `run.bat`. It sets itself up on the first run and opens http://localhost:8000.
Install ffmpeg once with: `winget install Gyan.FFmpeg`

**macOS / Linux**

```bash
./run.sh            # then open http://localhost:8000
```

Install ffmpeg with `brew install ffmpeg` or `sudo apt install ffmpeg`.

**Docker**

```bash
docker build -t de-ai . && docker run -p 8000:8000 de-ai
```

## How it works

| Step | What happens |
|---|---|
| You paste a link | The server opens the page (the browser can't, because of cross-site restrictions). |
| Find media | It reads `<img>`, `srcset`, `<picture>`, `<video>`, `<audio>`, `<source>`, links to files, Open Graph/Twitter tags, CSS backgrounds, JSON-LD data, and media URLs hidden in scripts. At the same time **yt-dlp** checks whether the page is a video/audio site or contains an HLS/DASH stream or embedded player (YouTube/Vimeo iframes, etc.). Each file is checked for its real type and size. |
| "Open the page in a browser first" | A headless Chromium loads the page, scrolls it to trigger lazy loading, and records every image/video/audio request it makes. Use this for sites that build their content with JavaScript. |
| You pick | Single files download straight through the server (with the right Referer, so hotlink-protected files work). Several files become one ZIP. Player media is downloaded by yt-dlp at the quality you choose, and video + audio are joined by ffmpeg into an MP4. |

## Project layout

```
app/main.py      API routes + serves the UI
app/scanner.py   finds media on a page (HTML, yt-dlp, headless browser)
app/jobs.py      background downloads: quality picks via yt-dlp, ZIP bundles
app/net.py       HTTP clients, private-network blocking, filenames
app/guard.py     optional password and per-visitor limits
app/ytopts.py    shared yt-dlp settings (speed, YouTube JavaScript runtime)
public/          the web UI (plain HTML/CSS/JS, no build step)
vercel_app.py    Vercel entry point (light version)
```

API docs are at http://localhost:8000/api/docs.

| Endpoint | Purpose |
|---|---|
| `POST /api/scan` `{url, deep}` | List media on a page |
| `GET /api/file?url=&referer=&name=&inline=` | Stream one file (supports Range, so previews can seek) |
| `POST /api/jobs` `{type:"media", url, option}` | Download from a player; `option` is `best`, `h1080`, `h720`, …, `audio`, or `mp3` |
| `POST /api/jobs` `{type:"zip", items:[{url, referer, name}]}` | Bundle several files |
| `GET /api/jobs/{id}` · `GET /api/jobs/{id}/file` | Progress · the finished file |

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Where the server listens. Use `HOST=0.0.0.0` to open it to your network. |
| `DEAI_ALLOW_PRIVATE` | `0` | `1` lets you grab from localhost / LAN addresses (e.g. your NAS). Keep `0` if others can reach the server. |
| `DEAI_JOB_TTL` | `7200` | Seconds finished downloads are kept on the server. |
| `DEAI_MAX_ZIP_MB` / `DEAI_MAX_ZIP_ITEMS` | `4096` / `300` | ZIP limits. |
| `DEAI_WORKERS` | `3` | Downloads that run at once. |
| `DEAI_WORK_DIR` | system temp | Where downloads are assembled. |
| `DEAI_PASSWORD` | none | Ask for a password before anyone can use it. |
| `DEAI_SCANS_PER_HOUR` / `DEAI_DOWNLOADS_PER_HOUR` / `DEAI_FILES_PER_HOUR` | `0` (off) | Per-visitor limits for public servers. |
| `DEAI_JOBS_PER_VISITOR` | `0` (off) | Downloads one visitor may run at the same time. |
| `DEAI_MAX_FILE_MB` | `0` (off) | Largest single file the server will fetch. |
| `DEAI_LIGHT` | `0` | `1` runs the light version (no server-side downloads). |

## When something doesn't work

- **Slow YouTube downloads** — YouTube needs a JavaScript runtime for full-speed streams. DE-AI installs one (the `deno` package) automatically; `GET /api/health` should show `"js_runtime": "deno"`. Downloads use 10 MB ranged requests (`DEAI_CHUNK_MB`) and 8 parallel stream pieces (`DEAI_FRAGMENTS`); ZIPs fetch 6 files at once (`DEAI_ZIP_PARALLEL`).
- **A video site stops working** — sites change; update yt-dlp: `pip install -U "yt-dlp[default]"` (the run scripts do this on every start).
- **"Requires you to be logged in"** — private, age-gated or members-only media needs your login. yt-dlp can use your browser's cookies; add `"cookiesfrombrowser": ("chrome",)` to the `opts` dict in `_run_ytdlp` in `app/jobs.py` (and in `ytdlp_probe` in `app/scanner.py`).
- **Nothing found** — try "Open the page in a browser first". Some sites block automated visitors entirely.
- **Streams/DRM** — DRM-protected streams (Netflix, Spotify, most paid streaming) can't be downloaded.

## Please use responsibly

Download only what you have the right to save — your own content, openly licensed media, or where the site's terms allow it. If you host DE-AI for other people, keep `DEAI_ALLOW_PRIVATE=0`, and consider setting `DEAI_PASSWORD`.
