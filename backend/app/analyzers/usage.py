"""Model usage analysis: which measures, columns and tables the report actually needs.

An object is USED when something the report shows needs it, directly or through a chain:
  report      a visual (fields, filters, sort, conditional formatting, tooltips), a slicer, a report/page
              filter, or a measure defined in the report itself
  indirect    referenced by another used object (measure -> measure/column, calculated column, calculated
              table / field parameter, hierarchy level, sort-by column of a used column)
  structure   needed by the model itself: relationship keys, hierarchy levels, row-level security (RLS) filters
  kept        marked "keep" by a person (e.g. used by a report or Excel workbook outside this upload)
Anything else is UNUSED - a candidate for removal (smaller model, faster refresh, less to maintain).
UNUSED objects referenced only by other unused objects are reported as "used only by unused" (Measure Killer's
"used by unused"): they become removable together with the objects that reference them, in one pass.

Limits (stated in the result): only the report(s) uploaded with the model are scanned - other reports
on the same semantic model, Analyze in Excel and composite models can still use "unused" objects;
model size per object needs VertiPaq statistics, which a PBIP does not contain.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Optional

from ..core.models import ModelMetadata

AUTO_DATE = re.compile(r"^(LocalDateTable_|DateTableTemplate_)", re.I)
_REF = re.compile(r"(?:'((?:[^']|'')+)'|\b([A-Za-z_][\w]*))?\s*\[((?:[^\]]|\]\])+)\]")


def _strip(dax: str) -> str:
    dax = re.sub(r"/\*.*?\*/", " ", dax or "", flags=re.S)
    dax = re.sub(r"(//|--)[^\n]*", " ", dax)
    return re.sub(r'"(?:[^"]|"")*"', '""', dax)


class Usage:
    def __init__(self, model: ModelMetadata, keep: Optional[set] = None, extra_visuals: Optional[list] = None,
                 extra_refs: Optional[list] = None, sizes: Optional[dict] = None):
        self.m = model
        self.keep = {k.lower() for k in (keep or set())}
        self.extra_visuals = extra_visuals or []          # visuals of other reports on the same semantic model
        self.extra_refs = extra_refs or []
        self.sizes = sizes or {}                          # object key -> {"bytes", "cardinality"} from a .vpax
        self.tables = {t.name.lower(): t for t in model.tables if not AUTO_DATE.match(t.name)}
        self.measures = {}                          # lower name -> (table, MeasureMeta)
        for t in self.tables.values():
            for ms in t.measures:
                self.measures[ms.name.lower()] = (t.name, ms)
        self.cols = {(t.name.lower(), c.name.lower()): (t.name, c) for t in self.tables.values() for c in t.columns}
        self.reasons: dict[str, list[str]] = defaultdict(list)   # object key -> why it is needed
        self.kind: dict[str, str] = {}                          # object key -> report | indirect | structure
        self.deps: dict[str, set[str]] = defaultdict(set)       # object key -> keys it references
        self.unresolved: list[str] = []

    # ------------------------------------------------------------------ keys
    @staticmethod
    def mkey(name: str) -> str:
        return f"m:{name.lower()}"

    @staticmethod
    def ckey(table: str, col: str) -> str:
        return f"c:{table.lower()}|{col.lower()}"

    def dax_refs(self, expr: str, home: Optional[str]) -> set[str]:
        out = set()
        for q, bare, name in _REF.findall(_strip(expr)):
            tbl = (q or bare).replace("''", "'")
            name = name.replace("]]", "]")
            if tbl:
                if (tbl.lower(), name.lower()) in self.cols:
                    out.add(self.ckey(tbl, name))
                elif name.lower() in self.measures:
                    out.add(self.mkey(name))
            elif name.lower() in self.measures:
                out.add(self.mkey(name))
            elif home and (home.lower(), name.lower()) in self.cols:
                out.add(self.ckey(home, name))
        return out

    def ref_key(self, ref: str) -> Optional[str]:
        """Report reference (m||Name, c|T|Col, h|T|Hier|Level) -> object key(s)."""
        parts = ref.split("|")
        if parts[0] == "m":
            n = parts[2].lower()
            if n in self.measures:
                return self.mkey(n)
            # implicit / report-level measure names are not model measures
            return None
        if parts[0] == "c" and len(parts) >= 3:
            if (parts[1].lower(), parts[2].lower()) in self.cols:
                return self.ckey(parts[1], parts[2])
            if parts[2].lower() in self.measures:          # measure written as a column ref
                return self.mkey(parts[2])
            return None
        if parts[0] == "h" and len(parts) >= 4:
            t = self.tables.get(parts[1].lower())
            if t:
                for h in t.hierarchies:
                    if h["name"].lower() == parts[2].lower():
                        for lv in h["levels"]:
                            if lv["name"].lower() == parts[3].lower():
                                return self.ckey(t.name, lv["column"])
                if (t.name.lower(), parts[3].lower()) in self.cols:
                    return self.ckey(t.name, parts[3])
        return None

    def need(self, key: Optional[str], why: str, kind: str):
        if not key:
            return
        if why not in self.reasons[key]:
            self.reasons[key].append(why)
        rank = {"report": 0, "kept": 1, "structure": 2, "indirect": 3}
        if key not in self.kind or rank[kind] < rank[self.kind[key]]:
            self.kind[key] = kind

    # ------------------------------------------------------------------ analysis
    def run(self) -> dict:
        m = self.m
        # dependency edges
        for lname, (tname, ms) in self.measures.items():
            self.deps[self.mkey(lname)] |= self.dax_refs(ms.expression, tname)
        calc_tables = {}
        for t in self.tables.values():
            for c in t.columns:
                k = self.ckey(t.name, c.name)
                if c.expression:
                    self.deps[k] |= self.dax_refs(c.expression, t.name)
                if c.sort_by and (t.name.lower(), c.sort_by.lower()) in self.cols:
                    self.deps[k].add(self.ckey(t.name, c.sort_by))
            if t.source_kind == "calculated" and t.source:
                calc_tables[t.name.lower()] = self.dax_refs(t.source, None)
        for tl, refs in calc_tables.items():                  # a calculated table / field parameter needs what it builds from
            for c in self.tables[tl].columns:
                self.deps[self.ckey(self.tables[tl].name, c.name)] |= refs
            tn = self.tables[tl].name                         # tables are never deleted here, so what builds them stays
            for k in refs:
                self.need(k, f"Builds calculated table '{tn}' (delete that table first to free it)", "structure")

        # roots: the report
        for v in list(m.visuals) + list(self.extra_visuals):
            rep_name = v.report
            where = f"{'[' + rep_name + '] ' if rep_name else ''}{'hidden page ' if v.page_hidden else ''}'{v.page}' › {v.title or v.visual_type}"
            refs = v.refs or ([f"m||{x.strip('[]')}" for x in v.measures if not x.startswith("implicit:")]
                              + [f"c|{x[9:].split('[')[0].strip(chr(39))}|{x[9:].split('[')[1].rstrip(']')}" for x in v.measures if x.startswith("implicit:") and "[" in x]
                              + [f"c|{d.split('[')[0].strip(chr(39))}|{d.split('[')[1].split(']')[0]}" for d in v.dimensions if "[" in d])
            for r in refs:
                k = self.ref_key(r)
                if k:
                    self.need(k, f"Visual {where}", "report")
                elif r.startswith(("c|", "h|")):
                    self.unresolved.append(f"{r} ({where})")
        for rr in list(m.report_refs) + list(self.extra_refs):
            self.need(self.ref_key(rr["ref"]), rr["where"], "report")
        for rep in m.reports:
            for rm in rep.get("report_measures", []):
                for k in self.dax_refs(rm.get("expression", ""), None):
                    self.need(k, f"Report measure [{rm.get('name')}]", "report")
        # roots: model structure
        for r in m.relationships:
            for t, c in ((r.from_table, r.from_column), (r.to_table, r.to_column)):
                if (t.lower(), c.lower()) in self.cols:
                    self.need(self.ckey(t, c), f"Relationship key {r.from_table}[{r.from_column}] → {r.to_table}[{r.to_column}]"
                              + ("" if r.active else " (inactive)"), "structure")
        for role in m.roles:
            for k in self.dax_refs(role.get("expression", ""), role.get("table")):
                self.need(k, f"Row-level security role '{role.get('role')}'", "structure")
        for t in self.tables.values():                    # deleting a level column would break the hierarchy
            for h in t.hierarchies:
                for lv in h.get("levels", []):
                    if (t.name.lower(), str(lv.get("column", "")).lower()) in self.cols:
                        self.need(self.ckey(t.name, lv["column"]), f"Level '{lv.get('name')}' of hierarchy '{h.get('name')}'", "structure")
        for k in list(self.keep):
            if k in self.deps or k.startswith(("m:", "c:")) and (k in {self.mkey(n) for n in self.measures} or
                                                                 k in {self.ckey(a, b) for (a, b) in self.cols}):
                self.need(k, "Kept by you (used outside this upload)", "kept")

        # propagate needs through dependencies
        q = deque(self.kind)
        seen = set(q)
        while q:
            k = q.popleft()
            for d in self.deps.get(k, ()):
                self.need(d, f"Used by {self.label(k)}", "indirect")
                if d not in seen:
                    seen.add(d)
                    q.append(d)
        # unused objects that only unused objects reference ("used by unused")
        self.by_unused: dict[str, list[str]] = defaultdict(list)
        for k, ds in self.deps.items():
            if k in self.kind:
                continue
            for d in ds:
                if d not in self.kind and d != k:
                    self.by_unused[d].append(self.label(k))
        return self.report()

    def label(self, key: str) -> str:
        if key.startswith("m:"):
            t, ms = self.measures[key[2:]]
            return f"[{ms.name}]"
        t, c = key[2:].split("|", 1)
        tn, cm = self.cols[(t, c)]
        return f"{tn}[{cm.name}]"

    def report(self) -> dict:
        rows = []
        for lname, (tname, ms) in self.measures.items():
            k = self.mkey(lname)
            rows.append(self._row(k, "Measure", tname, ms.name, ms.expression, ms.hidden))
        for (tl, cl), (tname, c) in self.cols.items():
            if c.name.startswith("RowNumber-"):
                continue
            k = self.ckey(tname, c.name)
            rows.append(self._row(k, "Calculated column" if c.expression else "Column", tname, c.name, c.expression or "", c.hidden))
        by_table = defaultdict(list)
        for r in rows:
            by_table[r["table"]].append(r)
        tables = []
        for t in self.tables.values():
            rs = by_table.get(t.name, [])
            only_keys = all(r["status"] in ("unused", "used_by_unused") or (r["status"] == "structure" and all(x.startswith(("Relationship key", "Level ")) for x in r["used_by"])) for r in rs)
            tables.append({"table": t.name, "objects": len(rs), "unused": sum(r["status"] in ("unused", "used_by_unused") for r in rs),
                           "size_bytes": sum(r["size_bytes"] or 0 for r in rs) if self.sizes else None,
                           "size_unused_bytes": sum(r["size_bytes"] or 0 for r in rs if r["status"] in ("unused", "used_by_unused")) if self.sizes else None,
                           "entirely_unused": bool(rs) and only_keys, "kind": t.source_kind or "", "hidden": t.hidden})
        auto = [t.name for t in self.m.tables if AUTO_DATE.match(t.name)]
        cnt = lambda typ, st: sum(1 for r in rows if r["type"] in typ and (r["status"] in st))  # noqa: E731
        U = ("unused", "used_by_unused")
        M, C, CC = ("Measure",), ("Column", "Calculated column"), ("Calculated column",)
        ALL = ("report", "indirect", "structure", "kept") + U
        size_total = sum(r["size_bytes"] or 0 for r in rows)
        size_unused = sum(r["size_bytes"] or 0 for r in rows if r["status"] in U)
        summary = {
            "measures": cnt(M, ALL), "measures_unused": cnt(M, U), "measures_used_by_unused": cnt(M, ("used_by_unused",)),
            "columns": cnt(C, ALL), "columns_unused": cnt(C, U), "columns_used_by_unused": cnt(C, ("used_by_unused",)),
            "calculated_columns_unused": cnt(CC, U), "kept": sum(r["status"] == "kept" for r in rows),
            "size_known": bool(self.sizes), "size_total_bytes": size_total if self.sizes else None,
            "size_unused_bytes": size_unused if self.sizes else None,
            "tables_unused": sum(t["entirely_unused"] for t in tables), "auto_date_tables": len(auto),
            "visuals_scanned": len(self.m.visuals) + len(self.extra_visuals),
            "reports_scanned": len(self.m.reports) + len({v.report for v in self.extra_visuals} - {None}),
        }
        notes = []
        if not self.m.visuals:
            notes.append("No report was uploaded with this model, so every object looks unused. Upload the PBIP with its .Report folder.")
        if auto:
            notes.append(f"Auto date/time is on: {len(auto)} hidden date table(s) are added to the model. Turning it off (File › Options › "
                         "Data load) usually shrinks the model noticeably.")
        notes.append("Only the report(s) uploaded here are scanned. Add other reports that use this semantic model (thin reports) "
                     "below, and mark objects used by Excel or other tools as 'keep', before deleting anything.")
        return {"summary": summary, "objects": sorted(rows, key=lambda r: (r["status"] not in U, -(r["size_bytes"] or 0), r["table"].lower(), r["type"], r["name"].lower())),
                "tables": sorted(tables, key=lambda t: (-t["unused"], t["table"].lower())),
                "unresolved_report_refs": sorted(set(self.unresolved))[:50], "auto_date_tables": auto, "notes": notes}

    def _row(self, k: str, typ: str, table: str, name: str, expr: str, hidden: bool) -> dict:
        st = self.kind.get(k)
        reasons = self.reasons.get(k, [])
        if st is None:
            bu = sorted(set(self.by_unused.get(k, [])))
            st, reasons = ("used_by_unused", [f"Only used by {x} (unused)" for x in bu]) if bu else ("unused", [])
        sz = self.sizes.get(k) or {}
        return {"key": k, "type": typ, "table": table, "name": name, "status": st, "hidden": hidden,
                "size_bytes": sz.get("bytes"), "cardinality": sz.get("cardinality"),
                "used_by": reasons[:12], "used_by_count": len(reasons),
                "expression": (expr or "")[:4000], "ref": f"[{name}]" if typ == "Measure" else f"'{table}'[{name}]"}


def analyze_usage(model: Optional[ModelMetadata], **kw) -> dict:
    if model is None:
        return {"available": False, "reason": "Upload a PBIP first."}
    out = Usage(model, **kw).run()
    out["available"] = True
    return out


def unused_measures(model: Optional[ModelMetadata], **kw) -> set[str]:
    """Lower-case names of model measures nothing in the report needs."""
    if model is None or not model.visuals:
        return set()
    u = Usage(model, **kw)
    u.run()
    return {ms.name.lower() for lname, (t, ms) in u.measures.items() if u.mkey(lname) not in u.kind}
