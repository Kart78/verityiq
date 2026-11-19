"""Measure Killer-style features: used-by-unused, keep, other reports, VertiPaq sizes, cleanup, best-practice rules."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyzers.bpa import run_rules  # noqa: E402
from app.analyzers.cleanup import clean_pbip, inject_remove_step  # noqa: E402
from app.analyzers.usage import analyze_usage  # noqa: E402
from app.analyzers.vpax import parse_vpax, size_map, to_json  # noqa: E402
from app.metadata.pbip import read_pbip  # noqa: E402

from test_functional import _card, _w  # noqa: E402
from test_usage import build_usage  # noqa: E402


def _vpax() -> bytes:
    view = {"Tables": [{"TableName": "Sales", "TableSize": 9_000_000, "RowsCount": 1000}],
            "Columns": [{"TableName": "Sales", "ColumnName": "Notes", "TotalSize": 6_000_000, "ColumnCardinality": 990, "DataType": "String"},
                        {"TableName": "Sales", "ColumnName": "Amount", "TotalSize": 2_000_000, "ColumnCardinality": 500},
                        {"TableName": "Sales", "ColumnName": "Discount", "TotalSize": 1_000_000, "ColumnCardinality": 20}]}
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("DaxVpaView.json", json.dumps(view))
        z.writestr("DaxModel.json", "{}")
    return b.getvalue()


def test_vpax_sizes_rank_unused_columns(tmp_path):
    model = read_pbip(build_usage(tmp_path))
    stats = to_json(parse_vpax(_vpax(), "m.vpax"))
    u = analyze_usage(model, sizes=size_map(stats))
    notes = next(o for o in u["objects"] if o["ref"] == "'Sales'[Notes]")
    assert notes["size_bytes"] == 6_000_000 and notes["cardinality"] == 990
    assert u["summary"]["size_unused_bytes"] == 7_000_000                     # Notes + Discount (used by unused)
    assert u["objects"][0]["ref"] == "'Sales'[Notes]"                          # biggest unused first
    with pytest.raises(ValueError):
        parse_vpax(b"PK\x05\x06" + b"\x00" * 18, "empty.vpax")


def test_inject_remove_step_multiline():
    m = ["let", "    Source = Sql.Database(\"s\", \"d\"),", "    T = Source{[Schema=\"dbo\",Item=\"x\"]}[Data]", "in", "    T"]
    out = inject_remove_step(m, ["a", "b \"q\""])
    assert out[2].endswith(",") and out[3].strip().startswith('#"Removed unused columns (VerityIQ)" = Table.RemoveColumns(T, {"a", "b ""q"""}')
    assert out[-1].strip() == '#"Removed unused columns (VerityIQ)"'
    assert inject_remove_step(out, ["c"]) is None                               # never stacks a second step


def test_cleanup_produces_verified_pbip(tmp_path):
    root = build_usage(tmp_path / "src")
    _w(root / "M.SemanticModel" / "definition" / "cultures" / "de-DE.tmdl",
       "cultureInfo de-DE\n\ttranslations\n\t\tmodel Model\n\t\t\ttable Sales\n\t\t\t\tmeasure 'Old KPI'\n\t\t\t\t\ttranslatedCaption: Alt\n"
       "\t\t\t\tmeasure 'Vehicle Sales'\n\t\t\t\t\ttranslatedCaption: Fahrzeuge\n")
    model = read_pbip(root)
    r = clean_pbip(root, analyze_usage(model), model)
    assert r["ok"] and not r["problems"], r["problems"]
    assert r["counts"] == {"measures": 2, "columns": 4, "queries": 2}
    out = tmp_path / "out"
    zipfile.ZipFile(io.BytesIO(r["zip"])).extractall(out)
    tops = list(out.iterdir())
    assert len(tops) == 1 and tops[0].is_dir()                                 # one folder: "<model> (cleaned)"
    out = tops[0]
    # openable in Power BI Desktop: a .pbip launcher -> report -> definition.pbir byPath -> semantic model
    pbip = list(out.glob("*.pbip"))
    assert len(pbip) == 1 and r["open_file"] == pbip[0].name             # relative to the top folder
    rep = out / json.loads(pbip[0].read_text())["artifacts"][0]["report"]["path"]
    ref = json.loads((rep / "definition.pbir").read_text())["datasetReference"]["byPath"]["path"]
    assert (rep / ref).resolve() == (out / "M.SemanticModel").resolve()
    assert (out / "M.SemanticModel" / "definition.pbism").exists()
    assert "Extract" in (out / "VERITYIQ_CLEANUP.md").read_text()
    m2 = read_pbip(out)
    sales = next(t for t in m2.tables if t.name == "Sales")
    assert {x.name for x in sales.measures} == {"Vehicle Sales", "Sales Revenue", "Base Revenue"}
    assert {c.name for c in sales.columns} == {"DealerKey", "Amount"}
    assert 'Table.RemoveColumns(T, {"discount", "notes"}' in sales.source
    culture = (out / "M.SemanticModel" / "definition" / "cultures" / "de-DE.tmdl").read_text()
    assert "Old KPI" not in culture and "Fahrzeuge" in culture
    assert (out / "VERITYIQ_CLEANUP.md").exists()
    assert analyze_usage(m2)["summary"]["measures_unused"] == 0                # nothing dead left after one pass


