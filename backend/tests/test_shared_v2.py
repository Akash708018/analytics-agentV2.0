"""B4: the nine shared tier-9 engine analyses, each against numbers worked out by hand BEFORE
the first run (docs/steps/B4.md lists them with the arithmetic)."""
from __future__ import annotations

import csv
import io
import secrets
from datetime import date, timedelta

import pytest

from backend.engine import workspace
from backend.engine.analysis.tools import _produce, _Refused
from backend.engine.util import db
from backend.services.datasets import V2Backend

BE = V2Backend()


class DS:
    def __init__(self, name, header, rows, *, key, measures, dims, date_col="date",
                 ratios=None, aggs=None, window=None):
        self.ws, self.name = f"ws_{secrets.token_hex(6)}", name
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        up = BE.save_upload(self.ws, f"{name}.csv", buf.getvalue().encode())
        d = BE.draft_ingest(self.ws, up.path)
        assert BE.confirm_ingest(self.ws, d.spec).ok
        aggs = aggs or {m: "sum" for m in measures}
        ratios = ratios or {}
        for r in ratios:
            aggs[r] = "ratio"
        ms = list(measures) + list(ratios)
        dates = sorted(r[header.index(date_col)] for r in rows)
        dr = BE.draft_contract(
            self.ws, name, grain="one row per test record", primary_key=key,
            date_column=date_col, measures=ms, dimensions=dims, aggregations=aggs,
            measure_definitions={m: m for m in ms},
            analysis_window_start=(window or dates)[0], analysis_window_end=(window or dates)[-1],
            ratios=ratios)
        assert not dr.provisional, (dr.provisional, dr.questions)
        assert BE.confirm_contract(self.ws, dr).ok

    def run(self, analysis, **params):
        con = db.connect(self.ws)
        try:
            _, out, _ = _produce(con, self.name, analysis, params, self.ws)
            return out
        finally:
            con.close()

    def close(self):
        workspace.reset(self.ws)


@pytest.fixture
def make():
    made = []

    def f(*a, **k):
        d = DS(*a, **k)
        made.append(d)
        return d
    yield f
    for d in made:
        d.close()


def col(out, name):
    return [r[out.headers.index(name)] for r in out.rows]


def n(x):
    return float(str(x).replace(",", "").rstrip("%")) if x is not None else None


# --- funnel: 5 users; view->cart->checkout->purchase -------------------------------------------

EVENTS = [  # user, date, event
    ("u1", "2026-01-01", "view"), ("u1", "2026-01-02", "cart"), ("u1", "2026-01-03", "checkout"),
    ("u1", "2026-01-04", "purchase"),
    ("u2", "2026-01-01", "view"), ("u2", "2026-01-01", "cart"), ("u2", "2026-01-05", "checkout"),
    ("u3", "2026-01-02", "view"), ("u3", "2026-01-03", "purchase"),       # skipped cart
    ("u4", "2026-01-01", "cart"), ("u4", "2026-01-02", "view"),           # cart before view
    ("u5", "2026-01-01", "view"), ("u5", "2026-01-20", "cart"),           # cart 19 days later
]


def test_funnel_ordered_steps_and_window(make):
    rows = [[i, u, d, e, 1] for i, (u, d, e) in enumerate(EVENTS)]
    ds = make("events", ["id", "user_id", "date", "event_name", "n"], rows, key=["id"],
              measures=["n"], dims=["user_id", "event_name"])
    steps = ["view", "cart", "checkout", "purchase"]
    out = ds.run("funnel", entity="user_id", step_column="event_name", steps=steps)
    assert [int(n(x)) for x in col(out, "entities")] == [5, 3, 2, 1]
    assert col(out, "from previous")[1:] == ["60.0%", "66.7%", "50.0%"]
    out = ds.run("funnel", entity="user_id", step_column="event_name", steps=steps,
                 window_days=7)
    assert [int(n(x)) for x in col(out, "entities")] == [5, 2, 2, 1]


# --- source_reconciliation ---------------------------------------------------------------------

def test_source_reconciliation_never_adds(make):
    rows = [["2026-02-01", "A", 10, 6, 12], ["2026-02-02", "A", 5, 4, 6],
            ["2026-02-01", "B", 3, 9, 7], ["2026-02-02", "B", 2, 1, 5]]
    ds = make("recon", ["date", "campaign", "google_conv", "meta_conv", "orders"], rows,
              key=["date", "campaign"], measures=["google_conv", "meta_conv", "orders"],
              dims=["campaign"])
    out = ds.run("source_reconciliation", measures=["google_conv", "meta_conv"],
                 reference="orders")
    r = out.rows[0]
    assert [n(x) for x in r[1:4]] == [20, 20, 30]
    assert [n(x) for x in r[4:6]] == [pytest.approx(0.6667, abs=1e-4)] * 2
    assert "never added" in " ".join(out.summary)
    out = ds.run("source_reconciliation", measures=["google_conv", "meta_conv"],
                 reference="orders", dimension="campaign")
    a = out.rows[0]
    assert a[0] == "A" and n(a[4]) == pytest.approx(0.8333, abs=1e-4) and n(a[5]) == \
        pytest.approx(0.5556, abs=1e-4)


