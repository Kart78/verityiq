"""Produce a cleaned copy of a PBIP with unused objects removed (Measure Killer's "delete unused" / "Clean TMDL"
plus its "M code to remove unused columns"), and verify the copy before it is offered.

What is removed (each switchable):
  measures             unused and "used only by unused" measures                  -> deleted from TMDL / model.bim
  calculated columns   unused calculated columns                                   -> deleted
  data columns         unused imported columns                                     -> deleted, and a Table.RemoveColumns step is
                                                                                       appended to the table's Power Query so the
                                                                                       column is no longer loaded at refresh
Objects marked "keep", relationship keys, hierarchy levels, sort-by targets and RLS columns are never touched.
Translations (cultures) and perspectives that mention a removed object are cleaned too.

Safety: the cleaned copy is re-read; it is rejected if any remaining measure, calculated column, calculated table,
RLS rule or report visual still references a removed object, or if the report gains broken fields.
The original upload is never modified.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from ..metadata.pbip import _Indent, _split_name_expr, _unquote, locate_pbip, read_pbip

STEP = '#"Removed unused columns (VerityIQ)"'
REMOVABLE = ("unused", "used_by_unused")


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def plan(usage: dict, model, measures: bool = True, calc_columns: bool = True, data_columns: bool = True) -> dict:
    """-> {table: {"measures": set, "calc_columns": set, "data_columns": {name: source_column}}}"""
    tables = {t.name: t for t in model.tables}
    out: dict[str, dict] = {}
    for o in usage.get("objects", []):
        if o["status"] not in REMOVABLE:
            continue
        t = out.setdefault(o["table"], {"measures": set(), "calc_columns": set(), "data_columns": {}})
        if o["type"] == "Measure" and measures:
            t["measures"].add(o["name"])
        elif o["type"] == "Calculated column" and calc_columns:
            t["calc_columns"].add(o["name"])
        elif o["type"] == "Column" and data_columns:
            tm = tables.get(o["table"])
            col = next((c for c in tm.columns if c.name == o["name"]), None) if tm else None
            if tm is not None and tm.source_kind not in ("calculated", "calculation_group") and col is not None:
                t["data_columns"][o["name"]] = col.source_column or o["name"]
    return {k: v for k, v in out.items() if v["measures"] or v["calc_columns"] or v["data_columns"]}


# --------------------------------------------------------------------------- Power Query step injection

def inject_remove_step(m_lines: list[str], source_cols: list[str]) -> Optional[list[str]]:
    """Append Table.RemoveColumns to a let ... in expression. Returns new lines, or None if the shape is not let/in."""
    idx_in = max((i for i, l in enumerate(m_lines) if l.strip() == "in"), default=None)
    if idx_in is None:                                      # one-line form: let A = ..., B = ... in B
        text = "\n".join(m_lines)
        mm = re.search(r'\bin\s+(#"(?:[^"]|"")+"|[A-Za-z_][\w.]*)\s*$', text)
        if not mm or not re.match(r"^\s*let\b", text) or STEP in text:
            return None
        lead = re.match(r"^\s*", m_lines[0]).group(0)
        body = text[:mm.start()].rstrip().rstrip(",")
        return body.splitlines()[:-1] + [body.splitlines()[-1] + ","] + [
            f"{lead}    {STEP} = Table.RemoveColumns({mm.group(1)}, {{{', '.join(_q(c) for c in source_cols)}}}, MissingField.Ignore)",
            f"{lead}in", f"{lead}    {STEP}"]
    nxt = next((i for i in range(idx_in + 1, len(m_lines)) if m_lines[i].strip()), None)
    prev = next((i for i in range(idx_in - 1, -1, -1) if m_lines[i].strip()), None)
    if nxt is None or prev is None:
        return None
    final = m_lines[nxt].strip()
    if final == STEP:                                       # already cleaned once: extend the list
        return None
    lead = re.match(r"^\s*", m_lines[prev]).group(0)
    out = list(m_lines)
    if not out[prev].rstrip().endswith(","):
        out[prev] = out[prev].rstrip() + ","
    step = f"{lead}{STEP} = Table.RemoveColumns({final}, {{{', '.join(_q(c) for c in source_cols)}}}, MissingField.Ignore)"
    out.insert(idx_in, step)
    nxt += 1
    out[nxt] = re.match(r"^\s*", m_lines[nxt - 1]).group(0) + STEP
    return out


def m_code_for(expression: str, source_cols: list[str]) -> Optional[str]:
    new = inject_remove_step(expression.splitlines(), source_cols)
    return "\n".join(new) if new else None


# --------------------------------------------------------------------------- TMDL editing

def _header(stripped: str) -> tuple[Optional[str], Optional[str]]:
    for kw in ("perspectiveMeasure", "perspectiveColumn", "perspectiveTable", "measure", "column", "table", "partition"):
        if stripped.startswith(kw + " ") or stripped == kw:
            return kw, _split_name_expr(stripped[len(kw):].strip())[0] if stripped != kw else ""
    return None, None


def edit_tmdl(text: str, removals: dict, changes: list, fname: str) -> str:
    """Remove measure/column (and perspective*) blocks named in removals for their table; inject M steps."""
    lines = text.splitlines()
    lvl = _Indent(lines)
    out: list[str] = []
    table: Optional[str] = None
    table_level = -1
    i = 0
    while i < len(lines):
        raw = lines[i]
        st = raw.strip()
        if not st:
            out.append(raw)
            i += 1
            continue
        level = lvl(raw)
        kw, name = _header(st)
        if kw in ("table", "perspectiveTable"):
            table, table_level = name, level
        elif table_level >= 0 and level <= table_level and kw not in ("table", "perspectiveTable"):
            table, table_level = None, -1
        rm = removals.get(table or "", {})
        drop = False
        if table and level == table_level + 1 and kw in ("measure", "perspectiveMeasure") and name in rm.get("measures", ()):
            drop = True
        if table and level == table_level + 1 and kw in ("column", "perspectiveColumn") and \
                (name in rm.get("calc_columns", ()) or name in rm.get("data_columns", {})):
            drop = True
        if drop:
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lvl(lines[j]) > level):
                j += 1
            # keep blank line separation tidy
            changes.append({"file": fname, "table": table, "object": name, "action": f"deleted {kw}"})
            i = j
            continue
        if kw == "partition" and table and rm.get("data_columns") and level == table_level + 1 and re.search(r"=\s*m\s*$", st):
            j = i + 1
            block = []
            while j < len(lines) and (not lines[j].strip() or lvl(lines[j]) > level):
                block.append(lines[j])
                j += 1
            src_idx = next((k for k, l in enumerate(block) if re.match(r"^\s*source\s*=", l)), None)
            new_block = None
            if src_idx is not None:
                body = block[src_idx + 1:]
                stop = next((k for k, l in enumerate(body) if l.strip() and lvl(l) <= lvl(block[src_idx])), len(body))
                expr, rest = body[:stop], body[stop:]
                cols = sorted(set(rm["data_columns"].values()))
                injected = inject_remove_step(expr, cols)
                if injected:
                    new_block = block[:src_idx + 1] + injected + rest
                    changes.append({"file": fname, "table": table, "object": ", ".join(cols),
                                    "action": "Power Query: appended Table.RemoveColumns"})
            if new_block is None:
                rm["_m_failed"] = True
            out.append(raw)
            out.extend(new_block if new_block is not None else block)
            i = j
            continue
        out.append(raw)
        i += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _edit_bim(path: Path, removals: dict, changes: list):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    m = data.get("model", data)
    for t in m.get("tables", []):
        rm = removals.get(t.get("name"))
        if not rm:
            continue
        before = {x["name"] for x in t.get("measures", [])}
        t["measures"] = [x for x in t.get("measures", []) if x["name"] not in rm["measures"]]
        for n in before - {x["name"] for x in t["measures"]}:
            changes.append({"file": path.name, "table": t["name"], "object": n, "action": "deleted measure"})
        cols = set(rm["calc_columns"]) | set(rm["data_columns"])
        if rm["data_columns"]:
            ok = False
            for p in t.get("partitions", []):
                src = p.get("source", {})
                if src.get("type", "m") != "m":
                    continue
                ex = src.get("expression")
                lines = ex if isinstance(ex, list) else str(ex or "").splitlines()
                new = inject_remove_step(lines, sorted(set(rm["data_columns"].values())))
                if new:
                    src["expression"] = new if isinstance(ex, list) else "\n".join(new)
                    ok = True
                    changes.append({"file": path.name, "table": t["name"], "object": ", ".join(sorted(rm["data_columns"].values())),
                                    "action": "Power Query: appended Table.RemoveColumns"})
            if not ok:
                rm["_m_failed"] = True
                cols -= set(rm["data_columns"])
        before = {x["name"] for x in t.get("columns", [])}
        t["columns"] = [x for x in t.get("columns", []) if x["name"] not in cols]
        for n in before - {x["name"] for x in t["columns"]}:
            changes.append({"file": path.name, "table": t["name"], "object": n, "action": "deleted column"})
    for persp in m.get("perspectives", []):
        for pt in persp.get("tables", []):
            rm = removals.get(pt.get("name"))
            if rm:
                pt["measures"] = [x for x in pt.get("measures", []) if x.get("name") not in rm["measures"]]
                pt["columns"] = [x for x in pt.get("columns", []) if x.get("name") not in set(rm["calc_columns"]) | set(rm["data_columns"])]
    for cul in m.get("cultures", []):
        for ct in ((cul.get("translations") or {}).get("model") or {}).get("tables", []):
            rm = removals.get(ct.get("name"))
            if rm:
                ct["measures"] = [x for x in ct.get("measures", []) if x.get("name") not in rm["measures"]]
                ct["columns"] = [x for x in ct.get("columns", []) if x.get("name") not in set(rm["calc_columns"]) | set(rm["data_columns"])]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- verification

def _verify(clean_root: Path, removed: dict, before_unresolved: int) -> list[str]:
    from .usage import Usage
    problems = []
    model = read_pbip(clean_root)
    gone_m = {n.lower() for t in removed.values() for n in t["measures"]}
    gone_c = {(tn.lower(), n.lower()) for tn, t in removed.items() for n in list(t["calc_columns"]) + list(t["data_columns"])}
    still_m = {m.name.lower() for t in model.tables for m in t.measures}
    still_c = {(t.name.lower(), c.name.lower()) for t in model.tables for c in t.columns}
    if gone_m & still_m:
        problems.append(f"Measures still present after deletion: {sorted(gone_m & still_m)[:5]}")
    if gone_c & still_c:
        problems.append(f"Columns still present after deletion: {sorted(gone_c & still_c)[:5]}")
    ref = re.compile(r"(?:'((?:[^']|'')+)'|\b([A-Za-z_]\w*))?\s*\[((?:[^\]]|\]\])+)\]")
    from .usage import _strip
    exprs = [(f"[{m.name}]", m.expression, t.name) for t in model.tables for m in t.measures]
    exprs += [(f"{t.name}[{c.name}]", c.expression, t.name) for t in model.tables for c in t.columns if c.expression]
    exprs += [(f"table {t.name}", t.source, None) for t in model.tables if t.source_kind == "calculated"]
    exprs += [(f"role {r['role']}", r["expression"], r["table"]) for r in model.roles]
    for who, e, home in exprs:
        for q, bare, name in ref.findall(_strip(e or "")):
            tbl, nm = (q or bare).replace("''", "'").lower(), name.replace("]]", "]").lower()
            if (tbl and (tbl, nm) in gone_c) or (not tbl and nm in gone_m and nm not in still_m) or \
                    (not tbl and home and (home.lower(), nm) in gone_c):
                problems.append(f"{who} still references removed {name}")
    u = Usage(model).run()
    if len(u["unresolved_report_refs"]) > before_unresolved:
        problems.append(f"The report would get {len(u['unresolved_report_refs']) - before_unresolved} broken field(s): "
                        + "; ".join(u["unresolved_report_refs"][:3]))
    return problems[:20]


# --------------------------------------------------------------------------- make the copy openable in Power BI Desktop

PBIP_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json"
PBISM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json"
PBIR_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/1.0.0/schema.json"


def _write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _rel(target: Path, start: Path) -> str:
    import os
    return os.path.relpath(target, start).replace("\\", "/")


def _minimal_report(rep: Path, sm: Path):
    rep.mkdir(parents=True, exist_ok=True)
    _write_json(rep / "definition.pbir", {"$schema": PBIR_SCHEMA, "version": "4.0",
                                          "datasetReference": {"byPath": {"path": _rel(sm, rep)}}})
    _write_json(rep / "report.json", {"config": json.dumps({"version": "5.43", "themeCollection": {}, "activeSectionIndex": 0}),
                                      "layoutOptimization": 0, "resourcePackages": [],
                                      "sections": [{"config": "{}", "displayName": "Page 1", "displayOption": 1, "filters": "[]",
                                                    "height": 720.0, "name": "ReportSection", "visualContainers": [], "width": 1280.0}]})


def ensure_openable(root: Path, name: str):
    """Add whatever Power BI Desktop needs to open the copy: <name>.pbip, definition.pbism, a report bound
    to this semantic model (definition.pbir byPath) and the .SemanticModel/.Report folder names."""
    fixes: list[str] = []
    safe = re.sub(r'[<>:"/\\|?*]+', "_", name).strip() or "Model"
    defn, _ = locate_pbip(root)
    if defn is None:
        return fixes, None
    sm = defn.parent if defn.is_file() or defn.name == "definition" else defn
    if sm == root or not sm.name.endswith((".SemanticModel", ".Dataset")):
        new = root / f"{safe}.SemanticModel"
        new.mkdir(exist_ok=True)
        if defn.is_file():
            shutil.move(str(defn), str(new / "model.bim"))
        else:
            shutil.move(str(defn), str(new / "definition"))
        sm = new
        fixes.append(f"put the semantic model in {new.name}")
    if not (sm / "definition.pbism").exists():
        _write_json(sm / "definition.pbism", {"$schema": PBISM_SCHEMA, "version": "4.0" if (sm / "definition").is_dir() else "1.0",
                                              "settings": {}})
        fixes.append("added definition.pbism")
    reports = sorted({p for p in root.rglob("*") if p.is_dir() and p.name.endswith(".Report")}, key=lambda p: len(p.parts))
    rep = None
    for r in reports:
        pbir = r / "definition.pbir"
        if not pbir.exists():
            if (r / "report.json").exists() or (r / "definition").is_dir():
                _write_json(pbir, {"$schema": PBIR_SCHEMA, "version": "4.0", "datasetReference": {"byPath": {"path": _rel(sm, r)}}})
                fixes.append(f"added {r.name}/definition.pbir")
                rep = rep or r
            continue
        try:
            ref = json.loads(pbir.read_text(encoding="utf-8-sig")).get("datasetReference") or {}
        except ValueError:
            ref = {}
        by = (ref.get("byPath") or {}).get("path")
        if by and (r / by).resolve() == sm.resolve():
            rep = rep or r
        elif by and not (r / by).exists():          # dangling path (folder renamed/not uploaded): bind to this model
            data = json.loads(pbir.read_text(encoding="utf-8-sig"))
            data["datasetReference"] = {"byPath": {"path": _rel(sm, r)}}
            _write_json(pbir, data)
            fixes.append(f"pointed {r.name} at {sm.name}")
            rep = rep or r
    if rep is None:                     # no report, or only live-connected ones: add a blank report bound to this model
        rep = sm.parent / f"{safe}.Report"
        if rep.exists():
            rep = sm.parent / f"{safe} (cleaned model).Report"
        _minimal_report(rep, sm)
        fixes.append(f"added a blank report {rep.name} bound to the cleaned model"
                     + (" (the original report uses a live connection to the service)" if reports else ""))
    def _target(f: Path):
        try:
            arts = json.loads(f.read_text(encoding="utf-8-sig")).get("artifacts") or [{}]
            return ((arts[0] or {}).get("report") or {}).get("path")
        except (ValueError, OSError, AttributeError):
            return None
    pbip = next((f for f in sorted(rep.parent.glob("*.pbip")) if _target(f) == rep.name), None)
    if pbip is None:
        pbip = rep.parent / f"{rep.name[:-len('.Report')]}.pbip"
        _write_json(pbip, {"$schema": PBIP_SCHEMA, "version": "1.0", "artifacts": [{"report": {"path": rep.name}}],
                           "settings": {"enableAutoRecovery": True}})
        fixes.append(f"added {pbip.name}")
    return fixes, pbip.relative_to(root).as_posix()


# --------------------------------------------------------------------------- entry point

def clean_pbip(pbip_root: Path, usage: dict, model, measures: bool = True, calc_columns: bool = True,
               data_columns: bool = True) -> dict:
    """-> {"ok", "zip": bytes|None, "changes": [...], "skipped": [...], "problems": [...], "m_code": {table: code}}"""
    removals = plan(usage, model, measures, calc_columns, data_columns)
    changes: list[dict] = []
    tmp = Path(tempfile.mkdtemp(prefix="verityiq-clean-"))
    root = tmp / "pbip"
    try:
        shutil.copytree(pbip_root, root, ignore=shutil.ignore_patterns(".cache.duckdb", "__pycache__"))
        defn, _ = locate_pbip(root)
        if defn is None:
            return {"ok": False, "zip": None, "changes": [], "skipped": [], "problems": ["No semantic model found in the upload."], "m_code": {}}
        if defn.is_file():
            _edit_bim(defn, removals, changes)
        else:
            files = sorted(defn.rglob("*.tmdl"))
            # data columns are only dropped where the Power Query step can be appended (or there is no query: Direct Lake)
            for f in files:
                if f.parent.name == "tables":
                    txt = f.read_text(encoding="utf-8-sig")
                    tname = next((_unquote(l.strip()[6:]) for l in txt.splitlines() if l.startswith("table ")), None)
                    rm = removals.get(tname or "")
                    if rm and rm["data_columns"]:
                        part = re.search(r"^\s*partition .*=\s*(\w+)\s*$", txt, re.M)
                        kind = part.group(1) if part else ""
                        if kind == "m" and not m_code_for(_partition_m(txt) or "", ["x"]):
                            rm["_m_failed"] = True
                        if kind not in ("m", "entity"):
                            rm["_m_failed"] = True
            for t, rm in removals.items():
                if rm.get("_m_failed"):
                    rm["skipped_data_columns"] = rm["data_columns"]
                    rm["data_columns"] = {}
            for f in files:
                txt = f.read_text(encoding="utf-8-sig")
                new = edit_tmdl(txt, removals, changes, str(f.relative_to(defn)))
                if new != txt:
                    f.write_text(new, encoding="utf-8")
        skipped = [{"table": t, "columns": sorted(rm.get("skipped_data_columns", {})),
                    "reason": "Power Query is not a simple let … in query - remove these columns in Power Query yourself (M code below)."}
                   for t, rm in removals.items() if rm.get("skipped_data_columns")]
        before = len(Usage_unresolved(model))
        problems = _verify(root, removals, before)
        m_code = {}
        for t in model.tables:
            rm = removals.get(t.name)
            cols = sorted(set((rm or {}).get("data_columns", {}).values()) | set((rm or {}).get("skipped_data_columns", {}).values())) if rm else []
            if cols and t.source:
                m_code[t.name] = m_code_for(t.source, cols) or (
                    f"// Add as the last step of the '{t.name}' query:\n{STEP} = Table.RemoveColumns(<previous step>, "
                    f"{{{', '.join(_q(c) for c in cols)}}}, MissingField.Ignore)")
        buf, opened, fixes = None, None, []
        if not problems:
            fixes, opened = ensure_openable(root, getattr(model, "model_name", "") or "Model")
            notes = _readme(changes, skipped, m_code, opened, fixes)
            (root / "VERITYIQ_CLEANUP.md").write_text(notes, encoding="utf-8")
            top = re.sub(r'[<>:"/\\|?*]+', "_", f"{getattr(model, 'model_name', '') or 'Model'} (cleaned)")
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(root.rglob("*")):
                    if f.is_file():
                        z.write(f, f"{top}/{f.relative_to(root).as_posix()}")
            buf = bio.getvalue()
        return {"ok": not problems, "zip": buf, "changes": changes, "skipped": skipped, "problems": problems, "m_code": m_code,
                "open_file": opened, "structure_fixes": fixes,
                "counts": {"measures": sum(c["action"] == "deleted measure" for c in changes if _is_model_file(c["file"])),
                           "columns": sum(c["action"] == "deleted column" for c in changes if _is_model_file(c["file"])),
                           "queries": sum(c["action"].startswith("Power Query") for c in changes)}}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _is_model_file(f: str) -> bool:
    return f.replace("\\", "/").startswith("tables/") or f.endswith(".bim")


def Usage_unresolved(model) -> list:
    from .usage import Usage
    return Usage(model).run()["unresolved_report_refs"]


def _partition_m(tmdl: str) -> Optional[str]:
    lines = tmdl.splitlines()
    lvl = _Indent(lines)
    for i, l in enumerate(lines):
        if re.match(r"^\s*partition .*=\s*m\s*$", l):
            base = lvl(l)
            for k in range(i + 1, len(lines)):
                if lines[k].strip() and lvl(lines[k]) <= base:
                    break
                if re.match(r"^\s*source\s*=", lines[k]):
                    s_lvl = lvl(lines[k])
                    body = []
                    for x in lines[k + 1:]:
                        if x.strip() and lvl(x) <= s_lvl:
                            break
                        body.append(x)
                    return "\n".join(body)
    return None


def _readme(changes: list, skipped: list, m_code: dict, opened: Optional[str] = None, fixes: Optional[list] = None) -> str:
    out = ["# VerityIQ model cleanup", "", "Unused objects removed from this copy of the PBIP.", "",
           "## How to open", "", "1. Extract the whole zip (Windows: right-click › Extract All) - Power BI cannot open it from inside the zip.",
           f"2. Double-click **{opened or 'the .pbip file'}** (Power BI Desktop, with *File › Options › Preview features › Power BI Project* on in older versions).",
           "3. Refresh, check every page, then replace your original project.", ""]
    if fixes:
        out += ["Files added so Power BI Desktop can open this copy: " + "; ".join(fixes) + ".", ""]
    out += ["## Changes", ""]
    for c in changes:
        out.append(f"- **{c['table']}** · {c['action']}: {c['object']}  _( {c['file']} )_")
    if skipped:
        out += ["", "## Not removed automatically", ""]
        for s in skipped:
            out.append(f"- **{s['table']}**: {', '.join(s['columns'])} - {s['reason']}")
    if m_code:
        out += ["", "## Power Query (M) per table", ""]
        for t, code in m_code.items():
            out += [f"### {t}", "", "```", code, "```", ""]
    return "\n".join(out) + "\n"
