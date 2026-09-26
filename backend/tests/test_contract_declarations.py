"""foreign_keys and domains on the contract, and what they refuse.

Phase 7, Step 11. Run from the repo root:

    uv run pytest tests/test_contract_declarations.py -q

dbt ships four generic tests -- unique, not_null, accepted_values,
relationships -- declared in schema.yml beside the model rather than in the
call that runs them. These are the two of those four this project did not have
a home for. They live on the contract for the same reason every other
definition does: a number is traceable to an agreement, and an agreement that
lives in a call argument is not versioned, not exported, and not in the report.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.contract.dataset_contract import (  # noqa: E402
    AnalysisWindow, Binding, DatasetContract, ForeignKey, Measure)
from backend.engine.contract.store import changed_fields  # noqa: E402

PAIRS = [("order_id", "VARCHAR"), ("region", "VARCHAR"),
         ("channel", "VARCHAR"), ("order_ts", "TIMESTAMP"), ("units", "BIGINT")]


def contract(**kw) -> DatasetContract:
    base = dict(
        dataset_name="sales",
        grain="one row = one order",
        primary_key=["order_id"],
        date_column="order_ts",
        analysis_window=AnalysisWindow(start=date(2024, 1, 1),
                                       end=date(2024, 12, 31)),
        measures=[Measure(name="units", agg="sum",
                          definition="how many were sold", unit="ea")],
        bound_to=Binding.from_pairs(PAIRS, 186),
    )
    base.update(kw)
    return DatasetContract(**base)


# --------------------------------------------------------------------------
# unset is a default, not a gap
# --------------------------------------------------------------------------


def test_both_default_to_empty_and_neither_is_unresolved():
    """The missing_values argument, applied twice more.

    A field that counted as a gap would make every contract in the workspace
    unconfirmable the day it was added, and most datasets reference nothing
    and constrain nothing.
    """
    c = contract()
    assert c.foreign_keys == []
    assert c.domains == {}
    assert c.unresolved == []
    assert c.is_confirmable


def test_declaring_them_does_not_make_a_contract_unconfirmable():
    c = contract(
        foreign_keys=[ForeignKey(columns=["region"], references="region_lookup")],
        domains={"channel": ["Online", "Retail", "Wholesale"]},
    )
    assert c.is_confirmable


# --------------------------------------------------------------------------
# what they refuse
# --------------------------------------------------------------------------


def test_a_foreign_key_naming_a_column_the_table_lacks_is_refused():
    """Checkable because bound_to is set -- the same condition date_column's
    type check runs under."""
    with pytest.raises(ValueError, match="which sales does not have"):
        contract(foreign_keys=[ForeignKey(columns=["nope"], references="x")])


def test_a_domain_on_a_column_the_table_lacks_is_refused():
    with pytest.raises(ValueError, match="a domain is declared for nope"):
        contract(domains={"nope": ["a", "b"]})


def test_an_unbound_contract_declares_without_being_checked():
    """A contract has to be readable on a machine that never loaded the table,
    so an unbound one says nothing about whether its columns exist."""
    c = DatasetContract(
        dataset_name="sales",
        grain="one row = one order",
        foreign_keys=[ForeignKey(columns=["anything"], references="x")],
        domains={"whatever": ["a"]},
        unresolved=["primary_key", "date_column", "measures"],
    )
    assert c.foreign_keys[0].columns == ["anything"]


def test_the_two_sides_must_be_the_same_length():
    with pytest.raises(ValueError, match="joins them in pairs"):
        ForeignKey(columns=["a", "b"], references="x", referenced_columns=["a"])


def test_a_foreign_key_needs_a_dataset_to_reference():
    with pytest.raises(ValueError, match="the dataset it references"):
        ForeignKey(columns=["a"], references="  ")


def test_a_foreign_key_needs_a_column():
    with pytest.raises(ValueError, match="at least one column"):
        ForeignKey(columns=[], references="x")


# --------------------------------------------------------------------------
# the conveniences
# --------------------------------------------------------------------------


def test_the_referenced_columns_default_to_the_same_names():
    """The common case and the one dbt writes. Filled in here rather than left
    for the checker to guess at."""
    fk = ForeignKey(columns=["region"], references="region_lookup")
    assert fk.referenced_columns == ["region"]
    assert fk.label() == "region -> region_lookup.region"


def test_a_composite_foreign_key_labels_both_sides():
    fk = ForeignKey(columns=["order_id", "line_no"], references="lines",
                    referenced_columns=["oid", "ln"])
    assert fk.label() == "order_id + line_no -> lines.oid + ln"


# --------------------------------------------------------------------------
# they travel with the contract
# --------------------------------------------------------------------------


def test_both_reach_the_version_controlled_export():
    """Definitions belong in version control. A declaration that never leaves
    the database cannot be reviewed in a pull request."""
    c = contract(
        foreign_keys=[ForeignKey(columns=["region"], references="region_lookup",
                                 reason="region is a controlled vocabulary")],
        domains={"channel": ["Online", "Retail", "Wholesale"]},
    )
    exported = c.to_yaml_dict()
    assert exported["foreign_keys"] == [{
        "columns": ["region"],
        "references": "region_lookup",
        "referenced_columns": ["region"],
        "reason": "region is a controlled vocabulary",
    }]
    assert exported["domains"] == {"channel": ["Online", "Retail", "Wholesale"]}


def test_both_survive_a_round_trip_through_json():
    """The contract goes out as JSON and comes back as JSON, so a declaration
    that does not survive the trip is a declaration nobody can confirm."""
    from backend.engine.contract.dataset_contract import contract_from_json
    c = contract(
        foreign_keys=[ForeignKey(columns=["region"], references="region_lookup")],
        domains={"channel": ["Online", "Retail"]},
    )
    back = contract_from_json(c.model_dump_json())
    assert back.foreign_keys[0].label() == "region -> region_lookup.region"
    assert back.domains == {"channel": ["Online", "Retail"]}


def test_a_changed_declaration_is_named_in_the_version_history():
    """changed_fields exists because a history recording THAT something changed
    without recording WHAT is a history nobody consults twice."""
    before = contract()
    after = contract(
        foreign_keys=[ForeignKey(columns=["region"], references="region_lookup")],
        domains={"channel": ["Online"]},
    )
    changed = changed_fields(after, before)
    assert "foreign_keys" in changed
    assert "domains" in changed