# --- ab_test -----------------------------------------------------------------------------------

def _ab(make, visitors_b=1000, conv_a=50, conv_b=60, per=5, name="ab"):
    rows = []
    for i in range(per):
        rows.append([f"2026-03-{i + 1:02d}", "A", 1000, conv_a])
        rows.append([f"2026-03-{i + 1:02d}", "B", visitors_b, conv_b])
    return make(name, ["date", "variant", "visitors", "conversions"], rows,
                key=["date", "variant"], measures=["visitors", "conversions"],
                dims=["variant"],
                ratios={"cr": {"numerator": ["conversions"], "denominator": ["visitors"],
                               "scale": 1}})


def test_ab_test_readout(make):
    out = _ab(make).run("ab_test", dimension="variant", measure="cr")
    got = {r[0]: r[3] for r in out.rows}
    assert got["A"] == "5.0%" and got["B"] == "6.0%"
    assert got["effect (B - A)"] == "+1.00 pts"
    assert float(got["p-value"]) == pytest.approx(0.02829, abs=2e-4)
    assert got["minimum detectable effect"] == "1.28 pts"
    assert "Sample ratio check passed" in out.summary[-1]


def test_ab_test_sample_ratio_mismatch_gives_no_readout(make):
    out = _ab(make, visitors_b=1100, conv_b=66, name="ab_srm").run(
        "ab_test", dimension="variant", measure="cr")
    assert out.summary[-1].startswith("SAMPLE RATIO MISMATCH")
    assert not any(r[0] == "p-value" for r in out.rows)


def test_ab_test_not_enough_data(make):
    out = _ab(make, per=1, name="ab_small").run("ab_test", dimension="variant", measure="cr")
    assert out.summary[-1].startswith("NOT ENOUGH DATA") is False    # 1000 trials each: enough
    rows = [["2026-03-01", "A", 40, 2], ["2026-03-01", "B", 40, 3]]
    ds = make("ab_tiny", ["date", "variant", "visitors", "conversions"], rows,
              key=["date", "variant"], measures=["visitors", "conversions"], dims=["variant"],
              ratios={"cr": {"numerator": ["conversions"], "denominator": ["visitors"],
                             "scale": 1}})
    out = ds.run("ab_test", dimension="variant", measure="cr")
    assert out.summary[-1].startswith("NOT ENOUGH DATA")


# --- pacing ------------------------------------------------------------------------------------

def test_pacing_straight_line(make):
    rows = [[f"2026-09-{d:02d}", 1000] for d in range(1, 11)]
    ds = make("spend", ["date", "cost"], rows, key=["date"], measures=["cost"], dims=[],
              window=["2026-09-01", "2026-09-30"])
    out = ds.run("pacing", measure="cost", budget=25000, month="2026-09")
    got = dict(out.rows)
    assert n(got["spent to date"]) == 10000 and got["share of budget used"] == "40.0%"
    assert got["days elapsed"] == "10 of 30"
    assert n(got["straight-line month-end projection"]) == 30000
    assert got["projection vs budget"] == "+20.0%" or got["projection vs budget"] == "20.0%"
    assert "STRAIGHT LINE" in out.summary[-1]


# --- text_ngrams -------------------------------------------------------------------------------

QUERIES = [["2026-04-01", "sushi delivery pune", 10], ["2026-04-01", "sushi near me", 5],
           ["2026-04-01", "best sushi pune", 3], ["2026-04-01", "ramen pune", 2]]


def test_text_ngrams(make):
    ds = make("q", ["date", "query", "clicks"], QUERIES, key=["query"], measures=["clicks"],
              dims=["query"])
    out = ds.run("text_ngrams", column="query", measure="clicks", n=1, limit=3)
    assert [(r[0], n(r[1])) for r in out.rows] == [("sushi", 18), ("pune", 15),
                                                   ("delivery", 10)]
    out = ds.run("text_ngrams", column="query", measure="clicks", n=2, limit=10)
    got = {r[0]: n(r[1]) for r in out.rows}
    assert got == {"sushi delivery": 10, "delivery pune": 10, "sushi near": 5, "near me": 5,
                   "best sushi": 3, "sushi pune": 3, "ramen pune": 2}


# --- key_overlap -------------------------------------------------------------------------------

def test_key_overlap(make):
    rows = [["2026-04-01", "q1", "p1", 60], ["2026-04-01", "q1", "p2", 40],
            ["2026-04-01", "q2", "p1", 95], ["2026-04-01", "q2", "p2", 5],
            ["2026-04-01", "q3", "p3", 30]]
    ds = make("ko", ["date", "query", "page", "clicks"], rows, key=["query", "page"],
              measures=["clicks"], dims=["query", "page"])
    out = ds.run("key_overlap", key="query", member="page", measure="clicks")
    assert [r[0] for r in out.rows] == ["q1"]
    assert n(out.rows[0][2]) == 100 and out.rows[0][3] == "60.0%"
    assert out.rows[0][4] == "p1 | p2"


