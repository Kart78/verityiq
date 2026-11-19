"""Databricks connection diagnostic for the BI Testing Assistant.

    .\\.venv\\Scripts\\python.exe scripts\\check_databricks.py
    .\\.venv\\Scripts\\python.exe scripts\\check_databricks.py --host adb-123.4.azuredatabricks.net --http-path /sql/1.0/warehouses/abc

Checks, in order, and stops at the first failure with the fix:
  1 connector package installed      4 network: DNS, port 443, TLS/proxy
  2 token present in the environment 5 login + SQL warehouse (SELECT current_user())
  3 hostname / HTTP path settings    6 read access to the project's source tables
Never prints the token. Read-only.
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"


def say(tag, msg):
    print(f"{tag} {msg}")


def stop(msg, fix):
    say(BAD, msg)
    print(f"       FIX: {fix}\n")
    sys.exit(1)


def project_settings():
    try:
        import yaml
    except ImportError:
        return None, None
    for p in sorted((ROOT / "data" / "projects").glob("*/environment.yaml"), key=lambda x: x.stat().st_mtime, reverse=True):
        env = yaml.safe_load(p.read_text())
        if env.get("data", {}).get("platform") == "databricks":
            return env, p
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host")
    ap.add_argument("--http-path")
    ap.add_argument("--token-var", default=None)
    a = ap.parse_args()
    print(f"\nPython {sys.version.split()[0]} at {sys.executable}\n")

    # 1 package
    try:
        from databricks import sql as dbsql  # noqa: F401
        import databricks.sql as m
        say(OK, f"databricks-sql-connector installed (version {getattr(m, '__version__', '?')})")
    except ImportError:
        stop("databricks-sql-connector is NOT installed in this Python environment.",
             r"run:  .\.venv\Scripts\python.exe -m pip install databricks-sql-connector   then restart the app")

    # 3 settings (read before token so we know which variable is referenced)
    env, path = project_settings()
    cfg = (env or {}).get("data", {}).get("config", {})
    host = a.host or cfg.get("server_hostname", "")
    http_path = a.http_path or cfg.get("http_path", "")
    tok_ref = cfg.get("access_token", "${DATABRICKS_TOKEN}")
    var = a.token_var or (re.match(r"^\$\{(\w+)\}$", tok_ref or "") or [None, "DATABRICKS_TOKEN"])[1]
    if path:
        say(OK, f"Project settings read from {path.parent.name}")
    elif not a.host:
        stop("No project has Databricks selected in Settings, and no --host given.",
             "in the app open Settings -> Data platform = Databricks SQL, fill hostname/HTTP path/token, Save; or pass --host and --http-path")

    # 2 token
    tok = os.environ.get(var)
    if not tok:
        stop(f"Environment variable {var} is not set in THIS PowerShell window.",
             f'run:  $env:{var} = "dapi..."   in the same window, then start the app (or this script) from that window. '
             f'Permanent: [Environment]::SetEnvironmentVariable("{var}","dapi...","User") and open a new window.')
    say(OK if tok.startswith("dapi") else WARN, f"{var} is set ({len(tok)} characters{'' if tok.startswith('dapi') else ', does not start with dapi - check it is a personal access token'})")
    if tok_ref and not tok_ref.startswith("${"):
        say(WARN, "Settings store the token value directly; use the reference ${" + var + "} instead.")

    raw_host = host
    host = re.sub(r"^https?://", "", (host or "").strip()).split("/")[0]
    if not host:
        stop("Server hostname is empty.", "copy it from Databricks: SQL Warehouses -> your warehouse -> Connection details -> Server hostname")
    if host != raw_host.strip():
        say(WARN, f"Hostname was '{raw_host}'. Use only '{host}' (no https://, no path).")
    if not re.match(r"^/sql/(1\.0/warehouses|protocolv1/o/\d+)/[\w-]+$", http_path or ""):
        stop(f"HTTP path looks wrong: '{http_path}'.",
             "copy it from SQL Warehouses -> your warehouse -> Connection details -> HTTP path (e.g. /sql/1.0/warehouses/1a2b3c4d5e6f7a8b)")
    say(OK, f"Hostname {host}  |  HTTP path {http_path}")

    # 4 network
    try:
        ip = socket.gethostbyname(host)
        say(OK, f"DNS resolves {host} -> {ip}")
    except OSError:
        stop(f"Cannot resolve {host}.", "check the hostname spelling; on VPN-only workspaces connect to the company VPN first")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        say(OK, f"HTTPS proxy in use: {proxy}")
    else:
        try:
            socket.create_connection((host, 443), timeout=8).close()
            say(OK, "Port 443 reachable")
        except OSError as e:
            stop(f"Cannot open a connection to {host}:443 ({e}).",
                 "a firewall/proxy blocks it. Ask IT for the proxy, then:  $env:HTTPS_PROXY = 'http://proxy.company.com:8080'  "
                 "- or the workspace has an IP access list / Private Link that excludes your network")
    try:
        urllib.request.urlopen(f"https://{host}/", timeout=10)
        say(OK, "TLS handshake OK")
    except ssl.SSLError as e:
        stop(f"TLS certificate not trusted ({e}). Your company inspects HTTPS traffic.",
             r"run:  .\.venv\Scripts\python.exe -m pip install truststore   (the app then uses the Windows certificate store), "
             "or ask IT for the root certificate (.pem) and set  $env:REQUESTS_CA_BUNDLE = 'C:\\path\\corp-root.pem'")
    except Exception as e:  # noqa: BLE001  (HTTP errors are fine: TLS worked)
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            stop(f"TLS certificate not trusted ({e.reason}).",
                 r"pip install truststore, or set $env:REQUESTS_CA_BUNDLE to your company root certificate")
        say(OK, "TLS handshake OK")

    # 5 login + warehouse
    print("       connecting to the SQL warehouse (a stopped warehouse can take 2-5 minutes to start)...")
    try:
        from databricks import sql as dbsql
        with dbsql.connect(server_hostname=host, http_path=http_path, access_token=tok,
                           _socket_timeout=300) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user() AS u, current_catalog() AS c, current_version().dbsql_version AS v")
            u, c, v = cur.fetchone()
            say(OK, f"Logged in as {u}  |  default catalog '{c}'  |  DBSQL {v}")
            # 6 source tables
            tables = [t.get("source") for t in (env or {}).get("mapping", {}).get("tables", {}).values()
                      if t.get("source") and not str(t.get("source")).startswith("(")]
            for t in tables[:15]:
                parts = t.split(".")
                fq = t if len(parts) == 3 else f"{c}.{t}"
                try:
                    cur.execute(f"SELECT 1 FROM {'.'.join('`' + p.strip('`') + '`' for p in fq.split('.'))} LIMIT 1")
                    cur.fetchall()
                    say(OK, f"Can read {fq}")
                except Exception as e:  # noqa: BLE001
                    msg = str(e).splitlines()[0][:160]
                    hint = ("table not found in that catalog - put the full catalog.schema.table on the Project page"
                            if re.search(r"TABLE_OR_VIEW_NOT_FOUND|not found|does not exist", msg, re.I)
                            else "no SELECT permission - ask the data owner to GRANT SELECT / USE SCHEMA / USE CATALOG")
                    say(BAD, f"Cannot read {fq}: {msg}\n       FIX: {hint}")
            if len(parts := tables) > 15:
                say(WARN, f"Checked 15 of {len(parts)} tables")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if re.search(r"401|403|invalid access token|Unauthorized|PERMISSION_DENIED.*token", msg, re.I):
            stop(f"Login refused: {msg[:200]}", "token expired/revoked or for another workspace - create a new one: User settings -> Developer -> Access tokens")
        if re.search(r"404|not found|RESOURCE_DOES_NOT_EXIST", msg, re.I):
            stop(f"Warehouse not found: {msg[:200]}", "HTTP path is for another warehouse/workspace - copy it again from Connection details")
        if re.search(r"CAN_USE|permission", msg, re.I):
            stop(f"No permission on the warehouse: {msg[:200]}", "ask an admin to give you CAN USE on the SQL warehouse")
        if re.search(r"SSL|CERTIFICATE", msg, re.I):
            stop(f"TLS problem: {msg[:200]}", r"pip install truststore, or set $env:REQUESTS_CA_BUNDLE to your company root certificate")
        if re.search(r"timed out|timeout", msg, re.I):
            stop(f"Timed out: {msg[:200]}", "warehouse may be starting (retry in 3 minutes) or a proxy is silently blocking - see step 4")
        stop(f"Connection failed: {msg[:300]}", "send this output to your Databricks admin or paste it in the chat")
    print("\nAll checks passed. In the app: Settings -> Test connections, then Run Automated Test.\n")


if __name__ == "__main__":
    main()
