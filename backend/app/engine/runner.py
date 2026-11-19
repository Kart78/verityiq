"""Test execution: dispatches each TestCase to an executor and produces TestResult + evidence.

Invariants enforced here:
  * expected values come only from the data connector (source) via kpi_value_sql;
  * actual values come only from the BI connector;
  * if either path cannot be established -> BLOCKED / NOT VALIDATED, never PASS;
  * every result records the run mode (LIVE/DEMO) of the connectors that produced it.
"""
from __future__ import annotations

from app.core.names import unbracket

import re
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from ..analyzers.dax import MeasureAnalysis
from ..connectors.base import BIConnector, ConnectorError, DataConnector, UnsupportedCapability
from ..core.environment import EnvironmentConfig
from ..core.models import (Evidence, KPIDefinition, ModelMetadata, RunMode, Status, TestCase, TestResult)
from .compare import compare_grain, compare_values
from .sql_generator import MappingError, dq_duplicate_sql, dq_freshness_sql, dq_null_sql, dq_ri_sql, kpi_value_sql
from .sql_safety import UnsafeSQL


@dataclass
class ExecContext:
    env: EnvironmentConfig
    bi: Optional[BIConnector]
    data: Optional[DataConnector]
    kpis: dict[str, KPIDefinition]
    model: Optional[ModelMetadata]
    analyses: dict[str, MeasureAnalysis]
    data_error: Optional[str] = None       # why the data connection is unavailable (shown on BLOCKED tests)

    @property
    def mode(self) -> RunMode:
        modes = {c.mode for c in (self.bi, self.data) if c}
        return RunMode.DEMO if RunMode.DEMO in modes else RunMode.LIVE


def _no_data(ctx: "ExecContext") -> str:
    why = f" ({ctx.data_error})" if ctx.data_error else ""
    return (f"{ctx.env.data.platform} connection not available{why} - fix it in Settings, or run the expected-value SQL "
            "manually and use 'Record result'.")


class Blocked(Exception):
    def __init__(self, status: Status, msg: str):
        super().__init__(msg)
        self.status = status


def _sql_evidence(g, qr=None, label="Expected value - source SQL") -> Evidence:
    content = {"sql": g.render_inline(), "parameterized_sql": g.sql, "params": g.params}
    if qr:
        content["executed_as"] = qr.extra.get("executed_as")
        content["engine"] = qr.extra.get("engine", qr.connector)
        content["elapsed_ms"] = qr.elapsed_ms
        content["rows"] = qr.rows[:100]
    return Evidence(kind="sql", label=label, content=content, connector=qr.connector if qr else None,
                    mode=qr.mode if qr else None)


def _bi_evidence(qr) -> Evidence:
    c = {"query": qr.query_text, "elapsed_ms": qr.elapsed_ms, "rows": qr.rows[:100]}
    c.update({k: v for k, v in qr.extra.items() if k != "raw_rows"})
    return Evidence(kind="dax" if qr.language == "dax" else "api", label="Actual value - BI query", content=c,
                    connector=qr.connector, mode=qr.mode)


def _context_evidence(ctx: ExecContext, t: TestCase) -> Evidence:
    rows = []
    for f in t.filter_context:
        fm = ctx.env.mapping.fields.get(f.field)
        rows.append({"logical_field": f.field, "op": f.op, "value": f.value,
                     "source": f"{fm.table}.{fm.source or fm.source_expr}" if fm else None,
                     "bi": fm.model if fm else None,
                     "aligned": bool(fm and (fm.source or fm.source_expr) and fm.model)})
    if t.grain:
        fm = ctx.env.mapping.fields.get(t.grain)
        rows.append({"logical_field": t.grain, "op": "GROUP BY", "value": None,
                     "source": (fm.source or fm.source_expr) if fm else None, "bi": fm.model if fm else None,
                     "aligned": bool(fm and fm.model)})
    return Evidence(kind="note", label="Filter / grain alignment", content=rows)


