"""B1: sessions, turns and the 501 stubs, through HTTP."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.services.sessions import UI_STATE_MAX_BYTES


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(tmp_path, clock):
    return TestClient(create_app(state_dir=tmp_path, now=clock))


def _new(client) -> dict:
    r = client.post("/sessions", json={})
    assert r.status_code == 201
    return r.json()


def test_health_and_version(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/version").json()["api_version"] == "0.1.0"


def test_create_returns_uuid4_and_version_1(client):
    s = _new(client)
    assert uuid.UUID(s["sid"]).version == 4 and s["version"] == 1


def test_create_without_body_and_with_workspace(client):
    assert client.post("/sessions").status_code == 201
    sid = client.post("/sessions", json={"workspace_id": "ws_keep"}).json()["sid"]
    assert client.get(f"/sessions/{sid}").json()["workspace_id"] == "ws_keep"


def test_get(client):
    s = _new(client)
    g = client.get(f"/sessions/{s['sid']}").json()
    assert g["version"] == 1 and g["ui_state"] == {} and g["workspace_id"].startswith("ws_")


def test_get_unknown_is_404_in_error_shape(client):
    r = client.get("/sessions/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_update_bumps_version(client):
    sid = _new(client)["sid"]
    r = client.put(f"/sessions/{sid}/ui-state", json={"ui_state": {"screen": "ask"},
                                                      "version": 1})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert client.get(f"/sessions/{sid}").json()["ui_state"] == {"screen": "ask"}


def test_stale_version_is_409_with_current_state(client):
    sid = _new(client)["sid"]
    client.put(f"/sessions/{sid}/ui-state", json={"ui_state": {"a": 1}, "version": 1})
    r = client.put(f"/sessions/{sid}/ui-state", json={"ui_state": {"a": 2}, "version": 1})
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "version_conflict"
    assert body["current"]["version"] == 2 and body["current"]["ui_state"] == {"a": 1}


def test_oversize_ui_state_is_413_and_not_stored(client):
    sid = _new(client)["sid"]
    big = {"blob": "x" * (UI_STATE_MAX_BYTES + 1)}
    r = client.put(f"/sessions/{sid}/ui-state", json={"ui_state": big, "version": 1})
    assert r.status_code == 413 and r.json()["error"]["code"] == "ui_state_too_large"
    assert client.get(f"/sessions/{sid}").json()["version"] == 1


def test_just_under_the_limit_is_accepted(client):
    sid = _new(client)["sid"]
    ok = {"b": "x" * (UI_STATE_MAX_BYTES - 20)}
    assert client.put(f"/sessions/{sid}/ui-state",
                      json={"ui_state": ok, "version": 1}).status_code == 200


@pytest.mark.parametrize("bad", [
    {"k": "sk-ant-api03-abcdefghijklmnopqrstuv"},
    {"gemini": "AIzaSyA1234567890abcdefghijklmnopqrstu"},
    {"contact": "priya.sharma@example.com"},
    {"phone": "+91 9876543210"},
    {"rows": [{"order_id": i, "revenue": i * 10} for i in range(25)]},
])
def test_keys_pii_and_data_rows_are_rejected(client, bad):
    sid = _new(client)["sid"]
    r = client.put(f"/sessions/{sid}/ui-state", json={"ui_state": bad, "version": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ui_state_rejected"


def test_expiry_is_sliding_30_days(client, clock):
    sid = _new(client)["sid"]
    clock.t += timedelta(days=29)
    assert client.get(f"/sessions/{sid}").status_code == 200   # activity extends
    clock.t += timedelta(days=29)
    assert client.get(f"/sessions/{sid}").status_code == 200
    clock.t += timedelta(days=30, seconds=1)
    assert client.get(f"/sessions/{sid}").status_code == 404


def test_delete(client):
    sid = _new(client)["sid"]
    assert client.delete(f"/sessions/{sid}").status_code == 204
    assert client.get(f"/sessions/{sid}").status_code == 404
    assert client.delete(f"/sessions/{sid}").status_code == 404


# --- turns ---------------------------------------------------------------------------------


def _wait(client, tid, until=("done", "failed")):
    for _ in range(200):
        t = client.get(f"/turns/{tid}").json()
        if t["status"] in until:
            return t
        time.sleep(0.01)
    raise AssertionError(t)


def test_turn_runs_once_and_rereads_never_rerun(tmp_path, clock):
    calls = {"tool": 0, "runs": 0}

    def runner(turn, emit):
        calls["runs"] += 1
        emit("plan", {"playbook": None, "steps": ["summary_stats"]})
        calls["tool"] += 1
        emit("tool_call", {"tool_id": "summary_stats", "params": {}, "ms": 1})
        emit("answer", {"text": "42"})
        return {"text": "42", "results": []}

    client = TestClient(create_app(state_dir=tmp_path, runner=runner, now=clock))
    sid = _new(client)["sid"]
    r = client.post("/turns", json={"sid": sid, "dataset_id": "ds1", "question": "how many?"})
    assert r.status_code == 202
    tid = r.json()["turn_id"]
    t = _wait(client, tid)
    assert t["status"] == "done" and [e["type"] for e in t["events"]] == \
        ["plan", "tool_call", "answer"]
    for _ in range(5):
        assert client.get(f"/turns/{tid}").json() == t
    assert client.get(f"/sessions/{sid}/turns").json()["turns"] == [t]
    assert calls == {"tool": 1, "runs": 1}

    # a restart (new app on the same state) serves the stored turn without running it
    client2 = TestClient(create_app(state_dir=tmp_path, runner=runner, now=clock))
    assert client2.get(f"/turns/{tid}").json() == t
    assert calls == {"tool": 1, "runs": 1}


def test_a_turn_running_at_restart_is_interrupted_not_rerun(tmp_path, clock):
    import threading
    gate, runs = threading.Event(), []

    def slow(turn, emit):
        runs.append(1)
        emit("plan", {"playbook": None, "steps": []})
        gate.wait(5)
        return {"text": "late"}

    app1 = create_app(state_dir=tmp_path, runner=slow, now=clock)
    c1 = TestClient(app1)
    sid = _new(c1)["sid"]
    tid = c1.post("/turns", json={"sid": sid, "dataset_id": "d", "question": "q"}).json()[
        "turn_id"]
    _wait(c1, tid, until=("running",))
    c2 = TestClient(create_app(state_dir=tmp_path, runner=slow, now=clock))
    assert c2.get(f"/turns/{tid}").json()["status"] == "interrupted"
    gate.set()
    app1.state.turns.wait(tid)
    assert runs == [1]


def test_default_runner_is_honest_about_no_agent(client):
    sid = _new(client)["sid"]
    tid = client.post("/turns", json={"sid": sid, "dataset_id": "d", "question": "q"}).json()[
        "turn_id"]
    t = _wait(client, tid)
    assert t["status"] == "failed" and t["events"][-1]["data"]["code"] == "agent_not_wired"


def test_runner_crash_is_recorded(tmp_path, clock):
    def boom(turn, emit):
        raise ValueError("bad")
    c = TestClient(create_app(state_dir=tmp_path, runner=boom, now=clock))
    sid = _new(c)["sid"]
    tid = c.post("/turns", json={"sid": sid, "dataset_id": "d", "question": "q"}).json()[
        "turn_id"]
    t = _wait(c, tid)
    assert t["status"] == "failed" and "ValueError" in t["events"][-1]["data"]["message"]


def test_turn_needs_a_live_session(client):
    r = client.post("/turns", json={"sid": "nope", "dataset_id": "d", "question": "q"})
    assert r.status_code == 404


def test_unknown_turn_is_404(client):
    assert client.get("/turns/t_nope").status_code == 404


# --- 501 stubs -----------------------------------------------------------------------------

STUBS = [("post", "/workspaces/ws1/uploads"), ("get", "/datasets/d/profile"),
         ("get", "/datasets/d/cleaning/proposals"), ("post", "/datasets/d/cleaning/approve"),
         ("get", "/datasets/d/domains/detect"), ("post", "/datasets/d/domains/confirm"),
         ("get", "/datasets/d/contract/proposal"), ("post", "/datasets/d/contract/confirm"),
         ("get", "/datasets/d/metrics/templates"), ("post", "/datasets/d/metrics/approve"),
         ("get", "/datasets/d/validity-rules"), ("post", "/datasets/d/validity-rules/approve"),
         ("get", "/datasets/d/tools"), ("post", "/tools/marketing.x/run"), ("get", "/packs"),
         ("get", "/packs/core"), ("get", "/datasets/d/keyword-groups"),
         ("post", "/datasets/d/keyword-groups/actions")]


@pytest.mark.parametrize("method,path", STUBS)
def test_stub_is_501_in_the_error_shape(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 501
    e = r.json()["error"]
    assert e["code"] == "not_implemented" and e["milestone"] in {"B2", "B3", "B7"}


def test_ui_state_scan_is_linear_on_a_max_size_string():
    """C1: an unbounded email regex took 63 s on 256 KB with no '@'."""
    from backend.services.sessions import ui_state_problem
    t0 = time.perf_counter()
    assert ui_state_problem({"b": "x" * UI_STATE_MAX_BYTES}) is None
    assert time.perf_counter() - t0 < 1.0
