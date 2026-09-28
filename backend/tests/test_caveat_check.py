"""contract/caveat_check.py: a caveat's count, checked against the table (recheck, 25/09/2026).

The retail fixture's contract said 'unknown' customer_age in 3,470 rows and blank customer_state in
3,470 rows; the table held 3,471 and 3,473, and the answer repeated 3,470 as a finding. Run:

    uv run pytest tests/test_caveat_check.py -q
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine.contract import caveat_check as cc


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
      ('a', '30', 'Assam', 2), ('b', 'unknown', NULL, -1), ('c', 'unknown', '  ', 3),
      ('d', '41', 'Goa', -1), ('d', '41', 'Goa', -1), ('e', 'unknown', NULL, 1)
    ) v(id, age, state, units)""")
    yield c
    c.close()


def check(con, caveat):
    return cc.check_caveats(con, "t", [caveat])[0]


def test_a_quoted_value_count_that_is_off_by_one_differs(con):
    k = check(con, "age has 'unknown' in 2 rows")
    assert (k.status, k.claimed, k.measured) == ("differs", 2, 3)
    assert "the table holds 3" in k.to_text() and "'unknown'" in k.to_text()


def test_blank_counts_nulls_and_whitespace(con):
    assert check(con, "state blank in 3 rows").status == "agrees"
    k = check(con, "state blank in 2 rows")
    assert (k.status, k.measured) == ("differs", 3)


def test_duplicates_count_extra_copies(con):
    assert check(con, "1 exact duplicate rows; remove before summing").status == "agrees"


def test_a_comparison_is_counted_and_says_what_it_counted(con):
    k = check(con, "3 lines have units = -1 and negative revenue/cost")
    assert (k.status, k.what) == ("agrees", "rows where units = -1")


def test_what_cannot_be_counted_is_never_guessed(con):
    for caveat in ("25 lines have units 5–20× list price",       # no predicate it parses
                   "age and state are blank together in 2 rows",  # two columns
                   "line_revenue excludes shipping",              # no count
                   "units has '-' in 5 rows"):                    # token gone at load
        assert check(con, caveat).status == "unchecked", caveat


def test_notes_put_differences_first_and_fold_the_unchecked(con):
    lines = cc.notes(cc.check_caveats(con, "t", [
        "line_revenue excludes shipping", "state blank in 3 rows", "age has 'unknown' in 9 rows"]))
    assert lines[0].startswith("CAVEAT DIFFERS") and lines[1].startswith("Caveat checked")
    assert lines[2].startswith("1 caveat(s) are not a count")


def test_a_proposal_carries_the_checks(con):
    from backend.engine.contract.propose import propose_contract
    p = propose_contract(con, "t", caveats=["age has 'unknown' in 2 rows"])
    assert any(n.startswith("CAVEAT DIFFERS") for n in p.notes)
