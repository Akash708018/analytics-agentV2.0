"""
Unit tests for profile/column_profile.py.

Bare in-memory connections: this module reads and renders, and writes nothing at
all -- not even a result file, which is the one place its rules differ from
render.py's. There is a test for that.

The `unknown` assertions are the ones to read first. Step 2 left `unknown`,
`none` and `-` out of the missing-value vocabulary and promised in exchange that
they would surface in the column's frequency table. This is the module that has
to make good on it.
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine.contract import ContractRefused
from backend.engine.contract.refusals import Reason, reason_of
from backend.engine.profile.column_profile import (
    HISTOGRAM_BINS,
    MAX_TOP_VALUES,
    TOP_VALUES,
    profile_column,
    render_column,
)
from backend.engine.profile.table_profile import profile_table


@pytest.fixture
def con():
    c = duckdb.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def sales(con):
    """
    200 rows. region holds 'unknown' 5 times and 'N/A' 11 times -- one of those
    is in the published vocabulary and one is deliberately not.
    """
    con.execute(
        """
        CREATE TABLE sales AS SELECT
          'ORD-' || lpad(i::VARCHAR, 5, '0')                    AS order_id,
          CASE WHEN i % 40 = 0 THEN 'unknown'
               WHEN i % 17 = 0 THEN 'N/A'
               WHEN i % 3  = 0 THEN 'North' ELSE 'South' END    AS region,
          CASE WHEN i = 199 THEN 9999.0 ELSE ((i % 97) + 1) * 1.5 END AS revenue,
          DATE '2024-01-01' + ((i * 2)::INTEGER)                AS order_date
        FROM range(200) t(i)
        """
    )
    return con


# --------------------------------------------------------------------------
# picking a column
# --------------------------------------------------------------------------

def test_an_unknown_column_is_refused_and_names_the_real_ones(sales):
    with pytest.raises(ContractRefused) as exc:
        profile_column(sales, "sales", "regionn")
    text = str(exc.value)
    assert reason_of(text) is Reason.COLUMN_NOT_FOUND
    assert "region" in text and "revenue" in text
    assert "profile_dataset(" in text


def test_an_unloaded_dataset_is_refused(con):
    con.execute("CREATE TABLE other AS SELECT 1 AS a")
    with pytest.raises(ContractRefused):
        profile_column(con, "nope", "a")


def test_the_numbers_agree_with_the_table_profile(sales):
    """
    Recomputing the nulls here with slightly different SQL is how two views of
    one column start disagreeing in the same conversation.
    """
    table = profile_table(sales, "sales")
    detail = profile_column(sales, "sales", "region")
    assert detail.column.missing_count == table.column("region").missing_count
    assert detail.column.evidence.distinct == table.column("region").evidence.distinct


def test_a_precomputed_table_profile_is_reused(sales):
    table = profile_table(sales, "sales")
    detail = profile_column(sales, "sales", "region", table=table)
    assert detail.column is table.column("region")


def test_profiling_a_column_writes_nothing(sales):
    before = {r[0] for r in sales.execute("SHOW TABLES").fetchall()}
    profile_column(sales, "sales", "revenue")
    assert {r[0] for r in sales.execute("SHOW TABLES").fetchall()} == before


# --------------------------------------------------------------------------
# the frequency table -- where a judgement call surfaces
# --------------------------------------------------------------------------

def test_the_values_come_back_most_frequent_first(sales):
    top = profile_column(sales, "sales", "region").top
    assert [v.value for v in top[:2]] == ["South", "North"]
    assert top[0].count > top[1].count


def test_each_value_carries_its_share(sales):
    top = profile_column(sales, "sales", "region").top
    assert top[0].share == pytest.approx(top[0].count / 200)


def test_unknown_shows_up_in_the_frequency_table(sales):
    """
    The promise Step 2 made when it excluded 'unknown' from the vocabulary:
    the value is not counted as missing, and it is not hidden either.
    """
    detail = profile_column(sales, "sales", "region")
    assert detail.column.missing_token_count == 11  # the 'N/A's only
    assert "UNKNOWN" not in detail.column.missing_token_breakdown
    assert ("unknown", 5) in [(v.value, v.count) for v in detail.top]


def test_the_render_says_to_judge_that_list(sales):
    text = render_column(profile_column(sales, "sales", "region"))
    assert "unknown" in text
    assert "values that MEAN absent" in text
    assert "a decision for you rather than for a constant" in text


def test_the_judgement_note_is_only_for_text_columns(sales):
    text = render_column(profile_column(sales, "sales", "revenue"))
    assert "values that MEAN absent" not in text


def test_the_list_is_capped_and_says_how_much_it_hid(sales):
    detail = profile_column(sales, "sales", "revenue")
    assert len(detail.top) == TOP_VALUES
    assert detail.distinct_not_shown == detail.column.evidence.distinct - TOP_VALUES


def test_the_truncation_ends_in_a_call_not_a_dead_end(sales):
    """
    Step 1's rule, in a module that writes no file: a truncation with no way to
    see the rest is a dead end. A truncation with an executable next step is
    not, and does not need a path to avoid being one.
    """
    text = render_column(profile_column(sales, "sales", "revenue"))
    assert "further distinct value(s) are not shown" in text
    assert 'profile_column(dataset_name="sales", column="revenue", top_n=50)' in text


def test_a_wider_list_can_be_asked_for(sales):
    assert len(profile_column(sales, "sales", "revenue", top_n=50).top) == 50


def test_the_widening_is_bounded(sales):
    """Past a couple of hundred it stops being a frequency table and becomes an
    export, which is a different tool."""
    detail = profile_column(sales, "sales", "revenue", top_n=10_000)
    assert detail.top_n == MAX_TOP_VALUES


def test_a_unique_column_gets_no_frequency_table(sales):
    """Ten rows of count 1 is noise. What a unique column has to say is
    elsewhere."""
    text = render_column(profile_column(sales, "sales", "order_id"))
    assert "occurs exactly once" in text
    assert "Most frequent" not in text


def test_nulls_and_blanks_are_shown_as_themselves(con):
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES ('a'),(NULL),(NULL),('  ')) v(x)"
    )
    text = render_column(profile_column(con, "t", "x"))
    assert "(null)" in text
    assert "(blank, 2 char(s))" in text


def test_a_very_long_value_is_cut(con):
    con.execute(f"CREATE TABLE t AS SELECT '{'x' * 200}' AS a FROM range(3)")
    text = render_column(profile_column(con, "t", "a"))
    assert "..." in text
    assert "x" * 200 not in text


# --------------------------------------------------------------------------
# distribution
# --------------------------------------------------------------------------

def test_a_numeric_column_gets_a_histogram(sales):
    h = profile_column(sales, "sales", "revenue").histogram
    assert h is not None and h.suppressed is None
    assert len(h.bins) == HISTOGRAM_BINS


def test_the_bins_account_for_every_value(sales):
    h = profile_column(sales, "sales", "revenue").histogram
    assert sum(c for _, _, c in h.bins) == 200


def test_the_outlier_lands_in_the_top_bin(sales):
    h = profile_column(sales, "sales", "revenue").histogram
    assert h.bins[-1][2] == 1
    assert h.bins[0][2] == 199


def test_boundaries_are_readable_numbers_not_exponents(sales):
    """`.4g` renders 10000 as 1e+04, which is correct and unreadable in a table
    of prices."""
    text = render_column(profile_column(sales, "sales", "revenue"))
    assert "10,000" in text
    assert "e+04" not in text


def test_an_all_null_column_has_no_distribution(con):
    con.execute("CREATE TABLE t AS SELECT CAST(NULL AS DOUBLE) AS n FROM range(4)")
    h = profile_column(con, "t", "n").histogram
    assert h.suppressed is not None
    assert not h.bins


def test_a_constant_column_does_not_crash_the_binning(con):
    """equi_width_bins(5, 5, 10, true) returns a single bin rather than
    failing. Verified on 1.5.5 rather than assumed."""
    con.execute("CREATE TABLE t AS SELECT 5.0::DOUBLE AS n FROM range(20)")
    h = profile_column(con, "t", "n").histogram
    assert h.suppressed is None
    assert len(h.bins) == 1
    assert h.bins[0][2] == 20


def test_text_columns_get_no_histogram(sales):
    assert profile_column(sales, "sales", "region").histogram is None


# --------------------------------------------------------------------------
# string length
# --------------------------------------------------------------------------

def test_a_text_column_reports_its_lengths(sales):
    lengths = profile_column(sales, "sales", "region").lengths
    assert lengths.shortest == 3 and lengths.longest == 7


def test_fixed_width_is_called_out(sales):
    """A column where every value is the same length is a code, and a code
    that gets summed or averaged is a specific kind of wrong."""
    text = render_column(profile_column(sales, "sales", "order_id"))
    assert "every value is 9 characters" in text
    assert "usually means a code" in text


def test_numeric_columns_get_no_length_summary(sales):
    assert profile_column(sales, "sales", "revenue").lengths is None


# --------------------------------------------------------------------------
# calendar coverage -- the cheap ancestor of F10
# --------------------------------------------------------------------------

def test_a_date_column_reports_which_days_are_present(sales):
    c = profile_column(sales, "sales", "order_date").coverage
    assert c.first == "2024-01-01"
    assert c.days_present == 200
    assert c.days_in_span == 399
    assert c.days_missing == 199


def test_the_longest_unbroken_gap_is_reported(con):
    """Twenty missing days spread singly is a different problem from twenty in
    a row, and only one of them is a feed outage."""
    con.execute(
        """CREATE TABLE t AS SELECT
             CASE WHEN i < 10 THEN DATE '2024-01-01' + (i::INTEGER)
                  ELSE DATE '2024-03-01' + (i::INTEGER) END AS d
           FROM range(20) s(i)"""
    )
    c = profile_column(con, "t", "d").coverage
    assert c.longest_gap > 40
    assert c.days_missing >= c.longest_gap


def test_a_full_calendar_has_no_gaps(con):
    con.execute(
        "CREATE TABLE t AS SELECT DATE '2024-01-01' + (i::INTEGER) AS d "
        "FROM range(30) s(i)"
    )
    c = profile_column(con, "t", "d").coverage
    assert c.days_missing == 0
    assert c.coverage == pytest.approx(1.0)


def test_a_timestamp_column_is_bucketed_to_days(con):
    """
    '3,412 distinct timestamps' answers nothing about whether a week is
    missing.
    """
    con.execute(
        "CREATE TABLE t AS SELECT TIMESTAMP '2024-01-01 08:00:00' + "
        "INTERVAL (i * 6) HOUR AS d FROM range(40) s(i)"
    )
    c = profile_column(con, "t", "d").coverage
    assert c.days_present == 11
    assert c.days_missing == 0


def test_an_all_null_date_column_says_so(con):
    con.execute("CREATE TABLE t AS SELECT CAST(NULL AS DATE) AS d FROM range(5)")
    text = render_column(profile_column(con, "t", "d"))
    assert "No dates present" in text


def test_the_gap_is_not_explained_away(sales):
    """A missing weekend and a broken feed look identical from here."""
    text = render_column(profile_column(sales, "sales", "order_date"))
    assert "not something the data says" in text


def test_non_temporal_columns_get_no_calendar(sales):
    assert profile_column(sales, "sales", "region").coverage is None


# --------------------------------------------------------------------------
# what comes back
# --------------------------------------------------------------------------

def test_the_render_leads_with_the_columns_own_sentence(sales):
    text = render_column(profile_column(sales, "sales", "revenue"))
    assert text.startswith("sales.revenue")
    assert "Tukey IQR fences" in text


def test_the_render_carries_no_path_because_nothing_was_written(sales):
    """
    render.py always writes and this never does. The rules differ because the
    outputs differ: a table profile is unbounded in the dimension that matters
    and this one is bounded by construction.
    """
    text = render_column(profile_column(sales, "sales", "revenue"))
    assert "read_result_file(" not in text
    assert ".csv" not in text


def test_nothing_here_reads_as_a_refusal(sales):
    assert reason_of(render_column(profile_column(sales, "sales", "region"))) is None


def test_a_null_in_the_list_is_not_counted_as_a_distinct_value():
    """Retail rating (recheck, 25/09/2026): 'Most frequent value(s), 7 of 6 distinct'."""
    import duckdb as _duckdb
    from backend.engine.profile.column_profile import profile_column, render_column
    c = _duckdb.connect(":memory:")
    c.execute("CREATE TABLE r AS SELECT * FROM (VALUES ('4'),('4'),(NULL),(NULL),(NULL),('3'),"
              "('n/a')) t(rating)")
    text = render_column(profile_column(c, "r", "rating", top_n=2))
    assert "1 of 3 distinct and (null):" in text
    assert "2 further distinct value(s)" in text