# --- expected_rate_by_bucket -------------------------------------------------------------------

def test_expected_rate_by_bucket(make):
    rows = [["2026-04-01", "P1", 1, 1000, 300], ["2026-04-01", "P2", 1, 1000, 100],
            ["2026-04-01", "P3", 5, 500, 25], ["2026-04-01", "P4", 5, 500, 15]]
    ds = make("gsc", ["date", "page", "position", "impressions", "clicks"], rows,
              key=["page"], measures=["impressions", "clicks", "position"], dims=["page"],
              aggs={"impressions": "sum", "clicks": "sum", "position": "mean"},
              ratios={"ctr": {"numerator": ["clicks"], "denominator": ["impressions"],
                              "scale": 100}})
    out = ds.run("expected_rate_by_bucket", measure="ctr", against="position", entity="page")
    got = [(r[0], r[1], n(r[6])) for r in out.rows]
    assert got == [("P2", "1-1", 10000), ("P4", "4-5", 500)]
    assert "1-1: 20" in out.summary[-2] and "4-5: 4" in out.summary[-2]


# --- before_after_baseline ---------------------------------------------------------------------

def test_before_after_baseline_is_correlational(make):
    start = date(2026, 6, 1)
    rows = [[(start + timedelta(days=d)).isoformat(), 100 if d < 0 else 130]
            for d in range(-28, 28)]
    ds = make("imp", ["date", "orders"], rows, key=["date"], measures=["orders"], dims=[])
    out = ds.run("before_after_baseline", measure="orders", start="2026-06-01")
    got = dict(out.rows)
    assert n(got["pre-period mean per day"]) == 100 and n(got["post-period mean per day"]) == 130
    assert n(got["post projected from the pre-period trend"]) == pytest.approx(100)
    assert got["post vs projection"] in ("+30.0%", "30.0%")
    assert "ASSOCIATED WITH" in out.summary[-1] and "correlational" in out.summary[-1]


# --- unit_economics ----------------------------------------------------------------------------

def test_unit_economics_payback_and_immature(make):
    rows = [["o1", "c1", "2026-01-05", 500], ["o2", "c1", "2026-02-10", 500],
            ["o3", "c1", "2026-03-01", 500], ["o4", "c2", "2026-01-20", 300],
            ["o5", "c3", "2026-02-03", 1000]]
    ds = make("orders", ["order_id", "customer_id", "date", "revenue"], rows,
              key=["order_id"], measures=["revenue"], dims=["customer_id"])
    out = ds.run("unit_economics", entity="customer_id", measure="revenue",
                 spend={"2026-01": 1200, "2026-02": 500})
    jan, feb = out.rows
    assert jan[0] == "2026-01" and n(jan[1]) == 2 and n(jan[2]) == 600
    assert n(jan[3]) == 900 and jan[4] == 2 and n(jan[5]) == 1.5 and jan[7] == ""
    assert feb[0] == "2026-02" and n(feb[2]) == 500 and feb[4] == 1 and n(feb[5]) == 2.0
    assert feb[7] == "IMMATURE"            # 2 months observed < 3


def test_rate_mix_shift_simpson(make):
    rows = [["2026-01-15", "A", 100, 10], ["2026-01-15", "B", 100, 2],
            ["2026-02-15", "A", 100, 12], ["2026-02-15", "B", 300, 9]]
    ds = make("mix", ["date", "channel", "clicks", "conversions"], rows,
              key=["date", "channel"], measures=["clicks", "conversions"], dims=["channel"],
              ratios={"cvr": {"numerator": ["conversions"], "denominator": ["clicks"],
                              "scale": 100}}, window=["2026-01-01", "2026-02-28"])
    out = ds.run("rate_mix_shift", measure="cvr", dimension="channel", period="2026-02",
                 baseline="2026-01")
    total = out.rows[-1]
    assert total[0] == "(all)" and n(total[1]) == 6 and n(total[2]) == 5.25
    assert [n(x) for x in total[5:]] == [pytest.approx(1.5), pytest.approx(-2.0),
                                         pytest.approx(-0.25)]
    assert "Simpson" in out.summary[-1]


def test_v2_analyses_stay_off_the_v1_mcp_surface():
    from backend.engine.analysis.registry import REGISTRY, catalogue
    v2 = {a.name for a in REGISTRY.values() if a.surface == "v2"}
    assert len(v2) == 12 and not v2 & {x for x, _, _ in catalogue()}
    assert v2 <= {x for x, _, _ in catalogue(surface=None)}


def test_refusals_are_engine_refusals(make):
    ds = make("q2", ["date", "query", "clicks"], QUERIES, key=["query"], measures=["clicks"],
              dims=["query"])
    with pytest.raises(_Refused):
        ds.run("funnel", entity="query", step_column="query", steps=["a"])
