"""Translate the generated (Databricks-dialect) validation SQL into any supported database's SQL.

sqlglot does most of the work; this module adds what it doesn't cover reliably:
  * text -> date/timestamp conversion with an explicit format (TRY_TO_DATE(col, 'MM/dd/yyyy')) per engine
  * date functions for Oracle / Teradata / DB2 (TRUNC, date arithmetic)
  * inlining named parameters (:p0) as safe literals (drivers use different parameter styles)
  * unquoting plain identifiers on engines that fold unquoted names to upper case (Snowflake, Oracle, DB2),
    so `orders`.`customer_id` still finds ORDERS.CUSTOMER_ID
"""
from __future__ import annotations

import re
from typing import Any, Optional

import sqlglot
from sqlglot import exp

SQLGLOT_DIALECT = {"databricks": "databricks", "snowflake": "snowflake", "tsql": "tsql", "postgres": "postgres",
                   "redshift": "redshift", "mysql": "mysql", "oracle": "oracle", "bigquery": "bigquery",
                   "teradata": "teradata", "db2": "postgres", "duckdb": "duckdb"}
UPPER_FOLD = {"snowflake", "oracle", "db2"}

_SPARK_TOKENS = [("yyyy", "Y4"), ("yy", "Y2"), ("MMMM", "MON_FULL"), ("MMM", "MON"), ("MM", "M2"), ("M", "M1"),
                 ("dd", "D2"), ("d", "D1"), ("HH", "H24"), ("H", "H24"), ("hh", "H12"), ("h", "H12"),
                 ("mm", "MI"), ("ss", "SS"), ("a", "AMPM")]
_FMT = {
    "snowflake": {"Y4": "YYYY", "Y2": "YY", "MON_FULL": "MMMM", "MON": "MON", "M2": "MM", "M1": "MM", "D2": "DD", "D1": "DD",
                  "H24": "HH24", "H12": "HH12", "MI": "MI", "SS": "SS", "AMPM": "AM"},
    "pg": {"Y4": "YYYY", "Y2": "YY", "MON_FULL": "Month", "MON": "Mon", "M2": "MM", "M1": "MM", "D2": "DD", "D1": "DD",
           "H24": "HH24", "H12": "HH12", "MI": "MI", "SS": "SS", "AMPM": "AM"},
    "mysql": {"Y4": "%Y", "Y2": "%y", "MON_FULL": "%M", "MON": "%b", "M2": "%m", "M1": "%c", "D2": "%d", "D1": "%e",
              "H24": "%H", "H12": "%h", "MI": "%i", "SS": "%s", "AMPM": "%p"},
    "bigquery": {"Y4": "%Y", "Y2": "%y", "MON_FULL": "%B", "MON": "%b", "M2": "%m", "M1": "%m", "D2": "%d", "D1": "%d",
                 "H24": "%H", "H12": "%I", "MI": "%M", "SS": "%S", "AMPM": "%p"},
}
# SQL Server CONVERT styles for the common date layouts
_TSQL_STYLE = {"MM/dd/yyyy": 101, "M/d/yyyy": 101, "dd/MM/yyyy": 103, "d/M/yyyy": 103, "yyyy-MM-dd": 23, "yyyyMMdd": 112,
               "dd-MM-yyyy": 105, "MM-dd-yyyy": 110, "yyyy/MM/dd": 111, "dd.MM.yyyy": 104, "dd MMM yyyy": 106,
               "MMM dd yyyy": 107, "yyyy-MM-dd HH:mm:ss": 120, "MM/dd/yyyy HH:mm:ss": 101, "MM/dd/yyyy HH:mm": 101}


def convert_format(fmt: str, target: str) -> str:
    """Spark/Java date pattern -> target engine pattern."""
    table, out, i = _FMT[target], [], 0
    while i < len(fmt):
        if fmt[i] == "'":                                        # quoted literal text
            j = fmt.find("'", i + 1)
            j = len(fmt) if j < 0 else j
            out.append(fmt[i + 1:j])
            i = j + 1
            continue
        for tok, key in _SPARK_TOKENS:
            if fmt.startswith(tok, i):
                out.append(table[key])
                i += len(tok)
                break
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


def _lit(v: Any) -> exp.Expression:
    if v is None:
        return exp.Null()
    if isinstance(v, bool):
        return exp.Boolean(this=v)
    if isinstance(v, (int, float)):
        return exp.Literal.number(v)
    return exp.Literal.string(str(v))


