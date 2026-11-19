"""Rule-based test-case generation from the KPI registry, model metadata and requirements.

Every KPI test carries the exact source SQL (expected) and BI query (actual) it will run, so
testers can review or execute them by hand. Deterministic IDs keep approvals on regeneration.
"""
from __future__ import annotations

from app.core.names import unbracket

from typing import Optional

from ..analyzers.dax import MeasureAnalysis, canonical
from ..analyzers.dax_infer import describe
from ..connectors.powerbi import build_dax
from ..core.environment import EnvironmentConfig
from ..core.models import FilterSpec, KPIDefinition, ModelMetadata, Requirement, TestCase, Tolerance, is_slicer
from .wiring import field_display, slicer_wiring, visual_kind, visual_label
from ..core.names import unbracket
import re

from .sql_generator import MappingError, dq_duplicate_sql, dq_freshness_sql, dq_null_sql, dq_ri_sql, kpi_value_sql

PLACEHOLDER = "<set value>"


def _tol_text(t: Tolerance) -> str:
    base = {"exact": "exact match", "absolute": f"±{t.value:g} absolute", "percent": f"±{t.value:g}%"}[t.type]
    return base + (f", rounded to {t.rounding} dp" if t.rounding is not None else "")


def _ctx_text(ctx: list[FilterSpec]) -> str:
    return "; ".join(f"{f.field} {f.op} {f.value}" for f in ctx) or "no filters"


def _field_for_model_ref(env: EnvironmentConfig, ref: str) -> Optional[str]:
    c = canonical(ref).replace(" ", "")
    for name, fm in env.mapping.fields.items():
        if fm.model and canonical(fm.model).replace(" ", "") == c:
            return name
    return None


def _reachable(kpi: KPIDefinition, env: EnvironmentConfig, field: str) -> bool:
    fm = env.mapping.fields.get(field)
    if not fm:
        return False
    bases = {s.table for s in kpi.all_specs()}
    if not bases:
        return False
    return all(fm.table == b or any({j.left, j.right} == {b, fm.table} for j in env.mapping.joins) for b in bases)


def _dialect(env: EnvironmentConfig) -> str:
    from ..connectors.registry import get_platform
    try:
        return get_platform(env.data.platform).sql_dialect or "databricks"
    except KeyError:
        return "databricks"


def _scripts(kpi: KPIDefinition, env: EnvironmentConfig, ctx: list[FilterSpec], grain: Optional[str]) -> dict:
    out: dict = {}
    try:
        g = kpi_value_sql(kpi, env, ctx, grain=grain, allow_draft=True)
        out["sql"] = g.render_inline(_dialect(env))
    except MappingError as e:
        out["sql"] = f"-- Expected-value SQL not generated for {kpi.id} {kpi.name}\n-- Reason: {e}\n" + \
                     "".join(f"-- {n}\n" for n in kpi.translation_notes)
        out["sql_error"] = str(e)
    if kpi.bi:
        try:
            filters = [(env.mapping.fields[f.field], f) for f in ctx if f.field in env.mapping.fields and env.mapping.fields[f.field].model]
            gfm = env.mapping.fields.get(grain) if grain else None
            out["dax"] = build_dax(kpi.bi.expression or kpi.bi.measure, filters, gfm)
        except Exception as e:  # noqa: BLE001
            out["dax"] = f"// not generated: {e}"
    return out


def _source_text(kpi: KPIDefinition, env: EnvironmentConfig) -> str:
    return describe(kpi)


CARD_TYPES = ("card", "cardVisual", "multiRowCard", "kpi", "gauge")


def visual_context(env: EnvironmentConfig, v) -> tuple[list[FilterSpec], list[str]]:
    """Report + page + slicer + visual filters in force on a visual, translated to logical fields."""
    out, notes = [], list(v.filter_notes)
    for f in v.filter_specs:
        fld = _field_for_model_ref(env, f["ref"])
        if not fld:
            notes.append(f"{f['level']} filter on {f['ref']} has no source mapping - not applied")
            continue
        out.append(FilterSpec(field=fld, op=f["op"], value=f["value"]))
    return out, notes


