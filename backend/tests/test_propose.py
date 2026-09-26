"""
Unit tests for contract/propose.py.

Bare in-memory connections, as in test_evidence.py and test_compatibility.py:
proposing stores nothing, so a workspace connection would only put a file on
disk. The day one of these fails on a missing `_agent_datasets`, something in
the proposal layer has started writing.
"""

from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from backend.engine.contract import ContractRefused
from backend.engine.contract.dataset_contract import (
    Exclusion,
    contract_from_json,
)
from backend.engine.contract.propose import propose_contract
from backend.engine.contract.refusals import Reason, reason_of


@pytest.fixture
def con():
    c = duckdb.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def items(con):
    """
    Line items. order_id + order_item_id is the key; price and freight_value
    read as measures; seller_id and status as dimensions; one date column.
    """
    con.execute(
        """
        CREATE TABLE order_items AS
        SELECT
          'ORD-' || lpad(((i // 3) + 1)::VARCHAR, 6, '0')  AS order_id,
          ((i % 3) + 1)::INTEGER                           AS order_item_id,
          'SELL-' || ((i % 40) + 1)::VARCHAR               AS seller_id,
          ['placed','shipped','cancelled'][(i % 3) + 1]    AS status,
          (DATE '2024-01-01' + ((i % 300)::INTEGER))       AS order_date,
          (((i % 97) + 1) * 1.5)                           AS price,
          (((i % 61) + 1) * 0.5)                           AS freight_value
        FROM range(300) t(i)
        """
    )
    return con


# --------------------------------------------------------------------------
# the derived half
# --------------------------------------------------------------------------

def test_the_key_comes_from_evidence_when_none_is_stated(items):
    c = propose_contract(items, "order_items").contract
    assert c.primary_key == ["order_id", "order_item_id"]


def test_the_grain_is_derived_from_the_key_in_column_names(items):
    """
    Not English. 'one row = one order line item' is the same sentence with a
    business meaning invented, and it reads as knowledge.
    """
    c = propose_contract(items, "order_items").contract
    assert c.grain == "one row = one (order_id, order_item_id)"


def test_a_single_column_key_gives_a_singular_grain(con):
    con.execute(
        "CREATE TABLE t AS SELECT i AS row_id, i % 4 AS grp FROM range(40) s(i)"
    )
    assert propose_contract(con, "t").contract.grain == "one row = one row_id"


def test_a_coincidentally_unique_column_does_not_become_the_key(items):
    """
    order_date holds 300 distinct dates in 300 rows, so it is unique -- by
    accident of the fixture, exactly as (order_date, unit_price) was unique by
    accident on the 200k probe. Evidence returns unique singles before pairs,
    so taking the first candidate produced 'one row = one order_date': true
    arithmetic, meaningless grain. Candidates whose columns all read as
    identifiers are ranked first.
    """
    p = propose_contract(items, "order_items")
    assert p.evidence.column("order_date").is_unique
    assert p.contract.primary_key == ["order_id", "order_item_id"]
    assert any("Also unique in this data" in n for n in p.notes)


def test_a_repeating_identifier_is_offered_as_a_dimension(items):
    """
    seller_id is a foreign key: an identifier by name, repeating 40 ways in
    300 rows. Excluding every identifier from the dimensions left `status` as
    the only breakdown this table had, which is not a description anyone would
    recognise. The key's own columns stay out -- grouping by the key returns
    the table.
    """
    c = propose_contract(items, "order_items").contract
    assert "seller_id" in c.dimensions
    assert "order_id" not in c.dimensions
    assert "order_item_id" not in c.dimensions


def test_measures_and_dimensions_come_from_the_roles(items):
    c = propose_contract(items, "order_items").contract
    assert c.measure_names == ["price", "freight_value"]
    assert c.dimensions == ["seller_id", "status"]


