"""B4: Tier-2 marketing tools through the API, on small files with hand-worked numbers."""
from __future__ import annotations

import csv
import io
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.engine import workspace

FIX = Path(__file__).resolve().parent / "fixtures_v2"
from backend.tests.test_shared_v2 import EVENTS  # noqa: E402


@pytest.fixture
def api(tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    spaces = []

    def load(name, header, rows, *, key, dims, window, forks=None, measures=None):
        ws = f"ws_{secrets.token_hex(6)}"
        spaces.append(ws)
        buf = io.StringIO()
        csv.writer(buf).writerows([header, *rows])
        r = c.post(f"/workspaces/{ws}/uploads", files={"file": (f"{name}.csv",
                                                                 buf.getvalue().encode())})
        assert r.status_code == 201, r.text
        did = r.json()["dataset_id"]
        c.post(f"/datasets/{did}/domains/confirm", json={"domains": ["marketing"]})
        p = c.get(f"/datasets/{did}/contract/proposal").json()
        ms = measures or [m["column"] for m in p["measures"]]
        answers = {"tax_basis": "net_excl_gst", "fiscal_year": "april", "timezone": "ist",
                   "conversion_source": "backend_orders", "roas_revenue_basis": "net_excl_gst",
                   "attribution_window": "mixed_unknown", "cac_scope": "paid_media_only",
                   "email_rate_denominator": "delivered", "gsc_aggregation": "by_property",
                   **(forks or {})}
        r = c.post(f"/datasets/{did}/contract/confirm", json={"contract": {
            "grain": "one row per record", "primary_key": key, "date_column": "date",
            "measures": ms, "dimensions": dims,
            "aggregations": {m: next((x["suggested_agg"] for x in p["measures"]
                                     if x["column"] == m and x["suggested_agg"]), "sum")
                             for m in ms},
            "measure_definitions": {m: m for m in ms},
            "analysis_window_start": window[0], "analysis_window_end": window[1]},
            "fork_choices": {f["fork_id"]: answers[f["fork_id"]] for f in p["forks"]}})
        assert r.status_code == 200, r.text
        return did

    def run(tool, did, **params):
        return c.post(f"/tools/{tool}/run", json={"dataset_id": did, "params": params})

    c.load, c.run = load, run
    yield c
    for ws in spaces:
        workspace.reset(ws)


def fig(res, name):
    return next(f["value"] for f in res["figures"] if f["name"] == name)


def test_delivered_roas_removes_cancels_and_rto_and_splits_cod(api):
    base = [["2026-05-01", "COD", 1180, 180, 100, 0, 0],
            ["2026-05-01", "COD", 1180, 180, 100, 1, 0],        # cancelled
            ["2026-05-02", "prepaid", 2360, 360, 200, 0, 0],
            ["2026-05-02", "prepaid", 1180, 180, 100, 0, 1],    # returned to origin
            ["2026-05-03", "COD", 590, 90, 100, 0, 0]]
    # each order 5 times: groups clear min_group_size 5; every ratio is unchanged
    rows = [[i * 5 + j + 1, *r] for i, r in enumerate(base) for j in range(5)]
    did = api.load("orders_d", ["row_id", "date", "payment_method", "order_revenue", "gst",
                                "cost", "cancelled", "rto"], rows, key=["row_id"],
                   dims=["payment_method"], window=("2026-05-01", "2026-05-03"),
                   measures=["order_revenue", "gst", "cost"])
    r = api.post(f"/datasets/{did}/metrics/approve", json={"template_id": "delivered_roas",
                                                           "bindings": {}})
    assert r.status_code == 200, r.text
    r = api.run("marketing.delivered_roas", did)
    assert r.status_code == 200, r.text
    res = r.json()
    assert fig(res, "group_compare: COD") == pytest.approx(7.5)
    assert fig(res, "group_compare: prepaid") == pytest.approx(10.0)
    assert fig(res, "group_compare: (all)") == pytest.approx(8.75)
    assert {"exclude:cancelled", "exclude:rto"} <= set(res["validity_filters_applied"])


def test_roas_delivered_variant_points_to_the_tool(api):
    rows = [[1, "2026-05-01", "COD", 1180, 180, 100]]
    did = api.load("o1", ["row_id", "date", "payment_method", "order_revenue", "gst", "cost"],
                   rows, key=["row_id"], dims=["payment_method"],
                   window=("2026-05-01", "2026-05-01"), measures=["order_revenue", "gst", "cost"])
    r = api.post(f"/datasets/{did}/metrics/approve", json={
        "template_id": "roas", "bindings": {}, "fork_choices": {
            "roas_revenue_basis": "delivered_net"}})
    assert r.status_code == 422 and "marketing.delivered_roas" in r.json()["error"]["message"]


def test_funnel_tool_on_ga4_event_names(api):
    names = {"view": "view_item", "cart": "add_to_cart", "checkout": "begin_checkout",
             "purchase": "purchase"}
    rows = [[i, d, u, names[e]] for i, (u, d, e) in enumerate(EVENTS)]
    did = api.load("ga4", ["row_id", "date", "user_pseudo_id", "event_name"], rows,
                   key=["row_id"], dims=["event_name", "user_pseudo_id"],
                   window=("2026-01-01", "2026-01-31"))
    res = api.run("marketing.funnel", did).json()
    assert [fig(res, f"funnel: {s}") for s in names.values()] == [5, 3, 2, 1]
    res = api.run("marketing.funnel", did, window_days=7).json()
    assert fig(res, "funnel: add_to_cart") == 2


def test_cac_payback_tool(api):
    rows = [["o1", "c1", "2026-01-05", 500], ["o2", "c1", "2026-02-10", 500],
            ["o3", "c1", "2026-03-01", 500], ["o4", "c2", "2026-01-20", 300],
            ["o5", "c3", "2026-02-03", 1000]]
    did = api.load("cust", ["order_id", "customer_id", "date", "revenue"], rows,
                   key=["order_id"], dims=["customer_id"], window=("2026-01-01", "2026-03-31"),
                   measures=["revenue"])
    r = api.run("marketing.cac_payback", did)
    assert r.status_code == 422 and r.json()["missing"] == ["spend"]
    res = api.run("marketing.cac_payback", did, spend={"2026-01": 1200, "2026-02": 500}).json()
    assert fig(res, "unit_economics: 2026-01 [CAC]") == 600
    assert fig(res, "unit_economics: 2026-01 [payback month]") == 2
    assert fig(res, "unit_economics: 2026-01 [LTV:CAC to date]") == 1.5
    assert fig(res, "unit_economics: 2026-02 [CAC]") == 500
    assert res["forks"] == {"cac_scope": "paid_media_only"}


def test_conversion_reconciliation_on_three_sources(api):
    rows = list(csv.reader(open(FIX / "conv_multi.csv")))
    did = api.load("cm", rows[0], rows[1:], key=["date", "campaign"], dims=["campaign"],
                   window=("2026-02-01", "2026-02-28"),
                   measures=["cost", "google_conversions", "meta_conversions", "ga4_key_events"])
    res = api.run("marketing.conversion_reconciliation", did,
                  sources=["google_conversions", "meta_conversions"],
                  reference="ga4_key_events").json()
    body = list(csv.DictReader(open(FIX / "conv_multi.csv")))
    g = sum(int(x["google_conversions"]) for x in body)
    ga4 = sum(int(x["ga4_key_events"]) for x in body)
    total = next(f for f in res["figures"] if f["name"] == "source_reconciliation: (all)")
    assert total["value"] == g and total["unit"] == "google_conversions"
    assert "no_cross_source_conversion_sum" in res["pack_rules_applied"]
    assert ga4 > 0


def test_campaign_impact_is_correlational_and_names_the_festival(api):
    from datetime import date, timedelta
    s = date(2025, 10, 1)
    rows = [[(s + timedelta(days=d)).isoformat(), 100 if d < 28 else 130] for d in range(56)]
    did = api.load("imp", ["date", "order_revenue"], rows, key=["date"], dims=[],
                   window=("2025-10-01", "2025-11-25"), measures=["order_revenue"])
    res = api.run("marketing.campaign_impact", did, start="2025-10-29").json()
    assert fig(res, "before_after_baseline: post vs projection") in (30.0, "+30.0%")
    assert any("ASSOCIATED WITH" in c for c in res["caveats"])
    assert any(c.startswith("festival_confound: Diwali") for c in res["caveats"])
    assert "correlational_only" in res["pack_rules_applied"]