def _resolve_kpi(ctx: ExecContext, t: TestCase) -> KPIDefinition:
    k = ctx.kpis.get(t.kpi_id or "")
    if not k:
        raise Blocked(Status.BLOCKED, f"KPI {t.kpi_id} not found in registry.")
    ok, why = k.is_independently_defined()
    if not ok:
        raise Blocked(Status.NOT_VALIDATED, f"Expected value cannot be independently established: {why}")
    if ctx.data is None:
        raise Blocked(Status.BLOCKED, _no_data(ctx))
    return k


def _bi_filters(ctx: ExecContext, t: TestCase):
    out = []
    for f in t.filter_context:
        if f.value == "<set value>":
            raise Blocked(Status.BLOCKED, f"Filter value for '{f.field}' not set - edit the test and choose a value.")
        fm = ctx.env.mapping.fields.get(f.field)
        if not fm or not fm.model:
            raise Blocked(Status.BLOCKED, f"Filter field '{f.field}' has no BI column mapping - contexts cannot be aligned.")
        out.append((fm, f))
    return out


def _hypothesis(ctx: ExecContext, k: KPIDefinition) -> tuple[Optional[str], Optional[str]]:
    a = ctx.analyses.get(unbracket(k.bi.measure).lower()) if k.bi else None
    if a and a.definition_findings:
        f = a.definition_findings[0]
        return (f"[AI/rule suggestion - unconfirmed] DAX analyzer finding {f.rule}: {f.message}", f.recommendation)
    return ("[AI/rule suggestion - unconfirmed] Possible causes: BI dataset refreshed at a different time than the source "
            "snapshot; model relationship/filter direction differs from source joins; source data changed after approval.",
            "Compare refresh timestamps, then run the .GR tests to locate the members that differ.")


# ------------------------------------------------------------------ executors

def ex_kpi_value(ctx: ExecContext, t: TestCase, r: TestResult):
    k = _resolve_kpi(ctx, t)
    r.evidence.append(_context_evidence(ctx, t))
    bi_filters = _bi_filters(ctx, t)
    try:
        g = kpi_value_sql(k, ctx.env, t.filter_context)
    except MappingError as e:
        raise Blocked(Status.BLOCKED, f"Cannot build source SQL: {e}")
    eq = ctx.data.execute_sql(g.sql, g.params)
    r.evidence.append(_sql_evidence(g, eq))
    if not eq.rows:
        raise Blocked(Status.BLOCKED, "Source query returned no rows.")
    expected = eq.rows[0].get("expected_value", next(iter(eq.rows[0].values())))
    r.expected = expected
    try:
        aq = ctx.bi.query_measure(k.bi.expression or k.bi.measure, bi_filters) if ctx.bi and k.bi else None
    except UnsupportedCapability as e:
        raise Blocked(Status.BLOCKED, f"Expected value computed ({expected}). Actual value not available: {e} - read it in the report "
                                      "and use 'Record result', or add Power BI credentials in Settings.")
    if aq is not None:
        r.evidence.append(_bi_evidence(aq))
        actual = aq.rows[0]["value"] if aq.rows else None
        c = compare_values(expected, actual, t.tolerance or k.tolerance, k.unit)
        r.status, r.expected, r.actual, r.difference, r.message = c.status, c.expected, c.actual, c.difference, c.message
        if c.status == Status.FAIL:
            r.severity, r.business_impact = "High", f"'{k.name}' shown to users differs from the {k.basis} definition."
            r.root_cause_hypothesis, r.remediation_suggestion = _hypothesis(ctx, k)
        return
    if ctx.bi is None or not k.bi:
        r.expected = expected
        r.status = Status.BLOCKED
        raise Blocked(Status.BLOCKED, "No BI connector / measure binding - actual value unavailable.")
    aq = ctx.bi.query_measure(k.bi.measure, bi_filters)
    r.evidence.append(_bi_evidence(aq))
    actual = aq.rows[0]["value"] if aq.rows else None
    c = compare_values(expected, actual, t.tolerance or k.tolerance, k.unit)
    r.status, r.expected, r.actual, r.difference, r.message = c.status, c.expected, c.actual, c.difference, c.message
    if c.status == Status.FAIL:
        r.severity, r.business_impact = "High", f"'{k.name}' shown to users differs from the approved definition."
        r.root_cause_hypothesis, r.remediation_suggestion = _hypothesis(ctx, k)


