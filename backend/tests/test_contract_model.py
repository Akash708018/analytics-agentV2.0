"""
Unit tests for contract/dataset_contract.py and contract/refusals.py.

No database and no files: this layer is a model and its rules. Everything it
knows about a table arrives as a `Binding`, which is the point -- validating a
contract must not require the table to be loaded, or a contract could never be
read back out of storage on a machine that no longer has it.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from backend.engine.contract.dataset_contract import (
    AnalysisWindow,
    Binding,
    ColumnBinding,
    DatasetContract,
    Exclusion,
    Measure,
    contract_from_json,
)
from backend.engine.contract.refusals import Reason, Refusal, reason_of

COLUMNS = [
    ("order_id", "VARCHAR"),
    ("order_item_id", "INTEGER"),
    ("shipping_limit_date", "TIMESTAMP"),
    ("price", "DECIMAL(10,2)"),
    ("freight_value", "DECIMAL(10,2)"),
    ("seller_id", "VARCHAR"),
]


def binding(columns=COLUMNS, row_count=112_650) -> Binding:
    return Binding.from_pairs(columns, row_count, datetime(2026, 8, 31, 9, 0))


def settled(**overrides) -> DatasetContract:
    """A contract with nothing outstanding."""
    kwargs = dict(
        dataset_name="order_items",
        grain="one row = one item on one order",
        primary_key=["order_id", "order_item_id"],
        date_column="shipping_limit_date",
        analysis_window=AnalysisWindow(start=date(2016, 9, 1), end=date(2018, 8, 31)),
        measures=[
            Measure(name="price", agg="sum", definition="item price, excludes freight",
                    unit="BRL"),
        ],
        dimensions=["seller_id"],
        bound_to=binding(),
    )
    kwargs.update(overrides)
    return DatasetContract(**kwargs)


# --------------------------------------------------------------------------
# refusals -- the machine-readable half
# --------------------------------------------------------------------------

def test_a_refusal_carries_prose_and_a_code():
    text = Refusal(
        reason=Reason.NO_CONTRACT,
        what="run_analysis requires a confirmed contract for 'sales'.",
        why="none exists.",
        next_call='propose_dataset_contract(dataset_name="sales")',
        state="loaded (51,290 rows), no contract",
    ).to_text()

    assert text.startswith("BLOCKED:")
    assert "NEXT STEP:" in text
    assert "CURRENT STATE:" in text
    assert text.splitlines()[-1] == "reason: NO_CONTRACT"


def test_the_code_is_parsed_back_out():
    text = Refusal(
        reason=Reason.CONTRACT_STALE,
        what="x", why="y", next_call="f(a=1)",
    ).to_text()
    assert reason_of(text) is Reason.CONTRACT_STALE


def test_a_successful_result_has_no_reason():
    """None is the assertion that a call was NOT refused."""
    assert reason_of("Loaded sales: 500 rows, 8 columns.") is None
    assert reason_of("") is None


def test_an_unknown_code_does_not_crash_the_parser():
    assert reason_of("BLOCKED: x\n\nreason: SOMETHING_ELSE") is None


def test_a_next_step_that_is_not_a_call_is_refused():
    """
    'Confirm the contract first' is an intention. The agent cannot execute an
    intention, and F1 is what happens when it tries.
    """
    with pytest.raises(ValueError) as exc:
        Refusal(reason=Reason.NO_CONTRACT, what="x", why="y",
                next_call="confirm the contract first")
    assert "parentheses" in str(exc.value)


def test_outstanding_questions_are_listed():
    text = Refusal(
        reason=Reason.CONTRACT_PROVISIONAL, what="x", why="y",
        next_call="f(a=1)", outstanding=["What is one row?"],
    ).to_text()
    assert "OUTSTANDING:" in text
    assert "  - What is one row?" in text


# --------------------------------------------------------------------------
# the D2 rule -- blanks are legal only when declared
# --------------------------------------------------------------------------

def test_a_settled_contract_is_confirmable():
    c = settled()
    assert c.is_confirmable
    assert c.unresolved == []


def test_a_blank_grain_must_be_declared_unresolved():
    with pytest.raises(ValidationError) as exc:
        settled(grain="")
    assert "not listed in unresolved" in str(exc.value)


def test_a_blank_grain_is_fine_once_declared():
    c = settled(grain="", unresolved=["grain"], questions=["What is one row?"])
    assert not c.is_confirmable


def test_a_measure_without_a_definition_must_be_declared():
    with pytest.raises(ValidationError) as exc:
        settled(measures=[Measure(name="price", agg="sum")])
    assert "measures[price].definition" in str(exc.value)


def test_a_measure_without_an_aggregation_must_be_declared():
    """
    No default, matching every production semantic layer: LookML requires
    `type:`, Cube requires `type`, dbt MetricFlow requires `agg`. The guess
    that gets guessed is sum, and summing a unit price is meaningless in a way
    nothing downstream can detect.
    """
    with pytest.raises(ValidationError) as exc:
        settled(measures=[Measure(name="price", definition="item price")])
    assert "measures[price].agg" in str(exc.value)
    assert "not a default" in str(exc.value)


def test_an_unstated_aggregation_is_fine_once_declared():
    c = settled(
        measures=[Measure(name="price", definition="item price")],
        unresolved=["measures[price].agg"],
    )
    assert not c.is_confirmable
    assert c.measure("price").agg_path == "measures[price].agg"


def test_non_additive_is_a_statement_not_an_absence():
    """
    'none' means a person decided the column must not be combined. It is a
    different thing from nobody having said, and only one of them confirms.
    """
    c = settled(measures=[Measure(name="price", agg="none", definition="d")])
    assert c.is_confirmable
    assert c.measure("price").agg == "none"


def test_an_unstated_aggregation_renders_as_not_stated():
    text = settled(
        measures=[Measure(name="price", definition="d")],
        unresolved=["measures[price].agg"],
    ).to_text()
    assert "| price | (NOT STATED) | - | d |" in text


def test_a_measure_without_a_definition_is_fine_once_declared():
    c = settled(
        measures=[Measure(name="price", agg="sum")],
        unresolved=["measures[price].definition"],
    )
    assert not c.is_confirmable
    assert c.measure("price").definition_path == "measures[price].definition"


def test_deleting_the_unresolved_entry_does_not_confirm_a_guess():
    """
    The Phase 3 failure mode, one layer up: a spec could be confirmed by
    hand-deleting the field that blocked it. Here the blank itself is invalid,
    so removing the declaration fails instead of succeeding quietly.
    """
    provisional = settled(
        measures=[Measure(name="price", agg="sum")],
        unresolved=["measures[price].definition"],
    )
    payload = json.loads(provisional.model_dump_json())
    payload["unresolved"] = []
    with pytest.raises(ValueError) as exc:
        contract_from_json(json.dumps(payload))
    assert reason_of(str(exc.value)) is Reason.CONTRACT_INVALID


def test_the_blocking_refusal_names_the_next_call_and_the_code():
    c = settled(grain="", unresolved=["grain"], questions=["What is one row?"])
    text = c.blocking_message()
    assert reason_of(text) is Reason.CONTRACT_PROVISIONAL
    assert "propose_dataset_contract" in text
    assert "What is one row?" in text


# --------------------------------------------------------------------------
# columns that do not exist
# --------------------------------------------------------------------------

def test_a_measure_naming_a_missing_column_is_refused():
    with pytest.raises(ValidationError) as exc:
        settled(
            measures=[Measure(name="revenue", agg="sum", definition="x")],
        )
    assert "revenue (measures)" in str(exc.value)


def test_a_primary_key_naming_a_missing_column_is_refused():
    with pytest.raises(ValidationError) as exc:
        settled(primary_key=["order_id", "line_no"])
    assert "line_no (primary_key)" in str(exc.value)


def test_the_error_lists_what_is_actually_there():
    with pytest.raises(ValidationError) as exc:
        settled(dimensions=["nope"])
    assert "freight_value" in str(exc.value)


def test_an_unbound_contract_skips_the_column_checks():
    """
    A contract read back from storage on a machine that never loaded the table
    still has to parse. Binding is what makes column checks possible, not what
    makes a contract valid.
    """
    c = DatasetContract(
        dataset_name="sales",
        grain="one row = one order",
        measures=[Measure(name="anything", agg="sum", definition="d")],
    )
    assert c.bound_to is None
    assert c.fingerprint is None
    assert c.is_confirmable


def test_a_text_date_column_is_refused():
    """
    Phase 3's asymmetry: the CSV path reads inside an ISO string and yields
    DATE, the Excel path does not and yields VARCHAR. A window over text sorts
    lexically, so '2024-10-01' lands before '2024-9-01'.
    """
    cols = [("order_id", "VARCHAR"), ("order_date", "VARCHAR")]
    with pytest.raises(ValidationError) as exc:
        DatasetContract(
            dataset_name="sales",
            grain="one row = one order",
            date_column="order_date",
            bound_to=Binding.from_pairs(cols, 10),
        )
    msg = str(exc.value)
    assert "not a date" in msg
    assert "'order_date': 'DATE'" in msg


def test_a_real_date_column_passes():
    cols = [("order_id", "VARCHAR"), ("order_date", "DATE")]
    c = DatasetContract(
        dataset_name="sales", grain="one row = one order",
        date_column="order_date", bound_to=Binding.from_pairs(cols, 10),
    )
    assert c.date_column == "order_date"


# --------------------------------------------------------------------------
# internal consistency
# --------------------------------------------------------------------------

def test_a_column_cannot_be_both_measure_and_dimension():
    with pytest.raises(ValidationError) as exc:
        settled(dimensions=["seller_id", "price"])
    assert "both a measure and a dimension" in str(exc.value)


def test_measure_names_must_be_unique():
    with pytest.raises(ValidationError) as exc:
        settled(
            measures=[
                Measure(name="price", agg="sum", definition="a"),
                Measure(name="price", agg="mean", definition="b"),
            ]
        )
    assert "must be unique" in str(exc.value)


def test_a_repeated_primary_key_column_is_refused():
    with pytest.raises(ValidationError):
        settled(primary_key=["order_id", "order_id"])


def test_a_window_without_a_date_column_is_refused():
    with pytest.raises(ValidationError) as exc:
        settled(date_column=None)
    assert "which column the window applies to" in str(exc.value)


def test_a_window_that_ends_before_it_starts_is_refused():
    with pytest.raises(ValidationError):
        AnalysisWindow(start=date(2024, 12, 31), end=date(2024, 1, 1))


def test_an_exclusion_needs_a_reason():
    with pytest.raises(ValidationError) as exc:
        Exclusion(rule="status = 'cancelled'", reason="  ")
    assert "defend later" in str(exc.value)


def test_an_unknown_aggregation_is_refused():
    with pytest.raises(ValidationError):
        Measure(name="price", agg="average", definition="d")


def test_a_bad_dataset_name_is_refused():
    with pytest.raises(ValidationError) as exc:
        settled(dataset_name="2024 sales")
    assert "letters, digits and underscores" in str(exc.value)


def test_columns_used_is_deduped_and_ordered():
    assert settled().columns_used() == [
        "order_id", "order_item_id", "shipping_limit_date", "price", "seller_id",
    ]


# --------------------------------------------------------------------------
# binding and fingerprint -- D1
# --------------------------------------------------------------------------

def test_the_fingerprint_covers_structure_only():
    """More rows is the same table. That is the whole D1 decision."""
    assert binding(row_count=100).fingerprint == binding(row_count=999_999).fingerprint


def test_a_dropped_column_changes_the_fingerprint():
    assert binding().fingerprint != binding(columns=COLUMNS[:-1]).fingerprint


def test_a_retyped_column_changes_the_fingerprint():
    retyped = [(n, "VARCHAR" if n == "price" else t) for n, t in COLUMNS]
    assert binding().fingerprint != binding(columns=retyped).fingerprint


def test_reordered_columns_change_the_fingerprint():
    """
    Position is part of the structure. A reorder is harmless to a named
    SELECT and is not harmless to a spec that carries `names` positionally,
    which is exactly what Phase 3 hands the loaders.
    """
    swapped = [COLUMNS[1], COLUMNS[0]] + COLUMNS[2:]
    assert binding().fingerprint != binding(columns=swapped).fingerprint


def test_the_fingerprint_is_short_and_stable():
    fp = binding().fingerprint
    assert len(fp) == 12
    assert fp == binding().fingerprint


def test_case_of_a_type_does_not_change_the_fingerprint():
    lowered = [(n, t.lower()) for n, t in COLUMNS]
    assert binding().fingerprint == binding(columns=lowered).fingerprint


def test_a_binding_needs_at_least_one_column():
    with pytest.raises(ValidationError):
        Binding(columns=[], row_count=0)


# --------------------------------------------------------------------------
# the JSON round trip
# --------------------------------------------------------------------------

def test_a_contract_survives_the_round_trip():
    c = settled()
    assert contract_from_json(c.model_dump_json()) == c


def test_a_fenced_block_parses():
    c = settled()
    assert contract_from_json("```json\n" + c.model_dump_json() + "\n```") == c


def test_an_edit_is_honoured_as_written():
    payload = json.loads(settled().model_dump_json())
    payload["caveats"] = ["November 2017 has no data -- source feed outage"]
    assert contract_from_json(json.dumps(payload)).caveats[0].startswith("November")


def test_nonsense_json_is_refused_with_a_code():
    with pytest.raises(ValueError) as exc:
        contract_from_json("{not json")
    text = str(exc.value)
    assert reason_of(text) is Reason.CONTRACT_INVALID
    assert "NEXT STEP" in text


# --------------------------------------------------------------------------
# rendering and export
# --------------------------------------------------------------------------

def test_to_text_reads_as_a_contract():
    text = settled().to_text()
    assert "grain          one row = one item on one order" in text
    assert "primary key    order_id + order_item_id" in text
    assert "| price | sum | BRL | item price, excludes freight |" in text
    assert "fingerprint" in text


def test_to_text_marks_a_provisional_contract():
    text = settled(grain="", unresolved=["grain"]).to_text()
    assert "PROVISIONAL" in text
    assert "(not stated)" in text


def test_an_undefined_measure_is_visibly_undefined():
    text = settled(
        measures=[Measure(name="price", agg="sum")],
        unresolved=["measures[price].definition"],
    ).to_text()
    assert "(NOT STATED)" in text


def test_the_yaml_export_carries_the_definitions():
    c = settled(version=3, confirmed_at=datetime(2026, 8, 31, 10, 30))
    out = c.to_yaml_dict()
    assert out["version"] == 3
    assert out["fingerprint"] == c.fingerprint
    assert out["measures"][0]["definition"] == "item price, excludes freight"
    assert out["analysis_window"] == {"start": "2016-09-01", "end": "2018-08-31"}


def test_the_yaml_export_is_json_serialisable():
    """It is written to disk on confirm, so nothing in it may be a date object."""
    json.dumps(settled().to_yaml_dict())
