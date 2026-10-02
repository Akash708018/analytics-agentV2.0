"""Session and turn services: the one place their rules live (HTTP and MCP both call here)."""
from __future__ import annotations

import json
import re
import secrets
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.sessions.store import Store

UI_STATE_MAX_BYTES = 256 * 1024
SESSION_TTL = timedelta(days=30)
ROW_LIKE_MIN = 20

_KEY_RX = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}"
                     r"|gsk_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,})")
# Bounded local part (RFC 5321: 64): an unbounded `+@` backtracks quadratically on a long
# string with no "@" (measured: 63 s on 256 KB of "x").
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}")
_PHONE_RX = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)|\+\d{1,3}[\s-]?\d{6,14}")
# The server's own ids (ds_/ws_ + 12 hex, t_ + 16 hex) are random hex: 0.34% of dataset ids
# and 0.51% of turn ids hold ten digits that read as an Indian mobile number (measured over
# 20,000 each; F3 browser check: ds_2a7207443888). An exact id is never personal data.
_SERVER_ID_RX = re.compile(r"(?:ds|ws)_[0-9a-f]{12}|t_[0-9a-f]{16}")


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ui_state_problem(ui_state: Any) -> str | None:
    """Why this ui_state may not be stored, or None. Data rows, API keys and PII are refused."""
    def walk(v: Any, path: str) -> str | None:
        if isinstance(v, str):
            if _KEY_RX.search(v):
                return f"{path}: looks like an API key"
            if _EMAIL_RX.search(v) or (_PHONE_RX.search(v) and not _SERVER_ID_RX.fullmatch(v)):
                return f"{path}: looks like personal data (email/phone)"
        elif isinstance(v, dict):
            for k, x in v.items():
                if isinstance(k, str) and (_KEY_RX.search(k) or _EMAIL_RX.search(k)):
                    return f"{path}: a key looks like an API key or email"
                if (p := walk(x, f"{path}.{k}")):
                    return p
        elif isinstance(v, list):
            dicts = [x for x in v if isinstance(x, dict)]
            if len(dicts) >= ROW_LIKE_MIN and len({tuple(sorted(d)) for d in dicts}) == 1:
                return f"{path}: {len(dicts)} same-shaped objects look like data rows"
            for i, x in enumerate(v):
                if (p := walk(x, f"{path}[{i}]")):
                    return p
        return None
    return walk(ui_state, "ui_state")


class SessionService:
    def __init__(self, store: Store, now: Callable[[], datetime] | None = None):
        self.store = store
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _stamp(self) -> tuple[str, str]:
        n = self.now()
        return _iso(n), _iso(n + SESSION_TTL)

    def create(self, workspace_id: str | None = None) -> dict:
        now, exp = self._stamp()
        s = {"sid": str(uuid.uuid4()), "version": 1,
             "workspace_id": workspace_id or f"ws_{secrets.token_hex(6)}",
             "ui_state": {}, "created_at": now, "updated_at": now, "expires_at": exp}
        self.store.insert_session(s)
        return s

    def get(self, sid: str, touch: bool = True) -> dict:
        s = self.store.get_session(sid)
        if s is None or s["expires_at"] <= _iso(self.now()):
            if s is not None:
                self.store.delete_session(sid)
            raise ServiceError(404, "not_found", f"session {sid} not found or expired")
        if touch:
            now, exp = self._stamp()
            self.store.touch_session(sid, now, exp)
            s["updated_at"], s["expires_at"] = now, exp
        return s

    def put_ui_state(self, sid: str, ui_state: dict, version: int) -> dict:
        current = self.get(sid, touch=False)
        blob = json.dumps(ui_state, separators=(",", ":"))
        if len(blob.encode()) > UI_STATE_MAX_BYTES:
            raise ServiceError(413, "ui_state_too_large",
                               f"ui_state is {len(blob.encode())} bytes; limit "
                               f"{UI_STATE_MAX_BYTES}")
        if (why := ui_state_problem(ui_state)):
            raise ServiceError(422, "ui_state_rejected", why)
        now, exp = self._stamp()
        if not self.store.cas_ui_state(sid, version, blob, now, exp):
            latest = self.get(sid, touch=False)
            raise ServiceError(409, "version_conflict",
                               f"version {version} is stale; current is {latest['version']}",
                               {"current": latest})
        del current
        return self.get(sid, touch=False)

    def delete(self, sid: str) -> None:
        if not self.store.delete_session(sid):
            raise ServiceError(404, "not_found", f"session {sid} not found")


# A runner answers one turn. It receives the turn and an emit(type, data) callback; it returns
# the answer dict (or None). It is called exactly once per turn.
Runner = Callable[[dict, Callable[[str, dict], None]], "dict | None"]


def unwired_runner(turn: dict, emit: Callable[[str, dict], None]) -> dict | None:
    emit("error", {"code": "agent_not_wired",
                   "message": "The question was stored; the agent that answers it arrives "
                              "in milestone B5."})
    raise _TurnFailed()


class _TurnFailed(Exception):
    pass


class TurnService:
    def __init__(self, store: Store, sessions: SessionService, runner: Runner | None = None,
                 workers: int = 4):
        self.store, self.sessions = store, sessions
        self.runner = runner or unwired_runner
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="turn")
        self.futures: dict[str, Future] = {}
        store.mark_running_interrupted()   # a turn never re-runs after a restart

    def create(self, sid: str, dataset_id: str, question: str) -> str:
        self.sessions.get(sid)
        turn = {"turn_id": f"t_{secrets.token_hex(8)}", "sid": sid, "dataset_id": dataset_id,
                "question": question, "status": "queued", "events": [],
                "created_at": _iso(self.sessions.now())}
        self.store.insert_turn(turn)
        self.futures[turn["turn_id"]] = self.pool.submit(self._run, turn)
        return turn["turn_id"]

    def _run(self, turn: dict) -> None:
        events: list[dict] = []
        tid = turn["turn_id"]

        def emit(kind: str, data: dict) -> None:
            events.append({"seq": len(events), "type": kind,
                           "at": _iso(self.sessions.now()), "data": data})
            self.store.update_turn(tid, "running", events, None)

        self.store.update_turn(tid, "running", events, None)
        try:
            answer = self.runner(turn, emit)
            self.store.update_turn(tid, "done", events, answer)
        except _TurnFailed:
            self.store.update_turn(tid, "failed", events, None)
        except Exception as e:  # noqa: BLE001 -- recorded on the turn, never re-raised
            emit("error", {"code": "internal", "message": f"{type(e).__name__}: {e}"})
            self.store.update_turn(tid, "failed", events, None)

    def get(self, turn_id: str) -> dict:
        t = self.store.get_turn(turn_id)
        if t is None:
            raise ServiceError(404, "not_found", f"turn {turn_id} not found")
        return t

    def list(self, sid: str) -> list[dict]:
        self.sessions.get(sid)
        return self.store.list_turns(sid)

    def wait(self, turn_id: str, timeout: float = 10) -> None:
        if (f := self.futures.get(turn_id)):
            f.result(timeout=timeout)
