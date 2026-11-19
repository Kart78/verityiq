"""Translate row-level DAX (calculated columns over the same table) to Databricks SQL.

Supported: same-table column refs ('T'[C], T[C], [C]) · numbers · "text" · + - * / & · comparisons · && || ! ·
IF · SWITCH (value and TRUE() forms) · AND/OR/NOT · BLANK · DATE/YEAR/MONTH/DAY/QUARTER/HOUR/MINUTE · TODAY/NOW ·
DATEDIFF(start, end, unit) · EOMONTH · LEFT/RIGHT/MID/LEN/UPPER/LOWER/TRIM/CONCATENATE/SUBSTITUTE · ROUND/INT/ABS/
DIVIDE · ISBLANK · VALUE · COALESCE. Anything else -> None (the column stays flagged).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

_TOK = re.compile(r"\s*(?:(?P<str>\"(?:[^\"]|\"\")*\")|(?P<ref>(?:'(?:[^']|'')+'|[A-Za-z_][\w]*)?\s*\[(?:[^\]]|\]\])+\])"
                  r"|(?P<num>\d+(?:\.\d+)?)|(?P<op>&&|\|\||<>|<=|>=|==|[-+*/&=<>(),!])|(?P<name>[A-Za-z_][\w.]*))")
UNITS = {"SECOND": "SECOND", "MINUTE": "MINUTE", "HOUR": "HOUR", "DAY": "DAY", "WEEK": "WEEK", "MONTH": "MONTH",
         "QUARTER": "QUARTER", "YEAR": "YEAR"}


class _P:
    def __init__(self, text: str, col: Callable[[Optional[str], str], Optional[str]]):
        self.t, self.i, self.col = [], 0, col
        pos = 0
        text = re.sub(r"//[^\n]*|--[^\n]*|/\*.*?\*/", " ", text, flags=re.S)
        while pos < len(text):
            m = _TOK.match(text, pos)
            if not m or m.end() == pos:
                if not text[pos:].strip():
                    break
                raise ValueError("token")
            self.t.append((m.lastgroup, m.group(m.lastgroup)))
            pos = m.end()

    def peek(self, v=None):
        if self.i >= len(self.t):
            return None
        return self.t[self.i] if v is None else self.t[self.i][1].upper() == v.upper()

    def take(self, v=None):
        tok = self.t[self.i]
        if v is not None and tok[1].upper() != v.upper():
            raise ValueError(v)
        self.i += 1
        return tok

    def expr(self):
        x = self.andx()
        while self.peek("||"):
            self.take()
            x = f"({x} OR {self.andx()})"
        return x

    def andx(self):
        x = self.cmp()
        while self.peek("&&"):
            self.take()
            x = f"({x} AND {self.cmp()})"
        return x

    def cmp(self):
        x = self.concat()
        while self.peek() and self.peek()[1] in ("=", "==", "<>", "<", "<=", ">", ">="):
            op = self.take()[1]
            op = "=" if op == "==" else op
            x = f"({x} {op} {self.concat()})"
        return x

    def concat(self):
        x = self.add()
        while self.peek("&"):
            self.take()
            x = f"(COALESCE(CAST({x} AS STRING), '') || COALESCE(CAST({self.add()} AS STRING), ''))"
        return x

    def add(self):
        x = self.mul()
        while self.peek() and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            x = f"({x} {op} {self.mul()})"
        return x

    def mul(self):
        x = self.unary()
        while self.peek() and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            y = self.unary()
            x = f"({x} * {y})" if op == "*" else f"({x} / NULLIF({y}, 0))"
        return x

    def unary(self):
        if self.peek("-"):
            self.take()
            return f"(-{self.unary()})"
        if self.peek("!"):
            self.take()
            return f"(NOT {self.unary()})"
        return self.atom()

    def args(self):
        self.take("(")
        out = []
        while not self.peek(")"):
            out.append(self.expr())
            if self.peek(","):
                self.take(",")
        self.take(")")
        return out

    def atom(self):
        kind, v = self.take()
        if kind == "num":
            return v
        if kind == "str":
            return "'" + v[1:-1].replace('""', '"').replace("'", "''") + "'"
        if kind == "ref":
            m = re.match(r"^(?:'((?:[^']|'')+)'|([A-Za-z_]\w*))?\s*\[((?:[^\]]|\]\])+)\]$", v)
            c = self.col((m.group(1) or m.group(2) or "").replace("''", "'") or None, m.group(3).replace("]]", "]"))
            if c is None:
                raise ValueError("ref")
            return c
        if v == "(":
            x = self.expr()
            self.take(")")
            return f"({x})"
        f = v.upper()
        if f in ("TRUE", "FALSE") and self.peek("("):
            self.args()
            return f
        if f in ("TRUE", "FALSE"):
            return f
        if f == "SWITCH":
            self.take("(")
            first = self.expr()
            pairs, default = [], None
            while self.peek(","):
                self.take(",")
                a = self.expr()
                if self.peek(","):
                    self.take(",")
                    pairs.append((a, self.expr()))
                else:
                    default = a
            self.take(")")
            whens = " ".join(f"WHEN {a} THEN {b}" if first == "TRUE" else f"WHEN {first} = {a} THEN {b}" for a, b in pairs)
            return f"CASE {whens} ELSE {default if default is not None else 'NULL'} END"
        if f in UNITS and not self.peek("("):
            return f
        a = self.args() if self.peek("(") else None
        if a is None:
            raise ValueError(v)
        n = len(a)
        simple = {"YEAR": "YEAR", "MONTH": "MONTH", "DAY": "DAY", "QUARTER": "QUARTER", "HOUR": "HOUR", "MINUTE": "MINUTE",
                  "UPPER": "UPPER", "LOWER": "LOWER", "TRIM": "TRIM", "LEN": "LENGTH", "ABS": "ABS", "INT": "FLOOR"}
        if f in simple and n == 1:
            return f"{simple[f]}({a[0]})"
        if f == "IF" and n in (2, 3):
            return f"CASE WHEN {a[0]} THEN {a[1]} ELSE {a[2] if n == 3 else 'NULL'} END"
        if f == "AND" and n == 2:
            return f"({a[0]} AND {a[1]})"
        if f == "OR" and n == 2:
            return f"({a[0]} OR {a[1]})"
        if f == "NOT" and n == 1:
            return f"(NOT {a[0]})"
        if f == "BLANK" and n == 0:
            return "NULL"
        if f == "ISBLANK" and n == 1:
            return f"({a[0]} IS NULL)"
        if f in ("TODAY", "UTCTODAY") and n == 0:
            return "CURRENT_DATE()"
        if f in ("NOW", "UTCNOW") and n == 0:
            return "CURRENT_TIMESTAMP()"
        if f == "DATE" and n == 3:
            return f"MAKE_DATE({a[0]}, {a[1]}, {a[2]})"
        if f == "DATEDIFF" and n == 3:
            u = UNITS.get(re.sub(r"\W", "", a[2]).upper())
            if not u:
                return None_()
            s0, e0 = a[0], a[1]                         # DAX counts calendar boundaries crossed
            if u == "YEAR":
                return f"(YEAR({e0}) - YEAR({s0}))"
            if u == "QUARTER":
                return f"((YEAR({e0}) - YEAR({s0})) * 4 + QUARTER({e0}) - QUARTER({s0}))"
            if u == "MONTH":
                return f"((YEAR({e0}) - YEAR({s0})) * 12 + MONTH({e0}) - MONTH({s0}))"
            if u == "DAY":
                return f"DATEDIFF(CAST({e0} AS DATE), CAST({s0} AS DATE))"
            if u == "WEEK":
                raise ValueError("week boundaries differ")
            return f"DATEDIFF({u}, {s0}, {e0})"
        if f == "EOMONTH" and n == 2:
            return f"LAST_DAY(ADD_MONTHS({a[0]}, {a[1]}))"
        if f == "LEFT" and n in (1, 2):
            return f"LEFT({a[0]}, {a[1] if n == 2 else 1})"
        if f == "RIGHT" and n in (1, 2):
            return f"RIGHT({a[0]}, {a[1] if n == 2 else 1})"
        if f == "MID" and n == 3:
            return f"SUBSTRING({a[0]}, {a[1]}, {a[2]})"
        if f == "CONCATENATE" and n == 2:
            return f"CONCAT(COALESCE(CAST({a[0]} AS STRING), ''), COALESCE(CAST({a[1]} AS STRING), ''))"
        if f == "SUBSTITUTE" and n == 3:
            return f"REPLACE({a[0]}, {a[1]}, {a[2]})"
        if f == "ROUND" and n in (1, 2):
            return f"ROUND({a[0]}, {a[1] if n == 2 else 0})"
        if f == "DIVIDE" and n in (2, 3):
            return f"COALESCE({a[0]} / NULLIF({a[1]}, 0), {a[2]})" if n == 3 else f"({a[0]} / NULLIF({a[1]}, 0))"
        if f == "VALUE" and n == 1:
            return f"CAST({a[0]} AS DOUBLE)"
        if f == "COALESCE" and n >= 2:
            return f"COALESCE({', '.join(a)})"
        raise ValueError(f)


def None_():
    raise ValueError("unit")


def dax_row_to_sql(expr: str, col: Callable[[Optional[str], str], Optional[str]]) -> Optional[str]:
    try:
        p = _P(expr or "", col)
        out = p.expr()
        return out if p.i == len(p.t) else None
    except (ValueError, IndexError):
        return None
