"""B9: the Future Concepts doc's "Implement now" items that fit the finished backend."""
from __future__ import annotations

import pytest

from backend.engine import workspace
from backend.packs.loader import load_all, merge
from backend.playbooks.agent import Agent
from backend.tests.test_logistics import _load
from backend.tests.test_playbooks import Script, make_env


@pytest.fixture
def env(tmp_path):
    yield from make_env(tmp_path)


# --- rank 10: playbook routing ------------------------------------------------------------------

def _pbs(*ids):
    m = merge(load_all(), ["marketing", "logistics"])
    return {i: m.playbooks[i] for i in ids}


def test_route_needs_two_pattern_words_and_a_clear_lead():
    pbs = _pbs("sla_where_and_why", "courier_scorecard", "where_is_spend_wasted")
    b, slots = Agent.route("Which hub is worst on SLA, and what goes with it?", pbs)
    assert b.id == "sla_where_and_why" and slots == {}
    assert Agent.route("which courier is late?", pbs) is None          # one word: ask the model
    assert Agent.route("hello", pbs) is None
    b, _ = Agent.route("where are we wasting spend?", pbs)        # wasting ~ wasted
    assert b.id == "where_is_spend_wasted"


def test_route_fills_slots_only_when_the_question_pins_them():
    pbs = _pbs("why_roas_dropped")
    b, slots = Agent.route("ROAS fell in 2026-01 vs 2025-12, why?", pbs)
    assert slots == {"period": "2026-01", "baseline": "2025-12"}
    assert Agent.route("ROAS fell last month, why?", pbs) is None    # months not pinned


def test_routed_sla_question_costs_one_llm_call(env):
    ws, did, _, _ = _load(env, metrics=("sla_breach", "rto_rate"))
    try:
        t = env.ask(did, "Which hub is worst on SLA, and what goes with it?",
                    script=Script({"playbook": None, "slots": {}}))   # planner would say none
        plan = next(e["data"] for e in t["events"] if e["type"] == "plan")
        assert plan["playbook"] == "sla_where_and_why" and plan["routed_by"] == "rules"
        assert t["answer"]["usage"]["llm_calls"] == 1, t["answer"]["usage"]
    finally:
        workspace.reset(ws)


# --- rank 12: column meaning match ---------------------------------------------------------------

def test_abbreviated_and_unit_suffixed_columns_bind():
    from backend.packs.detector import bind
    m = merge(load_all(), ["marketing", "logistics"])
    b = bind(["amt_spent_inr", "qty_ordered_nos", "conv_value_inr", "Impr", "delivery_mins"], m)
    assert b["spend"] == ["amt_spent_inr"] and b["qty_ordered"] == ["qty_ordered_nos"]
    assert b["conv_value"] == ["conv_value_inr"] and b["impressions"] == ["Impr"]
    assert b["delivery_minutes"] == ["delivery_mins"]


# --- rank 9: boundary-aware playbooks -----------------------------------------------------------

def test_blocked_playbook_answers_with_recovery_and_runs_nothing(env):
    ws, did, _, _ = _load(env, metrics=())
    try:
        t = env.ask(did, "Which hub is worst on SLA, and what goes with it?",
                    script=Script({"playbook": None, "slots": {}}))
        a = t["answer"]
        assert a["usage"]["llm_calls"] == 0 and a["usage"]["tool_calls"] == 0
        assert "the approved metric 'sla_breach'" in a["text"] and "Approve the SLA" in a["text"]
    finally:
        workspace.reset(ws)


# --- rank 13: contract pre-fill -----------------------------------------------------------------

def _upload(c, ws, name):
    from backend.tests.test_logistics import FIXTURE
    return c.post(f"/workspaces/{ws}/uploads", files={"file": (
        name, FIXTURE.read_bytes())}).json()["dataset_id"]


def test_second_export_is_prefilled_from_the_first_in_the_same_workspace(env):
    ws, first, _, _ = _load(env, metrics=("sla_breach",))
    try:
        second = _upload(env, ws, "logistics_sla_september.csv")
        p = env.get(f"/datasets/{second}/contract/proposal").json()["prefill"]
        assert p["from_dataset_id"] == first and p["similarity"] == 1.0
        assert p["contract"]["date_column"] == "order_date"
        assert "recorded_delivery_minutes" in p["contract"]["measures"]
        assert p["domains"] == ["logistics"] and p["metrics"] == ["sla_breach"]
        assert "analysis_window_start" not in p["contract"]
        # a suggestion only: the second dataset has no contract until the person confirms
        r = env.post("/tools/logistics.sla_compliance/run",
                     json={"dataset_id": second, "params": {"by": "hub"}})
        assert r.status_code in (409, 422)
        other = _upload(env, "ws_" + "b9other0001", "logistics_sla.csv")
        assert env.get(f"/datasets/{other}/contract/proposal").json()["prefill"] is None
        workspace.reset("ws_b9other0001")
    finally:
        workspace.reset(ws)


