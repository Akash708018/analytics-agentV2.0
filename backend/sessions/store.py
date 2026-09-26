"""SQLite session + turn store. Storage only: no validation, no policy (see services/)."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    sid TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    ui_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    sid TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    events TEXT NOT NULL,
    answer TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_sid ON turns(sid, created_at);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.executescript(SCHEMA)
            self._con.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._con.execute(sql, args).fetchall()
            self._con.commit()
            return rows

    # sessions
    def insert_session(self, s: dict) -> None:
        self._q("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                (s["sid"], s["version"], s["workspace_id"], json.dumps(s["ui_state"]),
                 s["created_at"], s["updated_at"], s["expires_at"]))

    def get_session(self, sid: str) -> dict | None:
        rows = self._q("SELECT * FROM sessions WHERE sid=?", (sid,))
        if not rows:
            return None
        d = dict(rows[0])
        d["ui_state"] = json.loads(d["ui_state"])
        return d

    def touch_session(self, sid: str, updated_at: str, expires_at: str) -> None:
        self._q("UPDATE sessions SET updated_at=?, expires_at=? WHERE sid=?",
                (updated_at, expires_at, sid))

    def cas_ui_state(self, sid: str, expected_version: int, ui_state_json: str,
                     updated_at: str, expires_at: str) -> bool:
        """Compare-and-set: writes only if the stored version is the expected one."""
        with self._lock:
            cur = self._con.execute(
                "UPDATE sessions SET ui_state=?, version=version+1, updated_at=?, expires_at=? "
                "WHERE sid=? AND version=?",
                (ui_state_json, updated_at, expires_at, sid, expected_version))
            self._con.commit()
            return cur.rowcount == 1

    def delete_session(self, sid: str) -> bool:
        with self._lock:
            cur = self._con.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            self._con.execute("DELETE FROM turns WHERE sid=?", (sid,))
            self._con.commit()
            return cur.rowcount == 1

    def purge_expired(self, now_iso: str) -> int:
        with self._lock:
            sids = [r[0] for r in self._con.execute(
                "SELECT sid FROM sessions WHERE expires_at <= ?", (now_iso,))]
            for sid in sids:
                self._con.execute("DELETE FROM turns WHERE sid=?", (sid,))
                self._con.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            self._con.commit()
            return len(sids)

    # turns
    def insert_turn(self, t: dict) -> None:
        self._q("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                (t["turn_id"], t["sid"], t["dataset_id"], t["question"], t["status"],
                 json.dumps(t["events"]), None, t["created_at"]))

    def update_turn(self, turn_id: str, status: str, events: list, answer: dict | None) -> None:
        self._q("UPDATE turns SET status=?, events=?, answer=? WHERE turn_id=?",
                (status, json.dumps(events), None if answer is None else json.dumps(answer),
                 turn_id))

    @staticmethod
    def _turn(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["events"] = json.loads(d["events"])
        d["answer"] = None if d["answer"] is None else json.loads(d["answer"])
        return d

    def get_turn(self, turn_id: str) -> dict | None:
        rows = self._q("SELECT * FROM turns WHERE turn_id=?", (turn_id,))
        return self._turn(rows[0]) if rows else None

    def list_turns(self, sid: str) -> list[dict]:
        return [self._turn(r) for r in
                self._q("SELECT * FROM turns WHERE sid=? ORDER BY created_at, rowid", (sid,))]

    def mark_running_interrupted(self) -> int:
        with self._lock:
            cur = self._con.execute(
                "UPDATE turns SET status='interrupted' WHERE status IN ('queued','running')")
            self._con.commit()
            return cur.rowcount
