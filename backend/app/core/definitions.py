"""Business KPI definition template: export (pre-listed with every measure) and import.

Business / data owners fill the definition columns. Filters use DAX-style column predicates,
e.g.  Cases[Status] = "Closed"; Cases[FirstContactResolved] = TRUE
so they map 1:1 onto model columns and, through the project mapping, onto source columns.
"""
from __future__ import annotations

from app.core.names import unbracket

import csv
import io
from typing import Optional

from ..analyzers.dax_infer import Untranslatable, _Ctx, _Interp, describe, fkey, parse_dax
from .models import FilterSpec, KPIDefinition, MeasureSpec, ModelMetadata, Tolerance

COLUMNS = ["KPI ID", "KPI Name", "Power BI Measure", "Business Definition", "Type (simple/ratio)", "Aggregation",
           "Table", "Column", "Filters", "Denominator Aggregation", "Denominator Table", "Denominator Column",
           "Denominator Filters", "Multiplier", "Tolerance Type (exact/absolute/percent)", "Tolerance Value",
           "Rounding (dp)", "Expected SQL (optional)", "Owner", "DAX interpretation (reference only)", "DAX expression (reference only)"]
AGGS = ["COUNT_ROWS", "COUNT_DISTINCT", "SUM", "AVG", "COUNT", "MIN", "MAX"]
HELP = ("Fill one row per KPI. Aggregation: COUNT_ROWS | COUNT_DISTINCT | SUM | AVG | COUNT | MIN | MAX. "
        "Table/Column = model names. Filters: DAX-style predicates separated by ';' e.g. Sales[Status] = \"Closed\"; Sales[IsValid] = TRUE. "
        "Ratio: Aggregation/Table/Column/Filters = numerator, Denominator* = denominator. Multiplier 100 for percentages shown as 0-100. "
        "Expected SQL (optional) overrides the generated SQL and must return a column named expected_value. "
        "Leave definition columns empty to keep the KPI unvalidated.")


def template_rows(kpis: list[KPIDefinition], model: ModelMetadata) -> list[list]:
    rows = []
    for k in kpis:
        m = model.measure(k.bi.measure) if k.bi else None
        b = k.basis == "business"
        rows.append([k.id, k.name, k.bi.measure if k.bi else "", k.business_definition if b else "",
                     k.kind if b else "", (k.measure or k.numerator).aggregation if b and (k.measure or k.numerator) else "",
                     "", "", "", "", "", "", "", k.multiplier if b and k.multiplier != 1 else "",
                     k.tolerance.type if b else "", k.tolerance.value if b and k.tolerance.type != "exact" else "",
                     k.tolerance.rounding if b and k.tolerance.rounding is not None else "", k.expected_sql or "", k.owner or "",
                     describe(k) if k.basis == "inferred" else "", m.expression if m else ""])
    return rows


def export_csv(kpis, model) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    w.writerows(template_rows(kpis, model))
    return buf.getvalue().encode("utf-8-sig")


