"""Which periods hold rows, and which are not there at all.

Phase 9, Step 2. Run from the repo root:

    uv run pytest tests/test_calendar_coverage.py -q

The fixture below has a hole in it on purpose. Every other analysis can be
tested against what the data contains; this one is tested against what it does
not, which is the only reason the module exists.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import calendar_coverage as _  # noqa: E402,F401
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
    measures: list = field(default_factory=lambda: [FakeMeasure("amount")])
    dimensions: list = field(default_factory=lambda: ["region"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# September, October, December, January. November is the hole: no row falls in
# it, so no GROUP BY over ts can return it, and a trend drawn over these four
# months crosses a month it never saw. Row 5 is undated -- in scope, in no
# period, and in neither the present nor the missing count.
GAPS = """SELECT * FROM (VALUES
  (1, TIMESTAMP '2016-09-04 10:00:00', 'north', 10.00),
  (2, TIMESTAMP '2016-10-02 11:00:00', 'south', 20.00),
  (3, TIMESTAMP '2016-12-05 09:00:00', 'north', 30.00),
  (4, TIMESTAMP '2017-01-09 09:00:00', 'south', 40.00),
  (5, NULL, 'west', 50.00)
) v(id, ts, region, amount)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE sales AS {GAPS}")
    yield c
    c.close()


def cover(con, contract=None, **params):
    """Run calendar_coverage the way the tool layer runs it: one scope, built
    by scope_for from the gate, handed in rather than rebuilt."""
    gate = FakeGate(contract or FakeContract())
    scope = scope_for(con, gate)
    return run(con, gate, scope, "calendar_coverage", **params)


def periods(out) -> dict:
    return {row[0]: row[1] for row in out.rows}


# --------------------------------------------------------------------------
# the hole
# --------------------------------------------------------------------------


def test_a_month_with_no_rows_is_a_row_reading_zero(con):
    got = periods(cover(con))
    assert got["2016-11"] == "0", (
        f"November is {got.get('2016-11')!r} in {sorted(got)}. It holds no rows "
        "and has to appear anyway -- that is the whole analysis. The cell is "
        "the string base.number() renders, not an int: a count reaches a table "
        "already formatted, the way every other analysis renders one."
    )


def test_the_group_by_this_replaces_cannot_return_that_month(con):
    """Why the calendar is generated rather than read off the rows."""
    grouped = con.execute(
        "SELECT strftime(date_trunc('month', ts), '%Y-%m') FROM sales "
        "WHERE ts IS NOT NULL GROUP BY 1"
    ).fetchall()
    assert "2016-11" not in {r[0] for r in grouped}
    assert "2016-11" in periods(cover(con))


def test_the_missing_month_is_named_in_the_summary(con):
    text = " ".join(cover(con).summary)
    assert "2016-11" in text, (
        "the absent month is in the table but not in the sentences, so a "
        "reader who sees only the summary does not know it is there."
    )


def test_the_longest_gap_counts_consecutive_periods(con):
    con.execute("DELETE FROM sales WHERE id IN (2, 3)")
    text = " ".join(cover(con).summary)
    assert "3 month(s)" in text, (
        f"October, November and December are now all empty and the summary "
        f"says: {text}"
    )


# --------------------------------------------------------------------------
# where the bounds come from
# --------------------------------------------------------------------------


def test_with_no_window_the_bounds_are_the_observed_span(con):
    out = cover(con)
    assert list(periods(out)) == ["2016-09", "2016-10", "2016-11", "2016-12",
                                  "2017-01"]
    assert "observed span" in " ".join(out.summary)


def test_with_a_window_the_bounds_are_the_window(con):
    contract = FakeContract(
        analysis_window=FakeWindow(date(2016, 10, 1), date(2016, 12, 31))
    )
    out = cover(con, contract)
    assert list(periods(out)) == ["2016-10", "2016-11", "2016-12"]
    assert "declared analysis window" in " ".join(out.summary), (
        "the window and the span bound the calendar differently and the "
        "summary has to say which one did -- one is a decision somebody made "
        "and the other is a fact about the data."
    )


