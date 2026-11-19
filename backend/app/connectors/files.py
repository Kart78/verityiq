"""File data platform: Excel / CSV / Parquet / JSON sources behind a Power BI model.

Each file-backed model table is loaded into a local DuckDB cache exactly as Power Query reads it
(sheet or named table, header row promoted or not, top rows skipped), then the generated Databricks
SQL is transpiled to DuckDB and executed against it. Expected values are therefore computed from the
same workbook the dashboard was built on, independently of Power BI.

Where the file is looked for, in order:
  1. files uploaded for the project (data/projects/<id>/sources/, matched by file name, any sub-folder)
  2. the original path written in the Power Query (works when the app runs on the same computer)
The cache is rebuilt for a table whenever its file's size/modified time or read options change.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import duckdb
import sqlglot

from ..core.models import ColumnMeta, RunMode, SourceTableMeta
from ..engine.sql_safety import assert_read_only
from .base import Capability, ConnectionTestResult, ConnectorError, DataConnector, QueryResult

META = "_verityiq_meta"


def _q(x: str) -> str:
    return '"' + x.replace('"', '""') + '"'


def _find(folder: Path, name: str, deep: bool = True) -> Optional[Path]:
    try:
        it = folder.rglob("*") if deep else folder.iterdir()
        hits = [p for p in it if p.is_file() and p.name.lower() == name]
    except OSError:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def locate(fs: dict, folder: Optional[Path], source_folder: Optional[str] = None) -> tuple[Optional[Path], str]:
    """-> (path, where) where is 'uploaded' | 'source folder' | 'original path' | ''."""
    name = (fs.get("file") or "").lower()
    if folder and folder.exists() and name:
        hit = _find(folder, name)
        if hit:
            return hit, "uploaded"
    for sf in [x.strip().strip('"') for x in (source_folder or "").split(";") if x.strip()]:
        try:
            d = Path(sf)
            if d.is_dir() and name:
                hit = _find(d, name)
                if hit:
                    return hit, "source folder"
        except OSError:
            pass
    orig = fs.get("path") or ""
    if orig and not orig.lower().startswith(("http://", "https://")):
        try:
            p = Path(orig)
            if p.is_file():
                return p, "original path"
        except OSError:
            pass
    return None, ""


# ------------------------------------------------------------------------------------------ readers

def _header_names(raw: list[Any], width: int, header: bool) -> list[str]:
    """Power Query naming: promoted headers as text, blanks -> ColumnN, duplicates -> name_1, name_2."""
    names, seen = [], {}
    for i in range(width):
        v = raw[i] if header and i < len(raw) else None
        if isinstance(v, dt.datetime):
            v = v.strftime("%m/%d/%Y %H:%M:%S") if v.time() != dt.time() else v.strftime("%m/%d/%Y")
        elif isinstance(v, float) and v.is_integer():
            v = int(v)
        n = str(v) if v not in (None, "") else f"Column{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 0
        names.append(n)
    return names


def _excel_rows(path: Path, fs: dict) -> tuple[list[list[Any]], list[str]]:
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover
        raise ConnectorError("openpyxl is not installed (pip install -r requirements.txt)") from e
    if path.suffix.lower() in (".xls", ".xlsb"):
        raise ConnectorError(f"{path.name}: old .xls/.xlsb workbooks can't be read - open it in Excel and save as .xlsx")
    kind, item = (fs.get("item_kind") or "Sheet"), fs.get("item")
    notes: list[str] = []
    if kind in ("Table", "DefinedName"):
        wb = openpyxl.load_workbook(path, data_only=True)        # tables need the full (non read-only) reader
        rng = None
        if kind == "Table":
            for ws in wb.worksheets:
                for tname, ref in ws.tables.items():
                    if tname.lower() == (item or "").lower():
                        rng = (ws, ref if isinstance(ref, str) else ref.ref)
        else:
            dn = wb.defined_names.get(item) if hasattr(wb.defined_names, "get") else None
            if dn is not None:
                for sheet, ref in dn.destinations:
                    rng = (wb[sheet], ref.replace("$", ""))
        if not rng:
            raise ConnectorError(f"{path.name}: {kind.lower()} '{item}' not found")
        rows = [[c.value for c in r] for r in rng[0][rng[1]]]
        wb.close()
    else:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        names = wb.sheetnames
        if item and item in names:
            ws = wb[item]
        elif item and item.lower() in [n.lower() for n in names]:
            ws = wb[next(n for n in names if n.lower() == item.lower())]
        elif not item:
            ws = wb[names[0]]
            notes.append(f"no sheet named in Power Query - read first sheet '{names[0]}'")
        else:
            wb.close()
            raise ConnectorError(f"{path.name}: sheet '{item}' not found (sheets: {', '.join(names)})")
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    while rows and all(v in (None, "") for v in rows[-1]):       # trailing empty rows
        rows.pop()
    return rows, notes


def _csv_rows(path: Path, fs: dict) -> list[list[Any]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    delim = fs.get("delimiter") or ("\t" if path.suffix.lower() == ".tsv" else None)
    if not delim:
        try:
            delim = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
    return [list(r) for r in csv.reader(text.splitlines(), delimiter=delim)]


def _infer(values: list[Any]) -> str:
    kinds = set()
    for v in values:
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            kinds.add("bool")
        elif isinstance(v, int):
            kinds.add("int")
        elif isinstance(v, float):
            kinds.add("int" if v.is_integer() else "float")
        elif isinstance(v, dt.datetime):
            kinds.add("date" if v.time() == dt.time() else "ts")
        elif isinstance(v, dt.date):
            kinds.add("date")
        else:
            kinds.add("str")
    if not kinds or "str" in kinds:
        return "VARCHAR"
    if kinds == {"bool"}:
        return "BOOLEAN"
    if kinds <= {"int"}:
        return "BIGINT"
    if kinds <= {"int", "float"}:
        return "DOUBLE"
    if kinds == {"date"}:
        return "DATE"
    if kinds <= {"date", "ts"}:
        return "TIMESTAMP"
    return "VARCHAR"


def _cell(v: Any, typ: str) -> str:
    if v is None or v == "":
        return ""
    if typ == "DATE":
        return (v.date() if isinstance(v, dt.datetime) else v).isoformat()
    if typ == "TIMESTAMP":
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else dt.datetime.combine(v, dt.time()).isoformat(sep=" ")
    if typ == "BIGINT":
        return str(int(v))
    if typ == "BOOLEAN":
        return "true" if v else "false"
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d") if v.time() == dt.time() else v.isoformat(sep=" ")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def load_table(con: duckdb.DuckDBPyConnection, schema: str, table: str, path: Path, fs: dict) -> dict:
    fmt = fs.get("format") or {".csv": "csv", ".tsv": "csv", ".txt": "csv", ".parquet": "parquet", ".json": "json"}.get(path.suffix.lower(), "excel")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}")
    target = f"{_q(schema)}.{_q(table)}"
    con.execute(f"DROP TABLE IF EXISTS {target}")
    if fmt == "parquet":
        con.execute(f"CREATE TABLE {target} AS SELECT * FROM read_parquet(?)", [str(path)])
        return {"rows": con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0], "notes": []}
    if fmt == "json":
        con.execute(f"CREATE TABLE {target} AS SELECT * FROM read_json_auto(?)", [str(path)])
        return {"rows": con.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0], "notes": []}
    notes: list[str] = []
    if fmt == "excel":
        rows, notes = _excel_rows(path, fs)
    else:
        rows = _csv_rows(path, fs)
    rows = rows[int(fs.get("skip") or 0):]
    width = max((len(r) for r in rows), default=0)
    header = bool(fs.get("header"))
    names = _header_names(rows[0] if rows else [], width, header)
    data = rows[1:] if header and rows else rows
    data = [r + [None] * (width - len(r)) for r in data]
    types = [_infer([r[i] for r in data]) if fmt == "excel" else "VARCHAR" for i in range(width)]
    if fmt == "csv":                                           # CSV: let DuckDB type text columns
        types = [_infer_text([r[i] for r in data]) for i in range(width)]
    if not width:
        raise ConnectorError(f"{path.name}: no data found{' in ' + fs['item'] if fs.get('item') else ''}")
    cols = ", ".join(f"{_q(n)} {t}" for n, t in zip(names, types))
    con.execute(f"CREATE TABLE {target} ({cols})")
    if data:
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for r in data:
                w.writerow([_cell(v, t) for v, t in zip(r, types)])
            tmp = fh.name
        try:
            con.execute(f"COPY {target} FROM '{tmp.replace(chr(39), chr(39) * 2)}' (FORMAT CSV, HEADER false, NULLSTR '', QUOTE '\"', ESCAPE '\"')")
        finally:
            os.unlink(tmp)
    if fmt == "excel" and any(t == "VARCHAR" for t in types) and not data:
        notes.append("sheet has a header but no rows")
    return {"rows": len(data), "notes": notes}


def _infer_text(values: list[str]) -> str:
    vals = [v.strip() for v in values if v not in (None, "") and v.strip() != ""]
    if not vals:
        return "VARCHAR"
    def all_(fn):
        try:
            for v in vals:
                fn(v)
            return True
        except ValueError:
            return False
    if all_(lambda v: int(v.replace(",", "")) if v.lstrip("-").replace(",", "").isdigit() else (_ for _ in ()).throw(ValueError())):
        return "VARCHAR" if any(v.startswith("0") and len(v) > 1 for v in vals) else "BIGINT"
    if all_(lambda v: float(v.replace(",", ""))):
        return "DOUBLE"
    if all_(lambda v: dt.date.fromisoformat(v[:10]) if len(v) == 10 else (_ for _ in ()).throw(ValueError())):
        return "DATE"
    return "VARCHAR"           # other text dates keep their text; the tool detects the format (TRY_TO_DATE)


def _signature(path: Path, fs: dict) -> str:
    st = path.stat()
    key = json.dumps([str(path), st.st_size, int(st.st_mtime), fs.get("item"), fs.get("item_kind"), fs.get("header"),
                      fs.get("skip"), fs.get("delimiter"), 3], sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()


# ------------------------------------------------------------------------------------------ connector

class FileConnector(DataConnector):
    platform = "files"
    sql_dialect = "databricks"
    mode = RunMode.LIVE

    def capabilities(self) -> set[str]:
        return {Capability.LIST_TABLES, Capability.DESCRIBE_TABLE, Capability.EXECUTE_SQL}

    @property
    def folder(self) -> Optional[Path]:
        f = self.config.get("folder")
        return Path(f) if f else None

    def _cache_path(self) -> str:
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
            return str(self.folder / ".cache.duckdb")
        return ":memory:"

    def sync(self) -> dict:
        """Load/refresh every file-backed table. -> {loaded: [...], missing: [...], errors: [...]}"""
        out: dict = {"loaded": [], "missing": [], "errors": [], "files": set()}
        con = duckdb.connect(self._cache_path())
        try:
            con.execute(f"CREATE TABLE IF NOT EXISTS {META} (fqn VARCHAR PRIMARY KEY, sig VARCHAR, rows BIGINT, path VARCHAR)")
            for fqn, fs in (self.config.get("tables") or {}).items():
                schema, table = fqn.split(".", 1)
                path, where = locate(fs, self.folder, self.config.get("source_folder"))
                if not path:
                    out["missing"].append({"table": fqn, "file": fs.get("file"), "path": fs.get("path")})
                    continue
                sig = _signature(path, fs)
                row = con.execute(f"SELECT sig, rows FROM {META} WHERE fqn = ?", [fqn]).fetchone()
                exists = con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?",
                                     [schema, table]).fetchone()[0]
                try:
                    if not row or row[0] != sig or not exists:
                        info = load_table(con, schema, table, path, fs)
                        con.execute(f"INSERT OR REPLACE INTO {META} VALUES (?, ?, ?, ?)", [fqn, sig, info["rows"], str(path)])
                        rows, notes = info["rows"], info["notes"]
                    else:
                        rows, notes = row[1], []
                    out["loaded"].append({"table": fqn, "file": path.name, "where": where, "rows": rows, "notes": notes})
                    out["files"].add(path.name)
                except ConnectorError as e:
                    out["errors"].append(f"{fqn}: {e}")
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"{fqn} ({path.name}): {type(e).__name__}: {str(e).splitlines()[0][:200]}")
        finally:
            con.close()
        out["files"] = sorted(out["files"])
        return out

    def test_connection(self) -> ConnectionTestResult:
        if not self.config.get("tables"):
            return ConnectionTestResult(False, "No Excel/CSV source tables found in the Power BI model", self.mode)
        r = self.sync()
        rows = sum(x["rows"] for x in r["loaded"])
        parts = [f"Loaded {len(r['loaded'])} table(s), {rows:,} rows from {', '.join(r['files']) or 'no files'}"] if r["loaded"] else []
        if r["missing"]:
            miss = sorted({m["file"] for m in r["missing"]})
            parts.append(f"Missing file(s): {', '.join(miss)} - upload them under Source files, or set the folder path in Settings")
        parts += r["errors"][:3]
        return ConnectionTestResult(not r["missing"] and not r["errors"] and bool(r["loaded"]), " · ".join(parts), self.mode)

    def _con(self):
        self.sync()
        return duckdb.connect(self._cache_path(), read_only=self.folder is not None)

    def list_tables(self, catalog=None, schema=None) -> list[SourceTableMeta]:
        return [self.describe_table(f) for f in (self.config.get("tables") or {})]

    def describe_table(self, fqn: str, con=None) -> SourceTableMeta:
        parts = [p.strip('`"') for p in fqn.split(".")][-2:]
        if len(parts) != 2:
            raise ConnectorError(f"Expected <workbook>.<sheet>, got {fqn}")
        s, t = parts
        con = con or self._con()
        cols = con.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE "
                           "table_schema=? AND table_name=? ORDER BY ordinal_position", [s, t]).fetchall()
        if not cols:
            raise ConnectorError(f"TABLE_OR_VIEW_NOT_FOUND: {s}.{t} - its file is not loaded (see Settings → Source files)")
        n = con.execute(f"SELECT COUNT(*) FROM {_q(s)}.{_q(t)}").fetchone()[0]
        return SourceTableMeta(catalog=None, schema_name=s, name=t, row_count=n,
                               columns=[ColumnMeta(name=a, data_type=b, nullable=c == "YES") for a, b, c in cols])

    def sample_values(self, fqn: str, column: str, limit: int = 200) -> list[str]:
        s, t = [p.strip('`"') for p in fqn.split(".")][-2:]
        con = self._con()
        return [r[0] for r in con.execute(f"SELECT DISTINCT CAST({_q(column)} AS VARCHAR) FROM {_q(s)}.{_q(t)} "
                                          f"WHERE {_q(column)} IS NOT NULL LIMIT {int(limit)}").fetchall()]

    def execute_sql(self, sql: str, params: Optional[dict[str, Any]] = None) -> QueryResult:
        assert_read_only(sql, self.sql_dialect)
        tree = sqlglot.parse_one(sql, read="databricks")
        for t in tree.find_all(sqlglot.exp.Table):           # files have no catalog
            if t.args.get("catalog"):
                t.set("catalog", None)
        duck = tree.sql(dialect="duckdb")
        t0 = time.time()
        con = self._con()
        try:
            cur = con.execute(duck, params or {})
        except duckdb.Error as e:
            raise ConnectorError(f"{type(e).__name__}: {str(e).splitlines()[0][:300]}") from e
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return QueryResult(rows=rows, query_text=sql, language="sql", connector=self.platform, mode=self.mode,
                           elapsed_ms=int((time.time() - t0) * 1000),
                           extra={"params": params or {}, "executed_as": duck, "engine": "duckdb over your Excel/CSV files"})
