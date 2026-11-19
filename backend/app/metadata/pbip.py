"""Readers for Power BI project files - tolerant of real-world variation.

Semantic model : TMDL folder (definition/*.tmdl)  or  TMSL model.bim  (*.SemanticModel / *.Dataset)
Report         : PBIR folder (definition/pages/*/visuals/*/visual.json)  or  legacy report.json
Sources        : Power Query M (incl. shared expressions & parameters), Direct Lake entity
                 partitions, native SQL, calculated (DAX) tables

Every file is parsed in isolation: a malformed file is reported in ModelMetadata.unsupported and
the rest of the model is still read. Nothing here executes any content.
"""
from __future__ import annotations

from app.core.names import unbracket

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..core.models import (ColumnMeta, MeasureMeta, ModelMetadata, RelationshipMeta, TableMeta, VisualMeta, is_slicer)

# --------------------------------------------------------------------------------- helpers


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig", errors="replace")


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] == "'":
        return name[1:-1].replace("''", "'")
    if len(name) >= 2 and name[0] == name[-1] == '"':
        return name[1:-1]
    return name


def _split_name_expr(rest: str) -> tuple[str, Optional[str]]:
    """"'FCR %' = DIVIDE(..)" -> ("FCR %", "DIVIDE(..)") respecting quoted names."""
    in_q = False
    for i, ch in enumerate(rest):
        if ch == "'":
            in_q = not in_q
        elif ch == "=" and not in_q:
            return _unquote(rest[:i]), rest[i + 1:].strip()
    return _unquote(rest), None


def _split_col_ref(ref: str) -> tuple[str, str]:
    """TMDL "'Case Facts'.'customer id'" or "Cases.customer_id" -> (table, column)."""
    in_q = False
    for i, ch in enumerate(ref):
        if ch == "'":
            in_q = not in_q
        elif ch == "." and not in_q:
            return _unquote(ref[:i]), _unquote(ref[i + 1:])
    return "", _unquote(ref)


class _Indent:
    """TMDL is tab-indented, but files edited by hand/tools may use spaces: detect the unit per file."""

    def __init__(self, lines: list[str]):
        widths = sorted({len(l) - len(l.lstrip(" ")) for l in lines if l.startswith(" ") and l.strip()})
        self.unit = widths[0] if widths else 4
        self.unit = self.unit if self.unit in (1, 2, 3, 4, 8) else 4

    def __call__(self, line: str) -> int:
        n, spaces = 0, 0
        for ch in line:
            if ch == "\t":
                n += 1 + spaces // self.unit
                spaces = 0
            elif ch == " ":
                spaces += 1
            else:
                break
        return n + spaces // self.unit


_PROP_RE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9]*\s*(:|=|$)")
_CHILD_KW = ("kpi", "annotation", "extendedProperty", "changedProperty", "formatStringDefinition", "detailRowsDefinition",
             "variation", "lineageTag", "sourceLineageTag", "displayFolder", "description", "isHidden", "dataCategory")


def _collect_block(lines: list[str], start: int, lvl, base: int, first: Optional[str]) -> tuple[str, int, list[str]]:
    """Collect a multi-line expression that belongs to an object at indent `base`.

    Returns (expression, next_index, property_lines). The expression ends at the first line
    indented at base+1 (a property or child object of the same object) or less. Lines of
    child objects (kpi, formatStringDefinition, ...) are returned as properties, never as expression.
    """
    body: list[str] = []
    props: list[str] = []
    fenced = first is not None and first.startswith("```")
    if first and not fenced:
        body.append(first)
    j, in_props = start, False
    while j < len(lines):
        raw = lines[j]
        if fenced:
            if raw.strip().startswith("```"):
                fenced = False
                j += 1
                continue
            body.append(raw.strip("\t ").rstrip())
            j += 1
            continue
        if not raw.strip():
            j += 1
            continue
        nl = lvl(raw)
        if nl <= base:
            break
        if not in_props and (nl >= base + 2 or (nl == base + 1 and not body and first is None and not _PROP_RE.match(raw))):
            body.append(raw.strip())
        else:
            in_props = True
            if nl == base + 1:
                props.append(raw.strip())
        j += 1
    return "\n".join(body).strip(), j, props


def _prop(props: list[str], name: str) -> Optional[str]:
    for p in props:
        m = re.match(rf"^{name}\s*:\s*(.*)$", p)
        if m:
            return m.group(1).strip()
    return None


# --------------------------------------------------------------------------------- Power Query column lineage

def _balanced(text: str, start: int) -> str:
    depth, i, in_str = 0, start, False
    while i < len(text):
        ch = text[i]
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return text[start:]


def pq_lineage(m_text: str) -> tuple[dict[str, str], set[str]]:
    """Undo Power Query steps: {output column name -> original source column}, {columns added in Power Query}."""
    renames: dict[str, str] = {}
    added: set[str] = set()
    for mm in re.finditer(r"Table\.(RenameColumns|AddColumn|DuplicateColumn)\s*\(", m_text or ""):
        call = _balanced(m_text, mm.end() - 1)
        if mm.group(1) == "RenameColumns":
            for old, new in re.findall(r'\{\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"\s*\}', call):
                old, new = old.replace('""', '"'), new.replace('""', '"')
                renames[new] = renames.pop(old, old) if old in renames else old
                if old in added:
                    added.discard(old)
                    added.add(new)
        elif mm.group(1) == "AddColumn":
            n = re.match(r'\(\s*[^,]+,\s*"((?:[^"]|"")*)"', call)
            if n:
                added.add(n.group(1).replace('""', '"'))
        else:  # DuplicateColumn(prev, "src", "new")
            n = re.match(r'\(\s*[^,]+,\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"', call)
            if n:
                renames[n.group(2)] = renames.get(n.group(1), n.group(1))
    return renames, added


