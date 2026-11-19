"""Demonstration adapters. Every result they produce is labelled RunMode.DEMO.

DemoSQLConnector  - a real SQL engine (DuckDB) over local sample data. Generated Databricks
                    SQL is transpiled and genuinely executed, so expected values are computed,
                    not invented. Also used for local CSV/Parquet files.
DemoBIConnector   - reads a real PBIP project for metadata/DAX, but it is NOT a DAX engine.
                    To return "actual" values it evaluates hand-written SQL equivalents of
                    the sample measures (demo/bi_emulation.yaml) - written to mirror the DAX,
                    including its defects - against the same sample data.
"""
from __future__ import annotations

from app.core.names import unbracket

import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb
import sqlglot
import yaml

from ..core.environment import FieldMapping, resolve_path
from ..core.models import ColumnMeta, FilterSpec, ModelMetadata, RunMode, SourceTableMeta
from ..engine.sql_safety import assert_read_only
from ..metadata.pbip import read_pbip
from .base import (BIConnector, Capability, ConnectionTestResult, ConnectorError, DataConnector, QueryResult)
from .powerbi import build_dax


def _db(path: str, alias: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"ATTACH '{resolve_path(path)}' AS {alias} (READ_ONLY)")
    return con


class DemoSQLConnector(DataConnector):
    platform = "demo_sql"
    sql_dialect = "databricks"
    mode = RunMode.DEMO

    def capabilities(self) -> set[str]:
        return {Capability.LIST_SCHEMAS, Capability.LIST_TABLES, Capability.DESCRIBE_TABLE,
                Capability.EXECUTE_SQL, Capability.TABLE_STATS}

    def _con(self):
        return _db(self.config["database"], self.config.get("catalog_alias", "demo_catalog"))

    def test_connection(self) -> ConnectionTestResult:
        p = resolve_path(self.config["database"])
        if not p.exists():
            return ConnectionTestResult(False, f"Sample database not found: {p.name}. Run scripts/seed_demo.py", RunMode.DEMO)
        return ConnectionTestResult(True, "Demo sample database (DuckDB) - not a live Databricks workspace", RunMode.DEMO)

    def list_tables(self, catalog=None, schema=None) -> list[SourceTableMeta]:
        alias = self.config.get("catalog_alias", "demo_catalog")
        con = self._con()
        rows = con.execute("SELECT table_catalog, table_schema, table_name, table_type FROM information_schema.tables "
                           "WHERE table_catalog = ? ORDER BY 2, 3", [alias]).fetchall()
        out = []
        for c, s, t, ty in rows:
            n = con.execute(f"SELECT COUNT(*) FROM {c}.{s}.{t}").fetchone()[0]
            out.append(SourceTableMeta(catalog=c, schema_name=s, name=t, table_type=ty, row_count=n,
                                       columns=self.describe_table(f"{c}.{s}.{t}", con).columns))
        return out

    def describe_table(self, fqn: str, con=None) -> SourceTableMeta:
        con = con or self._con()
        c, s, t = fqn.split(".")
        cols = con.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE "
                           "table_catalog=? AND table_schema=? AND table_name=? ORDER BY ordinal_position", [c, s, t]).fetchall()
        return SourceTableMeta(catalog=c, schema_name=s, name=t,
                               columns=[ColumnMeta(name=a, data_type=b, nullable=n == "YES") for a, b, n in cols])

    def execute_sql(self, sql: str, params: Optional[dict[str, Any]] = None) -> QueryResult:
        assert_read_only(sql, self.sql_dialect)
        tree = sqlglot.parse_one(sql, read="databricks")
        cmap = self.config.get("catalog_map") or {}
        if cmap:
            for t in tree.find_all(sqlglot.exp.Table):
                if t.catalog in cmap:
                    t.set("catalog", sqlglot.exp.to_identifier(cmap[t.catalog]))
        duck = tree.sql(dialect="duckdb")
        t0 = time.time()
        con = self._con()
        cur = con.execute(duck, params or {})
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return QueryResult(rows=rows, query_text=sql, language="sql", connector=self.platform, mode=RunMode.DEMO,
                           elapsed_ms=int((time.time() - t0) * 1000),
                           extra={"params": params or {}, "executed_as": duck, "engine": "duckdb (sample data)"})


