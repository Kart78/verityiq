"""Excel / CSV data sources: Power Query parsing, local loading, expected values computed from the workbook."""
from __future__ import annotations

import datetime as dt
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.metadata.pbip import parse_file_source, parse_partition_source   # noqa: E402

XL_PATH = r"C:\Users\someone\OneDrive\ABC Motors\Sales Data.xlsx"


def _w(p: Path, t: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")


def build(tmp: Path) -> Path:
    d = tmp / "Motors.SemanticModel" / "definition"
    _w(d / "tables" / "Sales.tmdl", "\n".join([
        "table Sales",
        "\tmeasure 'Vehicle Sales' = COUNTROWS(Sales)",
        "\tmeasure 'Sales Revenue' = SUM(Sales[Revenue])",
        "\tmeasure 'Conversion %' = DIVIDE([Vehicle Sales], [Leads Contacted])",
        "\tcolumn 'Sale Date'", "\t\tdataType: dateTime", "\t\tsourceColumn: Sale Date",
        "\tcolumn Segment", "\t\tdataType: string", "\t\tsourceColumn: Segment",
        "\tcolumn Revenue", "\t\tdataType: double", "\t\tsourceColumn: Revenue",
        "\tcolumn CustomerID", "\t\tdataType: int64", "\t\tsourceColumn: CustomerID",
        "\tpartition Sales = m", "\t\tmode: import", "\t\tsource =", "\t\t\t\tlet",
        f'\t\t\t\t    Source = Excel.Workbook(File.Contents("{XL_PATH}"), null, true),',
        '\t\t\t\t    Sales_Sheet = Source{[Item="Sales",Kind="Sheet"]}[Data],',
        '\t\t\t\t    #"Promoted Headers" = Table.PromoteHeaders(Sales_Sheet, [PromoteAllScalars=true]),',
        '\t\t\t\t    #"Renamed" = Table.RenameColumns(#"Promoted Headers",{{"Sale Amount", "Revenue"}, {"Vehicle Segment", "Segment"}})',
        "\t\t\t\tin", '\t\t\t\t    #"Renamed"', ""]))
    _w(d / "tables" / "Leads.tmdl", "\n".join([
        "table Leads",
        "\tmeasure Customers = DISTINCTCOUNT(Leads[CustomerID])",
        "\tmeasure 'Leads Contacted' = CALCULATE(COUNTROWS(Leads), Leads[Contacted] = \"Yes\")",
        "\tcolumn CustomerID", "\t\tdataType: int64", "\t\tsourceColumn: CustomerID",
        "\tcolumn Contacted", "\t\tdataType: string", "\t\tsourceColumn: Contacted",
        "\tpartition Leads = m", "\t\tmode: import", "\t\tsource =", "\t\t\t\tlet",
        '\t\t\t\t    Source = Csv.Document(File.Contents("C:\\data\\leads.csv"),[Delimiter=",", Columns=2, Encoding=65001, QuoteStyle=QuoteStyle.None]),',
        '\t\t\t\t    #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true])',
        "\t\t\t\tin", '\t\t\t\t    #"Promoted Headers"', ""]))
    r = tmp / "Motors.Report" / "definition"
    _w(r / "pages" / "p1" / "page.json", json.dumps({"name": "p1", "displayName": "Executive Sales Overview"}))
    for i, (ent, meas) in enumerate([("Sales", "Vehicle Sales"), ("Sales", "Sales Revenue"), ("Sales", "Conversion %"), ("Leads", "Customers")]):
        _w(r / "pages" / "p1" / "visuals" / f"c{i}" / "visual.json", json.dumps({"name": f"c{i}", "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
            {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": ent}}, "Property": meas}}}]}}}}}))
    return tmp


def workbook(path: Path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Notes"
    ws["A1"] = "readme"
    ws = wb.create_sheet("Sales")
    ws.append(["Sale Date", "Vehicle Segment", "Sale Amount", "CustomerID"])
    ws.append([dt.datetime(2026, 1, 5), "SUV", 41000.5, 1])
    ws.append([dt.datetime(2026, 2, 9), "Car", 28000, 2])
    ws.append([dt.datetime(2026, 2, 20), "SUV", 39500, 3])
    ws.append([None, None, None, None])
    wb.save(path)


def test_parse_excel_csv_sharepoint_sources():
    m = (f'let Source = Excel.Workbook(File.Contents("{XL_PATH}"), null, true), '
         's = Source{[Item="Sales",Kind="Sheet"]}[Data], p = Table.PromoteHeaders(s) in p')
    fs = parse_file_source(m)
    assert fs["file"] == "Sales Data.xlsx" and fs["item"] == "Sales" and fs["header"] and fs["format"] == "excel"
    assert parse_partition_source("partition x = m", m) == ("excel", "sales_data.sales")
    sp = ('let Source = SharePoint.Files("https://contoso.sharepoint.com/sites/bi", [ApiVersion = 15]), '
          'f = Source{[Name="Targets%202026.xlsx",#"Folder Path"="https://contoso.sharepoint.com/sites/bi/Shared Documents/"]}[Content], '
          'w = Excel.Workbook(f), t = w{[Item="tblTargets",Kind="Table"]}[Data] in t')
    fs = parse_file_source(sp)
    assert fs["file"] == "Targets 2026.xlsx" and fs["item_kind"] == "Table" and fs["header"]
    csv = 'let Source = Csv.Document(File.Contents("C:\\x\\leads.csv"),[Delimiter=";"]), s = Table.Skip(Source, 2) in s'
    fs = parse_file_source(csv)
    assert fs["format"] == "csv" and fs["delimiter"] == ";" and fs["skip"] == 2 and not fs["header"]
    assert parse_file_source('let s = Folder.Files("C:\\drop"), c = Table.Combine(s[Content]) , x = Csv.Document(c) in x')["format"] == "folder"


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("BIQA_PROJECTS", str(tmp_path / "projects"))
    import importlib
    import app.core.environment as envmod
    importlib.reload(envmod)
    import app.core.project, app.service  # noqa: E401
    importlib.reload(app.core.project)
    importlib.reload(app.service)
    return app.service.Service(db_path=tmp_path / "t.db", env_dir=tmp_path / "none")


def test_excel_project_end_to_end(svc, tmp_path):
    model = build(tmp_path / "m")
    files = [(str(p.relative_to(model)), p.read_bytes()) for p in model.rglob("*") if p.is_file()]
    s = svc.create_project_from_files(files, "pytest", name="ABC Motors")
    pid = s["id"]
    env = svc.env(pid)
    assert env.data.platform == "files" and s["source_platform_detected"] in ("excel", "csv")
    assert env.mapping.tables["Sales"].source == "sales_data.sales"
    assert env.mapping.fields["Sales.Revenue"].source == "Sale Amount"          # Power Query rename undone
    sf = svc.source_files(pid)
    assert sf["missing"] == ["Sales Data.xlsx", "leads.csv"] or set(sf["missing"]) == {"Sales Data.xlsx", "leads.csv"}

    xl = tmp_path / "Sales Data.xlsx"
    workbook(xl)
    leads = "CustomerID,Contacted\n1,Yes\n2,Yes\n3,No\n4,Yes\n4,Yes\n"
    out = svc.upload_source_files(pid, [("Sales Data.xlsx", xl.read_bytes()), ("leads.csv", leads.encode())], "pytest")
    assert not out["missing"] and out["connection"]["data"]["ok"], out["connection"]["data"]
    assert "Loaded 2 table(s)" in out["connection"]["data"]["message"]

    for k in svc.kpis(pid):
        svc.approve_kpi(pid, k.id, "APPROVED", "pytest")
    tests = svc.generate(pid, "pytest")
    svc.run(pid, "pytest")
    latest = svc.latest_results(pid)
    by_name = {t["name"]: t for t in tests}
    exp = {}
    for t in tests:
        if t["executor"] == "kpi_value" and t["id"] in latest:
            r = latest[t["id"]]
            ev = (r.get("expected") if isinstance(r.get("expected"), (int, float)) else None)
            exp[t["name"]] = (r["status"], ev, r.get("message"))
    vals = {k.split("Validate ", 1)[-1].split(" as shown")[0]: v for k, v in exp.items()}
    # expected values genuinely computed from the workbook / CSV (no BI connection -> not PASS)
    assert vals["Vehicle Sales"][1] == 3
    assert abs(vals["Sales Revenue"][1] - 108500.5) < 0.01
    assert vals["Customers"][1] == 4
    assert vals["Conversion %"][1] == 0.75
    assert all(v[0] == "BLOCKED" for v in vals.values())              # actual needs Power BI or 'Record result'
    kid = next(k.id for k in svc.kpis(pid) if k.name == "Vehicle Sales")
    assert svc.record_manual(pid, f"{kid}.VAL", {"actual": 3}, "pytest")["status"] == "PASS"
    assert by_name


def test_file_connector_sheet_errors(tmp_path):
    from app.connectors.files import FileConnector
    xl = tmp_path / "Sales Data.xlsx"
    workbook(xl)
    c = FileConnector({"folder": str(tmp_path / "src"), "tables": {
        "sales_data.sales": {"format": "excel", "file": "Sales Data.xlsx", "path": str(xl), "item": "Sales", "item_kind": "Sheet", "header": True},
        "sales_data.gone": {"format": "excel", "file": "Sales Data.xlsx", "path": str(xl), "item": "Gone", "item_kind": "Sheet", "header": True}}})
    r = c.test_connection()
    assert not r.ok and "sheet 'Gone' not found" in r.message and "Sales" in r.message
    meta = c.describe_table("sales_data.sales")
    types = {x.name: x.data_type for x in meta.columns}
    assert types["Sale Date"] == "DATE" and types["Sale Amount"] == "DOUBLE" and types["CustomerID"] == "BIGINT"
    assert meta.row_count == 3                                       # trailing blank row dropped
    q = c.execute_sql("SELECT SUM(t.`Sale Amount`) AS v FROM `sales_data`.`sales` AS t WHERE t.`Vehicle Segment` = 'SUV'")
    assert q.rows[0]["v"] == 80500.5


def test_screenshot_matching_tolerates_ocr_typos():
    from app.core.models import ModelMetadata
    from app.core.scope import match_kpis
    ocr = "500\n100\nCustomers\nLeads Cortscted\nVehicle Ssles\nSaies Revenue\nConversion %\nRepurchase\nInteligence"
    cands = ["Customers", "Leads Contacted", "Vehicle Sales", "Sales Revenue", "Conversion %", "Repurchase Rate", "Dealer Count"]
    got = {m["measure"] for m in match_kpis(ocr, ModelMetadata(platform="powerbi", model_name="x"), cands, fuzzy=0.8)}
    assert got == {"Customers", "Leads Contacted", "Vehicle Sales", "Sales Revenue", "Conversion %"}


def test_builtin_ocr_reads_dashboard_text():
    pytest.importorskip("rapidocr_onnxruntime")
    from PIL import Image, ImageDraw, ImageFont
    from app.core.scope import extract_text
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        pytest.skip("no TTF font for the synthetic screenshot")
    img = Image.new("RGB", (900, 200), "white")
    d = ImageDraw.Draw(img)
    for i, t in enumerate(["Vehicle Sales", "Sales Revenue", "Conversion %"]):
        d.text((30 + i * 290, 80), t, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    text, how = extract_text("dash.png", buf.getvalue())
    assert how == "OCR (built-in)" and "Revenue" in text


def test_source_folder_setting(tmp_path):
    from app.connectors.files import FileConnector
    synced = tmp_path / "OneDrive - Contoso" / "Sales Reports"
    synced.mkdir(parents=True)
    workbook(synced / "Sales Data.xlsx")
    c = FileConnector({"folder": str(tmp_path / "src"), "source_folder": str(tmp_path / "nowhere") + ";" + str(tmp_path / "OneDrive - Contoso"),
                       "tables": {"sales_data.sales": {"format": "excel", "file": "Sales Data.xlsx", "path": "https://contoso.sharepoint.com/x/Sales Data.xlsx",
                                                       "item": "Sales", "item_kind": "Sheet", "header": True}}})
    r = c.test_connection()
    assert r.ok and "3 rows" in r.message
    assert c.sync()["loaded"][0]["where"] == "source folder"


PG = {"host": "/tmp", "port": "54329", "database": "motors", "user": "reader", "password": "r3ad!", "sslmode": "disable"}


def _pg_up() -> bool:
    try:
        import psycopg
        psycopg.connect(host=PG["host"], port=int(PG["port"]), dbname=PG["database"], user=PG["user"], password=PG["password"],
                        sslmode="disable", connect_timeout=3).close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _pg_up(), reason="local PostgreSQL test server not running")
def test_excel_dashboard_validated_against_postgres(svc, tmp_path):
    """Same dashboard, connector switched to a database: tables mapped, columns auto-matched, values computed in the DB."""
    model = build(tmp_path / "m")
    pid = svc.create_project_from_files([(str(p.relative_to(model)), p.read_bytes()) for p in model.rglob("*") if p.is_file()],
                                        "pytest", name="ABC on Postgres")["id"]
    svc.update_settings(pid, {"data": {"platform": "postgres", "config": PG}}, "pytest")
    out = svc.test_connections(pid, "pytest")
    assert out["data"]["ok"], out["data"]
    chk = {c["table"]: c for c in out["source_types"]["checks"]}
    assert chk["Sales"]["status"] == "not_found" and chk["Sales"]["used"]           # Excel names don't exist in the DB
    svc.update_settings(pid, {"table_sources": {"Sales": "sales.vehicle_sales", "Leads": "sales.leads"}}, "pytest")
    rep = svc.store.snapshot(pid, "source_types")["body"]
    chk = {c["table"]: c for c in rep["checks"]}
    assert chk["Sales"]["status"] == "found" and chk["Sales"]["columns_found"] == chk["Sales"]["columns_total"] == 4
    assert chk["Leads"]["columns_found"] == 2 and not rep.get("unresolved")           # CustomerID -> customerid matched by case
    env = svc.env(pid)
    assert env.mapping.fields["Sales.Sale Date"].source_format == "MM/dd/yyyy"         # text dates detected in the DB
    for k in svc.kpis(pid):
        svc.approve_kpi(pid, k.id, "APPROVED", "pytest")
    tests = svc.generate(pid, "pytest")
    svc.run(pid, "pytest")
    latest = svc.latest_results(pid)
    vals = {t["name"].split("Validate ", 1)[-1].split(" as shown")[0]: latest[t["id"]] for t in tests
            if t["executor"] == "kpi_value" and t["id"] in latest}
    assert vals["Vehicle Sales"]["expected"] == 3 and vals["Customers"]["expected"] == 4
    assert abs(float(vals["Sales Revenue"]["expected"]) - 108500.5) < 0.01 and float(vals["Conversion %"]["expected"]) == 0.75
    assert vals["Vehicle Sales"]["mode"] == "LIVE"
    dq = [latest[t["id"]] for t in tests if t["executor"].startswith("dq_") and t["id"] in latest]
    assert dq and all(r["status"] in ("PASS", "FAIL", "NOT VALIDATED") for r in dq), [(r["status"], r.get("message")) for r in dq]


@pytest.mark.skipif(not _pg_up(), reason="local PostgreSQL test server not running")
def test_postgres_connector_is_read_only_and_reports_bad_login():
    from app.connectors.sqldb import PostgresConnector
    from app.engine.sql_safety import UnsafeSQL
    c = PostgresConnector(PG)
    assert c.test_connection().ok
    with pytest.raises(UnsafeSQL):
        c.execute_sql("DELETE FROM sales.leads")
    bad = PostgresConnector({**PG, "user": "nobody"}).test_connection()
    assert not bad.ok and "nobody" in bad.message
    q = c.execute_sql("SELECT COUNT(*) AS n FROM `sales`.`leads` WHERE `contacted` = :p0", {"p0": "Yes"})
    assert q.rows[0]["n"] == 4 and "'Yes'" in q.extra["executed_as"]
    assert PostgresConnector({"host": "x"}).test_connection().message.startswith("Missing in Settings")


@pytest.mark.skipif(not _pg_up(), reason="local PostgreSQL test server not running")
def test_translated_sql_gives_same_results_on_postgres(svc):
    """Every generated query of the sample project, translated to PostgreSQL and run there, returns exactly the
    values the reference engine computes."""
    import re
    import duckdb
    import psycopg
    from app.engine.dialects import to_dialect
    root = Path(__file__).resolve().parents[2]
    src = duckdb.connect(str(root / "demo" / "data" / "cc_sample.duckdb"), read_only=True)
    admin = psycopg.connect(host="/tmp", port=54329, dbname="postgres", user="verity", autocommit=True)
    admin.execute("DROP DATABASE IF EXISTS verity_regress")
    admin.execute("CREATE DATABASE verity_regress")
    pg = psycopg.connect(host="/tmp", port=54329, dbname="verity_regress", user="verity", autocommit=True)
    for sch, tbl in src.execute("SELECT table_schema, table_name FROM information_schema.tables").fetchall():
        cols = src.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=? AND table_name=? "
                           "ORDER BY ordinal_position", [sch, tbl]).fetchall()
        ty = {"DOUBLE": "double precision", "VARCHAR": "text"}
        pg.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')
        pg.execute(f'CREATE TABLE "{sch}"."{tbl}" (' + ", ".join(f'"{c}" {ty.get(t, t.lower())}' for c, t in cols) + ")")
        with pg.cursor() as cur:
            cur.executemany(f'INSERT INTO "{sch}"."{tbl}" VALUES (' + ",".join(["%s"] * len(cols)) + ")",
                            src.execute(f'SELECT * FROM "{sch}"."{tbl}"').fetchall())
    pid = svc.create_sample_project("pytest")["id"]
    tests = svc.generate(pid, "pytest")
    sqls = [t["params"]["sql"] for t in tests if t["params"].get("sql") and "SQL not generated" not in t["params"]["sql"]]
    assert len(sqls) >= 15
    strip = lambda q: re.sub(r"\bmain\.(customer_center)\.", r"\1.", q)  # noqa: E731
    norm = lambda rows: sorted([tuple(round(float(v), 6) if isinstance(v, (int, float)) or type(v).__name__ == "Decimal" else str(v)
                                      for v in r) for r in rows], key=str)  # noqa: E731
    for q in sqls:
        a = pg.execute(strip(to_dialect(q, "postgres"))).fetchall()
        b = src.execute(strip(to_dialect(q, "duckdb"))).fetchall()
        assert norm(a) == norm(b), q
    for d in ("snowflake", "tsql", "mysql", "oracle", "bigquery", "teradata", "redshift", "db2"):
        for q in sqls:
            to_dialect(q, d)


def test_manual_outcome_and_approval_cycle(svc):
    pid = svc.create_sample_project("pytest")["id"]
    tests = svc.generate(pid, "pytest")
    ui = next(t for t in tests if t["executor"] == "manual")
    with pytest.raises(ValueError):
        svc.record_manual(pid, ui["id"], {"status": "FAIL"}, "pytest")              # a failure needs an observation
    r = svc.record_manual(pid, ui["id"], {"status": "PASS", "note": "all visuals updated"}, "pytest")
    assert r["status"] == "PASS" and r["mode"] == "MANUAL" and svc.latest_results(pid)[ui["id"]]["status"] == "PASS"
    assert svc.update_test(pid, ui["id"], {"approval_status": "APPROVED"}, "lead")["approved_by"] == "lead"
    t = svc.update_test(pid, ui["id"], {"approval_status": "PENDING"}, "lead")
    assert t["approval_status"] == "PENDING" and t["approved_by"] is None
