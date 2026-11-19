"""Common connector interfaces.

The validation engine only ever talks to these abstract classes. A new platform
is added by writing an adapter that subclasses one of them and registering it in
connectors/registry.py - the engine does not change.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.environment import FieldMapping
from ..core.models import FilterSpec, ModelMetadata, RunMode, SourceTableMeta


class Capability:
    # BI
    LIST_WORKSPACES = "list_workspaces"
    LIST_REPORTS = "list_reports"
    READ_MODEL = "read_semantic_model"
    READ_VISUALS = "read_visuals"
    QUERY_MEASURE = "query_measure"
    QUERY_GRAIN = "query_measure_by_grain"
    REFRESH_HISTORY = "refresh_history"
    SECURITY_ROLES = "security_roles"
    # data
    LIST_SCHEMAS = "list_schemas"
    LIST_TABLES = "list_tables"
    DESCRIBE_TABLE = "describe_table"
    TABLE_STATS = "table_statistics"
    EXECUTE_SQL = "execute_sql"


class ConnectorError(Exception):
    pass


class UnsupportedCapability(ConnectorError):
    def __init__(self, platform: str, capability: str):
        super().__init__(f"{platform} adapter does not support '{capability}'")
        self.platform, self.capability = platform, capability


@dataclass
class QueryResult:
    rows: list[dict]
    query_text: str
    language: str                    # sql | dax | ...
    connector: str
    mode: RunMode
    elapsed_ms: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class ConnectionTestResult:
    ok: bool
    message: str
    mode: RunMode
    details: dict = field(default_factory=dict)


class BaseConnector(abc.ABC):
    platform: str = "base"
    mode: RunMode = RunMode.LIVE

    def __init__(self, config: dict):
        self.config = config

    @abc.abstractmethod
    def capabilities(self) -> set[str]: ...

    def supports(self, cap: str) -> bool:
        return cap in self.capabilities()

    def require(self, cap: str) -> None:
        if not self.supports(cap):
            raise UnsupportedCapability(self.platform, cap)

    @abc.abstractmethod
    def test_connection(self) -> ConnectionTestResult: ...


class BIConnector(BaseConnector):
    """Adapter for a BI platform (Power BI, Tableau, Looker, ...)."""

    expression_language: str = "unknown"

    @abc.abstractmethod
    def discover(self) -> ModelMetadata: ...

    @abc.abstractmethod
    def build_measure_query(self, measure: str, filters: list[tuple["FieldMapping", FilterSpec]],
                            grain: Optional["FieldMapping"] = None) -> str:
        """Translate (measure, [(field mapping, filter)], grain) into the platform query language."""

    @abc.abstractmethod
    def execute_query(self, query: str) -> QueryResult: ...

    def query_measure(self, measure: str, filters: list[tuple["FieldMapping", FilterSpec]],
                      grain: Optional["FieldMapping"] = None) -> QueryResult:
        """Return rows of {"value": x} or {"grain": g, "value": x}. Adapters normalise column names."""
        self.require(Capability.QUERY_GRAIN if grain else Capability.QUERY_MEASURE)
        return self.execute_query(self.build_measure_query(measure, filters, grain))


class DataConnector(BaseConnector):
    """Adapter for an authoritative source (Databricks, Snowflake, SQL Server, ...)."""

    sql_dialect: str = "ansi"   # sqlglot dialect name

    @abc.abstractmethod
    def list_tables(self, catalog: Optional[str] = None, schema: Optional[str] = None) -> list[SourceTableMeta]: ...

    @abc.abstractmethod
    def describe_table(self, fqn: str) -> SourceTableMeta: ...

    @abc.abstractmethod
    def execute_sql(self, sql: str, params: Optional[dict[str, Any]] = None) -> QueryResult:
        """Must only execute statements that passed engine.sql_safety.assert_read_only."""