def ex_kpi_grain(ctx: ExecContext, t: TestCase, r: TestResult):
    if t.params.get("unmapped_dimension"):
        raise Blocked(Status.BLOCKED, f"Visual axis {t.params['unmapped_dimension']} has no logical field mapping to the source.")
    k = _resolve_kpi(ctx, t)
    r.evidence.append(_context_evidence(ctx, t))
    bi_filters = _bi_filters(ctx, t)
    gfm = ctx.env.mapping.fields.get(t.grain or "")
    if not gfm or not gfm.model:
        raise Blocked(Status.BLOCKED, f"Grain '{t.grain}' is not mapped on both sides.")
    try:
        g = kpi_value_sql(k, ctx.env, t.filter_context, grain=t.grain)
    except MappingError as e:
        raise Blocked(Status.BLOCKED, f"Cannot build source SQL: {e}")
    eq = ctx.data.execute_sql(g.sql, g.params)
    r.evidence.append(_sql_evidence(g, eq))
    r.expected = f"{len(eq.rows)} rows"
    if ctx.bi is None:
        raise Blocked(Status.BLOCKED, "No BI connector.")
    try:
        aq = ctx.bi.query_measure(k.bi.expression or k.bi.measure, bi_filters, grain=gfm)
    except UnsupportedCapability as e:
        raise Blocked(Status.BLOCKED, f"Expected rows computed; BI breakdown not available: {e}. Add Power BI credentials in Settings.")
    r.evidence.append(_bi_evidence(aq))
    status, detail, msg = compare_grain(eq.rows, aq.rows, t.tolerance or k.tolerance)
    r.status, r.message = status, msg
    r.expected = f"{len(eq.rows)} rows"
    r.actual = f"{len(aq.rows)} rows"
    r.difference = sum(1 for d in detail if d["status"] != "PASS")
    r.evidence.append(Evidence(kind="rows", label="Row-by-row comparison", content=detail))
    if status == Status.FAIL:
        r.severity, r.business_impact = "Medium", f"Some {t.grain} members of '{k.name}' are wrong."
        r.root_cause_hypothesis, r.remediation_suggestion = _hypothesis(ctx, k)


def ex_kpi_freshness(ctx: ExecContext, t: TestCase, r: TestResult):
    k = _resolve_kpi(ctx, t)
    g = kpi_value_sql(k, ctx.env, [])
    q = ctx.data.execute_sql(g.sql, g.params)
    r.evidence.append(_sql_evidence(g, q, "Freshness query"))
    age = q.rows[0]["age_days"]
    r.expected, r.actual = f"<= {k.max_age_days} day(s)", f"{age} day(s) (latest {q.rows[0]['latest_record']})"
    r.difference = None if age is None else age - k.max_age_days
    if age is None:
        raise Blocked(Status.BLOCKED, "No records found.")
    r.status = Status.PASS if age <= k.max_age_days else Status.FAIL
    r.message = f"Latest source record is {age} day(s) old (limit {k.max_age_days})."
    if r.status == Status.FAIL:
        r.severity, r.business_impact = "High", "Dashboards show stale data; all KPIs are affected."
        r.root_cause_hypothesis = "[AI/rule suggestion - unconfirmed] Upstream ingestion job delayed or failed."
        r.remediation_suggestion = "Check the ingestion pipeline run history and the source system extract."


