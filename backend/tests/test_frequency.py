"""Counting rows into groups, and cutting the list.

Phase 8, Step 6. Run from the repo root:

    uv run pytest tests/test_frequency.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import frequency as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.registry import run  # noqa: E402


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
    dimensions: list = field(default_factory=lambda: ["region", "channel"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# North x3, South x2, NULL x2, East x1. Every region ties with another except
# East, so a cut at 2 lands in the middle of a tie.
SALES = """SELECT * FROM (VALUES
 (1,'North', TIMESTAMP '2024-02-01', 10.00::DECIMAL(18,2)),
 (2,'North', TIMESTAMP '2024-03-01', 20.00::DECIMAL(18,2)),
 (3,'North', TIMESTAMP '2024-04-01', -5.00::DECIMAL(18,2)),
 (4,'South', TIMESTAMP '2024-05-01', 20.00::DECIMAL(18,2)),
 (5,'South', TIMESTAMP '2024-06-01', 30.00::DECIMAL(18,2)),
 (6,NULL,    TIMESTAMP '2024-07-01', 40.00::DECIMAL(18,2)),
 (7,NULL,    TIMESTAMP '2024-08-01', 15.00::DECIMAL(18,2)),
 (8,'East',  TIMESTAMP '2024-09-01', 25.00::DECIMAL(18,2))
) v(id, region, ts, amount)"""


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE sales AS {SALES}")
    yield c
    c.close()


def gate(**kw) -> FakeGate:
    caveats = kw.pop("caveats", [])
    return FakeGate(contract=FakeContract(**kw), caveats=caveats)


def out_for(con, g, kind, **params):
    return run(con, g, scope_for(con, g), kind, **params)


# --- frequency


def test_every_value_is_counted_most_frequent_first(con):
    out = out_for(con, gate(), "frequency", column="region")
    assert [r[0] for r in out.rows] == ["North", "South", "(null)", "East"]
    assert [r[1] for r in out.rows] == ["3", "2", "2", "1"]
    assert out.rows[1][0] == "South", "NULLS LAST breaks the 2-2 tie, every run"


def test_null_is_a_group_with_a_name(con):
    """P8-D5. A blank cell reads as the empty string, which Step 1 measured is
    a different value."""
    out = out_for(con, gate(), "frequency", column="region")
    assert "(null)" in [r[0] for r in out.rows]
    assert any("not the empty string" in s for s in out.summary)


def test_the_share_is_of_the_analysed_rows(con):
    out = out_for(con, gate(), "frequency", column="region")
    assert out.rows[0][2] == "37.5%"
    assert any("share is of the 8 analysed row(s)".lower() in s.lower()
               for s in out.summary)


def test_a_cut_through_a_tie_says_so(con):
    """P8-D4: LIMIT is a claim about the output, "top 2" is a claim about the
    data, and they differ exactly when something ties at the cut."""
    out = out_for(con, gate(), "frequency", column="region", limit=2)
    assert len(out.rows) == 2
    assert any("share the value at the cut" in s for s in out.summary)


def test_a_cut_that_misses_every_tie_says_nothing(con):
    out = out_for(con, gate(), "frequency", column="region", limit=4)
    assert not any("at the cut" in s for s in out.summary)


def test_an_undeclared_column_is_refused_with_the_declared_list(con):
    out = gate()
    with pytest.raises(ValueError) as e:
        out_for(con, out, "frequency", column="id")
    assert "region, channel" in str(e.value)
    assert "profile_dataset" in str(e.value)


def test_an_excluded_column_is_refused_even_if_declared(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(excluded_columns=["region"]), "frequency", column="region")
    assert "never to read it" in str(e.value)


def test_an_empty_scope_counts_nothing_and_says_why(con):
    g = gate(analysis_window=FakeWindow(date(2030, 1, 1), date(2030, 12, 31)))
    out = out_for(con, g, "frequency", column="region")
    assert out.rows == []
    assert any("nothing to count" in s for s in out.summary)


# --- top_n


def test_groups_are_ranked_by_the_declared_aggregate(con):
    out = out_for(con, gate(), "top_n", dimension="region", measure="amount")
    # North 25, South 50, (null) 55, East 25 -- and the 25-25 tie is broken by
    # the group name, so East precedes North on every run.
    assert [r[0] for r in out.rows] == ["(null)", "South", "East", "North"]
    assert out.headers[1] == "amount (sum)"


def test_the_share_column_is_of_the_ranked_total(con):
    out = out_for(con, gate(), "top_n", dimension="region", measure="amount")
    assert out.rows[0][3] == "35.5%"  # 55 of 155


def test_a_negative_group_total_drops_the_share_column(con):
    """P8-D2: a share of a signed total is not a proportion."""
    con.execute("UPDATE sales SET amount = -100.0 WHERE id = 8")
    out = out_for(con, gate(), "top_n", dimension="region", measure="amount")
    assert all(r[3] == "" for r in out.rows)
    assert any("not a proportion" in s for s in out.summary)


def test_a_zero_total_drops_the_share_rather_than_dividing(con):
    """P8-D1: x/0 is inf in 1.5.5, not an error, and inf renders as a cell."""
    con.execute("UPDATE sales SET amount = 0.0")
    out = out_for(con, gate(), "top_n", dimension="region", measure="amount")
    assert all(r[3] == "" for r in out.rows)
    assert any("inf rather than an error" in s for s in out.summary)
    assert not any("inf" in str(r[1]) for r in out.rows)


def test_ranking_by_a_non_additive_measure_is_refused(con):
    g = gate(measures=[FakeMeasure("amount", agg="none")])
    with pytest.raises(ValueError) as e:
        out_for(con, g, "top_n", dimension="region", measure="amount")
    assert "non-additive" in str(e.value)


def test_ranking_by_a_measure_with_no_aggregate_is_refused(con):
    g = gate(measures=[FakeMeasure("amount", agg=None)])
    with pytest.raises(ValueError) as e:
        out_for(con, g, "top_n", dimension="region", measure="amount")
    assert "no declared aggregate" in str(e.value)


def test_an_undeclared_measure_is_refused_with_the_list(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), "top_n", dimension="region", measure="profit")
    assert "amount" in str(e.value)


def test_a_tie_at_the_cut_is_reported_for_a_ranking_too(con):
    """Flat amounts make the totals track the row counts: North 30, South 20,
    (null) 20, East 10 -- so a cut at 2 lands inside the 20-20 tie."""
    con.execute("UPDATE sales SET amount = 10.00")
    out = out_for(con, gate(), "top_n", dimension="region", measure="amount", n=2)
    assert any("share the value at the cut" in s for s in out.summary)


def test_the_method_note_and_caveats_travel(con):
    out = out_for(con, gate(caveats=["gained 30 rows since v1"]), "frequency",
                  column="region")
    assert out.summary[0].startswith("8 of 8 row(s) analysed")
    assert "gained 30 rows since v1" in out.summary


# Phase 8 Step 8a: which aggregates add across groups (P8-O16).


def test_a_count_ranking_keeps_its_share(con):
    g = gate(measures=[FakeMeasure("amount", agg="count")])
    out = out_for(con, g, "top_n", dimension="region", measure="amount")
    assert [r[3] for r in out.rows] == ["37.5%", "25.0%", "25.0%", "12.5%"]
    assert not any("does not add up" in s for s in out.summary)
    assert any("Share is of 8, the count of amount" in s for s in out.summary)


def test_a_count_share_names_its_denominator_when_a_null_shrinks_it(con):
    """AGG_SQL spells count as count(col), which skips a null measure."""
    con.execute("UPDATE sales SET amount = NULL WHERE id = 3")
    g = gate(measures=[FakeMeasure("amount", agg="count")])
    out = out_for(con, g, "top_n", dimension="region", measure="amount")
    assert out.rows[0] == ["North", "2", "3", "28.6%"]
    assert any("denominator is 7 and not the 8 analysed row(s)" in s
               for s in out.summary)


def test_count_distinct_does_not_add_and_the_fixture_proves_it(con):
    table_wide = con.execute("SELECT count(DISTINCT amount) FROM sales").fetchall()[0][0]
    summed = con.execute(
        "SELECT sum(d) FROM (SELECT count(DISTINCT amount) d FROM sales GROUP BY region)"
    ).fetchall()[0][0]
    assert (table_wide, summed) == (7, 8), "20.00 is in both North and South"
    g = gate(measures=[FakeMeasure("amount", agg="count_distinct")])
    out = out_for(con, g, "top_n", dimension="region", measure="amount")
    assert all(r[3] == "" for r in out.rows)
    assert any("count_distinct does not add up across groups" in s
               for s in out.summary)


def test_a_mean_ranking_still_has_no_share(con):
    g = gate(measures=[FakeMeasure("amount", agg="mean")])
    out = out_for(con, g, "top_n", dimension="region", measure="amount")
    assert all(r[3] == "" for r in out.rows)
    assert any("mean does not add up" in s for s in out.summary)


# --- one named period (Cleanup Step 9) --------------------------------------------------------
#
# "Which single orders drive the 2025-11 total?" had no answer: top_n ranks the contract's whole
# scope. One row a month here, February to September.

def test_top_n_in_one_month_ranks_only_that_months_rows(con):
    got = out_for(con, gate(), "top_n", dimension="region", measure="amount", period="2024-03")
    assert [r[0] for r in got.rows] == ["North"] and got.rows[0][1] == "20.00"
    note = got.summary[0]
    assert "1 of 8 row(s) analysed" in note and "outside the month 2024-03" in note


def test_a_quarter_is_a_period_too(con):
    got = out_for(con, gate(), "top_n", dimension="region", measure="amount",
                  period="2024-Q2", grain="quarter")
    assert [r[0] for r in got.rows] == ["South", "North"]
    assert got.rows[0][1] == "50.00"


def test_a_period_outside_the_calendar_is_refused_naming_the_range(con):
    from backend.engine.analysis.base import ParamsInvalid
    with pytest.raises(ParamsInvalid, match="2024-02 to 2024-09"):
        out_for(con, gate(), "top_n", dimension="region", measure="amount", period="2024-10")


def test_a_period_with_no_rows_narrows_to_none_and_says_so(con):
    con.execute("DELETE FROM sales WHERE id = 4")
    got = out_for(con, gate(), "top_n", dimension="region", measure="amount", period="2024-05")
    assert got.rows == []
    note = got.summary[0]
    assert "0 of 7 row(s) analysed" in note and "outside the month 2024-05" in note


def test_undated_rows_are_counted_out_of_a_period_not_lost(con):
    con.execute("INSERT INTO sales VALUES (9, 'West', NULL, 5.00)")
    note = out_for(con, gate(), "top_n", dimension="region", measure="amount",
                   period="2024-03").summary[0]
    assert "1 of 9 row(s) analysed" in note and "1 undated" in note


def test_grain_without_a_period_is_refused(con):
    from backend.engine.analysis.base import ParamsInvalid
    with pytest.raises(ParamsInvalid, match="period"):
        out_for(con, gate(), "top_n", dimension="region", measure="amount", grain="quarter")


def test_concentration_and_pareto_take_a_period(con):
    for kind in ("concentration", "pareto"):
        got = out_for(con, gate(), kind, dimension="region", measure="amount", period="2024-03")
        assert "outside the month 2024-03" in got.summary[0], kind


def test_top_n_states_what_the_rows_shown_hold_together(con):
    """Cleanup Step 10, live: the assistant said the top five orders held '~56%' of November;
    the five shares summed to 53.1%. A sum the reply states is a sum nobody has to do."""
    got = out_for(con, gate(), "top_n", dimension="region", measure="amount", n=2)
    assert "The 2 shown hold 67.7% of it together." in " ".join(got.summary), got.summary


def test_top_n_showing_every_group_says_nothing_about_together(con):
    got = out_for(con, gate(), "top_n", dimension="region", measure="amount", n=10)
    assert "together" not in " ".join(got.summary)
