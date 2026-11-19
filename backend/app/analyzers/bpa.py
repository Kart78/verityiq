"""Model and report best-practice rules (complements the DAX rules in analyzers/dax.py).

Each finding: {rule, scope (model|report), severity (High|Medium|Low|Info), title, objects, why, fix}.
Rules only use what a PBIP contains (plus VertiPaq sizes when a .vpax was uploaded).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ..core.models import ModelMetadata, is_slicer
from .usage import AUTO_DATE

MONEY = re.compile(r"(amount|price|cost|revenue|sales|margin|profit|value|total|tax|fee|discount|budget|spend)", re.I)
SEV = {"High": 0, "Medium": 1, "Low": 2, "Info": 3}


def _f(rule, scope, sev, title, objects, why, fix):
    return {"rule": rule, "scope": scope, "severity": sev, "title": title, "objects": objects[:200], "count": len(objects),
            "why": why, "fix": fix}


def _report_json(root: Optional[Path]) -> list[dict]:
    out = []
    if not root:
        return out
    for rj in root.rglob("report.json"):
        try:
            d = json.loads(rj.read_text(encoding="utf-8-sig"))
            cfg = d.get("config")
            if isinstance(cfg, str):
                try:
                    d["_config"] = json.loads(cfg)
                except ValueError:
                    d["_config"] = {}
            out.append(d)
        except (ValueError, OSError):
            continue
    return out


def _bookmarks(root: Optional[Path], reps: list[dict]) -> int:
    if not root:
        return 0
    n = sum(1 for _ in root.rglob("*.bookmark.json"))
    for d in reps:
        def walk(items):
            c = 0
            for b in items or []:
                c += 1 if not b.get("children") else walk(b.get("children"))
            return c
        n += walk((d.get("_config") or {}).get("bookmarks"))
    return n


def run_rules(model: ModelMetadata, usage: Optional[dict] = None, pbip_root: Optional[Path] = None,
              vpax: Optional[dict] = None) -> dict:
    F: list[dict] = []
    tables = [t for t in model.tables if not AUTO_DATE.match(t.name)]
    auto = [t.name for t in model.tables if AUTO_DATE.match(t.name)]
    rels = model.relationships

    # ------------------------------------------------------------------ model
    if auto:
        size = ""
        if vpax:
            b = sum(v["bytes"] for k, v in vpax.get("tables", {}).items() if AUTO_DATE.match(v.get("table", "")))
            size = f" ({b / 1048576:.1f} MB)" if b else ""
        F.append(_f("M01", "model", "High", f"Auto date/time is on: {len(auto)} hidden date tables{size}", auto,
                    "Power BI adds a hidden date table for every date column, inflating the model and refresh time.",
                    "File › Options › Current file › Data load › untick Auto date/time; use one marked Date table."))
    bidi = [f"{r.from_table}[{r.from_column}] ↔ {r.to_table}[{r.to_column}]" for r in rels if "both" in (r.cross_filter or "").lower()]
    if bidi:
        F.append(_f("M02", "model", "Medium", "Bi-directional relationships", bidi,
                    "Filters flowing both ways cause ambiguous paths, unexpected totals and slower queries.",
                    "Use single direction; apply CROSSFILTER(…, Both) inside the few measures that need it."))
    m2m = [f"{r.from_table}[{r.from_column}] → {r.to_table}[{r.to_column}]" for r in rels
           if (r.cardinality or "").lower().startswith("many-to-many") or (r.cardinality or "").lower() == "many-to-many"]
    if m2m:
        F.append(_f("M03", "model", "Medium", "Many-to-many relationships", m2m,
                    "Many-to-many joins are easy to misread and can return inflated or unexpected values.",
                    "Model a bridge table, or confirm the grain on both sides is intended."))
    all_dax = " ".join(m.expression or "" for t in tables for m in t.measures)
    inactive = [f"{r.from_table}[{r.from_column}] → {r.to_table}[{r.to_column}]" for r in rels if not r.active
                and not re.search(rf"USERELATIONSHIP\s*\([^)]*{re.escape(r.from_column)}", all_dax, re.I)]
    if inactive:
        F.append(_f("M04", "model", "Low", "Inactive relationships no measure activates", inactive,
                    "An inactive relationship only matters when a measure uses USERELATIONSHIP; otherwise it is dead weight.",
                    "Delete it, or add the measure that should use it."))
    nofmt = [f"[{m.name}]" for t in tables for m in t.measures if not m.hidden and not m.format_string]
    if nofmt:
        F.append(_f("M05", "model", "Low", "Visible measures without a format string", nofmt,
                    "Without a format string values show with full precision and no thousands separators or %.",
                    "Set a format string (e.g. #,0 or 0.0%) on each measure."))
    keys = sorted({f"{r.from_table}[{r.from_column}]" for r in rels for t in tables if t.name == r.from_table
                   for c in t.columns if c.name == r.from_column and not c.hidden})
    if keys:
        F.append(_f("M06", "model", "Low", "Visible key columns on the many side of relationships", keys,
                    "Report authors drag fact-table keys into visuals and get wrong groupings.",
                    "Hide foreign-key columns; use the dimension's column instead."))
    dbl = [f"{t.name}[{c.name}]" for t in tables for c in t.columns
           if (c.data_type or "").lower() == "double" and MONEY.search(c.name)]
    if dbl:
        F.append(_f("M07", "model", "Low", "Money-like columns stored as floating point (Decimal number)", dbl,
                    "Floating point can produce rounding noise in totals (e.g. 0.30000000000000004).",
                    "Use Fixed decimal number (currency) for monetary columns."))
    connected = {r.from_table for r in rels} | {r.to_table for r in rels}
    lone = [t.name for t in tables if t.name not in connected and t.columns and t.source_kind not in ("calculated", "calculation_group")
            and any(not c.name.startswith("RowNumber") for c in t.columns) and not (t.measures and len(t.columns) <= 1)]
    if lone and len(tables) > 1:
        F.append(_f("M08", "model", "Info", "Tables with no relationships", lone,
                    "A disconnected table is not filtered by slicers on other tables (fine for parameter tables, a defect for data).",
                    "Relate it, or confirm it is a deliberately disconnected helper table."))
    for t in tables:
        cc = [c.name for c in t.columns if c.expression]
        if len(cc) >= 5 and t.source_kind not in ("calculated",):
            F.append(_f("M10", "model", "Info", f"'{t.name}' has {len(cc)} calculated columns", [f"{t.name}[{c}]" for c in cc],
                        "Calculated columns are computed at refresh and compress worse than Power Query / source columns.",
                        "Move row-level logic to Power Query or the source view where possible."))
    spaces = [f"{t.name}[{c.name}]" for t in tables for c in t.columns if c.name != c.name.strip()] + \
             [t.name for t in tables if t.name != t.name.strip()]
    if spaces:
        F.append(_f("M12", "model", "Low", "Names with leading or trailing spaces", spaces,
                    "Invisible spaces break DAX references written by hand and confuse users.", "Trim the names."))
    if usage and usage.get("available"):
        s = usage["summary"]
        pm = s["measures_unused"] / s["measures"] if s["measures"] else 0
        pc = s["columns_unused"] / s["columns"] if s["columns"] else 0
        if pm > .25 or pc > .25:
            F.append(_f("M09", "model", "Medium", f"{round(pm * 100)}% of measures and {round(pc * 100)}% of columns are unused",
                        [f"{s['measures_unused']} measures", f"{s['columns_unused']} columns"],
                        "Unused objects add memory, refresh time and maintenance cost.", "Review Model Usage › Clean up."))
    if vpax and vpax.get("total_bytes"):
        tot = vpax["total_bytes"]
        big = sorted(vpax["columns"].values(), key=lambda c: -c["bytes"])[:5]
        heavy = [f"{c['table']}[{c['column']}] {c['bytes'] / 1048576:.1f} MB ({c['bytes'] / tot:.0%})" for c in big if c["bytes"] / tot >= .1]
        if heavy:
            F.append(_f("M16", "model", "Medium", "Columns taking 10% or more of the model", heavy,
                        "A few high-cardinality columns (IDs, timestamps, free text) usually dominate model size.",
                        "Remove them if unused; otherwise reduce cardinality (split date/time, round, drop GUIDs)."))

    # ------------------------------------------------------------------ report
    if model.visuals or model.reports:
        pages: dict[str, list] = {}
        for v in model.visuals:
            pages.setdefault(v.page, []).append(v)
        busy = [f"'{p}': {len(vs)} visuals" for p, vs in pages.items() if len(vs) > 15]
        if busy:
            F.append(_f("R01", "report", "Medium", "Pages with more than 15 visuals", busy,
                        "Every visual sends its own query; crowded pages load slowly.", "Split the page, or combine cards into one visual."))
        sl = [f"'{p}': {sum(is_slicer(v.visual_type) for v in vs)} slicers" for p, vs in pages.items() if sum(is_slicer(v.visual_type) for v in vs) > 5]
        if sl:
            F.append(_f("R02", "report", "Low", "Pages with more than 5 slicers", sl,
                        "Many slicers slow the page and overwhelm users.", "Move secondary filters to the filter pane."))
        if usage and usage.get("unresolved_report_refs"):
            F.append(_f("R03", "report", "High", "Visuals referencing fields that are not in the model", usage["unresolved_report_refs"],
                        "These visuals show an error instead of data.", "Re-bind the visual to an existing field or restore the field."))
        rms = [f"[{m['name']}]" for r in model.reports for m in r.get("report_measures", [])]
        if rms:
            F.append(_f("R04", "report", "Low", "Measures defined in the report instead of the model", rms,
                        "Report-level measures are invisible to other reports and to model tests.", "Move them into the semantic model."))
        empty = [p["name"] for r in model.reports for p in r.get("pages", []) if p["name"] not in pages]
        if empty:
            F.append(_f("R07", "report", "Low", "Empty report pages", empty, "Empty pages confuse users.", "Delete or fill them."))
        hidden = sorted({v.page for v in model.visuals if v.page_hidden})
        if hidden:
            F.append(_f("R09", "report", "Info", "Hidden pages (tooltip, drill-through or forgotten)", hidden,
                        "Hidden pages still load with the report and their fields count as used.", "Delete hidden pages nobody uses."))
        reps = _report_json(pbip_root)
        nb = _bookmarks(pbip_root, reps)
        if nb > 20:
            F.append(_f("R06", "report", "Low", f"{nb} bookmarks", [f"{nb} bookmarks"],
                        "Large bookmark sets are hard to maintain and often reference removed visuals.", "Remove stale bookmarks."))
        used_types = {v.visual_type.lower() for v in model.visuals}
        cv = set()
        for d in reps:
            for key in ("publicCustomVisuals",):
                cv |= set(d.get(key) or []) | set((d.get("_config") or {}).get(key) or [])
        unused_cv = sorted(x for x in cv if x.lower() not in used_types)
        if unused_cv:
            F.append(_f("R05", "report", "Low", "Custom visuals imported but not used", unused_cv,
                        "Unused custom visuals still load with the report.", "Remove them (Visualizations pane › … › Remove)."))
    F.sort(key=lambda f: (SEV[f["severity"]], f["scope"], f["rule"]))
    counts = {s: sum(f["severity"] == s for f in F) for s in SEV}
    return {"findings": F, "counts": counts}
