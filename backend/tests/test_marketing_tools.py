"""B3: marketing pack knowledge + Tier-1 tools, on synthetic files with planted traps.

Expected figures are computed here from the CSV rows with Python's csv module -- an independent
path from the engine -- and each trap has a test that the tools block it or warn.
"""
from __future__ import annotations

import csv
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.engine import workspace
from backend.packs.loader import load_all, merge
from backend.tools import registry

FIX = Path(__file__).resolve().parent / "fixtures_v2"
TEST_WORDS = ("test", "dummy", "donotuse", "do not use")
FORKS = {"tax_basis": "net_excl_gst", "fiscal_year": "april", "timezone": "ist",
         "conversion_source": "platform", "roas_revenue_basis": "net_excl_gst",
         "attribution_window": "7d_click_1d_view", "cac_scope": "paid_media_only"}


def rows(name):
    return list(csv.DictReader(open(FIX / name)))


def upload(c, ws, name):
    r = c.post(f"/workspaces/{ws}/uploads", files={"file": (name, (FIX / name).read_bytes())})
    assert r.status_code == 201, r.text
    return r.json()["dataset_id"]


def confirm(c, did, key, dims, window, forks=FORKS):
    assert c.post(f"/datasets/{did}/domains/confirm", json={"domains": ["marketing"]}).json()["ok"]
    p = c.get(f"/datasets/{did}/contract/proposal").json()
    ms = [m["column"] for m in p["measures"]]
    contract = {"grain": "one row per report row", "primary_key": key, "date_column": "date",
                "measures": ms, "dimensions": dims,
                "aggregations": {m["column"]: m["suggested_agg"] or "sum" for m in p["measures"]},
                "measure_definitions": {m: f"{m} as exported" for m in ms},
                "analysis_window_start": window[0], "analysis_window_end": window[1]}
    asked = {f["fork_id"] for f in p["forks"]}
    r = c.post(f"/datasets/{did}/contract/confirm",
               json={"contract": contract, "fork_choices": {k: v for k, v in forks.items()
                                                            if k in asked}})
    assert r.status_code == 200, r.text
    return p


@pytest.fixture(scope="module")
def ads(tmp_path_factory):
    c = TestClient(create_app(state_dir=tmp_path_factory.mktemp("state")))
    ws = f"ws_{secrets.token_hex(6)}"
    did = upload(c, ws, "ads_traps.csv")
    tools_before = {t["tool_id"]: t for t in c.get(f"/datasets/{did}/tools").json()["tools"]}
    run_before = c.post("/tools/marketing.channel_efficiency/run",
                        json={"dataset_id": did, "params": {}})
    confirm(c, did, ["row_id"], ["campaign", "channel", "device", "city"],
            ("2025-10-01", "2026-01-31"))
    rules = c.get(f"/datasets/{did}/validity-rules").json()["rules"]
    c.post(f"/datasets/{did}/validity-rules/approve", json={"approve": [
        "exclude_test_campaigns", "zero_impression_spend", "roas_spend_positive"]})
    approved = {t: c.post(f"/datasets/{did}/metrics/approve",
                          json={"template_id": t, "bindings": {}}).json()
                for t in ("ctr", "cpc", "cpa", "roas", "cvr")}
    yield {"c": c, "did": did, "ws": ws, "tools_before": tools_before, "rules": rules,
           "run_before": run_before, "approved": approved}
    workspace.reset(ws)


def run(ads, tool, **params):
    return ads["c"].post(f"/tools/{tool}/run", json={"dataset_id": ads["did"], "params": params})


def fig(result, name):
    return next(f["value"] for f in result["figures"] if f["name"] == name)


def live_rows():
    return [r for r in rows("ads_traps.csv")
            if not any(w in r["campaign"].lower() for w in TEST_WORDS)]


# --- pack --------------------------------------------------------------------------------------

def test_marketing_pack_has_20_tools_that_compile():
    m = merge(load_all(), ["marketing"])
    tools = [t for t in m.tools if t.startswith("marketing.")]
    assert len(tools) == 35      # 20 Tier-1 (B3) + 11 Tier-2 (B4) + 3 keyword (B7) + 1 (B12)
    assert registry.compile_errors(m) == []
    assert all(len(m.tools[t].description) <= 200 for t in tools)


def test_changelog_entries_are_dated_and_sourced():
    m = merge(load_all(), ["marketing"])
    assert [c.date for c in m.changelog] == ["2023-11-01", "2024-03-21", "2026-01-12",
                                             "2026-03-03"]
    assert all(c.source.startswith("https://") for c in m.changelog)


def test_schema_tokens_core_vs_core_plus_marketing():
    core = registry.llm_schemas(registry.core_tools())
    states = registry.statuses(["marketing"], set(merge(load_all(), ["marketing"]).concepts),
                               {"paid_ads", "ga4", "search_console", "email", "social"})
    both = registry.llm_schemas(states)
    a, b = registry.schema_tokens(core), registry.schema_tokens(both)
    print(f"\ntool-schema tokens (estimate): core-only {a}, core+marketing (all 31 active) {b}")
    assert a < b <= 2500      # budget: every active tool's schema, sent on every call