def pq_columns_sql(m_text: str) -> dict[str, Optional[str]]:
    """Columns added (Table.AddColumn) or transformed in place (Table.TransformColumns) in Power Query ->
    equivalent SQL over the source columns, or None when the expression is not translatable."""
    from .m_to_sql import UNARY_FN, m_expr_to_sql, split_args
    renames: dict[str, str] = {}
    computed: dict[str, Optional[str]] = {}

    def ref(name: str) -> Optional[str]:
        if name in computed:
            return None if computed[name] is None else f"({computed[name]})"
        return "{t}.`" + renames.get(name, name).replace("`", "``") + "`"

    for mm in re.finditer(r"Table\.(RenameColumns|AddColumn|DuplicateColumn|TransformColumns)\s*\(", m_text or ""):
        call = _balanced(m_text, mm.end() - 1)
        kind = mm.group(1)
        if kind == "RenameColumns":
            for old, new in re.findall(r'\{\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"\s*\}', call):
                old, new = old.replace('""', '"'), new.replace('""', '"')
                if old in computed:
                    computed[new] = computed.pop(old)
                else:
                    renames[new] = renames.pop(old, old) if old in renames else old
        elif kind == "DuplicateColumn":
            a = split_args(call)
            if len(a) >= 3:
                src, new = _unq(a[1]), _unq(a[2])
                if src in computed:
                    computed[new] = computed[src]
                else:
                    renames[new] = renames.get(src, src)
        elif kind == "AddColumn":
            a = split_args(call)
            if len(a) >= 3:
                computed[_unq(a[1])] = m_expr_to_sql(a[2], ref)
        else:                                            # TransformColumns(prev, {{"Col", fn, type}, ...})
            a = split_args(call)
            if len(a) < 2:
                continue
            lst = a[1].strip()
            items = [lst] if re.match(r'^\{\s*"', lst) else split_args(lst[1:-1] if lst.startswith("{") else lst)
            for it in items:
                parts = split_args(it.strip()[1:-1] if it.strip().startswith("{") else it)
                if len(parts) < 2:
                    continue
                cname, fn = _unq(parts[0]), parts[1].strip()
                base = ref(cname)
                if base is None:
                    computed[cname] = None
                    continue
                if fn in UNARY_FN:
                    fn = f"each {fn}(_)"
                sql = m_expr_to_sql(fn, lambda n, _b=base: _b if n == "_" else ref(n))
                computed[cname] = sql
    return computed


def _unq(s: str) -> str:
    s = s.strip()
    return s[1:-1].replace('""', '"') if len(s) >= 2 and s[0] == s[-1] == '"' else s


def apply_lineage(table: TableMeta, m_text: str) -> None:
    if not m_text or table.source_kind in ("calculated", "calculation_group", "directlake"):
        for c in table.columns:
            c.physical_column = c.physical_column or c.source_column
        return
    renames, added = pq_lineage(m_text)
    try:
        sqls = pq_columns_sql(m_text)
    except Exception:  # noqa: BLE001  (translation is best-effort)
        sqls = {}
    for c in table.columns:
        if c.expression is not None:
            continue
        sc = c.source_column or c.name
        if sc in added:
            c.pq_computed, c.physical_column = True, None
        else:
            c.physical_column = renames.get(sc, sc)
        if sqls.get(sc):
            c.pq_sql = sqls[sc]


# --------------------------------------------------------------------------------- TMDL

def parse_tmdl_table(text: str, source_file: str = "", expressions: Optional[dict] = None) -> Optional[TableMeta]:
    lines = text.splitlines()
    lvl = _Indent(lines)
    table: Optional[TableMeta] = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith("///") or s.startswith("//"):
            i += 1
            continue
        level = lvl(raw)
        if level == 0 and s.startswith("table "):
            table = TableMeta(name=_unquote(s[6:]))
        elif table and level == 1 and re.match(r"dataCategory\s*:\s*Time", s, re.I):
            table.is_date_table = True
        elif table and level == 1 and s == "isHidden":
            table.hidden = True
        elif table and level == 1 and s.startswith("hierarchy "):
            h = {"name": _unquote(s[10:]), "levels": []}
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lvl(lines[j]) >= 2):
                t2 = lines[j].strip()
                if lvl(lines[j]) == 2 and t2.startswith("level "):
                    h["levels"].append({"name": _unquote(t2[6:]), "column": _unquote(t2[6:])})
                elif lvl(lines[j]) >= 3 and t2.startswith("column:") and h["levels"]:
                    h["levels"][-1]["column"] = _unquote(t2.split(":", 1)[1].strip())
                j += 1
            table.hierarchies.append(h)
            i = j
            continue
        elif table and level == 1 and s.startswith("calculationGroup"):
            table.source_kind = "calculation_group"
        elif table and level == 1 and s.startswith("measure "):
            name, expr = _split_name_expr(s[8:])
            body, j, props = _collect_block(lines, i + 1, lvl, 1, expr)
            table.measures.append(MeasureMeta(name=name, table=table.name, expression=body, language="dax",
                                              format_string=_prop(props, "formatString"), source_file=source_file,
                                              hidden=any(p.strip() == "isHidden" for p in props)))
            i = j
            continue
        elif table and level == 1 and s.startswith("column "):
            name, expr = _split_name_expr(s[7:])
            body, j, props = _collect_block(lines, i + 1, lvl, 1, expr)
            col = ColumnMeta(name=name, expression=body if expr is not None else None,
                             data_type=_prop(props, "dataType"))
            sc = _prop(props, "sourceColumn")
            if sc:
                col.source_column = unbracket(_unquote(sc))
            sb = _prop(props, "sortByColumn")
            if sb:
                col.sort_by = _unquote(sb)
            col.hidden = any(p.strip() == "isHidden" for p in props)
            table.columns.append(col)
            i = j
            continue
        elif table and level == 1 and s.startswith("partition "):
            header = s
            _, src_inline = _split_name_expr(s[10:])
            body_lines, j = [], i + 1
            while j < len(lines) and (not lines[j].strip() or lvl(lines[j]) >= 2):
                body_lines.append(lines[j].strip())
                j += 1
            body = "\n".join(x for x in body_lines if x and not x.startswith("```"))
            table.source = body
            if table.source_kind != "calculation_group":
                table.source_kind, table.source_ref = parse_partition_source(header, body, expressions or {})
            i = j
            continue
        i += 1
    if table is not None:
        apply_lineage(table, _resolve_m(table.source or "", expressions or {}))
        if table.source_kind in FILE_KINDS:
            table.file_source = parse_file_source(_resolve_m(table.source or "", expressions or {}))
    return table


