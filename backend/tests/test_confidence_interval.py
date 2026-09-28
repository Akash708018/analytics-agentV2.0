"""How wide the uncertainty is, and whether the module says what kind of interval it drew.

Phase 10, Step 5. Run from the repo root:

    uv run pytest tests/test_confidence_interval.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import confidence_interval as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
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
    return run(con, g, scope_for(con, g), "confidence_interval", **params)


def said(out, fragment: str) -> bool:
    return any(fragment in s for s in out.summary)


def cell(out, group, column):
    row = next(r for r in out.rows if r[0] == group)
    return row[out.headers.index(column)]


def test_it_is_registered_in_tier_six():
    entry = next(c for c in catalogue() if c[0] == "confidence_interval")
    assert entry[1] == 6


# --- the interval around a mean ---------------------------------------------------------

def test_the_whole_scope_gets_one_interval(con):
    """No dimension: one row, named (all), over every usable value."""
    out = out_for(con, measure="score")
    assert cell(out, "(all)", "n") == "13"
    assert cell(out, "(all)", "low (95%)") is not None
    assert cell(out, "(all)", "high (95%)") is not None


def test_the_interval_is_the_t_form_and_says_so(con):
    """P10-D16: the t and normal widths differ by 15% at ten rows and 0.1% at a thousand, so
    the t form costs nothing where the difference is invisible."""
    out = out_for(con, measure="score")
    assert said(out, "Student's t on n - 1 degrees of freedom")
    assert said(out, "Not the normal interval")


def test_each_group_gets_its_own_interval(con):
    out = out_for(con, dimension="arm", measure="score")
    assert cell(out, "a", "n") == "6"
    assert cell(out, "b", "n") == "6"
    for group in ("a", "b"):
        assert cell(out, group, "half-width") is not None


def test_the_interval_brackets_the_mean(con):
    """Arithmetic, not vocabulary: low < mean < high, and the half-width is the distance."""
    out = out_for(con, dimension="arm", measure="score")
    low = float(cell(out, "a", "low (95%)").replace(",", ""))
    high = float(cell(out, "a", "high (95%)").replace(",", ""))
    mean = float(cell(out, "a", "mean").replace(",", ""))
    half = float(cell(out, "a", "half-width").replace(",", ""))
    assert low < mean < high
    assert abs((high - low) / 2 - half) < 0.01


def test_a_one_row_group_gets_a_mean_and_no_interval(con):
    """One row does produce a mean. It produces no evidence about what the next row would be,
    and a blank interval says that better than a zero-width one would."""
    out = out_for(con, dimension="lopsided", measure="score")
    assert cell(out, "solo", "n") == "1"
    assert cell(out, "solo", "mean") is not None
    assert cell(out, "solo", "low (95%)") is None
    assert said(out, "fewer than two rows")
    assert said(out, "no evidence about what the next row would be")


def test_the_excluded_rows_add_back(con):
    """P10-D6 and P10-D36: fourteen in scope, thirteen scored, one with no score, one with no
    arm. LostRows would fire if they did not."""
    out = out_for(con, dimension="arm", measure="score")
    assert cell(out, "(no arm)", "n") == "1"
    assert cell(out, "(no score)", "n") == "1"
    assert said(out, "add back to the 14 row(s) in scope")


def test_the_confidence_level_is_stated_in_the_headers(con):
    out = out_for(con, dimension="arm", measure="score", confidence=0.99)
    assert "low (99%)" in out.headers
    assert said(out, "99%")


def test_a_wider_level_gives_a_wider_interval(con):
    narrow = out_for(con, dimension="arm", measure="score", confidence=0.90)
    wide = out_for(con, dimension="arm", measure="score", confidence=0.99)
    n = float(cell(narrow, "a", "half-width").replace(",", ""))
    w = float(cell(wide, "a", "half-width").replace(",", ""))
    assert w > n


def test_what_an_interval_means_is_spelled_out(con):
    """The sentence readers get wrong, and the one a tool can afford a line on."""
    out = out_for(con, measure="score")
    assert said(out, "across repeated samples")
    assert said(out, "not a")


# --- the interval around a share --------------------------------------------------------

def test_a_dimension_alone_gives_wilson_intervals_on_shares(con):
    """P10-D15: Wald returns (0.0, 0.0) at zero successes in fifty. Wilson does not collapse."""
    out = out_for(con, dimension="arm")
    assert said(out, "Wilson")
    assert said(out, "Not Wald")
    assert cell(out, "a", "n") == "7"
    assert cell(out, "b", "n") == "6"
    assert cell(out, "(no arm)", "n") == "1"


def test_the_shares_are_against_the_whole_and_say_so(con):
    out = out_for(con, dimension="arm")
    assert said(out, "do not sum to 100%")
    assert said(out, "Overlapping")


def test_a_wilson_bound_stays_inside_zero_and_one(con):
    out = out_for(con, dimension="arm")
    for group in ("a", "b"):
        low = float(cell(out, group, "low (95%)").rstrip("%"))
        high = float(cell(out, group, "high (95%)").rstrip("%"))
        assert 0.0 <= low < high <= 100.0


# --- what it will not do ----------------------------------------------------------------

def test_neither_a_measure_nor_a_dimension_is_refused(con):
    with pytest.raises(ValueError, match="needs a measure"):
        out_for(con)


def test_an_impossible_confidence_level_is_refused(con):
    with pytest.raises(ValueError, match="outside"):
        out_for(con, measure="score", confidence=1.0)
    with pytest.raises(ValueError, match="outside"):
        out_for(con, measure="score", confidence=0.1)


def test_an_unknown_parameter_is_named(con):
    with pytest.raises(TypeError, match="alpha"):
        out_for(con, measure="score", alpha=0.05)
