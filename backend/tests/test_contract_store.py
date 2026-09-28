"""
Unit tests for contract/store.py.

These use a bare in-memory duckdb.connect(), like the rest of the contract
layer. The store creates its own table lazily, so nothing here needs
db.connect() and its `_agent_datasets` -- which is deliberate: the ingest
layer should not have to know that contracts exist.

Timestamps are injected rather than slept for. An SCD2 span is only checkable
if the clock is an argument.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pytest
import yaml

from backend.engine.contract import ContractRefused
from backend.engine.contract import store
from backend.engine.contract.dataset_contract import (
    AnalysisWindow,
    Binding,
    DatasetContract,
    Measure,
)
from backend.engine.contract.refusals import Reason, reason_of

T1 = datetime(2026, 8, 31, 9, 0)
T2 = datetime(2026, 9, 1, 14, 30)
T3 = datetime(2026, 9, 2, 11, 15)

COLUMNS = [
    ("order_id", "VARCHAR"),
    ("order_item_id", "INTEGER"),
    ("order_date", "DATE"),
    ("price", "DECIMAL(10,2)"),
    ("seller_id", "VARCHAR"),
]


@pytest.fixture
def con():
    c = duckdb.connect()
    try:
        yield c
    finally:
        c.close()


def contract(**overrides) -> DatasetContract:
    kwargs = dict(
        dataset_name="order_items",
        grain="one row = one item on one order",
        primary_key=["order_id", "order_item_id"],
        date_column="order_date",
        analysis_window=AnalysisWindow(start=date(2024, 1, 1), end=date(2024, 9, 30)),
        measures=[
            Measure(name="price", agg="sum",
                    definition="item price, excludes freight", unit="BRL")
        ],
        dimensions=["seller_id"],
        bound_to=Binding.from_pairs(COLUMNS, 300, T1),
    )
    kwargs.update(overrides)
    return DatasetContract(**kwargs)


# --------------------------------------------------------------------------
# storing
# --------------------------------------------------------------------------

def test_the_first_confirmation_is_version_one(con):
    s = store.confirm(con, contract(), now=T1)
    assert s.version == 1
    assert s.is_current
    assert s.valid_from == T1
    assert s.valid_to is None
    assert s.confirmed_at == T1


def test_the_stored_contract_carries_its_version(con):
    """The version is what a later 'what did we agree' question is asked about."""
    s = store.confirm(con, contract(), now=T1)
    assert s.contract.version == 1
    assert s.contract.confirmed_at == T1


def test_current_reads_it_back_intact(con):
    store.confirm(con, contract(), now=T1)
    live = store.current(con, "order_items")
    assert live.contract.grain == "one row = one item on one order"
    assert live.contract.measure("price").definition == "item price, excludes freight"
    assert live.contract.analysis_window.end == date(2024, 9, 30)
    assert live.fingerprint == contract().fingerprint


def test_current_is_none_before_anything_is_confirmed(con):
    assert store.current(con, "order_items") is None


def test_current_is_none_for_a_different_dataset(con):
    store.confirm(con, contract(), now=T1)
    assert store.current(con, "customers") is None


def test_the_table_is_created_on_demand(con):
    """Nothing in db.connect() has to know contracts exist."""
    assert store.current(con, "anything") is None
    names = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    assert store.CONTRACT_TABLE in names


def test_the_log_table_is_underscore_prefixed(con):
    """So evidence._user_tables and list_datasets treat it as bookkeeping."""
    assert store.CONTRACT_TABLE.startswith("_")


# --------------------------------------------------------------------------
# SCD2
# --------------------------------------------------------------------------

def test_a_change_writes_a_new_version_and_closes_the_old(con):
    store.confirm(con, contract(), now=T1)
    second = store.confirm(
        con, contract(grain="one row = one item, returns excluded"), now=T2
    )
    assert second.version == 2
    assert second.is_current

    rows = store.history(con, "order_items")
    assert [r.version for r in rows] == [2, 1]
    old = rows[1]
    assert not old.is_current
    assert old.valid_to == T2
    assert old.valid_from == T1


def test_the_old_version_is_kept_not_overwritten(con):
    store.confirm(con, contract(), now=T1)
    store.confirm(con, contract(grain="something else entirely"), now=T2)
    grains = [r.contract.grain for r in store.history(con, "order_items")]
    assert grains == ["something else entirely", "one row = one item on one order"]


def test_spans_are_contiguous_across_versions(con):
    store.confirm(con, contract(), now=T1)
    store.confirm(con, contract(grain="second"), now=T2)
    store.confirm(con, contract(grain="third"), now=T3)
    rows = sorted(store.history(con, "order_items"), key=lambda r: r.version)
    assert [r.valid_from for r in rows] == [T1, T2, T3]
    assert [r.valid_to for r in rows] == [T2, T3, None]
    assert [r.is_current for r in rows] == [False, False, True]


def test_exactly_one_version_is_current(con):
    for i, t in enumerate((T1, T2, T3)):
        store.confirm(con, contract(grain=f"v{i}"), now=t)
    live = con.execute(
        f"SELECT count(*) FROM {store.CONTRACT_TABLE} "
        f"WHERE dataset_name='order_items' AND is_current"
    ).fetchone()[0]
    assert live == 1


def test_confirming_the_same_contract_twice_does_not_version(con):
    """
    Versions are for changes. A history where half the entries are
    re-confirmations of an unchanged document is a history nobody reads.
    """
    first = store.confirm(con, contract(), now=T1)
    again = store.confirm(con, contract(), now=T2)
    assert again.version == first.version == 1
    assert again.valid_from == T1
    assert len(store.history(con, "order_items")) == 1


def test_a_changed_definition_does_count_as_a_change(con):
    store.confirm(con, contract(), now=T1)
    changed = contract(
        measures=[Measure(name="price", agg="sum",
                          definition="item price, INCLUDES freight", unit="BRL")]
    )
    assert store.confirm(con, changed, now=T2).version == 2


def test_a_rebinding_counts_as_a_change(con):
    """More rows is not a new agreement, but it is a new binding."""
    store.confirm(con, contract(), now=T1)
    rebound = contract(bound_to=Binding.from_pairs(COLUMNS, 4_000, T2))
    second = store.confirm(con, rebound, now=T2)
    assert second.version == 2
    assert second.row_count == 4_000


def test_datasets_do_not_share_a_version_counter(con):
    store.confirm(con, contract(), now=T1)
    store.confirm(con, contract(), now=T1)
    other = store.confirm(con, contract(dataset_name="customers"), now=T2)
    assert other.version == 1


def test_all_current_covers_every_dataset(con):
    store.confirm(con, contract(), now=T1)
    store.confirm(con, contract(dataset_name="customers"), now=T1)
    store.confirm(con, contract(grain="revised"), now=T2)
    live = store.all_current(con)
    assert [s.dataset_name for s in live] == ["customers", "order_items"]
    assert {s.version for s in live} == {1, 2}


def test_history_of_an_unknown_dataset_is_empty(con):
    assert store.history(con, "nothing") == []


# --------------------------------------------------------------------------
# what will not be stored
# --------------------------------------------------------------------------

def test_a_provisional_contract_is_refused(con):
    provisional = contract(
        grain="", unresolved=["grain"], questions=["What is one row?"]
    )
    with pytest.raises(ContractRefused) as exc:
        store.confirm(con, provisional, now=T1)
    text = str(exc.value)
    assert reason_of(text) is Reason.CONTRACT_PROVISIONAL
    assert store.current(con, "order_items") is None


def test_an_unbound_contract_is_refused(con):
    """
    A contract may EXIST unbound -- it has to be readable back on a machine
    that never loaded the table. It may not be CONFIRMED unbound, because
    nothing could then tell whether the agreement still holds.
    """
    with pytest.raises(ContractRefused) as exc:
        store.confirm(con, contract(bound_to=None), now=T1)
    text = str(exc.value)
    assert reason_of(text) is Reason.CONTRACT_INVALID
    assert "still holds" in text


def test_a_refused_confirmation_leaves_the_previous_one_in_force(con):
    store.confirm(con, contract(), now=T1)
    with pytest.raises(ContractRefused):
        store.confirm(con, contract(grain="", unresolved=["grain"]), now=T2)
    live = store.current(con, "order_items")
    assert live.version == 1
    assert live.is_current
    assert live.valid_to is None


def test_changed_fields_names_what_moved(con):
    """
    A log that records that something changed without recording what is a log
    nobody consults twice. Two versions here share a grain and a fingerprint,
    so the rendered lines are identical apart from timestamps -- the only
    difference is one measure definition, and it has to be named.
    """
    old = contract()
    new = contract(
        measures=[Measure(name="price", agg="sum",
                          definition="item price, INCLUDES freight", unit="BRL")]
    )
    assert store.changed_fields(new, old) == ["measures[price].definition"]


def test_changed_fields_catches_a_measure_appearing_or_going(con):
    old = contract()
    two = contract(
        measures=[
            Measure(name="price", agg="sum",
                    definition="item price, excludes freight", unit="BRL"),
            Measure(name="order_item_id", agg="count", definition="lines per order"),
        ]
    )
    assert store.changed_fields(two, old) == ["measures[order_item_id] (added)"]
    assert store.changed_fields(old, two) == ["measures[order_item_id] (removed)"]


def test_changed_fields_ignores_version_and_timestamp(con):
    first = store.confirm(con, contract(), now=T1).contract
    assert store.changed_fields(first, contract()) == []


def test_changed_fields_reports_a_narrowed_window(con):
    narrowed = contract(
        analysis_window=AnalysisWindow(start=date(2024, 1, 1), end=date(2024, 6, 30))
    )
    assert store.changed_fields(narrowed, contract()) == ["analysis_window"]


def test_history_text_says_what_each_version_changed(con):
    store.confirm(con, contract(), now=T1)
    store.confirm(
        con,
        contract(measures=[Measure(name="price", agg="sum",
                                   definition="item price, INCLUDES freight",
                                   unit="BRL")]),
        now=T2,
    )
    text = store.history_text(con, "order_items")
    assert "order_items: 2 version(s)" in text
    assert "changed: measures[price].definition" in text
    assert text.count("changed:") == 1  # the first version changed nothing


def test_history_text_on_a_dataset_with_no_contract(con):
    assert "No contract has ever been confirmed" in store.history_text(con, "nope")


# --------------------------------------------------------------------------
# the export
# --------------------------------------------------------------------------

def test_the_export_is_written_where_git_can_see_it(tmp_path):
    path = store.write_export(contract(), root=tmp_path)
    assert path == tmp_path / "order_items.yaml"
    assert path.exists()


def test_the_export_carries_the_definitions(tmp_path):
    path = store.write_export(contract(), root=tmp_path)
    loaded = yaml.safe_load(path.read_text())
    assert loaded["grain"] == "one row = one item on one order"
    assert loaded["measures"][0]["definition"] == "item price, excludes freight"
    assert loaded["analysis_window"] == {"start": "2024-01-01", "end": "2024-09-30"}
    assert loaded["primary_key"] == ["order_id", "order_item_id"]


def test_the_export_says_it_is_generated(tmp_path):
    text = store.write_export(contract(), root=tmp_path).read_text()
    assert text.startswith("# GENERATED BY confirm_dataset_contract -- DO NOT EDIT.")
    assert "authoritative copy lives in the workspace database" in text


def test_the_export_records_the_version_it_came_from(con, tmp_path):
    stored = store.confirm(con, contract(), now=T1)
    loaded = yaml.safe_load(store.write_export(stored.contract, root=tmp_path).read_text())
    assert loaded["version"] == 1
    assert loaded["confirmed_at"] == T1.isoformat()
    assert loaded["fingerprint"] == stored.fingerprint


def test_the_export_is_rewritten_not_appended(con, tmp_path):
    store.write_export(contract(), root=tmp_path)
    store.write_export(contract(grain="revised"), root=tmp_path)
    loaded = yaml.safe_load(store.export_path("order_items", tmp_path).read_text())
    assert loaded["grain"] == "revised"


def test_the_export_directory_is_created(tmp_path):
    root = tmp_path / "docs" / "contracts"
    assert not root.exists()
    store.write_export(contract(), root=root)
    assert root.is_dir()


# --------------------------------------------------------------------------
# what the user is told
# --------------------------------------------------------------------------

def test_the_summary_names_the_version_and_the_binding(con):
    text = store.summarise(store.confirm(con, contract(), now=T1))
    assert "version 1" in text
    assert "300 rows" in text
    assert "more rows will be noted rather than blocking" in text


def test_the_summary_mentions_a_supersession_only_when_there_is_one(con):
    first = store.summarise(store.confirm(con, contract(), now=T1))
    assert "superseded" not in first

    second = store.summarise(
        store.confirm(con, contract(grain="revised"), now=T2)
    )
    assert "Version 1 was superseded, not replaced" in second


def test_the_summary_names_the_export_when_one_was_written(con, tmp_path):
    stored = store.confirm(con, contract(), now=T1)
    path = store.write_export(stored.contract, root=tmp_path)
    assert str(path) in store.summarise(stored, export=path)


def test_a_stored_line_reads_as_history(con):
    store.confirm(con, contract(), now=T1)
    store.confirm(con, contract(grain="revised"), now=T2)
    lines = [s.line() for s in store.history(con, "order_items")]
    assert "v2" in lines[0] and "-> now" in lines[0]
    assert "v1" in lines[1] and "2026-09-01 14:30" in lines[1]