def ex_dax_definition(ctx: ExecContext, t: TestCase, r: TestResult):
    if ctx.model is None:
        raise Blocked(Status.BLOCKED, "Semantic model metadata not discovered.")
    a = ctx.analyses.get(unbracket(t.params["measure"]).lower())
    if not a:
        r.status, r.severity = Status.FAIL, "High"
        r.message = f"Measure [{t.params['measure']}] registered for this KPI does not exist in the model."
        return
    d = a.to_dict()
    r.evidence.append(Evidence(kind="dax", label="Measure expression", content={"expression": a.expression, "expanded": a.expanded}))
    r.evidence.append(Evidence(kind="metadata", label="DAX analyzer findings", content=d))
    r.expected = "Static: no errors; definition: all checks MATCH"
    r.actual = f"Static: {a.static_status}; definition: {a.definition_status.value}"
    r.status = a.overall_status
    errs = [f for f in a.static_findings + a.definition_findings if f.severity == "error"]
    warns = [f for f in a.static_findings + a.definition_findings if f.severity == "warning"]
    r.message = "; ".join(f.message for f in errs) or (f"{len(warns)} warning(s): " + "; ".join(f.message for f in warns) if warns else "All checks passed.")
    if a.overall_status == Status.NOT_VALIDATED:
        r.message = "Business-definition alignment could not be checked: " + "; ".join(
            c["detail"] for c in a.definition_checks if c["result"] == "UNKNOWN")
    if errs:
        r.severity = "Critical" if any(f.rule.startswith("D") for f in errs) else "High"
        r.root_cause_hypothesis = "[Rule finding - review required] " + errs[0].message
        r.remediation_suggestion = errs[0].recommendation


def ex_visual_binding(ctx: ExecContext, t: TestCase, r: TestResult):
    if ctx.model is None:
        raise Blocked(Status.BLOCKED, "Model metadata not discovered.")
    v = next((v for v in ctx.model.visuals if v.id == t.params["visual_id"]), None)
    if not v:
        raise Blocked(Status.BLOCKED, "Visual no longer present in report metadata.")
    cols = {(tb.name.lower(), c.name.lower()) for tb in ctx.model.tables for c in tb.columns}
    hier = {(tb.name.lower(), c.name.lower()) for tb in ctx.model.tables for c in tb.columns}

    def col_ok(ref: str) -> bool:
        ref = re.sub(r"\]\.[^\]]+$", "]", ref)          # auto date hierarchy level: Table[Date].Year
        tb, _, c = ref.partition("[")
        return (tb.strip("'").lower(), c.rstrip("]").lower()) in cols | hier
    report_ms = {x["name"].lower() for r in ctx.model.reports for x in r.get("report_measures", [])}

    def measure_ok(ref: str) -> bool:
        n = unbracket(ref)
        if ctx.model.measure(n) or n.lower() in report_ms:
            return True
        k = re.match(r"^_(.+) (Status|Goal|Trend)$", n)     # hidden measures generated by a measure KPI
        return bool(k and ctx.model.measure(k.group(1)))
    missing = []
    for m in v.measures:
        if m.startswith("implicit:"):
            if not col_ok(m[9:]):
                missing.append(m[9:])
        elif not measure_ok(m):
            missing.append(m)
    for d in v.dimensions:
        if not col_ok(d):
            missing.append(d)
    r.evidence.append(Evidence(kind="metadata", label="Visual bindings", content=v.model_dump()))
    r.expected, r.actual = "0 missing fields", f"{len(missing)} missing"
    r.status = Status.PASS if not missing else Status.FAIL
    r.message = "All fields resolve." if not missing else "Missing: " + ", ".join(missing)
    if missing:
        r.severity = "High"


def _dq(ctx: ExecContext, r: TestResult, g, col: str, label: str):
    if ctx.data is None:
        raise Blocked(Status.BLOCKED, _no_data(ctx))
    q = ctx.data.execute_sql(g.sql, g.params)
    r.evidence.append(_sql_evidence(g, q, label))
    bad = q.rows[0][col] or 0
    r.expected, r.actual, r.difference = 0, bad, bad
    r.status = Status.PASS if bad == 0 else Status.FAIL
    r.message = f"{bad} offending row(s)." if bad else "No offending rows."
    if bad:
        r.severity = "High" if "orphan" in col or "dup" in col else "Medium"
        r.business_impact = "Counts and relationships in dashboards may be inflated or show (Blank)."


