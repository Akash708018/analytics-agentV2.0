"""One measure folded onto a cycle, with what each position rests on visible.

Phase 9, Step 4d. Run from the repo root:

    uv run pytest tests/test_seasonality.py -q

The fixture spans January 2017 to March 2018, so the calendar is not a whole
number of years: January and March hold two months each, February holds one and
lost one to a gap, and April through December hold one apiece and lost it. That
is the shape every claim in this module has to survive.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import seasonality as _  # noqa: E402,F401
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


# Two Januaries and two Marches, one February, and a February 2018 that holds
# nothing. January 2018 carries two rows so a position's mean is not the same
# number as a pooled sum over its rows.
CYCLED = """SELECT * FROM (VALUES
  (1, TIMESTAMP '2017-01-15 10:00:00', 'north', 10.00, 5.00),
  (2, TIMESTAMP '2017-02-10 11:00:00', 'south', 20.00, 4.00),
  (3, TIMESTAMP '2017-03-05 11:00:00', 'south', 30.00, 4.00),
  (4, TIMESTAMP '2018-01-20 09:00:00', 'north', 50.00, 3.00),
  (5, TIMESTAMP '2018-01-25 09:00:00', 'north', 40.00, 3.00),
  (6, TIMESTAMP '2018-03-11 09:00:00', 'south', 60.00, 2.00),
  (7, NULL, 'west', 99.00, 1.00)
) v(id, ts, region, amount, unit_price)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE sales AS {CYCLED}")
    yield c
    c.close()


def folded(con, contract=None, **params):
    gate = FakeGate(contract or FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "amount")
    return run(con, gate, scope, "seasonality", **params)


def at(out, position):
    return next(row for row in out.rows if row[0] == position)


def test_the_position_is_named_and_not_numbered(con):
    assert [r[0] for r in folded(con).rows][:3] == ["Jan", "Feb", "Mar"]


def test_the_absent_period_leaves_the_mean_rather_than_zeroing_it(con):
    """February rests on one month. Zeroed, its index would be half this."""
    assert at(folded(con), "Feb")[2] == "0.48", (
        "February's one month reads 20.00 against a grand mean of 42.00. A "
        "zeroed February 2018 would make it 10.00 and the index 0.24."
    )


def test_a_position_reports_the_periods_it_rests_on(con):
    out = folded(con)
    assert (at(out, "Jan")[3], at(out, "Jan")[4]) == ("2", "0")
    assert (at(out, "Feb")[3], at(out, "Feb")[4]) == ("1", "1")


def test_a_position_the_calendar_reached_but_never_filled_is_blank(con):
    apr = at(folded(con), "Apr")
    assert (apr[1], apr[2], apr[3], apr[4]) == ("", "", "0", "1")


def test_the_index_is_a_ratio_of_means(con):
    out = folded(con)
    assert (at(out, "Jan")[2], at(out, "Mar")[2]) == ("1.19", "1.07")


def test_the_rows_behind_a_position_travel_with_it(con):
    assert at(folded(con), "Jan")[5] == "3", (
        "Three rows across two Januaries, and the mean is over the two "
        "months rather than the three rows."
    )


def test_an_undated_row_is_counted_and_not_folded(con):
    out = folded(con)
    assert sum(int(r[5]) for r in out.rows) == 6
    assert "no ts" in " ".join(out.summary)


def test_a_window_that_is_not_whole_cycles_is_named(con):
    text = " ".join(folded(con).summary)
    assert "not a whole number of years" in text


def test_a_balanced_calendar_says_nothing_about_imbalance(con):
    contract = FakeContract(
        analysis_window=FakeWindow(date(2017, 1, 1), date(2018, 12, 31))
    )
    out = folded(con, contract)
    assert "not a whole number" not in " ".join(out.summary)
    assert (at(out, "Jan")[3], at(out, "Apr")[4]) == ("2", "2")


def test_the_quarter_grain_folds_onto_four_positions(con):
    assert [r[0] for r in folded(con, grain="quarter").rows] == [
        "Q1", "Q2", "Q3", "Q4"]


def test_one_position_holding_rows_says_its_index_is_construction(con):
    text = " ".join(folded(con, grain="quarter").summary)
    assert "against itself and says nothing" in text


def test_the_day_grain_folds_onto_the_week(con):
    assert [r[0] for r in folded(con, grain="day").rows] == [
        "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def test_a_year_is_refused_a_cycle(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        folded(con, grain="year")
    assert "nothing for a year to repeat inside" in str(excinfo.value)


def test_a_grain_nobody_offers_is_refused(con):
    with pytest.raises(ParamsInvalid):
        folded(con, grain="fortnight")


def test_a_non_additive_measure_is_refused_not_averaged(con):
    with pytest.raises(ValueError) as excinfo:
        folded(con, measure="unit_price")
    assert "does not combine across rows" in str(excinfo.value)


def test_the_method_note_is_the_first_summary_line(con):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "seasonality", measure="amount")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "seasonality")
    assert entry[1] == 3 and "cycle" in entry[2]