def test_identifiers_are_neither_measured_nor_grouped(items):
    c = propose_contract(items, "order_items").contract
    assert "order_id" not in c.measure_names
    assert "order_id" not in c.dimensions


def test_the_only_date_column_is_taken_without_a_question(items):
    p = propose_contract(items, "order_items")
    assert p.contract.date_column == "order_date"
    assert "date_column" not in p.contract.unresolved


def test_the_contract_is_bound_to_the_live_table(items):
    c = propose_contract(items, "order_items").contract
    assert c.bound_to is not None
    assert c.bound_to.row_count == 300
    assert c.fingerprint is not None


# --------------------------------------------------------------------------
# the half nobody can derive
# --------------------------------------------------------------------------

def test_a_fresh_proposal_is_never_confirmable(items):
    p = propose_contract(items, "order_items")
    assert p.needs_answer
    assert not p.contract.is_confirmable


def test_grain_is_filled_in_and_still_unresolved(items):
    """D2, as two behaviours rather than one rule."""
    c = propose_contract(items, "order_items").contract
    assert c.grain
    assert "grain" in c.unresolved


def test_every_measure_definition_is_blank_and_declared(items):
    c = propose_contract(items, "order_items").contract
    assert all(m.definition == "" for m in c.measures)
    assert "measures[price].definition" in c.unresolved
    assert "measures[freight_value].definition" in c.unresolved


def test_the_window_is_asked_about_never_proposed(items):
    """
    On olist, shipping_limit_date runs to 2020-04-09, past the end of the
    order data. A window taken from a column's span would have looked
    defensible and included a tail nobody wants.
    """
    p = propose_contract(items, "order_items")
    assert p.contract.analysis_window is None
    assert "analysis_window" in p.contract.unresolved
    q = " ".join(p.contract.questions)
    assert "2024-01-01" in q and "2024-10-26" in q
    assert "not the same as the window you want" in q


def test_no_date_column_means_no_window_question(con):
    con.execute("CREATE TABLE t AS SELECT i AS id, i % 3 AS grp FROM range(9) s(i)")
    c = propose_contract(con, "t").contract
    assert c.date_column is None
    assert "analysis_window" not in c.unresolved
    assert c.analysis_window is None


def test_several_date_columns_are_asked_about(con):
    con.execute(
        """CREATE TABLE t AS SELECT
             i AS row_id,
             DATE '2024-01-01' + (i::INTEGER)      AS created_at,
             DATE '2024-02-01' + ((i % 5)::INTEGER) AS approved_at
           FROM range(30) s(i)"""
    )
    c = propose_contract(con, "t").contract
    assert "date_column" in c.unresolved
    assert any("Which date should analysis be based on" in q for q in c.questions)


def test_no_exclusions_are_ever_invented(items):
    """
    'cancelled is not real revenue' cannot be derived from a column of
    statuses, and the fixture has a status column precisely so this can fail
    if someone tries.
    """
    assert propose_contract(items, "order_items").contract.known_exclusions == []


def test_every_unresolved_entry_has_a_question(items):
    """
    One question may settle two entries -- a measure's definition and its
    aggregation are asked together, because they are the same question about
    the same column.
    """
    c = propose_contract(items, "order_items").contract
    for entry in c.unresolved:
        column = entry.split("[")[1].split("]")[0] if "[" in entry else None
        assert any((column or "one row") in q or "one row" in q
                   for q in c.questions), f"nothing asks about {entry}"
    assert all(q.strip().endswith(("?", ".")) for q in c.questions)


# --------------------------------------------------------------------------
# answers coming back
# --------------------------------------------------------------------------

def test_answering_everything_makes_it_confirmable(items):
    p = propose_contract(
        items,
        "order_items",
        grain="one row = one item on one order",
        measure_definitions={
            "price": "item price, excludes freight",
            "freight_value": "shipping charged to the customer",
        },
        aggregations={"price": "none", "freight_value": "sum"},
        analysis_window=(date(2024, 1, 1), date(2024, 10, 26)),
    )
    assert p.contract.is_confirmable
    assert p.contract.unresolved == []
    assert not p.needs_answer


