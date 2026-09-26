"""contract/llm_filter.py: a model fills the contract, the data filters every choice (25/09/2026).

The model here is scripted: a JSON answer with the mistakes models make -- a sum on a price, a
coordinate, a ratio, a zip code; a unit missed; a unit that does not hold; a key that repeats;
arithmetic on text; a column that does not exist. Every one must be caught. Run:

    uv run pytest tests/test_llm_filter.py -q
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine.contract import llm_filter as lf
from backend.engine.contract.evidence import gather, suggest_role
from backend.engine.contract.propose import propose_contract


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
      CREATE TABLE deliveries AS
      SELECT 'O' || (i // 2) AS order_id, (i % 2) + 1 AS line_no,
             DATE '2024-01-01' + CAST(i % 90 AS INTEGER) AS order_date,
             (i * 7) % 50 * 10.0 + 100 AS unit_price,
             ((i * 13) % 900) + 100.0 AS order_value,
             round(((i * 13) % 900 + 100.0) * 0.7, 2) AS cost,
             round(100.0 * (((i * 13) % 900 + 100.0) - round(((i * 13) % 900 + 100.0) * 0.7, 2))
                   / (((i * 13) % 900) + 100.0), 2) AS margin_pct,
             ((i // 2) % 3) * 20.0 AS delivery_fee,
             18.5 + (i % 50) / 1000.0 AS drop_lat,
             73.8 + (i % 50) / 1000.0 AS drop_lng,
             411000 + (i * 37) % 900 AS zip,
             (i * 3) % 30 + 0.5 AS distance_km,
             (i * 11) % 80 AS gps_drop_distance_m,
             CASE WHEN i % 5 = 0 THEN 1 ELSE 0 END AS is_late,
             CASE WHEN i % 3 = 0 THEN 'Pune_West' ELSE 'Pune_East' END AS zone
      FROM range(400) t(i)""")
    yield c
    c.close()


MODEL = {
    "grain": "one row = one item on one delivery order",
    "primary_key": ["order_id"],                      # repeats: two lines per order
    "date_column": "order_date",
    "columns": [
        {"name": "unit_price", "role": "measure", "agg": "sum", "confidence": 0.9,
         "meaning": "price of one item"},
        {"name": "order_value", "role": "measure", "agg": "sum", "confidence": 0.95,
         "meaning": "value of the line in INR"},
        {"name": "cost", "role": "measure", "agg": "sum", "confidence": 0.9},
        {"name": "margin_pct", "role": "measure", "agg": "sum", "confidence": 0.8},
        {"name": "delivery_fee", "role": "measure", "agg": "sum", "confidence": 0.9},
        {"name": "drop_lat", "role": "measure", "agg": "sum", "confidence": 0.6},
        {"name": "drop_lng", "role": "measure", "agg": "mean", "confidence": 0.6},
        {"name": "zip", "role": "measure", "agg": "sum", "confidence": 0.5},
        {"name": "distance_km", "role": "measure", "agg": "sum", "per": ["zone"],
         "confidence": 0.85},
        {"name": "gps_drop_distance_m", "role": "measure", "agg": "sum", "confidence": 0.7},
        {"name": "zone", "role": "measure", "agg": "sum", "confidence": 0.3},
        {"name": "rider_rating", "role": "measure", "agg": "mean"},
        {"name": "is_late", "role": "dimension"},
    ],
}


def run(con, model=MODEL):
    ev = gather(con, "deliveries")
    roles = {c.name: suggest_role(c)[0] for c in ev.columns}
    return lf.check(con, "deliveries", ev, model, roles, model="scripted")


def test_every_mistake_is_caught(con):
    f = run(con)
    by = f.by_path()
    verdict = lambda p: (by[p].status, by[p].final)
    assert verdict("measures[unit_price].agg") == ("overruled", "none")
    assert verdict("measures[margin_pct].agg") == ("overruled", "none")
    assert verdict("measures[drop_lat].agg") == ("overruled", "none")
    assert verdict("measures[drop_lng].agg")[0] == "agree", "mean of a coordinate: non-additive"
    assert verdict("measures[zip].agg") == ("overruled", "none")
    assert verdict("measures[gps_drop_distance_m].agg") == ("overruled", "none")
    assert verdict("measures[zone].agg") == ("blocked", None)
    assert verdict("columns[rider_rating]") == ("blocked", None)
    assert verdict("primary_key") == ("overruled", [])
    assert "repeats in 200 row(s)" in by["primary_key"].reason


def test_what_the_data_agrees_with_or_cannot_check(con):
    by = run(con).by_path()
    assert by["measures[order_value].agg"].status == "agree"
    assert by["measures[cost].agg"].status == "agree"
    assert by["measures[distance_km].agg"].status == "llm_only", "a distance may sum"
    assert by["measures[unit_price].definition"].status == "llm_only"
    assert by["grain"].status == "llm_only"
    assert by["date_column"].status == "agree"


def test_units_are_verified_and_found(con):
    by = run(con).by_path()
    assert (by["measures[delivery_fee].per"].status,
            by["measures[delivery_fee].per"].final) == ("data", ["order_id"])
    assert (by["measures[distance_km].per"].status,
            by["measures[distance_km].per"].final) == ("overruled", [])


def test_the_data_adds_what_it_derives(con):
    f = run(con)
    assert f.answers["ratios"]["margin_pct_of_sums"]["numerator"] == ["order_value", "-cost"]
    assert f.answers["measure_columns"] == {"is_late_rate": "is_late"}
    assert "is_late" in f.answers["dimensions"]
    assert f.answers["analysis_window"][0].isoformat() == "2024-01-01"


def test_the_filtered_answers_make_a_contract(con):
    f = run(con)
    kw = dict(f.answers)
    p = propose_contract(con, "deliveries", **kw)
    by = {m.name: m for m in p.contract.measures}
    assert by["unit_price"].agg == "none" and by["delivery_fee"].per == ["order_id"]
    assert by["margin_pct_of_sums"].agg == "ratio" and by["is_late_rate"].column == "is_late"
    assert "zone" not in by and "zip" in by


def test_an_unsure_model_on_what_the_data_cannot_check_leaves_a_blank(con):
    model = {"columns": [{"name": "distance_km", "role": "measure", "agg": "sum",
                          "confidence": 0.4}]}
    by = run(con, model).by_path()
    assert by["measures[distance_km].agg"].final is None
    assert "unsure (40%)" in by["measures[distance_km].agg"].reason


def test_counts_say_how_the_model_chose(con):
    n = run(con).counts()
    assert n["overruled"] >= 6 and n["blocked"] == 2 and n["agree"] >= 3


def test_a_mean_of_a_ratio_is_overruled(con):
    """The live bench (25/09/2026): gpt-oss said margin_pct 'mean', and the filter let it pass as
    'both non-additive'. The mean of a ratio over rows is the retail C5 trap (25.79% vs 18.40%)."""
    model = {"columns": [{"name": "margin_pct", "role": "measure", "agg": "mean",
                          "confidence": 0.85},
                         {"name": "order_value", "role": "measure", "agg": "sum"}]}
    f = run(con, model)
    chk = f.by_path()["measures[margin_pct].agg"]
    assert (chk.status, chk.final, chk.llm) == ("overruled", "none", "mean")
    assert "margin_pct_of_sums" in f.answers["ratios"], "found although cost is not a measure"
