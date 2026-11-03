#!/usr/bin/env bash
# Start DE-AI on macOS / Linux. First run sets everything up.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -U pip >/dev/null
  ./.venv/bin/pip install -r requirements-full.txt
  ./.venv/bin/python -m playwright install chromium || echo "(Deep scan browser not installed - everything else still works.)"
fi
./.venv/bin/pip install -q -r requirements-full.txt >/dev/null 2>&1 || true
./.venv/bin/pip install -q -U "yt-dlp[default]" >/dev/null 2>&1 || true   # sites change often; keep yt-dlp fresh
command -v ffmpeg >/dev/null || echo "Tip: install ffmpeg for HD video and MP3 (brew install ffmpeg / sudo apt install ffmpeg)."
echo "DE-AI is running at http://localhost:${PORT:-8000}"
exec ./.venv/bin/python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
