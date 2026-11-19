"""Orchestration service - glue between API, connectors, engine and repository."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .analyzers.dax import analyze_model, kpi_measure_missing
from .connectors.registry import create_connector, get_platform
import tempfile
import shutil
from datetime import datetime, timezone

from .analyzers.dax_infer import describe
from .core import definitions as defs
from .core.environment import (PROJECTS_DIR, ROOT, EnvironmentConfig, load_environments, load_kpis, load_requirements,
                               resolve_secrets, save_project_environment)
from .core.models import Evidence, FilterSpec, RunMode, Status, Tolerance
from .core.names import unbracket

from .core.models import is_slicer as is_slicer_type

TEXT_TYPES = ("string", "varchar", "char", "nvarchar", "nchar", "text", "character", "ntext", "clob", "varchar2", "nvarchar2")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
from .core.project import create_project, delete_project, infer_kpis, safe_extract, slug
from .engine.compare import compare_values
from .metadata.pbip import read_pbip
from .core.models import KPIDefinition, ModelMetadata, TestCase, TestResult
from .engine.runner import ExecContext, execute_test, new_run_id, summarize
from .engine.sql_generator import MappingError, dq_duplicate_sql, dq_null_sql, dq_ri_sql, kpi_value_sql
from .engine.test_generator import generate_tests
from .repository.store import Store


def _hyp(svc, env_id, k):
    from .engine.runner import _hypothesis
    model = svc.model(env_id)
    analyses = {a.measure.lower(): a for a in analyze_model(model, svc.env(env_id), svc.kpis(env_id))} if model else {}
    return _hypothesis(ExecContext(env=svc.env(env_id), bi=None, data=None, kpis={}, model=model, analyses=analyses), k)


class Service:
    def __init__(self, db_path: Optional[Path] = None, env_dir: Optional[Path] = None):
        self.store = Store(db_path or Path(os.environ.get("BIQA_DB", ROOT / "data" / "biqa.db")))
        self.env_dir = env_dir
        self.reload()

    def reload(self):
        self.envs: dict[str, EnvironmentConfig] = load_environments(self.env_dir)

    def env(self, env_id: str) -> EnvironmentConfig:
        if env_id not in self.envs:
            raise KeyError(f"Unknown environment {env_id}")
        return self.envs[env_id]

    # ----------------------------------------------------------- connectors
    def connector(self, env: EnvironmentConfig, side: str):
        b = env.bi if side == "bi" else env.data
        entry = get_platform(b.platform)
        if b.platform == "databricks" and not (b.config.get("server_hostname") and b.config.get("http_path")):
            raise ConnectionError("Databricks connection not configured (Settings → server hostname, HTTP path, client ID + secret)")
        if entry.support.value == "planned":
            raise NotImplementedError(f"{entry.name} adapter is planned, not implemented ({entry.notes})")
        if side == "data" and b.platform == "files":           # no credentials involved; ignore any leftover DB settings
            cfg = {"source_folder": (b.config or {}).get("source_folder"), "folder": str(self.sources_dir(env)),
                   "tables": {tm.source: tm.file for tm in env.mapping.tables.values() if tm.file and tm.source}}
        else:
            cfg = resolve_secrets(b.config)
        return create_connector(b.platform, cfg)

    # ------------------------------------------------------------------ Excel / CSV source files
    SOURCE_EXT = (".xlsx", ".xlsm", ".xls", ".xlsb", ".csv", ".tsv", ".txt", ".parquet", ".json")

    def sources_dir(self, env: EnvironmentConfig) -> Path:
        return PROJECTS_DIR / env.id / "sources"

    def source_files(self, env_id: str) -> dict:
        from .connectors.files import locate
        env = self.env(env_id)
        folder = self.sources_dir(env)
        rows = []
        for tname, tm in env.mapping.tables.items():
            if not tm.file:
                continue
            path, where = locate(tm.file, folder, (env.data.config or {}).get("source_folder"))
            rows.append({"table": tname, "sql_name": tm.source, "file": tm.file.get("file"), "original_path": tm.file.get("path"),
                         "item": tm.file.get("item"), "item_kind": tm.file.get("item_kind"), "header": tm.file.get("header"),
                         "status": where or "missing"})
        uploaded = sorted(p.name for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in self.SOURCE_EXT) if folder.exists() else []
        return {"tables": rows, "uploaded": uploaded, "file_based": bool(rows),
                "needed": sorted({r["file"] for r in rows}), "missing": sorted({r["file"] for r in rows if r["status"] == "missing"})}

    def upload_source_files(self, env_id: str, files: list[tuple[str, bytes]], actor: str) -> dict:
        env = self.env(env_id)
        folder = self.sources_dir(env)
        folder.mkdir(parents=True, exist_ok=True)
        saved, total = [], 0
        for name, data in files:
            base = Path(name.replace("\\", "/")).name
            if Path(base).suffix.lower() not in self.SOURCE_EXT or base.startswith("."):
                continue
            total += len(data)
            if total > 500 * 1024 * 1024:
                raise ValueError("Upload exceeds 500 MB")
            (folder / base).write_bytes(data)
            saved.append(base)
        if not saved:
            raise ValueError("No Excel/CSV/Parquet files in the upload (.xlsx, .xlsm, .csv, .tsv, .parquet, .json)")
        if env.data.platform != "files" and any(tm.file for tm in env.mapping.tables.values()):
            env.data = env.data.model_copy(update={"platform": "files", "config": {}})
            save_project_environment(env)
            self.reload()
        self.store.audit(actor, "source.files", env_id, {"files": saved})
        out = self.source_files(env_id)
        out["saved"] = saved
        out["connection"] = self.test_connections(env_id, actor)
        return out

    def refresh_source_types(self, env_id: str, actor: str = "system") -> dict:
        """Read physical column types from the source; for dates stored as text, detect their format.
        Lets the generated SQL convert them exactly as Power Query does (e.g. TRY_TO_DATE(col, 'MM/dd/yyyy'))."""
        from .core.formats import detect_date_format
        from .engine.sql_generator import qualify
        env = self.env(env_id)
        if not env.project:
            return {}
        data = self.connector(env, "data")
        if not hasattr(data, "describe_table"):
            return {}
        report: dict = {"tables": 0, "converted": [], "errors": [], "checks": [], "platform": env.data.platform,
                        "checked_at": now_iso()}
        cat = env.data.config.get("catalog") if env.data.platform != "files" else None
        used = self._used_tables(env)
        slicer_cols: set = set()
        try:
            from .core.models import is_slicer
            from .engine.test_generator import _field_for_model_ref
            for v in (self.model(env_id) or ModelMetadata(platform="", model_name="")).visuals:
                if is_slicer(v.visual_type):
                    slicer_cols |= {f for d in v.dimensions[:1] for f in [_field_for_model_ref(env, d)] if f}
        except Exception:  # noqa: BLE001
            pass
        for tname, tm in env.mapping.tables.items():
            if tm.calculated:
                continue
            chk = {"table": tname, "source": tm.source, "used": tname in used, "status": "no_source", "columns_found": 0, "columns_total": 0}
            report["checks"].append(chk)
            if not tm.source or tm.source.startswith("("):
                continue
            fq = qualify(tm.source, cat)
            chk["checked_as"] = fq
            try:
                meta = data.describe_table(fq)
            except Exception as e:  # noqa: BLE001
                msg = str(e).splitlines()[0][:200]
                chk.update(status="not_found", error=msg)
                report["errors"].append(f"{tname}: {msg}")
                continue
            chk["status"] = "found"
            report["tables"] += 1
            types = {c.name.lower(): c.data_type for c in meta.columns}
            actual = [c.name for c in meta.columns]
            report.setdefault("columns", {})[tname] = actual
            norm = lambda x: re.sub(r"[^0-9a-z]", "", x.lower())  # noqa: E731
            for fname, fm in env.mapping.fields.items():
                if fm.table != tname or not fm.source:
                    continue
                if fm.source_expr:                       # translated formula: check the source columns it reads
                    bad = []
                    for col in set(re.findall(r"\{t\}\.`((?:[^`]|``)+)`", fm.source_expr)):
                        c0 = col.replace("``", "`")
                        if c0.lower() in types:
                            continue
                        exact = [a for a in actual if norm(a) == norm(c0)]
                        if len(exact) == 1:
                            fm.source_expr = fm.source_expr.replace("{t}.`" + col + "`", "{t}.`" + exact[0].replace("`", "``") + "`")
                        else:
                            bad.append(c0)
                    if bad:
                        report.setdefault("formula_errors", []).append(f"{fname}: formula reads {', '.join(bad)} - not in {fq}")
                    continue
                chk["columns_total"] += 1
                if fm.source.lower() not in types:
                    exact = [a for a in actual if norm(a) == norm(fm.source)]
                    if len(exact) == 1:
                        fm.source = exact[0]                     # same name, different case/separators
                    else:
                        import difflib
                        sug = difflib.get_close_matches(fm.source.lower(), [a.lower() for a in actual], n=3, cutoff=0.5)
                        report.setdefault("unresolved", []).append({
                            "field": fname, "table": tname, "column": fname.split(".", 1)[-1], "current": fm.source,
                            "suggestions": [next(a for a in actual if a.lower() == x) for x in sug]})
                        continue
                chk["columns_found"] += 1
                fm.source_type = types.get(fm.source.lower(), fm.source_type)
                if fm.data_type == "date" and (fm.source_type or "").lower().startswith(TEXT_TYPES):
                    try:
                        fm.source_format = detect_date_format(data.sample_values(fq, fm.source)) if hasattr(data, "sample_values") else None
                    except Exception:  # noqa: BLE001
                        fm.source_format = None
                    report["converted"].append(f"{fname}: text -> date ({fm.source_format or 'format not detected'})")
                if fname in slicer_cols and not fm.sample_values and fm.data_type != "date" and hasattr(data, "sample_values"):
                    try:                                    # real values to test slicer selections with
                        fm.sample_values = [v for v in data.sample_values(fq, fm.source, 20) if str(v).strip()][:20]
                        if fm.sample_values:
                            report.setdefault("slicer_values", []).append(f"{fname}: {fm.sample_values[0]!r}")
                    except Exception:  # noqa: BLE001
                        pass
        save_project_environment(env)
        self.reload()
        self.store.save_snapshot(env_id, "source_types", report)
        self.store.audit(actor, "source.types", env_id, report)
        return report

    def _used_tables(self, env: EnvironmentConfig) -> set:
        """Model tables behind the KPIs in test scope (their absence blocks tests)."""
        try:
            model = self.model(env.id)
            from .core.scope import implicit_kpis, in_scope
            ks = in_scope(self.kpis(env.id) + implicit_kpis(model), model, self.scope(env.id))
            out = set()
            for k in ks:
                for sp in k.all_specs():
                    if sp.table:
                        out.add(sp.table)
            return out
        except Exception:  # noqa: BLE001
            return set()

    def test_connections(self, env_id: str, actor: str) -> dict:
        env = self.env(env_id)
        out = {}
        for side in ("bi", "data"):
            try:
                res = self.connector(env, side).test_connection()
                ok, msg, mode = res.ok, res.message, res.mode.value
            except Exception as e:  # noqa: BLE001
                ok, msg, mode = False, str(e), env.mode.value
            self.store.set_connection(env_id, side, ok, msg, mode)
            out[side] = {"ok": ok, "message": msg, "mode": mode}
        if not out.get("data", {}).get("ok") and env.project:
            self.store.save_snapshot(env_id, "source_types", {"platform": env.data.platform, "connection_error": out["data"]["message"],
                                                              "checked_at": now_iso()})
        if out.get("data", {}).get("ok") and env.project:
            try:
                out["source_types"] = self.refresh_source_types(env_id, actor)
                if self.store.tests(env_id):
                    self.generate(env_id, actor)          # scripts now reflect real column types + catalog
            except Exception as e:  # noqa: BLE001
                out["source_types"] = {"errors": [str(e)[:200]]}
        self.store.audit(actor, "connection.test", env_id, out)
        return out

    # ----------------------------------------------------------- KPIs
    def kpis(self, env_id: str) -> list[KPIDefinition]:
        env = self.env(env_id)
        base = {k.id: k for k in load_kpis(env)}
        for o in self.store.kpi_overrides(env_id):
            base[o["id"]] = KPIDefinition(**o)
        return list(base.values())

    def save_kpi(self, env_id: str, kpi: KPIDefinition, actor: str) -> KPIDefinition:
        existing = {k.id: k for k in self.kpis(env_id)}.get(kpi.id)
        if existing and existing.model_dump(exclude={"approval_status", "approved_by"}) != kpi.model_dump(exclude={"approval_status", "approved_by"}):
            kpi.approval_status, kpi.approved_by = "DRAFT", None   # any definition change needs re-approval
        elif not existing:
            kpi.approval_status, kpi.approved_by = "DRAFT", None
        self.store.save_kpi(env_id, kpi.model_dump(mode="json"))
        self.store.audit(actor, "kpi.save", f"{env_id}/{kpi.id}", {"status": kpi.approval_status})
        return kpi

    def approve_kpi(self, env_id: str, kpi_id: str, decision: str, actor: str) -> KPIDefinition:
        k = {k.id: k for k in self.kpis(env_id)}[kpi_id]
        k.approval_status, k.approved_by = decision, actor
        self.store.save_kpi(env_id, k.model_dump(mode="json"))
        self.store.audit(actor, f"kpi.{decision.lower()}", f"{env_id}/{kpi_id}")
        return k

    # ----------------------------------------------------------- discovery
    def discover(self, env_id: str, actor: str) -> dict:
        env = self.env(env_id)
        out: dict = {}
        bi = self.connector(env, "bi")
        meta = bi.discover()
        self.store.save_snapshot(env_id, "bi_model", meta.model_dump(mode="json"))
        out["bi"] = {"tables": len(meta.tables), "measures": len(meta.all_measures()), "visuals": len(meta.visuals),
                     "relationships": len(meta.relationships), "unsupported": meta.unsupported}
        try:
            data = self.connector(env, "data")
            tables = data.list_tables()
            self.store.save_snapshot(env_id, "source_tables", [t.model_dump(mode="json") for t in tables])
            out["data"] = {"tables": len(tables)}
        except Exception as e:  # noqa: BLE001
            out["data"] = {"error": str(e)}
        self.store.audit(actor, "environment.discover", env_id, out)
        return out

    def model(self, env_id: str) -> Optional[ModelMetadata]:
        s = self.store.snapshot(env_id, "bi_model")
        return ModelMetadata(**s["body"]) if s else None

    # ----------------------------------------------------------- projects (PBIP upload)
    def create_project_from_files(self, files: list[tuple[str, bytes]], actor: str, name: Optional[str] = None,
                                  connectors: Optional[dict] = None) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="pbip-"))
        try:
            n = safe_extract(files, tmp)
            if n == 0:
                raise ValueError("No usable files in upload (expected a PBIP folder or .zip containing .tmdl files).")
            model = read_pbip(tmp)
            pid = f"{slug(name or model.model_name)}-{datetime.now(timezone.utc).strftime('%m%d%H%M%S')}"
            dest = PROJECTS_DIR / pid / "pbip"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(dest))
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        model = read_pbip(dest)
        env, notes = create_project(model, dest, project_id=pid, name=name)
        needed = {(t.file_source or {}).get("file", "").lower() for t in model.tables if t.file_source} - {""}
        for f in dest.rglob("*"):                         # data files uploaded together with the PBIP
            if f.is_file() and f.name.lower() in needed:
                (PROJECTS_DIR / pid / "sources").mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, PROJECTS_DIR / pid / "sources" / f.name)
        if connectors:
            env.bi = env.bi.model_copy(update=connectors.get("bi", {})) if connectors.get("bi") else env.bi
            if connectors.get("data"):
                env.data = env.data.model_copy(update=connectors["data"])
            env.mode = connectors.get("mode", env.mode)
        save_project_environment(env)
        self.reload()
        self.store.save_snapshot(pid, "bi_model", model.model_dump(mode="json"))
        self.store.save_snapshot(pid, "project_notes", notes)
        for k in infer_kpis(model):
            self.store.save_kpi(pid, k.model_dump(mode="json"))
        if env.data.platform == "files" and not connectors:
            try:
                self.test_connections(pid, actor)      # loads the workbook(s) if found next to the PBIP or at their original path
            except Exception:  # noqa: BLE001
                pass
        self.store.audit(actor, "project.create", pid, {"files": n, "measures": len(model.all_measures()),
                                                       "visuals": len(model.visuals), "notes": notes[:20]})
        return self.project_summary(pid)

    def reanalyze(self, pid: str, actor: str) -> dict:
        """Re-read the stored PBIP with the current code (newer translations / fixes) without a new upload.
        Keeps connections, test context, table names, column fixes, approved or business KPI definitions."""
        root = PROJECTS_DIR / pid / "pbip"
        if not root.exists():
            raise ValueError("The PBIP files of this project are not stored - upload it again.")
        old = self.env(pid)
        model = read_pbip(root)
        env, notes = create_project(model, root, project_id=pid, name=old.name)
        env.bi, env.data, env.mode, env.default_context = old.bi, old.data, old.mode, old.default_context
        for tname, tm in env.mapping.tables.items():
            o = old.mapping.tables.get(tname)
            if o and o.source and not tm.calculated:
                tm.source = o.source                       # table names the user corrected
            if o and o.primary_key:
                tm.primary_key = o.primary_key
        for fname, fm in env.mapping.fields.items():
            o = old.mapping.fields.get(fname)
            if not o:
                continue
            if not fm.source_expr and o.source and not o.source_expr and fm.table == o.table:
                fm.source = o.source                       # column fixes (case, manual mapping)
            fm.source_type, fm.source_format = o.source_type or fm.source_type, o.source_format or fm.source_format
            fm.sample_values = o.sample_values or fm.sample_values
            fm.model_value_map = o.model_value_map or fm.model_value_map
        save_project_environment(env)
        self.reload()
        self.store.save_snapshot(pid, "bi_model", model.model_dump(mode="json"))
        self.store.save_snapshot(pid, "project_notes", notes)
        olds = {(k.bi.measure if k.bi else k.id).lower(): k for k in self.kpis(pid)}
        used_ids = {k.id for k in olds.values()}
        seen = set()
        nxt = max([int(i.split("-")[-1]) for i in used_ids if i.split("-")[-1].isdigit()] + [0]) + 1
        for k in infer_kpis(model):
            key = (k.bi.measure if k.bi else k.id).lower()
            o = olds.get(key)
            seen.add(key)
            if o and (o.basis == "business" or o.approval_status == "APPROVED"):
                continue                                   # keep the reviewed definition
            if o:
                k.id = o.id
            else:
                k.id = f"KPI-{nxt:03d}"
                nxt += 1
            self.store.save_kpi(pid, k.model_dump(mode="json"))
        for key, o in olds.items():
            if key not in seen and not key.startswith("implicit:") and not o.id.startswith("IMP-"):
                self.store.delete_kpi(pid, o.id)
        try:
            if env.data.platform:
                self.test_connections(pid, actor)
        except Exception:  # noqa: BLE001
            pass
        if self.store.tests(pid):
            self.generate(pid, actor)
        self.store.audit(actor, "project.reanalyze", pid, {"notes": len(notes)})
        return self.project_summary(pid)

    def _scope_fields(self, pid: str, env, model) -> tuple[set, set]:
        """Field keys ('Table.Column') and tables the tests in scope depend on."""
        from .core.scope import implicit_kpis, in_scope
        from .engine.test_generator import _field_for_model_ref
        fields, tables = set(), set()
        try:
            ks = in_scope(self.kpis(pid) + implicit_kpis(model), model, self.scope(pid))
        except Exception:  # noqa: BLE001
            return fields, tables
        meas = set()
        for k in ks:
            if k.bi:
                meas.add(k.bi.measure.lower() if k.bi.measure.startswith("implicit:") else unbracket(k.bi.measure).lower())
            for sp in k.all_specs():
                if sp.table:
                    tables.add(sp.table)
                    if sp.column:
                        fields.add(f"{sp.table}.{sp.column}")
                for f in sp.filters:
                    fields.add(f.field)
                if sp.per_field:
                    fields.add(sp.per_field)
                for x in re.findall(r"\{([^}]+)\}", sp.column_expr or ""):
                    fields.add(x)
            if k.date_field:
                fields.add(k.date_field)
            fields |= set(k.grains or [])
        pages = set()
        for v in model.visuals:
            if any((m.lower() if m.startswith("implicit:") else unbracket(m).lower()) in meas for m in v.measures) and not v.page_hidden:
                pages.add(v.page)
                for d in v.dimensions + [f.get("ref", "") for f in v.filter_specs]:
                    f = _field_for_model_ref(env, d) if d else None
                    if f:
                        fields.add(f)
        for v in model.visuals:
            if v.page in pages and is_slicer_type(v.visual_type):
                for d in v.dimensions[:1]:
                    f = _field_for_model_ref(env, d)
                    if f:
                        fields.add(f)
        tables |= {f.split(".", 1)[0] for f in fields}
        return fields, tables

    def _scope_count(self, pid: str, model) -> int:
        if model is None:
            return 0
        try:
            from .core.scope import implicit_kpis, in_scope
            return len(in_scope(self.kpis(pid) + implicit_kpis(model), model, self.scope(pid)))
        except Exception:  # noqa: BLE001
            return 0

    def project_summary(self, pid: str) -> dict:
        env, model = self.env(pid), self.model(pid)
        all_notes = (self.store.snapshot(pid, "project_notes") or {}).get("body", [])
        notes, notes_info = all_notes, []
        if model is not None and all_notes:
            fields, tables = self._scope_fields(pid, env, model)
            notes, notes_info = [], []
            for n in all_notes:
                m = re.match(r"^'((?:[^']|'')+)'\[((?:[^\]]|\]\])+)\]:", n)
                if m:
                    hit = f"{m.group(1).replace(chr(39) * 2, chr(39))}.{m.group(2).replace(']]', ']')}" in fields
                else:
                    t = re.match(r"^([^:\[]+?):", n)
                    named = [x.split(" (")[0].strip() for x in n.split(":", 1)[1].split(",")] if "need a physical source" in n else []
                    hit = (t and t.group(1).strip() in tables) or any(x in tables for x in named)
                (notes if hit else notes_info).append(n)
        return {"id": pid, "name": env.name, "model": model.model_name if model else None,
                "tables": [{"name": t.name, "source_kind": t.source_kind, "source": env.mapping.tables.get(t.name).source if env.mapping.tables.get(t.name) else None,
                            "columns": len(t.columns), "measures": len(t.measures)} for t in (model.tables if model else [])],
                "relationships": len(model.relationships) if model else 0, "visuals": len(model.visuals) if model else 0,
                "pages": sum(len(r.get("pages", [])) for r in (model.reports if model else [])),
                "measures": len(model.all_measures()) if model else 0, "source_platform_detected": env.source_platform_detected,
                "data_platform": env.data.platform, "notes": notes, "notes_info": notes_info,
                "unsupported": model.unsupported if model else [],
                "source_check": (self.store.snapshot(pid, "source_types") or {}).get("body"),
                "scope": {**self.scope(pid), "in_scope_count": self._scope_count(pid, model)},
                "source_files": self.source_files(pid) if any(tm.file for tm in env.mapping.tables.values()) else None,
                "kpis_business": sum(1 for k in self.kpis(pid) if k.basis == "business"),
                "kpis_inferred": sum(1 for k in self.kpis(pid) if k.basis == "inferred")}

    def create_sample_project(self, actor: str) -> dict:
        demo = ROOT / "demo"
        if not (demo / "data" / "cc_sample.duckdb").exists():
            import subprocess, sys
            subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo.py")], check=True)
        files = [(str(p.relative_to(demo / "pbip")), p.read_bytes()) for p in (demo / "pbip").rglob("*") if p.is_file()]
        summary = self.create_project_from_files(files, actor, name="Customer Center (Sample)", connectors={
            "mode": RunMode.DEMO,
            "bi": {"platform": "demo_bi", "config": {"pbip_path": "", "emulation": "demo/bi_emulation.yaml"}},
            "data": {"platform": "demo_sql", "config": {"database": "demo/data/cc_sample.duckdb", "catalog_alias": "demo_catalog",
                                                        "catalog_map": {"main": "demo_catalog"}}}})
        env = self.env(summary["id"])
        env.bi.config["pbip_path"] = str(PROJECTS_DIR / env.id / "pbip")
        env.sample = True
        if "Customers.State" in env.mapping.fields:
            env.mapping.fields["Customers.State"].sample_values = ["TX"]
        save_project_environment(env)
        self.reload()
        self.import_definitions(env.id, "kpi_definitions_sample.csv", (demo / "kpi_definitions_sample.csv").read_bytes(), actor)
        return self.project_summary(env.id)

    def delete_project(self, pid: str, actor: str) -> None:
        if not self.env(pid).project:
            raise ValueError("Only uploaded projects can be deleted")
        delete_project(pid)
        self.reload()
        self.store.audit(actor, "project.delete", pid)

    # ----------------------------------------------------------- test scope
    def scope(self, env_id: str) -> dict:
        snap = self.store.snapshot(env_id, "scope")
        default = {"mode": "dashboard" if self.env(env_id).project else "all", "selected": [], "evidence": []}
        sc = {**default, **(snap["body"] if snap else {})}
        if sc["mode"] == "all":
            try:
                sc["_dead"] = sorted(self.dead_measures(env_id))   # honours 'keep' marks and other reports
            except Exception:  # noqa: BLE001
                pass
        return sc

    def scope_view(self, env_id: str) -> dict:
        from .core.scope import displayed
        model = self.model(env_id)
        sc = self.scope(env_id)
        shown = displayed(model) if model else {}
        ev = {e["measure"]: e for e in sc.get("evidence", [])}
        rows = []
        for k in self.kpis(env_id):
            if not k.bi:
                continue
            key = k.bi.measure if k.bi.measure.startswith("implicit:") else unbracket(k.bi.measure)
            rows.append({"kpi_id": k.id, "name": k.name, "measure": key, "implicit": bool(k.bi.expression),
                         "displayed_on": shown.get(key, []), "evidence": ev.get(key),
                         "selected": key.lower() in {x.lower() for x in sc.get("selected", [])}})
        from .core.scope import in_scope, includes
        from .analyzers.usage import unused_measures
        n_in = len(in_scope(self.kpis(env_id), model, sc)) if model else 0
        dead = unused_measures(model)
        for r in rows:
            r["unused"] = (not r["implicit"]) and r["measure"].lower() in dead
        return {**sc, "rows": rows, "in_scope_count": n_in, "displayed_count": len(shown), "include": includes(sc),
                "unused_count": len(dead)}

    def set_scope(self, env_id: str, body: dict, actor: str) -> dict:
        from .core.scope import MODES
        sc = self.scope(env_id)
        if body.get("mode") in MODES:
            sc["mode"] = body["mode"]
        if "selected" in body:
            sc["selected"] = list(dict.fromkeys(body["selected"]))
        if isinstance(body.get("include"), dict):
            from .core.scope import INCLUDE_KEYS
            sc["include"] = {k: bool(v) for k, v in body["include"].items() if k in INCLUDE_KEYS}
        elif "mode" in body and body.get("mode") != sc.get("mode_prev"):
            sc.pop("include", None)                     # switching mode resets the switches to that mode's defaults
        sc["mode_prev"] = sc["mode"]
        if "include_unused" in body:
            sc["include_unused"] = bool(body["include_unused"])
        sc.pop("_dead", None)
        sc.pop("_dead", None)
        self.store.save_snapshot(env_id, "scope", sc)
        self.store.audit(actor, "scope.set", env_id, {"mode": sc["mode"], "selected": len(sc["selected"])})
        self.generate(env_id, actor)
        return self.scope_view(env_id)

    def scope_from_files(self, env_id: str, files: list[tuple[str, bytes]], actor: str) -> dict:
        """Read requirement docs / screenshots, find the KPIs they mention, preselect them (mode=requirements)."""
        from .core.scope import extract_text, match_kpis
        model = self.model(env_id)
        if not model:
            raise ValueError("Upload the PBIP first")
        cands = [k.bi.measure if k.bi.measure.startswith("implicit:") else unbracket(k.bi.measure)
                 for k in self.kpis(env_id) if k.bi]
        found, reads = [], []
        for name, data in files:
            text, how = extract_text(name, data)
            reads.append({"file": name, "how": how, "chars": len(text)})
            for m in match_kpis(text, model, cands, fuzzy=0.8 if how.startswith(("OCR", "Claude")) else 0.86):
                m["file"] = name
                found.append(m)
        best: dict = {}
        for m in found:
            if m["measure"] not in best or best[m["measure"]]["score"] < m["score"]:
                best[m["measure"]] = m
        sc = self.scope(env_id)
        sc.update({"mode": "requirements", "selected": list(best), "evidence": list(best.values()), "files": reads})
        sc.pop("_dead", None)
        self.store.save_snapshot(env_id, "scope", sc)
        self.store.audit(actor, "scope.from_files", env_id, {"files": reads, "matched": len(best)})
        self.generate(env_id, actor)
        return self.scope_view(env_id)

    def import_definitions(self, env_id: str, filename: str, data: bytes, actor: str) -> dict:
        model = self.model(env_id)
        if not model:
            raise ValueError("Upload the PBIP first")
        updated, errors = defs.import_definitions(filename, data, self.kpis(env_id), model, actor)
        for k in updated:
            self.store.save_kpi(env_id, k.model_dump(mode="json"))
        if updated:
            self.generate(env_id, actor)   # refresh test cases + SQL from the business definitions
        self.store.audit(actor, "definitions.import", env_id, {"file": filename, "updated": [k.id for k in updated], "errors": errors})
        return {"updated": len(updated), "errors": errors}

    def definition_template(self, env_id: str, fmt: str) -> bytes:
        model = self.model(env_id)
        kpis = self.kpis(env_id)
        return defs.export_xlsx(kpis, model) if fmt == "xlsx" else defs.export_csv(kpis, model)

    def update_settings(self, env_id: str, patch: dict, actor: str) -> dict:
        env = self.env(env_id)
        if not env.project:
            raise ValueError("Settings of built-in configurations are edited in YAML")
        for side in ("bi", "data"):
            if side in patch:
                b = getattr(env, side)
                if "platform" in patch[side]:
                    b.platform = patch[side]["platform"]
                for k, v in (patch[side].get("config") or {}).items():
                    secretish = bool(re.search(r"secret|password|passphrase|token", k, re.I))
                    if isinstance(v, str) and v.strip() in ("••••", "(saved)") and secretish:
                        continue                                   # unchanged masked value
                    if isinstance(v, str) and v.strip() == "":
                        if not secretish:                          # empty secret box = keep the saved one
                            b.config.pop(k, None)
                    elif secretish and isinstance(v, str) and not v.startswith("${"):
                        from .core.secrets import put_secret
                        b.config[k] = put_secret(f"{env_id}.{side}.{k}", v.strip())   # value goes to the vault, config keeps a reference
                    else:
                        b.config[k] = v.strip() if isinstance(v, str) else v
                if patch[side].get("clear"):
                    for k in patch[side]["clear"]:
                        b.config.pop(k, None)
        if "default_context" in patch:
            env.default_context = env.default_context.model_copy(update=patch["default_context"])
        for t, src in (patch.get("table_sources") or {}).items():
            if t in env.mapping.tables:
                env.mapping.tables[t].source = src or None
        for f, src in (patch.get("column_sources") or {}).items():
            if f in env.mapping.fields and src:
                env.mapping.fields[f].source = src
                env.mapping.fields[f].source_type = None
        for f, vals in (patch.get("sample_values") or {}).items():
            if f in env.mapping.fields:
                env.mapping.fields[f].sample_values = vals
        save_project_environment(env)
        self.reload()
        if patch.get("column_sources") or patch.get("table_sources"):
            try:
                self.refresh_source_types(env_id, actor)
            except Exception:  # noqa: BLE001
                pass
        if any(k in patch for k in ("data", "table_sources", "default_context", "column_sources")) and self.store.tests(env_id):
            self.generate(env_id, actor)
        self.store.audit(actor, "settings.update", env_id, {k: ("…" if k in ("bi", "data") else v) for k, v in patch.items()})
        return self.env(env_id).public_dict()

    def latest_results(self, env_id: str) -> dict:
        latest: dict = {}
        for run in reversed(self.store.runs(env_id, 500)):
            for r in self.store.results(run["run_id"]):
                latest[r["test_id"]] = r
        return latest

    def record_manual(self, env_id: str, test_id: str, body: dict, actor: str) -> dict:
        t = TestCase(**self.store.test(env_id, test_id))
        if t.executor != "kpi_value":
            return self._record_outcome(env_id, t, body, actor)
        k = {x.id: x for x in self.kpis(env_id)}.get(t.kpi_id or "")
        run_id = new_run_id() + "-M"
        r = TestResult(test_id=t.id, run_id=run_id, status=Status.ERROR, mode=RunMode.MANUAL,
                       executed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        exp, act = body.get("expected"), body.get("actual")
        exp_from = "entered"
        if exp is None:                                   # reuse the value the tool computed from the source
            prev = self.latest_results(env_id).get(test_id) or {}
            if prev.get("expected") is not None and prev.get("mode") != RunMode.MANUAL.value:
                exp, exp_from = prev["expected"], f"computed by the tool ({prev.get('mode')}, {prev.get('executed_at')})"
        r.evidence = [Evidence(kind="note", label="Manually recorded values", mode=RunMode.MANUAL,
                               content=f"Recorded by {actor}. Expected (source) = {exp} [{exp_from}]; actual (BI) = {act}. {body.get('note') or ''}".strip()),
                      Evidence(kind="sql", label="Expected value - source SQL (run manually)", content={"sql": t.params.get("sql", "")}),
                      Evidence(kind="dax", label="Actual value - BI query (reference)", content={"query": t.params.get("dax", "")})]
        ok, why = k.is_independently_defined() if k else (False, "KPI not found")
        c = compare_values(exp, act, t.tolerance or (k.tolerance if k else None) or Tolerance())
        r.expected, r.actual, r.difference = c.expected, c.actual, c.difference
        if not ok:
            r.status, r.message = Status.NOT_VALIDATED, f"Values recorded, but the expected value is not independently established: {why}"
        else:
            r.status, r.message = c.status, c.message
            if c.status == Status.FAIL and k:
                r.severity = "High"
                r.root_cause_hypothesis, r.remediation_suggestion = _hyp(self, env_id, k)
        self.store.start_run(run_id, env_id, RunMode.MANUAL.value, actor)
        self.store.save_result(r.model_dump(mode="json"))
        self.store.finish_run(run_id, {**summarize([r]), "mode": "MANUAL", "manual": True})
        self._defect_sync(env_id, t, r)
        self.store.audit(actor, "result.manual", f"{env_id}/{test_id}", {"expected": exp, "actual": act, "status": r.status.value})
        return r.model_dump(mode="json")

    def _record_outcome(self, env_id: str, t: TestCase, body: dict, actor: str) -> dict:
        """Tester's observed outcome for a non-numeric test (UI interaction, visual checks, manual steps)."""
        st = str(body.get("status") or "").upper()
        if st not in ("PASS", "FAIL", "BLOCKED"):
            raise ValueError("Choose Passed, Failed or Blocked")
        note = (body.get("note") or "").strip()
        if st == "FAIL" and not note:
            raise ValueError("Describe what you observed when recording a failure")
        run_id = new_run_id() + "-M"
        r = TestResult(test_id=t.id, run_id=run_id, status=Status(st), mode=RunMode.MANUAL,
                       executed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       message={"PASS": "Verified manually", "FAIL": "Failed manual check", "BLOCKED": "Blocked"}[st] + (f": {note}" if note else ""),
                       actual=note or None)
        r.evidence = [Evidence(kind="note", label="Manual check", mode=RunMode.MANUAL,
                               content=f"Recorded by {actor}: {st}. Steps: {' / '.join(t.steps)}. Observation: {note or '-'}")]
        if st == "FAIL":
            r.severity = "Medium"
        self.store.start_run(run_id, env_id, RunMode.MANUAL.value, actor)
        self.store.save_result(r.model_dump(mode="json"))
        self.store.finish_run(run_id, {**summarize([r]), "mode": "MANUAL", "manual": True})
        self._defect_sync(env_id, t, r)
        self.store.audit(actor, "result.manual", f"{env_id}/{t.id}", {"status": st})
        return r.model_dump(mode="json")

    # ------------------------------------------------------------------ model usage (Measure Killer-style)
    def pbip_root(self, env_id: str) -> Optional[Path]:
        p = PROJECTS_DIR / env_id / "pbip"
        if p.exists():
            return p
        demo = Path(__file__).resolve().parents[2] / "demo" / "pbip"
        return demo if demo.exists() and not self.env(env_id).project else None

    def _usage_inputs(self, env_id: str) -> dict:
        from .analyzers.vpax import size_map
        from .core.models import VisualMeta
        keep = (self.store.snapshot(env_id, "usage_keep") or {}).get("body", {}).get("keys", [])
        extra = (self.store.snapshot(env_id, "usage_reports") or {}).get("body", {}).get("reports", [])
        vpax = (self.store.snapshot(env_id, "usage_vpax") or {}).get("body")
        visuals = [VisualMeta(**v) for r in extra for v in r.get("visuals", [])]
        refs = [x for r in extra for x in r.get("report_refs", [])]
        return {"keep": set(keep), "extra_visuals": visuals, "extra_refs": refs, "sizes": size_map(vpax)}

    def usage(self, env_id: str) -> dict:
        from .analyzers.usage import analyze_usage
        out = analyze_usage(self.model(env_id), **self._usage_inputs(env_id))
        if out.get("available"):
            extra = (self.store.snapshot(env_id, "usage_reports") or {}).get("body", {}).get("reports", [])
            vpax = (self.store.snapshot(env_id, "usage_vpax") or {}).get("body")
            out["extra_reports"] = [{"name": r["name"], "visuals": len(r.get("visuals", [])), "pages": r.get("pages", 0)} for r in extra]
            out["vpax"] = {"source": vpax.get("source"), "total_bytes": vpax.get("total_bytes"), "uploaded_at": vpax.get("uploaded_at")} if vpax else None
        return out

    def dead_measures(self, env_id: str) -> set:
        from .analyzers.usage import unused_measures
        return unused_measures(self.model(env_id), **self._usage_inputs(env_id))

    def set_keep(self, env_id: str, keys: list[str], keep: bool, actor: str) -> dict:
        cur = set((self.store.snapshot(env_id, "usage_keep") or {}).get("body", {}).get("keys", []))
        cur = cur | set(keys) if keep else cur - set(keys)
        self.store.save_snapshot(env_id, "usage_keep", {"keys": sorted(cur)})
        self.store.audit(actor, "usage.keep", env_id, {"keys": keys, "keep": keep})
        return self.usage(env_id)

    def add_usage_reports(self, env_id: str, files: list[tuple[str, bytes]], actor: str) -> dict:
        """Other reports built on this semantic model (thin reports): their fields count as used."""
        from .metadata.pbip import collect_refs, parse_legacy_report, parse_pbir_report
        tmp = Path(tempfile.mkdtemp(prefix="rep-"))
        try:
            if safe_extract(files, tmp) == 0:
                raise ValueError("No report files found - upload the .Report folder or a .zip of it (PBIR or report.json).")
            dirs = sorted({p.parent.parent for p in tmp.rglob("definition/pages")} | {p.parent for p in tmp.rglob("report.json")
                                                                                        if not (p.parent / "definition" / "pages").exists()},
                          key=lambda p: str(p))
            if not dirs:
                raise ValueError("No report found in the upload (expected definition/pages/… or report.json).")
            store = (self.store.snapshot(env_id, "usage_reports") or {}).get("body", {}).get("reports", [])
            added = []
            for d in dirs:
                name = re.sub(r"\.Report$", "", d.name) or "Report"
                notes: list = []
                vis, pages = (parse_pbir_report(d, notes) if (d / "definition" / "pages").exists() else parse_legacy_report(d / "report.json", notes))
                for v in vis:
                    v.report = name
                refs = [{"ref": r, "where": f"[{name}] " + ("Report filter" if pg["id"] == "__report__" else f"Page filter on '{pg['name']}'")}
                        for pg in pages for r in pg.get("filter_refs", [])]
                store = [r for r in store if r["name"] != name]
                store.append({"name": name, "visuals": [v.model_dump(mode="json") for v in vis], "report_refs": refs,
                              "pages": sum(1 for pg in pages if pg["id"] != "__report__")})
                added.append(name)
            self.store.save_snapshot(env_id, "usage_reports", {"reports": store})
            self.store.audit(actor, "usage.reports.add", env_id, {"reports": added})
            out = self.usage(env_id)
            out["added"] = added
            return out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def remove_usage_report(self, env_id: str, name: str, actor: str) -> dict:
        store = (self.store.snapshot(env_id, "usage_reports") or {}).get("body", {}).get("reports", [])
        self.store.save_snapshot(env_id, "usage_reports", {"reports": [r for r in store if r["name"] != name]})
        self.store.audit(actor, "usage.reports.remove", env_id, {"report": name})
        return self.usage(env_id)

    def set_vpax(self, env_id: str, data: Optional[bytes], filename: str, actor: str) -> dict:
        from .analyzers.vpax import parse_vpax, to_json
        if data is None:
            self.store.save_snapshot(env_id, "usage_vpax", None)
        else:
            st = to_json(parse_vpax(data, filename))
            st["uploaded_at"] = now_iso()
            self.store.save_snapshot(env_id, "usage_vpax", st)
        self.store.audit(actor, "usage.vpax", env_id, {"file": filename, "removed": data is None})
        return self.usage(env_id)

    def best_practices(self, env_id: str) -> dict:
        from .analyzers.bpa import run_rules
        model = self.model(env_id)
        if model is None:
            return {"available": False, "reason": "Upload a PBIP first."}
        vp = (self.store.snapshot(env_id, "usage_vpax") or {}).get("body")
        if vp:
            vp = {**vp, "columns": {tuple(k.split("|", 1)): v for k, v in vp.get("columns", {}).items()}}
        return {"available": True, **run_rules(model, self.usage(env_id), self.pbip_root(env_id), vp)}

    def cleanup(self, env_id: str, opts: dict, actor: str, want_zip: bool = False) -> dict:
        from .analyzers.cleanup import clean_pbip
        model, root = self.model(env_id), self.pbip_root(env_id)
        if model is None or root is None:
            raise ValueError("The PBIP files of this project are not available - upload the PBIP again.")
        r = clean_pbip(root, self.usage(env_id), model, measures=opts.get("measures", True),
                       calc_columns=opts.get("calc_columns", True), data_columns=opts.get("data_columns", True))
        if want_zip:
            self.store.audit(actor, "usage.cleanup.download", env_id, r.get("counts", {}))
            return r
        r.pop("zip", None)
        return r

    def usage_csv(self, env_id: str) -> str:
        import csv
        import io
        u = self.usage(env_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Status", "Type", "Table", "Name", "Reference", "Hidden", "Used by", "Expression"])
        label = {"report": "Used in report", "indirect": "Used indirectly", "structure": "Needed by model", "unused": "Unused"}
        for o in u.get("objects", []):
            w.writerow([label[o["status"]], o["type"], o["table"], o["name"], o["ref"], "yes" if o["hidden"] else "",
                        " | ".join(o["used_by"]), o["expression"].replace("\n", " ")[:2000]])
        return "\ufeff" + buf.getvalue()

    def dax_analysis(self, env_id: str) -> dict:
        model = self.model(env_id)
        if not model:
            return {"available": False, "reason": "Run environment discovery first."}
        kpis = self.kpis(env_id)
        kmap = {k.id: k for k in kpis}
        analyses = analyze_model(model, self.env(env_id), kpis)
        from .analyzers.dax_infer import infer_kpi
        from .analyzers.dax import DaxAnalyzer
        from .core.scope import in_scope
        _an = DaxAnalyzer(model)
        scope_ids = {k.id for k in in_scope(kpis, model, self.scope(env_id))} if self.env(env_id).project else {k.id for k in kpis}
        from .analyzers.usage import unused_measures
        dead = unused_measures(model)
        rows = []
        for a in analyses:
            d = a.to_dict()
            mm = model.measure(a.measure)
            inf = infer_kpi(mm, a.expanded, "tmp", model,
                            marked=_an.expand(mm.expression, (mm.name.lower(),), mark=True)[0]) if mm else None
            if inf:
                bad = [n for n in inf.translation_notes if n.startswith("Not auto")]
                d["dax_logic"] = describe(inf) if not bad else "Not auto-translatable: " + bad[0].split(": ", 1)[-1].split(". Provide")[0]
            k = kmap.get(a.kpi_id or "")
            if k:
                d.update({"kpi_name": k.name, "business_definition": k.business_definition, "basis": k.basis,
                          "approval_status": k.approval_status, "logic": describe(k), "translation_notes": k.translation_notes,
                          "kind": k.kind, "unit": k.unit, "expected_sql": k.expected_sql})
            used_by = [m.name for m in model.all_measures() if m.name != a.measure and f"[{a.measure.lower()}]" in m.expression.lower()]
            d["referenced_by"] = used_by
            d["visuals"] = [v.title or v.id for v in model.visuals if f"[{a.measure}]".lower() in (x.lower() for x in v.measures)]
            d["in_scope"] = a.kpi_id in scope_ids
            d["unused"] = a.measure.lower() in dead
            rows.append(d)
        return {"available": True, "measures": rows,
                "kpis_without_measure": [{"kpi_id": k.id, "measure": k.bi.measure} for k in kpi_measure_missing(model, kpis)]}

    # ----------------------------------------------------------- tests
    def generate(self, env_id: str, actor: str) -> list[dict]:
        env = self.env(env_id)
        model = self.model(env_id) or ModelMetadata(platform=env.bi.platform, model_name="(not discovered)")
        kpis = self.kpis(env_id)
        if env.project and model.visuals:                    # implicit aggregations on visuals become KPIs too
            from .core.scope import implicit_kpis
            have = {k.bi.measure for k in kpis if k.bi}
            for k in implicit_kpis(model):
                if k.bi.measure not in have:
                    self.store.save_kpi(env_id, k.model_dump(mode="json"))
            kpis = self.kpis(env_id)
        analyses = analyze_model(model, env, kpis)
        tests = generate_tests(env, kpis, model, analyses, load_requirements(env),
                               scope=self.scope(env_id) if env.project else None)
        keep = {t.id for t in tests}
        for old in self.store.tests(env_id):               # tests now out of scope are removed
            if old["id"] not in keep and old.get("generated_by") != "user":
                self.store.delete_test(env_id, old["id"])
        self.store.upsert_tests(env_id, [t.model_dump(mode="json") for t in tests])
        self.store.audit(actor, "tests.generate", env_id, {"count": len(tests)})
        return self.store.tests(env_id)

    def update_test(self, env_id: str, test_id: str, patch: dict, actor: str) -> dict:
        t = self.store.test(env_id, test_id)
        if not t:
            raise KeyError(test_id)
        if "approval_status" in patch:
            st = patch["approval_status"]
            if st not in ("APPROVED", "REJECTED", "PENDING"):
                raise ValueError("approval_status must be APPROVED, REJECTED or PENDING")
            t["approval_status"], t["approved_by"] = st, (None if st == "PENDING" else actor)
        if "filter_value" in patch:   # set a slicer/filter value on a CTX test and refresh its scripts
            fld, val = patch.pop("filter_value")
            for f in t["filter_context"]:
                if f["field"] == fld:
                    f["value"] = val
            k = {x.id: x for x in self.kpis(env_id)}.get(t.get("kpi_id"))
            if k:
                from .engine.test_generator import _scripts
                t["params"].update(_scripts(k, self.env(env_id), [FilterSpec(**f) for f in t["filter_context"]], t.get("grain")))
            t["name"] = re.sub(r"= .*$", f"= {val}", t["name"])
        editable = {"name", "business_objective", "steps", "expected_result", "preconditions", "tolerance"}
        edits = {k: v for k, v in patch.items() if k in editable}
        if edits:
            t.update(edits)
            t["generated_by"], t["approval_status"] = "user", "PENDING"
        TestCase(**t)  # validate
        self.store.save_test(env_id, t)
        self.store.audit(actor, "test.update", f"{env_id}/{test_id}", patch)
        return t

    def run(self, env_id: str, actor: str, test_ids: Optional[list[str]] = None, approved_only: bool = False) -> dict:
        env = self.env(env_id)
        model = self.model(env_id)
        kpis = self.kpis(env_id)
        analyses = {a.measure.lower(): a for a in analyze_model(model, env, kpis)} if model else {}
        bi = data = None
        try:
            bi = self.connector(env, "bi")
        except Exception:  # noqa: BLE001
            pass
        data_error = None
        try:
            data = self.connector(env, "data")
            if hasattr(data, "connection"):          # SQL databases: connect once up front, reuse for every test
                data.connection()
        except Exception as e:  # noqa: BLE001
            data, data_error = None, str(e).splitlines()[0][:200]
        ctx = ExecContext(env=env, bi=bi, data=data, kpis={k.id: k for k in kpis}, model=model, analyses=analyses,
                          data_error=data_error)
        tests = [TestCase(**t) for t in self.store.tests(env_id)]
        if test_ids:
            tests = [t for t in tests if t.id in test_ids]
        if approved_only:
            tests = [t for t in tests if t.approval_status == "APPROVED"]
        tests = [t for t in tests if t.approval_status != "REJECTED"]
        run_id = new_run_id()
        self.store.start_run(run_id, env_id, ctx.mode.value, actor)
        results: list[TestResult] = []
        for t in tests:
            r = execute_test(ctx, t, run_id)
            results.append(r)
            self.store.save_result(r.model_dump(mode="json"))
            self._defect_sync(env_id, t, r)
        summary = summarize(results)
        summary["mode"] = ctx.mode.value
        self.store.finish_run(run_id, summary)
        self.store.audit(actor, "run.execute", env_id, {"run_id": run_id, **summary})
        return {"run_id": run_id, "summary": summary}

    def _defect_sync(self, env_id: str, t: TestCase, r: TestResult):
        existing = self.store.open_defect_for(env_id, t.id)
        if r.status.value in ("FAIL", "ERROR"):
            d = existing or {"defect_id": f"DEF-{t.id}-{r.run_id[-4:]}", "env_id": env_id, "test_id": t.id,
                             "title": f"{t.name}", "status": "Open", "owner": None, "priority": None, "comments": []}
            d.update({"run_id": r.run_id, "severity": r.severity, "expected": r.expected, "actual": r.actual,
                      "difference": r.difference, "message": r.message, "root_cause_hypothesis": r.root_cause_hypothesis,
                      "remediation_suggestion": r.remediation_suggestion, "mode": r.mode.value, "last_seen": r.executed_at})
            self.store.upsert_defect(d)
        elif existing and r.status.value == "PASS":
            existing.update({"status": "Retest Passed", "run_id": r.run_id})
            self.store.upsert_defect(existing)

    # ----------------------------------------------------------- SQL scripts
    def sql_scripts(self, env_id: str) -> str:
        env = self.env(env_id)
        tests = self.store.tests(env_id)
        if tests:
            out = [f"-- ============================================================\n-- Validation SQL - {env.name}\n"
                   f"-- Target dialect: {env.data.platform} (read-only). Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
                   f"-- Expected values only - never reads from the BI model.\n-- ============================================================\n"]
            for t in tests:
                if t.get("params", {}).get("sql"):
                    out.append(f"\n-- ### {t['id']} | {t['name']}\n{t['params']['sql'].rstrip().rstrip(';')};\n")
            return "".join(out)
        parts = [f"-- ============================================================\n"
                 f"-- Independent validation SQL - {env.name}\n-- Target dialect: Databricks SQL (read-only)\n"
                 f"-- Generated from APPROVED KPI definitions; never reads from the BI model.\n"
                 f"-- ============================================================\n"]
        for k in self.kpis(env_id):
            ok, why = k.is_independently_defined()
            if not ok:
                parts.append(f"\n-- {k.id} {k.name}: NOT GENERATED - {why}\n")
                continue
            ctx = env.default_context.filters_for(k)
            try:
                parts.append("\n" + kpi_value_sql(k, env, ctx).render_inline() + ";\n")
                for g in k.grains:
                    parts.append("\n" + kpi_value_sql(k, env, ctx, grain=g).render_inline() + ";\n")
            except MappingError as e:
                parts.append(f"\n-- {k.id}: NOT GENERATED - {e}\n")
        parts.append("\n-- ---------------- Data quality ----------------\n")
        for fname, fm in env.mapping.fields.items():
            if fm.source and fname.endswith("_id"):
                parts.append("\n" + dq_null_sql(env, fname).render_inline() + ";\n")
        for tname, t in env.mapping.tables.items():
            if t.primary_key:
                parts.append("\n" + dq_duplicate_sql(env, tname).render_inline() + ";\n")
        for i, _ in enumerate(env.mapping.joins):
            parts.append("\n" + dq_ri_sql(env, i).render_inline() + ";\n")
        return "".join(parts)

    # ----------------------------------------------------------- overview
    def overview(self, env_id: str) -> dict:
        runs = [r for r in self.store.runs(env_id, 60) if not (r["summary"] or {}).get("manual")]
        last = runs[0] if runs else None
        tests = {t["id"]: t for t in self.store.tests(env_id)}
        latest = self.latest_results(env_id)
        results = [r for tid, r in latest.items() if tid in tests]
        cats: dict = {}
        for r in results:
            c = tests.get(r["test_id"], {}).get("category", "Other")
            d = cats.setdefault(c, {"PASS": 0, "FAIL": 0, "other": 0})
            d[r["status"] if r["status"] in ("PASS", "FAIL") else "other"] += 1
        trend = [{"run_id": r["run_id"], "at": r["finished_at"] or r["started_at"], "mode": r["mode"],
                  "rate": (r["summary"] or {}).get("validated_pass_rate"),
                  "coverage": (r["summary"] or {}).get("coverage")} for r in reversed(runs) if r["summary"]]
        issues = [r for r in results if r["status"] in ("FAIL", "ERROR")]
        sev = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, None: 4}
        issues.sort(key=lambda r: sev.get(r.get("severity"), 4))
        counts = {st.value: 0 for st in Status}
        for t in tests:
            counts[latest[t]["status"] if t in latest else "NOT EXECUTED"] += 1
        executed = counts["PASS"] + counts["FAIL"]
        dax = self.dax_analysis(env_id)
        dm = dax.get("measures", [])
        kpi_rows = []
        for k in self.kpis(env_id):
            ks = [latest[t]["status"] for t, tc in tests.items() if tc.get("kpi_id") == k.id and t in latest and tc["category"] != "Expression"]
            d = next((m for m in dm if m.get("kpi_id") == k.id), None)
            kpi_rows.append({"id": k.id, "name": k.name, "basis": k.basis, "dax": d["overall_status"] if d else None,
                             "data": "FAIL" if any(x in ("FAIL", "ERROR") for x in ks) else "PASS" if ks and all(x == "PASS" for x in ks)
                             else ("PARTIAL" if "PASS" in ks else (ks[0] if ks else None))})
        return {"current": {"counts": counts, "total": len(tests), "validated_pass_rate": round(counts["PASS"] / executed * 100, 1) if executed else None,
                            "coverage": round(executed / len(tests) * 100, 1) if tests else 0},
                "dax": {"total": len(dm), "pass": sum(1 for m in dm if m["overall_status"] == "PASS"),
                        "fail": sum(1 for m in dm if m["overall_status"] == "FAIL"),
                        "verified": sum(1 for m in dm if m["definition_status"] == "PASS"),
                        "warnings": sum(m["warnings"] for m in dm)},
                "kpis": kpi_rows, "latest": latest,
                "last_run": last, "categories": cats, "trend": trend,
                "recent_issues": [{"test_id": r["test_id"], "name": tests.get(r["test_id"], {}).get("name"), "display_id": tests.get(r["test_id"], {}).get("display_id"),
                                   "message": r["message"], "severity": r.get("severity"), "status": r["status"]}
                                  for r in issues[:6]],
                "test_count": len(tests), "connections": self.store.connections(env_id),
                "approved_tests": sum(1 for t in tests.values() if t["approval_status"] == "APPROVED")}
