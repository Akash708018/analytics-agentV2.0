"""How big a difference is, and whether the module says the bands are a vocabulary.

Phase 10, Step 5. Run from the repo root:

    uv run pytest tests/test_effect_size.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import effect_size as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.effect_size import ETA_BANDS, G_BANDS, band  # noqa: E402
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
    if "second_dimension" not in params:
        params.setdefault("measure", "score")
    return run(con, g, scope_for(con, g), "effect_size", **params)


def said(out, fragment: str) -> bool:
    return any(fragment in s for s in out.summary)


def cell(out, group, column):
    row = next(r for r in out.rows if r[0] == group)
    return row[out.headers.index(column)]


def test_it_is_registered_in_tier_six():
    entry = next(c for c in catalogue() if c[0] == "effect_size")
    assert entry[1] == 6


# --- two groups -------------------------------------------------------------------------

def test_two_groups_get_hedges_g_not_cohens_d(con):
    """P10-D17: the correction is 0.9578 at ten rows per group and 0.9962 at a hundred, so it
    matters exactly where group breakdowns land."""
    out = out_for(con)
    assert said(out, "Hedges' g")
    assert said(out, "g rather than d")
    assert said(out, "0.9578")


def test_the_larger_group_is_named_as_the_larger(con):
    """The bug this step opened with. SQL returns the groups alphabetically, so naming them in
    that order printed 'a exceeds b' while b was in fact larger: a correct number inside a
    wrong sentence. arm b means 22.5 against a's 11.5."""
    out = out_for(con)
    assert said(out, "b exceeds a")
    assert not said(out, "a exceeds b")


def test_the_magnitude_is_banded_and_the_band_is_called_a_convention(con):
    out = out_for(con)
    assert said(out, "by Cohen's convention")
    assert said(out, "vocabulary, not a verdict")
    assert said(out, "proposed for psychology")


def test_an_effect_size_does_not_fall_as_rows_are_added(con):
    """The sentence that says why this analysis exists beside hypothesis_test."""
    out = out_for(con)
    assert said(out, "does not fall as rows are added")


def test_the_group_table_adds_back(con):
    out = out_for(con)
    assert cell(out, "a", "n") == "6"
    assert cell(out, "b", "n") == "6"
    assert cell(out, "(no arm)", "n") == "1"
    assert cell(out, "(no score)", "n") == "1"
    assert said(out, "add back to the 14 row(s) in scope")


# --- more than two ----------------------------------------------------------------------

def test_three_groups_get_eta_squared(con):
    out = out_for(con, dimension="region")
    assert said(out, "Eta squared")
    assert said(out, "of the variation in score lies between the groups of region")
    assert not said(out, "Hedges' g")


# --- two dimensions ---------------------------------------------------------------------

def test_two_dimensions_get_cramers_v(con):
    out = out_for(con, dimension="pair", second_dimension="channel")
    assert said(out, "Cramer's V")
    assert said(out, "rescaled to")
    assert said(out, "not which way and not why")


def test_v_stays_inside_zero_and_one(con):
    out = out_for(con, dimension="region", second_dimension="channel")
    line = next(s for s in out.summary if s.startswith("Cramer's V"))
    v = float(line.split("Cramer's V ")[1].split(" ")[0].replace(",", ""))
    assert 0.0 <= v <= 1.0


def test_a_single_row_table_is_refused_by_name(con):
    """P10-D32 again, in the other module: scipy returns a confident-looking number on zero
    degrees of freedom, so the engine checks the shape first."""
    out = out_for(con, dimension="constant", second_dimension="channel")
    assert said(out, "the table is 1x2")
    assert not said(out, "Cramer's V")


def test_a_dimension_is_not_sized_against_itself(con):
    with pytest.raises(ValueError, match="against itself"):
        out_for(con, dimension="region", second_dimension="region")


# --- where there is nothing to size -----------------------------------------------------

def test_a_one_row_group_has_no_scale_and_says_why(con):
    """P10-D30's asymmetry from the sizing side: a difference between a group of one and a
    group of twelve is real and has no standard deviation to be measured in."""
    out = out_for(con, dimension="lopsided")
    assert said(out, "holds one row")
    assert said(out, "no scale to be measured in")
    assert said(out, "compare them by rank")
    assert not said(out, "Hedges' g")


def test_one_group_is_counted_but_not_sized(con):
    out = out_for(con, dimension="constant")
    assert said(out, "has 1 group(s)")
    assert not said(out, "Eta squared")


# --- what it will not do ----------------------------------------------------------------

def test_the_association_table_names_both_dimensions_on_a_missing_row(con):
    """P9-O8: this row is missing one of two dimensions and no measure at all, so the label
    names the pair rather than a measure that is not what is absent.

    Until 21/09/2026 the branch was unasserted -- region and channel are non-null on all
    fourteen fixture rows, so `missing` was always zero and the label was never rendered by a
    test. Row 11 has no arm, so arm x channel excludes exactly one.
    """
    out = out_for(con, dimension="arm", second_dimension="channel")
    assert any(r[0] == "(no arm or channel)" for r in out.rows), (
        f"no row labelled for the missing pair; rows are {[r[0] for r in out.rows]}"
    )
    assert said(out, "(no arm or channel)")


def test_a_measure_and_a_second_dimension_are_not_both_allowed(con):
    with pytest.raises(ValueError, match="exactly one of measure, second_dimension"):
        out_for(con, measure="score", second_dimension="channel")


def test_neither_is_not_allowed_either(con):
    g = gate()
    with pytest.raises(ValueError, match="exactly one of measure, second_dimension"):
        run(con, g, scope_for(con, g), "effect_size", dimension="arm")


def test_an_unknown_parameter_is_named(con):
    with pytest.raises(TypeError, match="alpha"):
        out_for(con, alpha=0.05)


# --- the bands themselves ---------------------------------------------------------------

def test_the_bands_are_thresholds_on_magnitude_not_on_sign():
    """A large negative effect is large. Nothing in the vocabulary is directional."""
    assert band(-0.9, G_BANDS) == "large"
    assert band(0.9, G_BANDS) == "large"
    assert band(0.0, G_BANDS) == "negligible"
    assert band(0.3, G_BANDS) == "small"
    assert band(0.07, ETA_BANDS) == "medium"
