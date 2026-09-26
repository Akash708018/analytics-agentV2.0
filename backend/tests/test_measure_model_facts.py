"""What DuckDB 1.5.5 does with the shapes the measure model is built from (Cleanup Step 15).

Nothing here imports backend.engine. Pinned before any rule depends on them, the way every
phase's ground-facts file is.
"""

from __future__ import annotations

import duckdb
import pytest


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1, true, 'a', 10.0, 4.0), "
              "(2, false, 'a', 10.0, 6.0), (3, true, 'b', NULL, 5.0), (4, NULL, 'b', NULL, 5.0)"
              ") v(id, b, g, x, y)")
    yield c
    c.close()


@pytest.mark.parametrize("fn", ["avg", "stddev_samp"])
def test_a_boolean_cannot_be_averaged_or_spread(con, fn):
    with pytest.raises(duckdb.BinderException):
        con.execute(f"SELECT {fn}(b) FROM t")


def test_a_boolean_sums_and_counts_and_averages_once_cast(con):
    assert con.execute("SELECT sum(b), count(b) FROM t").fetchone() == (2, 3)
    assert con.execute("SELECT avg(CAST(b AS INTEGER)) FROM t").fetchone()[0] == pytest.approx(2 / 3)


def test_replace_keeps_the_column_order(con):
    cols = [d[0] for d in con.execute("SELECT * REPLACE (CAST(b AS INTEGER) AS b) FROM t").description]
    assert cols == ["id", "b", "g", "x", "y"]


def test_a_struct_field_sums_inside_an_aggregate(con):
    """The ratio of sums: n and d summed separately, then divided -- not a mean of ratios."""
    got = con.execute(
        "WITH r AS (SELECT g, struct_pack(n := y * 100, d := id::DOUBLE) AS m FROM t) "
        "SELECT g, sum(m.n) / nullif(sum(m.d), 0) FROM r GROUP BY g ORDER BY g").fetchall()
    assert got == [("a", pytest.approx(1000 / 3)), ("b", pytest.approx(1000 / 7))]


def test_a_null_inside_a_struct_is_skipped_by_sum(con):
    got = con.execute("SELECT sum(m.n) FROM (SELECT struct_pack(n := x) AS m FROM t)").fetchone()[0]
    assert got == 20


def test_distinct_over_a_unit_keeps_one_row_per_unit_when_the_value_is_constant(con):
    assert con.execute("SELECT count(*) FROM (SELECT DISTINCT g, x FROM t)").fetchone()[0] == 2
    # ... and more than one when it varies inside the unit, which is how variation is detected.
    assert con.execute("SELECT count(*) FROM (SELECT DISTINCT g, y FROM t)").fetchone()[0] == 3
