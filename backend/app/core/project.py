"""Build a generic project (environment + mapping + draft KPI registry) from an uploaded PBIP.

Nothing here is specific to a customer, KPI or dataset: physical tables come from the
partition sources, field mappings from sourceColumn, joins from model relationships, and
date-table columns are resolved through their relationship to each fact table.
"""
from __future__ import annotations

import io
import re
import shutil
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from ..analyzers.dax import DaxAnalyzer
from ..analyzers.dax_infer import fkey, infer_kpi
from .environment import (PROJECTS_DIR, DefaultContext, EnvironmentConfig, FieldMapping, JoinMapping, Mapping,
                          PlatformBinding, TableMapping)
from .models import KPIDefinition, ModelMetadata, RunMode, is_slicer

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_EXT = {".tmdl", ".json", ".pbir", ".pbism", ".pbip", ".platform", ".md", ".txt", ".csv", ".tsv", ".xlsx", ".xlsm", ".parquet", ".bim", ".gitignore", ""}

SOURCE_TO_PLATFORM = {"databricks": "databricks", "snowflake": "snowflake", "sqlserver": "sqlserver", "directlake": "fabric",
                      "fabric": "fabric", "bigquery": "bigquery", "redshift": "redshift", "oracle": "oracle",
                      "postgres": "postgres", "mysql": "mysql", "teradata": "teradata", "db2": "db2",
                      "excel": "files", "csv": "files", "parquet": "files", "json": "files"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"


def safe_extract(files: Iterable[tuple[str, bytes]], dest: Path) -> int:
    """Write uploaded files (or zip members) under dest, refusing path traversal and oversize uploads."""
    dest.mkdir(parents=True, exist_ok=True)
    total, count = 0, 0
    root = dest.resolve()

    def write(rel: str, data: bytes):
        nonlocal total, count
        rel = rel.replace("\\", "/").lstrip("/")
        if not rel or rel.endswith("/"):
            return
        if Path(rel).suffix.lower() not in ALLOWED_EXT:
            return
        target = (dest / rel).resolve()
        if root not in target.parents:
            raise ValueError(f"Refused path outside project folder: {rel}")
        total += len(data)
        if total > MAX_UPLOAD_BYTES:
            raise ValueError("Upload exceeds 200 MB")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        count += 1

    for name, data in files:
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    if info.file_size > MAX_UPLOAD_BYTES:
                        raise ValueError("Zip member too large")
                    if not info.is_dir():
                        write(info.filename, z.read(info))
        else:
            write(name, data)
    return count


_CALC_COL_RULES = [
    (r'^FORMAT\(\s*\[(\w+)\]\s*,\s*"([^"]+)"\s*\)$', lambda m: f"date_format({{k}}, '{m.group(2)}')"),
    (r"^YEAR\(\s*\[(\w+)\]\s*\)$", lambda m: "year({k})"),
    (r"^MONTH\(\s*\[(\w+)\]\s*\)$", lambda m: "month({k})"),
    (r"^QUARTER\(\s*\[(\w+)\]\s*\)$", lambda m: "quarter({k})"),
    (r"^DAY\(\s*\[(\w+)\]\s*\)$", lambda m: "day({k})"),
    (r"^WEEKNUM\(\s*\[(\w+)\]\s*\)$", lambda m: "weekofyear({k})"),
]


def _calc_to_key_expr(expr: str) -> Optional[str]:
    e = (expr or "").strip()
    for pat, fn in _CALC_COL_RULES:
        m = re.match(pat, e, re.I)
        if m:
            return fn(m)
    return None


def build_mapping(model: ModelMetadata) -> tuple[Mapping, list[str]]:
    notes: list[str] = []
    unmapped: list[str] = []
    mp = Mapping()
    tables = {t.name: t for t in model.tables}
    pending_calc: list = []
    for t in model.tables:
        virtual = t.source_kind == "calculated"
        mp.tables[t.name] = TableMapping(source=None if virtual else t.source_ref, model=t.name, calculated=virtual,
                                         file=t.file_source if t.file_source and t.file_source.get("file") else None)
        if t.source_kind == "calculation_group":
            continue
        if t.file_source and t.file_source.get("format") == "folder":
            notes.append(f"{t.name}: Power Query combines every file in a folder - combine them into one file and map it, or test manually.")
        if t.file_source and t.file_source.get("format") == "excel" and t.file_source.get("file", "").lower().endswith((".xls", ".xlsb")):
            notes.append(f"{t.name}: {t.file_source['file']} is an old .xls/.xlsb workbook - save it as .xlsx so the tool can read it.")
        if t.file_source and t.file_source.get("transforms"):
            notes.append(f"{t.name}: Power Query also applies {', '.join(t.file_source['transforms'])} to {t.file_source.get('file')} - "
                         "the expected values read the file as-is, so differences may come from those steps.")
        if not virtual and not t.source_ref:
            unmapped.append(f"{t.name} ({t.source_kind or 'no partition'})")
        for c in t.columns:
            model_ref = "'" + t.name.replace("'", "''") + "'[" + c.name.replace("]", "]]") + "]"
            mt = (c.data_type or "").lower()
            dt = ("date" if mt in ("datetime", "date", "datetimezone") else "boolean" if mt == "boolean" else
                  "integer" if mt == "int64" else "number" if mt in ("double", "decimal") else None)
            fm = FieldMapping(table=t.name, model=model_ref, data_type=dt)
            if virtual:
                if c.expression:
                    fm.key_expr = _calc_to_key_expr(c.expression)
                    if not fm.key_expr:
                        notes.append(f"{model_ref}: calculated column not translatable to SQL ({c.expression[:40]}).")
                else:
                    fm.key_expr = "{k}"
            elif c.expression:
                pending_calc.append((t, c, fm, model_ref))
            elif c.pq_sql:                               # Power Query formula translated to SQL
                fm.source, fm.source_expr = c.physical_column or c.source_column or c.name, c.pq_sql
            elif c.pq_computed:
                notes.append(f"{model_ref}: added in Power Query - no source column (map it manually if needed).")
            else:
                fm.source = c.physical_column or c.source_column or c.name
            mp.fields[fkey(t.name, c.name)] = fm
    # calculated columns on imported tables: translate the simple shapes, flag the rest
    for _ in range(4):                                    # calculated columns may build on each other
        left = []
        for item in pending_calc:
            t, c, fm, model_ref = item
            how = _translate_calc_column(t, c, mp, tables)
            if how:
                mp.fields[fkey(t.name, c.name)] = how
            else:
                left.append(item)
        if len(left) == len(pending_calc):
            break
        pending_calc = left
    for t, c, fm, model_ref in pending_calc:
        notes.append(f"{model_ref}: calculated column - no source mapping (tests using it will be BLOCKED).")
    for r in model.relationships:
        ft, tt = tables.get(r.from_table), tables.get(r.to_table)
        if not ft or not tt or not r.active:
            continue
        fc = next((c for c in ft.columns if c.name == r.from_column), None)
        tc = next((c for c in tt.columns if c.name == r.to_column), None)
        if not fc or not tc:
            continue
        if mp.tables[r.from_table].calculated and not mp.tables[r.to_table].calculated:
            continue  # calculated -> real joins can't be expressed in source SQL
        lk = fc.physical_column or fc.source_column or fc.name
        rk = tc.physical_column or tc.source_column or tc.name
        mp.joins.append(JoinMapping(left=r.from_table, right=r.to_table, left_key=lk, right_key=rk))
        if mp.tables[r.to_table].source and not mp.tables[r.to_table].primary_key:
            mp.tables[r.to_table].primary_key = rk
    if unmapped:
        notes.insert(0, f"{len(unmapped)} table(s) need a physical source (set on this page): " + ", ".join(unmapped[:12])
                     + (" …" if len(unmapped) > 12 else ""))
    return mp, notes


_REL = re.compile(r"^\s*RELATED\s*\(\s*(?:'((?:[^']|'')+)'|([A-Za-z_]\w*))\s*\[((?:[^\]]|\]\])+)\]\s*\)\s*$", re.I)


def _translate_calc_column(t, c, mp: Mapping, tables: dict) -> Optional[FieldMapping]:
    """RELATED('Dim'[Col]) -> the related table's column (joined along the relationship);
    same-row arithmetic over this table's columns -> a SQL expression on the source table."""
    expr = re.sub(r"//[^\n]*|--[^\n]*", "", c.expression or "").strip()
    m = _REL.match(expr)
    if m:
        dim, col = (m.group(1) or m.group(2)).replace("''", "'"), m.group(3).replace("]]", "]")
        target = mp.fields.get(fkey(dim, col))
        if target and (target.source or target.source_expr):
            return target.model_copy(update={"model": "'" + t.name.replace("'", "''") + "'[" + c.name.replace("]", "]]") + "]",
                                             "sample_values": list(target.sample_values)})
        return None
    from ..engine.dax_row_sql import dax_row_to_sql

    def col(tbl, name):
        if tbl and tbl != t.name:
            return None
        f = mp.fields.get(fkey(t.name, name))
        if f is None or f.table != t.name:
            return None
        if f.source_expr:
            return "(" + f.source_expr + ")"
        return "{t}.`" + f.source.replace("`", "``") + "`" if f.source else None

    sql = dax_row_to_sql(expr, col)
    if not sql:
        return None
    mt = (c.data_type or "").lower()
    return FieldMapping(table=t.name, model="'" + t.name.replace("'", "''") + "'[" + c.name.replace("]", "]]") + "]",
                        source=c.name, source_expr=sql,
                        data_type="date" if mt in ("datetime", "date") else "integer" if mt == "int64" else
                        "number" if mt in ("double", "decimal") else None)


def infer_kpis(model: ModelMetadata) -> list[KPIDefinition]:
    an = DaxAnalyzer(model)
    out = []
    for i, m in enumerate(model.all_measures(), 1):
        expanded, _ = an.expand(m.expression, (m.name.lower(),))
        marked, _ = an.expand(m.expression, (m.name.lower(),), mark=True)
        k = infer_kpi(m, expanded, f"KPI-{i:03d}", model, marked=marked)
        # grains from report visuals that plot this measure
        for v in model.visuals:
            if f"[{m.name}]".lower() in (x.lower() for x in v.measures) and not is_slicer(v.visual_type):
                for d in v.dimensions[:1]:
                    tbl, _, col = d.partition("[")
                    tbl, col = tbl.strip("'"), col.rstrip("]")
                    if not any(t.name == tbl and any(c.name == col for c in t.columns) for t in model.tables):
                        continue
                    key = fkey(tbl, col)
                    if key not in k.grains:
                        k.grains.append(key)
        out.append(k)
    from .scope import implicit_kpis
    return out + implicit_kpis(model)


def create_project(model: ModelMetadata, pbip_dir: Path, project_id: Optional[str] = None,
                   name: Optional[str] = None) -> tuple[EnvironmentConfig, list[str]]:
    mapping, notes = build_mapping(model)
    kinds = Counter(t.source_kind for t in model.tables if t.source_kind in SOURCE_TO_PLATFORM)
    detected = kinds.most_common(1)[0][0] if kinds else None
    data_platform = SOURCE_TO_PLATFORM.get(detected or "", "databricks")
    pid = project_id or f"{slug(model.model_name)}-{uuid.uuid4().hex[:4]}"
    has_date = any(t.is_date_table or t.source_kind == "calculated" for t in model.tables)
    env = EnvironmentConfig(
        id=pid, name=name or model.model_name, description=f"Created from uploaded PBIP '{model.model_name}'",
        mode=RunMode.LIVE, project=True, source_platform_detected=detected,
        bi=PlatformBinding(platform="powerbi", config={"pbip_path": str(pbip_dir)}),
        data=PlatformBinding(platform=data_platform, config={}),
        mapping=mapping,
        default_context=DefaultContext(label="As shown on the dashboard"))
    return env, notes


def project_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def delete_project(pid: str) -> None:
    d = project_dir(pid)
    if d.exists() and PROJECTS_DIR.resolve() in d.resolve().parents:
        shutil.rmtree(d)
