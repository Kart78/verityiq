"""HTTP API + static frontend.  Run:  uvicorn app.main:app --app-dir backend --port 8000"""
from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai import llm
from .connectors.registry import PLATFORMS
from .core.environment import ROOT
from .core.models import KPIDefinition
from .service import Service

app = FastAPI(title="VerityIQ - Universal BI & Data Quality Assurance", version="0.1.0-phase1")
svc = Service()

# ------------------------------------------------------------------ auth / RBAC
ROLE_RANK = {"viewer": 0, "tester": 1, "approver": 2, "admin": 3}


class User(BaseModel):
    name: str
    role: str


def current_user(x_api_key: Optional[str] = Header(default=None)) -> User:
    users = os.environ.get("BIQA_USERS_JSON")
    if not users:
        return User(name="local-dev", role="admin")   # dev mode - surfaced in /api/session
    table = json.loads(users)
    if not x_api_key or x_api_key not in table:
        raise HTTPException(401, "Missing or invalid API key")
    return User(**table[x_api_key])


def need(role: str):
    def dep(u: User = Depends(current_user)) -> User:
        if ROLE_RANK[u.role] < ROLE_RANK[role]:
            raise HTTPException(403, f"Requires role '{role}'")
        return u
    return dep


def _env(env_id: str):
    try:
        return svc.env(env_id)
    except KeyError:
        raise HTTPException(404, f"Unknown environment {env_id}")


# ------------------------------------------------------------------ routes
@app.get("/api/session")
def session(u: User = Depends(current_user)):
    return {"user": u.name, "role": u.role, "app_name": os.environ.get("BIQA_APP_NAME", "VerityIQ"),
            "app_tagline": os.environ.get("BIQA_APP_TAGLINE", "Universal BI & Data Quality Assurance"), "auth_mode": "api-key" if os.environ.get("BIQA_USERS_JSON") else "dev (auth disabled)",
            "ai": llm.status()}


@app.get("/api/platforms")
def platforms():
    from .connectors.sqldb import COMMON_FIELDS, SPECS
    return [{"key": p.key, "name": p.name, "kind": p.kind, "support": p.support.value, "notes": p.notes,
             "expression_language": p.expression_language, "sql_dialect": p.sql_dialect,
             "fields": [f.public() for f in SPECS[p.key].fields + COMMON_FIELDS] if p.key in SPECS else None} for p in PLATFORMS]


async def _upload_payload(files, bundle) -> list[tuple[str, bytes]]:
    """Files sent one by one (multipart) or packed into one gzip bundle by the browser (see core/bundle.py)."""
    from .core.bundle import unpack
    if bundle is not None:
        return unpack(await bundle.read())
    if not files:
        raise ValueError("No files were uploaded")
    return [(f.filename or "upload", await f.read()) for f in files]


@app.post("/api/projects")
async def create_project(files: Optional[list[UploadFile]] = File(default=None), bundle: Optional[UploadFile] = File(default=None),
                         name: Optional[str] = Form(default=None), u: User = Depends(need("tester"))):
    try:
        payload = await _upload_payload(files, bundle)
        return svc.create_project_from_files(payload, u.name, name=name or None)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/sample")
def sample_project(u: User = Depends(need("tester"))):
    return svc.create_sample_project(u.name)


@app.get("/api/projects/{pid}")
def project(pid: str):
    _env(pid)
    return svc.project_summary(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, u: User = Depends(need("admin"))):
    _env(pid)
    try:
        svc.delete_project(pid, u.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"deleted": pid}