def test_a_partial_answer_leaves_the_rest_outstanding(items):
    c = propose_contract(items, "order_items", grain="one row = one shipped item").contract
    assert "grain" not in c.unresolved
    assert "measures[price].definition" in c.unresolved
    assert c.grain == "one row = one shipped item"


def test_a_stated_grain_is_used_verbatim(items):
    c = propose_contract(items, "order_items", grain="  one row = one unit sold  ").contract
    assert c.grain == "one row = one unit sold"


def test_a_stated_key_is_verified_not_trusted(items):
    p = propose_contract(items, "order_items", primary_key=["order_id", "order_item_id"])
    assert p.key_verdict is not None
    assert p.key_verdict.holds
    assert any("Key as stated" in n for n in p.notes)


def test_a_stated_key_that_does_not_hold_is_refused_at_proposal_time(items):
    """
    Not written into a contract that fails at the gate three steps later.
    """
    with pytest.raises(ContractRefused) as exc:
        propose_contract(items, "order_items", primary_key=["order_id"])
    text = str(exc.value)
    assert reason_of(text) is Reason.KEY_NOT_UNIQUE
    assert "200 row(s) repeat" in text


def test_a_stated_key_search_would_have_missed(con):
    """
    The Step 1 hole, answered. `line` is numeric with 21 distinct values and
    is not identifier-named, so pair search never probes it.
    """
    con.execute(
        """CREATE TABLE invoices AS SELECT
             'INV-' || (i // 21)::VARCHAR AS invoice_ref,
             ((i % 21) + 1)::INTEGER      AS line,
             (i * 1.5)                    AS amount
           FROM range(420) s(i)"""
    )
    searched = propose_contract(con, "invoices").contract
    assert searched.primary_key != ["invoice_ref", "line"]

    stated = propose_contract(
        con, "invoices", primary_key=["invoice_ref", "line"]
    ).contract
    assert stated.primary_key == ["invoice_ref", "line"]
    assert stated.grain == "one row = one (invoice_ref, line)"


def test_a_stated_aggregation_is_honoured(items):
    c = propose_contract(
        items, "order_items",
        aggregations={"price": "mean", "freight_value": "sum"},
        measure_definitions={"price": "d", "freight_value": "d"},
    ).contract
    assert c.measure("price").agg == "mean"
    assert c.measure("freight_value").agg == "sum"


def test_an_unstated_aggregation_blocks_the_contract(items):
    """
    No default. Every semantic layer in production requires this field --
    LookML `type:`, Cube `type`, MetricFlow `agg` -- because the guess that
    gets guessed is sum, and summing a unit price is meaningless in a way
    nothing downstream can detect.
    """
    c = propose_contract(
        items, "order_items", grain="g",
        measure_definitions={"price": "d", "freight_value": "d"},
        analysis_window=(date(2024, 1, 1), date(2024, 10, 26)),
    ).contract
    assert c.measure("price").agg is None
    assert "measures[price].agg" in c.unresolved
    assert not c.is_confirmable


def test_the_aggregation_question_lists_the_vocabulary(items):
    """An answer is easier to give when the allowed values are in the ask."""
    q = " ".join(propose_contract(items, "order_items").contract.questions)
    assert "count_distinct" in q
    assert "'none' if it must not be combined" in q


def test_non_additive_is_statable(items):
    """
    A price is not summed and not averaged either, necessarily -- 'none' says
    the column is meaningful per row and must not be combined.
    """
    c = propose_contract(
        items, "order_items", grain="g",
        measure_definitions={"price": "d", "freight_value": "d"},
        aggregations={"price": "none", "freight_value": "sum"},
        analysis_window=(date(2024, 1, 1), date(2024, 10, 26)),
    ).contract
    assert c.measure("price").agg == "none"
    assert c.is_confirmable


