"""Dashboard-driven scope, Power Query lineage, report filters, requirement-document matching."""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.project import create_project, infer_kpis                       # noqa: E402
from app.core.scope import displayed, extract_text, in_scope, match_kpis       # noqa: E402
from app.engine.sql_generator import kpi_value_sql                             # noqa: E402
from app.engine.test_generator import generate_tests                           # noqa: E402
from app.analyzers.dax import DaxAnalyzer                                      # noqa: E402
from app.metadata.pbip import pq_lineage, read_pbip                            # noqa: E402


def _w(p: Path, t: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")


def _sel(src, prop, kind="Column"):
    return {kind: {"Expression": {"SourceRef": {"Source": src}}, "Property": prop}}


def build(tmp: Path) -> Path:
    d = tmp / "Acx.SemanticModel" / "definition"
    _w(d / "tables" / "rpt_acxiom_enrichment.tmdl", "\n".join([
        "table rpt_acxiom_enrichment",
        "\tmeasure 'Unique Customers' = SUM(rpt_acxiom_enrichment[unique_customer_id_count])",
        "\tmeasure 'Enriched Customers' = SUM(rpt_acxiom_enrichment[unique_customer_id_enriched])",
        "\tmeasure 'Helper Ratio' = DIVIDE([Enriched Customers], [Unique Customers])",
        "\tcolumn AuditDate", "\t\tdataType: dateTime", "\t\tsourceColumn: AuditDate",
        "\tcolumn Region", "\t\tdataType: string", "\t\tsourceColumn: Region",
        "\tcolumn unique_customer_id_count", "\t\tdataType: int64", "\t\tsourceColumn: unique_customer_id_count",
        "\tcolumn unique_customer_id_enriched", "\t\tdataType: int64", "\t\tsourceColumn: unique_customer_id_enriched",
        "\tcolumn LoadFlag", "\t\tdataType: string", "\t\tsourceColumn: LoadFlag",
        "\tpartition rpt_acxiom_enrichment = m", "\t\tmode: import", "\t\tsource =", "\t\t\t\tlet",
        '\t\t\t\t    Source = Databricks.Catalogs("h", "p", null),',
        '\t\t\t\t    db = Source{[Name="customer_cdw_ing",Kind="Schema"]}[Data],',
        '\t\t\t\t    t = db{[Name="rpt_acxiom_enrichment",Kind="Table"]}[Data],',
        '\t\t\t\t    #"Renamed Columns" = Table.RenameColumns(t,{{"audit_create_date", "AuditDate"}, {"region_cd", "Region"}}),',
        '\t\t\t\t    #"Added Custom" = Table.AddColumn(#"Renamed Columns", "LoadFlag", each "Y")',
        "\t\t\t\tin", '\t\t\t\t    #"Added Custom"', ""]))
    r = tmp / "Acx.Report" / "definition"
    _w(r / "report.json", json.dumps({"filterConfig": {"filters": [{"name": "f0", "filter": {"Version": 2, "From": [{"Name": "a", "Entity": "rpt_acxiom_enrichment"}],
        "Where": [{"Condition": {"Comparison": {"ComparisonKind": 2, "Left": _sel("a", "AuditDate"), "Right": {"Literal": {"Value": "datetime'2025-01-01T00:00:00'"}}}}}]}}]}}))
    _w(r / "pages" / "p1" / "page.json", json.dumps({"name": "p1", "displayName": "Enrichment",
        "filterConfig": {"filters": [{"name": "pf", "filter": {"Version": 2, "From": [{"Name": "a", "Entity": "rpt_acxiom_enrichment"}],
            "Where": [{"Condition": {"In": {"Expressions": [_sel("a", "Region")], "Values": [[{"Literal": {"Value": "'TX'"}}], [{"Literal": {"Value": "'CA'"}}]]}}}]}}]}}))
    _w(r / "pages" / "p1" / "visuals" / "c1" / "visual.json", json.dumps({"name": "c1", "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "rpt_acxiom_enrichment"}}, "Property": "Unique Customers"}}}]}}},
        "visualContainerObjects": {"title": [{"properties": {"text": {"expr": {"Literal": {"Value": "'Unique Customer IDs'"}}}}}]}}}))
    _w(r / "pages" / "p1" / "visuals" / "c2" / "visual.json", json.dumps({"name": "c2", "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": "rpt_acxiom_enrichment"}}, "Property": "unique_customer_id_enriched"}}, "Function": 0}}}]}}}},
        "filterConfig": {"filters": [{"name": "vf", "filter": {"Version": 2, "From": [{"Name": "a", "Entity": "rpt_acxiom_enrichment"}],
            "Where": [{"Condition": {"Comparison": {"ComparisonKind": 1, "Left": _sel("a", "Enriched Customers", "Measure"), "Right": {"Literal": {"Value": "0L"}}}}}]}}]}}))
    _w(tmp / "Acx.Report" / "definition" / "pages" / "hid" / "page.json", json.dumps({"name": "hid", "displayName": "Drill", "visibility": "HiddenInViewMode"}))
    _w(r / "pages" / "hid" / "visuals" / "h1" / "visual.json", json.dumps({"name": "h1", "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "rpt_acxiom_enrichment"}}, "Property": "Helper Ratio"}}}]}}}}}))
    return tmp


def test_power_query_renames_resolve_physical_columns(tmp_path):
    ren, added = pq_lineage('Table.RenameColumns(t,{{"a", "B"}}), x = Table.RenameColumns(y,{{"B", "C"}}), z = Table.AddColumn(x, "D", each 1)')
    assert ren == {"C": "a"} and added == {"D"}
    m = read_pbip(build(tmp_path))
    env, notes = create_project(m, tmp_path)
    assert env.mapping.fields["rpt_acxiom_enrichment.AuditDate"].source == "audit_create_date"
    assert env.mapping.fields["rpt_acxiom_enrichment.Region"].source == "region_cd"
    assert env.mapping.fields["rpt_acxiom_enrichment.LoadFlag"].source_expr == "'Y'"    # Power Query formula translated
    assert not any("LoadFlag" in n for n in notes)


def test_report_filters_and_hidden_pages(tmp_path):
    m = read_pbip(build(tmp_path))
    v = {x.id: x for x in m.visuals}
    c1 = v["c1"].filter_specs
    assert {"ref": "rpt_acxiom_enrichment[AuditDate]", "op": ">=", "value": "2025-01-01", "level": "report"} in c1
    assert {"ref": "rpt_acxiom_enrichment[Region]", "op": "in", "value": ["TX", "CA"], "level": "page"} in c1
    assert any("measure [Enriched Customers]" in n for n in v["c2"].filter_notes)
    assert v["c2"].implicit_aggs == {"implicit:rpt_acxiom_enrichment[unique_customer_id_enriched]": "SUM"}
    assert v["h1"].page_hidden
    shown = displayed(m)
    assert "Unique Customers" in shown and "Helper Ratio" not in shown


def test_dashboard_scope_generates_only_displayed_kpis_with_their_filters(tmp_path):
    m = read_pbip(build(tmp_path))
    env, _ = create_project(m, tmp_path)
    kpis = infer_kpis(m)
    for k in kpis:
        k.approval_status = "APPROVED"
    scope = {"mode": "dashboard"}
    names = {k.name for k in in_scope(kpis, m, scope)}
    assert names == {"Unique Customers", "Sum of unique_customer_id_enriched"}            # helper + hidden page excluded
    tests = generate_tests(env, kpis, m, DaxAnalyzer(m).analyze(env, kpis), [], scope=scope)
    val = next(t for t in tests if t.name.startswith("Validate Unique Customers"))
    assert "as shown on 'Enrichment' › 'Unique Customer IDs'" in val.name
    sql = val.params["sql"]
    assert "t_rpt_acxiom_enrichment.audit_create_date >= '2025-01-01'" in sql and "region_cd IN ('TX', 'CA')" in sql
    assert "KEEPFILTERS('rpt_acxiom_enrichment'[Region] IN {\"TX\", \"CA\"})" in val.params["dax"]
    imp = next(t for t in tests if "Sum of unique_customer_id_enriched" in t.name and t.id.endswith(".VAL"))
    assert "SUM('rpt_acxiom_enrichment'[unique_customer_id_enriched])" in imp.params["dax"]
    assert not any(t.id.endswith(".DAX") and "Sum of" in t.name for t in tests)
    assert not any("Helper Ratio" in t.name for t in tests)


def test_requirement_document_picks_kpis(tmp_path):
    m = read_pbip(build(tmp_path))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>KPI 1: Unique Customer IDs - must reconcile to CDW</w:t></w:r></w:p>"
                                        "<w:p><w:r><w:t>KPI 2: Enriched customers (%) by region</w:t></w:r></w:p></w:body></w:document>")
    text, how = extract_text("requirements.docx", buf.getvalue())
    assert how == "Word document" and "Unique Customer IDs" in text
    found = {x["measure"]: x for x in match_kpis(text, m, ["Unique Customers", "Enriched Customers", "Helper Ratio"])}
    assert found["Unique Customers"]["via"] == "visual title"
    assert found["Enriched Customers"]["via"] == "name"
    assert "Helper Ratio" not in found
