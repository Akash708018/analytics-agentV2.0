"""
Unit tests for contract/evidence.py.

These use a bare in-memory duckdb.connect() rather than db.connect(WORKSPACE),
which is the opposite of what tests/test_loaders_step5.py needs. The reason is
the whole point of this module: it reads a table and registers nothing, so
_agent_datasets is never touched and a workspace connection would only add a
file on disk. When a test here starts failing on a missing metadata table,
something in evidence.py has started writing.
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine.contract import ContractRefused
from backend.engine.contract.evidence import (
    ColumnEvidence,
    column_stats,
    find_key_candidates,
    gather,
    suggest_role,
)


@pytest.fixture
def con():
    c = duckdb.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def orders(con):
    """A line-item table: order_id repeats, order_id + line_number is the key."""
    con.execute(
        """
        CREATE TABLE orders AS
        SELECT
          'ORD-' || lpad(((i // 3) + 1)::VARCHAR, 6, '0') AS order_id,
          (i % 3) + 1                                     AS line_number,
          'CUST-' || ((i % 90) + 1)::VARCHAR              AS customer_id,
          ['North','South','East','West'][(i % 4) + 1]    AS region,
          DATE '2024-01-01' + ((i % 300)::INTEGER)        AS order_date,
          (i % 40) + 1                                    AS units,
          ((i % 997) * 1.5)                               AS unit_price,
          (i % 5) + 1                                     AS review_score,
          (i % 2 = 0)                                     AS is_return,
          'free text note number ' || (i % 250)::VARCHAR  AS memo
        FROM range(300) t(i)
        """
    )
    return con


# --------------------------------------------------------------------------
# column_stats
# --------------------------------------------------------------------------

def test_stats_cover_every_column_in_source_order(orders):
    stats = column_stats(orders, "orders")
    assert [c.name for c in stats] == [
        "order_id", "line_number", "customer_id", "region", "order_date",
        "units", "unit_price", "review_score", "is_return", "memo",
    ]
    assert all(c.row_count == 300 for c in stats)


def test_min_and_max_come_back_as_text_whatever_the_type(orders):
    stats = {c.name: c for c in column_stats(orders, "orders")}
    assert stats["order_date"].min_value == "2024-01-01"
    assert stats["review_score"].min_value == "1"
    assert stats["review_score"].max_value == "5"


def test_a_nullable_column_reports_its_nulls(con):
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1),(2),(NULL)) v(a)")
    c = column_stats(con, "t")[0]
    assert c.row_count == 3
    assert c.non_null == 2
    assert c.null_count == 1


class _Spy:
    """Records the SQL that passes through. A duckdb connection is a C object
    and will not accept a monkeypatched attribute, so wrap it instead."""

    def __init__(self, con):
        self._con = con
        self.sql = []

    def execute(self, sql, *a, **k):
        self.sql.append(sql)
        return self._con.execute(sql, *a, **k)


def test_stats_are_one_query_not_one_per_column(orders):
    """
    Four aggregates per column in a single SELECT. If this becomes a loop, a
    60-column table costs 60 scans instead of one and nothing else in the
    suite would notice.
    """
    spy = _Spy(orders)
    column_stats(spy, "orders")
    assert sum(1 for s in spy.sql if 'FROM "orders"' in s) == 1


# --------------------------------------------------------------------------
# uniqueness -- the two NULL traps
# --------------------------------------------------------------------------

def test_a_column_of_nulls_is_not_unique(con):
    """
    count(DISTINCT c) drops nulls, so 1, 2, NULL, NULL has distinct == 2 ==
    count(c). Comparing against count(*) is what catches it.
    """
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES (1),(2),(NULL),(NULL)) v(a)"
    )
    c = column_stats(con, "t")[0]
    assert c.distinct == 2
    assert c.non_null == 2
    assert not c.is_unique


def test_a_unique_pair_containing_nulls_is_reported_as_unusable(con):
    """
    count(DISTINCT (a, b)) does NOT drop nulls, so this pair passes the
    uniqueness test while half of b is null. Unique is not the same as
    usable, and the sentence has to say which.
    """
    con.execute(
        """CREATE TABLE t AS SELECT * FROM
           (VALUES (1,'x'),(1,NULL),(2,'y'),(2,NULL),(3,'x'),(3,NULL)) v(a,b)"""
    )
    stats = column_stats(con, "t")
    keys, _p, _s, _n = find_key_candidates(con, "t", stats)
    pair = [k for k in keys if len(k.columns) == 2]
    assert pair, "the pair should be found unique"
    assert pair[0].is_unique
    assert not pair[0].usable
    assert "cannot be a primary key" in pair[0].sentence()


def test_identical_null_bearing_rows_collapse_and_break_uniqueness(con):
    con.execute(
        """CREATE TABLE t AS SELECT * FROM
           (VALUES (1,'a'),(2,'a'),(NULL,'b'),(NULL,'b')) v(a,b)"""
    )
    stats = column_stats(con, "t")
    keys, _p, _s, _n = find_key_candidates(con, "t", stats)
    assert keys == []


# --------------------------------------------------------------------------
# key candidates
# --------------------------------------------------------------------------

def test_the_composite_key_is_found(orders):
    stats = column_stats(orders, "orders")
    keys, probed, skipped, _notes = find_key_candidates(orders, "orders", stats)
    labels = [k.label() for k in keys]
    assert "order_id + line_number" in labels
    assert probed > 0
    assert skipped == 0


def test_a_unique_single_is_found_and_needs_no_pair(con):
    con.execute("CREATE TABLE t AS SELECT i AS id, i % 4 AS grp FROM range(50) s(i)")
    stats = column_stats(con, "t")
    keys, _p, _s, notes = find_key_candidates(con, "t", stats)
    assert keys[0].columns == ("id",)
    assert keys[0].usable
    assert all(k.columns == ("id",) for k in keys)
    assert any("spare column" in n for n in notes)


def test_pairs_containing_a_unique_column_are_never_probed(con):
    """
    Minimality. Every pair containing an already-unique column is unique, so
    probing them returns noise: on a 14-column table it produced 13 spurious
    candidates, all of them the real key plus something irrelevant.
    """
    con.execute(
        "CREATE TABLE t AS SELECT i AS id, i % 4 AS a, i % 5 AS b FROM range(60) s(i)"
    )
    stats = column_stats(con, "t")
    keys, probed, _s, _n = find_key_candidates(con, "t", stats)
    assert not any("id" in k.columns and len(k.columns) == 2 for k in keys)
    assert probed <= 1  # only (a, b) is even eligible


def test_the_arithmetic_prune_skips_impossible_pairs(con):
    """
    A pair can only be unique if d_a * d_b >= row_count. 4 x 5 < 60, so the
    only eligible pair is not probed either.
    """
    con.execute(
        "CREATE TABLE t AS SELECT i % 4 AS a, i % 5 AS b FROM range(60) s(i)"
    )
    stats = column_stats(con, "t")
    keys, probed, _s, _n = find_key_candidates(con, "t", stats)
    assert probed == 0
    assert keys == []


def test_the_probe_cap_reports_what_it_skipped(con):
    cols = ", ".join(
        f"('v' || ((i * {p}) % 97)::VARCHAR) AS c{p}" for p in (7, 11, 13, 17, 19)
    )
    con.execute(f"CREATE TABLE t AS SELECT {cols} FROM range(1000) s(i)")
    stats = column_stats(con, "t")
    _keys, probed, skipped, notes = find_key_candidates(
        con, "t", stats, max_pair_probes=2
    )
    assert probed == 2
    assert skipped > 0
    assert any("not probed" in n for n in notes)


def test_a_unique_measure_is_cautioned_rather_than_offered_as_a_key(con):
    """
    gaps_and_dupes.csv has a revenue column with 200 distinct values in 200
    rows. It is unique, and it is not a key -- it is a DOUBLE that happens not
    to repeat. Suppressing it would be worse than reporting it, so it is
    reported with the reason it is unconvincing.
    """
    con.execute(
        "CREATE TABLE t AS SELECT i AS row_no, i * 1.5 AS revenue FROM range(50) s(i)"
    )
    stats = column_stats(con, "t")
    keys, _p, _s, _n = find_key_candidates(con, "t", stats)
    rev = [k for k in keys if k.columns == ("revenue",)]
    assert rev, "a unique column is still reported"
    assert "not a name for a row" in rev[0].sentence()


def test_probing_can_be_turned_off(orders):
    stats = column_stats(orders, "orders")
    keys, probed, skipped, _n = find_key_candidates(
        orders, "orders", stats, probe_pairs=False
    )
    assert probed == 0 and skipped == 0
    assert all(len(k.columns) == 1 for k in keys)


def test_an_identifier_named_pair_is_probed_first(con):
    """The cap decides what is probed, so the ranking has to put a plausible
    key ahead of a coincidence."""
    con.execute(
        """CREATE TABLE t AS SELECT
             'O' || (i // 2)::VARCHAR AS order_id,
             (i % 2)                  AS line_number,
             'n' || ((i * 7) % 120)::VARCHAR  AS noise_a,
             'n' || ((i * 11) % 120)::VARCHAR AS noise_b
           FROM range(500) s(i)"""
    )
    stats = column_stats(con, "t")
    keys, probed, _s, _n = find_key_candidates(con, "t", stats, max_pair_probes=1)
    assert probed == 1
    assert keys and keys[0].columns == ("order_id", "line_number")


# --------------------------------------------------------------------------
# roles
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "column,expected",
    [
        ("order_id", "identifier"),
        ("customer_id", "identifier"),
        ("region", "dimension"),
        ("order_date", "date"),
        ("unit_price", "measure"),
        ("review_score", "dimension"),
        ("is_return", "flag"),
        ("memo", "free_text"),
    ],
)
def test_roles_on_the_orders_shape(orders, column, expected):
    stats = {c.name: c for c in column_stats(orders, "orders")}
    assert suggest_role(stats[column])[0] == expected


def test_a_repeating_id_column_is_still_an_identifier(orders):
    """
    customer_id repeats 90 ways in 300 rows. Judged on cardinality alone it
    reads as a dimension; judged on its name it is a foreign key, and summing
    it is meaningless whatever its type (guide 6.2).
    """
    stats = {c.name: c for c in column_stats(orders, "orders")}
    role, why = suggest_role(stats["customer_id"])
    assert role == "identifier"
    assert "foreign key" in why


def test_a_small_integer_column_is_not_offered_as_a_measure(orders):
    """
    Olist review scores are 1-5 and J-shaped. An agent that sums them produces
    a number with no meaning, so the suggestion has to be dimension and the
    reason has to say why.
    """
    stats = {c.name: c for c in column_stats(orders, "orders")}
    role, why = suggest_role(stats["review_score"])
    assert role == "dimension"
    assert "rating or a bucket" in why


def test_an_all_null_column_is_ignored(con):
    con.execute("CREATE TABLE t AS SELECT NULL::INTEGER AS a FROM range(5)")
    assert suggest_role(column_stats(con, "t")[0])[0] == "ignore"


def test_a_constant_column_is_ignored(con):
    con.execute("CREATE TABLE t AS SELECT 'X' AS status FROM range(5)")
    assert suggest_role(column_stats(con, "t")[0])[0] == "ignore"


def test_role_reasons_are_never_blank(orders):
    for c in column_stats(orders, "orders"):
        _role, why = suggest_role(c)
        assert why.strip() and why.endswith(".")


# --------------------------------------------------------------------------
# gather
# --------------------------------------------------------------------------

def test_gather_assembles_the_whole_picture(orders):
    ev = gather(orders, "orders")
    assert ev.row_count == 300
    assert ev.column_count == 10
    assert "order_id + line_number" in [k.label() for k in ev.usable_keys()]
    assert [c.name for c in ev.date_columns()] == ["order_date"]


def test_gather_refuses_a_dataset_that_is_not_loaded(orders):
    with pytest.raises(ContractRefused) as exc:
        gather(orders, "nope")
    msg = str(exc.value)
    assert "NEXT STEP" in msg
    assert "orders" in msg  # names what IS loaded


def test_a_text_date_column_is_named_in_the_notes(con):
    con.execute(
        """CREATE TABLE t AS SELECT
             i AS id, ('2024-01-0' || ((i % 9) + 1)::VARCHAR) AS order_date
           FROM range(20) s(i)"""
    )
    notes = " ".join(gather(con, "t").notes)
    assert "named like a date but is stored as VARCHAR" in notes
    assert "'order_date': 'DATE'" in notes


def test_a_table_with_no_date_column_says_so(con):
    con.execute("CREATE TABLE t AS SELECT i AS id, i % 3 AS grp FROM range(10) s(i)")
    assert any("No DATE or TIMESTAMP" in n for n in gather(con, "t").notes)


def test_an_empty_table_does_not_claim_a_key(con):
    con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
    ev = gather(con, "t")
    assert ev.row_count == 0
    assert ev.usable_keys() == []
    assert any("empty" in n for n in ev.notes)


def test_to_text_is_pipe_delimited_and_names_the_key(orders):
    text = gather(orders, "orders").to_text()
    assert "| column | type | suggested role | distinct | nulls |" in text
    assert "order_id + line_number is unique across 300 of 300 rows" in text
    assert "Date columns:" in text


def test_to_text_says_so_when_nothing_is_unique(con):
    con.execute("CREATE TABLE t AS SELECT i % 3 AS a, i % 4 AS b FROM range(40) s(i)")
    text = gather(con, "t").to_text()
    assert "No column or pair identifies a row uniquely" in text


def test_evidence_sentences_carry_the_counts(orders):
    ev = gather(orders, "orders")
    assert (
        ev.column("region").sentence()
        == "region (VARCHAR): 4 distinct value(s) across 300 rows, no nulls, "
           "range East to West."
    )
