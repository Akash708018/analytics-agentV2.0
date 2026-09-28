"""One measure in two named periods, with what stands behind each of them.

Phase 9, Step 4e. Run from the repo root:

    uv run pytest tests/test_period_compare.py -q

The fixture runs February to May 2017, so a comparison crosses a 28-day month
and a 31-day one, April holds no rows at all, and February holds only the part
of itself after the 10th.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import period_compare as _  # noqa: E402,F401
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
        FakeMeasure("ticket", agg="mean", definition="what one order came to"),
        FakeMeasure("unit_price", agg="none", definition="price of one item"),
    ])
    dimensions: list = field(default_factory=lambda: ["region"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# February holds one row and March two, so a sum is not a row count. April
# holds nothing. The undated row belongs to neither period and to no period.
PAIRED = """SELECT * FROM (VALUES
  (1, TIMESTAMP '2017-02-10 10:00:00', 'north', 20.00, 4.00, 5.00),
  (2, TIMESTAMP '2017-03-05 11:00:00', 'south', 30.00, 4.00, 4.00),
  (3, TIMESTAMP '2017-03-20 11:00:00', 'south', 30.00, 2.00, 4.00),
  (4, TIMESTAMP '2017-05-06 09:00:00', 'north', 10.00, 1.00, 3.00),
  (5, NULL, 'west', 99.00, 9.00, 1.00)
) v(id, ts, region, amount, ticket, unit_price)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE sales AS {PAIRED}")
    yield c
    c.close()


def compared(con, contract=None, **params):
    gate = FakeGate(contract or FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "amount")
    params.setdefault("baseline", "2017-02")
    params.setdefault("period", "2017-03")
    return run(con, gate, scope, "period_compare", **params)


def test_the_two_periods_are_the_rows_in_calendar_order(con):
    assert [r[0] for r in compared(con).rows] == ["2017-02", "2017-03"]


def test_each_period_carries_its_value_rows_and_length(con):
    feb, mar = compared(con).rows
    assert (feb[1], feb[2], feb[3]) == ("20.00", "1", "28")
    assert (mar[1], mar[2], mar[3]) == ("60.00", "2", "31")


def test_the_change_is_stated_with_a_direction_and_an_amount(con):
    text = " ".join(compared(con).summary)
    assert "was 20.00 in 2017-02 and 60.00 in 2017-03: up by 40.00." in text


def test_the_percentage_is_taken_off_the_baseline(con):
    assert "+200.0% of the baseline" in " ".join(compared(con).summary)


def test_the_difference_in_length_is_named_for_an_additive_aggregate(con):
    text = " ".join(compared(con).summary)
    assert "28 calendar days against 31" in text
    assert "+10.7%" in text


def test_the_length_is_not_raised_for_an_aggregate_that_does_not_add(con):
    out = compared(con, measure="ticket")
    assert out.headers[1] == "ticket (mean) GBP", (
        f"{out.headers[1]!r}. The aggregate is the contract's word for it, and "
        "the contract's word is mean -- AGG_SQL has no avg."
    )
    assert "calendar days against" not in " ".join(out.summary), (
        "A mean over 28 days and a mean over 31 are the same kind of number; "
        "only a sum or a count is longer for being longer."
    )


def test_an_absent_period_refuses_a_change_rather_than_falling_to_zero(con):
    out = compared(con, period="2017-04")
    text = " ".join(out.summary)
    assert "did not fall to zero" in text
    assert "-100" not in text and "up by" not in text
    assert [r[1] for r in out.rows] == ["20.00", ""]


def test_a_period_the_calendar_does_not_have_is_refused_with_its_range(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        compared(con, period="2019-11")
    message = str(excinfo.value)
    assert "'2019-11' is not a month" in message
    assert "2017-02 to 2017-05" in message


def test_a_period_against_itself_is_refused(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        compared(con, period="2017-03", baseline="2017-03")
    assert "zero by construction" in str(excinfo.value)


def test_a_baseline_is_required_rather_than_guessed(con):
    with pytest.raises(TypeError):
        gate = FakeGate(FakeContract())
        scope = scope_for(con, gate)
        run(con, gate, scope, "period_compare", measure="amount",
            period="2017-03")


def test_a_non_additive_measure_is_refused(con):
    with pytest.raises(ValueError) as excinfo:
        compared(con, measure="unit_price")
    assert "does not combine across rows" in str(excinfo.value)


def test_a_partial_period_at_the_edge_of_the_calendar_is_named(con):
    text = " ".join(compared(con).summary)
    assert "first month of the calendar" in text
    assert "reads low for that reason alone" in text


def test_an_undated_row_belongs_to_neither_period(con):
    assert "no ts and are in neither month" in " ".join(compared(con).summary)


def test_the_grain_reaches_the_calendar(con):
    out = compared(con, grain="quarter", baseline="2017-Q1", period="2017-Q2")
    assert [r[0] for r in out.rows] == ["2017-Q1", "2017-Q2"]


def test_the_method_note_is_the_first_summary_line(con):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "period_compare", measure="amount",
              baseline="2017-02", period="2017-03")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "period_compare")
    assert entry[1] == 3 and "no rows" in entry[2]


# --- empty months inside a compared year (Cleanup Step 12, RF-O3) --------------------------------
# Retail: "2024 covers 366 days ... sets 366 days of data against 365" with 2024-09 empty.

YEARS = ("SELECT i AS id, TIMESTAMP '2023-01-15' + INTERVAL (i) MONTH AS ts, 'north' AS region, "
         "10.00 AS amount, 1.00 AS ticket, 1.00 AS unit_price FROM range(24) t(i) "
         "WHERE i <> 20")   # 2024-09 is month 20 from 2023-01


def test_an_empty_month_inside_a_compared_year_is_named(con):
    con.execute(f"CREATE OR REPLACE TABLE sales AS {YEARS}")
    text = " ".join(compared(con, baseline="2023", period="2024", grain="year").summary)
    assert "2024 holds no rows in 2024-09" in text
    assert "11 of its 12 months" in text


def test_a_day_count_is_called_calendar_days(con):
    assert "28 calendar days against 31" in " ".join(compared(con).summary)
