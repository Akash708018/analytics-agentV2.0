"""B5: each marketing playbook through a real turn (POST /turns) with a scripted LLM.

The planner's reply is scripted (which playbook, which slots). The explainer is a function that
writes only from the results it is shown -- quoting figure lines and naming caveats -- so what
is tested is the pipeline: plan -> engine steps -> explain -> checks, and the numbers it costs.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.engine import workspace
from backend.llm.provider import ScriptedLLM
from backend.tests.test_shared_v2 import EVENTS

FIX = Path(__file__).resolve().parent / "fixtures_v2"
FORKS = {"tax_basis": "net_excl_gst", "fiscal_year": "april", "timezone": "ist",
         "conversion_source": "platform", "roas_revenue_basis": "net_excl_gst",
         "attribution_window": "7d_click_1d_view", "cac_scope": "paid_media_only",
         "email_rate_denominator": "delivered", "gsc_aggregation": "by_property"}
COSTS: dict[str, dict] = {}


def explainer(bad_first: str | None = None):
    """Writes from the results only. With `bad_first`, the first explanation breaks a rule."""
    state = {"n": 0}

    def reply(system: str, user: str) -> str:
        if system.startswith("You route"):
            raise AssertionError("planner replies are scripted")
        state["n"] += 1
        if bad_first and state["n"] == 1:
            return bad_first
        figs = [ln[2:] for ln in system.splitlines() if ln.startswith("- ")][:3]
        cav = [ln for ln in system.splitlines() if ln.startswith("caveat: ")]
        out = ["Here is what the data shows: " + "; ".join(figs) + "."]
        for c in cav:
            if "festival_confound" in c:
                out.append("The period includes " + c.split(":", 2)[2].strip().split(" (")[0]
                           + ", so part of the change may be the festival.")
            if "measurement_change" in c:
                out.append("A measurement change falls in the period ("
                           + re.search(r"\d{4}-\d{2}-\d{2}", c).group(0) + ").")
            if "minimum group size" in c or "NOT ENOUGH DATA" in c:
                out.append("Some groups were too small to judge and are suppressed.")
        if "Skipped:" in system:
            out.append("Some steps could not run on this data.")
        return " ".join(dict.fromkeys(out))
    return reply


class Script:
    def __init__(self, plan: dict, bad_first: str | None = None):
        self.plan, self.explain = plan, explainer(bad_first)

    def __call__(self, system, user):
        if system.startswith("You route"):
            return json.dumps(self.plan)
        return self.explain(system, user)


@pytest.fixture
def env(tmp_path):
    yield from make_env(tmp_path)


def make_env(tmp_path):
    """The harness as a plain generator, so bench/compare.py can run the same scenarios."""
    holder = {}
    spaces = []

    def llm(system, user):
        return holder["script"](system, user)

    scripted = ScriptedLLM(llm)
    c = TestClient(create_app(state_dir=tmp_path, llm=scripted))

    def load(name, header, rows, *, key, dims, window, measures=None, metrics=(), rules=()):
        ws = f"ws_{secrets.token_hex(6)}"
        spaces.append(ws)
        buf = io.StringIO()
        csv.writer(buf).writerows([header, *rows])
        did = c.post(f"/workspaces/{ws}/uploads", files={"file": (
            f"{name}.csv", buf.getvalue().encode())}).json()["dataset_id"]
        c.post(f"/datasets/{did}/domains/confirm", json={"domains": ["marketing"]})
        p = c.get(f"/datasets/{did}/contract/proposal").json()
        ms = measures or [m["column"] for m in p["measures"]]
        agg = {m: next((x["suggested_agg"] for x in p["measures"] if x["column"] == m
                        and x["suggested_agg"]), "sum") for m in ms}
        r = c.post(f"/datasets/{did}/contract/confirm", json={"contract": {
            "grain": "one row per record", "primary_key": key, "date_column": "date",
            "measures": ms, "dimensions": dims, "aggregations": agg,
            "measure_definitions": {m: m for m in ms},
            "analysis_window_start": window[0], "analysis_window_end": window[1]},
            "fork_choices": {f["fork_id"]: FORKS[f["fork_id"]] for f in p["forks"]}})
        assert r.status_code == 200, r.text
        for t in metrics:
            assert c.post(f"/datasets/{did}/metrics/approve", json={
                "template_id": t, "bindings": {}}).status_code == 200, t
        if rules:
            c.post(f"/datasets/{did}/validity-rules/approve", json={"approve": list(rules)})
        return did

    def ask(did, question, plan=None, bad_first=None, script=None):
        holder["script"] = script or Script(plan, bad_first)
        sid = c.post("/sessions", json={}).json()["sid"]
        tid = c.post("/turns", json={"sid": sid, "dataset_id": did,
                                     "question": question}).json()["turn_id"]
        for _ in range(600):
            t = c.get(f"/turns/{tid}").json()
            if t["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        assert t["status"] == "done", t["events"][-3:]
        return t

    c.load, c.ask = load, ask
    yield c
    for ws in spaces:
        workspace.reset(ws)


def record(name, turn):
    u = turn["answer"]["usage"]
    COSTS[name] = {k: u[k] for k in ("llm_calls", "tool_calls", "tokens_in_est",
                                     "tokens_out_est")}


def ads(env):
    rows = list(csv.reader(open(FIX / "ads_traps.csv")))
    return env.load("ads_traps", rows[0], rows[1:], key=["row_id"],
                    dims=["campaign", "channel", "device", "city"],
                    window=("2025-10-01", "2026-01-31"),
                    measures=["impressions", "clicks", "cost", "conversions", "conv_value",
                              "order_revenue", "gst"],
                    metrics=("ctr", "cpc", "cpa", "roas", "cvr"),
                    rules=("exclude_test_campaigns", "zero_impression_spend",
                           "roas_spend_positive"))


def tools_run(turn):
    return [e["data"]["tool_id"] for e in turn["events"] if e["type"] == "tool_call"
            and e["data"]["status"] == "ok"]


def test_why_roas_dropped(env):
    did = ads(env)
    t = env.ask(did, "Why did ROAS change in January vs December?",
                {"playbook": "why_roas_dropped", "slots": {"period": "2026-01",
                                                           "baseline": "2025-12"}})
    a = t["answer"]
    assert a["playbook"] == "why_roas_dropped"
    assert tools_run(t) == ["marketing.roas_change_explainer", "marketing.channel_mix_shift",
                            "marketing.channel_efficiency"], a["skipped"]
    roas = {f["name"]: f["value"] for f in a["results"][0]["figures"]}
    assert roas["ROAS then vs now: 2026-01"] > roas["ROAS then vs now: 2025-12"]   # rose
    assert a["flags"] == [] and "2026-01-12" in a["text"]      # the Meta change is named
    assert a["usage"]["llm_calls"] == 2
    record("why_roas_dropped", t)


def test_where_is_spend_wasted(env):
    did = ads(env)
    t = env.ask(did, "Where are we wasting spend?", {"playbook": "where_is_spend_wasted",
                                                      "slots": {}})
    ran = tools_run(t)
    assert ran[:2] == ["marketing.spend_waste", "marketing.device_geo_split"]
    assert "Nashik" not in json.dumps([f for f in t["answer"]["results"][1]["figures"]
                                       if f["value"] is not None])
    assert "too small" in t["answer"]["text"]                 # small_sample honoured
    skipped = [s["tool_id"] for s in t["answer"]["skipped"]]
    assert skipped == ["marketing.keyword_ngrams"]            # no search terms in this file
    record("where_is_spend_wasted", t)


def test_did_the_campaign_work_is_worded_as_association(env):
    s = date(2025, 10, 1)
    rows = [[(s + timedelta(days=d)).isoformat(), 100 if d < 28 else 130] for d in range(56)]
    did = env.load("impact", ["date", "order_revenue"], rows, key=["date"], dims=[],
                   window=("2025-10-01", "2025-11-25"), measures=["order_revenue"])
    bad = "The campaign drove a 30% lift in revenue."
    t = env.ask(did, "Did the October 29 campaign work?",
                {"playbook": "did_the_campaign_work", "slots": {"start": "2025-10-29"}},
                bad_first=bad)
    a = t["answer"]
    kinds = [(e["type"], e["data"].get("rule"), e["data"].get("status")) for e in t["events"]
             if e["type"] == "interpretation_check"]
    assert ("interpretation_check", "correlational_only", "violated") in kinds
    assert a["flags"] == [] and "drove" not in a["text"]      # the one correction fixed it
    assert "Diwali" in a["text"]
    assert a["usage"]["llm_calls"] == 3                       # planner, explainer, correction
    record("did_the_campaign_work", t)


def test_unresolved_violation_is_flagged(env):
    s = date(2025, 10, 1)
    rows = [[(s + timedelta(days=d)).isoformat(), 100 if d < 28 else 130] for d in range(56)]
    did = env.load("impact2", ["date", "order_revenue"], rows, key=["date"], dims=[],
                   window=("2025-10-01", "2025-11-25"), measures=["order_revenue"])

    class Stubborn(Script):
        def __call__(self, system, user):
            if system.startswith("You route"):
                return json.dumps(self.plan)
            return "The campaign drove a lift; Diwali was in the period."
    t = env.ask(did, "Did it work?", script=Stubborn(
        {"playbook": "did_the_campaign_work", "slots": {"start": "2025-10-29"}}))
    assert any(f.startswith("[correlational_only]") for f in t["answer"]["flags"])
    assert t["answer"]["usage"]["llm_calls"] == 3             # one correction, never a second


def test_funnel_leak(env):
    names = {"view": "view_item", "cart": "add_to_cart", "checkout": "begin_checkout",
             "purchase": "purchase"}
    rows = [[i, d, u, names[e]] for i, (u, d, e) in enumerate(EVENTS)]
    did = env.load("ga4", ["row_id", "date", "user_pseudo_id", "event_name"], rows,
                   key=["row_id"], dims=["event_name", "user_pseudo_id"],
                   window=("2026-01-01", "2026-01-31"))
    t = env.ask(did, "Where does our checkout funnel leak?", {"playbook": "funnel_leak",
                                                               "slots": {}})
    res = t["answer"]["results"][0]
    assert [f["value"] for f in res["figures"] if "[" not in f["name"]] == [5, 3, 2, 1]
    assert "view_item: 5" in t["answer"]["text"]
    record("funnel_leak", t)


def test_email_health(env):
    rows = []
    for w in range(8):
        for camp in ("Weekly", "Promo"):
            sent = 10000
            bounces = 150 + 30 * w if camp == "Promo" else 120
            rows.append([(date(2026, 5, 4) + timedelta(weeks=w)).isoformat(), camp, sent,
                         sent - bounces, 3000, 400, 20 + (10 * w if camp == "Promo" else 0),
                         bounces, 2])
    did = env.load("email", ["date", "campaign", "sent", "delivered", "opens", "clicks",
                             "unsubscribes", "bounces", "complaints"], rows,
                   key=["date", "campaign"], dims=["campaign"],
                   window=("2026-05-04", "2026-06-28"),
                   measures=["sent", "delivered", "opens", "clicks", "unsubscribes", "bounces",
                             "complaints"],
                   metrics=("email_click_rate", "ctor", "unsubscribe_rate", "bounce_rate",
                            "complaint_rate"))
    t = env.ask(did, "How healthy is our email list?", {"playbook": "email_health",
                                                         "slots": {}})
    assert tools_run(t) == ["marketing.email_performance", "marketing.list_health"]
    clicks = {f["name"]: f["value"] for f in t["answer"]["results"][0]["figures"]}
    promo = 100 * 400 * 8 / sum(r[3] for r in rows if r[1] == "Promo")   # clicks / delivered
    assert clicks["Click rate: Promo"] == pytest.approx(promo, abs=1e-3)
    record("email_health", t)


def test_why_organic_traffic_dropped(env):
    rows = []
    for d in range(60):
        day = date(2026, 4, 1) + timedelta(days=d)
        for q, page, pos, imp in (("sushi pune", "/sushi", 3, 400), ("ramen pune", "/ramen", 7,
                                                                       300),
                                  ("sushi near me", "/sushi", 12, 200),
                                  ("sushi near me", "/menu", 14, 150)):
            clicks = max(1, imp * (12 - pos) // 100 - (d // 10 if page == "/ramen" else 0))
            rows.append([day.isoformat(), q, page, clicks, imp, pos])
    did = env.load("gsc", ["date", "query", "page", "clicks", "impressions", "position"], rows,
                   key=["date", "query", "page"], dims=["query", "page"],
                   window=("2026-04-01", "2026-05-30"),
                   measures=["clicks", "impressions", "position"], metrics=("ctr",))
    t = env.ask(did, "Why did organic clicks drop in May?",
                {"playbook": "why_organic_traffic_dropped",
                 "slots": {"period": "2026-05", "baseline": "2026-04"}})
    ran = tools_run(t)
    assert ran[0] == "marketing.seo_change_explainer" and "marketing.cannibalization" in ran
    canni = next(r for r in t["answer"]["results"] if r["tool_id"] == "marketing.cannibalization")
    assert any(f["name"].startswith("key_overlap: sushi near me") for f in canni["figures"])
    record("why_organic_traffic_dropped", t)


def test_no_playbook_falls_back_to_active_tools_only(env):
    did = ads(env)
    seen = []
    replies = iter([json.dumps({"playbook": None, "slots": {}}),
                    json.dumps({"call": "marketing_channel_efficiency",
                                "params": {"by": "device"}}),
                    json.dumps({"done": True})])

    class Fallback(Script):
        def __call__(self, system, user):
            seen.append(system)
            if system.startswith(("You route", "You answer with tools")):
                return next(replies)
            return self.explain(system, user)
    t = env.ask(did, "Anything odd about devices?", script=Fallback({}))
    offered = seen[1]
    assert "marketing_channel_efficiency" in offered
    assert "marketing_utm_hygiene" not in offered            # not active: no UTM columns
    assert tools_run(t) == ["marketing.channel_efficiency"]
    assert t["answer"]["playbook"] is None
    record("fallback (no playbook)", t)


def teardown_module(module):
    if COSTS:
        print("\nper question (estimates):")
        for k, v in COSTS.items():
            print(f"  {k:28s} {v}")