def test_cleanup_respects_options_and_keep(tmp_path):
    root = build_usage(tmp_path)
    model = read_pbip(root)
    u = analyze_usage(model, keep={"m:old kpi"})
    r = clean_pbip(root, u, model, data_columns=False)
    gone = {c["object"] for c in r["changes"]}
    assert "Old KPI" not in gone and "Older Helper" not in gone and "Net Amount" in gone and r["counts"]["queries"] == 0


def test_cleanup_safety_check_rejects_breaking_changes(tmp_path):
    root = build_usage(tmp_path)
    model = read_pbip(root)
    u = analyze_usage(model)
    for o in u["objects"]:                     # pretend a used measure were unused - verification must refuse
        if o["ref"] == "[Base Revenue]":
            o["status"] = "unused"
    r = clean_pbip(root, u, model)
    assert not r["ok"] and r["zip"] is None and any("Base Revenue" in p for p in r["problems"])


def test_best_practice_rules(tmp_path):
    root = build_usage(tmp_path)
    d = root / "M.SemanticModel" / "definition"
    _w(d / "tables" / "LocalDateTable_1.tmdl", "table LocalDateTable_1\n\tisHidden\n\tcolumn Date\n\t\tdataType: dateTime\n")
    _w(d / "relationships.tmdl", "relationship r1\n\tfromColumn: Sales.DealerKey\n\ttoColumn: Dealer.DealerKey\n\tcrossFilteringBehavior: bothDirections\n\n"
                                 "relationship r2\n\tfromColumn: Targets.RegionKey\n\ttoColumn: Region.RegionKey\n\tisActive: false\n")
    model = read_pbip(root)
    res = run_rules(model, analyze_usage(model), root)
    rules = {f["rule"]: f for f in res["findings"]}
    assert {"M01", "M02", "M04", "M05", "M06", "M07"} <= set(rules), sorted(rules)
    assert rules["M01"]["severity"] == "High" and "Sales[Amount]" in rules["M07"]["objects"]
    assert res["findings"][0]["severity"] == "High"


def test_service_keep_reports_vpax_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("BIQA_PROJECTS", str(tmp_path / "projects"))
    import importlib
    import app.core.environment as envmod
    importlib.reload(envmod)
    import app.core.project, app.service  # noqa: E401
    importlib.reload(app.core.project)
    importlib.reload(app.service)
    svc = app.service.Service(db_path=tmp_path / "t.db", env_dir=tmp_path / "none")
    root = build_usage(tmp_path / "src")
    pid = svc.create_project_from_files([(str(p.relative_to(root)), p.read_bytes()) for p in root.rglob("*") if p.is_file()], "t", name="mk")["id"]
    assert svc.usage(pid)["summary"]["measures_unused"] == 2
    assert "old kpi" in svc.dead_measures(pid)
    u = svc.set_keep(pid, ["m:old kpi"], True, "t")
    assert u["summary"]["kept"] == 1 and "old kpi" not in svc.dead_measures(pid)
    svc.set_keep(pid, ["m:old kpi"], False, "t")
    # another (thin) report that uses Sales[Notes]
    rep = tmp_path / "Finance.Report" / "definition" / "pages" / "f1"
    _w(rep / "page.json", {"name": "f1", "displayName": "Finance"})
    _w(rep / "visuals" / "n" / "visual.json", {"name": "n", "visual": {"visualType": "tableEx", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Notes"}}}]}}}}})
    files = [(str(p.relative_to(tmp_path)), p.read_bytes()) for p in (tmp_path / "Finance.Report").rglob("*") if p.is_file()]
    u = svc.add_usage_reports(pid, files, "t")
    assert u["added"] == ["Finance"] and next(o for o in u["objects"] if o["ref"] == "'Sales'[Notes]")["status"] == "report"
    u = svc.set_vpax(pid, _vpax(), "m.vpax", "t")
    assert u["summary"]["size_known"] and u["vpax"]["total_bytes"] == 9_000_000
    prev = svc.cleanup(pid, {}, "t")
    assert prev["ok"] and "zip" not in prev and "Notes" not in {c["object"] for c in prev["changes"]}
    full = svc.cleanup(pid, {}, "t", want_zip=True)
    assert full["zip"][:2] == b"PK"
    bp = svc.best_practices(pid)
    assert bp["available"] and any(f["rule"] == "M05" for f in bp["findings"])
    u = svc.remove_usage_report(pid, "Finance", "t")
    assert next(o for o in u["objects"] if o["ref"] == "'Sales'[Notes]")["status"] == "unused"
