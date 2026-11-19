"""Translate DAX measures into a structured, platform-neutral definition (best effort, never guessed).

Supported (combinable, with referenced measures expanded first):
  * aggregations   SUM AVERAGE MIN MAX COUNT COUNTA DISTINCTCOUNT[NOBLANK] COUNTROWS
                   SUMX/AVERAGEX/MINX/MAXX/COUNTX over a table or FILTER(table, ...) with a column or an
                   arithmetic expression of columns (RELATED allowed)
  * CALCULATE / CALCULATETABLE filters: column comparisons, IN {...}, OR on one column, AND/&&, NOT,
                   ISBLANK, TRUE/FALSE, DATE(), KEEPFILTERS, FILTER(ALL|VALUES|DISTINCT(col), ...),
                   FILTER(table, ...)
  * arithmetic between aggregations/measures and constants (+ - * /, DIVIDE), VAR ... RETURN anywhere
  * unqualified column references resolved against the measure's home table, names case-insensitive

Anything else (time intelligence, ALL/REMOVEFILTERS, IF/SWITCH, SELECTEDVALUE, TOPN, remote measures...)
is reported with a category so the tester knows to supply the business definition or expected SQL.
The result is a DRAFT with basis="inferred": it reconciles model vs. source data but cannot prove intent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional, Union

from ..core.models import FilterSpec, KPIDefinition, MeasureMeta, MeasureSpec, ModelMetadata, Tolerance
from .dax import Tok, _tname, tokenize


class Untranslatable(Exception):
    def __init__(self, msg: str, category: str = "unsupported"):
        super().__init__(msg)
        self.category = category


TIME_INTEL = {"SAMEPERIODLASTYEAR", "DATEADD", "DATESYTD", "DATESQTD", "DATESMTD", "PARALLELPERIOD", "DATESINPERIOD",
              "DATESBETWEEN", "PREVIOUSMONTH", "PREVIOUSYEAR", "PREVIOUSQUARTER", "PREVIOUSDAY", "NEXTMONTH", "NEXTYEAR",
              "NEXTQUARTER", "NEXTDAY", "TOTALYTD", "TOTALQTD", "TOTALMTD", "OPENINGBALANCEMONTH", "CLOSINGBALANCEMONTH",
              "OPENINGBALANCEYEAR", "CLOSINGBALANCEYEAR", "STARTOFMONTH", "ENDOFMONTH", "STARTOFYEAR", "ENDOFYEAR",
              "FIRSTDATE", "LASTDATE", "LASTNONBLANK", "FIRSTNONBLANK", "DATESWTD", "OFFSET", "WINDOW"}
CONTEXT_MODIFIERS = {"ALL", "REMOVEFILTERS", "ALLEXCEPT", "ALLSELECTED", "ALLNOBLANKROW", "USERELATIONSHIP", "CROSSFILTER",
                     "TREATAS", "TOPN", "SUMMARIZE", "SUMMARIZECOLUMNS", "ADDCOLUMNS", "GENERATE", "NATURALINNERJOIN"}
CONDITIONAL = {"IF", "IF.EAGER", "SWITCH", "SELECTEDVALUE", "HASONEVALUE", "HASONEFILTER", "ISFILTERED", "ISCROSSFILTERED",
               "ISINSCOPE", "FORMAT", "CONCATENATEX", "UNICHAR", "REPT", "COALESCE", "IFERROR", "VALUES", "ISBLANK",
               "USERPRINCIPALNAME", "USERNAME", "RANKX", "RANK", "ROWNUMBER", "SELECTEDMEASURE", "NAMEOF"}


# ------------------------------------------------------------------ tiny DAX parser -> AST tuples
#   ("call", NAME, [args])  ("col", table|None, column)  ("tbl", table)  ("str", v)  ("num", v)
#   ("bin", op, l, r)  ("not", x)  ("set", [items])  ("ident", name)

class _P:
    PREC = {"||": 1, "&&": 2, "=": 3, "==": 3, "<>": 3, "<": 3, ">": 3, "<=": 3, ">=": 3, "IN": 3,
            "&": 4, "+": 5, "-": 5, "*": 6, "/": 6, "^": 7}

    def __init__(self, toks: list[Tok]):
        self.t, self.i = toks, 0

    def peek(self, k=0) -> Optional[Tok]:
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def take(self) -> Tok:
        tok = self.peek()
        if tok is None:
            raise Untranslatable("unexpected end of expression", "parse")
        self.i += 1
        return tok

    def expect(self, text: str):
        tok = self.take()
        if tok.text != text:
            raise Untranslatable(f"expected '{text}' near '{tok.text}'", "parse")

    def is_kw(self, word: str, k=0) -> bool:
        tok = self.peek(k)
        return bool(tok and tok.kind == "ident" and tok.text.upper() == word)

    def expr(self, minp=0):
        if self.is_kw("VAR"):
            return self.var_block()
        left = self.unary()
        while True:
            tok = self.peek()
            if tok is None:
                break
            op = "IN" if (tok.kind == "ident" and tok.text.upper() == "IN") else tok.text
            p = self.PREC.get(op)
            if p is None or p < minp or tok.kind not in ("op", "ident"):
                break
            self.take()
            right = self.expr(p + 1)
            left = ("bin", op, left, right)
        return left

    def var_block(self):
        env: list[tuple[str, Any]] = []
        while self.is_kw("VAR"):
            self.take()
            name = self.take().text
            self.expect("=")
            env.append((name.lower(), self.expr()))
        if not self.is_kw("RETURN"):
            raise Untranslatable("VAR without RETURN", "parse")
        self.take()
        body = self.expr()
        resolved: dict[str, Any] = {}
        for n, e in env:
            resolved[n] = _subst(e, resolved)
        return _subst(body, resolved)

    def unary(self):
        tok = self.peek()
        if tok and tok.text == "-":
            self.take()
            nxt = self.peek()
            if nxt and nxt.kind == "number":
                self.take()
                return ("num", -float(nxt.text))
            return ("bin", "*", ("num", -1.0), self.unary())
        if tok and tok.text == "+":
            self.take()
            return self.unary()
        if self.is_kw("NOT") and not (self.peek(1) and self.peek(1).text == "("):
            self.take()
            return ("not", self.unary())
        return self.primary()

    def primary(self):
        tok = self.take()
        if tok.text == "(":
            e = self.expr()
            self.expect(")")
            return e
        if tok.text == "{":
            items = []
            while self.peek() and self.peek().text != "}":
                items.append(self.expr())
                if self.peek() and self.peek().text == ",":
                    self.take()
            self.expect("}")
            return ("set", items)
        if tok.kind == "string":
            return ("str", tok.text[1:-1].replace('""', '"'))
        if tok.kind == "number":
            return ("num", float(tok.text))
        if tok.kind in ("ident", "qtable"):
            nxt = self.peek()
            if nxt and nxt.kind == "bracket":
                self.take()
                return ("col", _tname(tok), nxt.text[1:-1].replace("]]", "]"))
            if tok.kind == "ident" and nxt and nxt.text == "(":
                self.take()
                args = []
                while self.peek() and self.peek().text != ")":
                    if self.peek().text == ",":            # empty argument, e.g. DIVIDE(a, b, )
                        self.take()
                        continue
                    args.append(self.expr())
                    if self.peek() and self.peek().text == ",":
                        self.take()
                self.expect(")")
                return ("call", tok.text.upper(), args)
            return ("tbl", _tname(tok)) if tok.kind == "qtable" else ("ident", tok.text)
        if tok.kind == "bracket":
            return ("col", None, tok.text[1:-1].replace("]]", "]"))
        raise Untranslatable(f"unsupported token '{tok.text}'", "parse")


def _subst(n, env: dict):
    if isinstance(n, tuple):
        if n[0] == "ident" and n[1].lower() in env:
            return env[n[1].lower()]
        return tuple(_subst(x, env) if isinstance(x, (tuple, list)) else x for x in n)
    if isinstance(n, list):
        return [_subst(x, env) for x in n]
    return n


def parse_dax(expr: str):
    p = _P(tokenize(expr))
    try:
        ast = p.expr()
    except RecursionError:
        raise Untranslatable("expression nested too deeply", "parse")
    if p.peek() is not None:
        raise Untranslatable(f"unparsed tail near '{p.peek().text}'", "parse")
    return ast


# ------------------------------------------------------------------ resolution against the model

class _Ctx:
    def __init__(self, model: Optional[ModelMetadata], home: Optional[str]):
        self.home = home
        self.tables = {t.name.lower(): t.name for t in model.tables} if model else {}
        self.cols = {(t.name.lower(), c.name.lower()): (t.name, c.name) for t in model.tables for c in t.columns} if model else {}
        self.by_col: dict[str, list[tuple[str, str]]] = {}
        for (tl, cl), v in self.cols.items():
            self.by_col.setdefault(cl, []).append(v)
        self.strict = model is not None

    def table(self, name: str) -> str:
        if not self.strict:
            return name
        t = self.tables.get(name.lower())
        if not t:
            raise Untranslatable(f"unknown table '{name}'", "reference")
        return t

    def col(self, table: Optional[str], column: str) -> tuple[str, str]:
        if not self.strict:
            return (table or self.home or "?", column)
        if table is None:
            if self.home and (self.home.lower(), column.lower()) in self.cols:
                return self.cols[(self.home.lower(), column.lower())]
            cands = self.by_col.get(column.lower(), [])
            if len(cands) == 1:
                return cands[0]
            raise Untranslatable(f"unqualified column [{column}] cannot be resolved", "reference")
        hit = self.cols.get((table.lower(), column.lower()))
        if not hit:
            raise Untranslatable(f"unknown column {table}[{column}]", "reference")
        return hit


def fkey(table: str, column: str) -> str:
    return f"{table}.{column}"


# ------------------------------------------------------------------ intermediate representation

@dataclass
class _Simple:
    agg: str
    table: str
    column: Optional[tuple[str, str]] = None
    filters: list[FilterSpec] = field(default_factory=list)
    column_expr: Optional[str] = None
    per_field: Optional[str] = None
    per_agg: Optional[str] = None


@dataclass
class _Bin:
    op: str
    l: "Node"
    r: "Node"


@dataclass
class _Const:
    v: float


Node = Union[_Simple, _Bin, _Const]

AGG = {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT", "COUNTA": "COUNT",
       "DISTINCTCOUNT": "COUNT_DISTINCT", "DISTINCTCOUNTNOBLANK": "COUNT_DISTINCT"}
AGGX = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT", "COUNTAX": "COUNT"}


def _literal(n) -> tuple[bool, Any]:
    if n[0] == "str":
        return True, n[1]
    if n[0] == "num":
        v = n[1]
        return True, int(v) if float(v).is_integer() else v
    if n[0] == "call" and n[1] in ("TRUE", "FALSE") and not n[2]:
        return True, n[1] == "TRUE"
    if n[0] == "ident" and n[1].upper() in ("TRUE", "FALSE"):
        return True, n[1].upper() == "TRUE"
    if n[0] == "call" and n[1] == "DATE" and len(n[2]) == 3 and all(a[0] == "num" for a in n[2]):
        y, m, d = (int(a[1]) for a in n[2])
        return True, date(y, m, d).isoformat()
    if n[0] == "call" and n[1] == "BLANK":
        return True, None
    return False, None


class _Interp:
    def __init__(self, ctx: _Ctx):
        self.ctx = ctx
        self.notes: list[str] = []

    # ---- filters
    def preds(self, n) -> list[FilterSpec]:
        if n[0] == "call" and n[1] == "KEEPFILTERS":
            return self.filter_arg(n[2][0])
        if (n[0] == "bin" and n[1] == "&&") or (n[0] == "call" and n[1] == "AND" and len(n[2]) == 2):
            a, b = (n[2], n[3]) if n[0] == "bin" else n[2]
            return self.preds(a) + self.preds(b)
        if (n[0] == "bin" and n[1] == "||") or (n[0] == "call" and n[1] == "OR" and len(n[2]) == 2):
            a, b = (n[2], n[3]) if n[0] == "bin" else n[2]
            pa, pb = self.preds(a), self.preds(b)
            if len(pa) == len(pb) == 1 and pa[0].field == pb[0].field and pa[0].op in ("=", "in") and pb[0].op in ("=", "in"):
                vals = (pa[0].value if pa[0].op == "in" else [pa[0].value]) + (pb[0].value if pb[0].op == "in" else [pb[0].value])
                return [FilterSpec(field=pa[0].field, op="in", value=vals)]
            raise Untranslatable("OR across different columns", "filter")
        if n[0] == "bin" and n[1] in ("=", "==", "<>", ">", ">=", "<", "<="):
            l, r = n[2], n[3]
            op = {"==": "=", "<>": "!="}.get(n[1], n[1])
            if l[0] != "col":
                l, r = r, l
                op = {">": "<", "<": ">", ">=": "<=", "<=": ">="}.get(op, op)
            ok, v = _literal(r)
            if l[0] == "col" and ok:
                t, c = self.ctx.col(l[1], l[2])
                if v is None:
                    return [FilterSpec(field=fkey(t, c), op="is null" if op == "=" else "is not null")]
                return [FilterSpec(field=fkey(t, c), op=op, value=v)]
        if n[0] == "bin" and n[1] == "IN" and n[2][0] == "col" and n[3][0] == "set":
            vals = [_literal(x) for x in n[3][1]]
            if all(ok for ok, _ in vals):
                t, c = self.ctx.col(n[2][1], n[2][2])
                return [FilterSpec(field=fkey(t, c), op="in", value=[v for _, v in vals])]
        if n[0] == "not" or (n[0] == "call" and n[1] == "NOT" and len(n[2]) == 1):
            inner = n[1] if n[0] == "not" else n[2][0]
            if inner[0] == "call" and inner[1] == "ISBLANK" and inner[2] and inner[2][0][0] == "col":
                t, c = self.ctx.col(inner[2][0][1], inner[2][0][2])
                return [FilterSpec(field=fkey(t, c), op="is not null")]
            got = self.preds(inner)
            if len(got) == 1:
                g = got[0]
                flip = {"=": "!=", "!=": "=", "in": "not in", "not in": "in", "is null": "is not null",
                        "is not null": "is null", ">": "<=", "<": ">=", ">=": "<", "<=": ">"}
                return [FilterSpec(field=g.field, op=flip[g.op], value=g.value)]
        if n[0] == "call" and n[1] == "ISBLANK" and n[2] and n[2][0][0] == "col":
            t, c = self.ctx.col(n[2][0][1], n[2][0][2])
            return [FilterSpec(field=fkey(t, c), op="is null")]
        if n[0] == "col":   # CALCULATE(..., T[Flag]) shorthand
            t, c = self.ctx.col(n[1], n[2])
            return [FilterSpec(field=fkey(t, c), op="=", value=True)]
        raise Untranslatable("filter expression outside the supported subset", "filter")

    def filter_arg(self, n) -> list[FilterSpec]:
        if n[0] == "call":
            name = n[1]
            if name == "KEEPFILTERS":
                return self.filter_arg(n[2][0])
            if name in TIME_INTEL:
                raise Untranslatable(f"{name}() time intelligence depends on the report's date selection", "time-intelligence")
            if name in CONTEXT_MODIFIERS:
                raise Untranslatable(f"{name}() modifies filter context", "context-modifier")
            if name == "FILTER" and len(n[2]) == 2:
                src = n[2][0]
                if src[0] in ("tbl", "ident"):
                    return self.preds(n[2][1])
                if src[0] == "call" and src[1] in ("ALL", "VALUES", "DISTINCT") and src[2] and src[2][0][0] == "col":
                    return self.preds(n[2][1])
                raise Untranslatable("FILTER over a computed table", "context-modifier")
        return self.preds(n)

    # ---- values
    def table_src(self, a) -> tuple[str, list[FilterSpec]]:
        if a[0] in ("tbl", "ident"):
            return self.ctx.table(a[1]), []
        if a[0] == "call" and a[1] == "FILTER" and len(a[2]) == 2 and a[2][0][0] in ("tbl", "ident"):
            return self.ctx.table(a[2][0][1]), self.preds(a[2][1])
        if a[0] == "call" and a[1] == "CALCULATETABLE" and a[2] and a[2][0][0] in ("tbl", "ident"):
            return self.ctx.table(a[2][0][1]), [f for x in a[2][1:] for f in self.filter_arg(x)]
        if a[0] == "call" and a[1] == "RELATEDTABLE":
            raise Untranslatable("RELATEDTABLE() is row-context dependent", "unsupported")
        raise Untranslatable("iterator over a computed table", "context-modifier")

    def scalar_expr(self, e, base: str, refs: list[str]) -> str:
        """Arithmetic of columns -> SQL template with {Table.Column} placeholders."""
        if e[0] == "col":
            t, c = self.ctx.col(e[1] if e[1] else base, e[2])
            refs.append(fkey(t, c))
            return "{" + fkey(t, c) + "}"
        if e[0] == "call" and e[1] == "RELATED" and e[2] and e[2][0][0] == "col":
            return self.scalar_expr(e[2][0], base, refs)
        if e[0] == "num":
            return repr(e[1]) if not float(e[1]).is_integer() else str(int(e[1]))
        if e[0] == "bin" and e[1] in ("+", "-", "*", "/"):
            l, r = self.scalar_expr(e[2], base, refs), self.scalar_expr(e[3], base, refs)
            return f"({l} {e[1]} NULLIF({r}, 0))" if e[1] == "/" else f"({l} {e[1]} {r})"
        if e[0] == "call" and e[1] == "DIVIDE" and len(e[2]) >= 2:
            return f"({self.scalar_expr(e[2][0], base, refs)} / NULLIF({self.scalar_expr(e[2][1], base, refs)}, 0))"
        if e[0] == "call" and e[1] in ("ABS", "ROUND", "INT", "TRUNC") and e[2]:
            inner = self.scalar_expr(e[2][0], base, refs)
            return {"ABS": f"ABS({inner})", "INT": f"FLOOR({inner})", "TRUNC": f"FLOOR({inner})",
                    "ROUND": f"ROUND({inner}, {int(e[2][1][1]) if len(e[2]) > 1 and e[2][1][0] == 'num' else 0})"}[e[1]]
        raise Untranslatable("iterator expression uses functions outside the supported subset", "iterator-expression")

    def value(self, n) -> Node:
        kind = n[0]
        if kind == "num":
            return _Const(n[1])
        if kind == "call" and n[1] == "__HOME" and len(n[2]) == 2 and n[2][0][0] == "str":
            saved = self.ctx.home
            self.ctx.home = n[2][0][1]
            try:
                return self.value(n[2][1])
            finally:
                self.ctx.home = saved
        if kind == "call":
            name, args = n[1], n[2]
            if name in AGGX and len(args) == 2 and args[0][0] == "call" and args[0][1] in ("VALUES", "DISTINCT") \
                    and args[0][2] and args[0][2][0][0] == "col":
                # e.g. AVERAGEX(VALUES('Date'[Date]), [Sales]) = average of the measure per date
                t, c = self.ctx.col(args[0][2][0][1], args[0][2][0][2])
                inner = self.value(args[1])
                if isinstance(inner, _Simple) and not inner.per_field:
                    inner.per_field, inner.per_agg = fkey(t, c), AGGX[name] if AGGX[name] != "COUNT" else "COUNT"
                    return inner
                raise Untranslatable("iterator over a column with a composite expression", "iterator-expression")
            if name == "COUNTROWS" and args:
                a = args[0]
                if a[0] == "call" and a[1] in ("DISTINCT", "VALUES") and a[2] and a[2][0][0] == "col":
                    t, c = self.ctx.col(a[2][0][1], a[2][0][2])
                    return _Simple("COUNT_DISTINCT", t, (t, c))
                t, flt = self.table_src(a)
                return _Simple("COUNT_ROWS", t, filters=flt)
            if name in AGG and len(args) == 1 and args[0][0] == "col":
                t, c = self.ctx.col(args[0][1], args[0][2])
                return _Simple(AGG[name], t, (t, c))
            if name in AGGX and len(args) == 2:
                t, flt = self.table_src(args[0])
                e = args[1]
                if e[0] == "col":
                    tc = self.ctx.col(e[1] if e[1] else t, e[2])
                    return _Simple(AGGX[name], t, tc, flt)
                if e[0] == "call" and e[1] == "RELATED" and e[2] and e[2][0][0] == "col":
                    tc = self.ctx.col(e[2][0][1], e[2][0][2])
                    return _Simple(AGGX[name], t, tc, flt)
                refs: list[str] = []
                tmpl = self.scalar_expr(e, t, refs)
                return _Simple(AGGX[name], t, None, flt, column_expr=tmpl)
            if name == "CALCULATE" and args:
                base = self.value(args[0])
                extra = [f for a in args[1:] for f in self.filter_arg(a)]
                return _apply_filters(base, extra)
            if name == "DIVIDE" and len(args) >= 2:
                if len(args) == 3 and not (args[2][0] == "num" and args[2][1] == 0) and not (args[2][0] == "call" and args[2][1] == "BLANK"):
                    self.notes.append("DIVIDE alternate result ignored (applies only when the denominator is 0/blank).")
                return _Bin("/", self.value(args[0]), self.value(args[1]))
            if name == "EXTERNALMEASURE":
                raise Untranslatable("measure is defined in a remote model (composite/DirectQuery to AS)", "remote-measure")
            if name == "BLANK" and not args:
                raise Untranslatable("placeholder measure returning BLANK()", "placeholder")
            if name in TIME_INTEL:
                raise Untranslatable(f"{name}() time intelligence depends on the report's date selection", "time-intelligence")
            if name in CONTEXT_MODIFIERS:
                raise Untranslatable(f"{name}() modifies filter context", "context-modifier")
            if name in CONDITIONAL:
                raise Untranslatable(f"{name}() conditional/display logic", "conditional")
            raise Untranslatable(f"{name}() is not in the supported translation subset", "unsupported")
        if kind == "bin" and n[1] in ("+", "-", "*", "/"):
            return _Bin(n[1], self.value(n[2]), self.value(n[3]))
        if kind == "bin" and n[1] == "&":
            raise Untranslatable("text/display measure (string concatenation)", "conditional")
        if kind == "bin":
            raise Untranslatable(f"measure returns a comparison ({n[1]})", "conditional")
        if kind == "str":
            raise Untranslatable("measure returns text", "conditional")
        if kind == "col":
            if n[1] is None:
                try:
                    self.ctx.col(None, n[2])
                except Untranslatable:
                    raise Untranslatable(f"[{n[2]}] is not a measure or column in the model", "reference")
            raise Untranslatable("bare column reference outside an aggregation (row context)", "unsupported")
        raise Untranslatable("expression shape not supported", "unsupported")


def _apply_filters(node: Node, extra: list[FilterSpec]) -> Node:
    if isinstance(node, _Simple):
        node.filters = node.filters + list(extra)
    elif isinstance(node, _Bin):
        _apply_filters(node.l, extra)
        _apply_filters(node.r, extra)
    return node


def _spec(s: _Simple) -> MeasureSpec:
    return MeasureSpec(table=s.table, aggregation=s.agg, column=fkey(*s.column) if s.column else None,
                       filters=s.filters, column_expr=s.column_expr, per_field=s.per_field, per_aggregation=s.per_agg)


def _const_fold(n: Node) -> Node:
    if isinstance(n, _Bin):
        n.l, n.r = _const_fold(n.l), _const_fold(n.r)
        if isinstance(n.l, _Const) and isinstance(n.r, _Const):
            a, b = n.l.v, n.r.v
            return _Const({"+": a + b, "-": a - b, "*": a * b, "/": (a / b if b else 0.0)}[n.op])
    return n


def _to_formula(n: Node, comps: list[MeasureSpec]) -> dict:
    if isinstance(n, _Simple):
        comps.append(_spec(n))
        return {"leaf": len(comps) - 1}
    if isinstance(n, _Const):
        return {"const": n.v}
    return {"op": n.op, "l": _to_formula(n.l, comps), "r": _to_formula(n.r, comps)}


def _unit(fmt: Optional[str], name: str) -> str:
    f = (fmt or "") + " " + name
    if "%" in f:
        return "percent"
    if re.search(r"[$€£¥]|currency", f, re.I):
        return "currency"
    return "number"


def _date_column(model: ModelMetadata, table: str, rel_col: str) -> Optional[str]:
    t = next((x for x in model.tables if x.name == table), None)
    if not t:
        return None
    by = {c.name: c for c in t.columns}
    if rel_col in by and (by[rel_col].data_type or "").lower() in ("datetime", "date"):
        return rel_col
    dates = [c.name for c in t.columns if (c.data_type or "").lower() in ("datetime", "date") and not c.expression]
    pref = [c for c in dates if c.lower() in ("date", "fulldate", "full date", "calendar date", "day")]
    return (pref or dates or [None])[0]


def translate(expanded: str, model: Optional[ModelMetadata] = None, home: Optional[str] = None) -> tuple[Node, list[str]]:
    it = _Interp(_Ctx(model, home))
    node = _const_fold(it.value(parse_dax(expanded)))
    return node, it.notes


def infer_kpi(m: MeasureMeta, expanded: str, kid: str, model: ModelMetadata, marked: Optional[str] = None) -> KPIDefinition:
    k = KPIDefinition(id=kid, name=m.name, business_definition="(not supplied - inferred from DAX; review against the business requirement)",
                      kind="simple", unit=_unit(m.format_string, m.name), approval_status="DRAFT", basis="inferred",
                      bi={"measure": m.name, "model_table": m.table, "expression_language": "dax"})
    try:
        if "\n…expansion truncated" in expanded:
            raise Untranslatable("measure dependency tree too large to expand", "size")
        node, notes = translate(marked or expanded, model, m.table)
        k.translation_notes += notes
        mult = 1.0
        if isinstance(node, _Bin) and node.op == "*" and isinstance(node.r, _Const):
            node, mult = node.l, node.r.v
        elif isinstance(node, _Bin) and node.op == "*" and isinstance(node.l, _Const):
            node, mult = node.r, node.l.v
        if isinstance(node, _Simple):
            if mult != 1:
                node = _Bin("*", node, _Const(mult))
                mult = 1.0
        if isinstance(node, _Simple):
            k.measure = _spec(node)
            exactish = node.agg.startswith("COUNT")
            k.tolerance = Tolerance(type="exact") if exactish else Tolerance(type="absolute", value=0.01, rounding=2)
            bases = [node.table]
        elif isinstance(node, _Bin) and node.op == "/" and isinstance(node.l, _Simple) and isinstance(node.r, _Simple):
            k.kind, k.numerator, k.denominator, k.multiplier = "ratio", _spec(node.l), _spec(node.r), mult
            k.tolerance = Tolerance(type="absolute", value=0.01 * (mult if mult > 1 else 1), rounding=4)
            if mult == 1 and k.unit == "percent":
                k.translation_notes.append("Format string shows %, value is a 0-1 ratio.")
            bases = [node.l.table]
        elif isinstance(node, _Const):
            raise Untranslatable("measure is a constant", "placeholder")
        else:
            comps: list[MeasureSpec] = []
            if mult != 1:
                node = _Bin("*", node, _Const(mult))
            k.kind, k.formula = "formula", _to_formula(node, comps)
            k.components = comps
            k.tolerance = Tolerance(type="absolute", value=0.01, rounding=4)
            bases = [c.table for c in comps]
        k.translation_notes.append("Translated from DAX - verifies model vs source data, not business intent.")
        for rel in model.relationships:
            tgt = next((t for t in model.tables if t.name == rel.to_table), None)
            if rel.from_table == bases[0] and rel.active and tgt and (tgt.is_date_table or tgt.source_kind == "calculated"):
                dc = _date_column(model, rel.to_table, rel.to_column)
                if dc:
                    k.date_field = fkey(rel.to_table, dc)
                break
    except Untranslatable as e:
        k.translation_notes.append(f"Not auto-translatable [{e.category}]: {e}. Provide the business definition or an expected SQL.")
    except (IndexError, TypeError, KeyError, ValueError, RecursionError) as e:
        k.translation_notes.append(f"Not auto-translatable [parse]: {type(e).__name__}. Provide the business definition or an expected SQL.")
    return k


def describe(k: KPIDefinition) -> str:
    """Plain-language reading of a structured definition."""
    def one(s: Optional[MeasureSpec]) -> str:
        if not s:
            return "?"
        what = s.column or (re.sub(r"\{([^}]*)\}", r"\1", s.column_expr) if s.column_expr else None)
        agg = {"COUNT_ROWS": f"count of {s.table} rows", "COUNT_DISTINCT": f"distinct count of {what}",
               "SUM": f"sum of {what}", "AVG": f"average of {what}", "COUNT": f"count of {what}",
               "MIN": f"minimum of {what}", "MAX": f"maximum of {what}"}[s.aggregation]
        if s.per_field:
            agg = f"{ {'AVG': 'average', 'SUM': 'sum', 'MIN': 'minimum', 'MAX': 'maximum', 'COUNT': 'count'}.get(s.per_aggregation, s.per_aggregation)} per {s.per_field} of the " + agg
        if s.filters:
            agg += " where " + " and ".join(f"{f.field} {f.op} {'' if f.value is None else repr(f.value)}".strip() for f in s.filters)
        return agg

    def tree(n: dict) -> str:
        if "leaf" in n:
            return f"({one(k.components[n['leaf']])})"
        if "const" in n:
            v = n["const"]
            return str(int(v)) if float(v).is_integer() else str(v)
        return f"{tree(n['l'])} {'÷' if n['op'] == '/' else '×' if n['op'] == '*' else n['op']} {tree(n['r'])}"
    if k.expected_sql:
        return "Custom expected SQL"
    if k.kind == "formula" and k.formula:
        return tree(k.formula)
    if k.kind == "ratio":
        return f"({one(k.numerator)}) ÷ ({one(k.denominator)})" + (f" × {k.multiplier:g}" if k.multiplier != 1 else "")
    if k.kind == "freshness":
        return f"age of MAX({k.freshness_field}) ≤ {k.max_age_days} day(s)"
    if k.measure:
        return one(k.measure)
    note = next((n for n in k.translation_notes if n.startswith("Not auto")), "")
    return note.split(". Provide")[0] if note else "Not translatable"
