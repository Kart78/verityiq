"""Vercel entry point (project root, so the whole project is bundled): serves the FastAPI app (UI + API) as one Python function.

Vercel's filesystem is read-only except /tmp, so the database, uploaded projects and the secrets fallback file live
there. /tmp is per-instance and temporary: projects and results are lost when the instance is recycled - use the
local install (README) for real work, or point BIQA_DB / BIQA_PROJECTS at persistent storage.

Vercel detects the entry point statically, so `app = FastAPI()` must stay a plain top-level statement. If VerityIQ
cannot start, that placeholder app answers every request with the start-up error as plain text (also printed to the
Vercel logs) instead of a bare FUNCTION_INVOCATION_FAILED.
"""
import os
import sys
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

os.environ.setdefault("BIQA_DB", "/tmp/verityiq/biqa.db")
os.environ.setdefault("BIQA_PROJECTS", "/tmp/verityiq/projects")
os.environ.setdefault("BIQA_SECRETS_FILE", "/tmp/verityiq/secrets.json")
os.environ.setdefault("HOME", "/tmp")

# This file sits at the project root (Vercel bundles the entry file's folder), next to backend/app.
_here = Path(__file__).resolve().parent
ROOT = next((d for d in (_here, _here.parent, Path.cwd()) if (d / "backend" / "app" / "main.py").exists()), _here)
sys.path.insert(0, str(ROOT / "backend"))

app = FastAPI()

try:
    Path("/tmp/verityiq/projects").mkdir(parents=True, exist_ok=True)
    from app.main import app as verityiq_app
except Exception:  # pragma: no cover - only on a broken deployment
    _err = traceback.format_exc()
    print("VerityIQ failed to start:\n" + _err, file=sys.stderr, flush=True)
    try:
        _present = sorted(str(p.relative_to(ROOT)) for p in ROOT.glob("*/*"))[:80]
    except OSError:
        _present = []
    _body = ("VerityIQ could not start on this deployment.\n\n" + _err +
             f"\nPython {sys.version.split()[0]}  root={ROOT}  contents={_present}\n")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def startup_error(path: str = ""):
        return PlainTextResponse(_body, status_code=500)
else:
    app = verityiq_app
