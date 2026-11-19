"""Core invariants + adapter tests. Run: cd backend && pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyzers.dax import DaxAnalyzer, tokenize                       # noqa: E402
from app.connectors.databricks import DatabricksConnector                  # noqa: E402
from app.connectors.powerbi import PowerBIConnector, build_dax             # noqa: E402
from app.connectors.registry import create_connector                       # noqa: E402
from app.core.environment import ROOT, FieldMapping, load_environment, load_kpis  # noqa: E402
from app.core.models import FilterSpec, KPIDefinition, Status, Tolerance  # noqa: E402
from app.engine.compare import compare_grain, compare_values               # noqa: E402
from app.engine.sql_generator import MappingError, kpi_value_sql           # noqa: E402
from app.engine.sql_safety import UnsafeSQL, assert_read_only              # noqa: E402
from app.metadata.pbip import read_pbip                                    # noqa: E402
from app.service import Service                                            # noqa: E402

ENV = load_environment(ROOT / "config" / "samples" / "demo_powerbi_databricks.yaml")
KPIS = {k.id: k for k in load_kpis(ENV)}


# ---------------------------------------------------------------- SQL safety
@pytest.mark.parametrize("sql", ["DELETE FROM t", "DROP TABLE t", "SELECT 1; DROP TABLE t", "INSERT INTO t VALUES (1)",
                                 "UPDATE t SET a=1", "CREATE TABLE x AS SELECT 1", "MERGE INTO t USING s ON 1=1 WHEN MATCHED THEN DELETE"])
def test_read_only_guard_rejects_writes(sql):
    with pytest.raises(UnsafeSQL):
        assert_read_only(sql)


def test_read_only_guard_accepts_select():
    assert_read_only("WITH a AS (SELECT 1 AS x) SELECT x FROM a")


# ---------------------------------------------------------------- comparison
def test_tolerances():
    assert compare_values(100, 100, Tolerance()).status == Status.PASS
    assert compare_values(100, 100.4, Tolerance(type="absolute", value=0.5)).status == Status.PASS
    assert compare_values(100, 101, Tolerance(type="percent", value=0.5)).status == Status.FAIL
    assert compare_values(72.401, 72.4049, Tolerance(type="exact", rounding=2)).status == Status.PASS
    assert compare_values(None, 5, Tolerance()).status == Status.FAIL
    assert compare_values(None, None, Tolerance(null_handling="null_equals_null")).status == Status.PASS
    assert compare_values(1, 1, Tolerance(), expected_unit="USD", actual_unit="EUR").status == Status.BLOCKED


def test_grain_missing_members_fail():
    st, detail, _ = compare_grain([{"grain": "TX", "expected_value": 1}, {"grain": "CA", "expected_value": 2}],
                                  [{"grain": "TX", "value": 1}], Tolerance())
    assert st == Status.FAIL and any(d["note"] == "Missing in BI" for d in detail)


# ---------------------------------------------------------------- SQL generation
def test_sql_is_independent_of_bi_and_parameterised():
    g = kpi_value_sql(KPIS["KPI-003"], ENV, [FilterSpec(field="state", op="=", value="TX")])
    assert ":p" in g.sql and "TX" in g.params.values()
    assert "LEFT JOIN" in g.sql and "customers" in g.sql
    assert "'TX'" in g.render_inline()
    body = "\n".join(l for l in g.sql.splitlines() if not l.startswith("--"))
    assert "FCR" not in body   # the executable SQL never references the BI model


def test_unapproved_kpi_cannot_produce_expected_value():
    k = KPIS["KPI-001"].model_copy(update={"approval_status": "DRAFT"})
    with pytest.raises(MappingError):
        kpi_value_sql(k, ENV, [])


def test_unmapped_field_blocks():
    with pytest.raises(MappingError):
        kpi_value_sql(KPIS["KPI-002"], ENV, [FilterSpec(field="region", op="=", value="South")])


# ---------------------------------------------------------------- PBIP + DAX
def test_tmdl_parse():
    m = read_pbip(ROOT / "demo" / "pbip")
    assert {t.name for t in m.tables} == {"Cases", "Customers", "Date"}
    fcr = m.measure("FCR %")
    assert fcr and "DIVIDE" in fcr.expression and "formatString" not in fcr.expression
    assert "SAMEPERIODLASTYEAR" in m.measure("Cases YoY %").expression       # fenced ``` block
    assert any(t.is_date_table for t in m.tables)
    assert len(m.relationships) == 2 and m.relationships[1].to_table == "Date"
    assert len(m.visuals) == 9


def test_dax_findings():
    m = read_pbip(ROOT / "demo" / "pbip")
    res = {a.measure: a for a in DaxAnalyzer(m).analyze(ENV, list(KPIS.values()))}
    assert res["FCR %"].definition_status == Status.FAIL
    assert any(f.rule == "A002" and "Denominator" in f.message for f in res["FCR %"].definition_findings)
    assert res["Total Customers"].overall_status == Status.PASS
    assert any(f.rule == "D004" for f in res["Avg Handle Time (min)"].static_findings)
    assert res["Avg Handle Time (min)"].overall_status == Status.PASS
    assert any(f.rule == "D005" for f in res["Open Cases"].static_findings)
    assert any(f.rule == "D002" for f in res["Escalation Rate"].static_findings)
    assert res["Cases YoY %"].overall_status == Status.NOT_VALIDATED


def test_dax_syntax_errors():
    m = read_pbip(ROOT / "demo" / "pbip")
    a = DaxAnalyzer(m)
    assert any(f.rule == "D001" for f in a.static("x", "SUM(Cases[HandleTimeMin]"))
    assert any(f.rule == "D003" for f in a.static("x", "SUM(Cases[Nope])"))
    assert not tokenize('"a // not a comment"')[0].kind == "comment"


def test_dax_query_builder():
    fm = FieldMapping(table="customers", source="state", model="Customers[State]")
    dm = FieldMapping(table="cases", source="created_date", model="'Date'[Date]", data_type="date")
    q = build_dax("FCR %", [(fm, FilterSpec(field="state", value="TX")), (dm, FilterSpec(field="d", op=">=", value="2025-01-31"))], None)
    assert 'KEEPFILTERS(Customers[State] = "TX")' in q and "DATE(2025, 1, 31)" in q and q.startswith("EVALUATE")
    g = build_dax("Total Cases", [], FieldMapping(table="cases", model="'Date'[Month]"))
    assert "SUMMARIZECOLUMNS('Date'[Month]" in g


# ---------------------------------------------------------------- live adapters (mocked transport)
class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)

    def json(self):
        return self._b


class _Session:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if "oauth2" in url:
            return _Resp(200, {"access_token": "tok", "expires_in": 3600})
        return _Resp(200, {"results": [{"tables": [{"rows": [{"[Value]": 50.6}]}]}]})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return _Resp(200, {"value": [{"id": "1", "name": "ds"}]})


def test_powerbi_execute_queries_contract():
    s = _Session()
    c = PowerBIConnector({"tenant_id": "t", "client_id": "c", "client_secret": "s", "workspace_id": "w", "dataset_id": "d"}, session=s)
    r = c.query_measure("FCR %", [])
    assert r.rows == [{"value": 50.6}]
    post = [x for x in s.calls if "executeQueries" in x[1]][0]
    assert post[1].endswith("/groups/w/datasets/d/executeQueries")
    assert post[2]["headers"]["Authorization"] == "Bearer tok"
    assert post[2]["json"]["queries"][0]["query"].startswith("EVALUATE")
    assert c.test_connection().ok


class _Cur:
    description = [("expected_value",)]

    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return [(72.4,)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cur(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_databricks_adapter_uses_params_and_blocks_writes():
    log = []
    c = DatabricksConnector({"server_hostname": "h", "http_path": "p"}, connect_fn=lambda: _Conn(log))
    g = kpi_value_sql(KPIS["KPI-003"], ENV, [FilterSpec(field="state", op="=", value="TX")])
    r = c.execute_sql(g.sql, g.params)
    assert r.rows == [{"expected_value": 72.4}] and log[0][1] == g.params
    with pytest.raises(UnsafeSQL):
        c.execute_sql("DROP TABLE x")


def test_planned_connectors_cannot_be_created():
    with pytest.raises(NotImplementedError):
        create_connector("tableau", {})


# ---------------------------------------------------------------- end to end (demo)
@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if not (ROOT / "demo" / "data" / "cc_sample.duckdb").exists():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo.py")], check=True)
    svc = Service(db_path=tmp_path_factory.mktemp("db") / "t.db", env_dir=ROOT / "config" / "samples")
    svc.discover("demo-pbi-dbx", "pytest")
    svc.generate("demo-pbi-dbx", "pytest")
    out = svc.run("demo-pbi-dbx", "pytest")
    return svc, {r["test_id"]: r for r in svc.store.results(out["run_id"])}, out


def test_e2e_statuses(run):
    _, res, out = run
    assert out["summary"]["mode"] == "DEMO"
    assert res["KPI-002.VAL"]["status"] == "PASS"
    assert res["KPI-003.VAL"]["status"] == "FAIL"                      # DAX defect detected numerically
    assert res["KPI-007.VAL"]["status"] == "NOT VALIDATED"             # never PASS without independent expected value
    assert res["KPI-009.VAL"]["status"] == "ERROR"
    assert res["KPI-008.FRESH"]["status"] == "FAIL"
    assert res["DQ.RI.cases.customers"]["status"] == "FAIL"
    assert res["VIS.tableAgents.KPI-002"]["status"] == "BLOCKED"
    assert res["VIS.slicerState.UI"]["status"] == "NOT EXECUTED"


def test_e2e_every_result_has_evidence_and_mode(run):
    _, res, _ = run
    for r in res.values():
        assert r["mode"] == "DEMO"
        assert r["evidence"], r["test_id"]
    ev = res["KPI-003.VAL"]["evidence"]
    assert any(e["kind"] == "sql" for e in ev) and any(e["kind"] == "dax" for e in ev)


def test_kpi_edit_resets_approval(run):
    svc, _, _ = run
    k = next(k for k in svc.kpis("demo-pbi-dbx") if k.id == "KPI-002")
    k2 = k.model_copy(update={"business_definition": "changed"})
    assert svc.save_kpi("demo-pbi-dbx", k2, "pytest").approval_status == "DRAFT"


def test_databricks_service_principal_token_exchange(monkeypatch):
    import requests
    calls = []

    class R:
        status_code = 200

        def json(self):
            return {"access_token": "tok-123", "expires_in": 3600}
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)) or R())
    c = DatabricksConnector({"server_hostname": "https://adb-1.2.azuredatabricks.net/", "http_path": "/sql/1.0/warehouses/x",
                             "client_id": "cid", "client_secret": "sec"})
    assert c.auth_mode() == "databricks-sp" and c._token() == "tok-123" and c._token() == "tok-123"
    assert calls[0][0] == "https://adb-1.2.azuredatabricks.net/oidc/v1/token" and len(calls) == 1      # cached
    e = DatabricksConnector({"server_hostname": "h", "http_path": "p", "client_id": "cid", "client_secret": "sec", "tenant_id": "t1"})
    assert e.auth_mode() == "entra-sp" and e._token() == "tok-123"
    assert "login.microsoftonline.com/t1" in calls[-1][0] and "2ff814a6" in calls[-1][1]["data"]["scope"]


def test_secrets_saved_outside_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BIQA_SECRETS_FILE", str(tmp_path / "s.json"))
    import app.core.secrets as sec
    monkeypatch.setattr(sec, "_keyring", lambda: None)
    from app.core.environment import resolve_secrets
    ref = sec.put_secret("p1.data.client_secret", "s3cr3t")
    assert ref == "${secret:p1.data.client_secret}"
    assert resolve_secrets({"client_secret": ref, "x": 1})["client_secret"] == "s3cr3t"


def test_text_dates_converted_like_power_query():
    from app.core.formats import detect_date_format
    assert detect_date_format(["01/05/2026", "12/31/2019"]) == "MM/dd/yyyy"
    assert detect_date_format(["2026-01-05"]) == "yyyy-MM-dd"
    assert detect_date_format(["31/12/2019", "01/05/2026"]) == "dd/MM/yyyy"
    assert detect_date_format(["20260105"]) == "yyyyMMdd"
    fm = FieldMapping(table="t", source="audit_create_date", data_type="date", source_type="string", source_format="MM/dd/yyyy")
    assert fm.source_sql("t_x") == "TRY_TO_DATE(t_x.audit_create_date, 'MM/dd/yyyy')"
    assert fm.source_sql("t_x", raw=True) == "t_x.audit_create_date"
    assert FieldMapping(table="t", source="d", data_type="date", source_type="date").source_sql("a") == "a.d"


def test_freshness_sql_uses_converted_date_and_full_catalog():
    from app.core.environment import EnvironmentConfig, Mapping, TableMapping, PlatformBinding
    from app.engine.sql_generator import dq_freshness_sql
    env = EnvironmentConfig(id="x", name="x", bi=PlatformBinding(platform="powerbi"),
                            data=PlatformBinding(platform="databricks", config={"catalog": "tbdp_catalog_dev"}),
                            mapping=Mapping(tables={"rpt_acxiom_enrichment": TableMapping(source="customer_cdw_ing.rpt_acxiom_enrichment")},
                                            fields={"rpt_acxiom_enrichment.Audit Create Date": FieldMapping(
                                                table="rpt_acxiom_enrichment", source="audit_create_date", data_type="date",
                                                source_type="string", source_format="MM/dd/yyyy")}))
    sql = dq_freshness_sql(env, "rpt_acxiom_enrichment.Audit Create Date").sql
    assert "MAX(TRY_TO_DATE(t_rpt_acxiom_enrichment.audit_create_date, 'MM/dd/yyyy')) AS latest_record" in sql
    assert "DATEDIFF(CURRENT_DATE(), MAX(TRY_TO_DATE(" in sql
    assert "FROM tbdp_catalog_dev.customer_cdw_ing.rpt_acxiom_enrichment AS t_rpt_acxiom_enrichment" in sql
    assert_read_only(sql)


def test_databricks_execution_strips_comments():
    log = []
    c = DatabricksConnector({"server_hostname": "h", "http_path": "p"}, connect_fn=lambda: _Conn(log))
    c.execute_sql("-- KPI-001 header\n-- Definition: x\nSELECT 1 AS expected_value")
    assert log[0][0] == "SELECT 1 AS expected_value"
