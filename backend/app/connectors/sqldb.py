"""Live connectors for SQL databases and warehouses: Snowflake, SQL Server / Azure Synapse / Microsoft Fabric,
PostgreSQL, Amazon Redshift, MySQL, Oracle, Google BigQuery, Teradata, IBM DB2.

Every connector shares one flow:
  generated Databricks SQL -> read-only guard -> translated to the engine's dialect (engine/dialects.py,
  parameters inlined as literals) -> executed with the engine's official Python driver -> rows back.

Drivers are imported only when a platform is used; if one is missing the connection test says exactly
which package to install. Sessions are opened read-only wherever the engine supports it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core.models import ColumnMeta, RunMode, SourceTableMeta
from ..engine.dialects import UPPER_FOLD, to_dialect
from ..engine.sql_safety import assert_read_only
from .base import Capability, ConnectionTestResult, ConnectorError, DataConnector, QueryResult


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"                  # text | password | select | number
    placeholder: str = ""
    help: str = ""
    options: list = field(default_factory=list)     # [(value, label)]
    show_if: Optional[dict] = None       # {"auth": ["password"]}
    required: bool = False

    def public(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "placeholder": self.placeholder, "help": self.help,
                "options": [{"value": v, "label": l} for v, l in self.options], "show_if": self.show_if, "required": self.required}


_MSSQL_AUTH = [("sql", "SQL login (user + password)"), ("entra-sp", "Microsoft Entra service principal (client ID + secret)"),
               ("entra-password", "Microsoft Entra user (email + password)"), ("entra-interactive", "Microsoft Entra interactive sign-in (browser, MFA)"),
               ("windows", "Windows integrated (this PC's login)")]
_MSSQL_FIELDS = [
    Field("server", "Server", placeholder="myserver.database.windows.net", required=True,
          help="Host[,port]. Synapse: <workspace>.sql.azuresynapse.net · Fabric: the warehouse's SQL connection string"),
    Field("database", "Database", required=True),
    Field("auth", "Sign-in method", "select", options=_MSSQL_AUTH),
    Field("user", "User / email / client ID", show_if={"auth": ["sql", "entra-password", "entra-interactive", "entra-sp"]},
          help="For a service principal enter its Application (client) ID"),
    Field("password", "Password / client secret", "password", show_if={"auth": ["sql", "entra-password", "entra-sp"]}),
    Field("driver", "ODBC driver (optional)", placeholder="ODBC Driver 18 for SQL Server", help="Detected automatically"),
    Field("trust_server_certificate", "Trust server certificate", "select", options=[("no", "No (recommended)"), ("yes", "Yes (self-signed servers)")]),
]
_PG_FIELDS = lambda port: [  # noqa: E731
    Field("host", "Host", required=True), Field("port", "Port", "number", placeholder=str(port)),
    Field("database", "Database", required=True), Field("user", "User", required=True), Field("password", "Password", "password"),
    Field("sslmode", "SSL mode", "select", options=[("require", "require"), ("prefer", "prefer"), ("verify-full", "verify-full"), ("disable", "disable")]),
]


@dataclass
class Spec:
    key: str
    dialect: str
    fields: list[Field]
    driver_pkg: str                       # pip package to install when missing
    connect: Callable[[dict], Any]
    version_sql: str = "SELECT 1 AS ok"
    columns_sql: Optional[Callable[[list[str]], tuple[str, list]]] = None
    read_only: Optional[Callable[[Any], None]] = None


# ----------------------------------------------------------------------------------------- drivers

def _need(mod: str, pkg: str):
    import importlib
    try:
        return importlib.import_module(mod)
    except ImportError as e:
        raise ConnectorError(f"Driver not installed: run  pip install {pkg}  (then restart the app)") from e


def _snowflake(c: dict):
    sf = _need("snowflake.connector", "snowflake-connector-python")
    kw = {k: c[k] for k in ("account", "user", "warehouse", "database", "schema", "role") if c.get(k)}
    auth = c.get("auth") or "password"
    if auth == "password":
        kw["password"] = c.get("password")
    elif auth == "keypair":
        kw["private_key_file"] = c.get("private_key_file")
        if c.get("private_key_passphrase"):
            kw["private_key_file_pwd"] = c["private_key_passphrase"]
    elif auth == "sso":
        kw["authenticator"] = "externalbrowser"
    elif auth == "oauth":
        kw.update(authenticator="oauth", token=c.get("password"))
    kw["account"] = (kw.get("account") or "").replace("https://", "").replace(".snowflakecomputing.com", "").strip("/")
    return sf.connect(login_timeout=60, network_timeout=300, application="VerityIQ", **kw)


def _mssql(c: dict):
    pyodbc = _need("pyodbc", "pyodbc")
    drv = c.get("driver") or next((d for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")
                                   if d in pyodbc.drivers()), None)
    if not drv:
        raise ConnectorError("No SQL Server ODBC driver found - install 'Microsoft ODBC Driver 18 for SQL Server' "
                             "(https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)")
    parts = [f"DRIVER={{{drv}}}", f"SERVER={c.get('server')}", f"DATABASE={c.get('database')}", "Encrypt=yes",
             f"TrustServerCertificate={'yes' if c.get('trust_server_certificate') == 'yes' else 'no'}",
             "ApplicationIntent=ReadOnly", "Connection Timeout=60"]
    auth = c.get("auth") or "sql"
    if auth == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        mode = {"sql": None, "entra-sp": "ActiveDirectoryServicePrincipal", "entra-password": "ActiveDirectoryPassword",
                "entra-interactive": "ActiveDirectoryInteractive"}[auth]
        if mode:
            parts.append(f"Authentication={mode}")
        if c.get("user"):
            parts.append(f"UID={c['user']}")
        if c.get("password") and auth != "entra-interactive":
            parts.append("PWD={" + str(c["password"]).replace("}", "}}") + "}")
    con = pyodbc.connect(";".join(parts), autocommit=True, timeout=60)
    con.timeout = 600
    return con


def _postgres(c: dict, default_port: int = 5432):
    try:
        import psycopg  # type: ignore
        con = psycopg.connect(host=c.get("host"), port=int(c.get("port") or default_port), dbname=c.get("database"),
                              user=c.get("user"), password=c.get("password"), sslmode=c.get("sslmode") or "require",
                              connect_timeout=60, application_name="VerityIQ", autocommit=True)
        return con
    except ImportError:
        pg = _need("psycopg2", "psycopg[binary]")
        con = pg.connect(host=c.get("host"), port=int(c.get("port") or default_port), dbname=c.get("database"),
                         user=c.get("user"), password=c.get("password"), sslmode=c.get("sslmode") or "require", connect_timeout=60)
        con.autocommit = True
        return con


def _pg_read_only(con):
    try:
        cur = con.cursor()
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cur.close()
    except Exception:  # noqa: BLE001  (Redshift does not support it; the SQL guard still applies)
        pass


def _mysql(c: dict):
    pymysql = _need("pymysql", "pymysql")
    kw = dict(host=c.get("host"), port=int(c.get("port") or 3306), user=c.get("user"), password=c.get("password") or "",
              database=c.get("database"), connect_timeout=60, read_timeout=600, autocommit=True)
    if c.get("ssl") == "yes":
        kw["ssl"] = {"ssl": {}}
    return pymysql.connect(**kw)


def _mysql_read_only(con):
    try:
        cur = con.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.close()
    except Exception:  # noqa: BLE001
        pass


def _oracle(c: dict):
    oracledb = _need("oracledb", "oracledb")
    dsn = c.get("dsn") or f"{c.get('host')}:{c.get('port') or 1521}/{c.get('service_name')}"
    return oracledb.connect(user=c.get("user"), password=c.get("password"), dsn=dsn)


def _oracle_read_only(con):
    try:
        cur = con.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        cur.close()
    except Exception:  # noqa: BLE001
        pass


def _bigquery(c: dict):
    bq = _need("google.cloud.bigquery", "google-cloud-bigquery")
    from google.cloud.bigquery import dbapi  # type: ignore
    if c.get("credentials_file"):
        client = bq.Client.from_service_account_json(c["credentials_file"], project=c.get("project") or None, location=c.get("location") or None)
    else:
        client = bq.Client(project=c.get("project") or None, location=c.get("location") or None)   # gcloud application-default login
    return dbapi.connect(client)


def _teradata(c: dict):
    td = _need("teradatasql", "teradatasql")
    kw = {"host": c.get("host"), "user": c.get("user"), "password": c.get("password")}
    if c.get("logmech"):
        kw["logmech"] = c["logmech"]
    if c.get("database"):
        kw["database"] = c["database"]
    return td.connect(**kw)


def _db2(c: dict):
    dbi = _need("ibm_db_dbi", "ibm_db")
    cs = (f"DATABASE={c.get('database')};HOSTNAME={c.get('host')};PORT={c.get('port') or 50000};PROTOCOL=TCPIP;"
          f"UID={c.get('user')};PWD={c.get('password')};" + ("SECURITY=SSL;" if c.get("ssl") == "yes" else ""))
    return dbi.connect(cs, "", "")


# --------------------------------------------------------------------------- column metadata queries

def _isc(view: str, upper: bool = False):
    """information_schema.columns lookup for [catalog.]schema.table."""
    def q(parts: list[str]) -> tuple[str, list]:
        cat, sch, tbl = ([None] * (3 - len(parts)) + parts)[-3:]
        cmp = (lambda col: f"UPPER({col}) = UPPER(%s)") if upper else (lambda col: f"LOWER({col}) = LOWER(%s)")
        where = [cmp("table_name")] + ([cmp("table_schema")] if sch else [])
        frm = f"{cat}.{view}" if cat and view.startswith("information_schema") else view
        return (f"SELECT column_name, data_type, is_nullable FROM {frm} WHERE {' AND '.join(where)} ORDER BY ordinal_position",
                [tbl] + ([sch] if sch else []))
    return q


SPECS: dict[str, Spec] = {
    "snowflake": Spec("snowflake", "snowflake", [
        Field("account", "Account identifier", placeholder="xy12345.east-us-2.azure  or  myorg-myaccount", required=True,
              help="From the Snowflake URL: <account>.snowflakecomputing.com"),
        Field("user", "User", required=True),
        Field("auth", "Sign-in method", "select", options=[("password", "Password"), ("keypair", "Key pair (private key file)"),
                                                           ("sso", "SSO in the browser (external browser)"), ("oauth", "OAuth access token")]),
        Field("password", "Password / OAuth token", "password", show_if={"auth": ["password", "oauth"]}),
        Field("private_key_file", "Private key file (.p8)", placeholder=r"C:\keys\rsa_key.p8", show_if={"auth": ["keypair"]}),
        Field("private_key_passphrase", "Private key passphrase", "password", show_if={"auth": ["keypair"]}),
        Field("warehouse", "Warehouse", required=True), Field("role", "Role"),
        Field("database", "Default database"), Field("schema", "Default schema"),
    ], "snowflake-connector-python", _snowflake, "SELECT CURRENT_VERSION() AS v, CURRENT_ROLE() AS r, CURRENT_WAREHOUSE() AS w",
        _isc("information_schema.columns", upper=True)),
    "sqlserver": Spec("sqlserver", "tsql", _MSSQL_FIELDS, "pyodbc", _mssql, "SELECT @@VERSION AS v",
                      _isc("information_schema.columns")),
    "synapse": Spec("synapse", "tsql", _MSSQL_FIELDS, "pyodbc", _mssql, "SELECT @@VERSION AS v", _isc("information_schema.columns")),
    "fabric": Spec("fabric", "tsql", _MSSQL_FIELDS, "pyodbc", _mssql, "SELECT @@VERSION AS v", _isc("information_schema.columns")),
    "postgres": Spec("postgres", "postgres", _PG_FIELDS(5432), "psycopg[binary]", lambda c: _postgres(c, 5432), "SELECT version() AS v",
                     _isc("information_schema.columns"), _pg_read_only),
    "redshift": Spec("redshift", "redshift", _PG_FIELDS(5439), "psycopg[binary]", lambda c: _postgres(c, 5439), "SELECT version() AS v",
                     _isc("information_schema.columns"), _pg_read_only),
    "mysql": Spec("mysql", "mysql", [
        Field("host", "Host", required=True), Field("port", "Port", "number", placeholder="3306"),
        Field("database", "Database", required=True), Field("user", "User", required=True), Field("password", "Password", "password"),
        Field("ssl", "Use SSL", "select", options=[("yes", "Yes"), ("no", "No")]),
    ], "pymysql", _mysql, "SELECT VERSION() AS v", _isc("information_schema.columns"), _mysql_read_only),
    "oracle": Spec("oracle", "oracle", [
        Field("host", "Host"), Field("port", "Port", "number", placeholder="1521"), Field("service_name", "Service name"),
        Field("dsn", "…or full DSN / TNS alias", placeholder="mydb_high  or  host:1521/service"),
        Field("user", "User", required=True), Field("password", "Password", "password"),
    ], "oracledb", _oracle, "SELECT banner AS v FROM v$version FETCH FIRST 1 ROWS ONLY", None, _oracle_read_only),
    "bigquery": Spec("bigquery", "bigquery", [
        Field("project", "Project ID", required=True), Field("location", "Location", placeholder="US / EU / us-central1"),
        Field("credentials_file", "Service account key file (.json)", placeholder=r"C:\keys\bq-reader.json",
              help="Leave blank to use your gcloud application-default login"),
    ], "google-cloud-bigquery", _bigquery, "SELECT 1 AS ok"),
    "teradata": Spec("teradata", "teradata", [
        Field("host", "Host", required=True), Field("user", "User", required=True), Field("password", "Password", "password"),
        Field("logmech", "Logon mechanism", "select", options=[("", "TD2 (default)"), ("LDAP", "LDAP"), ("BROWSER", "Browser SSO")]),
        Field("database", "Default database"),
    ], "teradatasql", _teradata, "SELECT InfoData AS v FROM DBC.DBCInfoV WHERE InfoKey = 'VERSION'"),
    "db2": Spec("db2", "db2", [
        Field("host", "Host", required=True), Field("port", "Port", "number", placeholder="50000"),
        Field("database", "Database", required=True), Field("user", "User", required=True), Field("password", "Password", "password"),
        Field("ssl", "Use SSL", "select", options=[("no", "No"), ("yes", "Yes")]),
    ], "ibm_db", _db2, "SELECT service_level AS v FROM SYSIBMADM.ENV_INST_INFO"),
}
COMMON_FIELDS = [Field("catalog", "Default database / catalog for table names (optional)",
                       help="Prefixed to table names that come without one (e.g. sales.orders -> PROD.sales.orders)")]


def _hint(platform: str, e: Exception) -> str:
    m = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
    low = m.lower()
    if "driver not installed" in low or "no sql server odbc driver" in low:
        return m
    if any(x in low for x in ("password", "login failed", "authentication", "incorrect username", "invalid credentials", "28000", "18456")):
        return f"Sign-in rejected - check user/password or client ID/secret ({m})"
    if any(x in low for x in ("timed out", "timeout", "could not connect", "unable to connect", "name or service not known",
                              "getaddrinfo", "connection refused", "network")):
        return f"Can't reach the server - check host/port, VPN and firewall ({m})"
    if "certificate" in low or "ssl" in low:
        return f"TLS/certificate problem - try 'Trust server certificate' or check the company proxy ({m})"
    return m


class SQLDatabaseConnector(DataConnector):
    """One adapter for every SQL engine in SPECS (selected by `platform`)."""
    platform = "sql"
    sql_dialect = "databricks"         # dialect of the SQL the engine hands us; translated before execution
    mode = RunMode.LIVE

    def __init__(self, config: dict, platform: str):
        super().__init__(config)
        self.platform = platform
        self.spec = SPECS[platform]
        self._con = None

    def capabilities(self) -> set[str]:
        return {Capability.DESCRIBE_TABLE, Capability.EXECUTE_SQL}

    # -------------------------------------------------------------- connection
    def connection(self):
        if self._con is None:
            missing = [f.label for f in self.spec.fields if f.required and not self.config.get(f.key)
                       and (not f.show_if or self.config.get(list(f.show_if)[0]) in list(f.show_if.values())[0])]
            if missing:
                raise ConnectorError(f"Missing in Settings: {', '.join(missing)}")
            try:
                self._con = self.spec.connect(self.config)
            except ConnectorError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ConnectorError(_hint(self.platform, e)) from e
            if self.spec.read_only:
                self.spec.read_only(self._con)
        return self._con

    def close(self):
        if self._con is not None:
            try:
                self._con.close()
            except Exception:  # noqa: BLE001
                pass
            self._con = None

    def _run(self, sql: str, params: Optional[list] = None) -> tuple[list[str], list[tuple]]:
        cur = self.connection().cursor()
        try:
            if params:
                style = {"tsql": "?", "teradata": "?", "db2": "?", "oracle": None, "bigquery": None}.get(self.spec.dialect, "%s")
                if style == "?":
                    sql = sql.replace("%s", "?")
                elif self.spec.dialect == "oracle":
                    for i in range(len(params)):
                        sql = sql.replace("%s", f":{i + 1}", 1)
                elif self.spec.dialect == "bigquery":
                    sql = sql.replace("%s", "%s")
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchall() if cur.description else []
            return cols, [tuple(r) for r in rows]
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    def test_connection(self) -> ConnectionTestResult:
        try:
            cols, rows = self._run(self.spec.version_sql)
            info = ", ".join(str(v)[:80] for v in (rows[0] if rows else ()) if v is not None)
            return ConnectionTestResult(True, f"Connected to {self.platform}{' · ' + info if info else ''}", self.mode)
        except ConnectorError as e:
            return ConnectionTestResult(False, str(e), self.mode)
        except Exception as e:  # noqa: BLE001
            return ConnectionTestResult(False, _hint(self.platform, e), self.mode)

    # -------------------------------------------------------------- metadata
    @staticmethod
    def _parts(fqn: str) -> list[str]:
        out, cur, q = [], "", None
        for ch in fqn:
            if q:
                if ch == q:
                    q = None
                else:
                    cur += ch
            elif ch in "`\"[":
                q = {"[": "]"}.get(ch, ch)
            elif ch == ".":
                out.append(cur)
                cur = ""
            else:
                cur += ch
        out.append(cur)
        return [p.strip() for p in out if p.strip()]

    def _table_sql(self, fqn: str) -> str:
        return ".".join("`" + p.replace("`", "``") + "`" for p in self._parts(fqn))

    def describe_table(self, fqn: str) -> SourceTableMeta:
        parts = self._parts(fqn)
        if not parts:
            raise ConnectorError("Empty table name")
        cols: list[ColumnMeta] = []
        if self.spec.columns_sql:
            try:
                q, params = self.spec.columns_sql(parts)
                _, rows = self._run(q, params)
                cols = [ColumnMeta(name=r[0], data_type=str(r[1]).lower(), nullable=str(r[2]).upper() == "YES") for r in rows]
            except Exception:  # noqa: BLE001
                cols = []
        if not cols:                                     # universal fallback: zero-row select
            sql = to_dialect(f"SELECT * FROM {self._table_sql(fqn)} WHERE 1 = 0", self.spec.dialect)
            try:
                names, _ = self._run(sql)
            except Exception as e:  # noqa: BLE001
                raise ConnectorError(f"TABLE_OR_VIEW_NOT_FOUND: {fqn} ({_hint(self.platform, e)})") from e
            cols = [ColumnMeta(name=n, data_type="unknown") for n in names]
        return SourceTableMeta(catalog=parts[-3] if len(parts) >= 3 else None, schema_name=parts[-2] if len(parts) >= 2 else None,
                               name=parts[-1], columns=cols)

    def list_tables(self, catalog: Optional[str] = None, schema: Optional[str] = None) -> list[SourceTableMeta]:
        """Tables visible to the login (used for suggestions when a mapped table is not found)."""
        d = self.spec.dialect
        if d == "oracle":
            q = "SELECT owner, table_name FROM all_tables WHERE owner NOT IN ('SYS','SYSTEM','XDB','MDSYS','CTXSYS','OUTLN','DBSNMP')"
        elif d == "teradata":
            q = "SELECT DatabaseName, TableName FROM DBC.TablesV WHERE TableKind IN ('T','V','O')"
        elif d == "db2":
            q = "SELECT tabschema, tabname FROM syscat.tables WHERE tabschema NOT LIKE 'SYS%'"
        elif d == "bigquery":
            return []
        else:
            q = ("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN "
                 "('information_schema','pg_catalog','INFORMATION_SCHEMA','sys','mysql','performance_schema')")
        try:
            _, rows = self._run(q + " ORDER BY 1, 2" if d not in ("teradata",) else q)
        except Exception:  # noqa: BLE001
            return []
        return [SourceTableMeta(catalog=None, schema_name=str(r[0]).strip(), name=str(r[1]).strip()) for r in rows[:5000]
                if not schema or str(r[0]).strip().lower() == schema.lower()]

    def sample_values(self, fqn: str, column: str, limit: int = 200) -> list[str]:
        col = "`" + column.replace("`", "``") + "`"
        sql = to_dialect(f"SELECT DISTINCT {col} AS v FROM {self._table_sql(fqn)} WHERE {col} IS NOT NULL LIMIT {int(limit)}",
                         self.spec.dialect)
        _, rows = self._run(sql)
        return [str(r[0]) for r in rows]

    # -------------------------------------------------------------- execution
    def execute_sql(self, sql: str, params: Optional[dict[str, Any]] = None) -> QueryResult:
        assert_read_only(sql, "databricks")
        native = to_dialect(sql, self.spec.dialect, params)
        t0 = time.time()
        try:
            cols, rows = self._run(native)
        except ConnectorError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"{type(e).__name__}: {_hint(self.platform, e)}") from e
        lower = [c.lower() for c in cols]                # engines that upper-case aliases (Snowflake, Oracle)
        out = [dict(zip(lower, r)) for r in rows]
        return QueryResult(rows=out, query_text=sql, language="sql", connector=self.platform, mode=self.mode,
                           elapsed_ms=int((time.time() - t0) * 1000),
                           extra={"params": params or {}, "executed_as": native, "dialect": self.spec.dialect})


def factory(platform: str):
    class _C(SQLDatabaseConnector):
        def __init__(self, config: dict):
            super().__init__(config, platform)
    _C.__name__ = f"{platform.title()}Connector"
    return _C


SnowflakeConnector = factory("snowflake")
SQLServerConnector = factory("sqlserver")
SynapseConnector = factory("synapse")
FabricConnector = factory("fabric")
PostgresConnector = factory("postgres")
RedshiftConnector = factory("redshift")
MySQLConnector = factory("mysql")
OracleConnector = factory("oracle")
BigQueryConnector = factory("bigquery")
TeradataConnector = factory("teradata")
DB2Connector = factory("db2")
