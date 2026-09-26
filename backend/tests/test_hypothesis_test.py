"""Whether groups differ by more than noise, and whether the answer says which test said so.

Phase 10, Step 3. Run from the repo root:

    uv run pytest tests/test_hypothesis_test.py -q

Every assertion traces to a decision measured in Steps 1 to 3, cited where it is not obvious.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import hypothesis_test as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.inferential import p_text  # noqa: E402
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


# Two arms of six, three regions, two channels, one column whose 'solo' value holds a single
# row, and one column with a single value. Row 11 has no arm; row 12 has no score. Fourteen
# rows, so every count below adds to 14.
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
    return run(con, g, scope_for(con, g), "hypothesis_test", **params)


def said(out, fragment: str) -> bool:
    return any(fragment in s for s in out.summary)


def cell(out, group, column):
    row = next(r for r in out.rows if r[0] == group)
    return row[out.headers.index(column)]


def test_it_is_registered_in_tier_six():
    entry = next(c for c in catalogue() if c[0] == "hypothesis_test")
    assert entry[1] == 6


# --- two groups -----------------------------------------------------------------------

def test_two_groups_get_welch_named_not_just_a_t_test(con):
    """P10-D1: on one measured shape the pooled and unequal-variance forms disagree across the
    threshold, 0.301 against 0.046. "t-test" does not say which ran, so the name does."""
    out = out_for(con)
    assert said(out, "Welch's unequal-variance t-test (two-sided)")
    assert said(out, "not assumed to share a variance")


def test_the_reported_df_is_fractional(con):
    """P10-D2. Six and six with unequal spread gives about 7.15, not 10."""
    line = next(s for s in out_for(con).summary if s.startswith("Test:"))
    df = line.split("df ")[1].split(",")[0]
    assert "." in df
    assert 7.0 < float(df) < 7.3


def test_the_groups_are_the_scopes_rows_and_they_add_back(con):
    """P10-D6: a test that drops nulls has fewer rows than its scope by construction, so the
    dropped rows are cells in the table. Six per arm, one with no arm, one with no score."""
    out = out_for(con)
    assert cell(out, "a", "n") == "6"
    assert cell(out, "b", "n") == "6"
    assert cell(out, "(no arm)", "n") == "1"
    assert cell(out, "(no score)", "n") == "1"
    assert said(out, "add back to the 14 in scope")


def test_an_effect_size_accompanies_the_p_value(con):
    """P10-D17: Hedges' g always, because a p-value says a difference is unlikely to be noise
    and says nothing about its size."""
    out = out_for(con)
    assert said(out, "Hedges' g")
    assert said(out, "says how big it is")


def test_the_interpretation_is_a_sentence_not_a_verdict(con):
    out = out_for(con)
    assert said(out, "differs between the groups of arm")
    assert said(out, "not a cause")


def test_the_assumption_line_reports_rather_than_judges(con):
    """P10-D14: shapiro gives 0.445 at n=50 and 1.89e-20 at n=2000 on the same shape, so a
    pass/fail on normality is a statement about how many rows there are."""
    out = out_for(con)
    assert said(out, "Assumption reported, not judged")
    assert said(out, "smallest group 6 row(s)")
    assert not said(out, "normally distributed")


# --- three or more groups -------------------------------------------------------------

def test_three_groups_get_anova_with_both_degrees_of_freedom(con):
    """P10-D26: the closed form from (n, mean, var_samp), measured against f_oneway to 0.0.
    Three regions over thirteen scored rows is df 2, 10."""
    out = out_for(con, dimension="region")
    assert said(out, "one-way ANOVA (F test)")
    assert said(out, "df 2, 10")
    assert said(out, "rather than 3 pairwise tests")
    assert said(out, "eta squared")


# --- two dimensions -------------------------------------------------------------------

def test_a_three_by_two_table_gets_chi_square_without_yates(con):
    """P10-D9: the correction applies only to a 2x2, and saying so is cheaper than leaving a
    reader to wonder."""
    out = out_for(con, dimension="region", second_dimension="channel")
    assert said(out, "chi-square test of independence")
    assert said(out, "No continuity correction")
    assert said(out, "3x2")


def test_a_two_by_two_table_states_yates(con):
    """P10-D9 measured it moving a p-value from 0.00918 to 0.01904 -- across 0.01."""
    out = out_for(con, dimension="pair", second_dimension="channel")
    assert said(out, "Yates' continuity correction is applied")


def test_a_single_row_table_is_refused_by_name(con):
    """P10-D32: chi2_contingency returns chi2 0.0, dof 0 and p 1.0 here. No error, no nan, and
    a reader sees "no association" where there is nothing to compare. scipy will not refuse
    it, so this does."""
    out = out_for(con, dimension="constant", second_dimension="channel")
    assert said(out, "No test: the table is 1x2")
    assert said(out, "reads like a finding and is not one")
    assert not said(out, "chi-square test of independence")


def test_the_expected_count_screen_is_ours(con):
    """P10-D10: scipy returns a p-value for a table whose every expected count is 2.5 and says
    nothing about it."""
    out = out_for(con, dimension="region", second_dimension="channel")
    assert said(out, "Smallest expected count")


def test_a_dimension_is_not_tested_against_itself(con):
    with pytest.raises(ValueError, match="against itself"):
        out_for(con, dimension="region", second_dimension="region")


# --- a group of one -------------------------------------------------------------------

def test_a_one_row_group_takes_the_rank_branch_and_says_so(con):
    """P10-D30: of the four branches only the two-group parametric test cannot proceed without
    a variance. That is selection on a fact, not on a threshold, so it is allowed to happen
    without being asked for -- and is named when it does."""
    out = out_for(con, dimension="lopsided")
    assert said(out, "Mann-Whitney U (two-sided, normal approximation, tie-corrected)")
    assert said(out, "Method chosen, not requested")
    assert said(out, "holds one row and therefore has no spread")
    assert cell(out, "solo", "n") == "1"
    assert cell(out, "solo", "stddev") is None


def test_the_same_case_refuses_by_name_when_parametric_is_demanded(con):
    with pytest.raises(ValueError, match="no variance and the test has no denominator"):
        out_for(con, dimension="lopsided", method="parametric")


def test_the_rank_test_can_be_asked_for_outright(con):
    """P10-D23 and D24: U from midrank sums with the tie correction and the continuity
    correction, matching mannwhitneyu to every printed digit."""
    out = out_for(con, method="rank")
    assert said(out, "Mann-Whitney U")
    assert said(out, "not a test of medians")
    assert not said(out, "Method chosen, not requested")


def test_the_rank_branch_handles_more_than_two_groups(con):
    """P10-O6 closed. It used to refuse here and name the missing test; H follows from the same
    midrank sums Mann-Whitney already uses, measured against scipy.stats.kruskal."""
    out = out_for(con, dimension="region", method="rank")
    assert said(out, "Kruskal-Wallis H (tie-corrected)")
    assert said(out, "df 2")
    assert said(out, "needs no variance")


# --- what it will not do --------------------------------------------------------------

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
    with pytest.raises(ValueError, match="exactly one of measure, second_dimension"):
        run(con, gate(), scope_for(con, gate()), "hypothesis_test", dimension="arm")


def test_an_unknown_method_is_named_in_the_refusal(con):
    with pytest.raises(ValueError, match="'bootstrap' is not one of"):
        out_for(con, method="bootstrap")


def test_an_unknown_parameter_is_named(con):
    with pytest.raises(TypeError, match="alpha"):
        out_for(con, alpha=0.01)


def test_one_group_is_counted_but_not_tested(con):
    """A dimension with a single value among the analysed rows. The counts are still true and
    are still shown; there is simply nothing to compare them with."""
    out = out_for(con, dimension="constant")
    assert said(out, "has 1 group(s) among the analysed rows")
    assert said(out, "The counts above are still true")
    assert not said(out, "Test:")


# --- reporting ------------------------------------------------------------------------

def test_a_p_value_never_prints_as_zero():
    """P10-D13: measured at t = -386.7, scipy returns exactly 0.0, and a cell reading p = 0
    claims a certainty no test delivers."""
    assert p_text(0.0) == "< 1e-300"
    assert p_text(1e-320) == "< 1e-300"
    assert p_text(0.0012345678) == "0.00123457"
