"""DAX expression analyzer (Power BI adapter-specific; never applied to other languages).

Two layers:
  1. Static checks   - syntax balance, unknown references, known anti-patterns.
  2. Definition check - does the DAX implement the APPROVED business definition in the KPI
                        registry? (aggregation, column, filters, ratio structure)

Layer 2 is a heuristic structural comparison. It is evidence, not proof: the numeric
expected-vs-actual reconciliation remains the authoritative test.
"""
from __future__ import annotations

from app.core.names import unbracket

import re
from dataclasses import dataclass, field
from typing import Optional

from ..core.environment import EnvironmentConfig
from ..core.models import FilterSpec, KPIDefinition, MeasureSpec, ModelMetadata, Status

KEYWORDS = {"NOT", "IN", "VAR", "RETURN", "TRUE", "FALSE", "AND", "OR", "ASC", "DESC", "DEFINE", "EVALUATE", "MEASURE",
            "ORDER", "BY", "START", "AT", "COLUMN", "TABLE", "FUNCTION"}
MAX_EXPANDED = 200_000

TIME_INTEL = {"TOTALYTD", "TOTALQTD", "TOTALMTD", "SAMEPERIODLASTYEAR", "DATEADD", "DATESYTD", "DATESQTD",
              "DATESMTD", "PARALLELPERIOD", "PREVIOUSMONTH", "PREVIOUSYEAR", "PREVIOUSQUARTER", "DATESINPERIOD",
              "NEXTMONTH", "NEXTYEAR"}

# ------------------------------------------------------------------ tokenizer

_TOKEN_RE = re.compile(r"""
    (?P<comment>//[^\n]*|--[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:[^"]|"")*")
  | (?P<qtable>'(?:[^']|'')*')
  | (?P<bracket>\[(?:[^\]]|\]\])*\])
  | (?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<ident>[^\W\d][\w.]*)
  | (?P<op><>|>=|<=|==|&&|\|\||[-+*/=<>(),{}&^!:;@?])
  | (?P<ws>\s+)
  | (?P<bad>.)
""", re.S | re.X)


@dataclass
class Tok:
    kind: str
    text: str


def tokenize(expr: str) -> list[Tok]:
    return [Tok(m.lastgroup, m.group()) for m in _TOKEN_RE.finditer(expr) if m.lastgroup not in ("ws", "comment")]


def _tname(t: Tok) -> str:
    return t.text[1:-1].replace("''", "'") if t.kind == "qtable" else t.text


def canonical(expr: str) -> str:
    """Lower-case, whitespace-free form with table refs normalised to 'table'[col]."""
    toks = tokenize(expr)
    out = []
    for i, t in enumerate(toks):
        if t.kind in ("ident", "qtable") and i + 1 < len(toks) and toks[i + 1].kind == "bracket" and not (t.kind == "ident" and t.text.upper() in KEYWORDS):
            out.append(f"'{_tname(t).lower()}'")
        elif t.kind == "string":
            out.append(t.text.lower())
        else:
            out.append(t.text.lower())
    return " ".join(out)


# ------------------------------------------------------------------ findings

@dataclass
class Finding:
    rule: str
    severity: str        # error | warning | info
    message: str
    recommendation: str = ""


@dataclass
class MeasureAnalysis:
    measure: str
    table: str
    expression: str
    expanded: str
    kpi_id: Optional[str] = None
    static_findings: list[Finding] = field(default_factory=list)
    definition_findings: list[Finding] = field(default_factory=list)
    definition_checks: list[dict] = field(default_factory=list)   # {check, result, detail}

    @property
    def static_status(self) -> str:
        if any(f.severity == "error" for f in self.static_findings):
            return "FAIL"
        if any(f.severity == "warning" for f in self.static_findings):
            return "PASS WITH WARNINGS"
        return "PASS"

    @property
    def definition_status(self) -> Status:
        if self.kpi_id is None:
            return Status.NOT_VALIDATED
        if any(f.severity == "error" for f in self.definition_findings):
            return Status.FAIL
        if any(c["result"] == "UNKNOWN" for c in self.definition_checks):
            return Status.NOT_VALIDATED
        return Status.PASS

    @property
    def overall_status(self) -> Status:
        if self.static_status == "FAIL" or self.definition_status == Status.FAIL:
            return Status.FAIL
        if self.definition_status == Status.NOT_VALIDATED:
            return Status.NOT_VALIDATED
        return Status.PASS

    def to_dict(self) -> dict:
        return {
            "measure": self.measure, "table": self.table, "expression": self.expression, "kpi_id": self.kpi_id,
            "static_status": self.static_status, "definition_status": self.definition_status.value,
            "overall_status": self.overall_status.value,
            "static_findings": [f.__dict__ for f in self.static_findings],
            "definition_findings": [f.__dict__ for f in self.definition_findings],
            "definition_checks": self.definition_checks,
            "warnings": sum(1 for f in self.static_findings + self.definition_findings if f.severity == "warning"),
        }


