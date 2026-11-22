# VerityIQ - Universal BI & Data Quality Assurance (Phase 1)

VerityIQ (configurable via BIQA_APP_NAME) checks a dashboard in four ways. By default only the KPIs actually shown on visible report pages are tested, with each visual's report, page, slicer and visual filters applied; scope can also come from an uploaded requirement document or screenshot, a hand-picked list, or all measures:
- **DAX check:** does each measure implement the approved business definition?
- **Number check:** does each KPI and chart point match a value computed independently from the source?
- **Data check:** are the source data complete, unique and fresh?
- **Evidence:** every verdict comes with traceable evidence.

Power BI and Databricks are the first implemented adapters, not architectural dependencies.

```
Business requirement → KPI definition (approved) → DAX measure → source SQL → expected value
                                                      ↓                          ↓
                                              DAX analyzer status      actual value (BI query, same filter context)
                                                                                 ↓
                                                         compare (tolerance) → result + evidence → defect
```

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --app-dir backend --port 8000      # open http://localhost:8000
cd backend && pytest -q                                 # 37 tests incl. robustness + DAX fuzzing
```

## How the tool is used

The tool is generic: there is nothing customer-, model- or KPI-specific in the code.

1. **Upload a PBIP.**
   - Pick the project folder, or a `.zip` of it, on the *Project / Upload PBIP* page.
   - The tool parses the TMDL model and the PBIR report: measures, columns, relationships, visuals and slicers.
   - It detects the physical source table for each table from the partition (Power Query M): Databricks catalogs, SQL Server `Schema/Item`, Snowflake and native queries.
   - Columns on a calculated date table, such as `FORMAT([Date],"yyyy-MM")` or `YEAR([Date])`, are resolved through the fact table's relationship key.
   - Every measure becomes a KPI. Its logic is **inferred from the DAX**; the result is a DRAFT and not independent.
2. **Tab 1 – DAX Analysis.** Each KPI gets a PASS/FAIL.
   - The checks cover syntax, references, best practice and business logic.
   - Business logic is only verified for KPIs with a **business definition**. Without one, a PASS is labelled *syntax only*.
   - Supply definitions in one of two ways:
     - Download the Excel template (pre-listed with every measure, with the DAX interpretation for reference), fill it in and upload it.
     - Open a KPI and enter its definition there.
3. **Tab 2 – Test Cases.**
   - Tests are generated per KPI (total, per chart axis, per slicer), per visual (field bindings) and per source table (nulls, duplicates, orphan keys, freshness).
   - Every KPI test shows the **Databricks SQL** for the expected value and the **DAX query** for the actual value.
   - Export them as `.sql` or `.csv`.
4. **Execute.**
   - Automatic: add Databricks and Power BI service-principal details in *Settings*. Secrets are only `${ENV_VAR}` references.
   - Manual: run the SQL in Databricks, read the report value, and use **Record result**. The result is logged as MANUAL evidence.
5. **Overview / Reports.**
   - QA score with coverage, trend and categories.
   - **KPI Trust Scorecard**: a KPI is *trusted* only with a business definition + DAX verified + numbers reconciled.
   - Issues and a printable report.

*Load the sample project* runs the same pipeline on `demo/pbip` with demo connectors:
- **Expected values:** genuinely computed. The generated SQL runs on DuckDB against sample data.
- **Actual values:** come from an emulation of the sample measures.
- All of these results are labelled DEMO.


## Robustness (what the tool accepts)

This was tested against 81 public Power BI models: 4,706 measures, 856 tables and 4,122 visuals from Microsoft, Tabular Editor, Fabric toolbox and community repositories. Every model is read, and no stage crashes.

**Model formats**
- TMDL: tab- or space-indented.
- TMSL `model.bim`, in both `*.SemanticModel` and older `*.Dataset` folders.
- Child objects are never mistaken for DAX: KPIs, dynamic format strings, annotations.
- Calculation groups are listed. Report-level measures and measure-KPI helper measures are recognised.

**Reports**
- PBIR folders and legacy `report.json`.
- Supported field types: implicit aggregations, sparklines, auto date hierarchies.
- A malformed visual is reported and skipped; the rest of the report is still read.

**Sources**
- Databricks, SQL Server / Azure SQL (shared expressions and parameters resolved), Snowflake, BigQuery, Redshift, Oracle, Postgres and MySQL.
- Fabric Direct Lake entities, native SQL queries, and DAX calculated tables (resolved through relationships).
- File, web and Kusto sources are flagged so you can set the source table on the Project page.

**DAX → SQL**, translated automatically:
- Aggregations, including iterator expressions with `RELATED` and per-member averages such as `AVERAGEX(VALUES(Date[Date]), [Sales])`.
- `CALCULATE` filters: `IN`, `OR` on one column, `NOT`, `ISBLANK`, `KEEPFILTERS`, and `FILTER(ALL|VALUES(col))`.
- Arithmetic between measures and `DIVIDE`, `VAR … RETURN` anywhere, unqualified columns, and case-insensitive names.

**DAX that is reported with a reason instead of guessed:**
- Time intelligence and filter-context modifiers (`ALL`, `REMOVEFILTERS`, `TREATAS`, `TOPN`).
- `IF`/`SWITCH`/`SELECTEDVALUE` logic, text and SVG measures, remote-model (`EXTERNALMEASURE`) and placeholder measures.
- For these, supply the business definition or an expected SQL.

**SQL**
- Multi-hop joins through snowflaked dimensions, quoted names with spaces and special characters, and native-query subqueries.
- Every statement passes the read-only parser guard.
- Scripts are produced in the source's dialect (T-SQL, Snowflake, BigQuery, …) for manual runs when no connector exists.

**Check your own models before rollout:**
```bash
python scripts/robustness_check.py "C:\path\to\folder-with-PBIP-projects"
```
It reports, per stage:
- models read and source tables recognised
- measures auto-translated, and why the others were not
- SQL generated
- crashes (expected: none)

It is read-only.

## Status semantics (enforced in `engine/runner.py`)

| Status | Meaning |
|---|---|
| PASS | Expected (source) and actual (BI) values agree within tolerance, with aligned context |
| FAIL | They disagree, or a structural check failed |
| NOT VALIDATED | No approved, independent definition, so an expected value can't be established |
| BLOCKED | A prerequisite is missing (a mapping, a connector, or a capability) |
| ERROR | The platform returned an error (for example, a broken measure) |
| NOT EXECUTED | Needs UI interaction, which isn't automated yet |

The **validated pass rate** is `PASS ÷ (PASS + FAIL)` and is always shown with **coverage**. Unproven tests never count as passes.

## Connect live Power BI + Databricks (Settings page, or YAML in config/samples/)

1. Copy `config/environments/template_powerbi_databricks.yaml.example` to `prod-xyz.yaml`.
2. Fill in IDs, and point `pbip_path` at the PBIP folder from source control. TMDL is how the DAX is read.
3. Set secrets as environment variables or in your secret manager: `PBI_CLIENT_SECRET`, `DATABRICKS_TOKEN`. Config holds only `${VAR}` references, and the API masks them.
4. Service-principal prerequisites:
   - Enable the tenant setting *Dataset Execute Queries REST API*.
   - Give the SP at least *Build* permission on the semantic model.
   - Grant the Databricks SP `USE CATALOG`, `USE SCHEMA` and `SELECT` only.
5. `pip install databricks-sql-connector`, then restart. Run **Test connections** → **Discover** → **Generate** → **Run**.

**Point a different BI/data stack at the same KPIs:** write a new environment YAML. The `mapping` block binds logical fields to `source` columns and `model` columns. No code changes.

**Add a KPI:** add it to the KPI YAML (or POST `/api/environments/{id}/kpis`), then have an approver approve it. No engine change.

**Add a connector:**
1. Subclass `BIConnector` or `DataConnector` in `backend/app/connectors/`.
2. Implement `capabilities()`, `test_connection()`, `discover()` / `list_tables()`, and `build_measure_query()` + `execute_query()` / `execute_sql()`.
3. Register it with `registry.register(...)`. The engine stays untouched.

## Layout

```
backend/app/
  core/          models (platform-neutral), environment config + secret resolution
  connectors/    base interfaces, registry, powerbi, databricks, demo
  metadata/      PBIP readers (TMDL, relationships, PBIR visuals)
  analyzers/     dax.py - DAX-only rules (never applied to other languages)
  engine/        sql_generator, sql_safety, compare, test_generator, runner
  repository/    SQLite store: metadata, tests, runs, results, defects, audit
  ai/            optional LLM (OpenAI-compatible / Anthropic), metadata-only prompts
  service.py     orchestration      main.py   FastAPI + RBAC
