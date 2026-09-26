"""What DuckDB's date arithmetic does, measured before a cohort grid is built on it.

Phase 10, Step 7. A cohort offset is a count of periods between a person's first activity and a
later one, and everything downstream is wrong if that count is off by one. Run with -s.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest


def say(label: str, value: object) -> None:
    print(f"    {label:<46} {value}")


@pytest.fixture()
def con():
    c = duckdb.connect()
    yield c
    c.close()


# --- what date_diff counts --------------------------------------------------------------

def test_date_diff_counts_boundaries_not_whole_months(con):
    """The distinction that decides every offset. One day apart across a month boundary is
    either 0 whole months or 1 boundary crossed, and a cohort grid needs to know which."""
    rows = con.execute(
        """
        SELECT date_diff('month', DATE '2017-01-31', DATE '2017-02-01'),
               date_diff('month', DATE '2017-01-01', DATE '2017-01-31'),
               date_diff('month', DATE '2017-01-15', DATE '2017-03-14'),
               date_diff('month', DATE '2017-03-14', DATE '2017-01-15')
        """
    ).fetchall()[0]
    say("31 Jan -> 1 Feb", rows[0])
    say("1 Jan -> 31 Jan", rows[1])
    say("15 Jan -> 14 Mar", rows[2])
    say("14 Mar -> 15 Jan (reversed)", rows[3])
    assert rows[3] == -rows[2]


def test_truncating_first_makes_the_offset_a_period_count(con):
    """Truncating both ends to the month makes date_diff count periods rather than partial
    ones. This is the spelling a cohort offset has to use."""
    rows = con.execute(
        """
        SELECT date_diff('month', date_trunc('month', DATE '2017-01-31'),
                                  date_trunc('month', DATE '2017-02-01')),
               date_diff('month', date_trunc('month', DATE '2017-01-01'),
                                  date_trunc('month', DATE '2017-01-31')),
               date_diff('month', date_trunc('month', DATE '2016-11-05'),
                                  date_trunc('month', DATE '2018-12-20'))
        """
    ).fetchall()[0]
    say("Jan -> Feb, truncated", rows[0])
    say("Jan -> Jan, truncated", rows[1])
    say("Nov 2016 -> Dec 2018, truncated", rows[2])
    assert rows[0] == 1
    assert rows[1] == 0
    assert rows[2] == 25


def test_date_trunc_returns_a_timestamp_and_needs_a_cast(con):
    """The cohort label is this value. Whether it arrives as a date or a string decides how it
    sorts and how it renders."""
    value, kind = con.execute(
        "SELECT date_trunc('month', DATE '2017-06-15'), "
        "       typeof(date_trunc('month', DATE '2017-06-15'))"
    ).fetchall()[0]
    say("value", value)
    say("python type", type(value).__name__)
    say("duckdb type", kind)
    cast, cast_kind = con.execute(
        "SELECT date_trunc('month', DATE '2017-06-15')::DATE, "
        "       typeof(date_trunc('month', DATE '2017-06-15')::DATE)"
    ).fetchall()[0]
    say("cast to DATE", cast)
    say("cast duckdb type", cast_kind)
    # Measured 19/09/2026: date_trunc returns TIMESTAMP even from a DATE argument, so a cohort
    # label carries a midnight unless it is cast. A monthly grid whose row headers read
    # "2017-01-01 00:00:00" reports a precision it has not got.
    assert kind == "TIMESTAMP"
    assert cast_kind == "DATE"
    assert cast == date(2017, 6, 1)


def test_date_trunc_on_a_timestamp(con):
    """Olist's order dates are timestamps, not dates. Truncating one has to land on the same
    month boundary or cohorts drift by the time of day."""
    value, kind = con.execute(
        "SELECT date_trunc('month', TIMESTAMP '2017-06-15 23:59:59'), "
        "       typeof(date_trunc('month', TIMESTAMP '2017-06-15 23:59:59'))"
    ).fetchall()[0]
    say("value", value)
    say("duckdb type", kind)
    say("equals the date form", value == con.execute(
        "SELECT date_trunc('month', DATE '2017-06-15')").fetchall()[0][0])


# --- the grid arithmetic ----------------------------------------------------------------

def test_a_cohort_grid_over_a_known_fixture(con):
    """Four people, hand-placed, so every cell is checkable by eye.

    p1 active Jan, Feb, Mar      cohort Jan, offsets 0 1 2
    p2 active Jan, Mar           cohort Jan, offsets 0 2
    p3 active Feb                cohort Feb, offset 0
    p4 active Feb, Mar           cohort Feb, offsets 0 1
    """
    con.execute("CREATE TABLE t (person VARCHAR, dt DATE)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [
        ("p1", date(2017, 1, 5)), ("p1", date(2017, 2, 9)), ("p1", date(2017, 3, 2)),
        ("p2", date(2017, 1, 20)), ("p2", date(2017, 3, 30)),
        ("p3", date(2017, 2, 14)),
        ("p4", date(2017, 2, 2)), ("p4", date(2017, 3, 19)),
    ])
    grid = con.execute(
        """
        WITH firsts AS (
          SELECT person, min(date_trunc('month', dt))::DATE AS cohort FROM t GROUP BY 1),
        activity AS (
          SELECT DISTINCT person, date_trunc('month', dt)::DATE AS period FROM t)
        SELECT f.cohort,
               date_diff('month', f.cohort, a.period) AS offset,
               count(DISTINCT a.person) AS people
        FROM firsts f JOIN activity a USING (person)
        GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).fetchall()
    for cohort, offset, people in grid:
        say(f"{cohort} offset {offset}", people)
    as_dict = {(c, o): n for c, o, n in grid}
    assert as_dict[(date(2017, 1, 1), 0)] == 2
    assert as_dict[(date(2017, 1, 1), 1)] == 1
    assert as_dict[(date(2017, 1, 1), 2)] == 2
    assert as_dict[(date(2017, 2, 1), 0)] == 2
    assert as_dict[(date(2017, 2, 1), 1)] == 1
    assert (date(2017, 2, 1), 2) not in as_dict


