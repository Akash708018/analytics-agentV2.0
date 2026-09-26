"""A relationship a coefficient cannot see, in bins of value.

Phase 9, Step 5b. Run from the repo root:

    uv run pytest tests/test_bivariate.py -q

The fixture is a symmetric U: `spend` against `age` correlates at exactly
0.000 while the binned means read 16.25, 4.25, 0.25, 4.25, 16.25. That is the
case this analysis exists for, and it is why the correlation test file and this
one describe the same kind of data differently.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import bivariate as _  # noqa: E402,F401
from backend.engine.analysis.base import ParamsInvalid, scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "a number about a customer"
    unit: str | None = None


@dataclass
class FakeContract:
    dataset_name: str = "customers"
    date_column: str | None = "ts"
    analysis_window: object | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [
        FakeMeasure("age"),
        FakeMeasure("spend"),
        FakeMeasure("tier", definition="a column with four distinct values"),
    ])
    dimensions: list = field(default_factory=lambda: ["region"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# age 1..10 and spend = (age - 5.5) squared, which is a perfect relationship
# with a correlation of exactly zero. tier takes four values across ten rows,
# so binning it by row would split its ties. Two rows are missing a spend.
CURVED = """SELECT * FROM (VALUES
  (1,  1.0, 20.25, 1.0), (2,  2.0, 12.25, 1.0), (3,  3.0,  6.25, 1.0),
  (4,  4.0,  2.25, 2.0), (5,  5.0,  0.25, 2.0), (6,  6.0,  0.25, 2.0),
  (7,  7.0,  2.25, 3.0), (8,  8.0,  6.25, 3.0), (9,  9.0, 12.25, 4.0),
  (10, 10.0, 20.25, 4.0),
  (11, 11.0, NULL,  4.0), (12, NULL, 30.25, 4.0)
) v(id, age, spend, tier)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE customers AS {CURVED}")
    yield c
    c.close()


def binned(con, **params):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "age")
    params.setdefault("against", "spend")
    return run(con, gate, scope, "bivariate", **params)


def test_the_shape_is_visible_where_a_coefficient_is_not(con):
    out = binned(con, bins=5)
    assert [r[4] for r in out.rows] == [
        "16.25", "4.25", "0.25", "4.25", "16.25"], (
        "These same pairs correlate at 0.000. The bins are the whole reason "
        "this analysis exists beside correlation."
    )


def test_each_bin_reports_the_range_it_covers(con):
    first, last = binned(con, bins=5).rows[0], binned(con, bins=5).rows[-1]
    assert (first[1], first[2]) == ("1.0", "2.0")
    assert (last[1], last[2]) == ("9.0", "10.0")


def test_the_turns_are_counted_and_the_curve_is_not_named(con):
    text = " ".join(binned(con, bins=5).summary)
    assert "changes direction 1 time(s)" in text
    assert "naming one would be fitting one" in text
    for named in ("U-shaped", "quadratic", "parabola"):
        assert named not in text


def test_a_monotone_relationship_says_so_without_claiming_straightness(con):
    con.execute("UPDATE customers SET spend = age * 2 WHERE age IS NOT NULL")
    text = " ".join(binned(con, bins=5).summary)
    assert "moves in one direction across every bin, upward" in text
    assert "not the same as a straight one" in text


def test_a_tie_is_never_split_across_two_bins(con):
    """tier holds four values in ten rows; ntile over rows would split them."""
    out = binned(con, measure="tier", against="spend", bins=4)
    lows = [r[1] for r in out.rows]
    highs = [r[2] for r in out.rows]
    assert lows == highs, (
        f"{list(zip(lows, highs))}. A bin whose range overlaps the next one is "
        "a bin that split a tied value."
    )


def test_uneven_bins_are_reported_rather_than_hidden(con):
    text = " ".join(binned(con, measure="tier", against="spend",
                           bins=4).summary)
    assert "Bins hold between" in text
    assert "rather than a defect in the binning" in text


def test_asking_for_more_bins_than_values_reports_what_it_produced(con):
    out = binned(con, measure="tier", against="spend", bins=8)
    assert len(out.rows) == 4
    assert "8 bin(s) were asked for and 4 produced" in " ".join(out.summary)


def test_a_row_missing_either_value_is_in_no_bin(con):
    out = binned(con, bins=5)
    assert sum(int(r[3]) for r in out.rows) == 10
    assert "2 hold one or neither" in " ".join(out.summary)


def test_the_binning_rule_is_stated_in_the_summary(con):
    text = " ".join(binned(con, bins=5).summary)
    assert "ranges of age, not slices of rows" in text


def test_a_measure_against_itself_is_refused(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        binned(con, against="age")
    assert "its own range back as its own mean" in str(excinfo.value)


def test_too_few_or_too_many_bins_are_refused(con):
    for count in (1, 51):
        with pytest.raises(ParamsInvalid) as excinfo:
            binned(con, bins=count)
        assert "bins must be between 2 and 50" in str(excinfo.value)


def test_bins_that_are_not_a_number_are_refused(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        binned(con, bins="ten")
    assert "whole number" in str(excinfo.value)


def test_a_measure_nobody_declared_is_refused(con):
    with pytest.raises(Exception):
        binned(con, against="profit")


def test_the_method_note_is_the_first_summary_line(con):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "bivariate", measure="age", against="spend")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "bivariate")
    assert entry[1] == 4 and "bin" in entry[2]
