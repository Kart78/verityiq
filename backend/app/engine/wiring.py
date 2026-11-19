"""Static analysis of how a slicer reaches the other visuals on its page.

A slicer only changes a visual when BOTH hold:
  1. the visual interaction from the slicer to that visual is not "None" (Format › Edit interactions), and
  2. a chain of active model relationships carries the filter, in its filter direction, from the slicer's
     table to the table(s) the visual's numbers come from.
If (1) fails the visual was deliberately excluded; if (2) fails the slicer silently does nothing for that
visual - a real defect that shows unfiltered totals next to filtered ones.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Optional

from ..core.models import KPIDefinition, ModelMetadata, VisualMeta, is_slicer
from ..core.names import unbracket

VISUAL_KIND = {
    "card": "Card", "cardVisual": "Card", "multiRowCard": "Multi-row card", "kpi": "KPI visual", "gauge": "Gauge",
    "tableEx": "Table", "pivotTable": "Matrix", "clusteredColumnChart": "Column chart", "columnChart": "Column chart",
    "stackedColumnChart": "Stacked column chart", "hundredPercentStackedColumnChart": "100% column chart",
    "clusteredBarChart": "Bar chart", "barChart": "Bar chart", "stackedBarChart": "Stacked bar chart",
    "lineChart": "Line chart", "areaChart": "Area chart", "stackedAreaChart": "Stacked area chart",
    "lineClusteredColumnComboChart": "Line & column chart", "lineStackedColumnComboChart": "Line & column chart",
    "pieChart": "Pie chart", "donutChart": "Donut chart", "funnel": "Funnel", "treemap": "Treemap", "map": "Map",
    "filledMap": "Filled map", "azureMap": "Map", "scatterChart": "Scatter chart", "waterfallChart": "Waterfall chart",
    "ribbonChart": "Ribbon chart", "decompositionTreeVisual": "Decomposition tree", "keyDriversVisual": "Key influencers",
    "slicer": "Slicer", "advancedSlicerVisual": "Button slicer", "listSlicer": "List slicer", "textSlicer": "Text slicer",
}


def visual_kind(t: str) -> str:
    if t in VISUAL_KIND:
        return VISUAL_KIND[t]
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", t or "visual").strip()
    return words[:1].upper() + words[1:]


def ref_parts(ref: str) -> tuple[Optional[str], Optional[str]]:
    """'Table'[Column] / Table[Column] / Table[Date].Year / implicit:T[C] -> (table, column)."""
    ref = ref[9:] if ref.startswith("implicit:") else ref
    ref = re.sub(r"\]\.[^\]]+$", "]", ref)
    m = re.match(r"^\s*'?(.+?)'?\s*\[(.+)\]\s*$", ref)
    return (m.group(1).replace("''", "'"), m.group(2).replace("]]", "]")) if m else (None, None)


def field_display(ref: str) -> str:
    t, c = ref_parts(ref)
    return c or unbracket(ref)


def visual_label(v: VisualMeta) -> str:
    if v.title:
        return v.title
    first = (v.dimensions or v.measures or [""])[0] if is_slicer(v.visual_type) else (v.measures or v.dimensions or [""])[0]
    return f"{visual_kind(v.visual_type)} · {field_display(first)}" if first else visual_kind(v.visual_type)


def _graph(model: ModelMetadata) -> dict[str, set[str]]:
    """Filter-propagation edges: the one side filters the many side; bidirectional relationships both ways."""
    g: dict[str, set[str]] = {}
    for r in model.relationships:
        if not r.active:
            continue
        a, b = r.to_table.lower(), r.from_table.lower()           # to = one side, from = many side
        g.setdefault(a, set()).add(b)
        if "both" in (r.cross_filter or "").lower():
            g.setdefault(b, set()).add(a)
    return g


def _path(g: dict, start: str, goals: set[str]) -> Optional[list[str]]:
    start = start.lower()
    if start in goals:
        return [start]
    prev, q = {start: None}, deque([start])
    while q:
        n = q.popleft()
        for m in g.get(n, ()):
            if m not in prev:
                prev[m] = n
                if m in goals:
                    out = [m]
                    while prev[out[-1]] is not None:
                        out.append(prev[out[-1]])
                    return out[::-1]
                q.append(m)
    return None


def measure_tables(model: ModelMetadata, ref: str, kpis_by_measure: dict[str, KPIDefinition]) -> set[str]:
    """Model tables a visual value is computed from."""
    if ref.startswith("implicit:"):
        t, _ = ref_parts(ref)
        return {t.lower()} if t else set()
    name = unbracket(ref)
    k = kpis_by_measure.get(name.lower())
    out: set[str] = set()
    if k:
        out |= {sp.table.lower() for sp in k.all_specs() if sp.table}
    m = model.measure(name)
    if m:
        tables = {t.name.lower(): t for t in model.tables}
        for tname in re.findall(r"'((?:[^']|'')+)'\s*\[|\b([A-Za-z_][\w]*)\s*\[", m.expression or ""):
            n = (tname[0] or tname[1]).replace("''", "'").lower()
            if n in tables and tables[n].columns:
                out.add(n)
        home = tables.get((m.table or "").lower())
        if not out and home is not None and any(not c.name.startswith("__") for c in home.columns):
            out.add(home.name.lower())
    return out


def _dax_bridge(model: ModelMetadata, measures: list[str], table: str) -> Optional[str]:
    """A measure that moves filters in DAX (TREATAS / CROSSFILTER / USERELATIONSHIP) referencing the slicer's table."""
    t = re.escape(table)
    for ref in measures:
        if ref.startswith("implicit:"):
            continue
        m = model.measure(unbracket(ref))
        e = m.expression if m else ""
        hit = re.search(r"\b(TREATAS|CROSSFILTER|USERELATIONSHIP)\s*\(", e or "", re.I)
        if hit and re.search(rf"'?{t}'?\s*\[", e, re.I):
            return hit.group(1).upper()
    return None


