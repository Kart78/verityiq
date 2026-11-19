"""Builds the optional demonstration scenario (sample data + PBIR report definition).

    python scripts/seed_demo.py

Creates demo/data/cc_sample.duckdb with deterministic sample data whose latest
record is 2 days old (so the freshness test demonstrates a FAIL), 3 orphan case rows
(referential-integrity FAIL) and realistic nulls. Sample data only - not customer data.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "demo" / "data" / "cc_sample.duckdb"
REPORT = ROOT / "demo" / "pbip" / "CustomerCenter.Report"

STATES = ["TX", "CA", "FL", "NY", "IL", "GA", "OH", "MI", "AZ", "WA"]
WEIGHTS = [18, 16, 12, 11, 8, 8, 7, 7, 7, 6]


def seed_data(today: date) -> None:
    rnd = random.Random(42)
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = duckdb.connect(str(DB))
    con.execute("CREATE SCHEMA customer_center")
    con.execute("""CREATE TABLE customer_center.customers (customer_id VARCHAR, customer_name VARCHAR,
                   state VARCHAR, is_active BOOLEAN, created_date DATE)""")
    con.execute("""CREATE TABLE customer_center.cases (case_id VARCHAR, customer_id VARCHAR, status VARCHAR,
                   first_contact_resolved BOOLEAN, is_escalated BOOLEAN, handle_time_min DOUBLE, csat_score INTEGER,
                   created_date DATE, updated_at TIMESTAMP)""")
    customers = []
    for i in range(1, 2401):
        customers.append((f"C{i:05d}", f"Customer {i}", rnd.choices(STATES, WEIGHTS)[0], rnd.random() < 0.9,
                          today - timedelta(days=rnd.randint(30, 1500))))
    con.executemany("INSERT INTO customer_center.customers VALUES (?,?,?,?,?)", customers)
    latest = today - timedelta(days=2)
    start = latest - timedelta(days=455)
    cases = []
    for i in range(1, 30001):
        d = start + timedelta(days=int((rnd.random() ** 0.8) * 455))
        status = rnd.choices(["Closed", "Open", "In Progress", "Cancelled"], [70, 14, 9, 7])[0]
        closed = status == "Closed"
        cust = f"C{rnd.randint(1, 2400):05d}"
        cases.append((f"K{i:06d}", cust, status, closed and rnd.random() < 0.72, rnd.random() < 0.06,
                      None if rnd.random() < 0.02 else round(rnd.lognormvariate(2.4, 0.5), 2),
                      rnd.choices([1, 2, 3, 4, 5], [5, 8, 17, 35, 35])[0] if closed and rnd.random() < 0.4 else None,
                      d, datetime.combine(min(d + timedelta(days=rnd.randint(0, 3)), latest), datetime.min.time())
                      + timedelta(hours=rnd.randint(8, 18))))
    for j, cid in enumerate(["C99901", "C99902", "C99903"]):       # orphan customer keys
        cases.append((f"K9{j:05d}", cid, "Closed", True, False, 10.0, 5, latest - timedelta(days=10),
                      datetime.combine(latest, datetime.min.time())))
    con.executemany("INSERT INTO customer_center.cases VALUES (?,?,?,?,?,?,?,?,?)", cases)
    con.close()
    print(f"Sample data: {len(customers)} customers, {len(cases)} cases, latest record {latest}")


def _field(kind: str, entity: str, prop: str) -> dict:
    return {"field": {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}}


def _visual(name, vtype, title, roles: dict, x=0, y=0):
    return {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/1.0.0/schema.json",
            "name": name, "position": {"x": x, "y": y, "width": 300, "height": 200},
            "visual": {"visualType": vtype,
                       "query": {"queryState": {role: {"projections": projs} for role, projs in roles.items()}},
                       "visualContainerObjects": {"title": [{"properties": {"text": {"expr": {"Literal": {"Value": f"'{title}'"}}}}}]}}}


def build_report() -> None:
    pages = {
        "overview": ("Overview", [
            _visual("cardTotalCases", "card", "Total Cases", {"Values": [_field("Measure", "Cases", "Total Cases")]}),
            _visual("cardFCR", "card", "First Contact Resolution %", {"Values": [_field("Measure", "Cases", "FCR %")]}),
            _visual("lineCasesByMonth", "lineChart", "Cases by Month",
                    {"Category": [_field("Column", "Date", "Month")], "Y": [_field("Measure", "Cases", "Total Cases")]}),
            _visual("barCsatByState", "clusteredBarChart", "CSAT by State",
                    {"Category": [_field("Column", "Customers", "State")], "Y": [_field("Measure", "Cases", "CSAT Score")]}),
            _visual("slicerState", "slicer", "State", {"Values": [_field("Column", "Customers", "State")]}),
            _visual("tableAgents", "tableEx", "Agent Workload",
                    {"Values": [_field("Column", "Cases", "AgentName"), _field("Measure", "Cases", "Total Cases")]}),
        ]),
        "quality": ("Service Quality", [
            _visual("colFcrByState", "clusteredColumnChart", "FCR % by State",
                    {"Category": [_field("Column", "Customers", "State")], "Y": [_field("Measure", "Cases", "FCR %")]}),
            _visual("cardAHT", "card", "Avg Handle Time", {"Values": [_field("Measure", "Cases", "Avg Handle Time (min)")]}),
            _visual("cardCustomers", "card", "Active Customers", {"Values": [_field("Measure", "Customers", "Total Customers")]}),
        ]),
    }
    for pid, (display, visuals) in pages.items():
        pdir = REPORT / "definition" / "pages" / pid
        (pdir / "visuals").mkdir(parents=True, exist_ok=True)
        (pdir / "page.json").write_text(json.dumps({"name": pid, "displayName": display}, indent=2))
        for v in visuals:
            vdir = pdir / "visuals" / v["name"]
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "visual.json").write_text(json.dumps(v, indent=2))
    (REPORT / "definition.pbir").write_text(json.dumps({"version": "4.0", "datasetReference": {"byPath": {"path": "../CustomerCenter.SemanticModel"}}}, indent=2))
    print(f"PBIR report: {sum(len(v) for _, v in pages.values())} visuals on {len(pages)} pages")


if __name__ == "__main__":
    seed_data(date.today())
    build_report()
