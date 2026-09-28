"""
Unit tests for state.py -- the gate, and the workflow report.

These are the first contract-layer tests that need `db.connect(WORKSPACE)`
rather than a bare duckdb connection. Everything up to now read a table and
wrote nothing; the gate reads `_agent_datasets` for load timestamps, which is
a table only db.connect() creates. A bare connection here fails on the
catalog, which is the same trap tests/test_loaders_step5.py documents.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backend.engine import workspace
from backend.engine.contract import ContractRefused
from backend.engine.contract import store
from backend.engine.contract.dataset_contract import (
    AnalysisWindow,
    Exclusion,
    Measure,
)
from backend.engine.contract.propose import propose_contract
from backend.engine.contract.refusals import Reason, reason_of
from backend.engine.state import (
    Gate,
    dataset_states,
    describe_workflow_state,
    require_contract,
)
from backend.engine.util import db

WORKSPACE = "state_test"
T1 = datetime(2026, 8, 31, 9, 0)


@pytest.fixture
def con():
    """
    Its own workspace, reset on the way in AND on the way out.

    db.connect is file-backed, so without the reset every table, every
    registration and every contract survives into the next test: the second
    confirmation of a contract becomes version 3, then 4, and a report that
    should list one dataset lists four. The pattern is copied from
    tests/test_loaders_step5.py, which documents the same trap.
    """
    workspace.reset(WORKSPACE)
    c = db.connect(WORKSPACE)
    try:
        yield c
    finally:
        c.close()
        workspace.reset(WORKSPACE)


def _load_items(con, name="order_items", rows=300):
    con.execute(
        f"""
        CREATE OR REPLACE TABLE "{name}" AS
        SELECT
          'ORD-' || lpad(((i // 3) + 1)::VARCHAR, 6, '0') AS order_id,
          ((i % 3) + 1)::INTEGER                          AS order_item_id,
          'SELL-' || ((i % 40) + 1)::VARCHAR              AS seller_id,
          (DATE '2024-01-01' + ((i % 300)::INTEGER))      AS order_date,
          (((i % 97) + 1) * 1.5)                          AS price
        FROM range({rows}) t(i)
        """
    )
    shape = db.table_shape(con, name)
    db.register_dataset(
        con, dataset_name=name, source_type="csv",
        source_detail=f"/tmp/{name}.csv", row_count=shape[0],
        column_count=shape[1],
    )
    return con


def _confirmed(con, name="order_items", **overrides):
    """Propose, answer everything, confirm."""
    kwargs = dict(
        grain="one row = one item on one order",
        measure_definitions={"price": "item price, excludes freight"},
        aggregations={"price": "none"},
        analysis_window=(date(2024, 1, 1), date(2024, 10, 26)),
    )
    kwargs.update(overrides)
    p = propose_contract(con, name, **kwargs)
    return store.confirm(con, p.contract, now=T1)


# --------------------------------------------------------------------------
# the gate refuses
# --------------------------------------------------------------------------

def test_an_unloaded_dataset_is_refused(con):
    with pytest.raises(ContractRefused) as exc:
        require_contract(con, "nope")
    text = str(exc.value)
    assert reason_of(text) is Reason.DATASET_NOT_LOADED
    assert "propose_ingest_spec" in text


def test_a_loaded_dataset_with_no_contract_is_refused(con):
    _load_items(con)
    with pytest.raises(ContractRefused) as exc:
        require_contract(con, "order_items")
    text = str(exc.value)
    assert reason_of(text) is Reason.NO_CONTRACT
    assert "propose_dataset_contract" in text


def test_the_no_contract_refusal_reports_the_current_state(con):
    """8.2: what was blocked, the exact next call, current state."""
    _load_items(con)
    with pytest.raises(ContractRefused) as exc:
        require_contract(con, "order_items")
    text = str(exc.value)
    assert "loaded (300 rows, 5 columns), no contract" in text
    assert 'dataset_name="order_items"' in text


def test_the_no_contract_refusal_says_why_it_matters(con):
    _load_items(con)
    with pytest.raises(ContractRefused) as exc:
        require_contract(con, "order_items")
    assert "double-counts" in str(exc.value)


def test_a_stale_contract_is_refused(con):
    _load_items(con)
    _confirmed(con)
    con.execute("ALTER TABLE order_items DROP COLUMN price")
    with pytest.raises(ContractRefused) as exc:
        require_contract(con, "order_items")
    text = str(exc.value)
    assert reason_of(text) is Reason.CONTRACT_STALE
    assert "price" in text


def test_a_key_that_stopped_holding_is_refused(con):
    """
    The check the fingerprint cannot do. Same columns, same types -- so no
    drift at all -- and the key now repeats because rows were duplicated.
    """
    _load_items(con)
    _confirmed(con)
    con.execute("INSERT INTO order_items SELECT * FROM order_items LIMIT 10")
    with pytest.raises(ContractRefused) as exc:
        require_contract(con, "order_items")
    text = str(exc.value)
    assert reason_of(text) is Reason.KEY_NOT_UNIQUE
    assert "10 row(s) repeat" in text


# --------------------------------------------------------------------------
# the gate passes
# --------------------------------------------------------------------------

def test_an_unchanged_dataset_passes_cleanly(con):
    _load_items(con)
    _confirmed(con)
    gate = require_contract(con, "order_items")
    assert isinstance(gate, Gate)
    assert gate.version == 1
    assert gate.caveats == []
    assert "one row = one item on one order" in gate.header()


def test_an_unchanged_dataset_has_its_key_checked_anyway(con):
    """
    This used to read: "The fingerprint's only job. Identical structure AND
    identical row count means nothing can have moved, so the count is not run
    again." Both sentences were true and the conclusion did not follow --
    nothing had moved since a state nobody checked, because store.confirm does
    not look at the data. Phase 8 Step 3 removed the skip, so an unchanged
    dataset now carries a verdict rather than a None.
    """
    _load_items(con)
    _confirmed(con)
    gate = require_contract(con, "order_items")
    assert gate.key is not None
    assert gate.key.holds


def test_more_rows_pass_with_a_caveat(con):
    _load_items(con)
    _confirmed(con)
    con.execute(
        """INSERT INTO order_items
           SELECT 'ORD-NEW-' || i::VARCHAR, 1, 'SELL-1',
                  DATE '2024-11-01', 9.0
           FROM range(30) t(i)"""
    )
    gate = require_contract(con, "order_items")
    assert gate.key is not None and gate.key.holds
    assert any("gained 30 rows" in c for c in gate.caveats)


def test_a_new_column_passes_with_a_caveat(con):
    _load_items(con)
    _confirmed(con)
    con.execute("ALTER TABLE order_items ADD COLUMN tax DECIMAL(10,2)")
    gate = require_contract(con, "order_items")
    assert any("new column(s) tax" in c for c in gate.caveats)


def test_the_contracts_own_caveats_are_carried(con):
    _load_items(con)
    _confirmed(con, caveats=["November is partial -- the extract stopped early"])
    gate = require_contract(con, "order_items")
    assert any("November is partial" in c for c in gate.caveats)


def test_exclusions_become_caveats_on_every_result(con):
    """
    A number computed with 100 rows deliberately removed has to say so, or it
    is a different number wearing the same name.
    """
    _load_items(con)
    _confirmed(
        con,
        known_exclusions=[
            Exclusion(rule="status = 'cancelled'", reason="not real revenue",
                      row_count=100)
        ],
    )
    gate = require_contract(con, "order_items")
    assert any(
        "Excluded: status = 'cancelled' (100 rows) -- not real revenue." == c
        for c in gate.caveats
    )


def test_drift_is_reported_before_the_contracts_own_caveats(con):
    _load_items(con)
    _confirmed(con, caveats=["a caveat someone wrote"])
    con.execute("ALTER TABLE order_items ADD COLUMN tax INTEGER")
    caveats = require_contract(con, "order_items").caveats
    assert "new column(s) tax" in caveats[0]
    # Marked as the person's statement, never bare beside a measured figure (recheck, 25/09/2026).
    assert caveats[1] == "Declared in the contract, not measured: a caveat someone wrote"


def test_the_gate_uses_the_current_version(con):
    _load_items(con)
    _confirmed(con)
    _confirmed(con, grain="one row = one item, returns excluded")
    gate = require_contract(con, "order_items")
    assert gate.version == 2
    assert "returns excluded" in gate.header()


# --------------------------------------------------------------------------
# workflow state -- F13
# --------------------------------------------------------------------------

def test_an_empty_workspace_says_what_to_do(con):
    text = describe_workflow_state(con)
    assert "Nothing is loaded" in text
    assert "propose_ingest_spec" in text


def test_every_loaded_dataset_appears_with_its_load_time(con):
    """
    Asserts the phrase db.get_dataset produces, not a phrase of our own. How
    the real age_phrase() words itself is util/db.py's business and it has
    changed before; what matters here is that whatever it says reaches the
    report for every dataset.
    """
    _load_items(con)
    _load_items(con, name="customers", rows=50)
    text = describe_workflow_state(con)
    assert "2 dataset(s)" in text
    for name in ("order_items", "customers"):
        assert db.get_dataset(con, name).age_phrase() in text


def test_a_dataset_without_a_contract_names_the_next_call(con):
    _load_items(con)
    text = describe_workflow_state(con)
    assert 'propose_dataset_contract(dataset_name="order_items")' in text
    assert "Analysis is blocked until one is confirmed" in text


def test_a_ready_dataset_points_at_run_analysis(con):
    _load_items(con)
    _confirmed(con)
    text = describe_workflow_state(con)
    assert 'run_analysis(dataset_name="order_items"' in text
    assert "contract v1, ready" in text


def test_the_report_names_the_version_and_the_grain(con):
    _load_items(con)
    _confirmed(con)
    text = describe_workflow_state(con)
    assert "Contract v1, confirmed 2026-08-31 09:00" in text
    assert "Grain: one row = one item on one order" in text


def test_a_stale_contract_shows_as_blocked_not_ready(con):
    _load_items(con)
    _confirmed(con)
    con.execute("ALTER TABLE order_items DROP COLUMN price")
    text = describe_workflow_state(con)
    assert "BLOCKED" in text
    assert "price no longer exist" in text
    assert 'propose_dataset_contract(dataset_name="order_items")' in text


def test_drift_that_does_not_block_shows_as_a_note(con):
    _load_items(con)
    _confirmed(con)
    con.execute("ALTER TABLE order_items ADD COLUMN tax INTEGER")
    text = describe_workflow_state(con)
    assert "NOTE:" in text
    assert "ready" in text


def test_the_summary_counts_what_needs_attention(con):
    _load_items(con)
    _load_items(con, name="customers", rows=50)
    _confirmed(con)
    con.execute("ALTER TABLE order_items DROP COLUMN price")
    text = describe_workflow_state(con)
    assert "1 dataset(s) have no contract: customers." in text
    assert "1 dataset(s) have a contract that no longer fits" in text


def test_a_clean_workspace_has_no_summary_block(con):
    _load_items(con)
    _confirmed(con)
    assert "Summary:" not in describe_workflow_state(con)


def test_a_table_nobody_loaded_is_still_listed(con):
    """
    A view or a hand-made table has no load record. Hiding it would mean
    get_workflow_state lies about what is in the workspace.
    """
    con.execute("CREATE TABLE hand_made AS SELECT 1 AS a")
    states = {s.dataset_name: s for s in dataset_states(con)}
    assert "hand_made" in states
    assert "not created by a loader" in states["hand_made"].source
    assert any("nothing is known" in n for n in states["hand_made"].notes)


def test_the_workspace_starts_empty(con):
    """
    Guards the bug this file shipped with once. A workspace connection is
    file-backed, so without the reset in the fixture every test inherits the
    tables and contracts of the ones before it. The failures then land on
    whichever test happens to assert a count, not on the one that leaked --
    eight of them at once, none pointing at the cause. This fails first and
    says what is actually wrong.
    """
    assert dataset_states(con) == []
    assert store.all_current(con) == []


def test_the_contract_log_is_not_listed_as_a_dataset(con):
    _load_items(con)
    _confirmed(con)
    names = [s.dataset_name for s in dataset_states(con)]
    assert store.CONTRACT_TABLE not in names
    assert db.METADATA_TABLE not in names


def test_every_dataset_says_when_it_arrived(con):
    """
    F13, as far as a test can go. The server outlives the conversation, so a
    dataset from an earlier chat is still here and the report has to say when
    it arrived rather than presenting it as fresh.

    The age itself cannot be faked portably -- register_dataset takes no
    timestamp, and the column it writes to belongs to util/db.py. So this
    asserts the weaker, checkable thing: every dataset carries a load phrase,
    and none is left blank.
    """
    _load_items(con, name="from_an_earlier_chat", rows=10)
    _load_items(con, name="order_items")
    for state in dataset_states(con):
        assert state.age_phrase.strip()
        assert state.age_phrase in describe_workflow_state(con)
