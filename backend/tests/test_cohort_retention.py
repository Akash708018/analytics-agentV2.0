"""The grid, and whether it declines to draw one when the shape would lie.

Phase 10, Step 8. Run from the repo root:

    uv run pytest tests/test_cohort_retention.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import cohort_retention as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "what the order came to"
    unit: str | None = None


@dataclass
class FakeContract:
    dataset_name: str = "orders"
    date_column: str | None = "placed"
    analysis_window: object | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [FakeMeasure("amount")])
    dimensions: list = field(default_factory=lambda: ["person", "order_id", "region"])
    primary_key: list = field(default_factory=lambda: ["order_id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# Same twelve rows as test_repeat_behaviour, so the two analyses can be read against each other.
# p1 Jan/Feb/Mar/Jul, p2 Jan/Mar, p3 Feb twice, p4 Feb, p5 Mar.
# January cohort: p1, p2. February: p3, p4. March: p5.
ORDERS = """SELECT * FROM (VALUES
 ('o1' ,'p1','north',DATE '2017-01-05', 10.0),
 ('o2' ,'p1','north',DATE '2017-02-09', 12.0),
 ('o3' ,'p1','south',DATE '2017-03-02', 11.0),
 ('o4' ,'p1','south',DATE '2017-07-02', 13.0),
 ('o5' ,'p2','north',DATE '2017-01-20', 20.0),
 ('o6' ,'p2','east' ,DATE '2017-03-30', 22.0),
 ('o7' ,'p3','east' ,DATE '2017-02-14', 21.0),
 ('o8' ,'p3','east' ,DATE '2017-02-28', 19.0),
 ('o9' ,'p4','north',DATE '2017-02-02', 23.0),
 ('o10','p5','south',DATE '2017-03-19', 18.0),
 ('o11',NULL ,'south',DATE '2017-03-21', 15.0),
 ('o12','p6','north',NULL            , 17.0)
) v(order_id, person, region, placed, amount)"""


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE orders AS {ORDERS}")
    yield c
    c.close()


def gate(**kw) -> FakeGate:
    caveats = kw.pop("caveats", [])
    return FakeGate(contract=FakeContract(**kw), caveats=caveats)


def out_for(con, g=None, **params):
    g = g or gate()
    params.setdefault("entity", "person")
    return run(con, g, scope_for(con, g), "cohort_retention", **params)


def said(out, fragment: str) -> bool:
    return any(fragment in s for s in out.summary)


def row_for(out, cohort):
    return next(r for r in out.rows if r[0] == cohort)


def test_it_is_registered_in_tier_seven():
    entry = next(c for c in catalogue() if c[0] == "cohort_retention")
    assert entry[1] == 7


def test_the_cohorts_are_the_first_active_period(con):
    """p1 and p2 started in January, p3 and p4 in February, p5 in March."""
    out = out_for(con)
    labels = [r[0] for r in out.rows if not r[0].startswith("(")]
    assert labels == ["2017-01-01", "2017-02-01", "2017-03-01"]


def test_the_size_column_holds_the_cohort(con):
    out = out_for(con)
    size = out.headers.index("size")
    assert row_for(out, "2017-01-01")[size] == "2"
    assert row_for(out, "2017-02-01")[size] == "2"
    assert row_for(out, "2017-03-01")[size] == "1"


def test_the_cells_count_people_at_each_offset(con):
    """January: both at +0, p1 alone at +1 and p1 and p2 at +2, p1 alone at +6.
    February: both at +0, p1 is not in this cohort so +1 is empty."""
    out = out_for(con)
    jan = row_for(out, "2017-01-01")
    assert jan[out.headers.index("+0")] == "2"
    assert jan[out.headers.index("+1")] == "1"
    assert jan[out.headers.index("+2")] == "2"
    assert jan[out.headers.index("+6")] == "1"
    assert jan[out.headers.index("+3")] is None


def test_the_grid_says_the_cells_are_people(con):
    out = out_for(con)
    assert said(out, "Cells are people, not percentages")
    assert said(out, "any cutoff would be a number nobody measured")


def test_every_person_belongs_to_exactly_one_cohort(con):
    """P10-D52: the accounting a grid can keep. Sizes sum to the people counted."""
    out = out_for(con)
    size = out.headers.index("size")
    total = sum(int(r[size]) for r in out.rows if r[size] is not None)
    assert total == 5


def test_the_excluded_rows_are_listed(con):
    out = out_for(con)
    labels = [r[0] for r in out.rows]
    assert "(no entity)" in labels
    assert "(no date)" in labels
    assert said(out, "add back to the 12 in scope")
    assert said(out, "does not reconcile to a row count")


def test_a_healthy_repeat_rate_is_reported_without_a_warning(con):
    """Three of five came back: 60%, well above the guide's 5%."""
    out = out_for(con)
    assert said(out, "60.000%")
    assert said(out, "enough for the rows to be worth comparing")
    assert not said(out, "stops saying anything")


