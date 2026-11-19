"""Environment configuration: which BI + data platform, and how logical fields map to each.

A new customer environment is a YAML file - no code change. Secrets are referenced as
${ENV_VAR} and resolved only server-side at connect time (never returned by the API).
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from .models import FilterSpec, KPIDefinition, Requirement, RunMode

ROOT = Path(__file__).resolve().parents[3]          # project root


class PlatformBinding(BaseModel):
    platform: str
    config: dict = Field(default_factory=dict)


class TableMapping(BaseModel):
    source: Optional[str] = None        # physical source table (catalog.schema.table) or "(SELECT ...)"
    calculated: bool = False            # DAX calculated table (e.g. CALENDAR) - resolved through relationships
    model: Optional[str] = None         # BI model table name
    primary_key: Optional[str] = None   # logical->source column name
    freshness_column: Optional[str] = None
    file: Optional[dict] = None         # file-backed table: {format, file, path, item, item_kind, header, skip}


class FieldMapping(BaseModel):
    table: str                          # logical table
    source: Optional[str] = None        # source column name
    source_expr: Optional[str] = None   # or a source SQL expression using {t} as table alias
    model: Optional[str] = None         # BI column reference e.g. "'Date'[Month]"
    data_type: Optional[str] = None
    sample_values: list[Any] = Field(default_factory=list)
    model_value_map: dict = Field(default_factory=dict)  # source value -> BI value when encodings differ
    key_expr: Optional[str] = None      # virtual-table field: SQL over the related key, e.g. "date_format({k}, 'yyyy-MM')"
    source_type: Optional[str] = None   # physical type discovered in the source (e.g. string, date, timestamp, decimal(18,2))
    source_format: Optional[str] = None # detected text format when a date is stored as text, e.g. MM/dd/yyyy

    def source_sql(self, alias: str, raw: bool = False) -> str:
        """SQL for this field. Converts text-stored dates/numbers to the type the BI model uses, the same
        way Power Query's 'Changed Type' step does, so comparisons, MAX and DATEDIFF behave correctly."""
        if self.source_expr:
            return self.source_expr.replace("{t}", alias)
        col = f"{alias}.{quote_ident(self.source)}"
        if raw or not self.source_type:
            return col
        st = self.source_type.lower()
        textual = st.startswith(("string", "varchar", "char", "nvarchar", "nchar", "text", "character", "varchar2", "nvarchar2", "clob"))
        if textual and self.data_type == "date":
            if self.source_format and re.search(r"[Hhms]", self.source_format):
                return f"TRY_TO_TIMESTAMP({col}, '{self.source_format}')"
            return f"TRY_TO_DATE({col}, '{self.source_format}')" if self.source_format else f"TRY_TO_DATE({col})"
        if textual and self.data_type in ("number", "integer"):
            return f"TRY_CAST({col} AS {'BIGINT' if self.data_type == 'integer' else 'DECIMAL(38, 10)'})"
        return col


def quote_ident(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or "") else "`" + str(name).replace("`", "``") + "`"


def quote_fqn(name: str) -> str:
    """catalog.schema.table -> each part quoted when needed; '(SELECT ...)' native queries pass through."""
    n = (name or "").strip()
    if n.startswith("("):
        return n
    parts, cur, q = [], "", None
    for ch in n:
        if q:
            if ch == q:
                q = None
            else:
                cur += ch
        elif ch in "`\"[":
            q = "]" if ch == "[" else ch
        elif ch == ".":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return ".".join(quote_ident(p.strip()) for p in parts if p.strip())


def sql_alias(name: str) -> str:
    return "t_" + re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


class JoinMapping(BaseModel):
    left: str
    right: str
    left_key: str
    right_key: str


class Mapping(BaseModel):
    tables: dict[str, TableMapping] = Field(default_factory=dict)
    fields: dict[str, FieldMapping] = Field(default_factory=dict)
    joins: list[JoinMapping] = Field(default_factory=list)


