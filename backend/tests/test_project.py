"""Generic PBIP-upload flow: discovery, DAX inference, definitions, SQL, manual results."""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[2]


def _zip(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in folder.rglob("*"):
            if p.is_file():
                z.write(p, "MyRepo/" + str(p.relative_to(folder)))
    return buf.getvalue()


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


def test_partition_source_parsing():
    from app.metadata.pbip import parse_partition_source
    assert parse_partition_source("partition X = m", 'Databricks.Catalogs(h, p){[Name="main",Kind="Database"]}[Data]{[Name="sales"]}[Data]{[Name="orders"]}[Data]') == ("databricks", "main.sales.orders")
    assert parse_partition_source("partition X = m", 'Sql.Database("srv", "DW"){[Schema="dbo",Item="FactSales"]}[Data]') == ("sqlserver", "DW.dbo.FactSales")
    assert parse_partition_source("partition D = calculated", "CALENDAR(DATE(2024,1,1), DATE(2025,1,1))")[0] == "calculated"
    k, ref = parse_partition_source("partition X = m", 'Value.NativeQuery(Src, "SELECT * FROM a.b.c", null)')
    assert k == "native_query" and ref == "(SELECT * FROM a.b.c)"


def test_dax_inference_patterns():
    from app.analyzers.dax_infer import translate, Untranslatable

    def _interp(x):
        return x
    r = _interp(translate('CALCULATE(DISTINCTCOUNT(Customers[CustomerID]), Customers[IsActive] = TRUE(), Customers[State] IN {"TX","CA"})')[0])
    assert r.agg == "COUNT_DISTINCT" and {f.op for f in r.filters} == {"=", "in"}
    r = translate("DIVIDE(CALCULATE(COUNTROWS(Cases), Cases[Status] = \"Closed\"), COUNTROWS(Cases)) * 100")[0]
    assert r.op == "*" and r.r.v == 100 and r.l.l.filters[0].value == "Closed" and not r.l.r.filters
    r = translate("VAR x = SUM(S[Amt]) RETURN x")[0]
    assert r.agg == "SUM"
    r = translate("CALCULATE(SUM(S[Amt]), S[Date] >= DATE(2025, 1, 31), NOT ISBLANK(S[Cust]))")[0]
    assert r.filters[0].value == "2025-01-31" and r.filters[1].op == "is not null"
    with pytest.raises(Untranslatable):
        translate("CALCULATE(SUM(S[Amt]), ALL(S))")
    with pytest.raises(Untranslatable):
        translate("IF(SUM(S[A]) > 0, 1, 0)")


def test_safe_extract_blocks_traversal(tmp_path):
    from app.core.project import safe_extract
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../evil.tmdl", "x")
    with pytest.raises(ValueError):
        safe_extract([("x.zip", buf.getvalue())], tmp_path / "d")
    assert safe_extract([("a.exe", b"MZ"), ("m/t.tmdl", b"table T")], tmp_path / "e") == 1   # non-model files skipped


def test_upload_flow_generic(svc):
    s = svc.create_project_from_files([("model.zip", _zip(ROOT / "demo" / "pbip"))], "pytest", name="Any Model")
    pid = s["id"]
    assert s["measures"] == 9 and s["source_platform_detected"] == "databricks" and s["kpis_inferred"] == 9
    env = svc.env(pid)
    assert env.mapping.tables["Cases"].source == "main.customer_center.cases"
    assert env.mapping.tables["Date"].source is None and env.mapping.fields["Date.Month"].key_expr.startswith("date_format")
    tests = svc.generate(pid, "pytest")
    by = {t["id"]: t for t in tests}
    # grain on a calculated date table resolves through the relationship key
    g = next(t for t in tests if t["id"].startswith("VIS.lineCasesByMonth.KPI"))
    assert "date_format(t_cases.created_date, 'yyyy-MM')" in g["params"]["sql"] and "SUMMARIZECOLUMNS('Date'[Month]" in g["params"]["dax"]
    assert all(t.get("display_id", "").startswith("TC") for t in tests)
    # dashboard scope: only measures shown on report visuals get KPI tests
    kpi_measures = {svc.kpis(pid)[0].__class__ and k.bi.measure for k in svc.kpis(pid) if any(t.get("kpi_id") == k.id and t["category"] == "KPI" for t in tests)}
    assert "Closed Cases" not in kpi_measures and "Cases YoY %" not in kpi_measures and "Total Cases" in kpi_measures
    assert svc.scope(pid)["mode"] == "dashboard"
    # nothing configured -> nothing can PASS a value test
    run = svc.run(pid, "pytest")
    res = {r["test_id"]: r for r in svc.store.results(run["run_id"])}
    assert all(r["status"] != "PASS" for tid, r in res.items() if by[tid]["executor"] in ("kpi_value", "kpi_grain"))
    dax = {m["measure"]: m for m in svc.dax_analysis(pid)["measures"]}
    assert dax["FCR %"]["definition_status"] == "NOT VALIDATED"          # no business definition yet
    assert dax["Escalation Rate"]["overall_status"] == "FAIL"            # broken reference caught statically
    # business definitions -> logic verified / mismatch
    out = svc.import_definitions(pid, "defs.csv", (ROOT / "demo" / "kpi_definitions_sample.csv").read_bytes(), "pytest")
    assert out["updated"] == 7 and not out["errors"]
    dax = {m["measure"]: m for m in svc.dax_analysis(pid)["measures"]}
    assert dax["FCR %"]["definition_status"] == "FAIL" and dax["Total Customers"]["definition_status"] == "PASS"
    # manual result recording
    kid = next(k.id for k in svc.kpis(pid) if k.name == "Total Cases")
    r = svc.record_manual(pid, f"{kid}.VAL", {"expected": 100, "actual": 99}, "pytest")
    assert r["status"] == "FAIL" and r["mode"] == "MANUAL"
    r = svc.record_manual(pid, f"{kid}.VAL", {"expected": 100, "actual": 100}, "pytest")
    assert r["status"] == "PASS"
    assert svc.latest_results(pid)[f"{kid}.VAL"]["status"] == "PASS"


def test_definition_template_and_errors(svc):
    s = svc.create_project_from_files([("model.zip", _zip(ROOT / "demo" / "pbip"))], "pytest")
    x = svc.definition_template(s["id"], "xlsx")
    assert x[:2] == b"PK"
    bad = "Power BI Measure,Business Definition,Aggregation,Table,Column\nTotal Cases,x,SUM,Cases,Nope\n".encode()
    out = svc.import_definitions(s["id"], "b.csv", bad, "pytest")
    assert out["updated"] == 0 and "Nope" in out["errors"][0]


def test_secrets_typed_in_settings_never_stored_in_config(svc, tmp_path, monkeypatch):
    import app.core.secrets as sec
    monkeypatch.setenv("BIQA_SECRETS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(sec, "_keyring", lambda: None)
    s = svc.create_project_from_files([("model.zip", _zip(ROOT / "demo" / "pbip"))], "pytest")
    pub = svc.update_settings(s["id"], {"data": {"platform": "databricks", "config": {
        "server_hostname": "adb-1.azuredatabricks.net", "http_path": "/sql/1.0/warehouses/a",
        "client_id": "cid", "client_secret": "super-secret-value"}}}, "pytest")
    assert pub["data"]["config"]["client_secret"] == "(saved)"
    cfg = svc.env(s["id"]).data.config
    assert cfg["client_secret"].startswith("${secret:") and "super-secret-value" not in str(cfg)
    # saving again with the masked/blank value keeps the stored secret
    svc.update_settings(s["id"], {"data": {"config": {"client_secret": ""}}}, "pytest")
    assert svc.env(s["id"]).data.config["client_secret"].startswith("${secret:")
