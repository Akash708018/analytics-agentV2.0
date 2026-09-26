"""contract/suggest.py: how each measure combines, suggested with a reason, never applied.

    uv run pytest tests/test_suggest.py -q
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine.contract import suggest as sg
from backend.engine.contract.evidence import gather
from backend.engine.contract.propose import propose_contract


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    # 60 orders of 1-3 lines. fee repeats on every line of its order; margin_pct is a ratio of two
    # other columns; everything else varies by line.
    c.execute("""
      CREATE TABLE sales AS
      SELECT 'L' || i AS line_id, 'O' || (i // 3) AS order_id,
             (i % 7) * 10.0 + 5 AS unit_price,
             ((i * 37) % 900) + 100.0 AS revenue,
             round(((i * 37) % 900 + 100.0) * 0.6, 2) AS cost,
             round(100.0 * (((i * 37) % 900 + 100.0) - round(((i * 37) % 900 + 100.0) * 0.6, 2))
                   / (((i * 37) % 900) + 100.0), 2) AS margin_pct,
             ((i // 3) % 4) * 5.0 AS fee,
             10000 + (i * 13) % 5000 AS zip,
             (i % 50) * 3.0 AS balance,
             (i % 9) + 0.5 AS delivery_hours,
             ((i * 7) % 23) + 0.25 AS mystery,
             CAST(i % 5 AS VARCHAR) AS rating_text
      FROM range(180) t(i)""")
    yield c
    c.close()


def run(con, names):
    return sg.suggest(con, "sales", gather(con, "sales"), names)


def test_every_rule_reads_its_column(con):
    got = run(con, ["unit_price", "revenue", "cost", "margin_pct", "fee", "zip", "balance",
                    "delivery_hours", "mystery", "rating_text"])
    table = {m: (s.agg, s.per, s.strength, s.rule) for m, s in got.items()}
    assert table["unit_price"] == ("none", [], "strong", "N2")
    assert table["revenue"] == ("sum", [], "strong", "A1")
    assert table["cost"] == ("sum", [], "strong", "A1")
    assert table["margin_pct"] == ("none", [], "strong", "R1")
    assert table["fee"] == ("sum", ["order_id"], "strong", "P1")
    assert table["zip"] == ("none", [], "strong", "N1")
    assert table["balance"] == ("none", [], "likely", "S1")
    assert table["delivery_hours"] == ("mean", [], "likely", "D2"), "how long a delivery took"
    assert table["mystery"] == (None, [], "unsure", "-")
    assert table["rating_text"] == (None, [], "unsure", "T1")


def test_a_ratio_is_named_with_its_sums(con):
    r = run(con, ["revenue", "cost", "margin_pct"])["margin_pct"].ratio
    assert r["numerator"] == ["revenue", "-cost"] and r["denominator"] == ["revenue"]
    assert r["scale"] == 100.0 and r["name"] == "margin_pct_of_sums"


def test_only_what_is_not_strong_carries_a_question(con):
    got = run(con, ["revenue", "balance", "mystery"])
    assert got["revenue"].question() == ""
    assert "from its name only" in got["balance"].question()
    assert "(a) yes, it is an amount" in got["mystery"].question()


def test_nothing_is_applied_by_a_proposal(con):
    p = propose_contract(con, "sales", measures=["unit_price", "revenue", "fee"])
    assert all(m.agg is None for m in p.contract.measures)
    assert all(m.agg_path in p.contract.unresolved for m in p.contract.measures)
    q = next(q for q in p.contract.questions if q.startswith("What does fee"))
    assert "The engine suggests sum per order_id" in q
    assert "aggregations={'unit_price': 'none', 'revenue': 'sum', 'fee': 'sum'}" in p.to_text()


def test_a_stated_aggregation_against_strong_evidence_is_said_not_refused(con):
    p = propose_contract(con, "sales", measures=["unit_price"], aggregations={"unit_price": "sum"})
    assert p.contract.measures[0].agg == "sum"
    assert any("stated as agg='sum', but the engine reads it as 'none'" in n for n in p.notes)
    p = propose_contract(con, "sales", measures=["unit_price"], aggregations={"unit_price": "mean"})
    assert not any("stated as" in n for n in p.notes), "a mean of a price is not contradicted"


def test_a_ratio_answered_without_measures_is_kept(con):
    p = propose_contract(con, "sales", ratios={"margin_of_sums": {
        "numerator": ["revenue", "-cost"], "denominator": ["revenue"], "scale": 100}})
    assert "margin_of_sums" in [m.name for m in p.contract.measures]


def test_tokens_split_snake_and_camel_case():
    assert {"stock", "on", "hand", "onhand"} <= sg.tokens("stockOnHand")
    assert {"rep", "monthly", "salary"} <= sg.tokens("rep_monthly_salary")


# --- geo, distance and 0/1 flags (25/09/2026) ------------------------------------------------------

@pytest.fixture()
def geo():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE trips AS SELECT 'T' || i AS trip_id,
        18.5 + (i % 90) / 1000.0 AS pickup_lat, 73.8 + (i % 90) / 1000.0 AS pickup_lng,
        CASE WHEN i = 7 THEN 120.0 ELSE 18.6 END AS drop_latitude,
        (i * 7) % 40 + 0.5 AS distance_km, (i * 11) % 90 AS gps_drop_distance_m,
        CASE WHEN i % 4 = 0 THEN 1 ELSE 0 END AS is_late, (i % 6 = 0) AS is_cancelled,
        (i % 3) + 1 AS long_term_rate_pct
        FROM range(300) t(i)""")
    yield c
    c.close()


def test_coordinates_distances_and_flags(geo):
    ev = gather(geo, "trips")
    got = sg.suggest(geo, "trips", ev, ["pickup_lat", "pickup_lng", "drop_latitude",
                                          "distance_km", "gps_drop_distance_m", "is_late"])
    table = {m: (s.agg, s.strength, s.rule) for m, s in got.items()}
    assert table["pickup_lat"] == ("none", "strong", "G1")
    assert table["pickup_lng"] == ("none", "strong", "G1")
    assert table["drop_latitude"] == ("none", "likely", "G1"), "a value out of range: only likely"
    assert table["distance_km"] == ("sum", "likely", "G2")
    assert table["gps_drop_distance_m"] == ("none", "likely", "G2")
    assert table["is_late"] == ("mean", "strong", "F1")


def test_a_flag_that_is_not_a_measure_is_offered_as_a_rate(geo):
    ev = gather(geo, "trips")
    rates = {s.measure: s.column for s in sg.flag_rates(ev, ["distance_km"])}
    assert rates == {"is_late_rate": "is_late", "is_cancelled_rate": "is_cancelled"}
    p = propose_contract(geo, "trips", measures=["distance_km"])
    assert p.suggestions["is_late_rate"].column == "is_late"
    assert 'measure_columns={"is_late_rate": "is_late"}' in p.to_text()


def test_a_rate_named_long_is_not_a_longitude(geo):
    ev = gather(geo, "trips")
    assert sg.suggest(geo, "trips", ev, ["long_term_rate_pct"])["long_term_rate_pct"].rule != "G1"


# --- marketing (25/09/2026, scripts/marketing_bench.py) ---------------------------------------------

@pytest.fixture()
def ads():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE ads AS SELECT 'AG' || (i % 12) AS ad_group_id,
        1000 + (i * 37) % 5000 AS impressions,
        CAST(floor((1000 + (i * 37) % 5000) * 0.02) AS BIGINT) AS clicks,
        round(floor((1000 + (i * 37) % 5000) * 0.02) * (0.5 + (i % 4) * 0.4), 2) AS spend,
        CAST(floor((1000 + (i * 37) % 5000) * 0.02) AS BIGINT) // 10 AS conversions,
        round(1000.0 * round(floor((1000 + (i * 37) % 5000) * 0.02) * (0.5 + (i % 4) * 0.4), 2)
              / (1000 + (i * 37) % 5000), 4) AS cpm,
        CAST(floor((1000 + (i * 37) % 5000) / 1.7) AS BIGINT) AS reach,
        round(0.3 + (i % 12) * 0.1, 2) AS bid_amount,
        round(100.0 * floor((1000 + (i * 37) % 5000) * 0.02) / (1000 + (i * 37) % 5000), 2)
            AS conversion_rate_pct
        FROM range(600) t(i)""")
    yield c
    c.close()


def test_marketing_counts_ratios_audiences_and_bids(ads):
    ev = gather(ads, "ads")
    got = sg.suggest(ads, "ads", ev, ["impressions", "clicks", "spend", "conversions", "cpm",
                                      "reach", "bid_amount", "conversion_rate_pct"])
    table = {m: (s.agg, s.rule) for m, s in got.items()}
    assert table["conversions"] == ("sum", "A1"), "a marketing count adds"
    assert table["cpm"] == ("none", "R1") and got["cpm"].ratio["scale"] == 1000.0
    assert got["cpm"].ratio["numerator"] == ["spend"]
    assert table["reach"] == ("none", "U1"), "distinct people do not add across rows"
    assert table["bid_amount"][0] != "sum", "an amount named as a bid is a price"
    assert table["conversion_rate_pct"][0] == "none", "a rate word wins over a count word"


def test_attribution_credit_that_sums_to_one_per_conversion_adds():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE touches AS
        SELECT 'CV' || cv AS conversion_id, t AS touch_no,
               CASE n WHEN 1 THEN 1.0 WHEN 2 THEN 0.5 ELSE CASE WHEN t = 3 THEN 0.34 ELSE 0.33 END
               END AS w
        FROM (SELECT i AS cv, 1 + i % 3 AS n FROM range(300) t(i)), range(1, 4) r(t)
        WHERE t <= n""")
    got = sg.suggest(c, "touches", gather(c, "touches"), ["w"])["w"]
    assert (got.agg, got.strength, got.rule) == ("sum", "strong", "A2")
    assert "within every conversion_id" in got.reason


# --- marketing v2 (25/09/2026, scripts/marketing_bench_v2.py) ---------------------------------------

def test_flows_of_a_level_add_and_the_level_does_not():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE subs AS SELECT 'AC' || (i % 50) AS account_id, i // 50 AS mo,
        (20 + i % 50 + i // 50) * 10.0 AS mrr, CASE WHEN i < 50 THEN 200.0 ELSE 0 END AS new_mrr,
        CASE WHEN i % 13 = 0 THEN 30.0 ELSE 0 END AS churned_mrr, 20 + i % 50 + i // 50 AS seats
        FROM range(600) t(i)""")
    got = sg.suggest(c, "subs", gather(c, "subs"), ["mrr", "new_mrr", "churned_mrr", "seats"])
    assert (got["mrr"].agg, got["mrr"].rule) == ("none", "S1")
    assert (got["new_mrr"].agg, got["new_mrr"].rule) == ("sum", "S2")
    assert (got["churned_mrr"].agg, got["churned_mrr"].rule) == ("sum", "S2")
    assert got["seats"].rule == "S1"


def test_money_in_several_currencies_is_not_summed():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE o AS SELECT 'OR' || i AS order_id,
        CASE i % 3 WHEN 0 THEN 'INR' WHEN 1 THEN 'USD' ELSE 'AED' END AS currency,
        100.0 + i AS order_revenue_local, round((100.0 + i) * 0.5, 2) AS order_revenue_usd,
        CASE i % 3 WHEN 0 THEN 10.0 ELSE 5.0 END AS discount_pct
        FROM range(300) t(i)""")
    got = sg.suggest(c, "o", gather(c, "o"), ["order_revenue_local", "order_revenue_usd",
                                               "discount_pct"])
    assert (got["order_revenue_local"].agg, got["order_revenue_local"].rule) == ("none", "C1")
    assert got["order_revenue_usd"].agg == "sum", "named as converted"
    assert got["discount_pct"].rule != "C1", "a percentage is no money"


def test_a_ratio_is_not_found_in_zeros_or_through_a_flag():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE s AS SELECT i % 60 AS a_mo, (20 + i % 40) * 10.0 AS mrr,
        20 + i % 40 AS seats, CASE WHEN i % 50 = 0 THEN 1 ELSE 0 END AS is_churned,
        CASE WHEN i % 50 = 0 THEN (20 + i % 40) * 10.0 ELSE 0 END AS churn_amount_x,
        CASE WHEN i % 7 = 0 THEN 3.0 ELSE 0 END AS contraction_x
        FROM range(3000) t(i)""")
    got = sg.suggest(c, "s", gather(c, "s"), ["mrr", "seats", "churn_amount_x", "contraction_x"])
    assert got["churn_amount_x"].rule != "R1", "mrr / is_churned is not a ratio"
    assert got["contraction_x"].rule != "R1", "zeros match anything"


def test_a_category_code_is_not_a_unit():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE p AS SELECT 'OR' || i AS order_id,
        CASE i % 3 WHEN 0 THEN 'SAVE10' WHEN 1 THEN 'FREESHIP' ELSE 'VIP5' END AS coupon_code,
        CASE i % 3 WHEN 0 THEN 10.0 WHEN 1 THEN 3.0 ELSE 5.0 END AS discount_pct
        FROM range(300) t(i)""")
    assert sg.suggest(c, "p", gather(c, "p"), ["discount_pct"])["discount_pct"].per == []
