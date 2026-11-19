"""Metadata, test, evidence and audit repository (SQLite for Phase 1; swap for Postgres/Azure SQL
by reimplementing this class - nothing else touches the database)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata_snapshot (env_id TEXT, kind TEXT, captured_at TEXT, body TEXT, PRIMARY KEY (env_id, kind));
CREATE TABLE IF NOT EXISTS kpi_override (env_id TEXT, kpi_id TEXT, body TEXT, updated_at TEXT, PRIMARY KEY (env_id, kpi_id));
CREATE TABLE IF NOT EXISTS test_case (env_id TEXT, test_id TEXT, body TEXT, updated_at TEXT, PRIMARY KEY (env_id, test_id));
CREATE TABLE IF NOT EXISTS test_run (run_id TEXT PRIMARY KEY, env_id TEXT, mode TEXT, started_at TEXT, finished_at TEXT,
                                     triggered_by TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS test_result (run_id TEXT, test_id TEXT, status TEXT, body TEXT, PRIMARY KEY (run_id, test_id));
CREATE TABLE IF NOT EXISTS connection_status (env_id TEXT, side TEXT, ok INTEGER, message TEXT, mode TEXT, tested_at TEXT,
                                              PRIMARY KEY (env_id, side));
CREATE TABLE IF NOT EXISTS defect (defect_id TEXT PRIMARY KEY, env_id TEXT, test_id TEXT, run_id TEXT, status TEXT, body TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT, action TEXT, target TEXT, detail TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(o: Any) -> str:
    return json.dumps(o, default=str)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._con.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._con.execute(sql, args)
            self._con.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._con.execute(sql, args).fetchall()

    # audit
    def audit(self, actor: str, action: str, target: str, detail: Any = None) -> None:
        self._exec("INSERT INTO audit_log (at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                   (now(), actor, action, target, _dump(detail) if detail is not None else None))

    def audit_log(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

    # metadata
    def save_snapshot(self, env_id: str, kind: str, body: Any) -> None:
        self._exec("INSERT OR REPLACE INTO metadata_snapshot VALUES (?,?,?,?)", (env_id, kind, now(), _dump(body)))

    def snapshot(self, env_id: str, kind: str) -> Optional[dict]:
        r = self._all("SELECT * FROM metadata_snapshot WHERE env_id=? AND kind=?", (env_id, kind))
        return {"captured_at": r[0]["captured_at"], "body": json.loads(r[0]["body"])} if r else None

    # connection status (only ever written after a real test_connection call)
    def set_connection(self, env_id: str, side: str, ok: bool, message: str, mode: str) -> None:
        self._exec("INSERT OR REPLACE INTO connection_status VALUES (?,?,?,?,?,?)", (env_id, side, int(ok), message, mode, now()))

    def connections(self, env_id: str) -> dict:
        return {r["side"]: {"ok": bool(r["ok"]), "message": r["message"], "mode": r["mode"], "tested_at": r["tested_at"]}
                for r in self._all("SELECT * FROM connection_status WHERE env_id=?", (env_id,))}

    # kpi overrides
    def save_kpi(self, env_id: str, kpi: dict) -> None:
        self._exec("INSERT OR REPLACE INTO kpi_override VALUES (?,?,?,?)", (env_id, kpi["id"], _dump(kpi), now()))

    def delete_kpi(self, env_id: str, kpi_id: str) -> None:
        self._exec("DELETE FROM kpi_override WHERE env_id=? AND kpi_id=?", (env_id, kpi_id))

    def kpi_overrides(self, env_id: str) -> list[dict]:
        return [json.loads(r["body"]) for r in self._all("SELECT body FROM kpi_override WHERE env_id=?", (env_id,))]

    # tests
    def upsert_tests(self, env_id: str, tests: list[dict]) -> None:
        existing = {t["id"]: t for t in self.tests(env_id)}
        for t in tests:
            old = existing.get(t["id"])
            if old and old.get("approval_status") != "PENDING":
                t["approval_status"], t["approved_by"] = old["approval_status"], old.get("approved_by")
            if old and old.get("generated_by") == "user":
                old["display_id"] = t.get("display_id")
                self._exec("INSERT OR REPLACE INTO test_case VALUES (?,?,?,?)", (env_id, t["id"], _dump(old), now()))
                continue  # never overwrite user-edited tests on regeneration
            self._exec("INSERT OR REPLACE INTO test_case VALUES (?,?,?,?)", (env_id, t["id"], _dump(t), now()))

    def save_test(self, env_id: str, t: dict) -> None:
        self._exec("INSERT OR REPLACE INTO test_case VALUES (?,?,?,?)", (env_id, t["id"], _dump(t), now()))

    def delete_test(self, env_id: str, test_id: str) -> None:
        self._exec("DELETE FROM test_case WHERE env_id=? AND test_id=?", (env_id, test_id))

    def tests(self, env_id: str) -> list[dict]:
        return [json.loads(r["body"]) for r in self._all("SELECT body FROM test_case WHERE env_id=? ORDER BY test_id", (env_id,))]

    def test(self, env_id: str, test_id: str) -> Optional[dict]:
        r = self._all("SELECT body FROM test_case WHERE env_id=? AND test_id=?", (env_id, test_id))
        return json.loads(r[0]["body"]) if r else None

    # runs
    def start_run(self, run_id: str, env_id: str, mode: str, actor: str) -> None:
        self._exec("INSERT INTO test_run VALUES (?,?,?,?,?,?,?)", (run_id, env_id, mode, now(), None, actor, None))

    def finish_run(self, run_id: str, summary: dict) -> None:
        self._exec("UPDATE test_run SET finished_at=?, summary=? WHERE run_id=?", (now(), _dump(summary), run_id))

    def save_result(self, r: dict) -> None:
        self._exec("INSERT OR REPLACE INTO test_result VALUES (?,?,?,?)", (r["run_id"], r["test_id"], r["status"], _dump(r)))

    def runs(self, env_id: str, limit: int = 50) -> list[dict]:
        out = []
        for r in self._all("SELECT * FROM test_run WHERE env_id=? ORDER BY started_at DESC, rowid DESC LIMIT ?", (env_id, limit)):
            d = dict(r)
            d["summary"] = json.loads(d["summary"]) if d["summary"] else None
            out.append(d)
        return out

    def results(self, run_id: str) -> list[dict]:
        return [json.loads(r["body"]) for r in self._all("SELECT body FROM test_result WHERE run_id=? ORDER BY test_id", (run_id,))]

    # defects
    def upsert_defect(self, d: dict) -> None:
        self._exec("INSERT OR REPLACE INTO defect VALUES (?,?,?,?,?,?,?)",
                   (d["defect_id"], d["env_id"], d["test_id"], d["run_id"], d["status"], _dump(d), now()))

    def defects(self, env_id: str) -> list[dict]:
        return [json.loads(r["body"]) for r in self._all("SELECT body FROM defect WHERE env_id=? ORDER BY updated_at DESC", (env_id,))]

    def open_defect_for(self, env_id: str, test_id: str) -> Optional[dict]:
        r = self._all("SELECT body FROM defect WHERE env_id=? AND test_id=? AND status NOT IN ('Closed','Resolved')", (env_id, test_id))
        return json.loads(r[0]["body"]) if r else None
