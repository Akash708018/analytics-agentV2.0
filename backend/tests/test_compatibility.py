"""
Unit tests for contract/compatibility.py.

Split in two on purpose. `classify_drift` is pure -- two Bindings and a
contract, no database -- so its tests are a table of shapes. `verify_key` and
`binding_for` run SQL, so those use a bare in-memory duckdb.connect() for the
same reason test_evidence.py does: nothing here registers a dataset, and a
workspace connection would only put a file on disk.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from backend.engine.contract import ContractRefused
from backend.engine.contract.compatibility import (
    Drift,
    binding_for,
    classify_drift,
    verify_key,
)
from backend.engine.contract.dataset_contract import (
    Binding,
    DatasetContract,
    Measure,
)
from backend.engine.contract.refusals import Reason, reason_of

COLUMNS = [
    ("order_id", "VARCHAR"),
    ("order_item_id", "INTEGER"),
    ("shipping_limit_date", "TIMESTAMP"),
    ("price", "DECIMAL(10,2)"),
    ("freight_value", "DECIMAL(10,2)"),
    ("seller_id", "VARCHAR"),
]


def bind(columns=COLUMNS, rows=112_650) -> Binding:
    return Binding.from_pairs(columns, rows, datetime(2026, 8, 31, 9, 0))


def contract(**overrides) -> DatasetContract:
    """Names order_id, order_item_id, shipping_limit_date, price, seller_id.

    freight_value is deliberately NOT named, so there is a column in the table
    the contract does not care about.
    """
    kwargs = dict(
        dataset_name="order_items",
        grain="one row = one item on one order",
        primary_key=["order_id", "order_item_id"],
        date_column="shipping_limit_date",
        measures=[Measure(name="price", agg="sum", definition="item price")],
        dimensions=["seller_id"],
        bound_to=bind(),
    )
    kwargs.update(overrides)
    return DatasetContract(**kwargs)


# --------------------------------------------------------------------------
# the four classes
# --------------------------------------------------------------------------

def test_nothing_changed_is_identical():
    v = classify_drift(contract(), bind())
    assert v.drift is Drift.IDENTICAL
    assert not v.blocks
    assert v.caveat() == ""


def test_more_rows_is_neutral():
    v = classify_drift(contract(), bind(rows=120_000))
    assert v.drift is Drift.NEUTRAL
    assert not v.blocks
    assert v.rows_changed == 7_350
    assert "gained 7,350 rows" in v.caveat()
    assert "definitions still apply" in v.caveat()


def test_fewer_rows_is_also_neutral_and_says_lost():
    v = classify_drift(contract(), bind(rows=100_000))
    assert v.drift is Drift.NEUTRAL
    assert "lost 12,650 rows" in v.caveat()


def test_a_new_column_is_additive():
    v = classify_drift(contract(), bind(COLUMNS + [("tax", "DECIMAL(10,2)")]))
    assert v.drift is Drift.ADDITIVE
    assert v.added == ["tax"]
    assert not v.blocks
    assert "new column(s) tax" in v.caveat()


def test_dropping_a_column_the_contract_ignores_is_additive():
    """
    freight_value is in the table and in no part of the contract. Refusing
    here would train whoever reads the refusal to ignore it, which is what a
    gate that fires unnecessarily actually costs.
    """
    without = [c for c in COLUMNS if c[0] != "freight_value"]
    v = classify_drift(contract(), bind(without))
    assert v.drift is Drift.ADDITIVE
    assert v.removed == ["freight_value"]
    assert not v.blocks
    assert "dropped column(s) freight_value" in v.caveat()


def test_an_additive_caveat_does_not_drop_the_row_count():
    """
    A structural change and a volume change can happen at once. The first
    version reported only the structure, which meant a result carried a
    caveat that quietly omitted the table having also grown.
    """
    v = classify_drift(contract(), bind(COLUMNS + [("tax", "INTEGER")], rows=120_000))
    caveat = v.caveat()
    assert "new column(s) tax" in caveat
    assert "gained 7,350 rows" in caveat


def test_reordering_is_not_breaking_here():
    """
    The contract refers to columns by name, so a reorder is harmless. One
    layer down it is not: IngestSpec carries `names` positionally. Same event,
    two correct and opposite answers.
    """
    swapped = [COLUMNS[1], COLUMNS[0]] + COLUMNS[2:]
    v = classify_drift(contract(), bind(swapped))
    assert v.drift is Drift.ADDITIVE
    assert v.reordered
    assert not v.blocks


def test_dropping_a_named_column_is_destructive():
    without = [c for c in COLUMNS if c[0] != "price"]
    v = classify_drift(contract(), bind(without))
    assert v.drift is Drift.DESTRUCTIVE
    assert v.blocks
    assert v.breaking == ["price"]


def test_retyping_a_named_column_is_destructive():
    retyped = [(n, "VARCHAR" if n == "price" else t) for n, t in COLUMNS]
    v = classify_drift(contract(), bind(retyped))
    assert v.drift is Drift.DESTRUCTIVE
    assert v.retyped == [("price", "DECIMAL(10,2)", "VARCHAR")]
    assert v.breaking == ["price"]


def test_retyping_a_column_nobody_named_is_not_destructive():
    retyped = [(n, "VARCHAR" if n == "freight_value" else t) for n, t in COLUMNS]
    v = classify_drift(contract(), bind(retyped))
    assert v.drift is Drift.ADDITIVE
    assert not v.blocks


def test_destructive_wins_over_everything_else():
    """A table that gained rows AND lost a named column is not neutral."""
    without = [c for c in COLUMNS if c[0] != "seller_id"]
    v = classify_drift(contract(), bind(without, rows=200_000))
    assert v.drift is Drift.DESTRUCTIVE
    assert v.breaking == ["seller_id"]


def test_case_of_a_type_is_not_drift():
    lowered = [(n, t.lower()) for n, t in COLUMNS]
    assert classify_drift(contract(), bind(lowered)).drift is Drift.IDENTICAL


# --------------------------------------------------------------------------
# the refusal a destructive verdict produces
# --------------------------------------------------------------------------

def test_a_destructive_verdict_refuses_with_a_code():
    without = [c for c in COLUMNS if c[0] != "price"]
    text = classify_drift(contract(), bind(without)).refusal().to_text()
    assert reason_of(text) is Reason.CONTRACT_STALE
    assert "price" in text
    assert "propose_dataset_contract" in text
    assert "CURRENT STATE:" in text


def test_the_refusal_says_the_old_contract_is_kept():
    without = [c for c in COLUMNS if c[0] != "price"]
    text = classify_drift(contract(), bind(without)).refusal().to_text()
    assert "superseded contract stays in the log" in text


def test_a_retype_is_spelled_out_in_the_detail():
    retyped = [(n, "VARCHAR" if n == "price" else t) for n, t in COLUMNS]
    text = classify_drift(contract(), bind(retyped)).refusal().to_text()
    assert "price was DECIMAL(10,2), is now VARCHAR" in text


def test_a_non_blocking_verdict_has_no_refusal():
    for observed in (bind(), bind(rows=1), bind(COLUMNS + [("tax", "INTEGER")])):
        assert classify_drift(contract(), observed).refusal() is None


def test_a_contract_with_no_binding_cannot_be_classified():
    unbound = DatasetContract(dataset_name="x", grain="one row = one thing")
    with pytest.raises(ContractRefused) as exc:
        classify_drift(unbound, bind())
    assert reason_of(str(exc.value)) is Reason.CONTRACT_INVALID


def test_to_text_reads_as_a_diff():
    retyped = [(n, "VARCHAR" if n == "price" else t) for n, t in COLUMNS]
    text = classify_drift(contract(), bind(retyped, rows=1)).to_text()
    assert "order_items: DESTRUCTIVE" in text
    assert "retyped   price: DECIMAL(10,2) -> VARCHAR" in text
    assert "rows      112,650 -> 1" in text
    assert "BREAKING  price" in text


# --------------------------------------------------------------------------
# live: binding_for and verify_key
# --------------------------------------------------------------------------

@pytest.fixture
def con():
    c = duckdb.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def items(con):
    """Line items: order_id repeats, order_id + order_item_id is the key."""
    con.execute(
        """
        CREATE TABLE order_items AS
        SELECT
          'ORD-' || lpad(((i // 3) + 1)::VARCHAR, 6, '0') AS order_id,
          ((i % 3) + 1)::INTEGER                          AS order_item_id,
          'SELL-' || ((i % 40) + 1)::VARCHAR              AS seller_id,
          ((i % 97) + 1) * 1.5                            AS price
        FROM range(300) t(i)
        """
    )
    return con


def test_binding_for_reads_the_live_table(items):
    b = binding_for(items, "order_items")
    assert b.column_names == ["order_id", "order_item_id", "seller_id", "price"]
    assert b.row_count == 300
    assert b.dtypes["order_item_id"] == "INTEGER"


def test_binding_for_is_stable_across_calls(items):
    assert binding_for(items, "order_items").fingerprint == (
        binding_for(items, "order_items").fingerprint
    )


def test_binding_for_refuses_a_table_that_is_not_there(items):
    with pytest.raises(ContractRefused) as exc:
        binding_for(items, "nope")
    assert reason_of(str(exc.value)) is Reason.DATASET_NOT_LOADED


def test_a_stated_composite_key_is_verified(items):
    v = verify_key(items, "order_items", ["order_id", "order_item_id"])
    assert v.holds
    assert v.is_unique
    assert v.refusal() is None
    assert "unique across 300 of 300 rows" in v.sentence()


def test_a_key_that_repeats_is_counted_not_just_rejected(items):
    """
    'order_id is not unique' is true and useless. How many rows repeat is what
    tells you whether the grain is wrong or the data is dirty.
    """
    v = verify_key(items, "order_items", ["order_id"])
    assert not v.holds
    assert v.distinct == 100
    assert v.duplicate_rows == 200
    assert "200 row(s) repeat" in v.sentence()


def test_a_repeating_key_refuses_with_key_not_unique(items):
    text = verify_key(items, "order_items", ["order_id"]).refusal().to_text()
    assert reason_of(text) is Reason.KEY_NOT_UNIQUE
    assert "double-counts" in text
    assert "primary_key=[...]" in text


def test_a_single_column_key_is_verified(con):
    con.execute("CREATE TABLE t AS SELECT i AS id FROM range(50) s(i)")
    assert verify_key(con, "t", ["id"]).holds


def test_a_key_column_with_nulls_does_not_hold(con):
    """
    count(DISTINCT (a,b)) does not drop nulls, so this pair passes the
    uniqueness test. Null counts are gathered separately for exactly this.
    """
    con.execute(
        """CREATE TABLE t AS SELECT * FROM
           (VALUES (1,'x'),(1,NULL),(2,'y'),(2,NULL),(3,'x'),(3,NULL)) v(a,b)"""
    )
    v = verify_key(con, "t", ["a", "b"])
    assert v.is_unique
    assert not v.holds
    assert v.null_bearing == ["b"]
    assert "b is null in 3 row(s)" in v.sentence()


def test_nulls_and_duplicates_are_both_reported(con):
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES (1),(1),(NULL),(NULL)) v(a)"
    )
    v = verify_key(con, "t", ["a"])
    assert not v.holds
    assert v.null_bearing == ["a"]
    assert "repeat" in v.sentence()


def test_a_key_naming_a_missing_column_says_so(items):
    v = verify_key(items, "order_items", ["order_id", "line_no"])
    assert not v.holds
    assert v.missing == ["line_no"]
    assert reason_of(v.refusal().to_text()) is Reason.CONTRACT_STALE


def test_verify_key_needs_a_column(items):
    with pytest.raises(ValueError):
        verify_key(items, "order_items", [])


def test_verify_key_refuses_a_table_that_is_not_there(items):
    with pytest.raises(ContractRefused) as exc:
        verify_key(items, "nope", ["a"])
    assert reason_of(str(exc.value)) is Reason.DATASET_NOT_LOADED


def test_verification_finds_a_key_search_would_miss(con):
    """
    The hole Step 1 recorded. `line` is numeric, has 21 distinct values -- past
    the twelve at which a numeric column stops being offered as a dimension --
    and is not named like an identifier, so pair search never probes it. Stated
    rather than searched for, it verifies in one query.
    """
    con.execute(
        """CREATE TABLE t AS SELECT
             'ORD-' || (i // 21)::VARCHAR AS order_ref,
             ((i % 21) + 1)::INTEGER      AS line,
             (i * 1.5)                    AS amount
           FROM range(420) s(i)"""
    )
    from backend.engine.contract.evidence import gather

    searched = [k.label() for k in gather(con, "t").usable_keys()]
    assert "order_ref + line" not in searched

    assert verify_key(con, "t", ["order_ref", "line"]).holds


def test_a_contract_round_trips_through_a_live_binding(items):
    """
    binding_for -> contract -> classify_drift against the same table. The
    fingerprint has to survive the trip, or every contract would read as
    stale the moment it was stored.
    """
    b = binding_for(items, "order_items")
    c = DatasetContract(
        dataset_name="order_items",
        grain="one row = one item on one order",
        primary_key=["order_id", "order_item_id"],
        measures=[Measure(name="price", agg="sum", definition="item price")],
        dimensions=["seller_id"],
        bound_to=b,
    )
    assert classify_drift(c, binding_for(items, "order_items")).drift is Drift.IDENTICAL


def test_a_reloaded_table_with_a_dropped_column_is_caught(items):
    b = binding_for(items, "order_items")
    c = DatasetContract(
        dataset_name="order_items",
        grain="one row = one item on one order",
        measures=[Measure(name="price", agg="sum", definition="item price")],
        bound_to=b,
    )
    items.execute("CREATE OR REPLACE TABLE order_items AS SELECT 1 AS order_id")
    v = classify_drift(c, binding_for(items, "order_items"))
    assert v.drift is Drift.DESTRUCTIVE
    assert v.breaking == ["price"]
    assert reason_of(v.refusal().to_text()) is Reason.CONTRACT_STALE


# --------------------------------------------------------------------------
# P7-D6: a null key is not a duplicate key
#
# Phase 7, Step 2. Appended rather than spliced. `distinct` comes from
# count(DISTINCT x) for a one-column key and count(DISTINCT (a, b)) for a
# composite; the first drops nulls and the second does not, so the two forms
# need different denominators. None of these changes a verdict -- `holds` is
# asserted unchanged in the last test -- they change what the refusal SAYS.
# --------------------------------------------------------------------------


def _p7_key_table(name: str, values: str, columns: str = "order_id"):
    """A throwaway in-memory table. Returns an open connection to close."""
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute(
        f"CREATE TABLE {name} AS SELECT * FROM (VALUES {values}) v({columns})"
    )
    return con


def test_p7_a_null_key_is_not_reported_as_a_duplicate():
    """The defect, in the shape it shipped in. Three rows, no repeat."""
    from backend.engine.contract.compatibility import verify_key

    con = _p7_key_table("t", "('a'),('b'),(NULL)")
    try:
        v = verify_key(con, "t", ["order_id"])
        assert (v.row_count, v.distinct, v.keyed_rows) == (3, 2, 2)
        assert v.duplicate_rows == 0
        assert "repeat" not in v.sentence()
        assert "null in 1 row(s)" in v.sentence()
    finally:
        con.close()


def test_p7_a_real_duplicate_is_still_reported():
    from backend.engine.contract.compatibility import verify_key

    con = _p7_key_table("t", "('a'),('a'),('b')")
    try:
        v = verify_key(con, "t", ["order_id"])
        assert v.duplicate_rows == 1
        assert not v.is_unique
        assert "1 row(s) repeat" in v.sentence()
    finally:
        con.close()


def test_p7_a_key_with_both_faults_counts_each_once():
    """One null and one repeat, reported as one of each rather than two
    repeats and a null."""
    from backend.engine.contract.compatibility import verify_key

    con = _p7_key_table("t", "('a'),('a'),(NULL)")
    try:
        v = verify_key(con, "t", ["order_id"])
        assert (v.keyed_rows, v.distinct, v.duplicate_rows) == (2, 1, 1)
        sentence = v.sentence()
        assert "1 row(s) repeat" in sentence
        assert "null in 1 row(s)" in sentence
    finally:
        con.close()


def test_p7_an_all_null_key_announces_no_repeats():
    """Nothing is comparable, so nothing is claimed. It still does not hold."""
    from backend.engine.contract.compatibility import verify_key

    con = _p7_key_table("t", "(NULL),(NULL)")
    try:
        v = verify_key(con, "t", ["order_id"])
        assert v.keyed_rows == 0
        assert not v.is_unique and not v.holds
        assert v.duplicate_rows == 0
        assert "repeat" not in v.sentence()
    finally:
        con.close()


def test_p7_a_composite_key_is_untouched():
    """The row constructor already counted null-bearing tuples, so the
    denominator for a composite key is the row count and nothing moves."""
    from backend.engine.contract.compatibility import verify_key

    con = _p7_key_table(
        "c", "('a',1),('b',NULL),('c',NULL)", "order_id, line_no"
    )
    try:
        v = verify_key(con, "c", ["order_id", "line_no"])
        assert v.keyed_rows == v.row_count == 3
        assert v.is_unique and not v.holds     # unique, not usable
        assert "repeat" not in v.sentence()
        assert "line_no is null in 2 row(s)" in v.sentence()
    finally:
        con.close()


def test_p7_an_empty_table_gets_a_sentence_of_its_own():
    """It used to read "0 distinct combinations across 0 rows"."""
    from backend.engine.contract.compatibility import verify_key

    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE e (order_id VARCHAR)")
        v = verify_key(con, "e", ["order_id"])
        assert not v.holds
        assert "no rows" in v.sentence()
        assert "distinct" not in v.sentence()
    finally:
        con.close()


def test_p7_no_verdict_moves():
    """The claim that makes this safe to land mid-phase.

    holds requires no null in any key column, and with no nulls keyed_rows IS
    row_count -- so every gate decision is what it was. Asserted across the
    five shapes rather than reasoned about in a comment.
    """
    from backend.engine.contract.compatibility import verify_key

    shapes = {
        "clean": ("('a'),('b')", True),
        "dupes": ("('a'),('a')", False),
        "nulls": ("('a'),(NULL)", False),
        "both_of_them": ("('a'),('a'),(NULL)", False),
        "all_null": ("(NULL),(NULL)", False),
    }
    for name, (values, expected) in shapes.items():
        con = _p7_key_table(name, values)
        try:
            assert verify_key(con, name, ["order_id"]).holds is expected, name
        finally:
            con.close()
