"""Databricks SQL warehouse adapter (Unity Catalog).

Requires `pip install databricks-sql-connector`. Uses named parameters (:p0 ...) supported
by databricks-sql-connector >= 3.0. Every statement passes the read-only guard first.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from ..core.models import ColumnMeta, RunMode, SourceTableMeta
from ..engine.sql_safety import assert_read_only
from .base import Capability, ConnectionTestResult, ConnectorError, DataConnector, QueryResult


class DatabricksConnector(DataConnector):
    platform = "databricks"
    sql_dialect = "databricks"
    mode = RunMode.LIVE

    def __init__(self, config: dict, connect_fn=None):
        super().__init__(config)
        self._connect_fn = connect_fn

    def capabilities(self) -> set[str]:
        return {Capability.LIST_SCHEMAS, Capability.LIST_TABLES, Capability.DESCRIBE_TABLE,
                Capability.EXECUTE_SQL, Capability.TABLE_STATS}

    # ------------------------------------------------------------ authentication
    AZURE_DATABRICKS_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"   # fixed Azure Databricks resource id

    def _host(self) -> str:
        return re.sub(r"^https?://", "", str(self.config["server_hostname"]).strip()).split("/")[0]

    def auth_mode(self) -> str:
        if self.config.get("client_id") and self.config.get("client_secret"):
            return "entra-sp" if self.config.get("tenant_id") else "databricks-sp"
        return "pat" if self.config.get("access_token") else "none"

    def _token(self) -> str:
        """Access token for the SQL warehouse. Service principals are exchanged automatically and refreshed."""
        mode = self.auth_mode()
        if mode == "pat":
            return str(self.config["access_token"]).strip()
        if mode == "none":
            raise ConnectorError("No credentials: enter a client ID + client secret (service principal) or an access token in Settings")
        if getattr(self, "_tok", None) and time.time() < self._tok_exp - 120:
            return self._tok
        import requests
        cid, sec = str(self.config["client_id"]).strip(), str(self.config["client_secret"]).strip()
        if mode == "databricks-sp":
            r = requests.post(f"https://{self._host()}/oidc/v1/token", auth=(cid, sec),
                              data={"grant_type": "client_credentials", "scope": "all-apis"}, timeout=30)
            where = "Databricks OAuth (workspace-managed secret)"
        else:
            r = requests.post(f"https://login.microsoftonline.com/{str(self.config['tenant_id']).strip()}/oauth2/v2.0/token",
                              data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec,
                                    "scope": self.AZURE_DATABRICKS_SCOPE}, timeout=30)
            where = "Microsoft Entra ID (Azure-managed secret)"
        if r.status_code != 200:
            detail = r.text[:300]
            try:
                j = r.json()
                detail = j.get("error_description") or j.get("error") or detail
            except ValueError:
                pass
            other = ("If the secret was created in Azure (Entra ID), also fill Tenant ID." if mode == "databricks-sp"
                     else "If the secret was created inside Databricks (starts with 'dose'), clear Tenant ID.")
            raise ConnectorError(f"Service principal login failed via {where} ({r.status_code}): {detail}. {other}")
        body = r.json()
        self._tok, self._tok_exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._tok

    def _connect(self):
        if self._connect_fn:
            return self._connect_fn()
        try:
            from databricks import sql as dbsql  # type: ignore
        except ImportError as e:
            raise ConnectorError("databricks-sql-connector is not installed: run  .\\.venv\\Scripts\\python.exe -m pip install "
                                 "databricks-sql-connector  and restart the app") from e
        try:                       # corporate TLS inspection: trust the Windows/macOS certificate store if available
            import truststore  # type: ignore
            truststore.inject_into_ssl()
        except Exception:  # noqa: BLE001
            pass
        kwargs = dict(server_hostname=self._host(), http_path=str(self.config["http_path"]).strip(),
                      access_token=self._token(), _socket_timeout=300)
        if self.config.get("catalog"):
            kwargs["catalog"] = self.config["catalog"]   # two-part names (schema.table) resolve in this catalog
        return dbsql.connect(**kwargs)

    def _run(self, sql: str, params: Optional[dict[str, Any]] = None) -> tuple[list[dict], int]:
        t0 = time.time()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params or None)
            cols = [d[0] for d in cur.description or []]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows, int((time.time() - t0) * 1000)

    def test_connection(self) -> ConnectionTestResult:
        try:
            rows, ms = self._run("SELECT current_catalog() AS catalog, current_user() AS usr")
            return ConnectionTestResult(True, f"Connected as {rows[0]['usr']} via {self.auth_mode()} "
                                              f"(catalog {rows[0]['catalog']}, {ms} ms)", RunMode.LIVE)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            hint = ("client ID or secret rejected" if "Service principal login failed" in msg else
                    "token invalid/expired" if re.search(r"401|403|invalid access token|Unauthorized", msg, re.I) else
                    "table/catalog not found - set Default catalog" if "TABLE_OR_VIEW_NOT_FOUND" in msg else
                    "HTTP path / warehouse not found" if re.search(r"404|RESOURCE_DOES_NOT_EXIST", msg, re.I) else
                    "TLS certificate not trusted - pip install truststore" if re.search(r"SSL|CERTIFICATE", msg, re.I) else
                    "timed out - warehouse starting or proxy blocking" if re.search(r"timed out|timeout", msg, re.I) else "")
            return ConnectionTestResult(False, (hint + " | " if hint else "") + msg[:300] +
                                        "  (run scripts\\check_databricks.py for a step-by-step diagnosis)", RunMode.LIVE)

    def list_tables(self, catalog=None, schema=None) -> list[SourceTableMeta]:
        catalog = catalog or self.config.get("catalog")
        where, params = [], {}
        if schema:
            where.append("table_schema = :schema")
            params["schema"] = schema
        sql = (f"SELECT table_catalog, table_schema, table_name, table_type FROM {catalog}.information_schema.tables"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY 2, 3 LIMIT 2000")
        rows, _ = self._run(sql, params)
        return [SourceTableMeta(catalog=r["table_catalog"], schema_name=r["table_schema"], name=r["table_name"],
                                table_type=r["table_type"]) for r in rows]

    def describe_table(self, fqn: str) -> SourceTableMeta:
        """Column names and physical types for a 1-, 2- or 3-part name (resolved like the queries)."""
        from ..core.environment import quote_fqn
        rows, _ = self._run(f"DESCRIBE TABLE {quote_fqn(fqn)}")
        cols = []
        for r in rows:
            name = r.get("col_name") or ""
            if not name or name.startswith("#"):
                break                                   # partition / detail sections follow
            cols.append(ColumnMeta(name=name, data_type=r.get("data_type")))
        parts = fqn.split(".")
        return SourceTableMeta(catalog=parts[0] if len(parts) == 3 else None, schema_name=parts[-2] if len(parts) > 1 else None,
                               name=parts[-1], columns=cols)

    def sample_values(self, fqn: str, column: str, n: int = 200) -> list:
        from ..core.environment import quote_fqn, quote_ident
        rows, _ = self._run(f"SELECT DISTINCT {quote_ident(column)} AS v FROM {quote_fqn(fqn)} "
                            f"WHERE {quote_ident(column)} IS NOT NULL LIMIT {int(n)}")
        return [r["v"] for r in rows]

    def execute_sql(self, sql: str, params=None) -> QueryResult:
        assert_read_only(sql, self.sql_dialect)
        body = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--")).strip()   # comments are for people
        rows, ms = self._run(body, params)
        return QueryResult(rows=rows, query_text=sql, language="sql", connector=self.platform, mode=RunMode.LIVE,
                           elapsed_ms=ms, extra={"params": params or {}})
