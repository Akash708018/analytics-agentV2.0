"""What DuckDB 1.5.5 actually does when a validation predicate meets a NULL.

Phase 7, Step 1. Run from the repo root:

    uv run pytest tests/test_duckdb_validation_facts.py -q

Nothing here imports backend.engine. These are facts about the engine, pinned
before any rule is written against them, the way Step 1 of Phase 6 pinned the
cleaning facts. Every one of them was measured; none was recalled.

The through-line is one sentence: **a validation predicate does not see NULLs,
and a check that reports only what its predicate saw will under-report and call
it a pass.** Every rule in validate/ therefore reports four numbers whose sum is
the row count -- checked, passed, failed, and not-checked -- rather than a
verdict computed from one.
"""

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
# referential integrity
# --------------------------------------------------------------------------


@pytest.fixture()
def fk(con):
    con.execute(
        """
        CREATE TABLE parent AS SELECT * FROM (VALUES ('A'),('B'),(NULL)) t(pk);
        CREATE TABLE child  AS SELECT * FROM (VALUES ('A'),('B'),('C'),(NULL)) t(fk);
        """
    )
    return con


def test_not_in_is_blind_to_a_null_in_the_parent(fk):
    """The single worst way to write this check, and it fails silently.

    One NULL anywhere in the parent column makes `NOT IN` return no rows at
    all: the comparison is UNKNOWN rather than TRUE for every child row, so an
    orphan check reports a clean pass on a table full of orphans. Nothing
    raises, nothing warns, and the report says the referential integrity held.
    """
    assert fk.execute(
        "SELECT count(*) FROM child WHERE fk NOT IN (SELECT pk FROM parent)"
    ).fetchone()[0] == 0

    # The same question, asked correctly, finds two rows.
    assert fk.execute(
        "SELECT count(*) FROM child c "
        "WHERE NOT EXISTS (SELECT 1 FROM parent p WHERE p.pk = c.fk)"
    ).fetchone()[0] == 2


def test_not_exists_and_the_anti_join_agree(fk):
    """Either form is safe. NOT EXISTS is the one validate/ uses, because it
    does not have to name a column on the right-hand side to test for null."""
    not_exists = fk.execute(
        "SELECT count(*) FROM child c "
        "WHERE NOT EXISTS (SELECT 1 FROM parent p WHERE p.pk = c.fk)"
    ).fetchone()[0]
    anti_join = fk.execute(
        "SELECT count(*) FROM child c LEFT JOIN parent p ON p.pk = c.fk "
        "WHERE p.pk IS NULL"
    ).fetchone()[0]
    assert not_exists == anti_join == 2


def test_an_orphan_and_a_null_key_are_two_different_findings(fk):
    """Both are 'unmatched'. Only one is a broken reference.

    A NULL foreign key is a row that points at nothing on purpose -- optional
    by design in most schemas. An orphan points at something that should be
    there and is not. Counting them together produces a number that cannot be
    acted on, so the rule counts them apart.
    """
    orphans = fk.execute(
        "SELECT count(*) FROM child c WHERE c.fk IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM parent p WHERE p.pk = c.fk)"
    ).fetchone()[0]
    nulls = fk.execute(
        "SELECT count(*) - count(fk) FROM child"
    ).fetchone()[0]
    assert (orphans, nulls) == (1, 1)


def test_a_foreign_key_across_two_types_joins_without_complaining(con):
    """VARCHAR '1' matches INTEGER 1, silently.

    Two datasets loaded by different routes can hold the same key as different
    types -- Phase 3's all_text path against a Postgres attach, for instance.
    DuckDB casts and answers rather than raising, which means a type mismatch
    is invisible in the RESULT and has to be reported from the schema instead.
    """
    con.execute(
        """
        CREATE TABLE p2 AS SELECT * FROM (VALUES (1),(2)) t(id);
        CREATE TABLE c2 AS SELECT * FROM (VALUES ('1'),('3')) t(id);
        """
    )
    assert con.execute(
        "SELECT count(*) FROM c2 x "
        "WHERE NOT EXISTS (SELECT 1 FROM p2 p WHERE p.id = x.id)"
    ).fetchone()[0] == 1
    types = dict(con.execute(
        "SELECT table_name, data_type FROM information_schema.columns "
        "WHERE table_name IN ('p2','c2')"
    ).fetchall())
    assert types == {"p2": "INTEGER", "c2": "VARCHAR"}