class _Raw:
    """Fragments written directly in the target dialect, spliced in after sqlglot generates the SQL."""
    def __init__(self):
        self.parts: list[str] = []

    def __call__(self, text: str) -> exp.Expression:
        self.parts.append(text)
        return exp.Anonymous(this=f"RAWFRAG{len(self.parts) - 1}X", expressions=[])

    def splice(self, sql: str) -> str:
        for _ in range(10):
            new = re.sub(r"RAWFRAG(\d+)X\(\)", lambda m: self.parts[int(m.group(1))], sql)
            if new == sql:
                break
            sql = new
        return sql


def _sql(node: exp.Expression, d: str) -> str:
    return node.sql(dialect=SQLGLOT_DIALECT.get(d, d))


def _text_to_date(col: exp.Expression, fmt: Optional[str], d: str, ts: bool, raw: _Raw) -> exp.Expression:
    c = _sql(col, d)
    typ = "TIMESTAMP" if ts else "DATE"
    if d == "snowflake":
        f = f", '{convert_format(fmt, 'snowflake')}'" if fmt else ""
        s = f"TRY_TO_{typ}({c}{f})"
    elif d in ("tsql",):
        style = _TSQL_STYLE.get(fmt or "")
        t = "DATETIME2" if ts else "DATE"
        s = f"TRY_CONVERT({t}, {c}, {style})" if style else f"TRY_CONVERT({t}, {c})"
    elif d in ("postgres", "redshift", "db2"):
        if fmt:
            f = convert_format(fmt, "pg")
            s = f"TO_TIMESTAMP(NULLIF(TRIM({c}), ''), '{f}')" if ts else f"TO_DATE(NULLIF(TRIM({c}), ''), '{f}')"
        else:
            s = f"CAST(NULLIF(TRIM({c}), '') AS {typ})"
    elif d == "mysql":
        s = f"STR_TO_DATE({c}, '{convert_format(fmt, 'mysql')}')" if fmt else f"CAST({c} AS {'DATETIME' if ts else 'DATE'})"
    elif d == "oracle":
        f = convert_format(fmt, "pg") if fmt else ("YYYY-MM-DD HH24:MI:SS" if ts else "YYYY-MM-DD")
        s = f"TO_{typ}({c} DEFAULT NULL ON CONVERSION ERROR, '{f}')"
    elif d == "bigquery":
        if fmt:
            s = f"SAFE.PARSE_{'DATETIME' if ts else 'DATE'}('{convert_format(fmt, 'bigquery')}', {c})"
        else:
            s = f"SAFE_CAST({c} AS {'DATETIME' if ts else 'DATE'})"
    elif d == "teradata":
        f = convert_format(fmt, "pg").replace("HH24", "HH").replace("MI", "MI") if fmt else "YYYY-MM-DD"
        s = f"CAST({c} AS {typ} FORMAT '{f}')"
    else:
        return exp.Anonymous(this=f"TRY_TO_{typ}", expressions=[col] + ([exp.Literal.string(fmt)] if fmt else []))
    return raw(s)


def _date_funcs(node: exp.Expression, d: str, raw: _Raw) -> exp.Expression:
    """Oracle / Teradata / DB2 lack DATE_TRUNC / DATEADD / DATEDIFF in the forms sqlglot emits."""
    if d == "postgres" and isinstance(node, exp.DateDiff) and (node.args.get("unit") is None or node.args["unit"].name.upper() == "DAY"):
        return raw(f"(CAST({_sql(node.this, d)} AS DATE) - CAST({_sql(node.expression, d)} AS DATE))")   # calendar days, like Databricks
    if d not in ("oracle", "teradata", "db2"):
        return node
    unit = lambda n: (n.args.get("unit").name if n.args.get("unit") is not None else "DAY").upper()  # noqa: E731
    if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
        u = {"YEAR": "YYYY", "QUARTER": "Q", "MONTH": "MM", "WEEK": "IW", "DAY": "DD"}.get(unit(node), "DD")
        if d == "db2":
            return raw(f"TRUNC_TIMESTAMP({_sql(node.this, d)}, '{u}')")
        return raw(f"TRUNC({_sql(node.this, d)}, '{u}')")
    if isinstance(node, (exp.TsOrDsAdd, exp.DateAdd)) and unit(node) == "DAY":
        return raw(f"({_sql(node.this, d)} + ({_sql(node.expression, d)}))")
    if isinstance(node, exp.DateDiff) and unit(node) == "DAY":
        a, b = _sql(node.this, d), _sql(node.expression, d)
        expr = {"oracle": f"(TRUNC({a}) - TRUNC({b}))", "db2": f"(DAYS({a}) - DAYS({b}))"}.get(d, f"(CAST({a} AS DATE) - CAST({b} AS DATE))")
        return raw(expr)
    if isinstance(node, exp.CurrentDate) and d == "oracle":
        return raw("TRUNC(SYSDATE)")
    return node


