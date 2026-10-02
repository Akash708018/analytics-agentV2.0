"""B11 (issue #22): core analyses run directly, and reports run playbooks with no model."""
from __future__ import annotations

import pytest

from backend.engine import workspace
from backend.tests.test_logistics import _load


@pytest.fixture
def env(tmp_path):
    from backend.tests.test_playbooks import make_env
    yield from make_env(tmp_path)


def test_the_analyses_list_has_fields_with_the_contracts_choices(env):
    ws, did, _, _ = _load(env, metrics=("sla_breach",))
    try:
        a = {x["name"]: x for x in env.get(f"/datasets/{did}/analyses").json()["analyses"]}
        assert len(a) == 27
        pc = {f["name"]: f for f in a["period_compare"]["fields"]}
        assert [n for n, f in pc.items() if f["required"]] == ["measure", "period", "baseline"]
        assert "distance_km" in pc["measure"]["choices"] and "grain" in pc
        assert "groups" in {f["name"] for f in a["group_compare"]["fields"]}
    finally:
        workspace.reset(ws)


def test_a_person_runs_a_core_analysis_directly(env):
    ws, did, _, _ = _load(env, metrics=("sla_breach",))
    run = lambda p: env.post("/tools/core.group_compare/run",            # noqa: E731
                             json={"dataset_id": did, "params": p})
    try:
        r = run({"dimension": "hub", "measure": "distance_km"})
        assert r.status_code == 200, r.text
        assert r.json()["result_id"] and r.json()["figures"]
        r = run({"dimension": "hub"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "param_required"
        r = run({"dimension": "hub", "measure": "distance_km", "where": "1=1"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_params"
        r = env.post("/tools/core.profile/run", json={"dataset_id": did, "params": {}})
        assert r.status_code in (404, 422)
    finally:
        workspace.reset(ws)


def test_a_report_runs_a_playbook_without_the_model(env):
    ws, did, _, _ = _load(env, metrics=("sla_breach", "rto_rate"))
    try:
        r = env.post(f"/datasets/{did}/reports", json={"playbook": "sla_where_and_why"})
        assert r.status_code == 200, r.text
        rep = r.json()
        assert [x["tool_id"] for x in rep["results"]] == [
            "logistics.sla_compliance", "logistics.sla_drivers", "logistics.rto_analysis"]
        assert {x["run_id"] for x in rep["results"]} == {rep["run_id"]}
        r = env.post(f"/datasets/{did}/reports", json={"playbook": "nope"})
        assert r.status_code == 404
    finally:
        workspace.reset(ws)


def test_a_blocked_report_says_what_to_approve(env):
    ws, did, _, _ = _load(env, metrics=())
    try:
        r = env.post(f"/datasets/{did}/reports", json={"playbook": "sla_where_and_why"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "playbook_blocked"
        assert "Approve the SLA" in r.json()["recovery"]
    finally:
        workspace.reset(ws)