def ex_dq_nulls(ctx, t, r):
    _dq(ctx, r, dq_null_sql(ctx.env, t.params["field"]), "null_rows", "Null check SQL")


def ex_dq_duplicates(ctx, t, r):
    _dq(ctx, r, dq_duplicate_sql(ctx.env, t.params["table"]), "duplicate_rows", "Duplicate check SQL")


def ex_dq_ri(ctx, t, r):
    _dq(ctx, r, dq_ri_sql(ctx.env, t.params["join_index"]), "orphan_rows", "Referential integrity SQL")


def ex_dq_freshness(ctx, t, r):
    if ctx.data is None:
        raise Blocked(Status.BLOCKED, _no_data(ctx))
    g = dq_freshness_sql(ctx.env, t.params["field"])
    q = ctx.data.execute_sql(g.sql, g.params)
    r.evidence.append(_sql_evidence(g, q, "Freshness query"))
    age, lim = q.rows[0]["age_days"], t.params.get("max_age_days", 1)
    if age is None:
        raise Blocked(Status.BLOCKED, "No records found.")
    r.expected, r.actual, r.difference = f"<= {lim} day(s)", f"{age} day(s) old (latest {q.rows[0]['latest_record']})", age - lim
    r.status = Status.PASS if age <= lim else Status.FAIL
    r.message = f"Source data is {age} day(s) old. Expected within {lim} day(s)."
    if r.status == Status.FAIL:
        r.severity, r.business_impact = "High", "Dashboards show stale data; all KPIs are affected."
        r.root_cause_hypothesis = "[Rule suggestion - unconfirmed] Upstream load delayed or failed."
        r.remediation_suggestion = "Check the ingestion pipeline and dataset refresh history."


def ex_slicer_wiring(ctx: ExecContext, t: TestCase, r: TestResult):
    """Automated: the slicer is configured to filter the page's visuals and the model can carry its filter."""
    from .wiring import slicer_wiring
    if ctx.model is None:
        raise Blocked(Status.BLOCKED, "Model metadata not discovered.")
    s = next((v for v in ctx.model.visuals if v.id == t.params.get("visual_id")), None)
    if not s:
        raise Blocked(Status.BLOCKED, "Slicer no longer present in the report.")
    w = slicer_wiring(ctx.model, s, list(ctx.kpis.values()))
    tg = w["targets"]
    r.evidence.append(Evidence(kind="metadata", label="Slicer → visual wiring (interactions + relationship paths)", content={
        "slicer_field": w["field"], "slicer_table": w["table"],
        "visuals": [{"visual": x["label"], "type": x["kind"], "interaction": x["interaction"], "verdict": x["verdict"],
                     "relationship_path": " → ".join(x["path"]) if x["path"] else None, "data_tables": x["tables"]} for x in tg]}))
    react = [x for x in tg if x["verdict"] == "reacts"]
    ign = [x for x in tg if x["verdict"] == "ignored"]
    nop = [x for x in tg if x["verdict"] == "no_path"]
    unk = [x for x in tg if x["verdict"] == "unknown"]
    ind = [x for x in tg if x["verdict"] == "independent"]
    names = lambda xs: ", ".join(f"'{x['label']}'" for x in xs[:6]) + (f" +{len(xs) - 6} more" if len(xs) > 6 else "")  # noqa: E731
    r.expected = "every visual on the page reacts (or is deliberately excluded)"
    r.actual = f"{len(react)} react, {len(ign)} set to ignore, {len(nop)} unreachable" + \
        (f", {len(ind)} independent" if ind else "") + (f", {len(unk)} undetermined" if unk else "")
    tail = (f" {len(ind)} visual(s) show only dimension/disconnected data and are normally unaffected: {names(ind)}." if ind else "")
    if w["missing_field"]:
        r.status, r.severity = Status.FAIL, "High"
        r.message = f"The slicer's field {w['field']} no longer exists in the model - the slicer is broken."
    elif nop and t.approval_status != "APPROVED":
        r.status, r.severity = Status.FAIL, "High"
        r.message = (f"Slicer has no effect on {names(nop)}: no active relationship carries a filter from '{w['table']}' to "
                     f"{', '.join(sorted({t for x in nop for t in x['tables']}))}. Those visuals keep showing unfiltered values "
                     "next to filtered ones. Fix the relationship (or its direction), or approve this test if that is intended." + tail)
    elif not tg:
        r.status, r.message = Status.NOT_VALIDATED, "No other data visuals on this page to filter."
    elif ign and t.approval_status != "APPROVED":
        r.status = Status.NOT_VALIDATED
        r.message = (f"{len(react)} visual(s) react. {len(ign)} are set to ignore this slicer (Edit interactions = None): {names(ign)}. "
                     "If that is intended, approve this test; otherwise turn the interaction back on." + tail)
    elif not react and unk:
        r.status, r.message = Status.NOT_VALIDATED, f"Could not determine the data tables of {names(unk)}." + tail
    elif not react and not nop and not ign:
        r.status, r.message = Status.NOT_VALIDATED, "No visual on this page depends on the slicer's data." + tail
    else:
        r.status = Status.PASS
        r.message = f"{len(react)} data visual(s) on the page react to the slicer" + \
            (f"; {len(ign) + len(nop)} excluded on purpose (approved)" if (ign or nop) else "") + (f"; {len(unk)} undetermined" if unk else "") + "." + tail


