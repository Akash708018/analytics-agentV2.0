"""What DuckDB 1.5.5 actually does when an analysis asks about time.

Phase 9, Step 1. Run from the repo root:

    uv run pytest tests/test_duckdb_temporal_facts.py -q

Nothing here imports backend.engine. These are facts about the engine, pinned
before any rule is written against them, the way Step 1 of Phase 7 pinned the
validation facts and Step 1 of Phase 6 pinned the cleaning facts. Every one of
them was measured; none was recalled.

The through-line is one sentence: **absence cannot be selected from the data --
a period with no rows has no row of its own to return.** Every Tier 3 analysis
therefore reads its periods off a generated calendar series and LEFT JOINs the
table onto it, rather than grouping the table and reporting what comes back.

The second sentence, which the series facts below are all about: the series is
generated from bounds already truncated to the grain. A series started at an
observed minimum walks days that no date_trunc output will ever join to.
"""

# ruff: noqa: DTZ001  -- the naive datetimes below are the subject, not an oversight

from __future__ import annotations

import datetime as dt

import duckdb
import pytest


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


# --------------------------------------------------------------------------
# the grain
# --------------------------------------------------------------------------


def test_date_trunc_widens_a_date_to_a_timestamp(con):
    got = con.execute(
        "SELECT typeof(date_trunc('month', DATE '2017-03-14'))"
    ).fetchone()[0]
    assert got == "TIMESTAMP", (
        f"date_trunc over a DATE now returns {got}. The period column was TIMESTAMP whatever the "
        "source column was, which is why periods are formatted by grain rather than printed raw."
    )


def test_date_trunc_over_a_timestamp_stays_a_timestamp(con):
    got = con.execute(
        "SELECT typeof(date_trunc('month', TIMESTAMP '2017-03-14 09:30:00'))"
    ).fetchone()[0]
    assert got == "TIMESTAMP", f"date_trunc over a TIMESTAMP now returns {got}."


def test_date_trunc_refuses_a_varchar(con):
    with pytest.raises(duckdb.BinderException) as excinfo:
        con.execute("SELECT date_trunc('month', '2017-03-14')")
    assert "date_trunc" in str(excinfo.value), (
        "date_trunc over a VARCHAR no longer raises a binder error. A text date reaching a "
        "temporal analysis is refused by the contract at confirm time; if the engine starts "
        "casting implicitly, that refusal is the only thing standing between a lexical sort and a"
        " trend."
    )


def test_the_truncated_value_is_the_first_instant_of_the_period(con):
    got = con.execute(
        "SELECT date_trunc('month', TIMESTAMP '2017-03-14 09:30:00')"
    ).fetchone()[0]
    assert got == dt.datetime(2017, 3, 1, 0, 0), f"month truncation now lands on {got}."


# --------------------------------------------------------------------------
# the series
# --------------------------------------------------------------------------


def test_generate_series_includes_the_upper_bound(con):
    n, lo, hi, typ = con.execute(
        "SELECT count(*), min(g), max(g), typeof(min(g)) FROM generate_series(DATE '2016-09-01', "
        "DATE '2016-12-01', INTERVAL 1 MONTH) s(g)"
    ).fetchone()
    expected = (4, dt.datetime(2016, 9, 1), dt.datetime(2016, 12, 1))
    assert (n, lo, hi) == expected, (
        f"generate_series returned {n} periods ending {hi}. It was inclusive of the upper bound; "
        "the last period of a window is the one a reader looks at first."
    )
    assert typ == "TIMESTAMP", f"generate_series over DATE bounds now yields {typ}."


def test_range_excludes_the_upper_bound(con):
    n, hi = con.execute(
        "SELECT count(*), max(g) FROM range(DATE '2016-09-01', DATE '2016-12-01', INTERVAL 1 "
        "MONTH) s(g)"
    ).fetchone()
    assert (n, hi) == (3, dt.datetime(2016, 11, 1)), (
        f"range returned {n} periods ending {hi}. range and generate_series differed by the last "
        "period, which is why the calendar is built with generate_series."
    )