# --- the redirect -----------------------------------------------------------------------

def test_a_low_repeat_rate_warns_and_names_repeat_behaviour(con):
    """The guide's rule, on a table where almost nobody returns: one repeater among
    twenty-one people, 4.762%. Twenty would be exactly 5.0% and the condition is
    strictly below, so the boundary is worth sitting just outside rather than on."""
    con.execute("CREATE TABLE sparse AS SELECT * FROM (VALUES "
                "('a','p1',DATE '2017-01-05'), ('b','p1',DATE '2017-02-05'), "
                "('c','p2',DATE '2017-01-06'), ('d','p3',DATE '2017-01-07'), "
                "('e','p4',DATE '2017-01-08'), ('f','p5',DATE '2017-01-09'), "
                "('g','p6',DATE '2017-02-10'), ('h','p7',DATE '2017-02-11'), "
                "('i','p8',DATE '2017-02-12'), ('j','p9',DATE '2017-03-13'), "
                "('k','p10',DATE '2017-03-14'), ('l','p11',DATE '2017-03-15'), "
                "('m','p12',DATE '2017-03-16'), ('n','p13',DATE '2017-03-17'), "
                "('o','p14',DATE '2017-03-18'), ('p','p15',DATE '2017-03-19'), "
                "('q','p16',DATE '2017-03-20'), ('r','p17',DATE '2017-03-21'), "
                "('s','p18',DATE '2017-03-22'), ('t','p19',DATE '2017-03-23'), "
                "('u','p20',DATE '2017-03-24'), "
                "('v','p21',DATE '2017-03-25')) v(order_id, person, placed)")
    g = FakeGate(contract=FakeContract(dataset_name="sparse", dimensions=["person", "order_id"],
                                       measures=[]))
    out = run(con, g, scope_for(con, g), "cohort_retention", entity="person")
    assert said(out, "stops saying anything")
    assert said(out, "repeat_behaviour")
    assert said(out, "without a grid implying a curve that is not there")


# --- refusals ---------------------------------------------------------------------------

def test_a_key_distinct_per_row_is_refused_by_name(con):
    """P10-D53 and Step 1 Part D: keyed on the order, every cohort has one member and every
    cell is zero. A clean grid of zeros reads like a finding."""
    with pytest.raises(ValueError, match="one per row"):
        out_for(con, entity="order_id")


def test_that_refusal_explains_what_the_grid_would_have_shown(con):
    with pytest.raises(ValueError, match="retention would be zero in every cell"):
        out_for(con, entity="order_id")


def test_a_contract_without_a_date_column_is_refused(con):
    with pytest.raises(ValueError, match="needs a date column"):
        out_for(con, g=gate(date_column=None))


def test_an_unknown_period_is_refused(con):
    with pytest.raises(ValueError, match="not one of"):
        out_for(con, period="fortnight")


def test_a_quarterly_grid_is_narrower_than_a_monthly_one(con):
    monthly = out_for(con)
    quarterly = out_for(con, period="quarter")
    assert len(quarterly.headers) < len(monthly.headers)
    assert said(quarterly, "quarterly cohort(s)")


def test_an_unknown_parameter_is_named(con):
    with pytest.raises(TypeError, match="cutoff"):
        out_for(con, cutoff=3)
