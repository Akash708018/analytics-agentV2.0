"""
The profiling stage in get_workflow_state.

Phase 4 built that tool to answer "where am I" across a server that outlives the
chat. It knew two stages, loaded and contracted. Section 8.2's own example state
string names a third -- `loaded (51,290 rows), profiled, not cleaned, no
contract` -- and nothing recorded it until Step 7a.

These assert on the rendered report rather than on `runs.state_notes`, which
`test_profile_runs.py` covers. What is being defended here is that the lines
reach the page, in the right place, without displacing what Phase 4 put there.
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine import workspace
from backend.engine.contract.refusals import reason_of
from backend.engine.profile import runs, tools
from backend.engine.state import dataset_states, describe_workflow_state
from backend.engine.util import db

WORKSPACE = "profile_state_test"


@pytest.fixture
def con():
    workspace.reset(WORKSPACE)
    c = db.connect(WORKSPACE)
    try:
        yield c
    finally:
        c.close()
        workspace.reset(WORKSPACE)


def _load(con, name="sales", rows=200):
    con.execute(
        f"""CREATE OR REPLACE TABLE "{name}" AS SELECT
              'ORD-' || lpad(i::VARCHAR, 5, '0') AS order_id,
              CASE WHEN i % 17 = 0 THEN 'N/A' ELSE 'North' END AS region,
              ((i % 97) + 1) * 1.5 AS revenue
            FROM range({rows}) t(i)"""
    )
    shape = db.table_shape(con, name)
    db.register_dataset(
        con, dataset_name=name, source_type="csv",
        source_detail=f"/tmp/{name}.csv", row_count=shape[0],
        column_count=shape[1],
    )
    return con


# --------------------------------------------------------------------------
# never profiled
# --------------------------------------------------------------------------

def test_an_unprofiled_dataset_says_so_and_names_the_call(con):
    _load(con)
    text = describe_workflow_state(con)
    assert "Not profiled." in text
    assert 'profile_dataset(dataset_name="sales")' in text


def test_the_offer_says_what_profiling_is_for(con):
    """
    'Not profiled' alone is a status. The agent needs to know why it would
    bother, and that profiling comes BEFORE anyone has to agree what the
    columns mean -- otherwise it reads as another gate to clear.
    """
    _load(con)
    assert "before anyone has to agree" in describe_workflow_state(con)


def test_the_offer_does_not_displace_the_next_step(con):
    """
    Phase 4 decided what the next call is. This adds a line, not a competing
    instruction, and an agent handed two NEXT STEPs picks one at random.
    """
    _load(con)
    text = describe_workflow_state(con)
    assert text.count("NEXT STEP:") == 1
    assert 'NEXT STEP: call propose_dataset_contract(dataset_name="sales")' in text


# --------------------------------------------------------------------------
# profiled
# --------------------------------------------------------------------------

def test_a_profiled_dataset_says_when(con):
    _load(con)
    tools.profile_dataset(con, WORKSPACE, "sales")
    text = describe_workflow_state(con)
    assert "profiled just now" in text
    assert "200 rows" in text
    assert "Not profiled." not in text


def test_the_report_offers_the_full_counts(con):
    _load(con)
    tools.profile_dataset(con, WORKSPACE, "sales")
    assert "Full counts: read_result_file(" in describe_workflow_state(con)


def test_the_path_it_offers_actually_opens(con):
    """
    The workflow state is the recovery tool. A path in it that does not open
    turns the one place an agent goes when lost into another dead end.
    """
    _load(con)
    tools.profile_dataset(con, WORKSPACE, "sales")
    run = runs.latest(con, "sales")
    assert reason_of(tools.read_result(WORKSPACE, run.result_path)) is None


def test_the_profile_lines_sit_between_the_source_and_the_contract(con):
    """
    Loaded, then profiled, then contracted: the order the workflow happens in.
    A reader scanning the block should not have to reassemble the sequence.
    """
    _load(con)
    tools.profile_dataset(con, WORKSPACE, "sales")
    block = describe_workflow_state(con).split("### sales")[1]
    assert block.index("Source:") < block.index("profiled just now")
    assert block.index("profiled just now") < block.index("No contract.")


# --------------------------------------------------------------------------
# profiled, then the table moved
# --------------------------------------------------------------------------

def test_a_stale_profile_is_reported_as_old_not_wrong(con):
    _load(con)
    tools.profile_dataset(con, WORKSPACE, "sales")
    con.execute(
        "INSERT INTO sales SELECT 'ORD-NEW-' || i::VARCHAR, 'South', 1.0 "
        "FROM range(30) t(i)"
    )
    text = describe_workflow_state(con)
    assert "out of date" in text
    assert "gained 30 row(s)" in text
    assert "200 then, 230 now" in text


def test_a_stale_profile_stops_offering_its_own_counts(con):
    """
    Full counts taken against 200 rows are not what anyone wants when 230 are
    loaded. The offer becomes a re-run instead.
    """
    _load(con)
    tools.profile_dataset(con, WORKSPACE, "sales")
    con.execute(
        "INSERT INTO sales SELECT 'ORD-NEW-' || i::VARCHAR, 'South', 1.0 "
        "FROM range(30) t(i)"
    )
    text = describe_workflow_state(con)
    assert "Full counts: read_result_file(" not in text
    assert 'Re-run profile_dataset(dataset_name="sales")' in text


def test_re_profiling_clears_the_staleness(con):
    _load(con)
    tools.profile_dataset(con, WORKSPACE, "sales")
    con.execute(
        "INSERT INTO sales SELECT 'ORD-NEW-' || i::VARCHAR, 'South', 1.0 "
        "FROM range(30) t(i)"
    )
    tools.profile_dataset(con, WORKSPACE, "sales")
    text = describe_workflow_state(con)
    assert "out of date" not in text
    assert "230 rows" in text


# --------------------------------------------------------------------------
# the log is not a dataset
# --------------------------------------------------------------------------

def test_the_profile_log_is_not_listed_as_a_dataset(con):
    """
    The bug this repeats. `_agent_contracts` was listed once, and the tool
    whose job is telling someone where they are then reported that the contract
    log had no contract. `_loadable_tables` filters on the leading underscore,
    so a third bookkeeping table needed no change -- this is the test that says
    so out loud.
    """
    _load(con)
    tools.profile_dataset(con, WORKSPACE, "sales")
    names = [s.dataset_name for s in dataset_states(con)]
    assert runs.PROFILE_TABLE not in names
    assert runs.PROFILE_TABLE not in describe_workflow_state(con)


def test_an_empty_workspace_still_says_what_to_do(con):
    """The profiling lines must not appear where there is nothing to profile."""
    text = describe_workflow_state(con)
    assert "Nothing is loaded" in text
    assert "Not profiled" not in text