def ex_manual(ctx, t, r):
    r.status = Status.NOT_EXECUTED
    r.message = "Manual test - follow the steps on the report and record the outcome (Passed / Failed / Blocked)."


EXECUTORS: dict[str, Callable] = {
    "kpi_value": ex_kpi_value, "kpi_grain": ex_kpi_grain, "kpi_freshness": ex_kpi_freshness,
    "dax_definition": ex_dax_definition, "visual_binding": ex_visual_binding, "dq_nulls": ex_dq_nulls,
    "dq_duplicates": ex_dq_duplicates, "dq_ri": ex_dq_ri, "dq_freshness": ex_dq_freshness, "manual": ex_manual,
    "slicer_wiring": ex_slicer_wiring,
}


def execute_test(ctx: ExecContext, t: TestCase, run_id: str) -> TestResult:
    r = TestResult(test_id=t.id, run_id=run_id, status=Status.ERROR, mode=ctx.mode,
                   executed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    t0 = time.time()
    try:
        fn = EXECUTORS.get(t.executor)
        if fn is None:
            raise Blocked(Status.BLOCKED, f"No executor '{t.executor}'")
        fn(ctx, t, r)
    except Blocked as b:
        r.status, r.message = b.status, str(b)
    except UnsupportedCapability as e:
        hint = (" - add Power BI service-principal credentials in Settings, or read the value in the report and use 'Record result'."
                if e.capability.startswith("query_measure") else "")
        r.status, r.message = Status.BLOCKED, f"Not available: {e}{hint}"
    except (ConnectorError, UnsafeSQL) as e:
        r.status, r.message = Status.ERROR, str(e)
        r.severity = "High"
        r.root_cause_hypothesis = "[Rule suggestion] The query failed on the platform - check the measure/SQL in evidence."
    except Exception as e:  # noqa: BLE001
        r.status, r.message = Status.ERROR, f"{type(e).__name__}: {e}"
        r.evidence.append(Evidence(kind="note", label="Traceback", content=traceback.format_exc()[-2000:]))
    r.duration_ms = int((time.time() - t0) * 1000)
    if ctx.mode == RunMode.DEMO:
        r.evidence.insert(0, Evidence(kind="note", label="Run mode",
                                      content="DEMO run: sample data and demo adapters. Not evidence about any production system."))
    return r


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("R%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def summarize(results: list[TestResult]) -> dict:
    counts = {s.value: 0 for s in Status}
    for r in results:
        counts[r.status.value] += 1
    executed = counts["PASS"] + counts["FAIL"]
    return {"total": len(results), "counts": counts,
            "validated_pass_rate": round(counts["PASS"] / executed * 100, 1) if executed else None,
            "coverage": round(executed / len(results) * 100, 1) if results else 0}