def test_stated_measures_override_the_roles(items):
    c = propose_contract(
        items, "order_items", measures=["price"],
        measure_definitions={"price": "item price"},
        aggregations={"price": "sum"},
    ).contract
    assert c.measure_names == ["price"]
    assert "freight_value" not in c.dimensions


def test_a_column_promoted_to_measure_leaves_the_dimensions(items):
    """order_item_id reads as an identifier; forcing it to a measure must not
    also leave it grouped."""
    c = propose_contract(
        items, "order_items",
        measures=["order_item_id"], dimensions=["seller_id", "order_item_id"],
        measure_definitions={"order_item_id": "line position"},
        aggregations={"order_item_id": "count"},
    ).contract
    assert c.measure_names == ["order_item_id"]
    assert c.dimensions == ["seller_id"]


def test_exclusions_and_caveats_are_carried_through(items):
    c = propose_contract(
        items, "order_items",
        grain="one row = one item",
        measure_definitions={"price": "d", "freight_value": "d"},
        aggregations={"price": "none", "freight_value": "sum"},
        analysis_window=(date(2024, 1, 1), date(2024, 6, 30)),
        known_exclusions=[
            Exclusion(rule="status = 'cancelled'", reason="not real revenue",
                      row_count=100)
        ],
        caveats=["February 2024 is short -- source feed outage"],
    ).contract
    assert c.known_exclusions[0].row_count == 100
    assert c.caveats[0].startswith("February")


def test_a_dataset_that_is_not_loaded_is_refused(con):
    con.execute("CREATE TABLE other AS SELECT 1 AS a")
    with pytest.raises(ContractRefused) as exc:
        propose_contract(con, "nope")
    assert "nope" in str(exc.value)


# --------------------------------------------------------------------------
# what the user reads
# --------------------------------------------------------------------------

def test_the_render_refuses_and_says_what_to_call(items):
    text = propose_contract(items, "order_items").to_text()
    assert "CANNOT BE CONFIRMED" in text
    assert "Put these to the user:" in text
    assert "propose_dataset_contract again" in text
    assert "```json" not in text


def test_each_question_is_asked_exactly_once(items):
    """
    The contract body and the render both had a question list, so a first-turn
    proposal printed all four twice. Phase 3 Step 8 found the same shape with
    assumptions and recorded why it matters: two identical lists in one result
    read as two different lists, and the user answers the wrong one.
    """
    p = propose_contract(items, "order_items")
    text = p.to_text()
    for q in p.contract.questions:
        assert text.count(q) == 1
    assert text.count("Put these to the user:") == 1
    assert "Open questions:" not in text


def test_the_render_warns_against_deleting_the_declaration(items):
    text = propose_contract(items, "order_items").to_text()
    assert "Do not delete the unresolved entries" in text


def test_an_answered_proposal_renders_the_json_and_no_questions(items):
    p = propose_contract(
        items, "order_items",
        grain="one row = one item on one order",
        measure_definitions={"price": "d", "freight_value": "d"},
        aggregations={"price": "none", "freight_value": "sum"},
        analysis_window=(date(2024, 1, 1), date(2024, 10, 26)),
    )
    text = p.to_text()
    assert "CANNOT BE CONFIRMED" not in text
    assert "Put these to the user:" not in text
    assert "```json" in text
    assert "confirm_dataset_contract" in text


def test_the_rendered_json_parses_back_to_the_same_contract(items):
    p = propose_contract(
        items, "order_items", grain="g",
        measure_definitions={"price": "d", "freight_value": "d"},
        aggregations={"price": "none", "freight_value": "sum"},
        analysis_window=(date(2024, 1, 1), date(2024, 10, 26)),
    )
    body = p.to_text().split("```json")[1].split("```")[0]
    assert contract_from_json(body) == p.contract


