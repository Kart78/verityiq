"""Platform-neutral domain model.

Nothing in this module knows about Power BI, Databricks or any customer.
Platform specifics live in connectors/; customer specifics live in config/.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .names import unbracket


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_VALIDATED = "NOT VALIDATED"
    ERROR = "ERROR"
    NOT_EXECUTED = "NOT EXECUTED"


class SupportLevel(str, Enum):
    IMPLEMENTED = "implemented"   # code path exists and is exercised by tests
    PARTIAL = "partial"           # some capabilities implemented, others reported unsupported
    PLANNED = "planned"           # registry entry only - cannot be connected
    DEMO = "demo"                 # sample/fixture adapter, never a real platform


class RunMode(str, Enum):
    LIVE = "LIVE"
    DEMO = "DEMO"
    MANUAL = "MANUAL"     # values entered by a tester (e.g. SQL run in Databricks, value read from report)


# --------------------------------------------------------------------------- KPI registry

class FilterSpec(BaseModel):
    """A filter expressed once, in business terms, then translated for each side."""
    field: str                         # logical field name, e.g. "case_status"
    op: Literal["=", "!=", "in", "not in", ">", ">=", "<", "<=", "is null", "is not null"] = "="
    value: Any = None


class MeasureSpec(BaseModel):
    """Source-side definition of an aggregation over one table."""
    table: str                          # logical table name (mapped to physical in env config)
    aggregation: Literal["SUM", "COUNT", "COUNT_DISTINCT", "AVG", "MIN", "MAX", "COUNT_ROWS"]
    column: Optional[str] = None
    filters: list[FilterSpec] = Field(default_factory=list)
    column_expr: Optional[str] = None   # arithmetic over fields, e.g. "({Sales.Qty} * {Sales.Price})" (iterators)
    per_field: Optional[str] = None     # AVERAGEX(VALUES(per_field), agg): aggregate per member first...
    per_aggregation: Optional[str] = None  # ...then combine members with this (AVG/SUM/MIN/MAX/COUNT)


class Tolerance(BaseModel):
    type: Literal["exact", "absolute", "percent"] = "exact"
    value: float = 0.0
    rounding: Optional[int] = None       # decimal places applied to both sides before compare
    null_handling: Literal["null_equals_null", "null_is_zero", "null_fails"] = "null_fails"


class BIBinding(BaseModel):
    """How the KPI is exposed in a BI semantic model. Platform-neutral fields."""
    measure: str                          # e.g. "Total Customers" (DAX), "implicit:Sales[Amount]" for a column on a visual
    expression: Optional[str] = None      # "=SUM('Sales'[Amount])" when there is no named measure
    model_table: Optional[str] = None
    expression_language: Optional[str] = None   # dax | tableau | lookml | sql | cognos


class KPIDefinition(BaseModel):
    id: str
    name: str
    business_definition: str
    owner: Optional[str] = None
    kind: Literal["simple", "ratio", "formula", "freshness"] = "simple"
    unit: Literal["count", "percent", "currency", "minutes", "score", "days", "number"] = "number"
    currency: Optional[str] = None
    measure: Optional[MeasureSpec] = None          # simple
    numerator: Optional[MeasureSpec] = None        # ratio
    denominator: Optional[MeasureSpec] = None      # ratio
    multiplier: float = 1.0                        # e.g. 100 for percent stored 0-100
    formula: Optional[dict] = None                 # formula: {"op","l","r"} | {"leaf": i} | {"const": n}
    components: list[MeasureSpec] = Field(default_factory=list)
    date_field: Optional[str] = None
    grains: list[str] = Field(default_factory=list)  # logical fields valid for breakdowns
    tolerance: Tolerance = Field(default_factory=Tolerance)
    bi: Optional[BIBinding] = None
    requirement_ids: list[str] = Field(default_factory=list)
    approval_status: Literal["DRAFT", "APPROVED", "REJECTED"] = "DRAFT"
    approved_by: Optional[str] = None
    # where the definition came from: "business" (supplied/approved by business) or
    # "inferred" (machine-translated from the DAX - NOT independent of the measure under test)
    basis: Literal["business", "inferred"] = "business"
    expected_sql: Optional[str] = None          # optional hand-written source SQL (returns expected_value)
    translation_notes: list[str] = Field(default_factory=list)
    # freshness KPIs
    freshness_field: Optional[str] = None
    max_age_days: Optional[float] = None
    notes: Optional[str] = None

    def all_specs(self) -> list["MeasureSpec"]:
        return [x for x in (self.measure, self.numerator, self.denominator) if x] + list(self.components)

    def is_independently_defined(self) -> tuple[bool, str]:
        """Can the expected value be computed from source without the BI tool?"""
        if self.expected_sql and self.approval_status == "APPROVED":
            return True, ""
        if self.kind == "simple" and self.measure is None:
            return False, "No source-side measure definition registered."
        if self.kind == "formula" and (not self.formula or not self.components):
            return False, "Formula KPI is missing its components."
        if self.kind == "ratio" and (self.numerator is None or self.denominator is None):
            return False, "Ratio KPI is missing numerator or denominator definition."
        if self.kind == "freshness" and (not self.freshness_field or self.max_age_days is None):
            return False, "Freshness KPI needs freshness_field and max_age_days."
        if self.expected_sql and self.approval_status == "APPROVED":
            return True, ""
        if self.approval_status != "APPROVED":
            return False, f"Business definition is {self.approval_status}; only APPROVED definitions can be used as expected-value logic."
        return True, ""


class Requirement(BaseModel):
    id: str
    title: str
    description: str
    kpi_ids: list[str] = Field(default_factory=list)
    visual_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- metadata

class ColumnMeta(BaseModel):
    name: str
    data_type: Optional[str] = None
    nullable: Optional[bool] = None
    expression: Optional[str] = None
    source_column: Optional[str] = None      # column name as Power Query outputs it (TMDL sourceColumn)
    physical_column: Optional[str] = None    # name in the source table, after undoing Power Query renames
    pq_computed: bool = False                # column added in Power Query (no physical source column)
    pq_sql: Optional[str] = None             # SQL equivalent of a Power Query added/transformed column ({t} = table alias)
    sort_by: Optional[str] = None            # sortByColumn
    hidden: bool = False


class MeasureMeta(BaseModel):
    name: str
    table: str
    expression: str
    language: str
    format_string: Optional[str] = None
    source_file: Optional[str] = None
    hidden: bool = False


class TableMeta(BaseModel):
    name: str
    columns: list[ColumnMeta] = Field(default_factory=list)
    measures: list[MeasureMeta] = Field(default_factory=list)
    source: Optional[str] = None          # raw partition expression
    source_ref: Optional[str] = None      # parsed physical object, e.g. main.sales.orders
    source_kind: Optional[str] = None     # databricks | sqlserver | snowflake | excel | csv | native_query | calculated | unknown
    file_source: Optional[dict] = None    # Excel/CSV/Parquet: {format, file, path, item, item_kind, header, skip}
    is_date_table: bool = False
    hidden: bool = False
    hierarchies: list[dict] = Field(default_factory=list)   # {"name", "levels": [{"name", "column"}]}


class RelationshipMeta(BaseModel):
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: Optional[str] = None
    cross_filter: Optional[str] = None
    active: bool = True


class VisualMeta(BaseModel):
    id: str
    page: str
    visual_type: str
    title: Optional[str] = None
    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)   # "Table[Column]"
    filters: list[str] = Field(default_factory=list)
    filter_specs: list[dict] = Field(default_factory=list)  # {"ref","op","value","level"}: report/page/visual/slicer filters in force
    filter_notes: list[str] = Field(default_factory=list)   # filters that could not be translated (e.g. on measures)
    implicit_aggs: dict = Field(default_factory=dict)       # "implicit:T[C]" -> SUM/AVG/COUNT/MIN/MAX
    page_hidden: bool = False
    page_id: Optional[str] = None
    interactions: dict = Field(default_factory=dict)        # as source: target visual id -> DataFilter | HighlightFilter | NoFilter | Default
    sync_group: Optional[str] = None                        # slicers synced across pages share a group
    selection: list = Field(default_factory=list)           # slicer: saved selected values
    refs: list[str] = Field(default_factory=list)           # every field reference in the visual: m||Name, c|T|Col, h|T|Hier|Level
    report: Optional[str] = None                            # set for visuals of additional reports on the same model


SLICER_HINT = "slicer"


def is_slicer(visual_type: Optional[str]) -> bool:
    """slicer, advancedSlicerVisual (button slicer), listSlicer, textSlicer, chicletSlicer..."""
    return SLICER_HINT in (visual_type or "").lower()


class ModelMetadata(BaseModel):
    platform: str
    model_name: str
    tables: list[TableMeta] = Field(default_factory=list)
    relationships: list[RelationshipMeta] = Field(default_factory=list)
    visuals: list[VisualMeta] = Field(default_factory=list)
    reports: list[dict] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)  # capabilities the adapter could not provide
    roles: list[dict] = Field(default_factory=list)       # RLS: {"role", "table", "expression"}
    report_refs: list[dict] = Field(default_factory=list) # field references in report/page filters: {"ref", "where"}
    discovered_at: Optional[str] = None
    mode: RunMode = RunMode.LIVE

    def measure(self, name: str) -> Optional[MeasureMeta]:
        key = unbracket(name).lower()
        for t in self.tables:
            for m in t.measures:
                if m.name.lower() == key:
                    return m
        return None

    def specs(self) -> list:  # pragma: no cover - convenience
        return []

    def all_measures(self) -> list[MeasureMeta]:
        return [m for t in self.tables for m in t.measures]


class SourceTableMeta(BaseModel):
    catalog: Optional[str] = None
    schema_name: Optional[str] = None
    name: str
    table_type: Optional[str] = None
    columns: list[ColumnMeta] = Field(default_factory=list)
    row_count: Optional[int] = None


# --------------------------------------------------------------------------- tests & results

class TestCase(BaseModel):
    __test__ = False  # not a pytest class
    id: str
    name: str
    business_objective: str
    display_id: Optional[str] = None
    category: Literal["KPI", "Data Quality", "Visual", "Semantic Model", "Expression", "Functional", "Security", "Performance", "Regression"]
    environment_id: str
    kpi_id: Optional[str] = None
    visual_id: Optional[str] = None
    preconditions: list[str] = Field(default_factory=list)
    source_mapping: str = ""
    target_mapping: str = ""
    steps: list[str] = Field(default_factory=list)
    expected_result: str = ""
    tolerance: Optional[Tolerance] = None
    filter_context: list[FilterSpec] = Field(default_factory=list)
    grain: Optional[str] = None
    executor: str                         # engine key: kpi_value | kpi_grain | dq_nulls | ...
    params: dict = Field(default_factory=dict)
    generated_by: str = "rule-engine"     # rule-engine | ai:<model> | user
    ai_explanation: str = ""
    approval_status: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING"
    approved_by: Optional[str] = None
    requirement_ids: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    kind: Literal["sql", "dax", "api", "metadata", "note", "rows"]
    label: str
    content: Any
    connector: Optional[str] = None
    mode: Optional[RunMode] = None


class TestResult(BaseModel):
    __test__ = False
    test_id: str
    run_id: str
    status: Status
    expected: Any = None
    actual: Any = None
    difference: Any = None
    message: str = ""
    severity: Optional[Literal["Critical", "High", "Medium", "Low"]] = None
    business_impact: Optional[str] = None
    root_cause_hypothesis: Optional[str] = None   # always labelled a suggestion in UI
    remediation_suggestion: Optional[str] = None
    evidence: list[Evidence] = Field(default_factory=list)
    duration_ms: int = 0
    executed_at: str = ""
    mode: RunMode = RunMode.LIVE
