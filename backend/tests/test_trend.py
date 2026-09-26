"""One declared measure per period, with the gaps visible.

Phase 9, Step 4. Run from the repo root:

    uv run pytest tests/test_trend.py -q

The fixture has a month with no rows in it, because the guide's own gold
question is that a trend request on a dataset with a missing quarter must
produce a gap warning rather than a clean line.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import trend as _  # noqa: E402,F401
from backend.engine.analysis.base import ParamsInvalid, scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "what was charged"
    unit: str | None = "GBP"


@dataclass
class FakeWindow:
    start: date
    end: date


@dataclass
class FakeContract:
    dataset_name: str = "sales"
    date_column: str | None = "ts"
    analysis_window: FakeWindow | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [
        FakeMeasure("amount"),
        FakeMeasure("unit_price", agg="none", definition="price of one item"),
    ])
    dimensions: list = field(default_factory=lambda: ["region"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# September, October, December, January. November holds nothing, and the two
# rows in October are there so a per-period sum is not the same number as a
# per-period row count.
GAPS = """SELECT * FROM (VALUES
  (1, TIMESTAMP '2016-09-04 10:00:00', 'north', 10.00, 5.00),
  (2, TIMESTAMP '2016-10-02 11:00:00', 'south', 20.00, 4.00),
  (3, TIMESTAMP '2016-10-20 11:00:00', 'south', 30.00, 4.00),
  (4, TIMESTAMP '2016-12-05 09:00:00', 'north', 40.00, 3.00),
  (5, TIMESTAMP '2017-01-09 09:00:00', 'south', 50.00, 2.00),
  (6, NULL, 'west', 60.00, 1.00)
) v(id, ts, region, amount, unit_price)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE sales AS {GAPS}")
    yield c
    c.close()


def trended(con, contract=None, **params):
    gate = FakeGate(contract or FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "amount")
    return run(con, gate, scope, "trend", **params)


def cells(out):
    return {row[0]: row[1] for row in out.rows}


def test_the_absent_period_is_a_row_with_no_value(con):
    got = cells(trended(con))
    assert got["2016-11"] == "", (
        f"November reads {got['2016-11']!r}. A blank says no rows; a zero says "
        "the period happened and came to nothing, which the data does not say."
    )


def test_the_periods_that_hold_rows_carry_their_measure(con):
    got = cells(trended(con))
    assert got["2016-09"] == "10.00" and got["2016-10"] == "50.00", (
        f"{got}. base.number renders a DECIMAL sum to two places, which is "
        "what a currency measure should look like in a cell."
    )


def test_the_row_count_travels_beside_the_value(con):
    counts = {row[0]: row[2] for row in trended(con).rows}
    assert counts["2016-10"] == "2" and counts["2016-11"] == "0", (
        "50 from two rows and 10 from one are different claims, and the count "
        "is what distinguishes them."
    )


def test_the_gap_is_warned_about_in_words(con):
    text = " ".join(trended(con).summary)
    assert "2016-11" in text and "not" in text and "zero" in text


def test_the_direction_is_two_endpoints_and_says_so(con):
    text = " ".join(trended(con).summary)
    assert "From 10.00 in 2016-09 to 50.00 in 2017-01" in text
    assert "not a fitted line" in text


def test_a_gap_is_named_in_the_direction_sentence(con):
    """The guide's gold question: a gap warning, not a clean line."""
    text = " ".join(trended(con).summary)
    assert "interpolating" in text


def test_a_dataset_with_no_gaps_says_nothing_about_them(con):
    con.execute("DELETE FROM sales WHERE id IN (4, 5)")
    text = " ".join(trended(con).summary)
    assert "hold no rows" not in text
    assert "not a fitted line" in text


def test_an_undated_row_is_counted_and_not_bucketed(con):
    out = trended(con)
    assert sum(int(r[2]) for r in out.rows) == 5
    assert "no ts" in " ".join(out.summary)


def test_a_non_additive_measure_is_refused_not_averaged(con):
    with pytest.raises(ValueError) as excinfo:
        trended(con, measure="unit_price")
    assert "does not combine across rows" in str(excinfo.value)


def test_a_measure_nobody_declared_is_refused(con):
    with pytest.raises(Exception):
        trended(con, measure="profit")


def test_the_grain_is_the_calendars(con):
    out = trended(con, grain="quarter")
    assert [r[0] for r in out.rows] == ["2016-Q3", "2016-Q4", "2017-Q1"]


def test_a_grain_nobody_offers_is_refused(con):
    with pytest.raises(ParamsInvalid):
        trended(con, grain="fortnight")


def test_the_window_bounds_the_calendar_when_there_is_one(con):
    contract = FakeContract(
        analysis_window=FakeWindow(date(2016, 10, 1), date(2016, 12, 31))
    )
    out = trended(con, contract)
    assert [r[0] for r in out.rows] == ["2016-10", "2016-11", "2016-12"]
    assert "declared analysis window" in " ".join(out.summary)


def test_the_method_note_is_the_first_summary_line(con):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "trend", measure="amount")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "trend")
    assert entry[1] == 3 and "gap" in entry[2]


# --------------------------------------------------------------------------
# split by a declared dimension (Cleanup Step 8)
#
# The bunty_babli run asked for order_value by month split by channel, and no analysis took a
# period and a dimension together. GAPS by region: north in September and December, south in
# October and January, November empty, and west only on the undated row.

def by_region(con):
    return trended(con, dimension="region")


def test_a_split_has_one_column_per_member_then_all_then_rows(con):
    assert by_region(con).headers == ["period", "north", "south", "(all)", "rows"], (
        "west is on the undated row only, so it can land in no period and has no column; the "
        "undated sentence already says where that row went."
    )


def test_a_member_with_no_rows_in_a_period_is_blank_not_zero(con):
    got = {r[0]: r[1:] for r in by_region(con).rows}
    assert got["2016-09"] == ["10.00", "", "10.00", "1"]
    assert got["2016-10"] == ["", "50.00", "50.00", "2"]


def test_a_period_with_no_rows_at_all_is_still_a_row(con):
    got = {r[0]: r[1:] for r in by_region(con).rows}
    assert got["2016-11"] == ["", "", "", "0"]
    assert "2016-11" in " ".join(by_region(con).summary)


def test_the_all_column_is_the_unsplit_trend(con):
    split = {r[0]: r[3] for r in by_region(con).rows}
    whole = {r[0]: r[1] for r in trended(con).rows}
    assert split == whole


def test_the_split_says_what_a_blank_cell_means_and_names_each_members_endpoints(con):
    text = " ".join(by_region(con).summary)
    assert "not zero" in text
    assert "north: from 10.00 in 2016-09 to 40.00 in 2016-12" in text
    assert "south: from 50.00 in 2016-10 to 50.00 in 2017-01" in text


def test_an_undeclared_dimension_is_refused_by_name(con):
    with pytest.raises(ValueError, match="not a declared dimension"):
        trended(con, dimension="unit_price")


def test_a_split_wider_than_the_table_limit_is_refused(con):
    con.execute("CREATE OR REPLACE TABLE sales AS SELECT i AS id, "
                "TIMESTAMP '2016-09-01' + INTERVAL (i) DAY AS ts, 'r' || i AS region, "
                "1.0 AS amount, 1.0 AS unit_price FROM range(60) t(i)")
    with pytest.raises(ValueError, match="capped at"):
        trended(con, dimension="region")
