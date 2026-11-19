"""Builds independent, source-side SQL from an APPROVED KPI definition + environment mapping.

The SQL never reads from the BI tool. Values are bound as named parameters (:p0, :p1 ...)
for execution; `render_inline` produces a readable script with literals for review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import sqlglot

import re

from ..core.environment import EnvironmentConfig, FieldMapping, quote_fqn, quote_ident, sql_alias
from ..core.models import FilterSpec, KPIDefinition, MeasureSpec
from .sql_safety import assert_read_only


class MappingError(ValueError):
    """A logical field/table has no source mapping - the test must be BLOCKED."""


@dataclass
class GeneratedSQL:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    dialect: str = "databricks"
    description: str = ""

    def render_inline(self, dialect: Optional[str] = None) -> str:
        out = self.sql
        for k in sorted(self.params, key=len, reverse=True):
            out = out.replace(f":{k}", _literal(self.params[k]))
        if dialect and dialect != "databricks":
            out = transpile_script(out, dialect)
        return out


def transpile_script(sql: str, dialect: str) -> str:
    """Translate generated Databricks SQL to another dialect, keeping the header comments."""
    lines = sql.splitlines()
    head = [l for l in lines if l.startswith("--")]
    body = "\n".join(l for l in lines if not l.startswith("--"))
    try:
        from .dialects import to_dialect
        out = to_dialect(body, dialect, pretty=True)
        return "\n".join(head + [f"-- Dialect: {dialect} (translated from Databricks SQL)", out])
    except Exception as e:  # noqa: BLE001
        return "\n".join(head + [f"-- Could not translate to {dialect} ({type(e).__name__}); Databricks SQL below", body])


def _literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def qualify(source: str, catalog: Optional[str]) -> str:
    """schema.table + default catalog -> catalog.schema.table, so a copied script runs as-is in the SQL editor."""
    if not source or source.strip().startswith("(") or not catalog:
        return source
    parts = [p for p in re.split(r"\.(?=(?:[^`]*`[^`]*`)*[^`]*$)", source.strip()) if p]
    return f"{catalog}.{source.strip()}" if len(parts) == 2 else source


class _Builder:
    def __init__(self, env: EnvironmentConfig):
        self.env = env
        self.params: dict[str, Any] = {}

    def bind(self, value: Any) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f":{name}"

    def field(self, name: str) -> FieldMapping:
        f = self.env.mapping.fields.get(name)
        if not f or not (f.source or f.source_expr or f.key_expr):
            raise MappingError(f"Field '{name}' has no source mapping in '{self.env.name}'")
        return f

    def is_virtual(self, table: str) -> bool:
        t = self.env.mapping.tables.get(table)
        if not t or t.source:
            return False
        return t.calculated or any(f.key_expr for f in self.env.mapping.fields.values() if f.table == table)

    def table(self, name: str) -> str:
        t = self.env.mapping.tables.get(name)
        if not t or not t.source:
            raise MappingError(f"Table '{name}' has no physical source table mapped (set it on the Project page)")
        return quote_fqn(qualify(t.source, self.env.data.config.get("catalog")))

    def _join(self, a: str, b: str):
        return next((j for j in self.env.mapping.joins if {j.left, j.right} == {a, b}), None)

    def path(self, base: str, target: str) -> Optional[list]:
        """Shortest chain of relationships from base towards target, following many->one (filter direction)."""
        if base == target:
            return []
        frontier, seen = [(base, [])], {base}
        while frontier:
            nxt = []
            for node, p in frontier:
                for j in self.env.mapping.joins:
                    if j.left == node and j.right not in seen:
                        np = p + [j]
                        if j.right == target:
                            return np
                        seen.add(j.right)
                        nxt.append((j.right, np))
            frontier = nxt
        j = self._join(base, target)          # fall back to a direct relationship in either direction
        return [j] if j else None

    def col(self, name: str, base: str, needed: set[str]) -> str:
        """SQL expression for a logical field in a query whose base table is `base`."""
        fm = self.field(name)
        if not self.is_virtual(fm.table):
            if self.env.mapping.tables.get(fm.table) is None:
                raise MappingError(f"Table '{fm.table}' is not in the project mapping")
            if fm.table != base:
                if self.path(base, fm.table) is None:
                    raise MappingError(f"No relationship path from '{base}' to '{fm.table}' (field {name})")
                needed.add(fm.table)
            return fm.source_sql(sql_alias(fm.table))
        # virtual (e.g. DAX calendar): resolve through the relationship key on a real table
        best = None
        for j in self.env.mapping.joins:
            if j.right != fm.table or self.is_virtual(j.left):
                continue
            p = self.path(base, j.left)
            if p is not None and (best is None or len(p) < len(best[1])):
                best = (j, p)
        if not best:
            raise MappingError(f"'{name}' is on calculated table '{fm.table}' with no relationship path from '{base}'")
        j = best[0]
        if j.left != base:
            needed.add(j.left)
        k = f"{sql_alias(j.left)}.{quote_ident(j.left_key)}"
        return (fm.key_expr or "{k}").replace("{k}", k)

    def join_path(self, base: str, needed: set[str]) -> list[str]:
        clauses, joined = [], {base}
        for tbl in sorted(needed - {base}):
            p = self.path(base, tbl)
            if p is None:
                raise MappingError(f"No relationship path between '{base}' and '{tbl}'")
            cur = base
            for j in p:
                other = j.right if j.left == cur else j.left
                if other not in joined:
                    lk, rk = (j.left_key, j.right_key) if j.left == cur else (j.right_key, j.left_key)
                    clauses.append(f"LEFT JOIN {self.table(other)} AS {sql_alias(other)} ON {sql_alias(cur)}.{quote_ident(lk)} = {sql_alias(other)}.{quote_ident(rk)}")
                    joined.add(other)
                cur = other
        return clauses

    def predicate(self, f: FilterSpec, base: str, needed: set[str]) -> str:
        col = self.col(f.field, base, needed)
        op = f.op
        if op == "is null":
            return f"{col} IS NULL"
        if op == "is not null":
            return f"{col} IS NOT NULL"
        if op in ("in", "not in"):
            vals = f.value if isinstance(f.value, list) else [f.value]
            return f"{col} {op.upper()} ({', '.join(self.bind(v) for v in vals)})"
        if isinstance(f.value, bool):
            return f"{col} = {'TRUE' if f.value else 'FALSE'}" if op == "=" else f"{col} <> {'TRUE' if f.value else 'FALSE'}"
        sql_op = "<>" if op == "!=" else op
        return f"{col} {sql_op} {self.bind(f.value)}"

    def agg(self, m: MeasureSpec, needed: set[str]) -> str:
        if m.aggregation == "COUNT_ROWS":
            return "COUNT(*)"
        if m.column_expr:
            col = re.sub(r"\{([^{}]+)\}", lambda mm: self.col(mm.group(1), m.table, needed), m.column_expr)
        else:
            if not m.column:
                raise MappingError("Aggregation has no column")
            col = self.col(m.column, m.table, needed)
        return {"SUM": f"SUM({col})", "COUNT": f"COUNT({col})", "COUNT_DISTINCT": f"COUNT(DISTINCT {col})",
                "AVG": f"AVG({col})", "MIN": f"MIN({col})", "MAX": f"MAX({col})"}[m.aggregation]

    def cte(self, name: str, m: MeasureSpec, context: list[FilterSpec], grain: Optional[str]) -> str:
        filters = list(m.filters) + list(context)
        needed: set[str] = set()
        aggsql = self.agg(m, needed)
        preds = [self.predicate(f, m.table, needed) for f in filters]
        sel = []
        gsql = self.col(grain, m.table, needed) if grain else None
        psql = self.col(m.per_field, m.table, needed) if m.per_field else None
        if gsql:
            sel.append(f"{gsql} AS grain")
        if psql:
            sel.append(f"{psql} AS per_member")
        sel.append(f"{aggsql} AS v")
        lines = [f"  SELECT {', '.join(sel)}", f"  FROM {self.table(m.table)} AS {sql_alias(m.table)}"]
        lines += [f"  {j}" for j in self.join_path(m.table, needed)]
        if preds:
            lines.append("  WHERE " + "\n    AND ".join(preds))
        if gsql or psql:
            lines.append("  GROUP BY " + ", ".join(str(i + 1) for i in range(bool(gsql) + bool(psql))))
        inner = "\n".join(lines)
        if psql:
            pagg = {"AVG": "AVG", "SUM": "SUM", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT"}.get(m.per_aggregation or "AVG", "AVG")
            outer = f"  SELECT {'grain, ' if gsql else ''}{pagg}(v) AS v FROM (\n{inner}\n  ) per_q" + ("\n  GROUP BY grain" if gsql else "")
            return f"{name} AS (\n{outer}\n)"
        return f"{name} AS (\n" + inner + "\n)"


def kpi_value_sql(kpi: KPIDefinition, env: EnvironmentConfig, context: list[FilterSpec],
                  grain: Optional[str] = None, allow_draft: bool = False) -> GeneratedSQL:
    ok, why = kpi.is_independently_defined()
    if not ok and not (allow_draft and kpi.approval_status == "DRAFT" and (kpi.all_specs() or kpi.expected_sql or kpi.freshness_field)):
        raise MappingError(why)
    b = _Builder(env)
    header = f"-- {kpi.id} {kpi.name}  [measure: {kpi.bi.measure if kpi.bi else '-'}]\n-- Definition: {kpi.business_definition}\n"
    if kpi.approval_status != "APPROVED":
        header += f"-- STATUS: {kpi.approval_status} ({kpi.basis}) - review before relying on this expected value\n"
    if kpi.basis == "inferred":
        header += "-- NOTE: logic inferred from the DAX - reconciles model vs source data, does not prove business intent\n"
    if kpi.expected_sql:
        if grain:
            raise MappingError("Custom expected SQL has no grain breakdown")
        sql = header + "-- Custom expected SQL supplied with the KPI definition (must return column expected_value)\n" + kpi.expected_sql.strip().rstrip(";")
        assert_read_only(sql, "databricks")
        return GeneratedSQL(sql=sql, params={}, description=f"Expected value for {kpi.id}")
    if context:
        header += "-- Filter context: " + "; ".join(f"{f.field} {f.op} {f.value}" for f in context) + "\n"
    if grain:
        header += f"-- Grain: {grain}\n"
    mult = f" * {kpi.multiplier}" if kpi.multiplier != 1 else ""
    if kpi.kind == "simple":
        body = "WITH " + b.cte("m", kpi.measure, context, grain) + "\n"
        body += f"SELECT {'grain, ' if grain else ''}CAST(v AS DOUBLE){mult} AS expected_value FROM m"
        body += "\nORDER BY grain" if grain else ""
    elif kpi.kind == "ratio":
        body = "WITH " + b.cte("num", kpi.numerator, context, grain) + ",\n" + b.cte("den", kpi.denominator, context, grain) + "\n"
        if grain:
            body += (f"SELECT COALESCE(num.grain, den.grain) AS grain,\n"
                     f"       CAST(COALESCE(num.v, 0) AS DOUBLE){mult} / NULLIF(den.v, 0) AS expected_value,\n"
                     f"       num.v AS numerator, den.v AS denominator\n"
                     f"FROM num FULL OUTER JOIN den ON num.grain IS NOT DISTINCT FROM den.grain\nORDER BY grain")
        else:
            body += (f"SELECT CAST(COALESCE(num.v, 0) AS DOUBLE){mult} / NULLIF(den.v, 0) AS expected_value,\n"
                     f"       num.v AS numerator, den.v AS denominator\nFROM num CROSS JOIN den")
    elif kpi.kind == "formula":
        ctes = [b.cte(f"c{i}", spec, context, grain) for i, spec in enumerate(kpi.components)]
        n = len(ctes)

        def render(node: dict, additive: bool) -> str:
            if "leaf" in node:
                v = f"CAST(c{node['leaf']}.v AS DOUBLE)"
                return f"COALESCE({v}, 0)" if additive else v
            if "const" in node:
                return repr(float(node["const"]))
            op = node["op"]
            if op == "/":
                return f"({render(node['l'], False)} / NULLIF({render(node['r'], False)}, 0))"
            add = op in ("+", "-")
            return f"({render(node['l'], add)} {op} {render(node['r'], add)})"
        expr = render(kpi.formula, False)
        body = "WITH " + ",\n".join(ctes) + "\n"
        if grain:
            g = "COALESCE(" + ", ".join(f"c{i}.grain" for i in range(n)) + ")" if n > 1 else "c0.grain"
            frm = "c0" + "".join(f"\nFULL OUTER JOIN c{i} ON c{i}.grain IS NOT DISTINCT FROM " +
                                 ("COALESCE(" + ", ".join(f"c{x}.grain" for x in range(i)) + ")" if i > 1 else "c0.grain")
                                 for i in range(1, n))
            body += f"SELECT {g} AS grain,\n       {expr}{mult} AS expected_value\nFROM {frm}\nORDER BY grain"
        else:
            body += f"SELECT {expr}{mult} AS expected_value\nFROM " + " CROSS JOIN ".join(f"c{i}" for i in range(n))
    else:
        f = b.field(kpi.freshness_field)
        tbl = f.table
        c = f.source_sql(sql_alias(tbl))
        body = (f"SELECT MAX({c}) AS latest_record,\n"
                f"       DATEDIFF(CURRENT_DATE(), MAX({c})) AS age_days\n"
                f"FROM {b.table(tbl)} AS {sql_alias(tbl)}")
    sql = header + body
    assert_read_only(sql, "databricks")
    return GeneratedSQL(sql=sql, params=b.params, description=f"Expected value for {kpi.id}")


# ----------------------------------------------------------------------------- data quality

def dq_null_sql(env: EnvironmentConfig, field_name: str) -> GeneratedSQL:
    b = _Builder(env)
    f = b.field(field_name)
    sql = (f"-- Null check: {field_name}\nSELECT COUNT(*) AS total_rows,\n"
           f"       SUM(CASE WHEN {f.source_sql(sql_alias(f.table), raw=True)} IS NULL THEN 1 ELSE 0 END) AS null_rows\n"
           f"FROM {b.table(f.table)} AS {sql_alias(f.table)}")
    assert_read_only(sql)
    return GeneratedSQL(sql=sql, description=f"Null check on {field_name}")


def dq_duplicate_sql(env: EnvironmentConfig, table: str) -> GeneratedSQL:
    b = _Builder(env)
    t = env.mapping.tables[table]
    if not t.primary_key:
        raise MappingError(f"No primary_key configured for '{table}'")
    sql = (f"-- Duplicate key check: {table}.{t.primary_key}\n"
           f"SELECT COUNT(*) AS total_rows, COUNT(DISTINCT {quote_ident(t.primary_key)}) AS distinct_keys,\n"
           f"       COUNT(*) - COUNT(DISTINCT {quote_ident(t.primary_key)}) AS duplicate_rows\nFROM {b.table(table)} AS t")
    assert_read_only(sql)
    return GeneratedSQL(sql=sql, description=f"Duplicate check on {table}")


def dq_ri_sql(env: EnvironmentConfig, join_index: int) -> GeneratedSQL:
    b = _Builder(env)
    j = env.mapping.joins[join_index]
    sql = (f"-- Referential integrity: {j.left}.{j.left_key} -> {j.right}.{j.right_key}\n"
           f"SELECT COUNT(*) AS orphan_rows\nFROM {b.table(j.left)} AS l\n"
           f"LEFT JOIN {b.table(j.right)} AS r ON l.{quote_ident(j.left_key)} = r.{quote_ident(j.right_key)}\n"
           f"WHERE l.{quote_ident(j.left_key)} IS NOT NULL AND r.{quote_ident(j.right_key)} IS NULL")
    assert_read_only(sql)
    return GeneratedSQL(sql=sql, description="Referential integrity")


def validate_sql_text(sql: str, dialect: str = "databricks") -> list[str]:
    """Used for user-supplied SQL in test cases: returns problems, empty if fine."""
    try:
        assert_read_only(sql, dialect)
        sqlglot.transpile(sql, read=dialect)
        return []
    except Exception as e:  # noqa: BLE001
        return [str(e)]


def dq_freshness_sql(env: EnvironmentConfig, field_name: str) -> GeneratedSQL:
    b = _Builder(env)
    f = b.field(field_name)
    c = f.source_sql(sql_alias(f.table))
    note = (f" (stored as text '{f.source_format}', converted)" if f.source_format else
            " (stored as text, converted)" if (f.source_type or "").lower().startswith("string") else "")
    sql = (f"-- Freshness: latest {field_name}{note}\nSELECT MAX({c}) AS latest_record,\n"
           f"       DATEDIFF(CURRENT_DATE(), MAX({c})) AS age_days\n"
           f"FROM {b.table(f.table)} AS {sql_alias(f.table)}")
    assert_read_only(sql)
    return GeneratedSQL(sql=sql, description=f"Freshness of {field_name}")