# --------------------------------------------------------------------------
# key uniqueness: the counting form against the grouping form
# --------------------------------------------------------------------------


def test_the_counting_form_invents_a_duplicate_on_a_one_column_key(con):
    """`count(DISTINCT (x))` is not a row constructor. It is `count(DISTINCT x)`.

    Two rows, 'a' and NULL, no duplicate anywhere. count(DISTINCT (x)) drops
    the NULL and returns 1, so `rows != distinct` reports a duplicate key that
    does not exist -- on a table where the real problem is a missing key value,
    which is a different finding with a different fix.
    """
    con.execute("CREATE TABLE u AS SELECT * FROM (VALUES ('a'),(NULL)) t(x)")
    rows = con.execute("SELECT count(*) FROM u").fetchone()[0]
    counted = con.execute("SELECT count(DISTINCT (x)) FROM u").fetchone()[0]
    grouped = con.execute(
        "SELECT x, count(*) FROM u GROUP BY x HAVING count(*) > 1"
    ).fetchall()

    assert (rows, counted) == (2, 1)   # would read as one duplicate
    assert grouped == []               # there is no duplicate


def test_the_counting_form_is_only_null_safe_when_the_key_is_composite(con):
    """`(x, y)` IS a row constructor, and it does treat NULLs as equal.

    So the same expression is null-safe for a two-column key and null-blind for
    a one-column key. A generic checker that formats the key list into
    `count(DISTINCT ({cols}))` is therefore correct on every composite key it
    is tested against and wrong on the single-column case nobody tested.
    """
    con.execute(
        """CREATE TABLE k AS SELECT * FROM (VALUES
             ('a', 1), ('a', 2), ('b', NULL), ('b', NULL), ('c', NULL)
           ) t(x, y)"""
    )
    assert con.execute("SELECT count(*) FROM k").fetchone()[0] == 5
    assert con.execute("SELECT count(DISTINCT (x, y)) FROM k").fetchone()[0] == 4

    con.execute(
        """CREATE TABLE nn AS SELECT * FROM (VALUES
             ('a', 1), (NULL, NULL), (NULL, NULL)
           ) t(x, y)"""
    )
    # An entirely NULL tuple still counts as a value under the row constructor.
    assert con.execute("SELECT count(DISTINCT (x, y)) FROM nn").fetchone()[0] == 2


def test_group_by_treats_nulls_as_equal_and_names_the_offenders(con):
    """The form validate/ uses, for both shapes of key.

    It is null-safe, it works identically for one column and for five, and --
    the reason it wins outright -- it returns the offending key values. A
    checker that reports "3 duplicate keys" sends somebody to write the query
    that finds them; one that reports the keys has already answered.
    """
    con.execute(
        """CREATE TABLE k AS SELECT * FROM (VALUES
             ('a', 1), ('a', 2), ('b', NULL), ('b', NULL), ('c', NULL)
           ) t(x, y)"""
    )
    assert con.execute(
        "SELECT x, y, count(*) FROM k GROUP BY x, y HAVING count(*) > 1 "
        "ORDER BY x"
    ).fetchall() == [("b", None, 2)]

    con.execute(
        "CREATE TABLE s AS SELECT * FROM (VALUES ('a'),('a'),(NULL),(NULL)) t(x)"
    )
    assert con.execute(
        "SELECT x, count(*) FROM s GROUP BY x HAVING count(*) > 1 ORDER BY x"
    ).fetchall() == [("a", 2), (None, 2)]


def test_a_null_in_a_key_column_is_counted_separately(con):
    """Grain is 'one row = one X'. A row whose key is partly NULL is not
    identified by that key at all, whether or not it duplicates another."""
    con.execute(
        "CREATE TABLE s AS SELECT * FROM (VALUES ('a'),('a'),(NULL),(NULL)) t(x)"
    )
    assert con.execute("SELECT count(*) - count(x) FROM s").fetchone()[0] == 2


# --------------------------------------------------------------------------
# dates, windows and ranges
# --------------------------------------------------------------------------