_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _ordinal_group_by(tree: exp.Expression) -> None:
    """GROUP BY 1, 2 -> the select expressions (SQL Server / Oracle reject ordinals)."""
    for sel in tree.find_all(exp.Select):
        grp = sel.args.get("group")
        if not grp:
            continue
        items = []
        for g in grp.expressions:
            if isinstance(g, exp.Literal) and not g.is_string and g.this.isdigit() and 0 < int(g.this) <= len(sel.expressions):
                e = sel.expressions[int(g.this) - 1]
                items.append((e.this if isinstance(e, exp.Alias) else e).copy())
            else:
                items.append(g)
        grp.set("expressions", items)


def to_dialect(sql: str, dialect: str, params: Optional[dict] = None, pretty: bool = False) -> str:
    """Databricks SQL (+ :named params) -> executable SQL for `dialect` with params inlined."""
    lines = [l for l in sql.splitlines() if not l.lstrip().startswith("--")]
    tree = sqlglot.parse_one("\n".join(lines), read="databricks")
    params = params or {}
    raw = _Raw()

    def pass1(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Placeholder) and node.name in params:
            return _lit(params[node.name])
        if dialect == "databricks":
            return node
        if isinstance(node, exp.Anonymous) and str(node.this).upper() in ("TRY_TO_DATE", "TRY_TO_TIMESTAMP"):
            args = node.expressions
            fmt = args[1].name if len(args) > 1 and isinstance(args[1], exp.Literal) else None
            return _text_to_date(args[0], fmt, dialect, str(node.this).upper() == "TRY_TO_TIMESTAMP", raw)
        if isinstance(node, exp.TryCast) and dialect == "snowflake":
            return raw(f"TRY_CAST(TO_VARCHAR({_sql(node.this, dialect)}) AS {_sql(node.args['to'], dialect)})")
        if isinstance(node, exp.TryCast) and dialect in ("oracle", "redshift", "db2"):
            t = _sql(node.args["to"], dialect)
            return raw(f"CAST({_sql(node.this, dialect)} AS {t} DEFAULT NULL ON CONVERSION ERROR)") if dialect == "oracle" \
                else exp.Cast(this=node.this, to=node.args["to"])
        if isinstance(node, exp.NullSafeEQ) and node.find_ancestor(exp.Join) is not None:
            # FULL JOIN needs a plain equality on most engines: compare NULL-safe text keys
            def key(e):
                return exp.Coalesce(this=exp.Cast(this=e.copy(), to=exp.DataType.build("VARCHAR(4000)")),
                                    expressions=[exp.Literal.string("~null~")])
            return exp.EQ(this=key(node.this), expression=key(node.expression))
        if isinstance(node, exp.NullSafeEQ) and dialect in ("tsql", "oracle", "teradata", "db2"):
            a, b = _sql(node.this, dialect), _sql(node.expression, dialect)
            return raw(f"({a} = {b} OR ({a} IS NULL AND {b} IS NULL))")
        return node

    tree = tree.transform(pass1)
    if dialect != "databricks":
        _ordinal_group_by(tree)
        tree = tree.transform(lambda n: _date_funcs(n, dialect, raw))
    if dialect in UPPER_FOLD:
        for ident in tree.find_all(exp.Identifier):
            if ident.args.get("quoted") and _PLAIN.match(ident.name) and ident.name.upper() not in _RESERVED:
                ident.set("quoted", False)
    return raw.splice(tree.sql(dialect=SQLGLOT_DIALECT.get(dialect, dialect), pretty=pretty))


_RESERVED = {"ORDER", "GROUP", "SELECT", "FROM", "WHERE", "TABLE", "DATE", "USER", "LEVEL", "SIZE", "NUMBER", "COMMENT",
             "VALUE", "VALUES", "KEY", "RANK", "ROW", "ROWS", "START", "END", "TO", "BY", "AS", "IN", "ON", "OR", "AND",
             "CASE", "WHEN", "THEN", "ELSE", "UNION", "JOIN", "LIMIT", "CHECK", "CURRENT", "SESSION", "MODE", "RESOURCE",
             "ACCESS", "FILE", "UID", "ROWID", "ROWNUM", "SYSDATE", "INCREMENT", "INDEX", "LOCK", "OPTION", "PRIOR", "SHARE"}
