"""Connector registry - the single place that maps a platform key to an adapter.

Support levels are reported honestly to the UI. PLANNED entries have no adapter
class and therefore cannot be instantiated or shown as connected.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Optional

from ..core.models import SupportLevel


@dataclass(frozen=True)
class PlatformEntry:
    key: str
    name: str
    kind: str                     # bi | data | database | file
    support: SupportLevel
    adapter: Optional[str] = None  # "module:Class"
    notes: str = ""
    expression_language: Optional[str] = None
    sql_dialect: Optional[str] = None


PLATFORMS: list[PlatformEntry] = [
    # ---- BI
    PlatformEntry("powerbi", "Microsoft Power BI", "bi", SupportLevel.IMPLEMENTED,
                  "app.connectors.powerbi:PowerBIConnector",
                  "Model/visual metadata from PBIP (TMDL + PBIR). Workspaces, datasets, reports, refresh history "
                  "and measure values via REST API + executeQueries (service principal). Live path has unit tests "
                  "with mocked HTTP; verify against your tenant before relying on it.",
                  expression_language="dax"),
    PlatformEntry("demo_bi", "Demo BI fixture", "bi", SupportLevel.DEMO,
                  "app.connectors.demo:DemoBIConnector",
                  "Reads a PBIP model for metadata; returns captured fixture values instead of querying a service. "
                  "Every result it produces is labelled DEMO.", expression_language="dax"),
    PlatformEntry("tableau", "Tableau", "bi", SupportLevel.PLANNED, notes="Phase 2: Metadata API (GraphQL) + VizQL Data Service.", expression_language="tableau"),
    PlatformEntry("qlik", "Qlik Sense", "bi", SupportLevel.PLANNED, notes="Phase 2: Engine API (QIX).", expression_language="qlik"),
    PlatformEntry("looker", "Looker", "bi", SupportLevel.PLANNED, notes="Phase 2: Looker API 4.0 + LookML parse.", expression_language="lookml"),
    PlatformEntry("cognos", "IBM Cognos", "bi", SupportLevel.PLANNED, notes="Phase 2: Cognos REST API.", expression_language="cognos"),
    PlatformEntry("microstrategy", "MicroStrategy", "bi", SupportLevel.PLANNED, notes="Phase 2: REST API.", expression_language="mstr"),
    PlatformEntry("ssrs", "SSRS", "bi", SupportLevel.PLANNED, notes="Phase 2: REST API v2.0 + RDL parse.", expression_language="ssrs"),
    # ---- data platforms
    PlatformEntry("databricks", "Databricks SQL", "data", SupportLevel.IMPLEMENTED,
                  "app.connectors.databricks:DatabricksConnector",
                  "databricks-sql-connector against a SQL warehouse; Unity Catalog information_schema discovery. "
                  "Read-only statements only.", sql_dialect="databricks"),
    PlatformEntry("demo_sql", "Demo SQL (DuckDB sample data)", "data", SupportLevel.DEMO,
                  "app.connectors.demo:DemoSQLConnector",
                  "Executes the generated Databricks SQL for real (transpiled to DuckDB) against a local sample "
                  "dataset. Expected values are genuinely computed; the data is sample data.", sql_dialect="databricks"),
    PlatformEntry("snowflake", "Snowflake", "data", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:SnowflakeConnector",
                  "Live: runs the validation SQL translated to snowflake SQL, read-only. snowflake-connector-python; password, key pair, browser SSO or OAuth. Verify against your environment before relying on it.", sql_dialect="snowflake"),
    PlatformEntry("bigquery", "Google BigQuery", "data", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:BigQueryConnector",
                  "Live: runs the validation SQL translated to bigquery SQL, read-only. google-cloud-bigquery; service-account key file or gcloud login. Verify against your environment before relying on it.", sql_dialect="bigquery"),
    PlatformEntry("redshift", "Amazon Redshift", "data", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:RedshiftConnector",
                  "Live: runs the validation SQL translated to redshift SQL, read-only. psycopg (PostgreSQL protocol). Verify against your environment before relying on it.", sql_dialect="redshift"),
    PlatformEntry("synapse", "Azure Synapse", "data", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:SynapseConnector",
                  "Live: runs the validation SQL translated to tsql SQL, read-only. pyodbc + Microsoft ODBC Driver 18; SQL login or Microsoft Entra. Verify against your environment before relying on it.", sql_dialect="tsql"),
    PlatformEntry("fabric", "Microsoft Fabric", "data", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:FabricConnector",
                  "Live: runs the validation SQL translated to tsql SQL, read-only. Warehouse / Lakehouse SQL endpoint via pyodbc + ODBC Driver 18; Microsoft Entra. Verify against your environment before relying on it.", sql_dialect="tsql"),
    PlatformEntry("teradata", "Teradata", "data", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:TeradataConnector",
                  "Live: runs the validation SQL translated to teradata SQL, read-only. teradatasql; TD2, LDAP or browser SSO. Verify against your environment before relying on it.", sql_dialect="teradata"),
    # ---- databases
    PlatformEntry("sqlserver", "SQL Server", "database", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:SQLServerConnector",
                  "Live: runs the validation SQL translated to tsql SQL, read-only. pyodbc + Microsoft ODBC Driver 18; SQL, Windows or Microsoft Entra sign-in. Verify against your environment before relying on it.", sql_dialect="tsql"),
    PlatformEntry("oracle", "Oracle", "database", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:OracleConnector",
                  "Live: runs the validation SQL translated to oracle SQL, read-only. oracledb (thin mode, no Oracle client needed). Verify against your environment before relying on it.", sql_dialect="oracle"),
    PlatformEntry("postgres", "PostgreSQL", "database", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:PostgresConnector",
                  "Live: runs the validation SQL translated to postgres SQL, read-only. psycopg. Verify against your environment before relying on it.", sql_dialect="postgres"),
    PlatformEntry("mysql", "MySQL", "database", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:MySQLConnector",
                  "Live: runs the validation SQL translated to mysql SQL, read-only. pymysql. Verify against your environment before relying on it.", sql_dialect="mysql"),
    PlatformEntry("db2", "IBM DB2", "database", SupportLevel.IMPLEMENTED, "app.connectors.sqldb:DB2Connector",
                  "Live: runs the validation SQL translated to db2 SQL, read-only. ibm_db (beta: limited dialect translation). Verify against your environment before relying on it.", sql_dialect="db2"),
    # ---- files
    PlatformEntry("files", "Excel / CSV / Parquet files", "file", SupportLevel.IMPLEMENTED,
                  "app.connectors.files:FileConnector",
                  "Reads the workbooks/CSV files behind the model exactly as Power Query does (sheet or table, header "
                  "row, skipped rows) into a local DuckDB cache and runs the validation SQL there. Upload the files "
                  "in Settings, or keep them at the path written in the Power Query.", sql_dialect="databricks"),
]

_BY_KEY = {p.key: p for p in PLATFORMS}


def get_platform(key: str) -> PlatformEntry:
    if key not in _BY_KEY:
        raise KeyError(f"Unknown platform '{key}'")
    return _BY_KEY[key]


def create_connector(key: str, config: dict):
    entry = get_platform(key)
    if entry.support == SupportLevel.PLANNED or not entry.adapter:
        raise NotImplementedError(f"{entry.name} is planned and has no adapter yet ({entry.notes})")
    module, cls = entry.adapter.split(":")
    return getattr(importlib.import_module(module), cls)(config)


def register(entry: PlatformEntry) -> None:
    """Plug-in hook: third-party adapters can register without editing this file."""
    PLATFORMS.append(entry)
    _BY_KEY[entry.key] = entry
