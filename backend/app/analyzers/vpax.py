"""VertiPaq Analyzer statistics (.vpax from DAX Studio › Advanced › Export Metrics, or Tabular Editor).

A .vpax is a zip holding DaxVpaView.json (flat Tables / Columns lists with sizes in bytes) and DaxModel.json
(nested model with dictionary and segment sizes). Only column/table sizes, cardinality and row counts are used:
they rank unused columns by the memory and refresh time removing them would save.
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Optional


def _name(v: Any) -> Optional[str]:
    if isinstance(v, dict):
        v = v.get("Name")
    return str(v) if v not in (None, "") else None


def _num(*vals) -> Optional[int]:
    for v in vals:
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _load_json(raw: bytes) -> Any:
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError("not JSON")


def parse_vpax(data: bytes, filename: str = "") -> dict:
    """-> {"columns": {(table, column): {...}}, "tables": {table: {...}}, "total_bytes", "source"} (names lower-case)."""
    docs: dict[str, Any] = {}
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                base = n.rsplit("/", 1)[-1]
                if base.lower() in ("daxvpaview.json", "daxmodel.json"):
                    docs[base.lower()] = _load_json(z.read(n))
    else:                                               # a bare DaxVpaView / DaxModel JSON
        docs["daxvpaview.json"] = _load_json(data)
    if not docs:
        raise ValueError("No VertiPaq statistics found - export a .vpax from DAX Studio (Advanced › Export Metrics).")
    cols: dict[tuple, dict] = {}
    tables: dict[str, dict] = {}
    view = docs.get("daxvpaview.json")
    if isinstance(view, dict) and view.get("Columns"):
        for c in view.get("Columns", []):
            t, n = _name(c.get("TableName")), _name(c.get("ColumnName"))
            if not t or not n:
                continue
            size = _num(c.get("TotalSize"), c.get("ColumnSize"))
            if size is None:
                size = sum(x for x in (_num(c.get("DataSize")), _num(c.get("DictionarySize")), _num(c.get("HierarchiesSize"))) if x)
            cols[(t.lower(), n.lower())] = {"table": t, "column": n, "bytes": size or 0,
                                            "cardinality": _num(c.get("ColumnCardinality"), c.get("Cardinality")),
                                            "data_type": c.get("DataType")}
        for t in view.get("Tables", []):
            name = _name(t.get("TableName"))
            if name:
                tables[name.lower()] = {"table": name, "bytes": _num(t.get("TableSize"), t.get("ColumnsSize")) or 0,
                                        "rows": _num(t.get("RowsCount"), t.get("Cardinality"))}
    model = docs.get("daxmodel.json")
    if not cols and isinstance(model, dict):
        for t in model.get("Tables", []):
            tname = _name(t.get("TableName"))
            if not tname:
                continue
            total = 0
            for c in t.get("Columns", []):
                cname = _name(c.get("ColumnName"))
                if not cname:
                    continue
                seg = sum(_num(s.get("UsedSize")) or 0 for s in c.get("ColumnSegments", []) or [])
                hier = sum(_num(h.get("UsedSize")) or 0 for h in c.get("ColumnHierarchies", []) or c.get("UserHierarchies", []) or [])
                size = (_num(c.get("DictionarySize")) or 0) + seg + hier
                total += size
                cols[(tname.lower(), cname.lower())] = {"table": tname, "column": cname, "bytes": size,
                                                        "cardinality": _num(c.get("ColumnCardinality")), "data_type": c.get("DataType")}
            tables[tname.lower()] = {"table": tname, "bytes": total, "rows": _num(t.get("RowsCount"))}
    for (tl, _), c in cols.items():                      # fill table totals when the export has none
        tables.setdefault(tl, {"table": c["table"], "bytes": 0, "rows": None})
    for tl, t in tables.items():
        if not t["bytes"]:
            t["bytes"] = sum(c["bytes"] for (x, _), c in cols.items() if x == tl)
    if not cols:
        raise ValueError("The file has no column statistics.")
    return {"columns": cols, "tables": tables, "total_bytes": sum(t["bytes"] for t in tables.values()),
            "source": filename or "vpax"}


def to_json(stats: dict) -> dict:
    """JSON-safe form for storage: column keys become 'table|column'."""
    return {**stats, "columns": {f"{t}|{c}": v for (t, c), v in stats["columns"].items()}}


def size_map(stored: Optional[dict]) -> dict:
    """Stored VertiPaq stats -> usage-object key -> {"bytes", "cardinality"} (keys as in analyzers.usage)."""
    if not stored:
        return {}
    return {f"c:{k}": {"bytes": v.get("bytes"), "cardinality": v.get("cardinality")} for k, v in stored.get("columns", {}).items()}