def test_a_series_from_the_31st_walks_clamped_days(con):
    got = con.execute(
        "SELECT list(g) FROM generate_series(DATE '2017-01-31', DATE '2017-05-31', INTERVAL 1 "
        "MONTH) s(g)"
    ).fetchone()[0]
    assert got == [
        dt.datetime(2017, 1, 31),
        dt.datetime(2017, 2, 28),
        dt.datetime(2017, 3, 28),
        dt.datetime(2017, 4, 28),
        dt.datetime(2017, 5, 28),
    ], (
        f"a monthly series started on the 31st now walks {got}. February's clamp used to stick for"
        " every later period, which is why bounds are truncated to the grain before the series is"
        " built and never taken raw from min()."
    )


def test_month_addition_is_not_associative(con):
    twice, once = con.execute(
        "SELECT DATE '2017-01-31' + INTERVAL 1 MONTH + INTERVAL 1 MONTH, DATE '2017-01-31' + "
        "INTERVAL 2 MONTH"
    ).fetchone()
    assert twice == dt.datetime(2017, 3, 28), f"stepwise month addition now gives {twice}."
    assert once == dt.datetime(2017, 3, 31), f"a single two-month add now gives {once}."
    assert twice != once, (
        "stepping a month at a time and adding two months at once now agree. They did not, which "
        "is the same clamp seen from the other side."
    )


def test_a_series_with_no_usable_bounds_is_empty_not_an_error(con):
    nulls, backwards, single = con.execute(
        "SELECT (SELECT count(*) FROM generate_series(CAST(NULL AS TIMESTAMP), CAST(NULL AS "
        "TIMESTAMP), INTERVAL 1 MONTH)), (SELECT count(*) FROM generate_series(TIMESTAMP "
        "'2017-05-01', TIMESTAMP '2017-01-01', INTERVAL 1 MONTH)), (SELECT count(*) FROM "
        "generate_series(TIMESTAMP '2017-05-01', TIMESTAMP '2017-05-01', INTERVAL 1 MONTH))"
    ).fetchone()
    assert (nulls, backwards, single) == (0, 0, 1), (
        f"empty/backwards/single-period bounds now give {nulls}/{backwards}/{single}. An all-NULL "
        "date column produced no periods rather than an error, so coverage has to say so itself "
        "rather than rely on the engine raising."
    )


# --------------------------------------------------------------------------
# week keys
# --------------------------------------------------------------------------


def test_the_week_starts_on_monday(con):
    start, name = con.execute(
        "SELECT date_trunc('week', DATE '2017-03-14'), dayname(date_trunc('week', DATE "
        "'2017-03-14'))"
    ).fetchone()
    assert (start, name) == (dt.datetime(2017, 3, 13), "Monday"), (
        f"date_trunc('week') now starts the week on {name}. A weekly figure means nothing without "
        "the day it starts on."
    )


def test_weekofyear_and_isoyear_disagree_across_new_year(con):
    rows = con.execute(
        "SELECT weekofyear(d), year(d), isoyear(d) FROM (SELECT unnest([DATE '2016-01-01', DATE "
        "'2018-12-31']) AS d)"
    ).fetchall()
    assert rows == [(53, 2016, 2015), (1, 2018, 2019)], (
        f"the year-boundary weeks now read {rows}. 2016-01-01 was week 53 of isoyear 2015 and "
        "2018-12-31 was week 1 of isoyear 2019, so a week keyed by (year, weekofyear) splits the "
        "straddling week in two. Weeks are keyed by date_trunc('week') and never by arithmetic on"
        " week numbers."
    )


# --------------------------------------------------------------------------
# absence -- the through-line
# --------------------------------------------------------------------------


@pytest.fixture()
def sparse(con):
    con.execute(
        "CREATE TABLE sparse AS SELECT * FROM (VALUES (TIMESTAMP '2016-09-04 10:00:00'), "
        "(TIMESTAMP '2016-10-02 11:00:00'), (TIMESTAMP '2016-12-05 09:00:00'), (TIMESTAMP "
        "'2017-01-09 09:00:00'), (CAST(NULL AS TIMESTAMP))) v(ordered_at)"
    )
    return con


