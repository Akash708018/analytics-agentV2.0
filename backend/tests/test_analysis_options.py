"""Cleanup Step 14: an event key, no cap where the answer is a few numbers, fences within groups,
and a chosen subset of a dimension's groups. From the retail fixture run (I2, D3, D4, G1, H1, H6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb
import pytest

from backend.engine.analysis.base import ParamsInvalid, scope_for
from backend.engine.analysis.registry import run


@dataclass
class M:
    name: str
    agg: str | None = "sum"
    definition: str = "d"
    unit: str | None = None


@dataclass
class C:
    dataset_name: str = "t"
    date_column: str | None = "ts"
    analysis_window: object = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [M("amount"), M("rating", agg="mean")])
    dimensions: list = field(default_factory=lambda: ["who", "order_id", "shop", "cat", "chan"])
    primary_key: list = field(default_factory=list)


@dataclass
class G:
    contract: C
    caveats: list = field(default_factory=list)


def go(con, kind, **params):
    g = G(C())
    return run(con, g, scope_for(con, g), kind, **params)


@pytest.fixture()
def con():
    c = duckdb.connect()
    # who a: one order of two lines. who b: two orders, a month apart. who c: one line.
    c.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
      ('a', 'o1', TIMESTAMP '2024-01-01 10:00', 's01', 'x', 'Store',  10.0, 5),
      ('a', 'o1', TIMESTAMP '2024-01-01 10:00', 's02', 'x', 'Store',  11.0, 5),
      ('b', 'o2', TIMESTAMP '2024-01-02 10:00', 's03', 'x', 'Online', 12.0, 3),
      ('b', 'o3', TIMESTAMP '2024-02-01 10:00', 's04', 'y', 'Online', 13.0, 4),
      ('c', 'o4', TIMESTAMP '2024-01-03 10:00', 's05', 'y', 'Market', 14.0, 2)
    ) v(who, order_id, ts, shop, cat, chan, amount, rating)""")
    yield c
    c.close()


# --- A. an event key ---------------------------------------------------------------------------

def test_without_an_event_key_a_two_line_order_counts_as_coming_back(con):
    assert "2 came back at least once" in " ".join(go(con, "repeat_behaviour", entity="who").summary)


def test_with_an_event_key_only_a_second_order_is_coming_back(con):
    text = " ".join(go(con, "repeat_behaviour", entity="who", event="order_id").summary)
    assert "1 came back at least once" in text and "4 distinct order_id" in text


def test_the_consecutive_gap_is_reported(con):
    text = " ".join(go(con, "repeat_behaviour", entity="who", event="order_id").summary)
    assert "median gap between consecutive order_id events is 30 day(s)" in text


# --- B. no cap where the answer is a few numbers ------------------------------------------------

@pytest.fixture()
def wide(con):
    con.execute("CREATE OR REPLACE TABLE t AS SELECT 'p' || i AS who, 'o' || i AS order_id, "
                "TIMESTAMP '2024-01-01' AS ts, 's' || lpad((i % 300)::VARCHAR, 3, '0') AS shop, "
                "'x' AS cat, 'Store' AS chan, (1 + i % 300)::DOUBLE AS amount, 1 AS rating "
                "FROM range(600) r(i)")
    return con


def test_pareto_ranks_beyond_the_table_cap(wide):
    out = go(wide, "pareto", dimension="shop", measure="amount")
    assert "of 300 group(s) reach 80%" in " ".join(out.summary)


def test_concentration_cuts_at_a_share_of_many_groups(wide):
    out = go(wide, "concentration", dimension="shop", measure="amount")
    assert any(r[0] == "top 1% (3)" for r in out.rows), out.rows
    assert "HHI 44.37" in " ".join(out.summary)


# --- C. fences within a group -------------------------------------------------------------------

