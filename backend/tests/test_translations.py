"""Power Query added/transformed columns and simple calculated columns are translated to SQL, notes are
split by relevance, and Re-analyze applies a newer parser to an existing project."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.metadata.m_to_sql import m_expr_to_sql  # noqa: E402
from app.metadata.pbip import pq_columns_sql  # noqa: E402

from test_files import svc  # noqa: E402,F401
from test_functional import _w  # noqa: E402

XL = r"C:\data\ABC_Motors.xlsx"


def _src(sheet, extra=""):
    return ["\tpartition {0} = m".format(sheet), "\t\tmode: import", "\t\tsource =", "\t\t\t\tlet",
            f'\t\t\t\t    Source = Excel.Workbook(File.Contents("{XL}"), null, true),',
            f'\t\t\t\t    S = Source{{[Item="{sheet}",Kind="Sheet"]}}[Data],',
            '\t\t\t\t    P = Table.PromoteHeaders(S, [PromoteAllScalars=true])' + ("," if extra else "")] + \
           ([f"\t\t\t\t    {x}" for x in extra.split("\n")] if extra else []) + \
           ["\t\t\t\tin", "\t\t\t\t    " + (extra.split("\n")[-1].split("=")[0].strip() if extra else "P"), ""]


def build(tmp: Path) -> Path:
    d = tmp / "ABC.SemanticModel" / "definition"
    _w(d / "tables" / "Dim_Dealer.tmdl", "\n".join(["table Dim_Dealer",
        "\tcolumn DealerID", "\t\tdataType: int64", "\t\tsourceColumn: DealerID",
        "\tcolumn Dealer", "\t\tdataType: string", "\t\tsourceColumn: Dealer"]
        + _src("Dealers", 'D = Table.AddColumn(P, "Dealer", each Text.Proper([dealer_name]), type text)')))
    _w(d / "tables" / "Dim_Customer.tmdl", "\n".join(["table Dim_Customer",
        "\tcolumn CustomerID", "\t\tdataType: int64", "\t\tsourceColumn: CustomerID",
        "\tcolumn 'Ownership Age Bucket'", "\t\tdataType: string", "\t\tsourceColumn: Ownership Age Bucket"]
        + _src("Customers", 'A = Table.AddColumn(P, "Ownership Age (Yrs)", each Duration.Days(Date.From(DateTime.LocalNow()) - [PurchaseDate]) / 365, type number),\n'
                            'B = Table.AddColumn(A, "Ownership Age Bucket", each if [#"Ownership Age (Yrs)"] < 3 then "0-3 yrs" else "3+ yrs", type text)')))
    _w(d / "tables" / "Fact_Leads.tmdl", "\n".join(["table Fact_Leads",
        "\tmeasure Leads = COUNTROWS(Fact_Leads)",
        "\tmeasure 'Leads Contacted' = CALCULATE([Leads], Fact_Leads[Status] = \"Contacted\")",
        "\tcolumn LeadID", "\t\tdataType: int64", "\t\tsourceColumn: LeadID",
        "\tcolumn CustomerID", "\t\tdataType: int64", "\t\tsourceColumn: CustomerID",
        "\tcolumn DealerID", "\t\tdataType: int64", "\t\tsourceColumn: DealerID",
        "\tcolumn Status", "\t\tdataType: string", "\t\tsourceColumn: Status",
        "\tcolumn 'Dealer Name' = RELATED(Dim_Dealer[Dealer])", "\t\tdataType: string",
        "\tcolumn 'Lead Score' = LOOKUPVALUE(Dim_Dealer[Dealer], Dim_Dealer[DealerID], Fact_Leads[DealerID])", "\t\tdataType: string",
        "\tcolumn 'Lead Tier' = SWITCH(TRUE(), Fact_Leads[LeadID] > 2, \"Hot\", \"Cold\")", "\t\tdataType: string"]
        + _src("Leads")))
    _w(d / "relationships.tmdl", "relationship r1\n\tfromColumn: Fact_Leads.CustomerID\n\ttoColumn: Dim_Customer.CustomerID\n\n"
                                 "relationship r2\n\tfromColumn: Fact_Leads.DealerID\n\ttoColumn: Dim_Dealer.DealerID\n")
    r = tmp / "ABC.Report" / "definition" / "pages" / "p1"
    _w(r / "page.json", {"name": "p1", "displayName": "Executive Sales Overview"})
    card = lambda n, m: {"name": n, "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [  # noqa: E731
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Fact_Leads"}}, "Property": m}}}]}}}}}
    _w(r / "visuals" / "c1" / "visual.json", card("c1", "Leads"))
    _w(r / "visuals" / "c2" / "visual.json", card("c2", "Leads Contacted"))
    _w(r / "visuals" / "ch" / "visual.json", {"name": "ch", "visual": {"visualType": "clusteredBarChart", "query": {"queryState": {
        "Category": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Fact_Leads"}}, "Property": "Dealer Name"}}}]},
        "Y": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Fact_Leads"}}, "Property": "Leads"}}}]}}}}})
    _w(r / "visuals" / "sl" / "visual.json", {"name": "sl", "visual": {"visualType": "slicer", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Dim_Customer"}}, "Property": "Ownership Age Bucket"}}}]}}},
        "objects": {"general": [{"properties": {"filter": {"filter": {"Version": 2, "From": [{"Name": "c", "Entity": "Dim_Customer", "Type": 0}],
            "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}}, "Property": "Ownership Age Bucket"}}],
                                            "Values": [[{"Literal": {"Value": "'0-3 yrs'"}}]]}}}]}}}}]}}})
    return tmp


def workbook(path: Path):
    import openpyxl
    today = dt.date.today()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Dealers"
    for row in [["DealerID", "dealer_name"], [1, "abc east"], [2, "abc west"]]:
        ws.append(row)
    ws = wb.create_sheet("Customers")
    ws.append(["CustomerID", "PurchaseDate"])
    ws.append([10, dt.datetime.combine(today - dt.timedelta(days=365), dt.time())])        # 1 year  -> 0-3 yrs
    ws.append([11, dt.datetime.combine(today - dt.timedelta(days=5 * 365), dt.time())])    # 5 years -> 3+ yrs
    ws = wb.create_sheet("Leads")
    ws.append(["LeadID", "CustomerID", "DealerID", "Status"])
    for row in [[1, 10, 1, "Contacted"], [2, 10, 2, "New"], [3, 11, 1, "Contacted"], [4, 10, 1, "Contacted"]]:
        ws.append(row)
    wb.save(path)


def test_m_translation_shapes():
    col = lambda n: "{t}.`" + n + "`"  # noqa: E731
    assert m_expr_to_sql('each if [a] = null then "none" else [a]', col) == "CASE WHEN ({t}.`a` IS NULL) THEN 'none' ELSE {t}.`a` END"
    assert m_expr_to_sql("(r) => r[Qty] * r[Price]", col) == "({t}.`Qty` * {t}.`Price`)"
    assert m_expr_to_sql("each List.Sum({[a],[b]})", col) is None
    sq = pq_columns_sql('Table.RenameColumns(P,{{"x", "X"}}), A = Table.AddColumn(B, "Y", each [X] + 1), '
                        'T = Table.TransformColumns(A, {{"X", each _ * 100, type number}})')
    assert sq == {"Y": "({t}.`x` + 1)", "X": "({t}.`x` * 100)"}


def test_excel_dashboard_with_pq_buckets_and_related(svc, tmp_path):  # noqa: F811
    root = build(tmp_path / "m")
    pid = svc.create_project_from_files([(str(p.relative_to(root)), p.read_bytes()) for p in root.rglob("*") if p.is_file()], "t", name="ABC")["id"]
    env = svc.env(pid)
    assert env.mapping.fields["Dim_Customer.Ownership Age Bucket"].source_expr.startswith("CASE WHEN")
    assert env.mapping.fields["Fact_Leads.Dealer Name"].table == "Dim_Dealer"                # RELATED -> join
    assert env.mapping.fields["Fact_Leads.Dealer Name"].source_expr == "INITCAP({t}.`dealer_name`)"
    assert env.mapping.fields["Fact_Leads.Lead Tier"].source_expr.startswith("CASE WHEN")              # row-level DAX translated
    s = svc.project_summary(pid)
    assert not any("Ownership Age Bucket" in n or "Dealer Name" in n for n in s["notes"] + s["notes_info"])
    assert any("Lead Score" in n for n in s["notes_info"]) and not any("Lead Score" in n for n in s["notes"])   # unused -> informational
    xl = tmp_path / "ABC_Motors.xlsx"
    workbook(xl)
    out = svc.upload_source_files(pid, [("ABC_Motors.xlsx", xl.read_bytes())], "t")
    assert out["connection"]["data"]["ok"], out["connection"]["data"]
    for k in svc.kpis(pid):
        svc.approve_kpi(pid, k.id, "APPROVED", "t")
    tests = svc.generate(pid, "t")
    svc.run(pid, "t")
    latest = svc.latest_results(pid)
    val = {t["name"].split("Validate ", 1)[-1].split(" as shown")[0]: latest[t["id"]] for t in tests if t["executor"] == "kpi_value" and ".VAL" in t["id"]}
    assert val["Leads"]["expected"] == 3                 # slicer '0-3 yrs' (computed from PurchaseDate in Power Query) -> customer 10
    assert val["Leads Contacted"]["expected"] == 2
    grain = next(latest[t["id"]] for t in tests if t["executor"] == "kpi_grain" and t["id"].startswith("VIS.ch"))
    rows = json.dumps([e.get("content") for e in grain.get("evidence", [])], default=str)
    assert "Abc East" in rows and "Abc West" in rows     # chart axis via RELATED + Text.Proper


def test_reanalyze_keeps_settings_and_refreshes_notes(svc, tmp_path):  # noqa: F811
    root = build(tmp_path / "m")
    pid = svc.create_project_from_files([(str(p.relative_to(root)), p.read_bytes()) for p in root.rglob("*") if p.is_file()], "t", name="ABC")["id"]
    svc.update_settings(pid, {"table_sources": {"Fact_Leads": "custom.leads_table"}}, "t")
    svc.store.save_snapshot(pid, "project_notes", ["'Dim_Customer'[Ownership Age Bucket]: added in Power Query - no source column (old)."])
    k = next(k for k in svc.kpis(pid) if k.name == "Leads")
    svc.approve_kpi(pid, k.id, "APPROVED", "t")
    s = svc.reanalyze(pid, "t")
    assert not any("(old)" in n for n in s["notes"] + s["notes_info"])
    assert svc.env(pid).mapping.tables["Fact_Leads"].source == "custom.leads_table"
    k2 = next(x for x in svc.kpis(pid) if x.name == "Leads")
    assert k2.id == k.id and k2.approval_status == "APPROVED"


def test_dax_row_translation_and_execution():
    import duckdb
    from app.engine.dax_row_sql import dax_row_to_sql
    from app.engine.dialects import to_dialect
    col = lambda t, n: None if t not in (None, "S") else "t.`" + n + "`"  # noqa: E731
    cases = {
        'IF(S[Qty] > 2, "Big", "Small")': "Big",
        'SWITCH(TRUE(), S[Qty] > 5, "A", S[Qty] > 2, "B", "C")': "B",
        'SWITCH([Region], "TX", "South", "Other")': "South",
        "DATEDIFF(S[Start], S[End], YEAR)": 1,                 # 2020-12-31 -> 2021-01-01 crosses one year boundary
        "DATEDIFF(S[Start], S[End], DAY)": 1,
        'DIVIDE(S[Qty], 0, -1)': -1,
        'LEFT([Region], 1) & "-" & S[Qty]': "T-3",
        "DATE(YEAR(S[End]), MONTH(S[End]), 1)": "2021-01-01",
    }
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 3 AS Qty, 'TX' AS Region, DATE '2020-12-31' AS Start, DATE '2021-01-01' AS \"End\"")
    for dax, want in cases.items():
        sql = dax_row_to_sql(dax, col)
        assert sql, dax
        got = con.execute(to_dialect(f"SELECT {sql} AS v FROM t", "duckdb")).fetchone()[0]
        assert (float(got) == float(want)) if isinstance(want, (int, float)) else str(got) == str(want), (dax, sql, got)
    assert dax_row_to_sql("RELATED(D[x]) & S[Qty]", col) is None
    assert dax_row_to_sql("DATEDIFF(S[Start], S[End], WEEK)", col) is None
