"""What DuckDB 1.5.5 does when asked how many rows are exact copies of another.

Cleanup Step 8. Run from the repo root:

    uv run pytest tests/test_duplicate_facts.py -q -s

Nothing here imports backend.engine. A keyless contract has no key to verify, so the one
arithmetic question left about double-counting is whether whole rows repeat. These pin how that
count behaves before the gate depends on it.

Phase 4 measured count(DISTINCT (a, b)) NOT dropping a row whose b is NULL. This is the other
half: SELECT DISTINCT treats two NULLs as the same value, so two rows (1, NULL) are ONE distinct
row -- they are exact copies, and a count that let NULL break the tie would miss them.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXACT = "SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM t)) FROM t"


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def test_distinct_collapses_rows_that_match_through_a_null(con):
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1, NULL), (1, NULL), (2, 'x')) v(a, b)")
    assert con.execute("SELECT count(*) FROM (SELECT DISTINCT * FROM t)").fetchone()[0] == 2
    assert con.execute(EXACT).fetchone()[0] == 1


def test_a_repeated_key_whose_rows_differ_is_not_an_exact_duplicate(con):
    # broken_sales.csv: 186 rows, 181 distinct order_id, and not one row an exact copy -- the
    # "duplicate keys whose rows differ are not duplicate rows" of P7-D12.
    con.execute(f"CREATE TABLE t AS SELECT * FROM read_csv('{FIXTURES / 'broken_sales.csv'}')")
    assert con.execute("SELECT count(*), count(DISTINCT order_id) FROM t").fetchone() == (186, 181)
    assert con.execute(EXACT).fetchone()[0] == 0


def test_rows_copied_whole_are_counted_once_each(con):
    con.execute(f"CREATE TABLE t AS SELECT * FROM read_csv('{FIXTURES / 'clean_sales.csv'}')")
    con.execute("INSERT INTO t SELECT * FROM t ORDER BY order_id LIMIT 3")
    assert con.execute("SELECT count(*), count(DISTINCT order_id) FROM t").fetchone() == (503, 500)
    assert con.execute(EXACT).fetchone()[0] == 3


def test_the_cost_on_five_million_rows(con):
    """Printed, not asserted: the gate would pay this on every analysis of a keyless contract."""
    con.execute(
        "CREATE TABLE t AS SELECT i AS id, i % 1000 AS a, i % 7 AS b, (i * 13) % 97 AS c, "
        "'r' || (i % 5) AS d, i * 1.5 AS e, DATE '2020-01-01' + (i % 900)::INT AS f, "
        "(i % 3 = 0) AS g FROM range(5000000) r(i)"
    )
    started = time.perf_counter()
    assert con.execute(EXACT).fetchone()[0] == 0
    distinct_all = time.perf_counter() - started
    started = time.perf_counter()
    con.execute("SELECT a, sum(e) FROM t GROUP BY a").fetchall()
    grouped = time.perf_counter() - started
    print(f"\n5M x 8: exact-duplicate count {distinct_all:.3f}s, one GROUP BY {grouped:.3f}s")
