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
