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


# --- ranks 2, 5, 7: typed results, lineage and staleness, inspection ----------------------------

def test_tool_result_is_typed_stored_and_goes_stale_when_the_data_changes(env):
    from backend.tests.test_logistics import FIXTURE
    ws, did, _, _ = _load(env, metrics=("sla_breach",))
    try:
        r = env.post("/tools/logistics.sla_compliance/run",
                     json={"dataset_id": did, "params": {"by": "hub"}}).json()
        assert r["result_id"].startswith("r_") and r["run_id"] == r["result_id"]
        assert r["status"] == "ok" and r["snapshot"]["rows"] == 492
        assert r["contract_version"] >= 1 and r["grain"] == "one row = one order"
        assert r["metrics_used"] == {"sla_breach": {"measure": "sla_breach"}}
        got = env.get(f"/results/{r['result_id']}").json()
        assert got["stale"] is False and got["figures"] == r["figures"]
        # a corrected upload of the same export: one order's minutes fixed
        text = FIXTURE.read_text().replace(",92,Delivered,", ",93,Delivered,", 1)
        env.post(f"/workspaces/{ws}/uploads", files={"file": ("logistics_sla.csv",
                                                              text.encode())})
        listed = env.get(f"/datasets/{did}/results").json()["results"]
        mine = next(x for x in listed if x["result_id"] == r["result_id"])
        assert mine["stale"] and any("the data changed" in w for w in mine["stale_reasons"])
    finally:
        workspace.reset(ws)


def test_inspection_reaches_rows_past_the_figure_cap_sorted(env):
    ws, did, _, _ = _load(env, metrics=("sla_breach",))
    try:
        r = env.post("/tools/logistics.sla_compliance/run",
                     json={"dataset_id": did, "params": {"by": "zone"}}).json()
        i = env.get(f"/results/{r['result_id']}/inspect",
                    params={"sort_by": "n", "limit": 3}).json()
        assert i["step"] == "Breach rate" and len(i["rows"]) == 3
        n = [int(str(x[i["headers"].index("n")]).replace(",", "")) for x in i["rows"]]
        assert n == sorted(n, reverse=True)
        one = env.get(f"/results/{r['result_id']}/inspect", params={"group": "wakad"}).json()
        assert one["total_rows"] == 1 and str(one["rows"][0][0]).lower() == "wakad"
        bad = env.get(f"/results/{r['result_id']}/inspect", params={"sort_by": "nope"})
        assert bad.status_code == 422 and "columns" in bad.text
        assert env.get("/results/r_missing/inspect").status_code == 404
    finally:
        workspace.reset(ws)


def test_turn_results_carry_the_turn_id_as_run_id(env):
    ws, did, _, _ = _load(env, metrics=("sla_breach", "rto_rate"))
    try:
        t = env.ask(did, "Which hub is worst on SLA, and what goes with it?",
                    script=Script({"playbook": None, "slots": {}}))
        rids = {x["run_id"] for x in t["answer"]["results"]}
        assert rids == {t["turn_id"]}
    finally:
        workspace.reset(ws)


def test_fallback_model_can_inspect_a_result(env):
    import json
    import re
    state = {"n": 0}

    def script(system, user):
        if system.startswith("You route"):
            return json.dumps({"playbook": None, "slots": {}})
        if system.startswith("You answer with tools"):
            state["n"] += 1
            if state["n"] == 1:
                return json.dumps({"calls": [{"call": "logistics_sla_compliance",
                                              "params": {"by": "zone"}}]})
            rid = re.search(r"result_id (r_[0-9a-f]+)", user).group(1)
            return json.dumps({"calls": [{"call": "inspect_result", "params": {
                "result_id": rid, "sort_by": "n", "limit": 2}}], "then": "answer"})
        return "The two largest zones are listed in the inspection."
    ws, did, _, _ = _load(env, metrics=("sla_breach",))
    try:
        t = env.ask(did, "tell me about zones please", script=script)
        calls = [e["data"] for e in t["events"] if e["type"] == "tool_call"]
        assert [c["tool_id"] for c in calls] == ["logistics.sla_compliance", "inspect_result"]
        assert calls[1]["status"] == "ok"
    finally:
        workspace.reset(ws)


# --- rank 6: claim validation -------------------------------------------------------------------

def test_claims_are_checked_for_unit_and_direction():
    from backend.rules import interpret
    trace = [{"tool_id": "logistics.sla_compliance", "figures": [
        {"name": "Breach rate: Pune_South", "value": 0.638, "unit": "total"},
        {"name": "Revenue change by group: google [change]", "value": -12.0, "unit": "change"}]}]
    rules = lambda t: {v.rule for v in interpret.check(t, trace)}   # noqa: E731
    assert "claim_unit" in rules("Pune_South breached on 0.638% of orders.")
    assert "claim_unit" not in rules("Pune_South breached on 63.8% of orders.")
    assert "claim_direction" in rules("Google revenue rose by 12 this month.")
    assert "claim_direction" not in rules("Google revenue fell by 12 this month.")


# --- rank 14: value matching --------------------------------------------------------------------

def test_focus_values_match_spellings_aliases_and_say_when_ambiguous(env):
    ws, did, _, _ = _load(env, metrics=("sla_breach",))
    run = lambda **p: env.post("/tools/logistics.sla_drivers/run",   # noqa: E731
                               json={"dataset_id": did, "params": {"by": "hub", **p}})
    try:
        r = run(focus="pune south")
        assert r.status_code == 200
        assert r.json()["figures"]                       # resolved to Pune_South's spelling
        r = run(focus="south")
        assert r.status_code == 200 and any("read as hub =" in c for c in r.json()["caveats"])
        r = run(focus="pune")
        assert r.status_code == 422 and r.json()["error"]["code"] == "ambiguous_value"
        r = run(focus="nagpur")
        assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_value"
    finally:
        workspace.reset(ws)


def test_aliases_are_pack_knowledge():
    m = merge(load_all(), ["marketing"])
    assert m.value_aliases["bombay"] == "mumbai" and m.value_aliases["insta"] == "instagram"
