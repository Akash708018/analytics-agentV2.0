"""What is actually in broken_sales.csv, counted against the file.

Phase 7, Step 3. Run from the repo root:

    uv run pytest tests/test_broken_sales_ground_truth.py -q

The Phase 7 Done-When is "a correct failure on a deliberately broken fixture",
and that sentence is only worth anything if the breaks are known independently
of the thing being tested. So these numbers are written out as literals here
rather than imported from `make_fixtures.BROKEN_BREAKS`. A test that reads its
expectations from the generator asserts that the generator is self-consistent,
which it always is, and nothing else.

Read with `read_csv_auto` rather than through `ingest.csv_loader`, on purpose:
this file is about the bytes on disk. Whether the loader reproduces them is a
different question, asked in Step 5 where the rules run against a loaded table.

Sibling of `tests/test_mixed_types_ground_truth.py`, which does the same job for
the coercion counts.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SALES = FIXTURES / "broken_sales.csv"
LOOKUP = FIXTURES / "region_lookup.csv"

WINDOW_START = "DATE '2024-01-01'"
WINDOW_END = "DATE '2024-12-31'"

# The declared vocabulary for the channel dimension. Repeated here rather than
# imported for the reason in the module docstring.
CHANNELS = ("Online", "Retail", "Wholesale")


@pytest.fixture()
def con():
    if not SALES.exists() or not LOOKUP.exists():
        pytest.skip(
            f"{SALES.name} / {LOOKUP.name} not generated. Run "
            f"`uv run python tests/fixtures/make_fixtures.py` first."
        )
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE sales AS SELECT * FROM read_csv_auto('{SALES}')")
    c.execute(f"CREATE TABLE lookup AS SELECT * FROM read_csv_auto('{LOOKUP}')")
    yield c
    c.close()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------


def test_the_fixture_is_the_size_the_generator_declares(con):
    """120 clean rows plus 66 injected, and the arithmetic is checkable by
    reading the BROKEN_BREAKS table in the generator rather than by trusting
    it."""
    assert one(con, "SELECT count(*) FROM sales") == 186


def test_the_date_column_loads_as_a_timestamp(con):
    """The whole reason the fixture writes times rather than dates.

    P7-D2's trap only exists on a TIMESTAMP column: `<= DATE '2024-12-31'`
    casts to midnight and drops the rest of that day. A DATE column cannot
    catch a regression in that bound, so this fixture makes sure there is one
    to catch.
    """
    types = dict(con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'sales' ORDER BY ordinal_position"
    ).fetchall())
    assert types["order_ts"] == "TIMESTAMP"
    assert types["units"] == "BIGINT"
    assert types["unit_price"] == "DOUBLE"


# --------------------------------------------------------------------------
# the key
# --------------------------------------------------------------------------


def test_the_key_has_two_nulls_and_three_repeats(con):
    assert one(con, "SELECT count(*) - count(order_id) FROM sales") == 2
    assert one(con, "SELECT count(DISTINCT order_id) FROM sales") == 181
    assert one(con, "SELECT count(order_id) FROM sales") == 184


def test_three_repeated_keys_are_six_rows(con):
    """Two different numbers, and a report that prints one of them as the
    other is the P7-D6 failure in a new place.

    Three order ids appear twice each. "3 keys repeat" and "6 rows are
    involved" are both true and answer different questions -- how many
    agreements are broken, and how much data is affected.
    """
    groups = one(con, """
        SELECT count(*) FROM (
            SELECT order_id FROM sales WHERE order_id IS NOT NULL
            GROUP BY order_id HAVING count(*) > 1)
    """)
    rows = one(con, """
        SELECT coalesce(sum(n), 0) FROM (
            SELECT count(*) AS n FROM sales WHERE order_id IS NOT NULL
            GROUP BY order_id HAVING count(*) > 1)
    """)
    assert (groups, rows) == (3, 6)


def test_the_repeated_keys_are_not_repeated_rows(con):
    """A duplicate key and a duplicate row are different faults.

    The injected repeats share an order_id and differ in every other column,
    so a grain failure cannot be mistaken for a copy-paste in the source file
    -- and `DISTINCT *` cannot find them.
    """
    assert one(con, "SELECT count(*) FROM (SELECT DISTINCT * FROM sales)") == 186


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------


def test_the_window_faults_are_four_five_and_six(con):
    before = one(con, f"SELECT count(*) FROM sales WHERE order_ts < {WINDOW_START}")
    after = one(con, f"SELECT count(*) FROM sales "
                     f"WHERE order_ts >= {WINDOW_END} + INTERVAL 1 DAY")
    missing = one(con, "SELECT count(*) - count(order_ts) FROM sales")
    assert (before, after, missing) == (4, 5, 6)


def test_the_four_numbers_sum_to_the_row_count(con):
    """P7-D4, on the fixture the rules will be measured against."""
    inside = one(con, f"SELECT count(*) FROM sales WHERE order_ts >= {WINDOW_START} "
                      f"AND order_ts < {WINDOW_END} + INTERVAL 1 DAY")
    before = one(con, f"SELECT count(*) FROM sales WHERE order_ts < {WINDOW_START}")
    after = one(con, f"SELECT count(*) FROM sales "
                     f"WHERE order_ts >= {WINDOW_END} + INTERVAL 1 DAY")
    missing = one(con, "SELECT count(*) - count(order_ts) FROM sales")
    assert inside == 171
    assert inside + before + after + missing == 186


def test_one_row_sits_in_the_last_second_of_the_window(con):
    """The regression detector for P7-D2.

    Written correctly the window holds 171 rows; written `<= end` it holds
    170. If a future edit reintroduces the inclusive-bound bug, this is the
    single row that notices.
    """
    correct = one(con, f"SELECT count(*) FROM sales WHERE order_ts >= {WINDOW_START} "
                       f"AND order_ts < {WINDOW_END} + INTERVAL 1 DAY")
    naive = one(con, f"SELECT count(*) FROM sales WHERE order_ts >= {WINDOW_START} "
                     f"AND order_ts <= {WINDOW_END}")
    assert (correct, naive) == (171, 170)


# --------------------------------------------------------------------------
# the reference
# --------------------------------------------------------------------------


def test_seven_rows_point_at_a_region_that_does_not_exist(con):
    orphan_rows = one(con, """
        SELECT count(*) FROM sales s WHERE s.region IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM lookup r WHERE r.region = s.region)
    """)
    orphan_values = one(con, """
        SELECT count(DISTINCT s.region) FROM sales s WHERE s.region IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM lookup r WHERE r.region = s.region)
    """)
    nulls = one(con, "SELECT count(*) - count(region) FROM sales")
    assert (orphan_rows, orphan_values, nulls) == (7, 3, 8)


def test_not_in_finds_none_of_them(con):
    """The regression detector for P7-D3.

    The lookup carries a blank row -- the trailing empty line every
    spreadsheet export eventually contributes to a dimension table -- so
    `NOT IN` returns UNKNOWN for every comparison and reports a clean pass on
    a table with seven orphans. If the rule is ever rewritten with `NOT IN`,
    the count drops from 7 to 0 and this says so.
    """
    assert one(con, "SELECT count(*) FROM sales "
                    "WHERE region NOT IN (SELECT region FROM lookup)") == 0


def test_the_lookup_holds_rows_nothing_points_at(con):
    """Central, which no sale uses, and the blank row. Neither is a fault --
    a dimension row with no facts is normal, and the check runs one way."""
    assert one(con, """
        SELECT count(*) FROM lookup r
        WHERE NOT EXISTS (SELECT 1 FROM sales s WHERE s.region = r.region)
    """) == 2


# --------------------------------------------------------------------------
# domain, range, and the measure
# --------------------------------------------------------------------------


def test_nine_rows_carry_a_channel_outside_the_declared_set(con):
    values = ", ".join(f"'{c}'" for c in CHANNELS)
    assert one(con, f"SELECT count(*) FROM sales "
                    f"WHERE channel NOT IN ({values})") == 9


def test_ten_rows_carry_a_negative_unit_count(con):
    assert one(con, "SELECT count(*) FROM sales WHERE units < 0") == 10


def test_eleven_rows_have_no_price_and_therefore_no_revenue(con):
    assert one(con, "SELECT count(*) - count(unit_price) FROM sales") == 11
    assert one(con, "SELECT count(*) - count(revenue) FROM sales") == 11


# --------------------------------------------------------------------------
# the design rule
# --------------------------------------------------------------------------


def test_no_row_carries_two_faults(con):
    """One fault per row, so a rule that double-counts is visible.

    If a single row were both null-keyed and out of window, a checker that
    reported "12 rows failed" could be right by accident while getting both
    numbers wrong. Every family here is disjoint, which means the totals add
    up and any rule that produces a different total is wrong rather than
    differently right.
    """
    faults = one(con, f"""
        SELECT count(*) FROM (
          SELECT
            (order_id IS NULL)::INT
          + (order_ts IS NULL)::INT
          + (order_ts < {WINDOW_START})::INT
          + (order_ts >= {WINDOW_END} + INTERVAL 1 DAY)::INT
          + (region IS NULL)::INT
          + (region IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM lookup r WHERE r.region = sales.region))::INT
          + (channel NOT IN ('Online','Retail','Wholesale'))::INT
          + (units < 0)::INT
          + (unit_price IS NULL)::INT AS n
          FROM sales)
        WHERE n > 1
    """)
    assert faults == 0
