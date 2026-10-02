"""B10: the frontend's api-request issues (#23, #17, #16, #21; #20 is in test_keywords)."""
from __future__ import annotations

import pytest

from backend.packs.loader import PackError, load_all, merge


# --- #23: optional params are declared ----------------------------------------------------------

def test_optional_params_are_declared_in_the_tool_spec(tmp_path):
    from fastapi.testclient import TestClient
    from backend.api.app import create_app
    c = TestClient(create_app(state_dir=tmp_path))
    tools = {t["id"]: t for t in c.get("/packs/logistics").json()["pack"]["tools"]}
    focus = tools["logistics.sla_drivers"]["params_optional"]
    assert [p["name"] for p in focus] == ["focus"] and focus[0]["default"] is None
    days = tools["logistics.stuck_shipments"]["params_optional"][0]
    assert days["name"] == "days" and days["default"] == "3"


def test_a_step_reading_an_undeclared_param_fails_pack_load():
    from backend.packs.models import Pack, Step, Tool
    packs = dict(load_all())
    lg = packs["logistics"]
    t = lg.tools[0]
    bad = Tool(**{**t.model_dump(), "params_optional": [],
                  "steps": [Step(analysis="group_compare",
                                 params={"dimension": "@param?:secret", "measure": "x"})]})
    packs["logistics"] = Pack(**{**lg.model_dump(), "tools": [bad.model_dump()]})
    with pytest.raises(PackError, match="optional param 'secret'"):
        merge(packs, ["logistics"])


def test_every_real_pack_declares_what_its_steps_read():
    from backend.packs.loader import step_params
    for pid in ("marketing", "logistics"):
        m = merge(load_all(), [pid])
        for t in m.tools.values():
            declared = set(t.params_required) | {p.name for p in t.params_optional}
            assert step_params(t) <= declared, t.id


# --- #17: the contract in force -----------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    from backend.tests.test_playbooks import make_env
    yield from make_env(tmp_path)


def test_contract_in_force_returns_what_the_person_confirmed(env):
    import secrets
    from backend.engine import workspace
    from backend.tests.test_logistics import FIXTURE, _load
    ws0 = f"ws_{secrets.token_hex(6)}"
    did0 = env.post(f"/workspaces/{ws0}/uploads", files={"file": (
        "logistics_sla.csv", FIXTURE.read_bytes())}).json()["dataset_id"]
    r = env.get(f"/datasets/{did0}/contract")
    assert r.status_code == 409 and r.json()["error"]["code"] == "contract_required"
    workspace.reset(ws0)
    ws, did, _, _ = _load(env, metrics=("sla_breach",))
    try:
        c = env.get(f"/datasets/{did}/contract").json()
        assert c["version"] >= 1 and c["date_column"] == "order_date"
        m = {x["column"]: x for x in c["measures"]}
        assert m["distance_km"]["definition"] == "distance km" and m["distance_km"]["agg"] == "mean"
        assert c["analysis_window_start"] == "2026-08-01"
        assert c["analysis_window_end"] == "2026-08-31"
        assert c["fork_choices"] and c["metrics"] == {"sla_breach": "sla_breach"}
        assert c["validity_rules"] == ["delivered_after_created"]
        assert c["measured_caveats"] and c["caveats"] == []        # never merged
    finally:
        workspace.reset(ws)


# --- #16: answer a refused upload's layout questions --------------------------------------------

def test_a_stacked_header_file_loads_after_its_answers(env):
    import secrets
    from pathlib import Path
    from backend.engine import workspace
    data = (Path(__file__).resolve().parent / "fixtures" / "multiheader.csv").read_bytes()
    ws = f"ws_{secrets.token_hex(6)}"
    try:
        r = env.post(f"/workspaces/{ws}/uploads", files={"file": ("multiheader.csv", data)})
        assert r.status_code == 422
        e = r.json()
        assert e["error"]["code"] == "ingest_needs_answers" and e["unresolved"] == ["header_rows"]
        uid, pv = e["upload_id"], e["preview"]
        assert pv["rows"][0][0] == "Identifiers" and pv["rows"][1][0] == "order_id"
        assert pv["guess"]["columns"]
        # the person: row 1 is a band of group labels, row 2 holds the names
        r = env.post(f"/workspaces/{ws}/uploads/{uid}/answers", json={
            "header_rows": [1, 2], "header_join": "bottom_only",
            "columns": [{"source": pv["guess"]["columns"][-1]["source"],
                         "target": "revenue_inr"}]})
        assert r.status_code == 201, r.text
        did = r.json()["dataset_id"]
        cols = [c["name"] for c in env.get(f"/datasets/{did}/profile").json()["columns"]]
        assert "order_id" in cols and "revenue_inr" in cols and "Identifiers" not in cols
        assert r.json()["rows"] > 0
        assert env.post(f"/workspaces/{ws}/uploads/nope.csv/answers",
                        json={"header_rows": [2]}).status_code == 404
        assert env.post(f"/workspaces/{ws}/uploads/..%2Fx/answers",
                        json={"header_rows": [2]}).status_code == 404
    finally:
        workspace.reset(ws)


# --- #21: cleaning proposals show what they lose ------------------------------------------------

def test_lossy_cleaning_steps_carry_samples_and_their_sql(env):
    import secrets
    from backend.engine import workspace
    from backend.tests.test_logistics import FIXTURE
    ws = f"ws_{secrets.token_hex(6)}"
    try:
        did = env.post(f"/workspaces/{ws}/uploads", files={"file": (
            "logistics_sla.csv", FIXTURE.read_bytes())}).json()["dataset_id"]
        props = {p["kind"] + ":" + str(p["column"]): p for p in
                 env.get(f"/datasets/{did}/cleaning/proposals").json()["proposals"]}
        dup = props["DROP_DUPLICATE_ROWS:None"]
        assert dup["lossy"] and dup["values_lost"] == 8 and dup["loss_unit"] == "row"
        assert len(dup["samples"]) == 3 and dup["samples"][0]["copies"] == 2
        assert "order_id" in dup["samples"][0]["row"]
        case = props["NORMALISE_CASE:hub"]
        assert {s["value"] for s in case["samples"]} == {"PUNE_WEST", "Pune_West"}
        assert case["sql"].startswith("CREATE OR REPLACE TABLE")
    finally:
        workspace.reset(ws)
