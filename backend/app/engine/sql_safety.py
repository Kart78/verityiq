"""Read-only guard for every SQL statement before it reaches a data connector."""
from __future__ import annotations

import sqlglot
from sqlglot import exp

_FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Merge,
              exp.Command, exp.TruncateTable, exp.Grant)


class UnsafeSQL(ValueError):
    pass


def assert_read_only(sql: str, dialect: str = "databricks") -> None:
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQL(f"SQL does not parse in {dialect} dialect: {e}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQL(f"Exactly one statement allowed, got {len(statements)}")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.With)) and not (
            isinstance(stmt, exp.Subquery)):
        raise UnsafeSQL(f"Only SELECT queries are allowed (got {type(stmt).__name__})")
    for node in stmt.walk():
        if isinstance(node, _FORBIDDEN):
            raise UnsafeSQL(f"Forbidden operation in query: {type(node).__name__}")
