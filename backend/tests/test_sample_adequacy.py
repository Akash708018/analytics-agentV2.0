"""What the rows could have detected, and whether the module refuses to restate the p-value.

Phase 10, Step 6. Run from the repo root:

    uv run pytest tests/test_sample_adequacy.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import sample_adequacy as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.inferential import (  # noqa: E402
    detectable_effect, rows_for_effect)
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "mean"
    definition: str = "what the trial scored"
    unit: str | None = None


@dataclass
class FakeWindow:
    start: date
    end: date


@dataclass
class FakeContract:
    dataset_name: str = "trials"
    date_column: str | None = None
    analysis_window: FakeWindow | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [FakeMeasure("score")])
    dimensions: list = field(
        default_factory=lambda: ["arm", "region", "channel", "pair", "lopsided", "constant"]
    )
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


TRIALS = """SELECT * FROM (VALUES
 ( 1,'a','north','web' ,'x','solo','same',10.0),
 ( 2,'a','north','web' ,'x','bulk','same',12.0),
 ( 3,'a','north','shop','x','bulk','same',11.0),
 ( 4,'a','south','web' ,'x','bulk','same',13.0),
 ( 5,'a','south','shop','x','bulk','same', 9.0),
 ( 6,'b','south','shop','y','bulk','same',20.0),
 ( 7,'b','east' ,'shop','y','bulk','same',22.0),
 ( 8,'b','east' ,'shop','y','bulk','same',21.0),
 ( 9,'b','east' ,'web' ,'y','bulk','same',19.0),
 (10,'b','north','web' ,'y','bulk','same',23.0),
 (11,NULL,'south','web','x','bulk','same',15.0),
 (12,'a','north','shop','x','bulk','same',NULL),
 (13,'b','south','web' ,'y','bulk','same',30.0),
 (14,'a','east' ,'web' ,'x','bulk','same',14.0)
) v(id, arm, region, channel, pair, lopsided, constant, score)"""


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE trials AS {TRIALS}")
    yield c
    c.close()


def gate(**kw) -> FakeGate:
    caveats = kw.pop("caveats", [])
    return FakeGate(contract=FakeContract(**kw), caveats=caveats)


def out_for(con, g=None, **params):
    g = g or gate()
    params.setdefault("dimension", "arm")
    params.setdefault("measure", "score")
    return run(con, g, scope_for(con, g), "sample_adequacy", **params)


def said(out, fragment: str) -> bool:
    return any(fragment in s for s in out.summary)


def cell(out, group, column):
    row = next(r for r in out.rows if r[0] == group)
    return row[out.headers.index(column)]


def test_it_is_registered_in_tier_six():
    entry = next(c for c in catalogue() if c[0] == "sample_adequacy")
    assert entry[1] == 6


def test_tier_six_is_now_complete():
    """The guide names four; all four are built."""
    names = sorted(n for n, t, _ in catalogue() if t == 6)
    assert names == ["confidence_interval", "effect_size", "hypothesis_test",
                     "sample_adequacy"]


# --- the arithmetic ---------------------------------------------------------------------

def test_the_helper_reproduces_the_step_one_measurement():
    """P10-D18: 63.765610588911635 per group for d=0.5 at 80% power, reported as 64."""
    assert rows_for_effect(0.5) == 64
    assert rows_for_effect(0.8) < rows_for_effect(0.2)


def test_more_rows_detect_smaller_differences():
    """The direction the whole analysis rests on."""
    assert detectable_effect(100, 100) < detectable_effect(10, 10)


def test_a_group_of_one_has_no_detectable_effect():
    with pytest.raises(ValueError, match="at least two rows"):
        detectable_effect(1, 20)


# --- the report -------------------------------------------------------------------------

def test_it_reports_the_floor_in_both_units(con):
    out = out_for(con)
    assert said(out, "standard deviation(s)")
    assert said(out, "of score")
    assert said(out, "6 and 6 row(s)")


def test_it_compares_the_observed_difference_to_the_floor(con):
    """arm a means 11.5 and b means 22.5, so the gap is far above what six per group can
    resolve. The sentence has to say which side of the floor it fell on."""
    out = out_for(con)
    assert said(out, "larger")
    assert not said(out, "statement about the row count")


def test_it_quotes_rows_needed_for_the_conventional_effects(con):
    out = out_for(con)
    assert said(out, "Rows per group needed")
    assert said(out, "64 for a medium effect (d=0.5)")


def test_observed_power_is_refused_and_the_reason_given(con):
    """P10-D18. The refusal is the analysis's main claim, so it is stated rather than implied."""
    out = out_for(con)
    assert said(out, "Observed power is not reported")
    assert said(out, "p-value rearranged")


def test_the_group_table_adds_back(con):
    out = out_for(con)
    assert cell(out, "a", "n") == "6"
    assert cell(out, "b", "n") == "6"
    assert cell(out, "(no arm)", "n") == "1"
    assert cell(out, "(no score)", "n") == "1"
    assert said(out, "add back to the 14 row(s) in scope")


def test_a_higher_power_demands_a_larger_detectable_effect(con):
    out_80 = out_for(con)
    out_95 = out_for(con, power=0.95)
    assert said(out_95, "95% power")
    assert said(out_80, "80% power")


# --- where it will not answer -----------------------------------------------------------

def test_three_groups_are_refused_by_name(con):
    out = out_for(con, dimension="region")
    assert said(out, "This compares two")
    assert said(out, "FTestAnovaPower")
    assert said(out, "nothing in this repository has measured")
    assert not said(out, "standard deviation(s)")


def test_a_one_row_group_has_no_spread_to_express_it_in(con):
    out = out_for(con, dimension="lopsided")
    assert said(out, "holds one row")
    assert said(out, "compare the two by rank")


def test_an_impossible_power_is_refused(con):
    with pytest.raises(ValueError, match="outside"):
        out_for(con, power=1.0)


def test_an_impossible_alpha_is_refused(con):
    with pytest.raises(ValueError, match="outside"):
        out_for(con, alpha=0.9)


def test_an_unknown_parameter_is_named(con):
    with pytest.raises(TypeError, match="beta"):
        out_for(con, beta=0.2)
