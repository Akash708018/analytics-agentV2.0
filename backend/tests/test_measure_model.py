"""A measure that belongs to a coarser unit, a ratio of sums, and a measure over another column.

Cleanup Step 15, RF-O1. Five lines, three orders' worth of order-level columns and two reps'
worth of rep-level ones, built so every trap the retail key names gives a different number from the
right answer: the fee sums to 100 per order and 150 per line; salary averages 1,500 per rep and
1,600 per line; rating averages 3.5 per order and 3.8 per line; the margin is 40.0 as a ratio of
sums and 43.33 as a mean of ratios. End to end through server.py.
"""

from __future__ import annotations

import pytest

from backend.engine import server, workspace
from backend.engine.contract import store
from backend.engine.contract.refusals import Reason, reason_of

W = "measure_model_test"
CSV = """line_id,order_id,rep_id,dept,cat,ts,rev,cost,fee,salary,rating,returned,units
L1,O1,R1,Field,x,2024-01-02,100,60,50,1000,5,true,2
L2,O1,R1,Field,y,2024-01-02,200,150,50,1000,5,false,1
L3,O2,R2,Inside,x,2024-02-03,300,100,0,2000,3,false,3
L4,O3,R2,Inside,y,2024-03-04,400,300,50,2000,4,true,1
L5,O4,R2,Inside,x,2024-03-05,50,20,0,2000,2,false,1
"""
AGGS = {"rev": "sum", "cost": "sum", "units": "sum", "fee": "sum", "salary": "mean",
        "rating": "mean", "returned_rate": "mean", "units_per_line": "mean",
        "gross_margin_pct": "ratio"}
DEFS = {k: f"{k} as the test defines it" for k in AGGS}
BASE = dict(
    grain="one row = one order line", primary_key=["line_id"], date_column="ts",
    measures=list(AGGS), dimensions=["order_id", "rep_id", "dept", "cat"], aggregations=AGGS,
    measure_definitions=DEFS, analysis_window_start="2024-01-01",
    analysis_window_end="2024-12-31",
    measure_per={"fee": ["order_id"], "salary": ["rep_id"], "rating": ["order_id"]},
    measure_columns={"returned_rate": "returned", "units_per_line": "units"},
    ratios={"gross_margin_pct": {"numerator": ["rev", "-cost"], "denominator": ["rev"],
                                 "scale": 100}},
)


@pytest.fixture()
def contracted(tmp_path, monkeypatch):
    workspace.reset(W)
    monkeypatch.setattr(store, "EXPORT_DIR", workspace.workspace_dir(W) / "contracts")
    p = tmp_path / "lines.csv"
    p.write_text(CSV)
    server.load_csv(path=str(p), dataset_name="lines", workspace_id=W)
    text = server.propose_dataset_contract(dataset_name="lines", workspace_id=W, **BASE)
    assert "```" in text, text[:1500]
    body = text.rsplit("```", 2)[-2]
    body = body[4:] if body.startswith("json") else body
    confirmed = server.confirm_dataset_contract(contract_json=body, workspace_id=W)
    assert reason_of(confirmed) is None, confirmed[:800]
    yield
    workspace.reset(W)


def ca(**kw):
    return server.compute_analysis(dataset_name="lines", workspace_id=W, **kw)


def row(text, first):
    return next(ln for ln in text.splitlines() if ln.startswith(f"| {first} |"))


def test_an_order_level_fee_sums_once_per_order(contracted):
    assert "| fee | sum |" in (s := ca(analysis_type="summary_stats")) and "| 100 |" in row(s, "fee")


def test_a_rep_level_salary_averages_over_reps(contracted):
    salary = row(ca(analysis_type="summary_stats"), "salary")
    assert "| 2 |" in salary and "1,500" in salary, salary


def test_an_order_level_rating_averages_over_orders(contracted):
    assert "3.5" in row(ca(analysis_type="summary_stats"), "rating")


def test_a_ratio_is_the_ratio_of_the_sums(contracted):
    assert "| 40 |" in row(ca(analysis_type="summary_stats"), "gross_margin_pct")


def test_a_boolean_read_as_a_rate_and_an_aliased_measure(contracted):
    s = ca(analysis_type="summary_stats")
    assert "0.4" in row(s, "returned_rate") and "1.6" in row(s, "units_per_line")
    assert "| 8 |" in row(s, "units"), "the column the alias reads keeps its own aggregate"


def test_a_per_unit_measure_by_a_dimension_constant_within_the_unit(contracted):
    out = ca(analysis_type="group_compare", dimension="dept", measure="salary")
    assert "1,000" in row(out, "Field") and "2,000" in row(out, "Inside")
    assert "2 rep_id unit(s)" in out


def test_a_per_unit_measure_by_a_dimension_that_varies_within_it_is_refused(contracted):
    out = ca(analysis_type="group_compare", dimension="cat", measure="salary")
    assert reason_of(out) is Reason.ANALYSIS_NOT_POSSIBLE
    assert "cat varies within rep_id" in out


def test_a_rate_by_group(contracted):
    out = ca(analysis_type="group_compare", dimension="cat", measure="returned_rate")
    assert "0.3333" in row(out, "x") and "0.5" in row(out, "y")


def test_a_ratio_by_month(contracted):
    out = ca(analysis_type="trend", measure="gross_margin_pct")
    assert "| 2024-01 | 30 |" in out and "| 2024-03 | 28.8889 |" in out, out


def test_a_ratio_is_refused_where_a_row_value_is_needed(contracted):
    out = ca(analysis_type="distribution", measure="gross_margin_pct")
    assert reason_of(out) is Reason.ANALYSIS_NOT_POSSIBLE and "ratio of sums" in out


def test_a_per_unit_measure_that_varies_within_its_unit_is_refused_at_proposal(tmp_path, monkeypatch):
    workspace.reset(W)
    monkeypatch.setattr(store, "EXPORT_DIR", workspace.workspace_dir(W) / "contracts")
    p = tmp_path / "lines.csv"
    p.write_text(CSV)
    server.load_csv(path=str(p), dataset_name="lines", workspace_id=W)
    bad = dict(BASE, measure_per={**BASE["measure_per"], "rating": ["rep_id"]})
    text = server.propose_dataset_contract(dataset_name="lines", workspace_id=W, **bad)
    assert reason_of(text) is Reason.CONTRACT_INVALID and "rating varies within rep_id" in text
    workspace.reset(W)