@app.post("/api/environments/{env_id}/definitions")
async def upload_definitions(env_id: str, file: UploadFile = File(...), u: User = Depends(need("approver"))):
    _env(env_id)
    try:
        return svc.import_definitions(env_id, file.filename or "definitions.csv", await file.read(), u.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/environments/{env_id}/definitions/template")
def definitions_template(env_id: str, format: str = "xlsx"):
    env = _env(env_id)
    data = svc.definition_template(env_id, format)
    ext = "xlsx" if format == "xlsx" else "csv"
    mt = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if ext == "xlsx" else "text/csv"
    return Response(data, media_type=mt, headers={"Content-Disposition": f'attachment; filename="kpi_definitions_{env.id}.{ext}"'})


@app.patch("/api/environments/{env_id}/settings")
def update_settings(env_id: str, patch: dict, u: User = Depends(need("admin"))):
    _env(env_id)
    try:
        return svc.update_settings(env_id, patch, u.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/environments/{env_id}/tests/{test_id}/manual-result")
def manual_result(env_id: str, test_id: str, body: dict, u: User = Depends(need("tester"))):
    _env(env_id)
    try:
        return svc.record_manual(env_id, test_id, body, u.name)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/environments/{env_id}/scope")
def get_scope(env_id: str):
    _env(env_id)
    return svc.scope_view(env_id)


@app.post("/api/environments/{env_id}/scope")
def set_scope(env_id: str, body: dict, u: User = Depends(need("tester"))):
    _env(env_id)
    return svc.set_scope(env_id, body, u.name)


@app.post("/api/environments/{env_id}/scope/files")
async def scope_files(env_id: str, files: list[UploadFile] = File(...), u: User = Depends(need("tester"))):
    _env(env_id)
    try:
        return svc.scope_from_files(env_id, [(f.filename or "file", await f.read()) for f in files], u.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{pid}/reanalyze")
def reanalyze_project(pid: str, u: User = Depends(need("tester"))):
    _env(pid)
    try:
        return svc.reanalyze(pid, u.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/environments/{env_id}/usage")
def model_usage(env_id: str):
    _env(env_id)
    return svc.usage(env_id)


@app.post("/api/environments/{env_id}/usage/keep")
def usage_keep(env_id: str, body: dict, u: User = Depends(need("tester"))):
    _env(env_id)
    return svc.set_keep(env_id, list(body.get("keys") or []), bool(body.get("keep", True)), u.name)


@app.post("/api/environments/{env_id}/usage/reports")
async def usage_reports(env_id: str, files: Optional[list[UploadFile]] = File(default=None),
                        bundle: Optional[UploadFile] = File(default=None), u: User = Depends(need("tester"))):
    _env(env_id)
    try:
        return svc.add_usage_reports(env_id, await _upload_payload(files, bundle), u.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/environments/{env_id}/usage/reports/{name}")
def usage_report_delete(env_id: str, name: str, u: User = Depends(need("tester"))):
    _env(env_id)
    return svc.remove_usage_report(env_id, name, u.name)


@app.post("/api/environments/{env_id}/usage/vpax")
async def usage_vpax(env_id: str, file: UploadFile = File(...), u: User = Depends(need("tester"))):
    _env(env_id)
    try:
        return svc.set_vpax(env_id, await file.read(), file.filename or "model.vpax", u.name)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, f"Could not read the VertiPaq file: {e}")


@app.delete("/api/environments/{env_id}/usage/vpax")
def usage_vpax_delete(env_id: str, u: User = Depends(need("tester"))):
    _env(env_id)
    return svc.set_vpax(env_id, None, "", u.name)


@app.get("/api/environments/{env_id}/best-practices")
def best_practices(env_id: str):
    _env(env_id)
    return svc.best_practices(env_id)


def _clean_opts(measures: bool, calc_columns: bool, data_columns: bool) -> dict:
    return {"measures": measures, "calc_columns": calc_columns, "data_columns": data_columns}


@app.get("/api/environments/{env_id}/usage/cleanup")
def usage_cleanup_preview(env_id: str, measures: bool = True, calc_columns: bool = True, data_columns: bool = True,
                          u: User = Depends(need("viewer"))):
    _env(env_id)
    try:
        return svc.cleanup(env_id, _clean_opts(measures, calc_columns, data_columns), u.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/environments/{env_id}/usage/cleanup.zip")
def usage_cleanup_zip(env_id: str, measures: bool = True, calc_columns: bool = True, data_columns: bool = True,
                      u: User = Depends(need("tester"))):
    _env(env_id)
    try:
        r = svc.cleanup(env_id, _clean_opts(measures, calc_columns, data_columns), u.name, want_zip=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not r["ok"] or not r.get("zip"):
        raise HTTPException(409, "Cleanup rejected by the safety check: " + "; ".join(r["problems"][:3]))
    return Response(r["zip"], media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{env_id}_cleaned_pbip.zip"'})


@app.get("/api/environments/{env_id}/usage.csv")
def model_usage_csv(env_id: str):
    _env(env_id)
    return Response(svc.usage_csv(env_id), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="model_usage_{env_id}.csv"'})


@app.get("/api/environments/{env_id}/source-files")
def source_files(env_id: str):
    _env(env_id)
    return svc.source_files(env_id)


@app.post("/api/environments/{env_id}/source-files")
async def upload_source_files(env_id: str, files: list[UploadFile] = File(...), u: User = Depends(need("tester"))):
    _env(env_id)
    try:
        return svc.upload_source_files(env_id, [(f.filename or "file", await f.read()) for f in files], u.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/environments/{env_id}/results/latest")
def latest(env_id: str):
    _env(env_id)
    return svc.latest_results(env_id)


@app.get("/api/environments")
def environments():
    return [{**e.public_dict(), "connections": svc.store.connections(e.id)} for e in svc.envs.values()]


@app.post("/api/environments/{env_id}/test-connection")
def test_connection(env_id: str, u: User = Depends(need("tester"))):
    _env(env_id)
    return svc.test_connections(env_id, u.name)


@app.post("/api/environments/{env_id}/discover")
def discover(env_id: str, u: User = Depends(need("tester"))):
    _env(env_id)
    try:
        return svc.discover(env_id, u.name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Discovery failed: {e}")


@app.get("/api/environments/{env_id}/metadata")
def metadata(env_id: str):
    _env(env_id)
    return {"bi_model": svc.store.snapshot(env_id, "bi_model"), "source_tables": svc.store.snapshot(env_id, "source_tables")}


@app.get("/api/environments/{env_id}/kpis")
def kpis(env_id: str):
    _env(env_id)
    return [k.model_dump(mode="json") for k in svc.kpis(env_id)]


@app.post("/api/environments/{env_id}/kpis")
def save_kpi(env_id: str, kpi: KPIDefinition, u: User = Depends(need("tester"))):
    _env(env_id)
    return svc.save_kpi(env_id, kpi, u.name).model_dump(mode="json")


class Decision(BaseModel):
    decision: str   # APPROVED | REJECTED


@app.post("/api/environments/{env_id}/kpis/{kpi_id}/definition")
def set_definition(env_id: str, kpi_id: str, row: dict, u: User = Depends(need("approver"))):
    """Save a business definition for one KPI using the template's column names (same parser as the file import)."""
    import csv, io
    from .core.definitions import COLUMNS
    _env(env_id)
    k = next((k for k in svc.kpis(env_id) if k.id == kpi_id), None)
    if not k:
        raise HTTPException(404, "Unknown KPI")
    row = {**row, "KPI ID": kpi_id, "Power BI Measure": k.bi.measure if k.bi else ""}
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerow({c: row.get(c, "") for c in COLUMNS})
    res = svc.import_definitions(env_id, "ui-form.csv", buf.getvalue().encode(), u.name)
    if res["errors"]:
        raise HTTPException(400, "; ".join(res["errors"]))
    return res


@app.post("/api/environments/{env_id}/kpis/{kpi_id}/approval")
def approve_kpi(env_id: str, kpi_id: str, d: Decision, u: User = Depends(need("approver"))):
    _env(env_id)
    if d.decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(400, "decision must be APPROVED or REJECTED")
    return svc.approve_kpi(env_id, kpi_id, d.decision, u.name).model_dump(mode="json")


@app.get("/api/environments/{env_id}/dax-analysis")
def dax_analysis(env_id: str):
    _env(env_id)
    return svc.dax_analysis(env_id)


@app.get("/api/environments/{env_id}/sql-scripts", response_class=PlainTextResponse)
def sql_scripts(env_id: str):
    _env(env_id)
    return svc.sql_scripts(env_id)


@app.post("/api/environments/{env_id}/tests/generate")
def generate(env_id: str, u: User = Depends(need("tester"))):
    _env(env_id)
    return svc.generate(env_id, u.name)


@app.get("/api/environments/{env_id}/tests")
def tests(env_id: str):
    _env(env_id)
    return svc.store.tests(env_id)


@app.patch("/api/environments/{env_id}/tests/{test_id}")
def update_test(env_id: str, test_id: str, patch: dict, u: User = Depends(current_user)):
    _env(env_id)
    if "approval_status" in patch and ROLE_RANK[u.role] < ROLE_RANK["approver"]:
        raise HTTPException(403, "Approving tests requires role 'approver'")
    try:
        return svc.update_test(env_id, test_id, patch, u.name)
    except KeyError:
        raise HTTPException(404, "Unknown test")


class RunRequest(BaseModel):
    test_ids: Optional[list[str]] = None
    approved_only: bool = False


@app.post("/api/environments/{env_id}/runs")
def run(env_id: str, req: RunRequest, u: User = Depends(need("tester"))):
    _env(env_id)
    return svc.run(env_id, u.name, req.test_ids, req.approved_only)


@app.get("/api/environments/{env_id}/runs")
def runs(env_id: str):
    _env(env_id)
    return svc.store.runs(env_id)


@app.get("/api/runs/{run_id}/results")
def results(run_id: str):
    return svc.store.results(run_id)


@app.get("/api/environments/{env_id}/overview")
def overview(env_id: str):
    _env(env_id)
    return svc.overview(env_id)


@app.get("/api/environments/{env_id}/defects")
def defects(env_id: str):
    _env(env_id)
    return svc.store.defects(env_id)


@app.patch("/api/environments/{env_id}/defects/{defect_id}")
def update_defect(env_id: str, defect_id: str, patch: dict, u: User = Depends(need("tester"))):
    d = next((d for d in svc.store.defects(env_id) if d["defect_id"] == defect_id), None)
    if not d:
        raise HTTPException(404, "Unknown defect")
    comment = patch.pop("comment", None)
    d.update({k: v for k, v in patch.items() if k in ("status", "owner", "priority", "resolution")})
    if comment:
        d.setdefault("comments", []).append({"by": u.name, "text": comment})
    svc.store.upsert_defect(d)
    svc.store.audit(u.name, "defect.update", defect_id, patch)
    return d


@app.get("/api/audit")
def audit(u: User = Depends(need("approver"))):
    return svc.store.audit_log()


# ------------------------------------------------------------------ frontend
FRONTEND = ROOT / "frontend"


@app.middleware("http")
async def basic_auth(request, call_next):
    """Password-protect the whole app (UI + API) when BIQA_BASIC_AUTH="user:password" is set - the browser shows its own
    sign-in prompt, nothing is kept in page script. On a hosted deployment (Vercel) without any auth configured the app
    refuses to serve rather than run open with admin rights."""
    import base64
    import hmac
    cred = os.environ.get("BIQA_BASIC_AUTH", "")
    if cred:
        given = ""
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                given = base64.b64decode(h[6:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                given = ""
        if not hmac.compare_digest(given.encode(), cred.encode()):
            return Response("Sign in required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="VerityIQ", charset="UTF-8"'})
    elif os.environ.get("VERCEL") and not os.environ.get("BIQA_USERS_JSON"):
        return Response("VerityIQ is not configured: set the BIQA_BASIC_AUTH environment variable (user:password) in Vercel "
                        "and redeploy.", status_code=503, media_type="text/plain")
    return await call_next(request)


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """Always revalidate the UI files so an updated app is never shown with an old cached page."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


app.mount("/static", StaticFiles(directory=FRONTEND, check_dir=False), name="static")   # never fail start-up over the UI folder


@app.get("/")
def index():
    if not (FRONTEND / "index.html").exists():
        return Response(f"VerityIQ API is running, but the UI files were not found at {FRONTEND}.", status_code=500, media_type="text/plain")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    try:
        v = str(int(max((FRONTEND / f).stat().st_mtime for f in ("app.js", "styles.css"))))
    except OSError:
        v = "1"
    html = html.replace("/static/app.js", f"/static/app.js?v={v}").replace("/static/styles.css", f"/static/styles.css?v={v}")
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache, must-revalidate"})