class DefaultContext(BaseModel):
    label: str = "All data"
    relative_days: Optional[int] = None    # e.g. 365 -> "date_field >= today-365"

    def filters_for(self, kpi: KPIDefinition, as_of: Optional[date] = None) -> list[FilterSpec]:
        if self.relative_days and kpi.date_field:
            start = (as_of or date.today()) - timedelta(days=self.relative_days)
            return [FilterSpec(field=kpi.date_field, op=">=", value=start.isoformat())]
        return []


class EnvironmentConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    mode: RunMode = RunMode.LIVE
    bi: PlatformBinding
    data: PlatformBinding
    kpi_file: Optional[str] = None       # projects keep KPIs in the repository instead
    project: bool = False                # created from an uploaded PBIP
    source_platform_detected: Optional[str] = None
    requirements_file: Optional[str] = None
    mapping: Mapping = Field(default_factory=Mapping)
    default_context: DefaultContext = Field(default_factory=DefaultContext)
    sample: bool = False                 # sample config shipped with the product

    def public_dict(self) -> dict:
        """Safe for the browser: platform config values that look like secrets are masked."""
        d = self.model_dump(mode="json")
        for side in ("bi", "data"):
            d[side]["config"] = {k: ("(saved)" if _looks_secret(k, v) else v) for k, v in d[side]["config"].items()}
        return d


def _looks_secret(key: str, value: Any) -> bool:
    return bool(re.search(r"secret|password|passphrase|token", key, re.I)) or (isinstance(value, str) and "${secret:" in value)


_ENV_RE = re.compile(r"\$\{(secret:[\w.\-]+|[A-Za-z0-9_]+)\}")


def resolve_secrets(config: dict) -> dict:
    """Replace ${VAR} with environment / secret-manager values. Server-side only."""
    out = {}
    for k, v in config.items():
        if isinstance(v, str):
            def sub(m):
                ref = m.group(1)
                if ref.startswith("secret:"):
                    from .secrets import get_secret
                    val = get_secret(ref[7:])
                    if val is None:
                        raise KeyError(f"Saved secret '{ref[7:]}' not found - enter it again in Settings")
                    return val
                val = os.environ.get(ref)
                if val is None:
                    raise KeyError(f"Environment variable {ref} is not set in the window that started the app")
                return val
            out[k] = _ENV_RE.sub(sub, v)
        else:
            out[k] = v
    return out


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def load_environment(path: Path) -> EnvironmentConfig:
    return EnvironmentConfig(**yaml.safe_load(path.read_text()))


PROJECTS_DIR = Path(os.environ.get("BIQA_PROJECTS", ROOT / "data" / "projects"))


def load_environments(directory: Optional[Path] = None) -> dict[str, EnvironmentConfig]:
    directory = directory or ROOT / "config" / "environments"
    envs = {}
    files = sorted(directory.glob("*.yaml")) + sorted(PROJECTS_DIR.glob("*/environment.yaml"))
    for f in files:
        e = load_environment(f)
        envs[e.id] = e
    return envs


def save_project_environment(env: EnvironmentConfig) -> Path:
    d = PROJECTS_DIR / env.id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "environment.yaml"
    p.write_text(yaml.safe_dump(env.model_dump(mode="json"), sort_keys=False))
    return p


def load_kpis(env: EnvironmentConfig) -> list[KPIDefinition]:
    if not env.kpi_file:
        return []
    data = yaml.safe_load(resolve_path(env.kpi_file).read_text())
    return [KPIDefinition(**k) for k in data.get("kpis", [])]


def load_requirements(env: EnvironmentConfig) -> list[Requirement]:
    if not env.requirements_file:
        return []
    data = yaml.safe_load(resolve_path(env.requirements_file).read_text())
    return [Requirement(**r) for r in data.get("requirements", [])]
