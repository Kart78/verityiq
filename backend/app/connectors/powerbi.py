"""Microsoft Power BI adapter.

Discovery
  * Semantic model (tables, columns, measures/DAX, relationships) and report visuals from a
    PBIP project folder (TMDL + PBIR) - works offline from source control.
  * Workspaces, datasets, reports, refresh history from the Power BI REST API.
Actual values
  * POST /groups/{ws}/datasets/{ds}/executeQueries with generated DAX.
Auth
  * Service principal (client credentials). Secret resolved server-side from the secret store.

Not yet supported (reported via ModelMetadata.unsupported): model metadata without PBIP
(needs XMLA/TOM), RLS impersonation tests, visual rendering/interaction.
"""
from __future__ import annotations

from app.core.names import unbracket

import time
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests

from ..core.environment import FieldMapping
from ..core.models import FilterSpec, ModelMetadata, RunMode
from ..metadata.pbip import read_pbip
from ..core.environment import resolve_path
from .base import BIConnector, Capability, ConnectionTestResult, ConnectorError, QueryResult

API = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"


def dax_literal(value: Any, data_type: Optional[str] = None) -> str:
    if value is None:
        return "BLANK()"
    if isinstance(value, bool):
        return "TRUE()" if value else "FALSE()"
    if isinstance(value, (int, float)):
        return repr(value)
    if data_type in ("date", "datetime") or isinstance(value, (date, datetime)):
        d = value if isinstance(value, (date, datetime)) else date.fromisoformat(str(value)[:10])
        return f"DATE({d.year}, {d.month}, {d.day})"
    return '"' + str(value).replace('"', '""') + '"'


def dax_predicate(fm: FieldMapping, f: FilterSpec) -> str:
    col = fm.model
    val = fm.model_value_map.get(f.value, f.value) if not isinstance(f.value, list) else [fm.model_value_map.get(v, v) for v in f.value]
    if f.op == "is null":
        return f"ISBLANK({col})"
    if f.op == "is not null":
        return f"NOT ISBLANK({col})"
    if f.op in ("in", "not in"):
        vals = val if isinstance(val, list) else [val]
        expr = f"{col} IN {{{', '.join(dax_literal(v, fm.data_type) for v in vals)}}}"
        return f"NOT ({expr})" if f.op == "not in" else expr
    op = "<>" if f.op == "!=" else f.op
    return f"{col} {op} {dax_literal(val, fm.data_type)}"


def build_dax(measure: str, filters: list[tuple[FieldMapping, FilterSpec]], grain: Optional[FieldMapping]) -> str:
    if measure.startswith("="):                      # an expression (implicit aggregation), not a measure name
        m = measure[1:]
    else:
        name = measure[1:-1] if measure.startswith("[") and measure.endswith("]") else measure
        m = "[" + name.replace("]", "]]") + "]"
    preds = [f"KEEPFILTERS({dax_predicate(fm, f)})" for fm, f in filters]
    if grain is None:
        inner = f"CALCULATE({m}{''.join(', ' + p for p in preds)})"
        return f'EVALUATE\nROW("Value", {inner})'
    table = f'SUMMARIZECOLUMNS({grain.model}, "Value", {m})'
    if preds:
        table = f"CALCULATETABLE(\n    {table},\n    " + ",\n    ".join(preds) + "\n)"
    return f"EVALUATE\n{table}\nORDER BY {grain.model}"