def parse_relationships(text: str) -> list[RelationshipMeta]:
    rels: list[RelationshipMeta] = []
    cur: dict = {}

    def flush():
        if "fromColumn" in cur and "toColumn" in cur:
            ft, fc = _split_col_ref(cur["fromColumn"])
            tt, tc = _split_col_ref(cur["toColumn"])
            rels.append(RelationshipMeta(
                from_table=ft, from_column=fc, to_table=tt, to_column=tc,
                cardinality=f"{cur.get('fromCardinality', 'many')}-to-{cur.get('toCardinality', 'one')}",
                cross_filter=cur.get("crossFilteringBehavior", "oneDirection"),
                active=cur.get("isActive", "true").lower() != "false"))

    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("relationship "):
            flush()
            cur = {}
        elif ":" in s:
            k, v = s.split(":", 1)
            cur[k.strip()] = v.strip()
    flush()
    return rels


def parse_expressions(text: str) -> dict[str, str]:
    """expressions.tmdl -> {name: M expression text} (shared queries and parameters)."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    lvl = _Indent(lines)
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if lvl(lines[i]) == 0 and s.startswith("expression "):
            name, expr = _split_name_expr(s[11:])
            body, j, _ = _collect_block(lines, i + 1, lvl, 0, expr)
            out[name] = body
            i = j
            continue
        i += 1
    return out


# --------------------------------------------------------------------------------- sources

def _param_value(expr: str) -> Optional[str]:
    m = re.match(r'^\s*"((?:[^"]|"")*)"\s*meta\s*\[', expr or "")
    return m.group(1).replace('""', '"') if m else None


def _resolve_m(body: str, expressions: dict[str, str], depth: int = 0) -> str:
    """Inline referenced shared expressions and parameter values so the source can be recognised."""
    if depth > 4 or not expressions:
        return body
    out = body
    for name, expr in expressions.items():
        val = _param_value(expr)
        pat = rf'#"{re.escape(name)}"' if not re.fullmatch(r"\w+", name) else rf'(?:#"{re.escape(name)}"|(?<![\w.#"]){re.escape(name)}(?![\w("]))'
        if not re.search(pat, out):
            continue
        if val is not None:
            out = re.sub(pat, '"' + val.replace("\\", "\\\\") + '"', out)
        else:
            out = out + "\n" + _resolve_m(expr, expressions, depth + 1)
    return out


def parse_partition_source(header: str, body: str, expressions: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """Identify the physical object behind a partition (Power Query M, DAX, entity)."""
    b = body or ""
    if re.search(r"=\s*calculated\b", header) or re.search(r"\b(CALENDAR|CALENDARAUTO|DATATABLE|GENERATESERIES)\s*\(", b, re.I) \
            and not re.search(r"\blet\b", b):
        return "calculated", None
    if re.search(r"=\s*entity\b", header) or re.search(r"^\s*entityName\s*:", b, re.M):
        ent = re.search(r"^\s*entityName\s*:\s*(.+)$", b, re.M)
        sch = re.search(r"^\s*schemaName\s*:\s*(.+)$", b, re.M)
        if ent:
            return "directlake", ".".join(_unquote(x.group(1).strip()) for x in (sch, ent) if x)
        return "directlake", None
    if re.search(r"=\s*calculationGroup\b", header):
        return "calculation_group", None
    b = _resolve_m(b, expressions or {})
    pos = b.find("Value.NativeQuery(")
    if pos >= 0:
        for lit in re.finditer(r'"((?:[^"]|"")*)"', b[pos:]):
            q = lit.group(1).replace('""', '"').replace("#(lf)", "\n").replace("#(cr)", "").replace("#(tab)", " ")
            if re.match(r"\s*(SELECT|WITH)\b", q, re.I):
                return "native_query", "(" + q.strip() + ")"
        return "native_query", None
    names = re.findall(r'\[\s*Name\s*=\s*"([^"]+)"', b)
    if re.search(r"Databricks(MultiCloud)?\.(Catalogs|Query|Contents)", b):
        return "databricks", ".".join(names[-3:]) if names else None
    if re.search(r"Snowflake\.Databases", b):
        return "snowflake", ".".join(names[-3:]) if names else None
    if re.search(r"GoogleBigQuery\.Database", b):
        return "bigquery", ".".join(names[-3:]) if names else None
    if re.search(r"AmazonRedshift\.Database", b):
        return "redshift", ".".join(names[-2:]) if names else None
    if re.search(r"(Lakehouse|Warehouse)\.Contents|Fabric\.", b):
        return "fabric", ".".join(names[-2:]) if names else None
    m = re.search(r'(Sql\.Databases?|AzureSqlDatabase\.Database|Oracle\.Database|PostgreSQL\.Database|MySQL\.Database|Teradata\.Database|DB2\.Database)\(\s*("([^"]*)"|[^,)]+)\s*(?:,\s*"([^"]+)")?', b)
    if m:
        kind = {"Oracle": "oracle", "PostgreSQL": "postgres", "MySQL": "mysql", "Teradata": "teradata", "DB2": "db2"}.get(m.group(1).split(".")[0], "sqlserver")
        item = re.search(r'Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"', b)
        q = re.search(r'\[\s*Query\s*=\s*"((?:[^"]|"")*)"', b, re.S)
        if q:
            return "native_query", "(" + q.group(1).replace('""', '"').replace("#(lf)", "\n") + ")"
        if item:
            return kind, ".".join(x for x in (m.group(4), item.group(1), item.group(2)) if x)
        return kind, None
    if re.search(r"AzureDataExplorer\.Contents|Kusto\.Contents", b):
        return "kusto", None
    if re.search(r"Binary\.Decompress\(\s*Binary\.FromText\(", b) and not re.search(r"File\.Contents|Web\.Contents|SharePoint\.|Folder\.Files", b):
        return "inline", None                      # "Enter data" table embedded in the model
    if re.search(r"(Csv\.Document|Excel\.Workbook|Parquet\.Document|Json\.Document|Folder\.Files|SharePoint\.Files|SharePoint\.Contents)", b):
        fs = parse_file_source(b)
        if fs and fs.get("file"):
            return fs["format"], file_table_name(fs)
        return "file", None
    if re.search(r"(Web\.Contents|OData\.Feed|SharePoint\.Tables|Salesforce\.)", b):
        return "web", None
    if re.search(r"#table\s*\(|Table\.FromRows|Table\.FromRecords", b):
        return "inline", None
    return "unknown", (".".join(names[-3:]) if len(names) >= 2 else None)


# --------------------------------------------------------------------------------- file sources (Excel / CSV / Parquet)

FILE_EXT = ("xlsx", "xlsm", "xlsb", "xls", "csv", "tsv", "txt", "parquet", "json")
FILE_KINDS = ("excel", "csv", "parquet", "json")


def _ident(x: str) -> str:
    v = re.sub(r"[^0-9a-z]+", "_", (x or "").lower()).strip("_") or "t"
    return ("t_" + v) if v[0].isdigit() else v


def file_table_name(fs: dict) -> str:
    """Stable SQL name for a file-backed table: <workbook>.<sheet or table> (e.g. sales_data.orders)."""
    stem = re.sub(r"\.[^.]+$", "", fs["file"])
    return f"{_ident(stem)}.{_ident(fs.get('item') or stem)}"


def parse_file_source(m: str) -> Optional[dict]:
    """Excel.Workbook / Csv.Document / Parquet.Document / Json.Document over File.Contents, Web.Contents or
    SharePoint.Files -> {format, file, path, item, item_kind, header, skip, delimiter}."""
    b = m or ""
    if re.search(r"Folder\.Files|Table\.Combine|Table\.ExpandTableColumn\([^)]*Transform File", b):
        if not re.search(r'\[\s*Name\s*=\s*"[^"]+\.(?:%s)"' % "|".join(FILE_EXT), b, re.I):
            return {"format": "folder", "file": None, "note": "combines every file in a folder"}
    fmt = ("excel" if re.search(r"Excel\.Workbook", b) else "csv" if re.search(r"Csv\.Document", b) else
           "parquet" if re.search(r"Parquet\.Document", b) else "json" if re.search(r"Json\.Document", b) else None)
    if not fmt:
        return None
    lits = [x.replace('""', '"') for x in re.findall(r'"((?:[^"]|"")*\.(?:%s))"' % "|".join(FILE_EXT), b, re.I)]
    path = lits[0] if lits else None
    name = re.search(r'\[\s*Name\s*=\s*"([^"]+)"', b)
    if name and re.search(r"\.(%s)$" % "|".join(FILE_EXT), name.group(1), re.I) and not path:
        path = name.group(1)
    if not path:
        return {"format": fmt, "file": None}
    from urllib.parse import unquote
    file = unquote(re.split(r"[\\/]", path)[-1])
    out: dict = {"format": fmt, "file": file, "path": path, "item": None, "item_kind": None, "header": False, "skip": 0}
    it = re.search(r'\{\s*\[\s*Item\s*=\s*"((?:[^"]|"")*)"\s*,\s*Kind\s*=\s*"(\w+)"\s*\]\s*\}', b) or \
        re.search(r'\{\s*\[\s*Name\s*=\s*"((?:[^"]|"")*)"\s*,\s*Kind\s*=\s*"(\w+)"\s*\]\s*\}', b)
    if it:
        out["item"], out["item_kind"] = it.group(1).replace('""', '"'), it.group(2)
    if re.search(r"Table\.PromoteHeaders", b) or out["item_kind"] == "Table" or \
            re.search(r'Excel\.Workbook\((?:[^()]|\([^()]*\))*?,\s*true', b):
        out["header"] = True
    sk = re.search(r"Table\.Skip\(\s*[^,]+,\s*(\d+)\s*\)", b)
    if sk:
        out["skip"] = int(sk.group(1))
    d = re.search(r'Delimiter\s*=\s*"((?:[^"]|"")*)"', b)
    if d:
        out["delimiter"] = d.group(1).replace("#(tab)", "\t")
    elif fmt == "csv" and re.search(r"Csv\.Document\([^,]+,\s*\[?\s*\"#\(tab\)\"", b):
        out["delimiter"] = "\t"
    if re.search(r"Table\.SelectRows|Table\.Distinct|Table\.Group|Table\.NestedJoin|Table\.Pivot|Table\.Unpivot", b):
        out["transforms"] = sorted(set(re.findall(r"Table\.(SelectRows|Distinct|Group|NestedJoin|Pivot|UnpivotOtherColumns|Unpivot)", b)))
    return out


# --------------------------------------------------------------------------------- TMSL (model.bim)

def _expr(v: Any) -> str:
    return "\n".join(v) if isinstance(v, list) else (v or "")


def read_bim(path: Path) -> ModelMetadata:
    data = json.loads(_read(path))
    m = data.get("model", data)
    meta = ModelMetadata(platform="powerbi", model_name=data.get("name") or path.parent.name.rsplit(".", 1)[0])
    expressions = {e.get("name"): _expr(e.get("expression")) for e in m.get("expressions", []) if e.get("name")}
    for t in m.get("tables", []):
        try:
            tm = TableMeta(name=t["name"], is_date_table=(t.get("dataCategory") == "Time"), hidden=bool(t.get("isHidden")),
                           hierarchies=[{"name": h.get("name"), "levels": [{"name": lv.get("name"), "column": lv.get("column", lv.get("name"))}
                                                                         for lv in h.get("levels", [])]} for h in t.get("hierarchies", [])])
            if t.get("calculationGroup"):
                tm.source_kind = "calculation_group"
            for c in t.get("columns", []):
                if c.get("type") == "rowNumber":
                    continue
                tm.columns.append(ColumnMeta(name=c["name"], data_type=c.get("dataType"),
                                             expression=_expr(c.get("expression")) if c.get("type") == "calculated" else None,
                                             source_column=unbracket(c.get("sourceColumn", "")) or None,
                                             sort_by=c.get("sortByColumn"), hidden=bool(c.get("isHidden"))))
            for ms in t.get("measures", []):
                tm.measures.append(MeasureMeta(name=ms["name"], table=t["name"], expression=_expr(ms.get("expression")),
                                               language="dax", format_string=ms.get("formatString"), source_file=path.name,
                                               hidden=bool(ms.get("isHidden"))))
            for p in t.get("partitions", [])[:1]:
                src = p.get("source", {})
                typ = src.get("type", "m")
                body = _expr(src.get("expression") or src.get("query"))
                if typ == "entity":
                    body = f"entityName: {src.get('entityName', '')}\nschemaName: {src.get('schemaName', '')}"
                tm.source = body
                if tm.source_kind != "calculation_group":
                    tm.source_kind, tm.source_ref = parse_partition_source(f"partition x = {typ}", body, expressions)
                apply_lineage(tm, _resolve_m(body, expressions) if typ == "m" else "")
                if tm.source_kind in FILE_KINDS:
                    tm.file_source = parse_file_source(_resolve_m(body, expressions))
            meta.tables.append(tm)
        except Exception as e:  # noqa: BLE001
            meta.unsupported.append(f"Table {t.get('name')}: could not be read ({type(e).__name__})")
    for role in m.get("roles", []):
        for tp in role.get("tablePermissions", []):
            if tp.get("filterExpression"):
                meta.roles.append({"role": role.get("name"), "table": tp.get("name"), "expression": _expr(tp.get("filterExpression"))})
    for r in m.get("relationships", []):
        try:
            meta.relationships.append(RelationshipMeta(
                from_table=r["fromTable"], from_column=r["fromColumn"], to_table=r["toTable"], to_column=r["toColumn"],
                cardinality=f"{r.get('fromCardinality', 'many')}-to-{r.get('toCardinality', 'one')}",
                cross_filter=r.get("crossFilteringBehavior", "oneDirection"), active=r.get("isActive", True)))
        except KeyError:
            continue
    return meta


# --------------------------------------------------------------------------------- reports

AGG_FUNCTION = {0: "SUM", 1: "AVG", 2: "COUNT_DISTINCT", 3: "MIN", 4: "MAX", 5: "COUNT"}


def _walk_fields(node, measures: list[str], columns: list[str], aliases: Optional[dict] = None, aggs: Optional[dict] = None):
    aliases = aliases or {}
    if isinstance(node, dict):
        if "Aggregation" in node and isinstance(node["Aggregation"], dict):
            inner_m, inner_c = [], []
            _walk_fields(node["Aggregation"].get("Expression", {}), inner_m, inner_c, aliases)
            for c in inner_c:
                ref = f"implicit:{c}"
                if ref not in measures:
                    measures.append(ref)
                if aggs is not None:
                    aggs[ref] = AGG_FUNCTION.get(node["Aggregation"].get("Function"), "SUM")
            return
        if "HierarchyLevel" in node and isinstance(node["HierarchyLevel"], dict):
            hl = node["HierarchyLevel"]
            h = hl.get("Expression", {}).get("Hierarchy", {})
            src = h.get("Expression", {})
            pvs = src.get("PropertyVariationSource")
            if pvs:
                ent = aliases.get(pvs.get("Expression", {}).get("SourceRef", {}).get("Source"), pvs.get("Expression", {}).get("SourceRef", {}).get("Entity", ""))
                ref = f"{ent}[{pvs.get('Property', '')}].{hl.get('Level', '')}"
            else:
                sr = src.get("SourceRef", {})
                ent = aliases.get(sr.get("Source"), sr.get("Entity", ""))
                ref = f"{ent}[{hl.get('Level', '')}]"
            if ref not in columns:
                columns.append(ref)
            return
        for kind, target in (("Measure", measures), ("Column", columns)):
            if kind in node and isinstance(node[kind], dict):
                spec = node[kind]
                if "Property" not in spec:          # wrapped, e.g. SparklineData.Measure.Measure
                    _walk_fields(spec, measures, columns, aliases, aggs)
                    continue
                sr = spec.get("Expression", {}).get("SourceRef", {})
                entity = sr.get("Entity") or aliases.get(sr.get("Source"), "")
                prop = spec.get("Property", "")
                ref = f"[{prop}]" if kind == "Measure" else f"{entity}[{prop}]"
                if ref not in target:
                    target.append(ref)
                return
        for v in node.values():
            _walk_fields(v, measures, columns, aliases, aggs)
    elif isinstance(node, list):
        for v in node:
            _walk_fields(v, measures, columns, aliases, aggs)


def _lit(v: Any) -> tuple[bool, Any]:
    if not isinstance(v, dict) or "Literal" not in v:
        return False, None
    raw = str(v["Literal"].get("Value", ""))
    if raw.startswith("'") and raw.endswith("'"):
        return True, raw[1:-1].replace("''", "'")
    m = re.match(r"^datetime'(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return True, m.group(1)
    if raw in ("true", "false"):
        return True, raw == "true"
    if raw == "null":
        return True, None
    m = re.match(r"^(-?\d+)L$", raw)
    if m:
        return True, int(m.group(1))
    m = re.match(r"^(-?\d+(?:\.\d+)?)[DM]$", raw)
    if m:
        return True, float(m.group(1))
    return False, None


def _ref(expr: dict, aliases: dict) -> tuple[Optional[str], Optional[str]]:
    """-> ("Table[Col]", None) for a column, (None, "measure name") for a measure."""
    for kind in ("Column", "Measure"):
        spec = expr.get(kind) if isinstance(expr, dict) else None
        if isinstance(spec, dict):
            sr = spec.get("Expression", {}).get("SourceRef", {})
            ent = sr.get("Entity") or aliases.get(sr.get("Source"), "")
            return (f"{ent}[{spec.get('Property', '')}]", None) if kind == "Column" else (None, spec.get("Property"))
    return None, None


def _conditions(cond: dict, aliases: dict, specs: list, notes: list, level: str, neg: bool = False):
    if "And" in cond:
        _conditions(cond["And"]["Left"], aliases, specs, notes, level, neg)
        _conditions(cond["And"]["Right"], aliases, specs, notes, level, neg)
        return
    if "Not" in cond:
        _conditions(cond["Not"].get("Expression", {}), aliases, specs, notes, level, not neg)
        return
    if "In" in cond:
        exprs, vals = cond["In"].get("Expressions", []), cond["In"].get("Values", [])
        if len(exprs) == 1:
            col, meas = _ref(exprs[0], aliases)
            lits = [_lit(v[0]) for v in vals if v]
            if col and lits and all(ok for ok, _ in lits):
                values = [x for _, x in lits]
                if values == [None]:
                    specs.append({"ref": col, "op": "is not null" if neg else "is null", "value": None, "level": level})
                else:
                    specs.append({"ref": col, "op": ("not in" if neg else "in") if len(values) > 1 else ("!=" if neg else "="),
                                  "value": values if len(values) > 1 else values[0], "level": level})
                return
            if meas:
                notes.append(f"{level} filter on measure [{meas}] is not applied to the source SQL")
                return
        notes.append(f"{level} filter with a multi-column/complex condition is not applied")
        return
    if "Comparison" in cond:
        c = cond["Comparison"]
        col, meas = _ref(c.get("Left", {}), aliases)
        ok, val = _lit(c.get("Right", {}))
        op = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}.get(c.get("ComparisonKind"))
        if col and ok and op:
            if neg:
                op = {"=": "!=", ">": "<=", ">=": "<", "<": ">=", "<=": ">"}[op]
            specs.append({"ref": col, "op": op, "value": val, "level": level})
            return
        if meas:
            notes.append(f"{level} filter on measure [{meas}] is not applied to the source SQL")
            return
    notes.append(f"{level} filter of a type not translated (e.g. Top N, relative date) is not applied")


def parse_filters(container: Any, level: str) -> tuple[list[dict], list[str]]:
    """PBIR filterConfig / legacy filters (dict, list or JSON string) -> filters actually in force."""
    specs: list[dict] = []
    notes: list[str] = []
    if isinstance(container, str):
        try:
            container = json.loads(container) if container.strip() else []
        except ValueError:
            return specs, [f"{level} filters could not be read"]
    items = container.get("filters", []) if isinstance(container, dict) else (container or [])
    for f in items:
        flt = f.get("filter") if isinstance(f, dict) else None
        if not flt:
            continue                                  # filter card with nothing selected
        aliases = {x.get("Name"): x.get("Entity") for x in flt.get("From", [])}
        for w in flt.get("Where", []):
            try:
                _conditions(w.get("Condition", {}), aliases, specs, notes, level)
            except (KeyError, TypeError, AttributeError):
                notes.append(f"{level} filter could not be interpreted")
    return specs, notes


def _slicer_selection(objects: Any) -> Any:
    try:
        return {"filters": [{"filter": objects["general"][0]["properties"]["filter"]["filter"]}]}
    except (KeyError, IndexError, TypeError):
        return None


def _title(v: dict) -> Optional[str]:
    for key in ("visualContainerObjects", "vcObjects"):
        try:
            return v[key]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"].strip("'")
        except (KeyError, IndexError, TypeError):
            continue
    return None


LEGACY_INTERACTION = {0: "Default", 1: "DataFilter", 2: "HighlightFilter", 3: "NoFilter"}


def collect_refs(node: Any, aliases: Optional[dict] = None, out: Optional[list] = None) -> list[str]:
    """Every model field referenced anywhere in a report JSON fragment - projections, filters, sort,
    conditional formatting, tooltips, slicer selections - as m||Measure, c|Table|Column, h|Table|Hierarchy|Level.
    Legacy reports keep nested JSON in strings; those are decoded too."""
    out = [] if out is None else out
    aliases = dict(aliases or {})
    if isinstance(node, str):
        t = node.strip()
        if t[:1] in "[{" and '"Property"' in t or ('"Level"' in t and t[:1] in "[{"):
            try:
                collect_refs(json.loads(t), aliases, out)
            except ValueError:
                pass
        return out
    if isinstance(node, list):
        for x in node:
            collect_refs(x, aliases, out)
        return out
    if not isinstance(node, dict):
        return out
    frm = node.get("From")
    if isinstance(frm, list):
        aliases.update({f.get("Name"): f.get("Entity") for f in frm if isinstance(f, dict) and f.get("Name") and f.get("Entity")})
    ent = lambda sr: (sr or {}).get("Entity") or aliases.get((sr or {}).get("Source"), "")  # noqa: E731
    for kind in ("Measure", "Column"):
        spec = node.get(kind)
        if isinstance(spec, dict) and "Property" in spec:
            sr = (spec.get("Expression") or {}).get("SourceRef")
            r = f"m||{spec['Property']}" if kind == "Measure" else f"c|{ent(sr)}|{spec['Property']}"
            if r not in out:
                out.append(r)
    hl = node.get("HierarchyLevel")
    if isinstance(hl, dict):
        h = (hl.get("Expression") or {}).get("Hierarchy") or {}
        src = h.get("Expression") or {}
        pvs = src.get("PropertyVariationSource")
        if pvs:                                          # auto date/time hierarchy on a date column
            r = f"c|{ent((pvs.get('Expression') or {}).get('SourceRef'))}|{pvs.get('Property', '')}"
        else:
            r = f"h|{ent(src.get('SourceRef'))}|{h.get('Hierarchy', '')}|{hl.get('Level', '')}"
        if r not in out:
            out.append(r)
    for k, v in node.items():
        if k != "From":
            collect_refs(v, aliases, out)
    return out


def parse_roles(defn: Path) -> list[dict]:
    """definition/roles/*.tmdl: tablePermission <Table> = <DAX filter>."""
    out = []
    for f in sorted((defn / "roles").glob("*.tmdl")) if (defn / "roles").exists() else []:
        lines = _read(f).splitlines()
        role = f.stem
        for i, raw in enumerate(lines):
            st = raw.strip()
            if st.startswith("role "):
                role = _unquote(st[5:])
            m = re.match(r"tablePermission\s+('(?:[^']|'')+'|[^=\s]+)\s*=\s*(.*)$", st)
            if m:
                body = [m.group(2)] if m.group(2) else []
                base = len(raw) - len(raw.lstrip("\t "))
                for nxt in lines[i + 1:]:
                    if nxt.strip() and len(nxt) - len(nxt.lstrip("\t ")) <= base:
                        break
                    body.append(nxt.strip())
                out.append({"role": role, "table": _unquote(m.group(1)), "expression": "\n".join(b for b in body if b)})
    return out


def _interactions(items: Any) -> dict[str, dict[str, str]]:
    """visualInteractions (PBIR) / section relationships (legacy) -> {source: {target: type}}."""
    out: dict[str, dict[str, str]] = {}
    for it in items or []:
        if not isinstance(it, dict) or not it.get("source") or not it.get("target"):
            continue
        typ = it.get("type")
        typ = LEGACY_INTERACTION.get(typ, str(typ)) if isinstance(typ, int) else str(typ or "Default")
        out.setdefault(it["source"], {})[it["target"]] = typ
    return out


def _selection_values(specs: list[dict]) -> list:
    vals = []
    for sp in specs:
        v = sp.get("value")
        vals += v if isinstance(v, list) else [v]
    return [x for x in vals if x is not None][:20]


def _sync_group(v: dict) -> Optional[str]:
    try:
        return (v.get("syncGroup") or {}).get("groupName")
    except AttributeError:
        return None


def _finish_page(page_visuals: list[VisualMeta], base: list[dict], base_notes: list[str], slicers: list[tuple[str, list]],
                 inter: Optional[dict] = None):
    inter = inter or {}
    by_id = {v.id: v for v in page_visuals}
    for sid, targets in inter.items():
        if sid in by_id:
            by_id[sid].interactions = targets
    for v in page_visuals:
        extra = [dict(x, level="slicer") for sid, sp in slicers if sid != v.id
                 and inter.get(sid, {}).get(v.id) != "NoFilter" for x in sp]       # visuals set to ignore the slicer
        v.filter_specs = base + extra + v.filter_specs
        v.filter_notes = base_notes + v.filter_notes


def parse_pbir_report(report_dir: Path, notes: Optional[list] = None) -> tuple[list[VisualMeta], list[dict]]:
    visuals: list[VisualMeta] = []
    pages_dir = report_dir / "definition" / "pages"
    pages = []
    if not pages_dir.exists():
        return visuals, pages
    rep_specs, rep_notes = [], []
    rj = report_dir / "definition" / "report.json"
    if rj.exists():
        try:
            _rj = json.loads(_read(rj))
            rep_specs, rep_notes = parse_filters(_rj.get("filterConfig", {}), "report")
            pages.append({"id": "__report__", "name": "(report)", "hidden": False, "filter_refs": collect_refs(_rj.get("filterConfig", {}))})
        except ValueError:
            pass
    for page_dir in sorted(p for p in pages_dir.iterdir() if p.is_dir()):
        page_name, hidden, pg_specs, pg_notes, inter, pg_refs = page_dir.name, False, [], [], {}, []
        try:
            if (page_dir / "page.json").exists():
                pj = json.loads(_read(page_dir / "page.json"))
                page_name = pj.get("displayName", page_name)
                inter = _interactions(pj.get("visualInteractions"))
                hidden = pj.get("visibility") == "HiddenInViewMode" or pj.get("type") == "Tooltip"
                pg_specs, pg_notes = parse_filters(pj.get("filterConfig", {}), "page")
                pg_refs = collect_refs(pj.get("filterConfig", {}))
        except ValueError:
            pass
        pages.append({"id": page_dir.name, "name": page_name, "hidden": hidden, "filter_refs": pg_refs})
        page_visuals, slicers = [], []
        for vjson in sorted((page_dir / "visuals").glob("*/visual.json")):
            try:
                data = json.loads(_read(vjson))
                v = data.get("visual", {})
                measures, columns, aggs = [], [], {}
                _walk_fields(v.get("query", {}), measures, columns, None, aggs)
                fm, fc = [], []
                _walk_fields(data.get("filterConfig", {}), fm, fc)
                if not v.get("visualType") and not measures and not columns:
                    continue  # groups / shapes
                vs, vn = parse_filters(data.get("filterConfig", {}), "visual")
                vm = VisualMeta(id=data.get("name", vjson.parent.name), page=page_name, visual_type=v.get("visualType", "unknown"),
                                title=_title(v), measures=measures, dimensions=columns, filters=fc, filter_specs=vs,
                                filter_notes=vn, implicit_aggs=aggs, page_hidden=hidden, page_id=page_dir.name,
                                sync_group=_sync_group(v), refs=collect_refs(data))
                if is_slicer(vm.visual_type):
                    sel = _slicer_selection(v.get("objects"))
                    if sel:
                        sp = parse_filters(sel, "slicer")[0]
                        vm.selection = _selection_values(sp)
                        slicers.append((vm.id, sp))
                page_visuals.append(vm)
            except Exception as e:  # noqa: BLE001
                if notes is not None:
                    notes.append(f"Visual {vjson.parent.name} on '{page_name}' could not be read ({type(e).__name__})")
        _finish_page(page_visuals, rep_specs + pg_specs, rep_notes + pg_notes, slicers, inter)
        visuals += page_visuals
    return visuals, pages