@pytest.fixture()
def priced(con):
    # Two categories ~25x apart, and one planted price 20x its own category.
    con.execute("CREATE OR REPLACE TABLE t AS SELECT 'p' AS who, 'o' AS order_id, "
                "TIMESTAMP '2024-01-01' AS ts, 's' AS shop, "
                "CASE WHEN i < 300 THEN 'cheap' ELSE 'dear' END AS cat, 'Store' AS chan, "
                "CASE WHEN i = 7 THEN 2000.0 WHEN i < 300 THEN 90 + i % 20 "
                "ELSE 2400 + (i % 20) * 30 END AS amount, 1 AS rating FROM range(400) r(i)")
    return con


def test_a_fence_within_each_group_finds_the_planted_value(priced):
    out = go(priced, "outlier_detection", measure="amount", dimension="cat")
    cheap = next(r for r in out.rows if r[0] == "cheap")
    assert cheap[4] == "1", out.rows          # Tukey within cheap flags the 2000
    text = " ".join(out.summary)
    assert "within cat" in text and "across all groups at once" in text


# --- D. chosen groups ---------------------------------------------------------------------------

def test_two_of_three_groups_can_be_tested(con):
    out = go(con, "hypothesis_test", dimension="chan", measure="rating", groups=["Store", "Online"])
    text = " ".join(out.summary)
    assert "Welch" in text and "outside the groups Store, Online of chan" in text


def test_a_group_that_has_no_rows_is_refused_naming_those_that_do(con):
    with pytest.raises(ParamsInvalid, match="Market, Online, Store"):
        go(con, "hypothesis_test", dimension="chan", measure="rating", groups=["Stor"])


# --- a key that never spans two moments names events (Cleanup Step 16, 2.1) ----------------------
# The re-run's I1x: cohort_retention keyed on order_id reported "Repeat rate 39.965%: 59,948 of
# 150,000 came back" -- the lines of one order, counted as returns.

def test_a_cohort_key_that_never_spans_two_moments_is_refused(con):
    with pytest.raises(ValueError, match="one moment"):
        go(con, "cohort_retention", entity="order_id", period="month")


def test_two_lines_at_one_moment_are_not_coming_back(con):
    text = " ".join(go(con, "cohort_retention", entity="who", period="month").summary)
    assert "1 of 3 came back" in text, text


def test_repeat_behaviour_refuses_a_key_that_never_spans_two_moments(con):
    with pytest.raises(ValueError, match="one moment"):
        go(con, "repeat_behaviour", entity="order_id")


# --- members moved against each other, or did not (Cleanup Step 16, 2.2) -------------------------
# The re-run's E4 said "Gross movement exceeds the net change, so members moved against each other"
# on one run and not the next, with all five categories rising: a float sum compared exactly.

@pytest.fixture()
def floats(con):
    con.execute("CREATE OR REPLACE TABLE t AS SELECT * FROM (VALUES "
                "('p', 'o', TIMESTAMP '2024-01-10', 's', 'x', 'Store', 0.0::DOUBLE, 1), "
                "('p', 'o', TIMESTAMP '2024-01-10', 's', 'y', 'Store', 0.0::DOUBLE, 1), "
                "('p', 'o', TIMESTAMP '2024-01-10', 's', 'z', 'Store', 0.0::DOUBLE, 1), "
                "('p', 'o', TIMESTAMP '2024-02-10', 's', 'x', 'Store', 0.3::DOUBLE, 1), "
                "('p', 'o', TIMESTAMP '2024-02-10', 's', 'y', 'Store', 0.2::DOUBLE, 1), "
                "('p', 'o', TIMESTAMP '2024-02-10', 's', 'z', 'Store', 0.1::DOUBLE, 1)"
                ") v(who, order_id, ts, shop, cat, chan, amount, rating)")
    return con


def test_members_that_all_rose_did_not_move_against_each_other(floats):
    out = go(floats, "growth_decomposition", measure="amount", dimension="cat",
             period="2024-02", baseline="2024-01")
    assert "3 rose, 0 fell" in " ".join(out.summary)
    assert "moved against each other" not in " ".join(out.summary)