def test_a_group_by_cannot_return_the_missing_month(sparse):
    rows = sparse.execute(
        "SELECT date_trunc('month', ordered_at), count(*) FROM sparse GROUP BY 1 ORDER BY 1"
    ).fetchall()
    months = [r[0] for r in rows if r[0] is not None]
    assert dt.datetime(2016, 11, 1) not in months, (
        "a GROUP BY now returns the month that has no rows. It did not: November was absent from "
        "the output entirely, not present as a zero."
    )
    assert len(months) == 4, f"the grouped output now holds {len(months)} dated periods."


def test_a_left_join_from_the_series_returns_it_as_zero(sparse):
    rows = sparse.execute(
        "WITH b AS (SELECT date_trunc('month', min(ordered_at)) lo, date_trunc('month', "
        "max(ordered_at)) hi FROM sparse), s AS (SELECT x.g AS period FROM b, "
        "generate_series(b.lo, b.hi, INTERVAL 1 MONTH) x(g)), d AS (SELECT date_trunc('month', "
        "ordered_at) m, count(*) n FROM sparse WHERE ordered_at IS NOT NULL GROUP BY 1) SELECT "
        "strftime(s.period, '%Y-%m'), coalesce(d.n, 0) FROM s LEFT JOIN d ON d.m = s.period ORDER"
        " BY 1"
    ).fetchall()
    assert rows == [
        ("2016-09", 1),
        ("2016-10", 1),
        ("2016-11", 0),
        ("2016-12", 1),
        ("2017-01", 1),
    ], (
        f"the series join now returns {rows}, so absence is no longer reportable this way."
    )


def test_an_undated_row_is_in_neither_present_nor_missing(sparse):
    undated, total = sparse.execute(
        "SELECT count(*) FILTER (WHERE ordered_at IS NULL), count(*) FROM sparse"
    ).fetchone()
    assert (undated, total) == (1, 5), (
        f"the fixture now holds {undated} undated of {total}. A NULL date is not a missing period "
        "-- the period is in the window and the row is in scope -- so the two numbers are "
        "reported separately and neither is dropped."
    )


@pytest.fixture()
def gappy(con):
    con.execute(
        "CREATE TABLE gappy AS SELECT * FROM (VALUES (TIMESTAMP '2016-09-04'), (TIMESTAMP "
        "'2016-10-02'), (TIMESTAMP '2017-02-05'), (TIMESTAMP '2017-03-01'), (TIMESTAMP "
        "'2017-07-01')) v(ordered_at)"
    )
    return con


def test_the_longest_run_of_missing_periods_is_countable(gappy):
    periods, present, missing, longest = gappy.execute(
        "WITH b AS (SELECT date_trunc('month', min(ordered_at)) lo, date_trunc('month', "
        "max(ordered_at)) hi FROM gappy), s AS (SELECT x.g AS period FROM b, "
        "generate_series(b.lo, b.hi, INTERVAL 1 MONTH) x(g)), d AS (SELECT date_trunc('month', "
        "ordered_at) m, count(*) n FROM gappy GROUP BY 1), j AS (SELECT s.period, coalesce(d.n, "
        "0) AS n FROM s LEFT JOIN d ON d.m = s.period), r AS (SELECT period, n, row_number() OVER"
        " (ORDER BY period) - row_number() OVER (PARTITION BY (n = 0) ORDER BY period) AS grp "
        "FROM j) SELECT count(*), count(*) FILTER (WHERE n > 0), count(*) FILTER (WHERE n = 0), "
        "(SELECT max(c) FROM (SELECT count(*) c FROM r WHERE n = 0 GROUP BY grp)) FROM j"
    ).fetchone()
    assert (periods, present, missing, longest) == (11, 5, 6, 3), (
        f"the gap arithmetic now reads {periods}/{present}/{missing}/{longest}. The row_number "
        "difference groups consecutive missing periods; six missing months in runs of 3, 2 and 1 "
        "is a different story from six scattered ones, and the longest run is what distinguishes "
        "a broken feed from a quiet season."
    )


# --------------------------------------------------------------------------
# text that looks like a date
# --------------------------------------------------------------------------