def report_measures(report_dir: Path) -> list[MeasureMeta]:
    """Measures defined in the report itself (report extensions), not in the semantic model."""
    out: list[MeasureMeta] = []
    sources = []
    ext = report_dir / "definition" / "reportExtensions.json"
    if ext.exists():
        sources.append(json.loads(_read(ext)))
    rj = report_dir / "report.json"
    if rj.exists():
        cfg = json.loads(_read(rj)).get("config")
        cfg = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
        sources += cfg.get("modelExtensions", [])
    for src in sources:
        for ent in src.get("entities", []):
            for m in ent.get("measures", []):
                if m.get("name"):
                    out.append(MeasureMeta(name=m["name"], table=ent.get("name", "Report"), expression=m.get("expression", ""),
                                           language="dax", format_string=m.get("formatString"), source_file="report-extension"))
    return out


def parse_legacy_report(report_json: Path, notes: Optional[list] = None) -> tuple[list[VisualMeta], list[dict]]:
    visuals, pages = [], []
    data = json.loads(_read(report_json))
    rep_specs, rep_notes = parse_filters(data.get("filters", "[]"), "report")
    pages.append({"id": "__report__", "name": "(report)", "hidden": False, "filter_refs": collect_refs(data.get("filters", "[]"))})
    for si, sec in enumerate(data.get("sections", [])):
        page = sec.get("displayName") or sec.get("name") or f"Page {si + 1}"
        try:
            scfg = json.loads(sec.get("config") or "{}") if isinstance(sec.get("config"), str) else (sec.get("config") or {})
        except ValueError:
            scfg = {}
        hidden = scfg.get("visibility") == 1
        inter = _interactions(scfg.get("relationships"))
        pg_specs, pg_notes = parse_filters(sec.get("filters", "[]"), "page")
        pages.append({"id": sec.get("name", str(si)), "name": page, "hidden": hidden, "filter_refs": collect_refs(sec.get("filters", "[]"))})
        page_visuals, slicers = [], []
        for vi, vc in enumerate(sec.get("visualContainers", [])):
            try:
                cfg = vc.get("config")
                cfg = json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
                sv = cfg.get("singleVisual") or {}
                if not sv:
                    continue
                q = sv.get("prototypeQuery", {})
                aliases = {f.get("Name"): f.get("Entity") for f in q.get("From", [])}
                measures, columns, aggs = [], [], {}
                _walk_fields(q.get("Select", []), measures, columns, aliases, aggs)
                fl = vc.get("filters")
                fc: list[str] = []
                if isinstance(fl, str) and fl.strip().startswith("["):
                    _walk_fields(json.loads(fl), [], fc)
                vs, vn = parse_filters(fl or "[]", "visual")
                vm = VisualMeta(id=cfg.get("name", f"{si}-{vi}"), page=page, visual_type=sv.get("visualType", "unknown"),
                                title=_title(sv), measures=measures, dimensions=columns, filters=fc, filter_specs=vs,
                                filter_notes=vn, implicit_aggs=aggs, page_hidden=hidden, page_id=sec.get("name", str(si)),
                                sync_group=_sync_group(sv), refs=collect_refs([cfg, vc.get("filters")]))
                if is_slicer(vm.visual_type):
                    sel = _slicer_selection(sv.get("objects"))
                    if sel:
                        sp = parse_filters(sel, "slicer")[0]
                        vm.selection = _selection_values(sp)
                        slicers.append((vm.id, sp))
                page_visuals.append(vm)
            except Exception as e:  # noqa: BLE001
                if notes is not None:
                    notes.append(f"Visual {vi} on '{page}' could not be read ({type(e).__name__})")
        _finish_page(page_visuals, rep_specs + pg_specs, rep_notes + pg_notes, slicers, inter)
        visuals += page_visuals
    return visuals, pages


