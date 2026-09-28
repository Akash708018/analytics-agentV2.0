"""Two open items, measured before either is fixed.

P10-O2: a DuckDB DOUBLE can hold NaN. P10-D5 measured that a NULL crossing into Python is loud
and a nan is silent, and base.number renders a nan into a cell as the text 'nan' with LostRows
none the wiser, because the row count is right. What was never measured is whether a NaN can get
*into* a column in the first place, what the aggregates do with it, and what screens it.

P10-O6: Kruskal-Wallis is the rank test for more than two groups, and hypothesis_test currently
refuses rather than approximating. H is arithmetic on the same midrank sums P10-D23 already
computes in SQL, so the question is only whether our arithmetic lands on scipy's.

Run with -s.
"""

from __future__ import annotations

import math

import duckdb
import pytest
import scipy.stats as st


def say(label: str, value: object) -> None:
    print(f"    {label:<44} {value}")


# --- P10-O2: can a NaN be in a column, and what happens then --------------------------

def test_a_double_column_accepts_nan_and_infinity():
    """If DuckDB refuses them at insert, P10-O2 closes here and no screen is needed."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t (x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)",
                    [(1.0,), (float("nan"),), (float("inf"),), (3.0,)])
    rows = [r[0] for r in con.execute("SELECT x FROM t ORDER BY 1").fetchall()]
    con.close()
    say("stored values", rows)
    say("count of nan", sum(1 for v in rows if isinstance(v, float) and math.isnan(v)))
    assert any(isinstance(v, float) and math.isnan(v) for v in rows)


def test_a_nan_is_not_a_null_to_sql():
    """The distinction the screen depends on: IS NOT NULL does not exclude a NaN."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t (x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)",
                    [(1.0,), (float("nan"),), (None,), (3.0,)])
    not_null, finite = con.execute(
        "SELECT count(*) FILTER (WHERE x IS NOT NULL), "
        "       count(*) FILTER (WHERE x IS NOT NULL AND NOT isnan(x) AND NOT isinf(x)) "
        "FROM t"
    ).fetchall()[0]
    con.close()
    say("IS NOT NULL", not_null)
    say("IS NOT NULL and finite", finite)
    assert not_null == 3
    assert finite == 2


def test_each_aggregate_meets_a_nan_differently():
    """Not propagate and not skip: var_samp raises. Measured one aggregate at a time, because
    asking for four in one SELECT lets the first failure hide the other three.

    This is what makes the screen load-bearing rather than tidy. Unscreened, a NaN in a measure
    aborts group_stats with a message that names neither the column nor the row -- so the screen
    is the difference between the module working and the module failing uselessly."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t (x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)",
                    [(1.0,), (2.0,), (float("nan"),), (4.0,)])
    for expr in ("count(x)", "avg(x)", "var_samp(x)", "stddev(x)", "skewness(x)",
                 "kurtosis(x)", "min(x)", "max(x)", "sum(x)"):
        try:
            value = con.execute(f"SELECT {expr} FROM t").fetchall()[0][0]
            say(expr, value)
        except Exception as exc:  # noqa: BLE001 - which one is the measurement
            say(expr, f"{type(exc).__name__}: {exc}")
    finite = con.execute(
        "SELECT avg(x), var_samp(x) FROM t WHERE NOT isnan(x) AND NOT isinf(x)"
    ).fetchall()[0]
    con.close()
    say("avg, var_samp once screened", finite)
    assert finite[0] is not None and finite[1] is not None


def test_isnan_survives_a_cast_from_other_numeric_types():
    """The screen has to work on DECIMAL and INTEGER columns too, and isnan refuses those
    directly. Casting to DOUBLE is the type-agnostic spelling -- if it raises here, the screen
    has to consult column_types instead."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t (a INTEGER, b DECIMAL(18,2), c DOUBLE)")
    con.execute("INSERT INTO t VALUES (1, 2.50, 3.0)")
    row = con.execute(
        "SELECT NOT isnan(CAST(a AS DOUBLE)), NOT isnan(CAST(b AS DOUBLE)), "
        "       NOT isnan(CAST(c AS DOUBLE)) FROM t"
    ).fetchall()[0]
    con.close()
    say("cast screen on INTEGER, DECIMAL, DOUBLE", row)
    assert row == (True, True, True)


