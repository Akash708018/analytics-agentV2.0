"""Two measures against each other, linear and by rank, with the pairs counted.

Phase 9, Step 5a. Run from the repo root:

    uv run pytest tests/test_correlation.py -q

The fixture is perfectly ordered and not straight: price and freight rise
together across five pairs, but the last pair is far out, so Pearson lags
Spearman by a quarter. That gap is the reason both coefficients are reported.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import correlation as _  # noqa: E402,F401
from backend.engine.analysis.base import ParamsInvalid, scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "a number about an order"
    unit: str | None = None


@dataclass
class FakeContract:
    dataset_name: str = "sales"
    date_column: str | None = "ts"
    analysis_window: object | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [
        FakeMeasure("price"),
        FakeMeasure("freight"),
        FakeMeasure("unit_price", agg="none", definition="price of one item"),
        FakeMeasure("flat", definition="a column that never varies"),
    ])
    dimensions: list = field(default_factory=lambda: ["region"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# Five complete pairs, rising together, with the fifth far out of line. One row
# has no freight and one has no price, so neither is a pair. flat is one value
# in every row.
PAIRS = """SELECT * FROM (VALUES
  (1, 1.0,   2.0, 5.0, 7.0),
  (2, 2.0,   4.0, 4.0, 7.0),
  (3, 3.0,   6.0, 3.0, 7.0),
  (4, 4.0,   9.0, 2.0, 7.0),
  (5, 5.0, 100.0, 1.0, 7.0),
  (6, 6.0,  NULL, 1.0, 7.0),
  (7, NULL,  7.0, 1.0, 7.0)
) v(id, price, freight, unit_price, flat)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE sales AS {PAIRS}")
    yield c
    c.close()


def correlated(con, **params):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "price")
    params.setdefault("against", "freight")
    return run(con, gate, scope, "correlation", **params)


def test_both_coefficients_are_reported(con):
    row = correlated(con).rows[0]
    assert (row[3], row[4]) == ("+0.749", "+1.000"), (
        "Perfectly ordered, not straight. Reporting either number alone "
        "answers a question the reader did not get to choose."
    )


def test_the_pair_count_and_the_rows_without_a_pair_travel_together(con):
    row = correlated(con).rows[0]
    assert (row[2], row[5]) == ("5", "2")


def test_a_row_missing_either_value_is_not_a_pair(con):
    assert "not a pair" in " ".join(correlated(con).summary)


def test_the_disagreement_between_the_two_is_named(con):
    assert "disagree by 0.251" in " ".join(correlated(con).summary)


def test_a_constant_column_is_undefined_and_not_zero(con):
    out = correlated(con, against="flat")
    assert (out.rows[0][3], out.rows[0][4]) == ("", ""), (
        "DuckDB returns nan for a constant column, and nan reaches a cell as "
        "the string 'nan' unless it is turned back into an absence."
    )
    assert "undefined rather than zero" in " ".join(out.summary)


def test_a_measure_that_cannot_be_summed_can_still_be_correlated(con):
    out = correlated(con, measure="unit_price", against="price")
    assert out.rows[0][3] != "", (
        "agg='none' refuses a trend because a value per period would be "
        "invented. A correlation never combines values, so it does not apply."
    )


def test_a_column_against_itself_is_refused(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        correlated(con, against="price")
    assert "with itself at 1.000" in str(excinfo.value)


def test_a_measure_nobody_declared_is_refused(con):
    with pytest.raises(Exception):
        correlated(con, against="profit")


def test_below_three_pairs_no_coefficient_is_reported(con):
    con.execute("DELETE FROM sales WHERE id > 2")
    out = correlated(con)
    assert (out.rows[0][3], out.rows[0][4]) == ("", ""), (
        "Any two points lie on a line. A cell reading +1.000 beneath a "
        "summary saying no coefficient is reported is worse than either."
    )
    assert "below 3" in " ".join(out.summary)


def test_the_pair_count_is_still_reported_when_the_coefficients_are_not(con):
    con.execute("DELETE FROM sales WHERE id > 2")
    assert correlated(con).rows[0][2] == "2"


def test_no_coefficient_is_called_strong_or_weak(con):
    assert "called strong or weak here" in " ".join(correlated(con).summary)


def test_neither_column_is_said_to_move_the_other(con):
    text = " ".join(correlated(con).summary)
    assert "a third column driving both produces these same two numbers" in text


def test_the_method_note_is_the_first_summary_line(con):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "correlation", measure="price",
              against="freight")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "correlation")
    assert entry[1] == 4 and "Spearman" in entry[2]
