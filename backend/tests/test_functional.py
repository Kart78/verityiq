"""Functional (slicer) tests: interactions, relationship reachability, saved selections, manual steps."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.environment import EnvironmentConfig  # noqa: E402
from app.core.models import TestCase  # noqa: E402
from app.core.project import create_project, infer_kpis  # noqa: E402,F401
from app.engine.runner import ExecContext, execute_test as execute  # noqa: E402
from app.engine.test_generator import generate_tests  # noqa: E402
from app.engine.wiring import slicer_wiring  # noqa: E402
from app.metadata.pbip import parse_legacy_report, read_pbip  # noqa: E402


def _w(p: Path, t):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t if isinstance(t, str) else json.dumps(t), encoding="utf-8")


def _card(ent, meas, name):
    return {"name": name, "visual": {"visualType": "card", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": ent}}, "Property": meas}}}]}}}}}


def build(tmp: Path, interactions=True) -> Path:
    d = tmp / "M.SemanticModel" / "definition"
    src = lambda t: ["\tpartition {0} = m".format(t), "\t\tmode: import", "\t\tsource =",  # noqa: E731
                     f'\t\t\t\tlet S = Databricks.Catalogs("h", "/p"), C = S{{[Name="cat",Kind="Database"]}}[Data], '
                     f'D = C{{[Name="sales",Kind="Schema"]}}[Data], T = D{{[Name="{t.lower()}",Kind="Table"]}}[Data] in T', ""]
    _w(d / "tables" / "Dealer.tmdl", "\n".join(["table Dealer", "\tcolumn DealerKey", "\t\tdataType: int64", "\t\tsourceColumn: dealer_key",
                                                "\tcolumn DealerName", "\t\tdataType: string", "\t\tsourceColumn: dealer_name"] + src("Dealer")))
    _w(d / "tables" / "Sales.tmdl", "\n".join(["table Sales", "\tmeasure 'Vehicle Sales' = COUNTROWS(Sales)",
                                               "\tmeasure 'Sales Revenue' = SUM(Sales[Amount])",
                                               "\tcolumn DealerKey", "\t\tdataType: int64", "\t\tsourceColumn: dealer_key",
                                               "\tcolumn Amount", "\t\tdataType: double", "\t\tsourceColumn: amount"] + src("Sales")))
    _w(d / "tables" / "Targets.tmdl", "\n".join(["table Targets", "\tmeasure 'Sales Target' = SUM(Targets[Target])",
                                                 "\tcolumn Target", "\t\tdataType: double", "\t\tsourceColumn: target",
                                                 "\tcolumn RegionKey", "\t\tdataType: int64", "\t\tsourceColumn: region_key"] + src("Targets")))
    _w(d / "tables" / "Region.tmdl", "\n".join(["table Region", "\tcolumn RegionKey", "\t\tdataType: int64", "\t\tsourceColumn: region_key"] + src("Region")))
    _w(d / "tables" / "Refresh.tmdl", "\n".join(["table Refresh", "\tmeasure 'Data As Of' = MAX(Refresh[LoadedAt])",
                                                 "\tcolumn LoadedAt", "\t\tdataType: dateTime", "\t\tsourceColumn: loaded_at"] + src("Refresh")))
    _w(d / "relationships.tmdl", "relationship r1\n\tfromColumn: Sales.DealerKey\n\ttoColumn: Dealer.DealerKey\n\n"
                                 "relationship r2\n\tfromColumn: Targets.RegionKey\n\ttoColumn: Region.RegionKey\n")
    r = tmp / "M.Report" / "definition" / "pages" / "p1"
    page = {"name": "p1", "displayName": "Dealer Performance"}
    if interactions:
        page["visualInteractions"] = [{"source": "slc", "target": "rev", "type": "NoFilter"}]
    _w(r / "page.json", page)
    _w(r / "visuals" / "slc" / "visual.json", {"name": "slc", "visual": {"visualType": "slicer", "query": {"queryState": {"Values": {"projections": [
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "Dealer"}}, "Property": "DealerName"}}}]}}},
        "objects": {"general": [{"properties": {"filter": {"filter": {"Version": 2, "From": [{"Name": "d", "Entity": "Dealer", "Type": 0}],
            "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": "DealerName"}}],
                                            "Values": [[{"Literal": {"Value": "'ABC East'"}}]]}}}]}}}}]}}})
    _w(r / "visuals" / "vs" / "visual.json", _card("Sales", "Vehicle Sales", "vs"))
    _w(r / "visuals" / "rev" / "visual.json", _card("Sales", "Sales Revenue", "rev"))
    _w(r / "visuals" / "tgt" / "visual.json", _card("Targets", "Sales Target", "tgt"))
    _w(r / "visuals" / "asof" / "visual.json", _card("Refresh", "Data As Of", "asof"))
    return tmp


def _setup(tmp):
    root = build(tmp)
    model = read_pbip(root)
    env, _ = create_project(model, root, project_id="fx", name="fx")
    return model, env


def test_parser_reads_interactions_and_respects_them(tmp_path):
    model, _ = _setup(tmp_path)
    slc = next(v for v in model.visuals if v.id == "slc")
    assert slc.interactions == {"rev": "NoFilter"} and slc.selection == ["ABC East"]
    rev = next(v for v in model.visuals if v.id == "rev")
    vs = next(v for v in model.visuals if v.id == "vs")
    assert not any(f.get("level") == "slicer" for f in rev.filter_specs)       # set to ignore the slicer
    assert any(f.get("level") == "slicer" for f in vs.filter_specs)


def test_wiring_finds_ignored_and_unreachable_visuals(tmp_path):
    model, env = _setup(tmp_path)
    slc = next(v for v in model.visuals if v.id == "slc")
    w = slicer_wiring(model, slc, [])
    verdict = {x["id"]: x["verdict"] for x in w["targets"]}
    assert verdict == {"vs": "reacts", "rev": "ignored", "tgt": "no_path", "asof": "independent"}
    assert next(x for x in w["targets"] if x["id"] == "vs")["path"] == ["Dealer", "Sales"]


def test_generated_slicer_tests_and_execution(tmp_path):
    model, env = _setup(tmp_path)
    from app.core.project import infer_kpis as _ik
    kpis = _ik(model)
    tests = {t.id: t for t in generate_tests(env, kpis, model, [], [], scope={"mode": "dashboard"})}
    wire, ui = tests["VIS.slc.WIRE"], tests["VIS.slc.UI"]
    assert "'DealerName' on 'Dealer Performance'" in wire.name
    assert any("select 'ABC East'" in s for s in ui.steps)                       # saved selection used
    assert any("stay unchanged" in s and "Sales Revenue" in s for s in ui.steps)
    ctx = [t for t in tests.values() if ".CTX." in t.id]
    assert ctx and all("= ABC East" in t.name for t in ctx)
    assert not any("Sales Revenue" in t.name for t in ctx)                      # slicer ignores that card
    ec = ExecContext(env=env, bi=None, data=None, kpis={k.id: k for k in kpis}, model=model, analyses={})
    r = execute(ec, wire, "run1")
    assert r.status.value == "FAIL" and "Sales Target" in r.message and "to Targets" in r.message
    assert "Data As Of" in r.message and "normally unaffected" in r.message
    wire.approval_status = "APPROVED"                                            # accepting the current wiring
    assert execute(ec, wire, "run2").status.value == "PASS"
    m = execute(ec, ui, "run1")
    assert m.status.value == "NOT EXECUTED"


def test_ignored_only_needs_approval_then_passes(tmp_path):
    model, env = _setup(tmp_path)
    model.relationships.append(model.relationships[0].model_copy(update={"from_table": "Targets", "from_column": "DealerKey"}))
    from app.core.project import infer_kpis as _ik
    kpis = _ik(model)
    wire = next(t for t in generate_tests(env, kpis, model, [], []) if t.id == "VIS.slc.WIRE")
    ec = ExecContext(env=env, bi=None, data=None, kpis={k.id: k for k in kpis}, model=model, analyses={})
    assert execute(ec, wire, "r").status.value == "NOT VALIDATED"
    wire.approval_status = "APPROVED"
    r = execute(ec, wire, "r")
    assert r.status.value == "PASS" and "excluded on purpose" in r.message


def test_legacy_report_interaction_codes(tmp_path):
    rel = [{"source": "s1", "target": "c1", "type": 3}, {"source": "s1", "target": "c2", "type": 1}]
    rep = {"sections": [{"name": "sec", "displayName": "P", "config": json.dumps({"relationships": rel}), "filters": "[]",
                         "visualContainers": [{"config": json.dumps({"name": "s1", "singleVisual": {"visualType": "slicer", "prototypeQuery": {
                             "From": [{"Name": "d", "Entity": "Dealer"}], "Select": [{"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": "DealerName"}}]}}})}]}]}
    p = tmp_path / "report.json"
    p.write_text(json.dumps(rep))
    vs, _ = parse_legacy_report(p)
    assert vs[0].interactions == {"c1": "NoFilter", "c2": "DataFilter"}