# --- gating ------------------------------------------------------------------------------------

def test_tools_need_the_domain_before_confirmation(ads):
    assert ads["tools_before"]["marketing.channel_efficiency"]["status"] == "needs_domain"
    assert ads["run_before"].status_code == 409


def test_tools_without_their_data_say_what_is_missing(ads):
    t = {x["tool_id"]: x for x in ads["c"].get(f"/datasets/{ads['did']}/tools").json()["tools"]}
    assert t["marketing.channel_efficiency"]["status"] == "active"
    assert t["marketing.striking_distance"]["status"] == "needs_data"
    assert "position" in t["marketing.striking_distance"]["missing_concepts"]


def test_metric_approval_adds_contract_versions(ads):
    ap = ads["approved"]
    assert [ap[t]["version"] for t in ("ctr", "cpc", "cpa", "roas", "cvr")] == [2, 3, 4, 5, 6]
    assert ap["ctr"]["measure"] == "ctr_ratio"      # the file's own per-row ctr keeps its name


def test_templates_list_says_available_and_approved(ads):
    t = {x["template_id"]: x for x in ads["c"].get(
        f"/datasets/{ads['did']}/metrics/templates").json()["templates"]}
    assert t["roas"]["approved"] and t["roas"]["measure"] == "roas"
    assert not t["gsc_position"]["available"]              # engine cannot do weighted mean yet
    r = ads["c"].post(f"/datasets/{ads['did']}/metrics/approve",
                      json={"template_id": "gsc_position", "bindings": {}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "engine_not_ready"


def test_delivered_roas_variant_is_refused_not_guessed(ads):
    r = ads["c"].post(f"/datasets/{ads['did']}/metrics/approve",
                      json={"template_id": "roas", "bindings": {},
                            "fork_choices": {"roas_revenue_basis": "delivered_net"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "variant_unsupported"


# --- traps -------------------------------------------------------------------------------------

def test_trap_per_row_ctr_is_never_summed_or_averaged(ads):
    r = run(ads, "marketing.channel_efficiency", by="channel")
    assert r.status_code == 200, r.text
    res = r.json()
    g = [x for x in live_rows() if x["channel"] == "google"]
    expected = 100 * sum(int(x["clicks"]) for x in g) / sum(int(x["impressions"]) for x in g)
    got = fig(res, "CTR: google")
    assert got == pytest.approx(expected, abs=1e-4)
    assert got != pytest.approx(sum(float(x["ctr"]) for x in g), abs=0.01)
    assert got != pytest.approx(sum(float(x["ctr"]) for x in g) / len(g), abs=1e-3)
    assert "no_sum_of_rate" in res["pack_rules_applied"]


def test_trap_test_campaign_is_excluded_by_the_approved_rule(ads):
    rules = {x["rule_id"]: x for x in ads["rules"]}
    assert rules["exclude_test_campaigns"]["rows_affected"] == 123
    res = run(ads, "marketing.channel_efficiency", by="channel").json()
    spend = sum(float(x["cost"]) for x in live_rows() if x["channel"] == "google")
    assert fig(res, "Spend by group: google") == pytest.approx(spend, abs=0.01)
    assert "exclude_test_campaigns" in res["validity_filters_applied"]


def test_trap_zero_impression_spend_is_flagged_not_dropped(ads):
    res = run(ads, "marketing.channel_efficiency", by="channel").json()
    assert any(c.startswith("zero_impression_spend: 2 row(s) flagged") for c in res["caveats"])
    zero = [x for x in live_rows() if x["impressions"] == "0"]
    assert len(zero) == 2 and all(x["channel"] == "google" for x in zero)   # still in spend


def test_trap_gst_inclusive_revenue_roas_is_net_as_chosen(ads):
    res = run(ads, "marketing.channel_efficiency", by="channel").json()
    g = [x for x in live_rows() if x["channel"] == "google" and float(x["cost"]) > 0]
    net = sum(float(x["order_revenue"]) - float(x["gst"]) for x in g)
    gross = sum(float(x["order_revenue"]) for x in g)
    cost = sum(float(x["cost"]) for x in g)
    assert fig(res, "ROAS: google") == pytest.approx(net / cost, abs=1e-4)
    assert fig(res, "ROAS: google") != pytest.approx(gross / cost, abs=1e-3)
    assert res["forks"]["roas_revenue_basis"] == "net_excl_gst"


def test_trap_tiny_city_is_suppressed(ads):
    res = run(ads, "marketing.device_geo_split", by="city").json()
    assert fig(res, "Clicks by group: Nashik") is None
    assert any("'Nashik' has 2 row(s), under the minimum group size 5" in c
               for c in res["caveats"])
    pune = sum(int(x["clicks"]) for x in live_rows() if x["city"] == "Pune")
    assert fig(res, "Clicks by group: Pune") == pune


def test_trap_measurement_break_is_named(ads):
    r = run(ads, "marketing.roas_change_explainer", period="2026-01", baseline="2025-12",
            by="channel")
    assert r.status_code == 200, r.text
    cav = r.json()["caveats"]
    # ROAS here is on backend order revenue: Meta's window change does not touch it (C2) ...
    assert not any(c.startswith("measurement_change") for c in cav)
    assert any("INCLUDING GST" in c for c in cav)        # the gross decomposition is said
    # ... while CPA/CVR on platform-reported conversions across 2026-01-12 carry it.
    eff = run(ads, "marketing.channel_efficiency", by="channel").json()["caveats"]
    assert any(c.startswith("measurement_change: Meta removed 7-day and 28-day view") for c in eff)


def test_trap_diwali_needs_confirmed_dates_and_is_named(ads):
    r = run(ads, "marketing.festive_compare", festival="diwali", year="2025",
            measure="conversions")
    assert r.status_code == 422 and r.json()["error"]["code"] == "festival_dates_unconfirmed"
    r = run(ads, "marketing.festive_compare", festival="diwali", year="2025",
            measure="conversions", dates_confirmed=True, by="channel")
    assert r.status_code == 200, r.text
    res = r.json()
    d = [x for x in live_rows() if x["date"] in ("2025-10-20", "2025-10-21")]
    assert fig(res, "This year: (all)") == sum(int(x["conversions"]) for x in d)
    assert any(c.startswith("festival_confound: Diwali") for c in res["caveats"])
    assert any(c.startswith("Last year: the engine refused") for c in res["caveats"])


def test_period_label_params_are_required(ads):
    r = run(ads, "marketing.roas_change_explainer")
    assert r.status_code == 422 and set(r.json()["missing"]) == {"period", "baseline"}


def test_unapproved_metric_step_is_skipped_and_said(ads, tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    ws = f"ws_{secrets.token_hex(6)}"
    try:
        did = upload(c, ws, "ads_traps.csv")
        confirm(c, did, ["row_id"], ["campaign", "channel"], ("2025-10-01", "2026-01-31"))
        res = c.post("/tools/marketing.channel_efficiency/run",
                     json={"dataset_id": did, "params": {}}).json()
        assert any("metric 'ctr' is not approved" in x for x in res["caveats"])
        assert res["validity_filters_applied"] == []      # nothing approved, nothing applied
        tc = [x for x in rows("ads_traps.csv") if x["channel"] == "google"]
        assert fig(res, "Spend by group: google") == pytest.approx(
            sum(float(x["cost"]) for x in tc), abs=0.01)
    finally:
        workspace.reset(ws)


def test_trap_three_sources_of_conversions_are_never_added(tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    ws = f"ws_{secrets.token_hex(6)}"
    try:
        did = upload(c, ws, "conv_multi.csv")
        confirm(c, did, ["date", "campaign"], ["campaign"], ("2026-02-01", "2026-02-28"))
        r = c.post("/tools/marketing.spend_waste/run", json={"dataset_id": did, "params": {}})
        assert r.status_code == 422
        e = r.json()
        assert e["error"]["code"] == "ambiguous_binding"
        assert "no_cross_source_conversion_sum" in e["error"]["message"]
        assert set(e["columns"]) == {"google_conversions", "meta_conversions"}
        r = c.post("/tools/marketing.spend_waste/run", json={"dataset_id": did, "params": {
            "bindings": {"conversions": "google_conversions"}}})
        assert r.status_code == 200, r.text
        want = sum(int(x["google_conversions"]) for x in rows("conv_multi.csv")
                   if x["campaign"] == "Brand")
        assert fig(r.json(), "Conversions per group: Brand") == want
    finally:
        workspace.reset(ws)


def test_trap_utm_case_variants_not_set_and_missing_medium(tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    ws = f"ws_{secrets.token_hex(6)}"
    try:
        did = upload(c, ws, "utm.csv")
        confirm(c, did, ["date", "session_source"], ["session_source", "session_medium"],
                ("2026-03-01", "2026-03-14"))
        r = c.post("/tools/marketing.utm_hygiene/run", json={"dataset_id": did, "params": {}})
        assert r.status_code == 200, r.text
        cav = r.json()["caveats"]
        variants = next(x for x in cav if x.startswith("utm case_variants"))
        assert "['GOOGLE', 'Google', 'google'] are one source (42 rows)" in variants
        assert "not_set: 14 row(s)" in " ".join(cav)
        blank = sum(1 for x in rows("utm.csv") if x["session_medium"] in ("", "(not set)"))
        assert f"missing_medium: {blank} row(s)" in " ".join(cav)
    finally:
        workspace.reset(ws)