def test_an_inclusive_window_on_a_timestamp_column_drops_its_last_day(con):
    """The trap that would have silently shortened every window by a day.

    AnalysisWindow says both ends are inclusive, and the contract permits a
    date_column of type TIMESTAMP as readily as DATE. `dt <= DATE '2024-12-31'`
    casts the bound to midnight, so every timestamp later that day falls
    outside a window that claims to include it. The upper bound is written as
    `< end + INTERVAL 1 DAY`, never as `<= end`.
    """
    con.execute(
        """CREATE TABLE ts AS SELECT * FROM (VALUES
             (TIMESTAMP '2024-12-31 23:59:59'),
             (TIMESTAMP '2025-01-01 00:00:00')
           ) t(dt)"""
    )
    assert con.execute(
        "SELECT count(*) FROM ts WHERE dt <= DATE '2024-12-31'"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM ts WHERE dt < DATE '2024-12-31' + INTERVAL 1 DAY"
    ).fetchone()[0] == 1


def test_a_window_predicate_loses_its_nulls(con):
    """inside + outside does not equal the row count, and the gap is silent."""
    con.execute(
        """CREATE TABLE d AS SELECT * FROM (VALUES
             (DATE '2024-01-15'), (DATE '2024-06-01'),
             (DATE '2025-03-01'), (NULL)
           ) t(dt)"""
    )
    rows = con.execute("SELECT count(*) FROM d").fetchone()[0]
    inside = con.execute(
        "SELECT count(*) FROM d WHERE dt >= DATE '2024-01-01' "
        "AND dt < DATE '2024-12-31' + INTERVAL 1 DAY"
    ).fetchone()[0]
    outside = con.execute(
        "SELECT count(*) FROM d WHERE dt < DATE '2024-01-01' "
        "OR dt >= DATE '2024-12-31' + INTERVAL 1 DAY"
    ).fetchone()[0]
    missing = con.execute("SELECT count(*) - count(dt) FROM d").fetchone()[0]

    assert (rows, inside, outside, missing) == (4, 2, 1, 1)
    assert inside + outside != rows
    assert inside + outside + missing == rows


def test_a_range_predicate_loses_its_nulls_too(con):
    """The same arithmetic, on a measure rather than a date. One rule, not two."""
    con.execute("CREATE TABLE r AS SELECT * FROM (VALUES (5),(-1),(NULL)) t(v)")
    rows, below, at_or_above, missing = (
        con.execute(q).fetchone()[0]
        for q in (
            "SELECT count(*) FROM r",
            "SELECT count(*) FROM r WHERE v < 0",
            "SELECT count(*) FROM r WHERE v >= 0",
            "SELECT count(*) - count(v) FROM r",
        )
    )
    assert (rows, below, at_or_above, missing) == (3, 1, 1, 1)
    assert below + at_or_above + missing == rows


def test_a_domain_check_is_blind_to_nulls(con):
    """A value outside a declared set and a value that is absent are different
    findings, and `NOT IN` reports only the first."""
    con.execute(
        "CREATE TABLE g AS SELECT * FROM (VALUES ('A'),('B'),(NULL)) t(v)"
    )
    assert con.execute(
        "SELECT count(*) FROM g WHERE v NOT IN ('A')"
    ).fetchone()[0] == 1
    assert con.execute("SELECT count(*) - count(v) FROM g").fetchone()[0] == 1


def test_a_python_date_and_an_iso_string_bind_identically(con):
    """Either may be passed as a parameter against a DATE column.

    Worth pinning because AnalysisWindow holds `datetime.date` objects while
    every refusal and report renders them with isoformat(), and a rule that
    binds one and prints the other must not be comparing two different things.
    """
    con.execute(
        """CREATE TABLE d AS SELECT * FROM (VALUES
             (DATE '2024-06-01'), (DATE '2025-03-01')
           ) t(dt)"""
    )
    as_date = con.execute(
        "SELECT count(*) FROM d WHERE dt > ?", [dt.date(2024, 12, 31)]
    ).fetchone()[0]
    as_text = con.execute(
        "SELECT count(*) FROM d WHERE dt > ?", ["2024-12-31"]
    ).fetchone()[0]
    assert as_date == as_text == 1
