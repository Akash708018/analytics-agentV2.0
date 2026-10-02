"""B8: the logistics pack -- the 19 SLA checks of v1's scripts/sla_bench.py, reproduced through
the API's tools and the `sla_where_and_why` playbook (the user's key, 26/09/2026)."""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.engine import workspace
from backend.packs.loader import load_all, merge
from backend.rules import interpret
from backend.tests.test_playbooks import make_env

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "logistics_sla.csv"
TOL = 2.0                                         # percentage points, as the v1 bench
KEY_HUBS = {"Pune_South": (59, 93), "Pune_East": (58, 96), "Pune_West": (42, 85),
            "Pune_Central": (29, 77)}
KEY_SOUTH = {"By weather": {"heavy rain": (12, 13), "rain": (15, 23), "clear": (32, 57)},
             "By address quality": {"low": (10, 14), "medium": (22, 34), "high": (27, 45)},
             "By attempts": {"2": (12, 12)},
             "By payment mode": {"prepaid": (42, 60), "cod": (17, 33)}}
KEY_DISTANCE = (8.44, 8.64)
MEASURES = ["order_value_inr", "distance_km", "package_weight_kg", "recorded_delivery_minutes",
            "delivery_cost_inr"]
DIMS = ["hub", "zone", "rider_id", "payment_mode", "delivery_status", "attempt_count",
        "address_quality", "weather", "source_system", "promised_minutes"]


def _load(c, rules=("delivered_after_created",), metrics=("sla_breach",)):
    ws = f"ws_{secrets.token_hex(6)}"
    did = c.post(f"/workspaces/{ws}/uploads", files={"file": (
        "logistics_sla.csv", FIXTURE.read_bytes())}).json()["dataset_id"]
    det = c.get(f"/datasets/{did}/domains/detect").json()
    props = c.get(f"/datasets/{did}/cleaning/proposals").json()["proposals"]
    c.post(f"/datasets/{did}/cleaning/approve",
           json={"approve": [p["action_id"] for p in props]})
    c.post(f"/datasets/{did}/domains/confirm", json={"domains": ["logistics"]})
    p = c.get(f"/datasets/{did}/contract/proposal").json()
    r = c.post(f"/datasets/{did}/contract/confirm", json={"contract": {
        "grain": "one row = one order", "primary_key": [], "date_column": "order_date",
        "measures": MEASURES, "dimensions": DIMS,
        "aggregations": {m: "sum" if m.endswith("_inr") else "mean" for m in MEASURES},
        "measure_definitions": {m: m.replace("_", " ") for m in MEASURES},
        "analysis_window_start": "2026-08-01", "analysis_window_end": "2026-08-31"},
        "fork_choices": {f["fork_id"]: f["options"][0]["id"] for f in p["forks"]}})
    assert r.status_code == 200, r.text
    approved = {}
    for t in metrics:
        r = c.post(f"/datasets/{did}/metrics/approve", json={"template_id": t, "bindings": {}})
        assert r.status_code == 200, r.text
        approved[t] = r.json()
    if rules:
        c.post(f"/datasets/{did}/validity-rules/approve", json={"approve": list(rules)})
    return ws, did, det, approved


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    c = TestClient(create_app(state_dir=tmp_path_factory.mktemp("lg")))
    ws, did, det, approved = _load(c, metrics=("sla_breach", "rto_rate"))
    yield c, did, det, approved
    workspace.reset(ws)


def _run(c, did, tool, **params):
    r = c.post(f"/tools/{tool}/run", json={"dataset_id": did, "params": params})
    assert r.status_code == 200, r.text
    return r.json()


def _rates(res, title):
    """{group (lower): (rate %, n)} for one step's figures."""
    out = {}
    for f in res["figures"]:
        name = f["name"]
        if name.startswith(f"{title}: ") and "[" not in name:
            g = name[len(title) + 2:].lower()
            out[g] = (f["value"], None)
    return out


def _pct(v):
    return v * 100 if v is not None and v <= 1 else v


def test_pack_loads_and_detects_logistics(api):
    _, _, det, _ = api
    m = merge(load_all(), ["logistics"])
    assert len(m.tools) == 6 and set(m.playbooks) == {"sla_where_and_why", "courier_scorecard"}
    assert det["domains"][0]["domain"] == "logistics", det
    assert all(t.shape == "comparison" for t in m.templates.values()
               if t.id in ("sla_breach", "on_time", "in_full", "rto_rate"))


def test_metric_is_approved_as_a_provisional_comparison(api):
    _, _, _, approved = api
    a = approved["sla_breach"]
    assert a["measure"] == "sla_breach" and "recorded_delivery_minutes > promised_minutes" in \
        a["provisional"]


def test_hub_breach_rates_match_the_key_and_rank(api):
    c, did, _, _ = api
    res = _run(c, did, "logistics.sla_compliance", by="hub")
    got = _rates(res, "Breach rate")
    print("\nbreach rate by hub (engine vs key):")
    for hub, (b, n) in KEY_HUBS.items():
        rate, n_got = got[hub.lower()]
        print(f"  {hub:<13} {_pct(rate):5.1f}%   key {100 * b / n:5.1f}% of {n}")
        assert abs(_pct(rate) - 100 * b / n) <= TOL, hub
    ranking = sorted((h for h in got if h.startswith("pune")), key=lambda h: -got[h][0])
    assert ranking == ["pune_south", "pune_east", "pune_west", "pune_central"]
    assert "delivered_after_created" in res["validity_filters_applied"]
    assert any("PROVISIONAL" in x for x in res["caveats"])