frontend/        no-build SPA (index.html, app.js, styles.css)
config/          environments/*.yaml, kpis/*.yaml
demo/            sample PBIP project, emulation of sample measures, sample data
```

## Security notes

- Every statement passes a parser-level read-only guard (`sql_safety.py`): single SELECT only, no DDL or DML.
- Filter values are bound as parameters. Inlined literals appear only in the exported, human-readable scripts.
- Secrets are resolved server-side only. Nothing secret is stored in the browser.
- RBAC roles are viewer < tester < approver < admin, set via `BIQA_USERS_JSON='{"<api-key>":{"name":"a@x.com","role":"approver"}}'`. Without it the app runs in **dev mode (auth disabled)**, and the UI says so. Replace this with SSO (Entra ID) before shared use.
- The LLM receives only metadata (definitions, DAX text, aggregate numbers), never raw rows. Its output is stored and shown as a *suggestion*.

## Roadmap

- **Phase 2:**
  - Tableau and Snowflake adapters.
  - Model metadata via XMLA when there's no PBIP folder.
  - Playwright-driven slicer / drill-through tests (the `.UI` tests that are NOT EXECUTED today).
  - Regression baselines.
  - Expression analyzers for LookML and Tableau calculations.
- **Phase 3:**
  - RLS impersonation (`impersonatedUserName`) per role.
  - Performance capture.
  - Scheduled runs.
  - SSO.
  - Postgres repository.

## Excel / CSV data sources

If the dashboard's tables come from Excel workbooks or CSV files (Power Query `Excel.Workbook`, `Csv.Document`,
`Parquet.Document`, over a local path, SharePoint or OneDrive), the project is created with the **Excel / CSV / Parquet files**
data platform. Upload the same files under *Project → Source files* (or *Settings*); files sitting at the path written in the
Power Query are picked up automatically when the app runs on that computer. Each table is read the way Power Query reads it
(sheet or named table, promoted headers, skipped rows, renamed columns) into a local DuckDB cache, and the validation SQL runs
there. Folder-combine queries and `.xls/.xlsb` workbooks are not supported (combine into one `.xlsx` instead).

## Screenshot OCR

Dashboard screenshots uploaded for test scope are read with the bundled offline OCR (`rapidocr-onnxruntime`, installed by
`requirements.txt`; no Tesseract needed). Matching tolerates small OCR typos; review the ticked KPIs before applying.

## Data platforms: where expected values come from

Settings → *Data platform* decides what the project is validated against:

| Platform | How | Driver |
|---|---|---|
| Excel / CSV / Parquet | the dashboard's own files (upload, folder path, or the Power Query path) | built in |
| Databricks | SQL warehouse, service principal or token | databricks-sql-connector |
| Snowflake | password, key pair, browser SSO or OAuth | snowflake-connector-python |
| SQL Server · Azure Synapse · Microsoft Fabric | SQL login, Windows, or Microsoft Entra (service principal / user / interactive) | pyodbc + Microsoft ODBC Driver 18 |
| PostgreSQL · Amazon Redshift | user + password | psycopg |
| MySQL | user + password | pymysql |
| Oracle | user + password, host/service or TNS | oracledb |
| BigQuery · Teradata · DB2 | see `requirements-optional.txt` | optional |

After a successful connection the **Project check** (Project page) verifies every model table and mapped column in that
platform, matches column names that differ only in case/separators, detects text-stored dates and lets you correct table
names. The validation SQL is generated once and translated to the platform's dialect (`engine/dialects.py`); only single
SELECT statements run, and sessions are opened read-only where the database supports it.

## Model Usage (Measure Killer-style)

*Model Usage* finds what the report really needs and produces a cleaned model:

- **Usage** - every measure and column classified as used in report / used indirectly / needed by model (relationship keys,
  hierarchy levels, sort-by, RLS, inputs of calculated tables) / kept by you / **used only by unused** / unused, with the
  "needed by" chain. Add other reports that use the same semantic model (thin reports) so their fields count; mark objects
  used by Excel or other tools as **Keep**.
- **Model size** - upload a VertiPaq Analyzer `.vpax` (DAX Studio › Advanced › Export Metrics) to see memory per table and
  column and how much the unused columns cost.
- **Clean up** - download a cleaned copy of the PBIP: unused measures and calculated columns deleted, unused data columns
  deleted *and* removed in Power Query (`Table.RemoveColumns` appended), translations and perspectives tidied. The copy is
  re-read and rejected if anything would still reference a removed object or a visual would break. M code per table is shown
  for applying by hand.
- **Best practices** - model and report rules (auto date/time, bi-directional and many-to-many relationships, unused
  inactive relationships, format strings, visible keys, floating-point money, crowded pages, broken fields, report-level
  measures, unused custom visuals, bookmarks, large columns from the .vpax).

Unused measures are excluded from KPI scopes automatically. Tenant-wide scanning (admin APIs) is not included.

## Deploying to Vercel

The repo is ready for Vercel (`vercel.json`, `index.py`, lean dependencies in `pyproject.toml`).

1. Import the GitHub repo in Vercel (Framework preset: **Other**) - or run `npx vercel --prod` in this folder.
2. **Required:** add the environment variable `BIQA_BASIC_AUTH` = `user:a-strong-password` (Settings › Environment Variables) and redeploy. Without it the hosted app refuses to start, so it is never open to anyone with the link.
3. Open the URL and sign in with that user and password.

Limits of the hosted version: storage is temporary (`/tmp`) - projects and results disappear when Vercel recycles the instance; uploads are capped at about 4.5 MB per request; no Windows Credential Manager (secrets go to a temporary file); no SQL Server/Fabric ODBC driver and no screenshot OCR. For day-to-day work use the local install.
