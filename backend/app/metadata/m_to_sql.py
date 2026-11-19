"""Translate simple Power Query (M) column expressions to Databricks SQL.

Covers what Table.AddColumn / Table.TransformColumns usually contain in business models:
  [Field] references · numbers · "text" · null/true/false · + - * / & · = <> < <= > >= · and/or/not ·
  if … then … else … · common Text / Number / Date functions (see FUNCS).
Anything else returns None - the column stays flagged instead of being tested with a wrong formula.
Column references come out as {t}.`physical name` (the source table alias is filled in later).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

FUNCS: dict[str, Callable[[list[str]], Optional[str]]] = {
    "Text.Upper": lambda a: f"UPPER({a[0]})", "Text.Lower": lambda a: f"LOWER({a[0]})",
    "Text.Trim": lambda a: f"TRIM({a[0]})" if len(a) == 1 else None, "Text.Clean": lambda a: a[0],
    "Text.Proper": lambda a: f"INITCAP({a[0]})", "Text.Length": lambda a: f"LENGTH({a[0]})",
    "Text.Start": lambda a: f"LEFT({a[0]}, {a[1]})", "Text.End": lambda a: f"RIGHT({a[0]}, {a[1]})",
    "Text.Middle": lambda a: f"SUBSTRING({a[0]}, ({a[1]}) + 1, {a[2]})" if len(a) == 3 else f"SUBSTRING({a[0]}, ({a[1]}) + 1)",
    "Text.Range": lambda a: f"SUBSTRING({a[0]}, ({a[1]}) + 1, {a[2]})" if len(a) == 3 else f"SUBSTRING({a[0]}, ({a[1]}) + 1)",
    "Text.Replace": lambda a: f"REPLACE({a[0]}, {a[1]}, {a[2]})", "Text.Contains": lambda a: f"(INSTR({a[0]}, {a[1]}) > 0)" if len(a) == 2 else None,
    "Text.StartsWith": lambda a: f"STARTSWITH({a[0]}, {a[1]})" if len(a) == 2 else None,
    "Text.EndsWith": lambda a: f"ENDSWITH({a[0]}, {a[1]})" if len(a) == 2 else None,
    "Text.From": lambda a: f"CAST({a[0]} AS STRING)" if len(a) == 1 else None,
    "Text.PadStart": lambda a: f"LPAD({a[0]}, {a[1]}, {a[2] if len(a) > 2 else chr(39) + ' ' + chr(39)})",
    "Number.From": lambda a: f"CAST({a[0]} AS DOUBLE)", "Int64.From": lambda a: f"CAST({a[0]} AS BIGINT)",
    "Number.Round": lambda a: f"ROUND({a[0]}, {a[1] if len(a) > 1 else 0})" if len(a) <= 2 else None,
    "Number.RoundDown": lambda a: f"FLOOR({a[0]})" if len(a) == 1 else None, "Number.RoundUp": lambda a: f"CEIL({a[0]})" if len(a) == 1 else None,
    "Number.Abs": lambda a: f"ABS({a[0]})", "Number.Mod": lambda a: f"MOD({a[0]}, {a[1]})",
    "Number.IntegerDivide": lambda a: f"FLOOR(({a[0]}) / ({a[1]}))", "Number.Power": lambda a: f"POWER({a[0]}, {a[1]})",
    "Date.From": lambda a: f"CAST({a[0]} AS DATE)" if len(a) == 1 else None, "DateTime.Date": lambda a: f"CAST({a[0]} AS DATE)",
    "Date.Year": lambda a: f"YEAR({a[0]})", "Date.Month": lambda a: f"MONTH({a[0]})", "Date.Day": lambda a: f"DAY({a[0]})",
    "Date.QuarterOfYear": lambda a: f"QUARTER({a[0]})",
    "Date.StartOfMonth": lambda a: f"CAST(DATE_TRUNC('MONTH', {a[0]}) AS DATE)",
    "Date.EndOfMonth": lambda a: f"LAST_DAY({a[0]})", "Date.AddDays": lambda a: f"DATE_ADD({a[0]}, {a[1]})",
    "Date.AddMonths": lambda a: f"ADD_MONTHS({a[0]}, {a[1]})", "Date.AddYears": lambda a: f"ADD_MONTHS({a[0]}, 12 * ({a[1]}))",
    "Duration.Days": lambda a: a[0] if a[0].startswith("DATEDIFF(") else None,
    "DateTime.LocalNow": lambda a: "CURRENT_TIMESTAMP()", "DateTime.FixedLocalNow": lambda a: "CURRENT_TIMESTAMP()",
    "Date.Today": lambda a: "CURRENT_DATE()", "DateTimeZone.UtcNow": lambda a: "CURRENT_TIMESTAMP()",
}
UNARY_FN = {"Text.Upper", "Text.Lower", "Text.Trim", "Text.Proper", "Text.Clean", "Number.Abs", "Date.From", "DateTime.Date",
            "Number.From", "Text.From", "Number.RoundDown", "Number.RoundUp"}

_TOK = re.compile(r'\s*(?:(?P<str>"(?:[^"]|"")*")|(?P<field>\[(?:#"(?:[^"]|"")*"|[^\]]*)\])|(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
                  r'|(?P<op><>|<=|>=|=>|[-+*/&=<>(),_])|(?P<name>#?[A-Za-z_][\w.]*))')


class _P:
    def __init__(self, text: str, col: Callable[[str], Optional[str]]):
        self.t, self.i, self.col = [], 0, col
        pos = 0
        while pos < len(text):
            m = _TOK.match(text, pos)
            if not m or m.end() == pos:
                if text[pos:].strip() == "":
                    break
                raise ValueError(f"unexpected {text[pos:pos + 10]!r}")
            kind = m.lastgroup
            self.t.append((kind, m.group(kind)))
            pos = m.end()

    def peek(self, v=None):
        if self.i >= len(self.t):
            return None
        return self.t[self.i] if v is None else (self.t[self.i][1] == v)

    def take(self, v=None):
        tok = self.t[self.i]
        if v is not None and tok[1] != v:
            raise ValueError(f"expected {v}")
        self.i += 1
        return tok

    def expr(self) -> str:
        if self.peek("if"):
            self.take("if")
            c = self.expr()
            self.take("then")
            a = self.expr()
            self.take("else")
            b = self.expr()
            return f"CASE WHEN {c} THEN {a} ELSE {b} END"
        return self.orx()

    def orx(self):
        x = self.andx()
        while self.peek("or"):
            self.take()
            x = f"({x} OR {self.andx()})"
        return x

    def andx(self):
        x = self.notx()
        while self.peek("and"):
            self.take()
            x = f"({x} AND {self.notx()})"
        return x

    def notx(self):
        if self.peek("not"):
            self.take()
            return f"(NOT {self.notx()})"
        return self.cmp()

    def cmp(self):
        x = self.concat()
        while self.peek() and self.peek()[1] in ("=", "<>", "<", "<=", ">", ">="):
            op = self.take()[1]
            y = self.concat()
            if y == "NULL":
                x = f"({x} IS {'NOT ' if op == '<>' else ''}NULL)" if op in ("=", "<>") else f"({x} {op} NULL)"
            else:
                x = f"({x} {op} {y})"
        return x

    def concat(self):
        x = self.add()
        while self.peek("&"):
            self.take()
            x = f"({x} || {self.add()})"
        return x

    def add(self):
        x = self.mul()
        while self.peek() and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            y = self.mul()
            x = f"DATEDIFF({x}, {y})" if op == "-" and _looks_date(x) and _looks_date(y) else f"({x} {op} {y})"
        return x

    def mul(self):
        x = self.unary()
        while self.peek() and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            x = f"({x} {op} {self.unary()})"
        return x

    def unary(self):
        if self.peek("-"):
            self.take()
            return f"(-{self.unary()})"
        return self.atom()

    def atom(self):
        kind, v = self.take()
        if kind == "num":
            return v
        if kind == "str":
            return "'" + v[1:-1].replace('""', '"').replace("'", "''") + "'"
        if kind == "field":
            name = v[1:-1]
            name = name[2:-1].replace('""', '"') if name.startswith('#"') else name
            c = self.col(name)
            if c is None:
                raise ValueError(f"unknown column {name}")
            return c
        if v == "(":
            x = self.expr()
            self.take(")")
            return f"({x})"
        if v == "_":
            c = self.col("_")
            if c is None:
                raise ValueError("_ outside a transform")
            return c
        if kind == "name":
            if v in ("null",):
                return "NULL"
            if v in ("true", "false"):
                return v.upper()
            if v in FUNCS:
                args = []
                if self.peek("("):
                    self.take("(")
                    while not self.peek(")"):
                        args.append(self.expr())
                        if self.peek(","):
                            self.take(",")
                    self.take(")")
                out = FUNCS[v](args)
                if out is None:
                    raise ValueError(f"{v} with these arguments")
                return out
        raise ValueError(f"unsupported {v}")


def _looks_date(x: str) -> bool:
    return bool(re.search(r"CURRENT_(DATE|TIMESTAMP)|AS DATE|DATE_|TO_DATE|`[^`]*(date|dt|time)[^`]*`", x, re.I))


def m_expr_to_sql(body: str, col: Callable[[str], Optional[str]]) -> Optional[str]:
    """'each …' / '(r) => …' body -> SQL, or None if anything is not supported."""
    b = (body or "").strip()
    b = re.sub(r"^each\s+", "", b)
    lam = re.match(r"^\(\s*(\w+)\s*(?:as\s+\w+)?\s*\)\s*=>\s*(.*)$", b, re.S)
    if lam:                                          # (r) => r[Col]  ->  [Col]
        b = re.sub(rf"\b{re.escape(lam.group(1))}\s*\[", "[", lam.group(2))
    try:
        p = _P(b, col)
        out = p.expr()
        if p.i != len(p.t):
            return None
        return out
    except (ValueError, IndexError):
        return None


def split_args(call: str) -> list[str]:
    """'(a, "b", each f(x, y), type text)' -> top-level arguments."""
    inner = call.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    out, depth, cur, q = [], 0, [], False
    for ch in inner:
        if ch == '"':
            q = not q
        elif not q and ch in "([{":
            depth += 1
        elif not q and ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0 and not q:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out
