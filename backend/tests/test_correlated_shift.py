"""Two series breaking together, and how often that happens for no reason.

Phase 9, Step 6c. Run from the repo root:

    uv run pytest tests/test_correlated_shift.py -q

The Done-When clause this closes is that `correlated_shift` fires on a
deliberately injected synthetic shift. It does -- and the number beside the
verdict is the point of the analysis: on this fixture two *unrelated* series
land within tolerance of each other 43% of the time, and once a single month
goes missing that rises to 60%.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import correlated_shift as _  # noqa: E402,F401
from backend.engine.analysis.base import ParamsInvalid, scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "a reading"
    unit: str | None = None


@dataclass
class FakeContract:
    dataset_name: str = "mandi"
    date_column: str | None = "ts"
    analysis_window: object | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [
        FakeMeasure("price"),
        FakeMeasure("volume"),
        FakeMeasure("calm", definition="a series with no break in it"),
        FakeMeasure("unit_price", agg="none", definition="price of one item"),
    ])
    dimensions: list = field(default_factory=lambda: ["site"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


def series(skip: tuple[int, ...] = (), volume_break: int = 6) -> str:
    """Twelve months. price steps after June; volume steps after volume_break.

    calm cycles through three values and never breaks. A month in `skip` is
    absent from the table entirely.
    """
    rows = ", ".join(
        f"(TIMESTAMP '2017-{m:02d}-05', {10.0 if m <= 6 else 50.0}, "
        f"{100.0 if m <= volume_break else 20.0}, {20.0 + (m % 3) * 2}, 1.0)"
        for m in range(1, 13) if m not in skip
    )
    return f"SELECT * FROM (VALUES {rows}) v(ts, price, volume, calm, unit_price)"


def con_for(**kwargs):
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE mandi AS {series(**kwargs)}")
    return c


def shifted(con, **params):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "price")
    params.setdefault("against", "volume")
    return run(con, gate, scope, "correlated_shift", **params)


def test_an_injected_shift_in_both_series_is_found():
    """The Phase 9 Done-When clause, in one assertion."""
    out = shifted(con_for())
    assert [r[1] for r in out.rows] == ["2017-06", "2017-06"]
    assert "Both break in the same place" in " ".join(out.summary)


def test_the_chance_of_coinciding_is_reported_beside_the_verdict():
    text = " ".join(shifted(con_for()).summary)
    assert "about 43% of the time" in text
    assert "7 admissible split(s) and a window of 3" in text


def test_two_series_breaking_apart_are_not_called_coincident():
    out = shifted(con_for(volume_break=9))
    text = " ".join(out.summary)
    assert "They do not break in the same place" in text
    assert "3 month(s) apart against a tolerance of 1" in text


def test_a_gap_narrows_admissibility_and_raises_the_chance_rate():
    """A missing month does not only block splits -- it cheapens the verdict."""
    out = shifted(con_for(skip=(7,)))
    assert [r[4] for r in out.rows] == ["5", "5"]
    assert "about 60% of the time" in " ".join(out.summary)


def test_the_absent_period_is_named_with_what_it_cost():
    text = " ".join(shifted(con_for(skip=(7,))).summary)
    assert "1 month(s) hold no pair" in text
    assert "2017-07" in text
    assert "admissible count is 5 and not 11" in text


def test_each_series_reports_its_own_separation():
    out = shifted(con_for())
    assert [r[3] for r in out.rows] == [
        "no within-segment variation", "no within-segment variation"]


def test_a_series_whose_break_is_arithmetic_is_named():
    out = shifted(con_for(), against="calm")
    assert out.rows[1][3] == "0.4x"
    assert "two pieces of arithmetic agreeing" in " ".join(out.summary)


def test_a_series_with_no_admissible_split_gets_no_verdict():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE mandi AS SELECT * FROM (VALUES "
        "(TIMESTAMP '2017-01-05', 1.0, 1.0, 1.0, 1.0),"
        "(TIMESTAMP '2017-02-05', 2.0, 2.0, 2.0, 1.0),"
        "(TIMESTAMP '2017-03-05', 3.0, 3.0, 3.0, 1.0),"
        "(TIMESTAMP '2017-04-05', 4.0, 4.0, 4.0, 1.0),"
        "(TIMESTAMP '2017-05-05', 5.0, 5.0, 5.0, 1.0)"
        ") v(ts, price, volume, calm, unit_price)")
    out = shifted(con)
    assert "have no admissible split" in " ".join(out.summary)
    assert "no verdict is offered" in " ".join(out.summary)


def test_nothing_says_one_series_moved_the_other():
    text = " ".join(shifted(con_for()).summary)
    assert "Nothing here says price moved volume" in text
    assert "a third thing moving both -- is the one most often true" in text


def test_a_measure_against_itself_is_refused():
    with pytest.raises(ParamsInvalid) as excinfo:
        shifted(con_for(), against="price")
    assert "coincide by construction" in str(excinfo.value)


def test_a_measure_with_no_value_per_period_is_refused():
    with pytest.raises(ValueError) as excinfo:
        shifted(con_for(), against="unit_price")
    assert "no level to shift" in str(excinfo.value)


def test_a_measure_nobody_declared_is_refused():
    with pytest.raises(Exception):
        shifted(con_for(), against="profit")


def test_the_method_note_is_the_first_summary_line():
    con = con_for()
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "correlated_shift", measure="price",
              against="volume")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence():
    entry = next(c for c in catalogue() if c[0] == "correlated_shift")
    assert entry[1] == 5 and "chance" in entry[2]