# ------------------------------------------------------------------ analyzer

class DaxAnalyzer:
    def __init__(self, model: ModelMetadata):
        self.model = model
        self.measures = {m.name.lower(): m for m in model.all_measures()}
        self.columns = {(t.name.lower(), c.name.lower()) for t in model.tables for c in t.columns}
        self.tables = {t.name.lower() for t in model.tables}
        self.has_date_table = any(t.is_date_table for t in model.tables)

    # ---- expansion
    def expand(self, expr: str, stack: tuple[str, ...] = (), mark: bool = False) -> tuple[str, list[str]]:
        """Inline referenced measures. mark=True wraps each as __HOME("table", expr) so unqualified
        column references keep resolving against the referenced measure's own home table."""
        self._mark = mark
        key = (expr, stack[-1] if stack else None, mark)
        memo = self.__dict__.setdefault("_memo", {})
        if key in memo and not any(p.startswith("Circular") for p in memo[key][1]):
            return memo[key]
        res = self._expand(expr, stack)
        if len(res[0]) > MAX_EXPANDED:
            res = (res[0][:MAX_EXPANDED] + "\n…expansion truncated", res[1])
        memo[key] = res
        return res

    def _expand(self, expr: str, stack: tuple[str, ...] = ()) -> tuple[str, list[str]]:
        problems: list[str] = []
        if len(stack) > 40:
            return expr, ["Measure dependency chain deeper than 40 levels"]
        toks = tokenize(expr)
        out = []
        for i, t in enumerate(toks):
            prev = toks[i - 1] if i else None
            if t.kind == "bracket" and not (prev and prev.kind in ("ident", "qtable") and not (prev.kind == "ident" and prev.text.upper() in KEYWORDS)):
                name = t.text[1:-1].replace("]]", "]").lower()
                if name in self.measures:
                    if name in stack:
                        problems.append(f"Circular reference: {' -> '.join(stack + (name,))}")
                        out.append(t.text)
                        continue
                    mm = self.measures[name]
                    sub, p = self.expand(mm.expression, stack + (name,), getattr(self, "_mark", False))
                    problems += p
                    if getattr(self, "_mark", False):
                        out.append(f'__HOME ( "{mm.table.replace(chr(34), chr(34) * 2)}" , {sub} )')
                    else:
                        out.append(f"( {sub} )")
                    continue
            out.append(t.text if t.kind != "qtable" else t.text)
        return " ".join(out), problems

    # ---- static
    def static(self, name: str, expr: str) -> list[Finding]:
        f: list[Finding] = []
        if not expr.strip():
            return [Finding("D010", "error", "Measure expression is empty.")]
        toks = tokenize(expr)
        bad = [t.text for t in toks if t.kind == "bad"]
        if bad:
            f.append(Finding("D001", "error", f"Unrecognised characters: {''.join(bad)[:20]} (unterminated string or bracket?)"))
        depth = 0
        for t in toks:
            depth += t.text == "("
            depth -= t.text == ")"
            if depth < 0:
                break
        if depth != 0:
            f.append(Finding("D001", "error", "Unbalanced parentheses.", "Check that every '(' has a matching ')'."))
        ext_cols = {t.text[1:-1].lower() for t in toks if t.kind == "string"}
        upper = {t.text.upper() for t in toks if t.kind == "ident"}
        implicit_cols = set()
        if any(t.text == "{" for t in toks) or upper & {"GENERATESERIES", "DATATABLE"}:
            implicit_cols |= {"value"} | {f"value{i}" for i in range(1, 64)}
        if upper & {"CALENDAR", "CALENDARAUTO"}:
            implicit_cols.add("date")
        vars_ = {toks[i + 1].text.lower() for i, t in enumerate(toks[:-1]) if t.text.upper() == "VAR"}
        for i, t in enumerate(toks):
            prev = toks[i - 1] if i else None
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if t.kind == "bracket" and prev and prev.kind in ("ident", "qtable") and prev.text.lower() not in vars_ \
                    and not (prev.kind == "ident" and prev.text.upper() in KEYWORDS):
                tbl, col = _tname(prev).lower(), t.text[1:-1].replace("]]", "]").lower()
                if tbl not in self.tables:
                    f.append(Finding("D003", "error", f"Unknown table '{_tname(prev)}'."))
                elif (tbl, col) not in self.columns:
                    if col in self.measures:
                        f.append(Finding("D007", "info", f"Measure [{t.text[1:-1]}] is table-qualified.",
                                         "Reference measures without a table prefix to distinguish them from columns."))
                    else:
                        f.append(Finding("D003", "error", f"Unknown column {_tname(prev)}[{t.text[1:-1]}]."))
            elif t.kind == "bracket":
                n = t.text[1:-1].replace("]]", "]").lower()
                if n not in self.measures:
                    if any(c == n for (_, c) in self.columns):
                        f.append(Finding("D008", "info", f"Unqualified column reference {t.text}.",
                                         "Qualify column references as Table[Column]."))
                    elif n in ext_cols or n in implicit_cols:
                        pass  # extension column (ADDCOLUMNS/SUMMARIZE "Name") or table-constructor column
                    else:
                        f.append(Finding("D002", "error", f"Reference {t.text} is not a measure or column in the model.",
                                         "Create the missing measure or correct the name."))
            elif t.kind == "op" and t.text == "/":
                f.append(Finding("D004", "warning", "Division operator '/' used.",
                                 "Use DIVIDE(numerator, denominator) to return BLANK instead of an error on divide-by-zero."))
            elif t.kind == "ident" and t.text.upper() == "FILTER" and nxt and nxt.text == "(":
                arg = toks[i + 2] if i + 2 < len(toks) else None
                after = toks[i + 3] if i + 3 < len(toks) else None
                if arg and arg.kind in ("ident", "qtable") and _tname(arg).lower() in self.tables and after and after.text == ",":
                    f.append(Finding("D005", "warning", f"FILTER over the whole table {_tname(arg)}.",
                                     "Filter a column (e.g. KEEPFILTERS(Table[Col] = value)) instead of iterating the full table."))
            elif t.kind == "ident" and t.text.upper() in TIME_INTEL and not self.has_date_table:
                f.append(Finding("D006", "warning", f"{t.text.upper()} used but no table is marked as a date table.",
                                 "Mark a contiguous calendar table as the date table."))
            elif t.kind == "ident" and t.text.upper() in ("IFERROR", "ISERROR"):
                f.append(Finding("D009", "warning", f"{t.text.upper()} forces cell-by-cell evaluation.",
                                 "Prefer DIVIDE or explicit checks."))
        _, problems = self.expand(expr, (name.lower(),))
        for p in problems:
            f.append(Finding("D011", "error", p))
        # de-duplicate
        seen, uniq = set(), []
        for x in f:
            if (x.rule, x.message) not in seen:
                seen.add((x.rule, x.message))
                uniq.append(x)
        return uniq

    # ---- definition alignment
    def _col_ref(self, env: EnvironmentConfig, logical: str) -> Optional[str]:
        fm = env.mapping.fields.get(logical)
        if not fm or not fm.model:
            return None
        return canonical(fm.model).replace(" ", "")

    def _check_measure(self, env, spec: MeasureSpec, text: str, label: str, a: MeasureAnalysis):
        flat = text.replace(" ", "")
        # aggregation
        tbl_model = (env.mapping.tables.get(spec.table).model if env.mapping.tables.get(spec.table) else None)
        tref = f"'{tbl_model.lower().replace(' ', '')}'" if tbl_model else None
        col = self._col_ref(env, spec.column) if spec.column else None
        pats = {
            "COUNT_DISTINCT": [rf"distinctcount(noblank)?\({re.escape(col or '§')}\)",
                               rf"countrows\((distinct|values)\({re.escape(col or '§')}\)\)"],
            "COUNT": [rf"counta?\({re.escape(col or '§')}\)"],
            "COUNT_ROWS": [rf"countrows\({re.escape(tref.strip(chr(39)) if tref else '§')}\)", rf"countrows\({re.escape(tref or '§')}\)"],
            "SUM": [rf"sum\({re.escape(col or '§')}\)", rf"sumx\([^,]+,{re.escape(col or '§')}\)"],
            "AVG": [rf"average\({re.escape(col or '§')}\)", rf"averagex\([^,]+,{re.escape(col or '§')}\)",
                    rf"sum\({re.escape(col or '§')}\)/count\({re.escape(col or '§')}\)",
                    rf"divide\(sum\({re.escape(col or '§')}\),count\({re.escape(col or '§')}\)"],
            "MIN": [rf"min\({re.escape(col or '§')}\)"], "MAX": [rf"max\({re.escape(col or '§')}\)"],
        }[spec.aggregation]
        if spec.column and not col:
            a.definition_checks.append({"check": f"{label}: aggregation", "result": "UNKNOWN",
                                        "detail": f"Field '{spec.column}' has no model column mapping."})
        else:
            hit = any(re.search(p, flat) for p in pats)
            target = f"{spec.aggregation}({spec.column or spec.table})"
            a.definition_checks.append({"check": f"{label}: aggregation {target}", "result": "MATCH" if hit else "MISMATCH",
                                        "detail": "" if hit else f"Expected {target} on {col or tref}; not found in DAX."})
            if not hit:
                a.definition_findings.append(Finding("A001", "error",
                    f"{label}: definition requires {target} but the DAX does not aggregate {col or tref} that way.",
                    "Align the measure's aggregation with the approved KPI definition, or update the definition (requires approval)."))
            if spec.aggregation == "COUNT" and re.search(r"countrows\(", flat) and not hit:
                pass
        # filters
        for flt in spec.filters:
            self._check_filter(env, flt, flat, label, spec, a)

    def _check_filter(self, env, flt: FilterSpec, flat: str, label: str, spec: MeasureSpec, a: MeasureAnalysis):
        col = self._col_ref(env, flt.field)
        desc = f"{flt.field} {flt.op} {flt.value if flt.value is not None else ''}".strip()
        if not col:
            a.definition_checks.append({"check": f"{label}: filter {desc}", "result": "UNKNOWN",
                                        "detail": f"Field '{flt.field}' has no model column mapping."})
            return
        if flt.op == "is not null" and spec.column == flt.field and spec.aggregation in ("AVG", "COUNT", "SUM", "COUNT_DISTINCT", "MIN", "MAX"):
            a.definition_checks.append({"check": f"{label}: filter {desc}", "result": "MATCH",
                                        "detail": "Implicit - DAX aggregations ignore BLANK."})
            return
        fm = env.mapping.fields[flt.field]

        def lit(v):
            v = fm.model_value_map.get(v, v) if not isinstance(v, (list, dict)) else v
            if isinstance(v, bool):
                return [f"true()", "true"] if v else ["false()", "false"]
            if isinstance(v, (int, float)):
                return [str(v)]
            return [f'"{str(v).lower()}"'.replace(" ", "")]

        c = re.escape(col)
        found = False
        if flt.op in ("=", "!="):
            ops = ["=", "=="] if flt.op == "=" else ["<>"]
            for o in ops:
                for lv in lit(flt.value):
                    if re.search(rf"{c}{re.escape(o)}{re.escape(lv)}", flat) or re.search(rf"{re.escape(lv)}{re.escape(o)}{c}", flat):
                        found = True
            if flt.op == "=" and isinstance(flt.value, bool) and flt.value and re.search(rf"[,(]{c}[,)]", flat):
                found = True  # CALCULATE(..., Table[Flag]) shorthand
        elif flt.op in ("in", "not in"):
            vals = flt.value if isinstance(flt.value, list) else [flt.value]
            m = re.search(rf"(not)?{c}in\{{([^}}]*)\}}", flat)
            if m:
                got = {x.strip().lower().replace(" ", "") for x in m.group(2).split(",")}
                want = {lit(v)[0] for v in vals}
                found = got == want and bool(m.group(1)) == (flt.op == "not in")
        elif flt.op in ("is null", "is not null"):
            found = bool(re.search(rf"(not\()?isblank\({c}\)", flat))
        else:
            found = bool(re.search(rf"{c}{re.escape(flt.op)}", flat))
        a.definition_checks.append({"check": f"{label}: filter {desc}", "result": "MATCH" if found else "MISMATCH",
                                    "detail": "" if found else f"No predicate on {col} matching '{desc}' found."})
        if not found:
            a.definition_findings.append(Finding("A002", "error",
                f"{label}: approved definition filters {desc}, but the DAX does not apply this filter.",
                f"Add the filter on {fm.model} to the {label.lower()} or confirm the definition."))

    def _divide_args(self, expanded: str) -> Optional[tuple[str, str]]:
        toks = tokenize(expanded)
        for i, t in enumerate(toks):
            if t.kind == "ident" and t.text.upper() == "DIVIDE" and i + 1 < len(toks) and toks[i + 1].text == "(":
                depth, args, cur = 0, [], []
                for tk in toks[i + 2:]:
                    if tk.text in "({":
                        depth += 1
                    elif tk.text in ")}":
                        if depth == 0:
                            args.append(cur)
                            break
                        depth -= 1
                    if tk.text == "," and depth == 0:
                        args.append(cur)
                        cur = []
                        continue
                    cur.append(tk.text)
                if len(args) >= 2:
                    return canonical(" ".join(args[0])), canonical(" ".join(args[1]))
        # fallback: top-level "/" split
        depth, left, right, hit = 0, [], [], False
        for tk in toks:
            if tk.text == "(":
                depth += 1
            elif tk.text == ")":
                depth -= 1
            if tk.text == "/" and depth <= 1 and not hit:
                hit = True
                continue
            (right if hit else left).append(tk.text)
        if hit:
            return canonical(" ".join(left)), canonical(" ".join(right))
        return None

    def analyze(self, env: EnvironmentConfig, kpis: list[KPIDefinition]) -> list[MeasureAnalysis]:
        by_measure = {unbracket(k.bi.measure).lower(): k for k in kpis if k.bi and k.bi.measure}
        results = []
        for m in self.model.all_measures():
            expanded, _ = self.expand(m.expression, (m.name.lower(),))
            a = MeasureAnalysis(measure=m.name, table=m.table, expression=m.expression, expanded=expanded)
            a.static_findings = self.static(m.name, m.expression)
            kpi = by_measure.get(m.name.lower())
            if kpi:
                a.kpi_id = kpi.id
                self.align(env, kpi, a)
            results.append(a)
        return results

    def align(self, env: EnvironmentConfig, kpi: KPIDefinition, a: MeasureAnalysis) -> None:
        if kpi.basis == "inferred":
            a.definition_checks.append({"check": "Business definition available", "result": "UNKNOWN",
                                        "detail": "No business definition supplied - the only definition is inferred from this DAX, so business logic cannot be verified."})
            return
        if kpi.expected_sql and not (kpi.measure or kpi.numerator):
            a.definition_checks.append({"check": "Structured definition", "result": "UNKNOWN",
                                        "detail": "Only custom expected SQL supplied - logic is verified numerically by the KPI tests."})
            return
        ok, why = kpi.is_independently_defined()
        structured = (kpi.kind == "simple" and kpi.measure) or (kpi.kind == "ratio" and kpi.numerator and kpi.denominator)
        if kpi.kind == "freshness" or not structured:
            a.definition_checks.append({"check": "Business definition available", "result": "UNKNOWN", "detail": why or "Not applicable"})
            return
        canon = canonical(a.expanded)
        if kpi.kind == "simple":
            self._check_measure(env, kpi.measure, canon, "Measure", a)
        else:
            parts = self._divide_args(a.expanded)
            if not parts:
                a.definition_checks.append({"check": "Ratio structure", "result": "MISMATCH",
                                            "detail": "Definition is a ratio but no DIVIDE() or '/' found."})
                a.definition_findings.append(Finding("A003", "error", "KPI is defined as a ratio but the DAX has no division.",
                                                     "Implement as DIVIDE(<numerator>, <denominator>)."))
                return
            a.definition_checks.append({"check": "Ratio structure", "result": "MATCH", "detail": "DIVIDE/ratio found"})
            self._check_measure(env, kpi.numerator, parts[0], "Numerator", a)
            self._check_measure(env, kpi.denominator, parts[1], "Denominator", a)
        if kpi.multiplier not in (1, 1.0):
            has = re.search(rf"\*\s*{int(kpi.multiplier)}\b|{int(kpi.multiplier)}\s*\*", canon)
            a.definition_checks.append({"check": f"Scaling x{int(kpi.multiplier)}", "result": "MATCH" if has else "MISMATCH",
                                        "detail": "" if has else f"Definition expects value scaled by {kpi.multiplier}."})
            if not has:
                a.definition_findings.append(Finding("A004", "warning",
                    f"Definition scales the result by {kpi.multiplier}; DAX does not. Check format string vs unit.",
                    "Either multiply in DAX or set multiplier: 1 and use a percentage format string."))


def analyze_model(model: ModelMetadata, env: EnvironmentConfig, kpis: list[KPIDefinition]) -> list[MeasureAnalysis]:
    return DaxAnalyzer(model).analyze(env, kpis)


def kpi_measure_missing(model: ModelMetadata, kpis: list[KPIDefinition]) -> list[KPIDefinition]:
    return [k for k in kpis if k.bi and k.bi.measure and not model.measure(k.bi.measure)]