def test_the_render_carries_the_evidence_notes(items):
    text = propose_contract(items, "order_items").to_text()
    assert "What the data showed:" in text
    assert "Key from evidence" in text


def test_deleting_the_declaration_from_the_rendered_json_fails(items):
    """
    The whole guarantee, end to end: the render tells you not to, and the
    model makes it impossible rather than merely discouraged.
    """
    p = propose_contract(items, "order_items", grain="one row = one item",
                         analysis_window=(date(2024, 1, 1), date(2024, 6, 30)))
    payload = json.loads(p.contract.model_dump_json())
    payload["unresolved"] = []
    with pytest.raises(ValueError) as exc:
        contract_from_json(json.dumps(payload))
    assert reason_of(str(exc.value)) is Reason.CONTRACT_INVALID


# --------------------------------------------------------------------------
# no key found: say how close the nearest one is (Cleanup Step 8)

@pytest.fixture
def copied(con):
    """clean_sales with three of its rows copied whole: 503 rows, 500 order_ids. The bunty_babli
    run's shape -- a key that fails only because some rows are exact copies."""
    from pathlib import Path
    csv = Path(__file__).resolve().parent / "fixtures" / "clean_sales.csv"
    con.execute(f"CREATE TABLE orders AS SELECT * FROM read_csv('{csv}')")
    con.execute("INSERT INTO orders SELECT * FROM orders ORDER BY order_id LIMIT 3")
    return con


def test_with_no_key_the_nearest_identifier_is_reported_with_its_repeats(copied):
    """Phase 4: 'two duplicates says the data is dirty'. With no key found, that sentence was
    never produced, and the proposal said only that nothing identifies a row."""
    p = propose_contract(copied, "orders")
    assert p.contract.primary_key == []
    notes = " ".join(p.notes)
    assert "order_id does not identify a row: 500 distinct value(s) across 503" in notes


def test_with_no_key_exact_copies_are_counted_and_the_cleaning_step_named(copied):
    notes = " ".join(propose_contract(copied, "orders").notes)
    assert "3 row(s) of orders are exact copies of another row" in notes
    assert "would leave order_id unique" in notes
    assert 'propose_cleaning_plan(dataset_name="orders")' in notes


def test_repeats_that_are_not_copies_are_not_blamed_on_copies(con):
    """broken_sales: 186 rows, 181 order_ids, no row an exact copy (P7-D12). Removing copies
    fixes nothing there, and the note must not say it would."""
    from pathlib import Path
    csv = Path(__file__).resolve().parent / "fixtures" / "broken_sales.csv"
    con.execute(f"CREATE TABLE broken AS SELECT * FROM read_csv('{csv}')")
    notes = " ".join(propose_contract(con, "broken").notes)
    assert "order_id does not identify a row" in notes
    assert "exact copies" not in notes


def test_a_grain_that_names_a_column_is_checked_when_no_key_is_stated(copied):
    """The bunty_babli contract: grain 'grain: [order_id]', primary_key []. The person meant a
    key and nothing checked it."""
    p = propose_contract(copied, "orders", grain="grain: [order_id]")
    notes = " ".join(p.notes)
    assert "The grain names order_id, but no primary key is stated" in notes
    assert 'primary_key=["order_id"]' in notes
    assert "500 distinct value(s) across 503" in notes


def test_a_grain_naming_no_column_adds_nothing(copied):
    notes = " ".join(propose_contract(copied, "orders", grain="one row = one sale").notes)
    assert "The grain names" not in notes


def test_the_grain_question_does_not_deny_the_candidate_the_notes_name(copied):
    """Found by the live replay: 'Nearest to a key: order_id ...' in the notes, and beneath it
    'there is not even a candidate to correct'. A claim and the thing it describes, edited
    separately."""
    p = propose_contract(copied, "orders")
    asked = " ".join(p.contract.questions)
    assert "not even a candidate" not in asked
    assert "order_id" in asked
