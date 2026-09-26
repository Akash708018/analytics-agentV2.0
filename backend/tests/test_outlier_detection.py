"""Three methods, and the one that misses both outliers.

Phase 9, Step 6a. Run from the repo root:

    uv run pytest tests/test_outlier_detection.py -q

The fixture is the masking case: ten values from 1 to 10 plus 1000 and 1010.
Tukey's fence flags both extremes, the median absolute deviation flags both,
and the z-score flags neither -- the two extremes take the standard deviation
to 389.07, so three deviations reach past 1339 and catch nothing at all.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import outlier_detection as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "a number about a reading"
    unit: str | None = None


@dataclass
class FakeContract:
    dataset_name: str = "readings"
    date_column: str | None = "ts"
    analysis_window: object | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [
        FakeMeasure("value"),
        FakeMeasure("concentrated", definition="mostly one value"),
        FakeMeasure("flat", definition="one value throughout"),
    ])
    dimensions: list = field(default_factory=lambda: ["site"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# value: 1..10 with 1000 and 1010 appended -- two extremes, which is the
# fewest that masks a z-score. concentrated: eight identical values and two
# far ones, so its median absolute deviation is exactly zero. flat never
# varies. One row has no value at all.
READINGS = """SELECT * FROM (VALUES
  (1,  1.0, 5.0, 7.0), (2,  2.0, 5.0, 7.0), (3,  3.0, 5.0, 7.0),
  (4,  4.0, 5.0, 7.0), (5,  5.0, 5.0, 7.0), (6,  6.0, 5.0, 7.0),
  (7,  7.0, 5.0, 7.0), (8,  8.0, 5.0, 7.0), (9,  9.0, 99.0, 7.0),
  (10, 10.0, 100.0, 7.0), (11, 1000.0, 5.0, 7.0),
  (12, 1010.0, 5.0, 7.0), (13, NULL, 5.0, 7.0)
) v(id, value, concentrated, flat)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE readings AS {READINGS}")
    yield c
    c.close()


def flagged(con, **params):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "value")
    return run(con, gate, scope, "outlier_detection", **params)


def at(out, method):
    return next(row for row in out.rows if row[0] == method)


def test_all_three_methods_are_reported(con):
    assert [r[0] for r in flagged(con).rows] == [
        "Tukey's fence", "z-score", "median absolute deviation"]


def test_the_z_score_misses_what_the_other_two_catch(con):
    out = flagged(con)
    assert at(out, "Tukey's fence")[3] == "2"
    assert at(out, "median absolute deviation")[3] == "2"
    assert at(out, "z-score")[3] == "0", (
        "Two extremes take the standard deviation to 389.07. Three of those "
        "reach past 1339, so the fence built to catch them cannot."
    )


def test_masking_is_named_rather_than_left_to_be_noticed(con):
    text = " ".join(flagged(con).summary)
    assert "what masking looks like from the inside" in text
    assert "widens the fence meant to catch it" in text


def test_the_bounds_are_reported_not_just_the_counts(con):
    tukey = at(flagged(con), "Tukey's fence")
    assert (tukey[1], tukey[2]) == ("-4.55", "17.45"), tukey


def test_the_agreement_between_methods_is_counted(con):
    text = " ".join(flagged(con).summary)
    assert "2 value(s) are flagged by at least one method: 2 by 2 of the 3" in text, (
        "Both extremes are caught by Tukey and by MAD and missed by the "
        "z-score. A summary reporting only 'all three' and 'exactly one' "
        "would say 0 and 0 and never mention them."
    )


def test_a_zero_mad_leaves_the_robust_method_without_a_scale(con):
    out = flagged(con, measure="concentrated")
    mad = at(out, "median absolute deviation")
    assert (mad[1], mad[2], mad[3]) == ("", "", ""), mad
    assert "no scale to work with" in " ".join(out.summary)
    assert "arithmetic and not a finding" in " ".join(out.summary)


def test_a_column_with_no_spread_has_no_z_score(con):
    out = flagged(con, measure="flat")
    assert at(out, "z-score")[1] == ""
    assert "no denominator and no bounds" in " ".join(out.summary)


def test_a_row_without_a_value_is_in_no_method(con):
    text = " ".join(flagged(con).summary)
    assert "12 of 13 analysed row(s)" in text
    assert "1 do not and are in no method below" in text


def test_nothing_is_removed_or_recommended_for_removal(con):
    text = " ".join(flagged(con).summary)
    assert "Nothing above is removed" in text
    assert "a question about that row" in text


def test_too_few_values_reports_no_method_at_all(con):
    con.execute("DELETE FROM readings WHERE id > 6")
    out = flagged(con)
    assert out.rows == []
    assert "below 8" in " ".join(out.summary)
    assert "a distribution to sit in" in " ".join(out.summary)


def test_a_measure_nobody_declared_is_refused(con):
    with pytest.raises(Exception):
        flagged(con, measure="profit")


def test_the_method_note_is_the_first_summary_line(con):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "outlier_detection", measure="value")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "outlier_detection")
    assert entry[1] == 5 and "masking" in entry[2]