class PowerBIConnector(BIConnector):
    platform = "powerbi"
    expression_language = "dax"
    mode = RunMode.LIVE

    def __init__(self, config: dict, session: Optional[requests.Session] = None):
        super().__init__(config)
        self.session = session or requests.Session()
        self._token: Optional[str] = None
        self._token_exp = 0.0

    # -------------------------------------------------- capabilities
    def _has_creds(self) -> bool:
        return all(self.config.get(k) for k in ("tenant_id", "client_id", "client_secret"))

    def capabilities(self) -> set[str]:
        caps = set()
        if self.config.get("pbip_path"):
            caps |= {Capability.READ_MODEL, Capability.READ_VISUALS}
        if self._has_creds():
            caps |= {Capability.LIST_WORKSPACES, Capability.LIST_REPORTS, Capability.REFRESH_HISTORY}
            if self.config.get("workspace_id") and self.config.get("dataset_id"):
                caps |= {Capability.QUERY_MEASURE, Capability.QUERY_GRAIN}
        return caps

    # -------------------------------------------------- REST
    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.session.post(
            f"https://login.microsoftonline.com/{self.config['tenant_id']}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self.config["client_id"],
                  "client_secret": self.config["client_secret"], "scope": SCOPE}, timeout=30)
        if r.status_code != 200:
            raise ConnectorError(f"Azure AD token request failed ({r.status_code}): {r.text[:200]}")
        body = r.json()
        self._token, self._token_exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _get(self, path: str) -> dict:
        r = self.session.get(f"{self.config.get('api_base', API)}{path}",
                             headers={"Authorization": f"Bearer {self._auth()}"}, timeout=60)
        if r.status_code != 200:
            raise ConnectorError(f"GET {path} failed ({r.status_code}): {r.text[:200]}")
        return r.json()

    def test_connection(self) -> ConnectionTestResult:
        if not self._has_creds():
            if self.config.get("pbip_path"):
                ok = resolve_path(self.config["pbip_path"]).exists()
                return ConnectionTestResult(ok, "PBIP folder found (offline metadata only - no service credentials)"
                                            if ok else "PBIP folder not found", RunMode.LIVE)
            return ConnectionTestResult(False, "No credentials and no PBIP path configured", RunMode.LIVE)
        try:
            ws = self.config.get("workspace_id")
            data = self._get(f"/groups/{ws}/datasets" if ws else "/groups")
            return ConnectionTestResult(True, f"Authenticated; {len(data.get('value', []))} items visible", RunMode.LIVE)
        except Exception as e:  # noqa: BLE001
            return ConnectionTestResult(False, str(e), RunMode.LIVE)

    # -------------------------------------------------- discovery
    def discover(self) -> ModelMetadata:
        if self.supports(Capability.READ_MODEL):
            meta = read_pbip(resolve_path(self.config["pbip_path"]))
        else:
            meta = ModelMetadata(platform=self.platform, model_name=self.config.get("dataset_id", "unknown"))
            meta.unsupported.append("Semantic model metadata without a PBIP folder requires the XMLA endpoint (planned).")
        if self._has_creds() and self.config.get("workspace_id"):
            ws = self.config["workspace_id"]
            try:
                meta.reports = [{"id": r["id"], "name": r["name"], "dataset_id": r.get("datasetId")}
                                for r in self._get(f"/groups/{ws}/reports").get("value", [])] or meta.reports
                if self.config.get("dataset_id"):
                    hist = self._get(f"/groups/{ws}/datasets/{self.config['dataset_id']}/refreshes?$top=10").get("value", [])
                    meta.reports.append({"refresh_history": hist})
            except ConnectorError as e:
                meta.unsupported.append(f"REST discovery failed: {e}")
        else:
            meta.unsupported.append("Workspace/report/refresh discovery needs service-principal credentials.")
        meta.unsupported.append("Row-level security role testing (impersonation) - Phase 3.")
        meta.unsupported.append("Visual rendering, slicer clicks and drill-through - Phase 2 (UI automation).")
        meta.discovered_at = datetime.now(timezone.utc).isoformat()
        return meta

    # -------------------------------------------------- queries
    def build_measure_query(self, measure, filters, grain=None) -> str:
        return build_dax(measure, filters, grain)

    def execute_query(self, query: str) -> QueryResult:
        self.require(Capability.QUERY_MEASURE)
        ws, ds = self.config["workspace_id"], self.config["dataset_id"]
        t0 = time.time()
        payload: dict = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
        if self.config.get("impersonated_user"):
            payload["impersonatedUserName"] = self.config["impersonated_user"]
        for attempt in range(4):
            r = self.session.post(f"{self.config.get('api_base', API)}/groups/{ws}/datasets/{ds}/executeQueries",
                                  json=payload, headers={"Authorization": f"Bearer {self._auth()}"}, timeout=120)
            if r.status_code in (429, 502, 503, 504) and attempt < 3:
                wait = r.headers.get("Retry-After") if hasattr(r, "headers") and r.headers else None
                time.sleep(min(float(wait) if wait and str(wait).isdigit() else 2 ** attempt, 30))
                continue
            break
        if r.status_code != 200:
            detail = r.text[:400]
            try:
                err = r.json().get("error", {})
                detail = err.get("pbi.error", {}).get("details", [{}])[0].get("detail", {}).get("value") or err.get("message") or detail
            except Exception:  # noqa: BLE001
                pass
            raise ConnectorError(f"executeQueries failed ({r.status_code}): {detail}")
        body = r.json()
        res = body.get("results", [{}])[0]
        if res.get("error"):
            raise ConnectorError(f"DAX query error: {res['error']}")
        tables = res.get("tables") or [{}]
        raw = tables[0].get("rows", [])
        rows = []
        for row in raw:
            norm = {}
            for k, v in row.items():
                norm["value" if unbracket(k).lower() == "value" else "grain"] = v
            rows.append(norm)
        return QueryResult(rows=rows, query_text=query, language="dax", connector=self.platform, mode=RunMode.LIVE,
                           elapsed_ms=int((time.time() - t0) * 1000), extra={"raw_rows": raw[:50]})