def test_offset_zero_holds_every_person_exactly_once(con):
    """The accounting a cohort grid can actually keep. It cannot add back to rows -- one person
    occupies several -- but every person appears in exactly one cohort at offset 0, and that
    sums to the number of people."""
    con.execute("CREATE TABLE t (person VARCHAR, dt DATE)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [
        ("p1", date(2017, 1, 5)), ("p1", date(2017, 2, 9)), ("p1", date(2017, 3, 2)),
        ("p2", date(2017, 1, 20)), ("p2", date(2017, 3, 30)),
        ("p3", date(2017, 2, 14)),
        ("p4", date(2017, 2, 2)), ("p4", date(2017, 3, 19)),
    ])
    zero, people = con.execute(
        """
        WITH firsts AS (
          SELECT person, min(date_trunc('month', dt))::DATE AS cohort FROM t GROUP BY 1)
        SELECT (SELECT count(*) FROM firsts), (SELECT count(DISTINCT person) FROM t)
        """
    ).fetchall()[0]
    say("cohort memberships", zero)
    say("distinct people", people)
    assert zero == people == 4


# --- the repeat distribution ------------------------------------------------------------

def test_the_repeat_distribution_and_the_gap(con):
    """What repeat_behaviour reports: how many people came back, how many times, and how long
    they took. The gap needs the second event, not the last."""
    con.execute("CREATE TABLE t (person VARCHAR, dt DATE)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [
        ("p1", date(2017, 1, 5)), ("p1", date(2017, 2, 9)), ("p1", date(2017, 6, 2)),
        ("p2", date(2017, 1, 20)), ("p2", date(2017, 3, 30)),
        ("p3", date(2017, 2, 14)),
        ("p4", date(2017, 2, 2)),
    ])
    counts = con.execute(
        "SELECT n, count(*) FROM (SELECT person, count(*) AS n FROM t GROUP BY 1) "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    gaps = con.execute(
        """
        WITH ordered AS (
          SELECT person, dt, row_number() OVER (PARTITION BY person ORDER BY dt) AS seq
          FROM t)
        SELECT a.person, date_diff('day', a.dt, b.dt) AS gap
        FROM ordered a JOIN ordered b ON a.person = b.person AND a.seq = 1 AND b.seq = 2
        ORDER BY 1
        """
    ).fetchall()
    say("events per person", counts)
    say("gap to second event", gaps)
    assert dict(counts) == {1: 2, 2: 1, 3: 1}
    assert len(gaps) == 2
    assert all(g > 0 for _, g in gaps)