# --------------------------------------------------------------------------------- entry point

def locate_pbip(root: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Find the semantic-model definition (TMDL folder or model.bim) and the report folder under root."""
    by_depth = lambda p: (len(p.parts), str(p))  # noqa: E731
    tables = sorted(root.rglob("definition/tables"), key=by_depth)
    defn: Optional[Path] = tables[0].parent if tables else None
    if defn is None:
        bims = sorted(list(root.rglob("model.bim")) + list(root.rglob("*.bim")), key=by_depth)
        if bims:
            defn = bims[0]
    if defn is None:
        tmdl = sorted(root.rglob("*.tmdl"), key=by_depth)
        defn = tmdl[0].parent if tmdl else None
    reports = sorted([p.parent.parent for p in root.rglob("definition/pages")] + [p.parent for p in root.rglob("report.json")], key=by_depth)
    return defn, (reports[0] if reports else None)


def read_pbip(project_dir: str | Path) -> ModelMetadata:
    root = Path(project_dir)
    defn, report = locate_pbip(root)
    if defn is None:
        if report is not None:
            raise FileNotFoundError("Only a report was found (thin report connected to a published model). Upload the "
                                    "PBIP of the semantic model, or the report together with its *.SemanticModel folder.")
        raise FileNotFoundError("No semantic model found. Upload the PBIP project folder or a .zip of it - it must contain "
                                "*.SemanticModel (or *.Dataset) with definition/*.tmdl or model.bim.")
    notes: list[str] = []
    if defn.is_file():                                   # model.bim
        meta = read_bim(defn)
        sm_dir = defn.parent
    else:
        sm_dir = next((p for p in [defn, *defn.parents] if p.name.endswith((".SemanticModel", ".Dataset"))), defn.parent)
        meta = ModelMetadata(platform="powerbi", model_name=re.sub(r"\.(SemanticModel|Dataset)$", "", sm_dir.name))
        expressions: dict[str, str] = {}
        for ef in [defn / "expressions.tmdl", *defn.glob("expressions*.tmdl")]:
            if ef.exists():
                try:
                    expressions.update(parse_expressions(_read(ef)))
                except Exception as e:  # noqa: BLE001
                    notes.append(f"{ef.name}: shared expressions not read ({type(e).__name__})")
        files = sorted((defn / "tables").glob("*.tmdl")) if (defn / "tables").exists() else sorted(defn.glob("*.tmdl"))
        for f in files:
            try:
                txt = _read(f)
                if txt.lstrip().startswith(("table ", "///")) and re.search(r"^table ", txt, re.M):
                    t = parse_tmdl_table(txt, source_file=f.name, expressions=expressions)
                    if t:
                        meta.tables.append(t)
                elif "relationship " in txt and not (defn / "relationships.tmdl").exists():
                    meta.relationships += parse_relationships(txt)
            except Exception as e:  # noqa: BLE001
                notes.append(f"{f.name}: could not be parsed ({type(e).__name__}: {str(e)[:80]})")
        try:
            meta.roles = parse_roles(defn)
        except Exception as e:  # noqa: BLE001
            notes.append(f"Security roles could not be read ({type(e).__name__})")
        rel = defn / "relationships.tmdl"
        if rel.exists():
            try:
                meta.relationships = parse_relationships(_read(rel))
            except Exception as e:  # noqa: BLE001
                notes.append(f"relationships.tmdl could not be parsed ({type(e).__name__})")
    sms = [p for p in root.rglob("*") if p.is_dir() and p.name.endswith((".SemanticModel", ".Dataset"))]
    if len(sms) > 1:
        notes.append(f"{len(sms)} semantic models found; using '{sm_dir.name}'. Upload one model per project.")
    if report:
        try:
            if (report / "definition" / "pages").exists():
                meta.visuals, pages = parse_pbir_report(report, notes)
            else:
                meta.visuals, pages = parse_legacy_report(report / "report.json", notes)
            rms = []
            try:
                rms = report_measures(report)
            except Exception:  # noqa: BLE001
                pass
            meta.report_refs = [{"ref": r, "where": "Report filter" if pg["id"] == "__report__" else f"Page filter on '{pg['name']}'"}
                                for pg in pages for r in pg.get("filter_refs", [])]
            pages = [{k: v for k, v in pg.items() if k != "filter_refs"} for pg in pages if pg["id"] != "__report__"]
            meta.reports.append({"name": re.sub(r"\.Report$", "", report.name), "pages": pages,
                                 "report_measures": [{"name": m.name, "table": m.table, "expression": m.expression} for m in rms]})
            if rms:
                notes.append(f"{len(rms)} report-level measure(s) found in the report (not in the semantic model) - listed, not validated.")
        except Exception as e:  # noqa: BLE001
            notes.append(f"Report could not be read ({type(e).__name__}: {str(e)[:80]}) - visual tests skipped")
    else:
        notes.append("No report folder found - chart/visual tests are not generated (model-level tests still are).")
    calc_groups = [t.name for t in meta.tables if t.source_kind == "calculation_group"]
    if calc_groups:
        notes.append(f"Calculation groups ({', '.join(calc_groups)}) are listed but their items are not tested yet.")
    meta.unsupported += notes
    return meta
