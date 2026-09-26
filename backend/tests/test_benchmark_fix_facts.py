"""Ground facts for Phase 14 Step 12, pinned before any code depends on them.

1. A type reading counted over DISTINCT values weighted by their counts equals the same reading
   counted row by row -- the casts are a function of the value -- including NULL, blanks,
   missing tokens, padding and mixed types. It is what lets the profile read types once per
   value instead of once per row (N3).
2. A share test decided on a sample first ("more failures in the sample than the column may
   hold and still reach the threshold") never reverses the full count's decision. The failures
   in any subset are failures of the whole.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.profile import table_profile as tp  # noqa: E402

VALUES = ["12", " 7 ", "007", "4.5", "1e3", "2024-01-31", "2024-01-31 10:30:00",
          "2024-01-31 00:00:00", "true", "FALSE", "N/A", "", "   ", None, "abc", "O0000001",
          "12", "12", "N/A", None, "31/12/2024", "-3", "inf"]


def _table(con, values):
    con.execute("CREATE OR REPLACE TABLE t (c VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?)", [[v] for v in values])


def _listed():
    return ", ".join(tp._lit(v) for v in dict.fromkeys(
        v.strip().upper() for v in tp.MISSING_VALUES if v.strip()))


@pytest.mark.parametrize("seed", range(5))
def test_distinct_weighted_type_counts_equal_row_counts(seed):
    rng = random.Random(seed)
    con = duckdb.connect()
    _table(con, [rng.choice(VALUES) for _ in range(500)])
    exprs = tp._cast_exprs("c", _listed())
    by_row = con.execute(f"SELECT {', '.join(exprs)} FROM t").fetchone()
    weighted = [e.replace("count(*)", "sum(n)", 1) for e in exprs]
    by_value = con.execute(
        f"SELECT {', '.join(f'coalesce({w}, 0)' for w in weighted)} "
        f"FROM (SELECT c, count(*) AS n FROM t GROUP BY c)").fetchone()
    assert tuple(by_row) == tuple(int(v) for v in by_value)


def test_count_star_occurs_once_in_every_cast_expression():
    """The weighting replaces the first count(*): there must be exactly one to replace."""
    for e in tp._cast_exprs("c", _listed()):
        assert e.count("count(*)") == 1


@pytest.mark.parametrize("fail_share", [0.0, 0.05, 0.1, 0.11, 0.5, 0.97, 1.0])
def test_a_sample_disproof_never_reverses_the_full_decision(fail_share):
    con = duckdb.connect()
    rng = random.Random(int(fail_share * 100))
    n = 1000
    _table(con, [("x" if rng.random() < fail_share else "1") for _ in range(n)])
    passing = con.execute("SELECT count(*) FROM t WHERE c = '1'").fetchone()[0]
    full = passing / n >= 0.90
    max_fails = n - next(k for k in range(n + 1) if k / n >= 0.90)
    k = min(n, (max_fails + 1) * 3 // 2 + 1)
    sample_fails = con.execute(
        f"SELECT count(*) FROM (SELECT c FROM t WHERE c IS NOT NULL LIMIT {k}) "
        f"WHERE NOT coalesce(c = '1', false)").fetchone()[0]
    if sample_fails > max_fails:
        assert full is False
