"""Who came back, how often, and whether the module refuses a key that cannot answer.

Phase 10, Step 8. Run from the repo root:

    uv run pytest tests/test_repeat_behaviour.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import repeat_behaviour as _  # noqa: E402,F401
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


# Six people, ten orders. p1 four times, p2 twice, p3 twice, p4 p5 once each.
# o11 has no person; o12 has no date. Twelve rows, so the event counts add to 12.
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
    return run(con, g, scope_for(con, g), "repeat_behaviour", **params)


def said(out, fragment: str) -> bool:
    return any(fragment in s for s in out.summary)


def cell(out, label_value, column):
    row = next(r for r in out.rows if r[0] == label_value)
    return row[out.headers.index(column)]


def test_it_is_registered_in_tier_seven():
    entry = next(c for c in catalogue() if c[0] == "repeat_behaviour")
    assert entry[1] == 7


def test_the_distribution_buckets_people_by_how_often(con):
    """p4 and p5 once, p2 and p3 twice, p1 four times. Five people with a date."""
    out = out_for(con)
    assert cell(out, "once", "people") == "2"
    assert cell(out, "twice", "people") == "2"
    assert cell(out, "four or five times", "people") == "1"


def test_the_repeat_rate_is_reported(con):
    """Three of five came back."""
    out = out_for(con)
    assert said(out, "5 distinct person value(s)")
    assert said(out, "3 came back")
    assert said(out, "60.000%")


def test_the_events_add_back_and_the_people_do_not(con):
    """P10-D52: two books. Events reconcile to scope.analysed; people reconcile to the
    distinct key count, and the summary says the people column is not a row count."""
    out = out_for(con)
    assert cell(out, "(no entity)", "events") == "1"
    assert cell(out, "(no date)", "events") == "1"
    assert said(out, "add back to the 12 in scope")
    assert said(out, "does not add to a row count")


def test_the_gap_to_a_second_event_is_reported(con):
    out = out_for(con)
    assert said(out, "median gap between a first event and a second")
    assert said(out, "day(s)")


def test_the_busiest_and_the_median_are_named(con):
    out = out_for(con)
    assert said(out, "accounts for 4 event(s)")


# --- the refusal ------------------------------------------------------------------------

def test_a_key_distinct_per_row_is_refused_by_name(con):
    """P10-D53, the Olist trap in miniature: order_id is one per row, so keyed on it nobody
    ever returns. That is a fact about the column, not about behaviour."""
    with pytest.raises(ValueError, match="one per row"):
        out_for(con, entity="order_id")


def test_the_refusal_says_what_to_do_instead(con):
    with pytest.raises(ValueError, match="identifies a person across their events"):
        out_for(con, entity="order_id")


def test_a_contract_without_a_date_column_is_refused(con):
    with pytest.raises(ValueError, match="needs a date column"):
        out_for(con, g=gate(date_column=None))


def test_an_undeclared_entity_is_refused(con):
    with pytest.raises(Exception):
        out_for(con, entity="amount")


def test_an_unknown_parameter_is_named(con):
    with pytest.raises(TypeError, match="period"):
        out_for(con, period="month")
