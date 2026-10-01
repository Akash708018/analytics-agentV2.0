"""A stateful in-memory stand-in for the backend's session, turn and dataset routes.

It follows the backend's observable rules (backend/services/sessions.py, checked
live in F1): versions bump on each ui-state write, a stale version answers 409
with `current`, emails are refused 422 `ui_state_rejected`, unknown ids are 404
`not_found`. test_f2_fake.py pins its shapes to the spec's required keys.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

import httpx

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}")
STAMP = "2026-10-01T10:00:00Z"
EXPIRES = "2026-10-31T10:00:00Z"


def _error(status: int, code: str, message: str, **extra) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message,
                                                  "milestone": None}, **extra})


class FakeBackend:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.turns: dict[str, dict] = {}
        self.datasets: dict[str, dict] = {}
        self.calls: list[tuple[str, str, object]] = []
        self.faults: dict[tuple[str, str], tuple[type[Exception], bool]] = {}
        self.down = False
        self.after = None          # called with (method, path) once a request is answered

    # --- test controls ------------------------------------------------------------------
    def client_factory(self):
        from frontend.api_client import APIClient
        return lambda: APIClient(base_url="http://backend.test",
                                 transport=httpx.MockTransport(self.handle))

    def new_session(self, ui_state: dict | None = None, workspace_id: str = "ws_0123456789ab") -> str:
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"sid": sid, "version": 1, "workspace_id": workspace_id,
                              "ui_state": ui_state or {}, "created_at": STAMP,
                              "updated_at": STAMP, "expires_at": EXPIRES}
        return sid

    def write_elsewhere(self, sid: str, ui_state: dict) -> None:
        """Another tab saved: the version moves on."""
        s = self.sessions[sid]
        s["ui_state"], s["version"] = ui_state, s["version"] + 1

    def add_dataset(self, workspace_id: str, name: str, rows: int = 4, columns: int = 8) -> dict:
        ds = {"dataset_id": f"ds_{uuid.uuid4().hex[:12]}", "workspace_id": workspace_id,
              "name": name, "rows": rows, "columns": columns, "created_at": STAMP,
              "assumptions": ["row 1 is the header"]}
        self.datasets[ds["dataset_id"]] = ds
        return ds

    def finish_turn(self, turn_id: str, text: str = "ROAS fell from 4.1 to 3.2.") -> None:
        t = self.turns[turn_id]
        t["status"] = "done"
        t["events"].append({"seq": len(t["events"]), "type": "answer", "at": STAMP,
                            "data": {"text": text}})
        t["answer"] = {"text": text, "flags": [], "playbook": None, "results": [],
                       "skipped": [], "usage": {"llm_calls": 0, "tool_calls": 0}}

    def fail_on(self, method: str, path: str, error: type[Exception], *, stored: bool) -> None:
        """Raise a transport error for the next matching request, after (stored=True) or
        before the server acts on it."""
        self.faults[(method, path)] = (error, stored)

    def count(self, method: str, prefix: str = "") -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p.startswith(prefix))

    def bodies(self, method: str, prefix: str = "") -> list[object]:
        return [b for m, p, b in self.calls if m == method and p.startswith(prefix)]

    # --- the HTTP surface --------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        method, path = request.method, request.url.path
        body: object = None
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"null")
        self.calls.append((method, path, body))
        fault = self.faults.pop((method, path), None)
        if fault and not fault[1]:
            raise fault[0]("injected", request=request)
        response = self._route(method, path, body, request)
        if self.after is not None:
            self.after(method, path)
        if fault:
            raise fault[0]("injected after the server acted", request=request)
        return response

    def _route(self, method: str, path: str, body, request) -> httpx.Response:
        parts = path.strip("/").split("/")
        if method == "POST" and parts == ["sessions"]:
            sid = self.new_session(workspace_id=(body or {}).get("workspace_id")
                                   or f"ws_{uuid.uuid4().hex[:12]}")
            return httpx.Response(201, json={"sid": sid, "version": 1})
        if parts[0] == "sessions" and len(parts) >= 2:
            s = self.sessions.get(parts[1])
            if s is None:
                return _error(404, "not_found", f"session {parts[1]} not found or expired")
            if method == "GET" and len(parts) == 2:
                return httpx.Response(200, json=s)
            if method == "PUT" and parts[2:] == ["ui-state"]:
                if body["version"] != s["version"]:
                    return _error(409, "version_conflict",
                                  f"version {body['version']} is stale; current is "
                                  f"{s['version']}", current=s)
                if _EMAIL.search(json.dumps(body["ui_state"])):
                    return _error(422, "ui_state_rejected",
                                  "ui_state.label: looks like personal data (email/phone)")
                s["ui_state"], s["version"] = body["ui_state"], s["version"] + 1
                s["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                return httpx.Response(200, json=s)
            if method == "GET" and parts[2:] == ["turns"]:
                mine = [t for t in self.turns.values() if t["sid"] == s["sid"]]
                return httpx.Response(200, json={"turns": mine})
        if method == "POST" and parts == ["turns"]:
            if body["sid"] not in self.sessions:
                return _error(404, "not_found", f"session {body['sid']} not found or expired")
            turn_id = f"t_{uuid.uuid4().hex[:16]}"
            self.turns[turn_id] = {"turn_id": turn_id, "sid": body["sid"],
                                   "dataset_id": body["dataset_id"],
                                   "question": body["question"], "status": "running",
                                   "events": [], "answer": None, "created_at": STAMP}
            return httpx.Response(202, json={"turn_id": turn_id})
        if method == "GET" and parts[0] == "turns" and len(parts) == 2:
            t = self.turns.get(parts[1])
            return httpx.Response(200, json=t) if t else _error(404, "not_found",
                                                                f"turn {parts[1]} not found")
        if method == "POST" and parts[0] == "workspaces" and parts[2:] == ["uploads"]:
            filename = re.search(rb'filename="([^"]+)"', request.content).group(1).decode()
            return httpx.Response(201, json=self.add_dataset(parts[1], filename.rsplit(".", 1)[0]))
        if method == "GET" and parts[0] == "datasets" and len(parts) == 2:
            ds = self.datasets.get(parts[1])
            return httpx.Response(200, json=ds) if ds else _error(
                404, "not_found", f"dataset {parts[1]} not found")
        return _error(404, "not_found", f"{method} {path} is not in the fake")
