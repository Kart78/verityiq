"""Vercel entry point: serves the FastAPI app (UI + API) as one Python function.

Vercel's filesystem is read-only except /tmp, so the database, uploaded projects and the secrets fallback file live
there. /tmp is per-instance and temporary: projects and results are lost when the instance is recycled - use the
local install (README) for real work, or point BIQA_DB / BIQA_PROJECTS at persistent storage.

If the app cannot start, a minimal fallback answers every request with the start-up error as plain text, so the cause
is visible in the browser and in the Vercel logs instead of a bare FUNCTION_INVOCATION_FAILED.
"""
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("BIQA_DB", "/tmp/verityiq/biqa.db")
os.environ.setdefault("BIQA_PROJECTS", "/tmp/verityiq/projects")
os.environ.setdefault("BIQA_SECRETS_FILE", "/tmp/verityiq/secrets.json")
os.environ.setdefault("HOME", "/tmp")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

try:
    Path("/tmp/verityiq/projects").mkdir(parents=True, exist_ok=True)
    from app.main import app  # noqa: E402,F401
except Exception:  # pragma: no cover - only on a broken deployment
    _err = traceback.format_exc()
    print("VerityIQ failed to start:\n" + _err, file=sys.stderr, flush=True)
    try:
        _present = sorted(p.name for p in ROOT.iterdir())
    except OSError:
        _present = []
    _body = ("VerityIQ could not start on this deployment.\n\n" + _err +
             f"\nPython {sys.version.split()[0]}  root={ROOT}  contents={_present}\n").encode()

    async def app(scope, receive, send):  # minimal ASGI fallback
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 500,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": _body})