# --- ranks 1 and 16: embedding version contract + keyword carry-forward -------------------------

def _kw_upload(c, ws, kws):
    import csv
    import io
    buf = io.StringIO()
    csv.writer(buf).writerows([["row_id", "date", "query", "page", "clicks", "impressions",
                                "position"],
                               *[[i, "2026-09-01", k, "/p", 5, 50, 3] for i, k in
                                 enumerate(kws)]])
    return c.post(f"/workspaces/{ws}/uploads", files={"file": (
        "gsc_kw.csv", buf.getvalue().encode())}).json()["dataset_id"]


def test_new_month_keywords_join_approved_groups_within_one_generation(env):
    import secrets
    from backend.tests.fixtures_v2 import keywords_gold as G
    ws = f"ws_{secrets.token_hex(6)}"
    try:
        did = _kw_upload(env, ws, G.KEYWORDS)
        env.post(f"/datasets/{did}/domains/confirm", json={"domains": ["marketing"]})
        got = env.post(f"/datasets/{did}/keyword-groups/run",
                       json={"backend": "chargram"}).json()
        v = got["run"]["embedding_version"]
        assert v["embedding"].startswith("chargram") and v["normalisation"] == "l2"
        assert len(v["generation"]) == 12
        g = next(x for x in got["groups"] if "sushi delivery pune" in x["keywords"])
        assert g["generation"] == v["generation"]
        env.post(f"/datasets/{did}/keyword-groups/actions",
                 json={"action": "approve", "group_ids": [g["group_id"]]})
        # next month: the same export plus new phrasings
        did2 = _kw_upload(env, ws, G.KEYWORDS + ["sushi delivery wakad", "sushi delivry aundh"])
        assert did2 == did
        got = env.post(f"/datasets/{did}/keyword-groups/run",
                       json={"backend": "chargram"}).json()
        assert got["run"]["carry_forward"]["status"] == "ok"
        join = next(x for x in got["groups"] if x["joins"] == g["group_id"])
        assert "sushi delivery wakad" in join["keywords"] and not join["approved"]
        r = env.post(f"/datasets/{did}/keyword-groups/actions", json={
            "action": "merge", "group_ids": [g["group_id"], join["group_id"]]}).json()
        merged = next(x for x in r["groups"] if x["group_id"] == g["group_id"])
        assert merged["approved"] and "sushi delivery wakad" in merged["keywords"]
    finally:
        workspace.reset(ws)


def test_a_generation_switch_disables_carry_forward_and_says_so(env):
    import secrets
    ws = f"ws_{secrets.token_hex(6)}"
    try:
        did = _kw_upload(env, ws, ["sushi delivery pune", "sushi home delivery",
                                   "sushi near me", "ramen near me"])
        env.post(f"/datasets/{did}/domains/confirm", json={"domains": ["marketing"]})
        got = env.post(f"/datasets/{did}/keyword-groups/run",
                       json={"backend": "chargram"}).json()
        gid = got["groups"][0]["group_id"]
        env.post(f"/datasets/{did}/keyword-groups/actions",
                 json={"action": "approve", "group_ids": [gid]})
        from backend.services import keywords as K
        orig = K.KeywordService._rows

        def rows(self, d):                     # an approval recorded under another generation
            return [{**g, "generation": "0ld0ld0ld0ld"} if g["approved"] else g
                    for g in orig(self, d)]
        K.KeywordService._rows = rows
        try:
            _kw_upload(env, ws, ["sushi delivery pune", "sushi home delivery",
                                 "sushi near me", "ramen near me", "sushi delivery wakad"])
            got = env.post(f"/datasets/{did}/keyword-groups/run",
                           json={"backend": "chargram"}).json()
        finally:
            K.KeywordService._rows = orig
        cf = got["run"]["carry_forward"]
        assert cf["status"] == "disabled" and "0ld0ld0ld0ld" in cf["reason"]
        assert not any(x["joins"] for x in got["groups"])
    finally:
        workspace.reset(ws)
