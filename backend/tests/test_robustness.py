"""Robustness regression tests: real-world TMDL / TMSL / report variants and DAX fuzzing.

The fixture deliberately mixes the variations found in public Power BI projects: space indentation,
KPI and dynamic-format-string child objects, calculation groups, shared M expressions with
parameters, Direct Lake entity partitions, native SQL, a calculated calendar, snowflaked
dimensions, names with spaces/quotes/brackets, legacy report.json and model.bim.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyzers.dax import DaxAnalyzer, tokenize                     # noqa: E402
from app.analyzers.dax_infer import Untranslatable, translate             # noqa: E402
from app.core.project import create_project, infer_kpis                  # noqa: E402
from app.engine.runner import ExecContext, execute_test                  # noqa: E402
from app.engine.sql_generator import kpi_value_sql, transpile_script     # noqa: E402
from app.engine.sql_safety import assert_read_only                       # noqa: E402
from app.engine.test_generator import generate_tests                     # noqa: E402
from app.metadata.pbip import read_pbip                                  # noqa: E402

S4 = "    "


def _w(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8-sig")


@pytest.fixture()
def stress(tmp_path) -> Path:
    root = tmp_path / "Stress"
    d = root / "Stress.SemanticModel" / "definition"
    _w(d / "expressions.tmdl",
       'expression SqlEndpoint = "dw.example.net" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n'
       '\tlineageTag: 1\n\n'
       'expression Database = "DW" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n\tlineageTag: 2\n')
    # 4-space indentation, KPI child, dynamic format string, bracket in name, unqualified columns
    _w(d / "tables" / "Sales Fact.tmdl", "\n".join([
        "table 'Sales Fact'",
        f"{S4}lineageTag: abc",
        "",
        f"{S4}/// Net revenue",
        f"{S4}measure 'Revenue' = SUMX('Sales Fact', 'Sales Fact'[Qty] * RELATED(Product[List Price]))",
        f"{S4}{S4}formatString: #,0",
        f"{S4}{S4}kpi",
        f"{S4}{S4}{S4}targetExpression = [Target]",
        f"{S4}{S4}{S4}statusExpression =",
        f"{S4}{S4}{S4}{S4}VAR x = [Revenue] RETURN IF(x > [Target], 1, -1)",
        "",
        f"{S4}measure 'Rev [Net]' =",
        f"{S4}{S4}{S4}CALCULATE(",
        f"{S4}{S4}{S4}    SUM([Amount]),",
        f"{S4}{S4}{S4}    KEEPFILTERS(FILTER(ALL(Category[Category Name]), Category[Category Name] = \"Bikes\" || Category[Category Name] = \"Parts\"))",
        f"{S4}{S4}{S4})",
        f"{S4}{S4}formatStringDefinition = ```",
        f"{S4}{S4}{S4}{S4}IF([Revenue] > 1e6, \"#,0,,M\", \"#,0\")",
        f"{S4}{S4}{S4}{S4}```",
        "",
        f"{S4}measure Target = 1000",
        f"{S4}measure 'Avg Daily Revenue' = AVERAGEX(VALUES('Calendar'[Date]), [Revenue])",
        f"{S4}measure 'Margin' = [Revenue] - SUM('Sales Fact'[Cost])",
        f"{S4}measure 'Margin %' = DIVIDE([Margin], [Revenue]) * 100",
        f"{S4}measure 'Orders .5' = COUNTROWS(FILTER('Sales Fact', 'Sales Fact'[Discount] > .5 && NOT ISBLANK('Sales Fact'[OrderId])))",
        f"{S4}measure 'Label' = \"Revenue: \" & FORMAT([Revenue], \"#,0\")",
        "",
        f"{S4}column OrderId",
        f"{S4}{S4}dataType: string",
        f"{S4}{S4}sourceColumn: order_id",
        f"{S4}column Qty",
        f"{S4}{S4}dataType: int64",
        f"{S4}{S4}sourceColumn: qty",
        f"{S4}column Amount",
        f"{S4}{S4}dataType: decimal",
        f"{S4}{S4}sourceColumn: amount",
        f"{S4}column Cost",
        f"{S4}{S4}dataType: decimal",
        f"{S4}{S4}sourceColumn: cost",
        f"{S4}column Discount",
        f"{S4}{S4}dataType: double",
        f"{S4}{S4}sourceColumn: discount",
        f"{S4}column ProductKey",
        f"{S4}{S4}dataType: int64",
        f"{S4}{S4}sourceColumn: product_key",
        f"{S4}column 'Order Date'",
        f"{S4}{S4}dataType: dateTime",
        f"{S4}{S4}sourceColumn: order date",
        f"{S4}column 'Big Order' = 'Sales Fact'[Qty] > 10",
        f"{S4}{S4}dataType: boolean",
        "",
        f"{S4}partition 'Sales Fact' = m",
        f"{S4}{S4}mode: import",
        f"{S4}{S4}source =",
        f"{S4}{S4}{S4}{S4}let",
        f'{S4}{S4}{S4}{S4}    Source = Sql.Database(#"SqlEndpoint", #"Database"),',
        f'{S4}{S4}{S4}{S4}    dbo_FactSales = Source{{[Schema="dbo",Item="Fact Sales"]}}[Data]',
        f"{S4}{S4}{S4}{S4}in",
        f"{S4}{S4}{S4}{S4}    dbo_FactSales",
        ""]))
    _w(d / "tables" / "Product.tmdl", "table Product\n\n\tcolumn ProductKey\n\t\tdataType: int64\n\t\tsourceColumn: product_key\n\n"
       "\tcolumn 'List Price'\n\t\tdataType: decimal\n\t\tsourceColumn: list_price\n\n\tcolumn CategoryKey\n\t\tdataType: int64\n\t\tsourceColumn: category_key\n\n"
       "\tpartition product = entity\n\t\tmode: directLake\n\t\tsource\n\t\t\tentityName: dim_product\n\t\t\tschemaName: gold\n\t\t\texpressionSource: DatabaseQuery\n")
    _w(d / "tables" / "Category.tmdl", "table Category\n\n\tcolumn CategoryKey\n\t\tdataType: int64\n\t\tsourceColumn: category_key\n\n"
       "\tcolumn 'Category Name'\n\t\tdataType: string\n\t\tsourceColumn: name\n\n"
       '\tpartition Category = m\n\t\tmode: import\n\t\tsource = Value.NativeQuery(Sql.Database("srv", "DW"), "SELECT category_key, name FROM dbo.dim_category WHERE active = 1", null, [EnableFolding=true])\n')
    _w(d / "tables" / "Calendar.tmdl", "table Calendar\n\tdataCategory: Time\n\n\tcolumn Date\n\t\tdataType: dateTime\n\t\tisKey\n\t\tsourceColumn: [Date]\n\n"
       '\tcolumn Month = FORMAT([Date], "yyyy-MM")\n\t\tdataType: string\n\n\tpartition Calendar = calculated\n\t\tmode: import\n\t\tsource = CALENDAR(DATE(2020,1,1), DATE(2026,12,31))\n')
    _w(d / "tables" / "Time Intelligence.tmdl", "table 'Time Intelligence'\n\n\tcalculationGroup\n\t\tprecedence: 1\n\n\t\tcalculationItem YTD = CALCULATE(SELECTEDMEASURE(), DATESYTD('Calendar'[Date]))\n\n"
       "\tcolumn Name\n\t\tdataType: string\n\t\tsourceColumn: Name\n")
    _w(d / "relationships.tmdl",
       "relationship r1\n\tfromColumn: 'Sales Fact'.ProductKey\n\ttoColumn: Product.ProductKey\n\n"
       "relationship r2\n\tfromColumn: Product.CategoryKey\n\ttoColumn: Category.CategoryKey\n\n"
       "relationship r3\n\tfromColumn: 'Sales Fact'.'Order Date'\n\ttoColumn: Calendar.Date\n")
    # legacy report.json (PBIR-Legacy) with sparkline, implicit aggregation, auto date hierarchy
    sel = [{"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Revenue"}},
           {"Column": {"Expression": {"SourceRef": {"Source": "c"}}, "Property": "Month"}},
           {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Qty"}}, "Function": 0}},
           {"SparklineData": {"Measure": {"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Revenue"}}}}]
    vc = {"config": json.dumps({"name": "v1", "singleVisual": {"visualType": "lineChart", "prototypeQuery": {
        "From": [{"Name": "s", "Entity": "Sales Fact"}, {"Name": "c", "Entity": "Calendar"}], "Select": sel},
        "vcObjects": {"title": [{"properties": {"text": {"expr": {"Literal": {"Value": "'Revenue trend'"}}}}}]}}})}
    slicer = {"config": json.dumps({"name": "v2", "singleVisual": {"visualType": "slicer", "prototypeQuery": {
        "From": [{"Name": "k", "Entity": "Category"}], "Select": [{"Column": {"Expression": {"SourceRef": {"Source": "k"}}, "Property": "Category Name"}}]}}})}
    broken = {"config": json.dumps({"name": "v3", "singleVisual": {"visualType": "card", "prototypeQuery": {
        "From": [{"Name": "s", "Entity": "Sales Fact"}], "Select": [{"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Deleted Measure"}}]}}})}
    _w(root / "Stress.Report" / "report.json", json.dumps({"sections": [{"name": "p1", "displayName": "Overview",
                                                                          "visualContainers": [vc, slicer, broken, {"config": "{not json"}]}]}))
    return root


def test_reader_handles_real_world_tmdl(stress):
    m = read_pbip(stress)
    t = {x.name: x for x in m.tables}
    assert set(t) == {"Sales Fact", "Product", "Category", "Calendar", "Time Intelligence"}
    rev = m.measure("Revenue")
    assert rev.expression.startswith("SUMX(") and "targetExpression" not in rev.expression      # KPI child not leaked
    net = m.measure("Rev [Net]")
    assert net and "formatStringDefinition" not in net.expression and "```" not in net.expression and "1e6" not in net.expression
    assert t["Sales Fact"].source_kind == "sqlserver" and t["Sales Fact"].source_ref == "DW.dbo.Fact Sales"   # parameters resolved
    assert (t["Product"].source_kind, t["Product"].source_ref) == ("directlake", "gold.dim_product")
    assert t["Category"].source_kind == "native_query" and t["Category"].source_ref.startswith("(SELECT")
    assert t["Calendar"].source_kind == "calculated" and t["Calendar"].is_date_table
    assert t["Time Intelligence"].source_kind == "calculation_group"
    assert next(c for c in t["Sales Fact"].columns if c.name == "Big Order").expression
    assert len(m.relationships) == 3
    v = {x.id: x for x in m.visuals}
    assert v["v1"].measures == ["[Revenue]", "implicit:Sales Fact[Qty]"] and v["v1"].dimensions == ["Calendar[Month]"]
    assert v["v1"].title == "Revenue trend" and v["v2"].visual_type == "slicer"
    assert any("could not be read" in n for n in m.unsupported)                                 # bad visual isolated


def test_static_analysis_no_false_positives(stress):
    m = read_pbip(stress)
    an = DaxAnalyzer(m)
    for ms in m.all_measures():
        errs = [f for f in an.static(ms.name, ms.expression) if f.severity == "error"]
        assert not errs, (ms.name, errs)
    assert tokenize("[A]]B]")[0].text == "[A]]B]" and tokenize(".5")[0].kind == "number"


def test_translation_patterns(stress):
    m = read_pbip(stress)
    k = {x.name: x for x in infer_kpis(m)}
    assert k["Revenue"].measure.column_expr == "({Sales Fact.Qty} * {Product.List Price})"
    net = k["Rev [Net]"].measure
    assert net.column == "Sales Fact.Amount" and net.filters[0].op == "in" and net.filters[0].value == ["Bikes", "Parts"]
    assert k["Avg Daily Revenue"].measure.per_field == "Calendar.Date"
    assert k["Margin"].kind == "formula" and len(k["Margin"].components) == 2
    assert k["Margin %"].kind == "formula" and k["Margin %"].formula["op"] == "*"
    assert {f.op for f in k["Orders .5"].measure.filters} == {">", "is not null"}
    assert "text/display" in " ".join(k["Label"].translation_notes)
    assert "constant" in " ".join(k["Target"].translation_notes)
    assert k["Revenue"].date_field == "Calendar.Date"


def test_sql_generation_snowflake_joins_quoting_dialects(stress):
    m = read_pbip(stress)
    env, notes = create_project(m, stress)
    kpis = {x.name: x for x in infer_kpis(m)}
    g = kpi_value_sql(kpis["Rev [Net]"], env, [], allow_draft=True)
    sql = g.render_inline()
    assert "FROM DW.dbo.`Fact Sales` AS t_sales_fact" in sql
    assert "LEFT JOIN gold.dim_product AS t_product" in sql and "LEFT JOIN (SELECT category_key" in sql   # 2-hop + native query
    g2 = kpi_value_sql(kpis["Revenue"], env, [], grain="Calendar.Month", allow_draft=True)
    assert "date_format(t_sales_fact.`order date`, 'yyyy-MM') AS grain" in g2.sql
    g3 = kpi_value_sql(kpis["Avg Daily Revenue"], env, [], allow_draft=True)
    assert "per_member" in g3.sql and "AVG(v)" in g3.sql
    g4 = kpi_value_sql(kpis["Margin %"], env, [], grain="Calendar.Month", allow_draft=True)
    assert "FULL OUTER JOIN c1" in g4.sql
    for x in (g, g2, g3, g4):
        assert_read_only(x.sql)
    tsql = transpile_script(sql, "tsql")
    assert "[Fact Sales]" in tsql and "Dialect: tsql" in tsql


def test_full_pipeline_never_errors(stress):
    m = read_pbip(stress)
    env, _ = create_project(m, stress)
    kpis = infer_kpis(m)
    an = DaxAnalyzer(m)
    analyses = an.analyze(env, kpis)
    tests = generate_tests(env, kpis, m, analyses, [])
    ctx = ExecContext(env=env, bi=None, data=None, kpis={k.id: k for k in kpis}, model=m,
                      analyses={a.measure.lower(): a for a in analyses})
    res = {t.id: execute_test(ctx, t, "R") for t in tests}
    assert not [r for r in res.values() if r.status.value == "ERROR"]
    binding = {t.visual_id: res[t.id].status.value for t in tests if t.executor == "visual_binding"}
    assert binding == {"v1": "PASS", "v2": "PASS", "v3": "FAIL"}                               # only the truly broken visual


def test_model_bim(tmp_path):
    bim = {"name": "SemanticModel", "model": {"tables": [
        {"name": "Sales", "columns": [{"name": "Amount", "dataType": "decimal", "sourceColumn": "amount"},
                                      {"name": "Region", "dataType": "string", "sourceColumn": "region"},
                                      {"name": "RowNumber-2662979B", "type": "rowNumber"}],
         "measures": [{"name": "Total", "expression": ["CALCULATE(", "  SUM(Sales[Amount]),", "  Sales[Region] = \"EU\")"]}],
         "partitions": [{"name": "p", "source": {"type": "m", "expression": [
             "let", '  Source = Snowflake.Databases("acct.snowflakecomputing.com", "WH"),',
             '  db = Source{[Name="SALES_DB",Kind="Database"]}[Data],', '  sc = db{[Name="PUBLIC",Kind="Schema"]}[Data],',
             '  t = sc{[Name="ORDERS",Kind="Table"]}[Data]', "in", "  t"]}}]}],
        "relationships": []}}
    _w(tmp_path / "X.Dataset" / "model.bim", json.dumps(bim))
    m = read_pbip(tmp_path)
    assert m.model_name == "SemanticModel" and m.tables[0].source_ref == "SALES_DB.PUBLIC.ORDERS"
    assert [c.name for c in m.tables[0].columns] == ["Amount", "Region"]
    k = infer_kpis(m)[0]
    assert k.measure.filters[0].value == "EU"
    env, _ = create_project(m, tmp_path)
    assert env.data.platform == "snowflake"


def test_thin_report_message(tmp_path):
    _w(tmp_path / "Only.Report" / "report.json", "{}")
    with pytest.raises(FileNotFoundError, match="thin report"):
        read_pbip(tmp_path)


ATOMS = ["SUM('T'[A])", "COUNTROWS(T)", "[M1]", "1", ".5", "BLANK()", "'T'[A]", "\"x\"", "T[B]", "DATE(2024,1,1)",
         "SELECTEDVALUE(T[B])", "VALUES(T[B])", "ALL(T)", "{1,2}", "TRUE()", "[Missing]"]
OPS = ["+", "-", "*", "/", "&&", "||", "=", "<>", ">=", "&", "IN"]
FUNCS = ["CALCULATE", "DIVIDE", "IF", "FILTER", "SUMX", "AVERAGEX", "KEEPFILTERS", "NOT", "COUNTROWS", "SWITCH", "MAX", "ISBLANK"]


def _rand_expr(r: random.Random, depth=0) -> str:
    c = r.random()
    if depth > 3 or c < 0.3:
        return r.choice(ATOMS)
    if c < 0.55:
        return f"{_rand_expr(r, depth + 1)} {r.choice(OPS)} {_rand_expr(r, depth + 1)}"
    if c < 0.65:
        return f"VAR v{depth} = {_rand_expr(r, depth + 1)} RETURN v{depth} {r.choice(OPS)} {_rand_expr(r, depth + 1)}"
    if c < 0.7:
        return _rand_expr(r, depth + 1)[: r.randint(1, 12)]          # truncated / malformed
    args = ", ".join(_rand_expr(r, depth + 1) for _ in range(r.randint(0, 3)))
    return f"{r.choice(FUNCS)}({args})"


def test_dax_fuzz_never_crashes(stress):
    m = read_pbip(stress)
    an = DaxAnalyzer(m)
    r = random.Random(7)
    for _ in range(1500):
        e = _rand_expr(r)
        an.static("fuzz", e)
        an.expand(e, ("fuzz",), mark=True)
        try:
            translate(e, m, "Sales Fact")
        except Untranslatable:
            pass
