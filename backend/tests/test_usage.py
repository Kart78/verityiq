"""Model usage (unused DAX / columns) and strict requirement/screenshot scope."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyzers.usage import analyze_usage, unused_measures  # noqa: E402
from app.core.project import create_project, infer_kpis  # noqa: E402
from app.core.scope import in_scope  # noqa: E402
from app.engine.test_generator import generate_tests  # noqa: E402
from app.metadata.pbip import read_pbip  # noqa: E402

from test_functional import _card, _w, build  # noqa: E402


def build_usage(tmp: Path) -> Path:
    root = build(tmp)
    d = root / "M.SemanticModel" / "definition"
    # extra objects: a dead measure chain, a helper used only by a used measure, sort-by, hierarchy, RLS, calc column
    _w(d / "tables" / "Sales.tmdl", "\n".join([
        "table Sales",
        "\tmeasure 'Vehicle Sales' = COUNTROWS(Sales)",
        "\tmeasure 'Sales Revenue' = [Base Revenue] * 1",
        "\tmeasure 'Base Revenue' = SUM(Sales[Amount])",
        "\tmeasure 'Old KPI' = [Older Helper] + 1",
        "\tmeasure 'Older Helper' = SUM(Sales[Discount])",
        "\tcolumn DealerKey", "\t\tdataType: int64", "\t\tsourceColumn: dealer_key",
        "\tcolumn Amount", "\t\tdataType: double", "\t\tsourceColumn: amount",
        "\tcolumn Discount", "\t\tdataType: double", "\t\tsourceColumn: discount",
        "\tcolumn Notes", "\t\tdataType: string", "\t\tsourceColumn: notes",
        "\tcolumn 'Net Amount' = Sales[Amount] - Sales[Discount]", "\t\tdataType: double",
        "\tpartition Sales = m", "\t\tmode: import", "\t\tsource =",
        '\t\t\t\tlet S = Databricks.Catalogs("h", "/p"), C = S{[Name="cat",Kind="Database"]}[Data], D = C{[Name="sales",Kind="Schema"]}[Data], T = D{[Name="sales",Kind="Table"]}[Data] in T', ""]))
    _w(d / "tables" / "Dealer.tmdl", "\n".join([
        "table Dealer", "\tcolumn DealerKey", "\t\tdataType: int64", "\t\tsourceColumn: dealer_key",
        "\tcolumn DealerName", "\t\tdataType: string", "\t\tsourceColumn: dealer_name", "\t\tsortByColumn: DealerOrder",
        "\tcolumn DealerOrder", "\t\tdataType: int64", "\t\tsourceColumn: dealer_order",
        "\tcolumn Country", "\t\tdataType: string", "\t\tsourceColumn: country",
        "\tcolumn Phone", "\t\tdataType: string", "\t\tsourceColumn: phone",
        "\thierarchy Geo", "\t\tlevel Region", "\t\t\tcolumn: Country", "",
        "\tpartition Dealer = m", "\t\tmode: import", "\t\tsource =",
        '\t\t\t\tlet S = Databricks.Catalogs("h", "/p"), C = S{[Name="cat",Kind="Database"]}[Data], D = C{[Name="sales",Kind="Schema"]}[Data], T = D{[Name="dealer",Kind="Table"]}[Data] in T', ""]))
    _w(d / "roles" / "EU.tmdl", "role EU\n\tmodelPermission: read\n\n\ttablePermission Dealer = 'Dealer'[Country] == \"DE\"\n")
    return root


def test_usage_classifies_every_object(tmp_path):
    model = read_pbip(build_usage(tmp_path))
    u = analyze_usage(model)
    st = {o["ref"]: o["status"] for o in u["objects"]}
    assert st["[Vehicle Sales]"] == "report" and st["[Sales Revenue]"] == "report"
    assert st["[Base Revenue]"] == "indirect"                                 # only used through [Sales Revenue]
    assert st["[Old KPI]"] == "unused" and st["[Older Helper]"] == "used_by_unused"   # dead chain, removable together
    assert st["'Sales'[Discount]"] == "used_by_unused" and st["'Sales'[Notes]"] == "unused" and st["'Sales'[Net Amount]"] == "unused"
    helper = next(o for o in u["objects"] if o["ref"] == "[Older Helper]")
    assert helper["used_by"] == ["Only used by [Old KPI] (unused)"]
    assert st["'Sales'[Amount]"] == "indirect"
    assert st["'Dealer'[DealerName]"] == "report" and st["'Dealer'[DealerOrder]"] == "indirect"   # sort order of a used column
    assert st["'Dealer'[Country]"] == "structure"                             # row-level security + hierarchy level
    assert st["'Dealer'[Phone]"] == "unused"
    assert st["'Sales'[DealerKey]"] == "structure"                            # relationship key
    base = next(o for o in u["objects"] if o["ref"] == "[Base Revenue]")
    assert "Used by [Sales Revenue]" in base["used_by"]
    assert u["summary"]["measures_unused"] == 2
    assert unused_measures(model) == {"old kpi", "older helper"}


def test_unused_measures_never_become_tests(tmp_path):
    model = read_pbip(build_usage(tmp_path))
    env, _ = create_project(model, tmp_path, project_id="u", name="u")
    kpis = infer_kpis(model)
    names = {k.name for k in in_scope(kpis, model, {"mode": "all"})}
    assert "Old KPI" not in names and "Older Helper" not in names and "Vehicle Sales" in names
    assert "Old KPI" in {k.name for k in in_scope(kpis, model, {"mode": "all", "include_unused": True})}


def test_screenshot_scope_writes_only_the_selected_kpis(tmp_path):
    model = read_pbip(build_usage(tmp_path))
    env, _ = create_project(model, tmp_path, project_id="s", name="s")
    kpis = infer_kpis(model)
    tests = generate_tests(env, kpis, model, [], [], scope={"mode": "requirements", "selected": ["Vehicle Sales"]})
    ids = [t.id for t in tests]
    assert ids and all(t.kpi_id == next(k.id for k in kpis if k.name == "Vehicle Sales") for t in tests), ids
    assert not any(t.id.startswith(("VIS.", "DQ.")) or ".CTX." in t.id for t in tests)
    with_slicers = generate_tests(env, kpis, model, [], [], scope={"mode": "requirements", "selected": ["Vehicle Sales"],
                                                                    "include": {"slicers": True}})
    extra = {t.id for t in with_slicers} - set(ids)
    assert "VIS.slc.WIRE" in extra and not any(x.startswith("DQ.") or x.endswith(".BIND") for x in extra)
    dash = generate_tests(env, kpis, model, [], [], scope={"mode": "dashboard"})
    assert any(t.id.startswith("DQ.") for t in dash) and any(t.id.endswith(".BIND") for t in dash)


def test_keep_and_other_reports_count_as_usage(tmp_path):
    from app.core.models import VisualMeta
    model = read_pbip(build_usage(tmp_path))
    u = analyze_usage(model, keep={"m:old kpi"})
    st = {o["ref"]: o["status"] for o in u["objects"]}
    assert st["[Old KPI]"] == "kept" and st["[Older Helper]"] == "indirect"
    other = VisualMeta(id="x1", page="Finance", visual_type="card", report="Finance Report", refs=["c|Sales|Notes"])
    u2 = analyze_usage(model, extra_visuals=[other])
    o = next(o for o in u2["objects"] if o["ref"] == "'Sales'[Notes]")
    assert o["status"] == "report" and o["used_by"][0].startswith("Visual [Finance Report]")
    assert u2["summary"]["reports_scanned"] == 2