def test_try_cast_parses_iso_spellings_and_nothing_else(con):
    rows = con.execute(
        "SELECT s, TRY_CAST(s AS TIMESTAMP) IS NOT NULL FROM (SELECT unnest(['2017-03-14', "
        "'2017-03-14 09:30:00', '14/03/2017', '03/14/2017', '2017-13-01', '2017-02-30', "
        "'20170314']) AS s)"
    ).fetchall()
    assert rows == [
        ("2017-03-14", True),
        ("2017-03-14 09:30:00", True),
        ("14/03/2017", False),
        ("03/14/2017", False),
        ("2017-13-01", False),
        ("2017-02-30", False),
        ("20170314", False),
    ], (
        f"TRY_CAST now parses {rows}. Day-first and month-first spellings both failed rather than "
        "one of them silently winning, which is the only reason a text date can be refused "
        "instead of guessed at."
    )


# --------------------------------------------------------------------------
# the session zone
# --------------------------------------------------------------------------


def test_a_tz_aware_value_truncates_to_a_different_day(con):
    con.execute("SET TimeZone='UTC'")
    utc = con.execute(
        "SELECT CAST(date_trunc('day', TIMESTAMPTZ '2017-03-14 23:30:00+00') AS VARCHAR)"
    ).fetchone()[0]
    con.execute("SET TimeZone='Asia/Kolkata'")
    ist = con.execute(
        "SELECT CAST(date_trunc('day', TIMESTAMPTZ '2017-03-14 23:30:00+00') AS VARCHAR)"
    ).fetchone()[0]
    assert utc.startswith("2017-03-14"), f"the UTC bucket now reads {utc}."
    assert ist.startswith("2017-03-15"), (
        f"the Asia/Kolkata bucket now reads {ist}. One instant fell on two different days under "
        "two session zones, which is why a TIMESTAMPTZ column's result states the zone its "
        "buckets were cut in."
    )


def test_a_naive_timestamp_is_unmoved_by_the_session_zone(con):
    con.execute("SET TimeZone='Asia/Kolkata'")
    got = con.execute(
        "SELECT CAST(date_trunc('day', TIMESTAMP '2017-03-14 23:30:00') AS VARCHAR)"
    ).fetchone()[0]
    assert got == "2017-03-14 00:00:00", (
        f"a naive TIMESTAMP now buckets to {got} under a non-UTC session zone. It did not move, "
        "which is why the zone is stated only when the column is tz-aware -- every Olist "
        "timestamp column is 'without time zone'."
    )


def test_the_tz_aware_type_survives_truncation(con):
    con.execute("SET TimeZone='Asia/Kolkata'")
    got = con.execute(
        "SELECT typeof(date_trunc('day', TIMESTAMPTZ '2017-03-14 23:30:00+00'))"
    ).fetchone()[0]
    assert got == "TIMESTAMP WITH TIME ZONE", (
        f"truncating a TIMESTAMPTZ now returns {got}, so the period column's type no longer "
        "carries the fact that a zone was involved."
    )


# --------------------------------------------------------------------------
# what reaches a reader
# --------------------------------------------------------------------------


def test_a_day_grain_series_outruns_the_render_cap(con):
    n = con.execute(
        "SELECT count(*) FROM generate_series(TIMESTAMP '2016-09-01', TIMESTAMP '2018-10-01', "
        "INTERVAL 1 DAY)"
    ).fetchone()[0]
    assert n == 761, f"the two-year day series now holds {n} periods."
    assert n > 50, (
        "a day-grain calendar over a two-year window no longer outruns the 50-row render cap. It "
        "did, which is why a coverage answer is counts, a missing list and a longest run rather "
        "than the series itself."
    )


def test_strftime_labels_each_grain_unambiguously(con):
    year, month, day, quarter = con.execute(
        "SELECT strftime(TIMESTAMP '2017-03-14', '%Y'), strftime(TIMESTAMP '2017-03-14', "
        "'%Y-%m'), strftime(TIMESTAMP '2017-03-14', '%Y-%m-%d'), strftime(TIMESTAMP '2017-03-14',"
        " '%Y-Q') || quarter(TIMESTAMP '2017-03-14')"
    ).fetchone()
    expected = ("2017", "2017-03", "2017-03-14", "2017-Q1")
    assert (year, month, day, quarter) == expected, (
        f"the grain labels now read {year}/{month}/{day}/{quarter}. A month rendered as a raw "
        "truncated timestamp reads as a day, and all four spellings sort lexically in calendar "
        "order."
    )