def export_xlsx(kpis, model) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = Workbook()
    ws = wb.active
    ws.title = "KPI Definitions"
    ws.append(COLUMNS)
    for r in template_rows(kpis, model):
        ws.append(r)
    hdr_fill, ref_fill = PatternFill("solid", fgColor="0F2A66"), PatternFill("solid", fgColor="7A869A")
    for i, c in enumerate(ws[1], 1):
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = ref_fill if "reference only" in c.value else hdr_fill
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[c.column_letter].width = 46 if c.value in ("Business Definition", "Filters", "Denominator Filters",
                                                                       "Expected SQL (optional)") or "reference" in c.value else 18
    ws.freeze_panes = "D2"
    n = ws.max_row
    for col, vals in (("E", "simple,ratio"), ("F", ",".join(AGGS)), ("J", ",".join(AGGS)), ("O", "exact,absolute,percent")):
        dv = DataValidation(type="list", formula1=f'"{vals}"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{max(n, 200)}")
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    h = wb.create_sheet("How to fill")
    for line in HELP.split(". "):
        h.append([line.strip()])
    h.column_dimensions["A"].width = 140
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _read_rows(filename: str, data: bytes) -> list[dict]:
    if filename.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb["KPI Definitions"] if "KPI Definitions" in wb.sheetnames else wb.active
        it = ws.iter_rows(values_only=True)
        hdr = [str(x or "").strip() for x in next(it)]
        return [{hdr[i]: ("" if v is None else v) for i, v in enumerate(r) if i < len(hdr)} for r in it]
    text = data.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _g(row: dict, prefix: str) -> str:
    for k, v in row.items():
        if k and k.lower().startswith(prefix.lower()):
            return str(v).strip() if v is not None else ""
    return ""


def import_definitions(filename: str, data: bytes, current: list[KPIDefinition], model: ModelMetadata,
                       actor: str) -> tuple[list[KPIDefinition], list[str]]:
    by_measure = {k.bi.measure.lower(): k for k in current if k.bi}
    by_id = {k.id: k for k in current}
    cols = {(t.name.lower(), c.name.lower()): (t.name, c.name) for t in model.tables for c in t.columns}
    tables = {t.name.lower(): t.name for t in model.tables}
    updated, errors = [], []

    def spec(agg: str, table: str, column: str, filters: str, where: str) -> Optional[MeasureSpec]:
        agg = agg.upper().replace(" ", "_")
        if not agg:
            return None
        if agg not in AGGS:
            raise ValueError(f"{where}: aggregation '{agg}' not in {AGGS}")
        t = tables.get(table.strip("' ").lower())
        if not t:
            raise ValueError(f"{where}: table '{table}' not in model")
        col = None
        if agg != "COUNT_ROWS":
            c = cols.get((t.lower(), column.strip("[] ").lower()))
            if not c:
                raise ValueError(f"{where}: column '{column}' not in table {t}")
            col = fkey(*c)
        flt: list[FilterSpec] = []
        interp = _Interp(_Ctx(model, t))
        norm = (filters.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
                .replace("\u00a0", " "))
        for part in [p for p in norm.split(";") if p.strip()]:
            try:
                flt += interp.preds(parse_dax(part.strip()))
            except Untranslatable as e:
                raise ValueError(f"{where}: cannot read filter '{part.strip()}' ({e})")
        return MeasureSpec(table=t, aggregation=agg, column=col, filters=flt)

    for i, row in enumerate(_read_rows(filename, data), 2):
        measure = unbracket(_g(row, "Power BI Measure"))
        kid = _g(row, "KPI ID")
        k = by_measure.get(measure.lower()) or by_id.get(kid)
        definition, agg, sql = _g(row, "Business Definition"), _g(row, "Aggregation"), _g(row, "Expected SQL")
        if not (definition or agg or sql):
            continue
        where = f"Row {i} ({measure or kid})"
        if not k:
            errors.append(f"{where}: measure not found in the uploaded model")
            continue
        try:
            kind = (_g(row, "Type") or ("ratio" if _g(row, "Denominator Aggregation") else "simple")).lower()
            num = spec(agg, _g(row, "Table"), _g(row, "Column"), _g(row, "Filters"), where)
            den = spec(_g(row, "Denominator Aggregation"), _g(row, "Denominator Table"), _g(row, "Denominator Column"),
                       _g(row, "Denominator Filters"), where + " denominator") if kind == "ratio" else None
            if kind == "ratio" and not (num and den) and not sql:
                raise ValueError(f"{where}: ratio needs numerator and denominator")
            if not num and not sql:
                raise ValueError(f"{where}: give Aggregation/Table/Column or an Expected SQL")
            tt = (_g(row, "Tolerance Type") or ("exact" if (num and num.aggregation.startswith("COUNT")) else "absolute")).lower()
            tv = float(_g(row, "Tolerance Value") or (0 if tt == "exact" else 0.01))
            rd = _g(row, "Rounding")
            nk = k.model_copy(deep=True)
            nk.name = _g(row, "KPI Name") or k.name
            nk.business_definition = definition or k.business_definition
            nk.kind = "ratio" if kind == "ratio" else "simple"
            nk.measure, nk.numerator, nk.denominator = (None, num, den) if kind == "ratio" else (num, None, None)
            nk.multiplier = float(_g(row, "Multiplier") or 1)
            nk.tolerance = Tolerance(type=tt, value=tv, rounding=int(float(rd)) if rd else None)
            nk.expected_sql = sql or None
            nk.owner = _g(row, "Owner") or None
            nk.basis, nk.approval_status, nk.approved_by = "business", "APPROVED", actor
            nk.translation_notes = [f"Business definition imported from {filename}"]
            updated.append(nk)
        except ValueError as e:
            errors.append(str(e))
    return updated, errors