def slicer_wiring(model: ModelMetadata, slicer: VisualMeta, kpis: list[KPIDefinition]) -> dict:
    """-> {field, table, missing_field, targets: [{id, label, kind, interaction, verdict, path, tables}]}
    verdict: reacts | ignored (interaction set to None) | no_path (a fact table the filter cannot reach) |
             independent (values come only from dimension / disconnected tables - normally unaffected) | unknown"""
    ref = (slicer.dimensions or [""])[0]
    table, col = ref_parts(ref)
    out = {"field": ref, "table": table, "column": col, "missing_field": False, "targets": []}
    tmeta = next((t for t in model.tables if table and t.name.lower() == table.lower()), None)
    if not tmeta or not any(c.name.lower() == (col or "").lower() for c in tmeta.columns):
        out["missing_field"] = True
    kbm = {(k.bi.measure if k.bi.measure.startswith("implicit:") else unbracket(k.bi.measure)).lower(): k for k in kpis if k.bi}
    g = _graph(model)
    names = {t.name.lower(): t.name for t in model.tables}
    many_side = {r.from_table.lower() for r in model.relationships if r.active}     # fact-like tables
    many_side |= {r.to_table.lower() for r in model.relationships if r.active and "both" in (r.cross_filter or "").lower()}
    for v in model.visuals:
        same_page = (v.page_id and v.page_id == slicer.page_id) or (not v.page_id and v.page == slicer.page)
        if v.id == slicer.id or not same_page or is_slicer(v.visual_type) or not (v.measures or v.dimensions):
            continue
        inter = slicer.interactions.get(v.id, "Default")
        tabs: set[str] = set()
        for m in v.measures:
            tabs |= measure_tables(model, m, kbm)
        if not v.measures:
            tabs |= {t.lower() for t, _ in map(ref_parts, v.dimensions) if t}
        row = {"id": v.id, "label": visual_label(v), "kind": visual_kind(v.visual_type), "interaction": inter,
               "tables": sorted(tabs), "path": None}
        if inter == "NoFilter":
            row["verdict"] = "ignored"
        elif not table or not tabs:
            row["verdict"] = "unknown"
        else:
            p = _path(g, table, tabs)
            row["path"] = p
            via_dax = _dax_bridge(model, v.measures, table)
            if not p and via_dax:
                row["path"], p = [table.lower(), f"(DAX {via_dax})"] + sorted(tabs), True
            if p:
                row["verdict"] = "reacts"
            elif not (tabs & many_side):
                row["verdict"] = "independent"      # e.g. 'data as of' date cards, parameter / info tables
            else:
                row["verdict"] = "no_path"
        nice = lambda n: names.get(n, n)  # noqa: E731
        row["tables"] = [nice(t) for t in row["tables"]]
        if row["path"]:
            row["path"] = [nice(t) for t in row["path"]]
        out["targets"].append(row)
    return out