class DemoBIConnector(BIConnector):
    platform = "demo_bi"
    expression_language = "dax"
    mode = RunMode.DEMO

    def __init__(self, config: dict):
        super().__init__(config)
        self.emu = yaml.safe_load(resolve_path(config["emulation"]).read_text())

    def capabilities(self) -> set[str]:
        return {Capability.READ_MODEL, Capability.READ_VISUALS, Capability.QUERY_MEASURE, Capability.QUERY_GRAIN,
                Capability.REFRESH_HISTORY}

    def test_connection(self) -> ConnectionTestResult:
        ok = resolve_path(self.config["pbip_path"]).exists()
        return ConnectionTestResult(ok, "Demo BI fixture: PBIP metadata + SQL emulation of sample measures (not a Power BI service)"
                                    if ok else "Demo PBIP folder missing", RunMode.DEMO)

    def discover(self) -> ModelMetadata:
        meta = read_pbip(resolve_path(self.config["pbip_path"]))
        meta.platform = "powerbi"
        meta.mode = RunMode.DEMO
        meta.reports.append({"refresh_history": self.emu.get("refresh_history", [])})
        meta.unsupported += ["Demo adapter: no live service - values come from SQL emulation of sample measures.",
                             "Visual rendering, slicer clicks and drill-through - Phase 2 (UI automation)."]
        meta.discovered_at = datetime.now(timezone.utc).isoformat()
        return meta

    def build_measure_query(self, measure, filters, grain=None) -> str:
        return build_dax(measure, filters, grain)

    def execute_query(self, query: str) -> QueryResult:
        raise ConnectorError("Demo BI adapter cannot execute arbitrary DAX; use query_measure.")

    def _col(self, model_ref: str) -> Optional[str]:
        norm = model_ref.replace("'", "").replace(" ", "").lower()
        for k, v in self.emu["columns"].items():
            if k.replace("'", "").replace(" ", "").lower() == norm:
                return v
        return None

    def query_measure(self, measure: str, filters: list[tuple[FieldMapping, FilterSpec]],
                      grain: Optional[FieldMapping] = None) -> QueryResult:
        name = unbracket(measure)
        spec = self.emu["measures"].get(name)
        dax = build_dax(measure, filters, grain)
        if spec is None:
            raise ConnectorError(f"Query error: measure [{name}] not found in model")
        if spec.get("error"):
            raise ConnectorError(f"Query error returned by model: {spec['error']}")
        frm = spec["from"]
        aliases = {a for a in re.findall(r"\.\w+\s+(\w+)", frm)}
        where, params, notes = [], {}, []
        for fm, f in filters:
            col = self._col(fm.model)
            if not col:
                raise ConnectorError(f"Demo emulation has no column for {fm.model}")
            if col.split(".")[0].split("(")[-1] not in aliases:
                notes.append(f"{fm.model} does not filter this measure (no relationship path) - ignored, as the model would.")
                continue
            val = fm.model_value_map.get(f.value, f.value) if not isinstance(f.value, list) else f.value
            k = f"f{len(params)}"
            if f.op in ("in", "not in"):
                keys = []
                for v in (val if isinstance(val, list) else [val]):
                    kk = f"f{len(params)}"
                    params[kk] = v
                    keys.append(f"${kk}")
                where.append(f"{col} {f.op.upper()} ({', '.join(keys)})")
            elif f.op in ("is null", "is not null"):
                where.append(f"{col} {f.op.upper()}")
            else:
                params[k] = val
                cast = "::DATE" if fm.data_type == "date" else ""
                where.append(f"{col} {'<>' if f.op == '!=' else f.op} ${k}{cast}")
        sel = spec["expr"] + " AS value"
        if grain:
            gcol = self._col(grain.model)
            sel = f"{gcol} AS grain, {sel}"
        sql = f"SELECT {sel} FROM {frm}" + (" WHERE " + " AND ".join(where) if where else "")
        if grain:
            sql += " GROUP BY 1 ORDER BY 1"
        t0 = time.time()
        con = _db(self.emu["database"], self.emu.get("catalog_alias", "demo_catalog"))
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return QueryResult(rows=rows, query_text=dax, language="dax", connector=self.platform, mode=RunMode.DEMO,
                           elapsed_ms=int((time.time() - t0) * 1000),
                           extra={"emulation_sql": sql, "emulation_params": params, "notes": notes,
                                  "disclaimer": "DEMO: DAX shown is what would be sent to Power BI; value computed by SQL emulation of the sample measure."})