# --- P10-O6: does H follow from the rank sums --------------------------------------------

def test_kruskal_follows_from_the_same_midrank_sums():
    """H = 12/(N(N+1)) * sum(R_i^2/n_i) - 3(N+1), divided by the tie correction. The rank sums
    are the ones P10-D23 already computes; the only new arithmetic is this line."""
    groups = {"a": [1.0, 3.0, 5.0, 7.0], "b": [2.0, 4.0, 6.0, 9.0, 11.0],
              "c": [8.0, 10.0, 12.0, 13.0]}
    con = duckdb.connect()
    con.execute("CREATE TABLE t (g VARCHAR, x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?)",
                    [(k, v) for k, vs in groups.items() for v in vs])
    rows = con.execute(
        """
        WITH ranked AS (
          SELECT g, rank() OVER (ORDER BY x) AS r_min,
                 count(*) OVER (PARTITION BY x) AS tie
          FROM t)
        SELECT g, sum(r_min + (tie - 1) / 2.0), count(*) FROM ranked GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    tie_term, total = con.execute(
        "SELECT coalesce(sum(c * c * c - c), 0), sum(c) FROM "
        "(SELECT count(*) AS c FROM t GROUP BY x)"
    ).fetchall()[0]
    con.close()

    n = total
    h = (12.0 / (n * (n + 1))) * sum(float(r) ** 2 / c for _, r, c in rows) - 3.0 * (n + 1)
    correction = 1.0 - float(tie_term) / (n ** 3 - n)
    h_corrected = h / correction if correction else h
    k = len(rows)
    ours = float(st.chi2.sf(h_corrected, k - 1))
    theirs = st.kruskal(*groups.values())

    say("rank sums from SQL", [(g, r, c) for g, r, c in rows])
    say("tie term", tie_term)
    say("H ours / scipy", f"{h_corrected} / {theirs.statistic}")
    say("p ours / scipy", f"{ours} / {theirs.pvalue}")
    assert abs(h_corrected - theirs.statistic) < 1e-9
    assert abs(ours - theirs.pvalue) < 1e-12


def test_kruskal_with_heavy_ties():
    """The tie correction carrying the result rather than nudging it, as P10-D23's tied sample
    did for U."""
    groups = {"a": [1.0] * 15 + [9.0] * 15, "b": [8.0] * 20 + [10.0] * 10,
              "c": [9.0] * 25 + [1.0] * 5}
    con = duckdb.connect()
    con.execute("CREATE TABLE t (g VARCHAR, x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?)",
                    [(k, v) for k, vs in groups.items() for v in vs])
    rows = con.execute(
        """
        WITH ranked AS (
          SELECT g, rank() OVER (ORDER BY x) AS r_min,
                 count(*) OVER (PARTITION BY x) AS tie
          FROM t)
        SELECT g, sum(r_min + (tie - 1) / 2.0), count(*) FROM ranked GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    tie_term, total = con.execute(
        "SELECT coalesce(sum(c * c * c - c), 0), sum(c) FROM "
        "(SELECT count(*) AS c FROM t GROUP BY x)"
    ).fetchall()[0]
    con.close()

    n = total
    h = (12.0 / (n * (n + 1))) * sum(float(r) ** 2 / c for _, r, c in rows) - 3.0 * (n + 1)
    h /= 1.0 - float(tie_term) / (n ** 3 - n)
    theirs = st.kruskal(*groups.values())
    say("tie term", tie_term)
    say("H ours / scipy", f"{h} / {theirs.statistic}")
    say("p ours / scipy", f"{float(st.chi2.sf(h, 2))} / {theirs.pvalue}")
    assert abs(h - theirs.statistic) < 1e-9


def test_kruskal_needs_no_variance():
    """The reason it belongs in the rank branch: a one-row group has no spread and H does not
    ask for one. P10-D30's asymmetry, extended past two groups."""
    res = st.kruskal([5.0], [1.0, 2.0, 3.0], [7.0, 8.0, 9.0])
    say("H with a one-row group", res.statistic)
    say("p with a one-row group", res.pvalue)
    assert not math.isnan(res.pvalue)