def generate_tests(env: EnvironmentConfig, kpis: list[KPIDefinition], model: ModelMetadata,
                   analyses: list[MeasureAnalysis], requirements: list[Requirement],
                   scope: Optional[dict] = None) -> list[TestCase]:
    from ..core.scope import in_scope, includes
    tests: list[TestCase] = []
    mode = (scope or {}).get("mode", "all")
    inc = includes(scope) if scope else {"slicers": True, "charts": True, "visual_checks": True, "data_quality": True}
    broad = mode in ("dashboard", "all")
    if scope:
        kpis = in_scope(kpis, model, scope)
    shows: dict[str, list] = {}
    for v in model.visuals:
        if v.page_hidden:
            continue
        for m in v.measures:
            shows.setdefault((m if m.startswith("implicit:") else unbracket(m)).lower(), []).append(v)
    by_measure = {a.measure.lower(): a for a in analyses}
    kpi_by_measure = {(k.bi.measure.lower() if k.bi.measure.startswith("implicit:") else unbracket(k.bi.measure).lower()): k
                      for k in kpis if k.bi}
    req_for_kpi = {k.id: [r.id for r in requirements if k.id in r.kpi_ids] for k in kpis}

    # slicer fields -> a value to test with: the slicer's saved selection, else a value read from the source
    slicer_fields: dict[str, object] = {}
    value_from: dict[str, str] = {}
    slicers_of: dict[str, list] = {}
    for v in model.visuals:
        if is_slicer(v.visual_type) and not v.page_hidden:
            for d in v.dimensions[:1]:
                f = _field_for_model_ref(env, d)
                if not f:
                    continue
                slicers_of.setdefault(f, []).append(v)
                fm = env.mapping.fields[f]
                if v.selection and (f not in slicer_fields or slicer_fields[f] == PLACEHOLDER):
                    slicer_fields[f], value_from[f] = v.selection[0], "the slicer's saved selection"
                elif fm.sample_values and slicer_fields.get(f, PLACEHOLDER) == PLACEHOLDER:
                    slicer_fields[f], value_from[f] = fm.sample_values[0], f"a value read from {env.data.platform}"
                else:
                    slicer_fields.setdefault(f, PLACEHOLDER)
    if mode == "all":
        for f, fm in env.mapping.fields.items():
            if fm.sample_values and f not in slicer_fields:
                slicer_fields[f], value_from[f] = fm.sample_values[0], "a configured sample value"

    for k in kpis:
        ok, why = k.is_independently_defined()
        reqs = sorted(set(req_for_kpi.get(k.id, []) + k.requirement_ids))
        tgt = f"[{k.bi.measure}]" if k.bi else "No BI binding registered"
        basis = ("business definition" if k.basis == "business" else "DAX-inferred definition (draft)")
        pre = [f"KPI definition {k.id} is APPROVED (current: {k.approval_status}, basis: {k.basis})",
               "Source and BI connections configured, or values recorded manually",
               "BI dataset refreshed after the source snapshot used for expected values"]
        expl_note = "" if ok else f" Will report NOT VALIDATED until resolved: {why}"
        if k.kind == "freshness":
            tests.append(TestCase(
                id=f"{k.id}.FRESH", name=f"{k.name}", category="Data Quality", environment_id=env.id, kpi_id=k.id,
                business_objective=k.business_definition, preconditions=pre[:2], source_mapping=_source_text(k, env),
                target_mapping="Source table", steps=["Query MAX(freshness column) on source", "Compute age in days",
                                                      f"Compare against max age {k.max_age_days} day(s)"],
                expected_result=f"Latest record age <= {k.max_age_days} day(s)", executor="kpi_freshness",
                params=_scripts(k, env, [], None), requirement_ids=reqs,
                ai_explanation="Rule-based: stale source data makes every downstream KPI wrong even when formulas are correct."))
            continue

        ctx = [] if k.expected_sql else env.default_context.filters_for(k)
        where_shown = shows.get(unbracket(k.bi.measure).lower(), []) if k.bi else []
        home = next((v for v in where_shown if v.visual_type in CARD_TYPES), where_shown[0] if where_shown else None)
        vnotes: list[str] = []
        if home and not k.expected_sql:
            vctx, vnotes = visual_context(env, home)
            ctx = ctx + vctx
        shown_txt = f" as shown on '{home.page}' › '{visual_label(home)}'" if home else ""
        if k.bi and not k.bi.expression:
            a = by_measure.get(unbracket(k.bi.measure).lower())
            tests.append(TestCase(
                id=f"{k.id}.DAX", name=f"DAX for [{k.bi.measure}] is valid and implements the definition", category="Expression",
                environment_id=env.id, kpi_id=k.id, business_objective="Measure logic is syntactically valid and matches the approved business definition.",
                preconditions=["PBIP uploaded"], source_mapping=k.business_definition,
                target_mapping=f"[{k.bi.measure}] = {a.expression if a else '(measure not found)'}",
                steps=["Static checks (syntax, references, anti-patterns)", "Expand referenced measures",
                       "Compare aggregation, columns, filters and ratio structure with the business definition"],
                expected_result="No errors; aggregation, filters and structure match the definition",
                executor="dax_definition", params={"measure": k.bi.measure, "dax": a.expression if a else ""},
                ai_explanation="Rule-based structural check. Evidence, not proof - the numeric KPI test is authoritative.",
                requirement_ids=reqs))
        tests.append(TestCase(
            id=f"{k.id}.VAL", name=f"Validate {k.name}{shown_txt}", category="KPI",
            environment_id=env.id, kpi_id=k.id, business_objective=k.business_definition,
            preconditions=pre + [f"Not applied: {n}" for n in vnotes],
            source_mapping=_source_text(k, env), target_mapping=tgt,
            steps=[f"Run the expected-value SQL on {env.data.platform} (read-only) - built from the {basis}",
                   f"Query {tgt} on {env.bi.platform} with the identical filter context (DAX below)",
                   f"Compare with tolerance {_tol_text(k.tolerance)}"],
            expected_result=f"BI value equals source value ({_tol_text(k.tolerance)}). Context: {_ctx_text(ctx)}",
            tolerance=k.tolerance, filter_context=ctx, executor="kpi_value", params=_scripts(k, env, ctx, None),
            ai_explanation=("Expected value computed from the source using the business definition; actual value queried from the model with the same context." if k.basis == "business"
                            else "Definition inferred from the DAX: this reconciles model vs source data (refresh, joins, transforms) but cannot prove the business logic.") + expl_note,
            requirement_ids=reqs))
        if k.expected_sql:
            continue
        for g in ([] if mode == "dashboard" or not inc["charts"] else k.grains):
            tests.append(TestCase(
                id=f"{k.id}.GR.{g}", name=f"{k.name} by {g.split('.', 1)[-1]}", category="KPI", environment_id=env.id, kpi_id=k.id,
                business_objective=f"{k.name} is correct for every {g} member, not just in total.", preconditions=pre,
                source_mapping=_source_text(k, env) + f", grouped by {g}", target_mapping=f"{tgt} by {g}",
                steps=["Run the grouped source SQL", "Query the measure grouped by the mapped column",
                       "Match rows by grain key; flag missing/extra members", "Compare each row with tolerance"],
                expected_result=f"Every {g} member matches ({_tol_text(k.tolerance)}); no missing or extra members",
                tolerance=k.tolerance, filter_context=ctx, grain=g, executor="kpi_grain",
                params=_scripts(k, env, ctx, g), requirement_ids=reqs,
                ai_explanation="Totals can match while individual members are wrong (offsetting errors, wrong relationships)."))
        kpi_visual_ids = {v.id for v in where_shown}
        def filters_kpi(sv) -> bool:            # the slicer is not set to ignore every visual showing this KPI on its page
            on_page = [v for v in where_shown if v.page == sv.page]
            return bool(on_page) and any(sv.interactions.get(v.id) != "NoFilter" for v in on_page)
        page_slicers = {f for f, svs in slicers_of.items() if any(filters_kpi(sv) for sv in svs)}
        cands = [] if not inc["slicers"] else \
            [(f, x) for f, x in slicer_fields.items() if (f in page_slicers if mode != "all" else True)]
        for fname, val in cands[:4]:
            if not _reachable(k, env, fname) or k.date_field == fname:
                continue
            sctx = ctx + [FilterSpec(field=fname, op="=", value=val)]
            fm = env.mapping.fields[fname]
            col = fname.split(".", 1)[-1]
            sv = next((x for x in slicers_of.get(fname, []) if x.page in {v.page for v in where_shown}), (slicers_of.get(fname) or [None])[0])
            on = f" on '{sv.page}'" if sv else ""
            tests.append(TestCase(
                id=f"{k.id}.CTX.{fname}", name=f"{k.name} filtered by slicer '{col}' = {val}{on}" if val != PLACEHOLDER
                else f"{k.name} filtered by slicer '{col}'{on} (choose a value)", category="Functional", environment_id=env.id,
                kpi_id=k.id, business_objective=f"When a user picks a value in the '{col}' slicer, {k.name} shows the same number as the source filtered the same way.",
                preconditions=pre + ([f"Choose a value for {col} below, or connect the data platform so one is read automatically"] if val == PLACEHOLDER else
                                     [f"Test value {val!r} comes from {value_from.get(fname, 'configuration')}"]),
                source_mapping=_source_text(k, env) + f", WHERE {fname} = {val}", target_mapping=f"{tgt} with {fm.model} = {val}",
                steps=[f"Run the expected-value SQL with {col} = {val} on {env.data.platform}",
                       f"Query {tgt} with {fm.model} = {val} (KEEPFILTERS, same as selecting it in the slicer)",
                       "Compare with tolerance"],
                expected_result=f"Filtered BI value equals filtered source value ({_tol_text(k.tolerance)})",
                tolerance=k.tolerance, filter_context=sctx, executor="kpi_value", params=_scripts(k, env, sctx, None),
                requirement_ids=reqs,
                ai_explanation="Checks the numbers behind a slicer selection. Whether the slicer is wired to the visuals is checked by the slicer's wiring test."))

    # ---- visuals (requirement / screenshot / hand-picked scopes: only visuals showing the selected KPIs)
    scoped_keys = {(k.bi.measure.lower() if k.bi.measure.startswith("implicit:") else unbracket(k.bi.measure).lower()) for k in kpis if k.bi}
    kpi_visuals = {v.id for v in model.visuals
                   if any((m.lower() if m.startswith("implicit:") else unbracket(m).lower()) in scoped_keys for m in v.measures)}
    kpi_pages = {v.page for v in model.visuals if v.id in kpi_visuals}
    for v in model.visuals:
        if v.page_hidden:
            continue
        if not broad and v.id not in kpi_visuals and not (is_slicer(v.visual_type) and v.page in kpi_pages):
            continue
        label, kind = visual_label(v), visual_kind(v.visual_type)
        if inc["visual_checks"]:
          tests.append(TestCase(
            id=f"VIS.{v.id}.BIND", name=f"{kind} '{label}' on '{v.page}': all fields exist in the model", category="Visual",
            environment_id=env.id, visual_id=v.id, business_objective=f"The {kind.lower()} on page '{v.page}' is bound to valid model objects, so it renders instead of showing an error.",
            source_mapping="Semantic model metadata", target_mapping=f"{kind} '{label}' on {v.page}: " + ", ".join(map(field_display, v.measures + v.dimensions))[:200],
            steps=["Read the visual definition from the report", "Resolve each measure and column against the semantic model"],
            expected_result="All bound measures and columns exist", executor="visual_binding", params={"visual_id": v.id},
            ai_explanation="Renamed or deleted fields make the visual show 'Something's wrong with one or more fields'."))
        dims = [] if is_slicer(v.visual_type) else v.dimensions
        for mref in v.measures:
            k = kpi_by_measure.get(mref.lower() if mref.startswith("implicit:") else unbracket(mref).lower())
            if not k or not dims or k.expected_sql or not inc["charts"]:
                continue
            vctx, vnotes = visual_context(env, v)
            ctx = env.default_context.filters_for(k) + vctx
            g = _field_for_model_ref(env, dims[0])
            axis = field_display(dims[0])
            tests.append(TestCase(
                id=f"VIS.{v.id}.{k.id}", name=f"{kind} '{label}': {k.name} by {axis}", category="Visual",
                environment_id=env.id, kpi_id=k.id, visual_id=v.id,
                business_objective=f"Every {axis} value shown in '{label}' on '{v.page}' matches the source.",
                preconditions=["KPI definition approved", f"{axis} mapped to a source column"] + [f"Not applied: {n}" for n in vnotes],
                source_mapping=_source_text(k, env) + f", grouped by {g or '(unmapped)'}",
                target_mapping=f"{kind}: {k.name} by {axis}",
                steps=[f"Query {k.name} by {axis} with the visual's filters", "Run the grouped source SQL", f"Compare each {axis} value"],
                expected_result=f"All points match ({_tol_text(k.tolerance)})", tolerance=k.tolerance,
                filter_context=ctx, grain=g, executor="kpi_grain",
                params=(_scripts(k, env, ctx, g) if g else {"unmapped_dimension": dims[0]}),
                ai_explanation="Reconciles the visual point by point; totals can match while individual points are wrong."))
        if is_slicer(v.visual_type) and v.dimensions and inc["slicers"]:
            col = field_display(v.dimensions[0])
            w = slicer_wiring(model, v, kpis)
            react = [x["label"] for x in w["targets"] if x["verdict"] in ("reacts", "unknown")]
            ignore = [x["label"] for x in w["targets"] if x["verdict"] in ("ignored", "independent")]
            f = _field_for_model_ref(env, v.dimensions[0])
            val = v.selection[0] if v.selection else (slicer_fields.get(f) if f else None)
            pick = f"'{val}'" if val not in (None, PLACEHOLDER) else "any value"
            lst = lambda xs: ", ".join(f"'{x}'" for x in xs[:8]) + (f" (+{len(xs) - 8} more)" if len(xs) > 8 else "")  # noqa: E731
            synced = sorted({x.page for x in model.visuals if v.sync_group and x.sync_group == v.sync_group and x.page != v.page and not x.page_hidden})
            tests.append(TestCase(
                id=f"VIS.{v.id}.WIRE", name=f"Slicer '{col}' on '{v.page}' is wired to the page's visuals", category="Functional",
                environment_id=env.id, visual_id=v.id,
                business_objective=f"Picking a value in the '{col}' slicer filters every data visual on '{v.page}' (unless deliberately excluded).",
                source_mapping=f"Semantic model relationships from {w['table'] or '?'}", target_mapping=f"Visual interactions on '{v.page}' ({len(w['targets'])} data visuals)",
                steps=["Read the page's visual interactions (Format › Edit interactions)",
                       f"Trace active model relationships from '{w['table']}' to the tables behind each visual, in filter direction",
                       "Flag visuals set to ignore the slicer and visuals the filter cannot reach"],
                expected_result="Every data visual reacts to the slicer; any exclusion is intended (approve the test to confirm)",
                executor="slicer_wiring", params={"visual_id": v.id},
                ai_explanation="Automated from report and model metadata. A visual the filter cannot reach keeps showing unfiltered numbers next to filtered ones."))
            steps = [f"Open the report page '{v.page}'", f"In the '{col}' slicer select {pick}"]
            if react:
                steps.append(f"Check these visuals change: {lst(react)}")
            if ignore:
                steps.append(f"Check these stay unchanged (set to ignore this slicer, or not related to its data): {lst(ignore)}")
            steps.append(f"Compare the KPI values with the expected values of the '… filtered by slicer {col!r}' tests")
            if synced:
                steps.append(f"Go to {lst(synced)}: the same selection should be applied there (synced slicer)")
            steps.append("Clear the selection: the visuals return to their unfiltered values")
            tests.append(TestCase(
                id=f"VIS.{v.id}.UI", name=f"Slicer '{col}' on '{v.page}': select a value and check the visuals (manual)", category="Functional",
                environment_id=env.id, visual_id=v.id,
                business_objective=f"A user selecting a value in the '{col}' slicer sees the page update correctly.",
                preconditions=["Report published or opened in Power BI Desktop with refreshed data"],
                source_mapping="-", target_mapping=f"Slicer {v.dimensions[0]} on {v.page}", steps=steps,
                expected_result=(f"{len(react)} visual(s) update to the selected value" + (f", {len(ignore)} stay unchanged" if ignore else "")
                                 + "; KPI values match the filtered-slicer tests; clearing restores the totals"),
                executor="manual",
                ai_explanation="Done by a person on the live report. The wiring and the numbers are verified automatically by the WIRE and 'filtered by slicer' tests."))

    # ---- data quality on the source tables behind the KPIs in scope
    used_tables = {sp.table for k in kpis for sp in k.all_specs()}
    used_tables |= {env.mapping.fields[f.field].table for k in kpis for sp in k.all_specs() for f in sp.filters
                    if f.field in env.mapping.fields}
    if not inc["data_quality"]:
        keep = lambda t: False  # noqa: E731
    elif scope and mode != "all":
        keep = lambda t: t in used_tables  # noqa: E731
    else:
        keep = lambda t: True  # noqa: E731
    join_keys = set()
    for j in env.mapping.joins:
        for f, fm in env.mapping.fields.items():
            if fm.table == j.left and fm.source == j.left_key:
                join_keys.add(f)
    filter_fields = {f.field for k in kpis for s in k.all_specs()
                     for f in s.filters if f.op not in ("is null", "is not null")}
    for fname in sorted(join_keys | filter_fields | {f for f in env.mapping.fields if f.endswith("_id")}):
        fm = env.mapping.fields.get(fname)
        tm = env.mapping.tables.get(fm.table) if fm else None
        if not fm or not fm.source or not tm or not tm.source or not keep(fm.table):
            continue
        g = dq_null_sql(env, fname)
        tests.append(TestCase(
            id=f"DQ.NULL.{fname}", name=f"No nulls in {fname}", category="Data Quality", environment_id=env.id,
            business_objective=f"{fname} is populated (used as a relationship key or KPI filter).",
            source_mapping=f"{tm.source}.{fm.source}", target_mapping="-", steps=["Count null rows on source"],
            expected_result="0 null rows", executor="dq_nulls", params={"field": fname, "sql": g.render_inline(_dialect(env))},
            ai_explanation="Nulls in keys or filter columns silently drop rows from KPIs or create (Blank) members."))
    for tname, t in env.mapping.tables.items():
        if t.primary_key and t.source and keep(tname):
            g = dq_duplicate_sql(env, tname)
            tests.append(TestCase(
                id=f"DQ.DUP.{tname}", name=f"Unique {t.primary_key} in {tname}", category="Data Quality", environment_id=env.id,
                business_objective="The 'one' side key of a relationship is unique so values are not duplicated.",
                source_mapping=t.source, target_mapping="-", steps=["Compare COUNT(*) with COUNT(DISTINCT key)"],
                expected_result="0 duplicate rows", executor="dq_duplicates", params={"table": tname, "sql": g.render_inline(_dialect(env))},
                ai_explanation="Duplicate keys on the one-side make the relationship ambiguous and inflate totals."))
    fact_tables = {j.left for j in env.mapping.joins} or set(env.mapping.tables)
    for tname in sorted(t for t in fact_tables if keep(t)):
        tm = env.mapping.tables.get(tname)
        if not tm or not tm.source:
            continue
        cands = [(f, fm) for f, fm in env.mapping.fields.items() if fm.table == tname and fm.source and fm.data_type == "date"]
        pick = (next(((f, fm) for f, fm in cands if re.search(r"updat|modif|load|ingest|etl|refresh|_at$|timestamp", fm.source, re.I)), None)
                or (cands[0] if tm.freshness_column is None and len(cands) == 1 else None))
        if not pick:
            continue
        g = dq_freshness_sql(env, pick[0])
        tests.append(TestCase(
            id=f"DQ.FRESH.{tname}", name=f"{tname} data freshness ({pick[1].source})", category="Data Quality", environment_id=env.id,
            business_objective="Dashboard data is no more than 1 day old.", source_mapping=f"{tm.source}.{pick[1].source}",
            target_mapping="-", steps=["Query MAX(timestamp) on source", "Compare age with the limit"],
            expected_result="Latest record <= 1 day old", executor="dq_freshness",
            params={"field": pick[0], "max_age_days": 1, "sql": g.render_inline(_dialect(env))},
            ai_explanation="Stale data makes every KPI wrong even when formulas are correct. Column picked by name - confirm it is the load/update timestamp."))
    for i, j in enumerate(env.mapping.joins):
        lt, rt = env.mapping.tables.get(j.left), env.mapping.tables.get(j.right)
        if not (lt and rt and lt.source and rt.source) or not (keep(j.left) or keep(j.right)):
            continue
        g = dq_ri_sql(env, i)
        tests.append(TestCase(
            id=f"DQ.RI.{j.left}.{j.right}", name=f"Every {j.left}.{j.left_key} exists in {j.right}", category="Data Quality",
            environment_id=env.id, business_objective="Relationships do not drop or blank-bucket rows.",
            source_mapping=f"{j.left} -> {j.right}", target_mapping="-", steps=["Count orphan keys"],
            expected_result="0 orphan rows", executor="dq_ri", params={"join_index": i, "sql": g.render_inline(_dialect(env))},
            ai_explanation="Orphans appear as (Blank) members in Power BI and break slicer totals."))
    order = {"KPI": 0, "Visual": 1, "Functional": 2, "Expression": 3, "Data Quality": 4}
    for i, t in enumerate(sorted(tests, key=lambda t: (order.get(t.category, 9), t.kpi_id or "~", t.id)), 1):
        t.display_id = f"TC{i:03d}"
    return tests
