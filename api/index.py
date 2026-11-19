"""Vercel entry point: serves the FastAPI app (UI + API) as one Python function.

Vercel's filesystem is read-only except /tmp, so the database, uploaded projects and the secrets fallback file live
there. /tmp is per-instance and temporary: projects and results are lost when the instance is recycled - use the
local install (README) for real work, or point BIQA_DB / BIQA_PROJECTS at persistent storage.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("BIQA_DB", "/tmp/verityiq/biqa.db")
os.environ.setdefault("BIQA_PROJECTS", "/tmp/verityiq/projects")
os.environ.setdefault("BIQA_SECRETS_FILE", "/tmp/verityiq/secrets.json")
Path("/tmp/verityiq/projects").mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.main import app  # noqa: E402,F401