def test_a_window_opening_mid_period_says_that_period_is_partial(con):
    contract = FakeContract(
        analysis_window=FakeWindow(date(2016, 9, 15), date(2016, 12, 31))
    )
    text = " ".join(cover(con, contract).summary)
    assert "2016-09-15" in text and "part of its period" in text


# --------------------------------------------------------------------------
# the rows that are in scope and in no period
# --------------------------------------------------------------------------


def test_an_undated_row_is_counted_and_not_bucketed(con):
    out = cover(con)
    assert sum(int(str(v).replace(",", "")) for v in periods(out).values()) == 4
    assert "no ts" in " ".join(out.summary), (
        "the undated row is in scope and in no period; unsaid, the four "
        "counted rows silently disagree with the five analysed."
    )


def test_the_method_note_is_the_first_summary_line(con):
    """The tool layer refuses a result whose first line is not the note."""
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "calendar_coverage")
    assert out.summary[0] == scope.method_note()


def test_a_scope_with_no_dated_rows_returns_no_periods(con):
    con.execute("DELETE FROM sales WHERE ts IS NOT NULL")
    out = cover(con)
    assert out.rows == []
    assert "no calendar to cover" in " ".join(out.summary)


# --------------------------------------------------------------------------
# the grain
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grain,first,count",
    [("day", "2016-09-04", 128), ("week", "2016-08-29", 20),
     ("month", "2016-09", 5), ("quarter", "2016-Q3", 3), ("year", "2016", 2)],
)
def test_each_grain_labels_its_periods_and_counts_them(con, grain, first, count):
    out = cover(con, grain=grain)
    assert out.rows[0][0] == first
    assert len(out.rows) == count, (
        f"{grain} produced {len(out.rows)} period(s) starting {out.rows[0][0]}."
    )


def test_a_grain_nobody_offers_is_refused_with_the_ones_on_offer(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        cover(con, grain="fortnight")
    for name in ("day", "week", "month", "quarter", "year"):
        assert name in str(excinfo.value)


def test_an_argument_for_another_analysis_is_refused_not_ignored(con):
    with pytest.raises(TypeError):
        cover(con, bins=10)


# --------------------------------------------------------------------------
# what the contract has to say first
# --------------------------------------------------------------------------


def test_without_a_date_column_there_is_no_calendar(con):
    with pytest.raises(ValueError) as excinfo:
        cover(con, FakeContract(date_column=None))
    assert "confirm_dataset_contract" in str(excinfo.value), (
        "a refusal names the call that fixes it; without one the agent knows "
        "only that it failed."
    )


def test_a_tz_aware_column_states_the_zone_its_periods_were_cut_in(con):
    con.execute("SET TimeZone='Asia/Kolkata'")
    con.execute("DROP TABLE sales")
    con.execute(
        "CREATE TABLE sales AS SELECT * FROM (VALUES "
        "(1, TIMESTAMPTZ '2017-03-14 23:30:00+00', 'north', 10.00)) "
        "v(id, ts, region, amount)"
    )
    out = cover(con, grain="day")
    assert out.rows[0][0] == "2017-03-15", (
        "23:30 UTC is the next day at +05:30, and the bucket follows the "
        "session zone rather than the instant."
    )
    assert "Asia/Kolkata" in " ".join(out.summary)


def test_a_naive_column_says_nothing_about_a_zone(con):
    """A zone line over buckets it cannot have affected teaches a reader to
    skip the line that matters."""
    assert "TimeZone" not in " ".join(cover(con).summary)
    assert "Asia/Kolkata" not in " ".join(cover(con).summary)


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "calendar_coverage")
    assert entry[1] == 3
    assert "absent" in entry[2]
