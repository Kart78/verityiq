"""Exports a read-only, single-file HTML snapshot of the UI with the latest DEMO run embedded.

    BIQA_DB=data/biqa.db python scripts/build_snapshot.py demo-pbi-dbx out/snapshot.html
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main(env_id: str, out: str) -> None:
    c = TestClient(app)
    if env_id == "auto":
        env_id = next(e["id"] for e in c.get("/api/environments").json() if e.get("sample"))
    get = lambda p: c.get(p).json() if "sql-scripts" not in p else c.get(p).text  # noqa: E731
    E = f"/api/environments/{env_id}"
    snap = {p: get(p) for p in ["/api/session", "/api/platforms", "/api/environments", "/api/audit"] +
            [E + s for s in ["/overview", "/tests", "/runs", "/metadata", "/kpis", "/dax-analysis", "/sql-scripts", "/defects", "/results/latest"]]
            + [f"/api/projects/{env_id}"]}
    snap["/api/environments"] = [e for e in snap["/api/environments"] if e["id"] == env_id]
    snap["/api/session"] = {**snap["/api/session"], "user": "snapshot-viewer", "role": "viewer", "auth_mode": "static snapshot"}
    for r in snap[E + "/runs"]:
        snap[f"/api/runs/{r['run_id']}/results"] = get(f"/api/runs/{r['run_id']}/results")
    snap["__env__"] = env_id
    snap["__captured__"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fe = ROOT / "frontend"
    html = (fe / "index.html").read_text()
    css, js = (fe / "styles.css").read_text(), (fe / "app.js").read_text()
    data = json.dumps(snap, default=str).replace("</", "<\\/")
    html = html.replace('<link rel="stylesheet" href="/static/styles.css">', f"<style>{css}</style>")
    html = html.replace('<script src="/static/app.js"></script>', f"<script>window.__SNAPSHOT__={data};</script>\n<script>{js}</script>")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(html)
    print(f"Wrote {out} ({len(html) // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "auto", sys.argv[2] if len(sys.argv) > 2 else "out/snapshot.html")
