"""Entry point for Vercel: runs DE-AI in light mode.

Vercel finds the `app` object here through [tool.vercel] in pyproject.toml.
"""
import os

os.environ.setdefault("DEAI_LIGHT", "1")
os.environ.setdefault("DEAI_SCANS_PER_HOUR", "120")

from app.main import app  # noqa: E402,F401