def test_drivers_inside_the_worst_hub_chosen_by_the_engine(api):
    c, did, _, _ = api
    res = _run(c, did, "logistics.sla_drivers", by="hub")
    assert any(x.lower().startswith("focus: hub = pune_south") and "chosen by the engine" in x
               for x in res["caveats"])
    print("\nwithin pune_south:")
    for title, key in KEY_SOUTH.items():
        got = _rates(res, title)
        for value, (b, n) in key.items():
            rate, n_got = got[value]
            print(f"  {title} {value:<10} {_pct(rate):5.1f}%   key "
                  f"{100 * b / n:5.1f}% of {n}")
            assert abs(_pct(rate) - 100 * b / n) <= TOL, (title, value)
    weather = _rates(res, "By weather")
    assert max(weather, key=lambda k: weather[k][0] or 0) == "heavy rain"
    late = _rates(res, "Distance when late")["pune_south"][0]
    ok = _rates(res, "Distance when on time")["pune_south"][0]
    print(f"  distance late {late:.2f} km vs on time {ok:.2f} km (key {KEY_DISTANCE})")
    assert abs(late - KEY_DISTANCE[0]) <= 0.05 and abs(ok - KEY_DISTANCE[1]) <= 0.05
    assert late <= ok + 0.5                     # distance does not explain it


def test_the_person_can_pick_the_focus(api):
    c, did, _, _ = api
    res = _run(c, did, "logistics.sla_drivers", by="hub", focus="Pune_Central")
    assert not any(x.startswith("Focus:") for x in res["caveats"])
    assert _rates(res, "Distance when late")["pune_central"][0] is not None


def test_rto_reads_the_flag_and_names_the_conflict(api):
    c, did, _, _ = api
    res = _run(c, did, "logistics.rto_analysis", by="hub")
    conflict = [x for x in res["caveats"] if x.startswith("rto_flag_conflict")]
    assert conflict and "11 delivered but flagged RTO, 2 RTO by status" in conflict[0]
    assert _rates(res, "RTO rate")


def test_stuck_shipments_needs_an_as_of_date_and_counts_open_orders(api):
    c, did, _, _ = api
    r = c.post("/tools/logistics.stuck_shipments/run", json={"dataset_id": did, "params": {}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "param_required"
    res = _run(c, did, "logistics.stuck_shipments", by="hub", as_of="2026-09-01", days=3)
    n = {f["name"].split(": ")[1].lower(): f["value"] for f in res["figures"]
         if f["name"].startswith("Stuck by group: ") and "[" not in f["name"]}
    # 18 orders are Pending; 15 were created before 2026-08-29 (3 are newer: not stuck yet)
    assert n == {"pune_west": 5, "pune_central": 4, "pune_east": 4, "pune_south": 2}, n


def test_otif_without_quantities_says_what_it_cannot_do(api):
    c, did, _, _ = api
    r = c.post("/tools/logistics.otif/run", json={"dataset_id": did, "params": {"by": "hub"}})
    assert r.status_code == 422                 # on_time not approved, no quantity columns
    assert "on_time" in r.text or "no step could run" in r.text


def test_without_the_metric_nothing_is_computed(tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    ws, did, _, _ = _load(c, rules=(), metrics=())
    try:
        r = c.post("/tools/logistics.sla_compliance/run", json={"dataset_id": did,
                                                                 "params": {"by": "hub"}})
        assert r.status_code == 422 and "sla_breach' is not approved" in r.text
    finally:
        workspace.reset(ws)


def test_interpretation_rules_catch_logistics_misreadings():
    trace = [{"tool_id": "logistics.sla_drivers",
              "pack_rules_applied": ["drivers_are_associations", "sla_on_delivered_only",
                                     "compare_within_zone"]}]
    bad = {"drivers_are_associations": "Heavy rain caused the breaches in Pune_South.",
           "sla_on_delivered_only": "RTO orders count as late, so the SLA breach rate is 70%.",
           "compare_within_zone": "Courier A is the best courier overall."}
    for rule, text in bad.items():
        assert rule in {v.rule for v in interpret.check(text, trace)}, rule
    good = ("In Pune_South, heavy-rain deliveries were late more often (92.3% of 13) -- an "
            "association in this data; within each zone courier A was late less often.")
    assert not interpret.check(good, trace)


# --- the playbook, end to end, with a scripted model -------------------------------------------

@pytest.fixture
def env(tmp_path):
    yield from make_env(tmp_path)


def test_sla_where_and_why_playbook(env):
    from backend.tests.test_playbooks import Script
    c = env
    ws, did, _, _ = _load(c, metrics=("sla_breach", "rto_rate"))
    try:
        t = c.ask(did, "Which hub is worst on SLA, and what goes with it?",
                  script=Script({"playbook": "sla_where_and_why", "slots": {}}))
        calls = [e["data"] for e in t["events"] if e["type"] == "tool_call"]
        assert [x["tool_id"] for x in calls] == ["logistics.sla_compliance",
                                                  "logistics.sla_drivers",
                                                  "logistics.rto_analysis"]
        assert t["answer"]["text"] and not t["answer"]["flags"], t["answer"]
        print("\nplaybook usage:", t["answer"]["usage"])
    finally:
        workspace.reset(ws)


@pytest.mark.parametrize("tool", ["core.summary_stats", "core.trend"])
def test_a_core_analysis_is_refused_as_a_tool_run_not_a_500(api, tool):
    """C11 (F4 probe): GET /tools lists core analyses as active, but they run through a turn.
    POST /tools/core.*/run hit m.tools[tool_id] before the core check: KeyError, HTTP 500."""
    c, did, _, _ = api
    assert {t["tool_id"]: t["status"] for t in c.get(f"/datasets/{did}/tools").json()["tools"]}[tool] == "active"
    r = c.post(f"/tools/{tool}/run", json={"dataset_id": did, "params": {}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_a_domain_tool"
