"""
Tests for contract/tools.py -- the layer the four MCP tools call.

These assert on the STRINGS the agent will read, because that is the whole
interface. A tool that computes the right thing and says it unusably is a tool
that produces F1.

Workspace-backed, with the reset either side, for the reason test_state.py
documents: db.connect opens a file that outlives the test.
"""

from __future__ import annotations

import json

import pytest

from backend.engine import workspace
from backend.engine.contract import store
from backend.engine.contract import tools
from backend.engine.contract.refusals import Reason, reason_of
from backend.engine.util import db

WORKSPACE = "tools_test"


@pytest.fixture
def con():
    workspace.reset(WORKSPACE)
    c = db.connect(WORKSPACE)
    try:
        yield c
    finally:
        c.close()
        workspace.reset(WORKSPACE)


@pytest.fixture
def loaded(con):
    con.execute(
        """
        CREATE OR REPLACE TABLE order_items AS
        SELECT
          'ORD-' || lpad(((i // 3) + 1)::VARCHAR, 6, '0') AS order_id,
          ((i % 3) + 1)::INTEGER                          AS order_item_id,
          'SELL-' || ((i % 40) + 1)::VARCHAR              AS seller_id,
          (DATE '2024-01-01' + ((i % 300)::INTEGER))      AS order_date,
          (((i % 97) + 1) * 1.5)                          AS price
        FROM range(300) t(i)
        """
    )
    rows, cols = db.table_shape(con, "order_items")
    db.register_dataset(
        con, dataset_name="order_items", source_type="csv",
        source_detail="/data/order_items.csv", row_count=rows, column_count=cols,
    )
    return con


ANSWERS = dict(
    grain="one row = one item on one order",
    measure_definitions={"price": "item price in BRL, excludes freight"},
    aggregations={"price": "none"},
    analysis_window_start="2024-01-01",
    analysis_window_end="2024-10-26",
)


def _settled(con, **extra) -> str:
    """A confirmable proposal, rendered."""
    return tools.propose(con, "order_items", **{**ANSWERS, **extra})


def _json_of(text: str) -> str:
    return text.split("```json")[1].split("```")[0]


# --------------------------------------------------------------------------
# propose
# --------------------------------------------------------------------------

def test_a_first_proposal_asks_rather_than_confirming(loaded):
    text = tools.propose(loaded, "order_items")
    assert "CANNOT BE CONFIRMED" in text
    assert "Put these to the user:" in text
    assert "```json" not in text


def test_an_answered_proposal_returns_json_to_confirm(loaded):
    text = _settled(loaded)
    assert "CANNOT BE CONFIRMED" not in text
    assert "confirm_dataset_contract" in text
    assert "```json" in text


def test_an_unloaded_dataset_is_refused_as_text_not_raised(loaded):
    """
    A raised exception inside a FastMCP tool becomes a traceback, and a
    traceback is not an instruction.
    """
    text = tools.propose(loaded, "nope")
    assert reason_of(text) is Reason.DATASET_NOT_LOADED
    assert "NEXT STEP" in text


def test_half_a_window_is_refused_rather_than_guessed(loaded):
    text = tools.propose(loaded, "order_items", analysis_window_start="2024-01-01")
    assert reason_of(text) is Reason.CONTRACT_INVALID
    assert "both ends" in text


def test_a_window_that_is_not_dates_is_refused(loaded):
    text = tools.propose(
        loaded, "order_items",
        analysis_window_start="last January", analysis_window_end="now",
    )
    assert reason_of(text) is Reason.CONTRACT_INVALID
    assert "not a pair of dates" in text


def test_exclusions_arrive_as_objects(loaded):
    text = _settled(
        loaded,
        known_exclusions=[
            {"rule": "status = 'cancelled'", "reason": "not real revenue",
             "row_count": 100}
        ],
    )
    assert "status = 'cancelled' (100 rows) -- not real revenue" in text


def test_an_exclusion_without_a_reason_is_refused(loaded):
    text = _settled(loaded, known_exclusions=[{"rule": "status = 'cancelled'"}])
    assert reason_of(text) is Reason.CONTRACT_INVALID
    assert "defend later" in text


def test_a_stated_key_that_does_not_hold_is_refused(loaded):
    text = tools.propose(loaded, "order_items", primary_key=["order_id"], **ANSWERS)
    assert reason_of(text) is Reason.KEY_NOT_UNIQUE
    assert "200 row(s) repeat" in text


# --------------------------------------------------------------------------
# confirm
# --------------------------------------------------------------------------

def test_confirming_stores_and_reports_the_version(loaded, tmp_path):
    payload = _json_of(_settled(loaded))
    text = tools.confirm(loaded, payload, export_root=tmp_path)
    assert "version 1" in text
    assert store.current(loaded, "order_items") is not None


def test_confirming_writes_the_export(loaded, tmp_path):
    tools.confirm(loaded, _json_of(_settled(loaded)), export_root=tmp_path)
    assert (tmp_path / "order_items.yaml").exists()


def test_the_export_dir_is_the_repository_not_the_working_directory():
    """C94. Every other path in this engine comes from __file__ (config.py:74); this one came
    from wherever the process was started."""
    from backend.engine.config import PROJECT_ROOT
    assert store.EXPORT_DIR.is_absolute()
    assert store.EXPORT_DIR == PROJECT_ROOT / "docs" / "contracts"


def test_the_default_workspace_exports_where_it_always_has(loaded, tmp_path, monkeypatch):
    from backend.engine.config import DEFAULT_WORKSPACE_ID
    monkeypatch.setattr(store, "EXPORT_DIR", tmp_path)
    tools.confirm(loaded, _json_of(_settled(loaded)), workspace_id=DEFAULT_WORKSPACE_ID)
    assert (tmp_path / "order_items.yaml").exists()


def test_another_workspace_exports_under_its_own_id(loaded, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "EXPORT_DIR", tmp_path)
    text = tools.confirm(loaded, _json_of(_settled(loaded)), workspace_id="session_a")
    assert (tmp_path / "session_a" / "order_items.yaml").exists()
    assert not (tmp_path / "order_items.yaml").exists()
    assert "session_a" in text


def test_two_workspaces_with_one_dataset_name_keep_their_own_export(loaded, tmp_path,
                                                                    monkeypatch):
    """What Track B would hit with its second user: same dataset name, different contract."""
    import yaml
    from backend.engine.config import DEFAULT_WORKSPACE_ID
    monkeypatch.setattr(store, "EXPORT_DIR", tmp_path)
    tools.confirm(loaded, _json_of(_settled(loaded)), workspace_id=DEFAULT_WORKSPACE_ID)
    changed = _settled(loaded, measure_definitions={"price": "item price, INCLUDES freight"})
    tools.confirm(loaded, _json_of(changed), workspace_id="session_b")
    mine = (tmp_path / "order_items.yaml").read_text()
    theirs = (tmp_path / "session_b" / "order_items.yaml").read_text()
    assert "INCLUDES freight" not in mine
    assert "INCLUDES freight" in theirs
    assert yaml.safe_load(mine) != yaml.safe_load(theirs)


def test_a_fenced_json_block_is_accepted(loaded, tmp_path):
    """The agent will paste back what it was shown, fences and all."""
    payload = "```json\n" + _json_of(_settled(loaded)) + "\n```"
    assert "version 1" in tools.confirm(loaded, payload, export_root=tmp_path)


def test_confirming_a_provisional_contract_is_refused(loaded, tmp_path):
    provisional = tools.propose(loaded, "order_items")
    assert "```json" not in provisional  # nothing to paste, by design

    # the agent invents one anyway, by deleting what blocked it
    payload = json.loads(_json_of(_settled(loaded)))
    payload["grain"] = ""
    text = tools.confirm(loaded, json.dumps(payload), export_root=tmp_path)
    assert reason_of(text) is Reason.CONTRACT_INVALID


def test_nonsense_is_refused_with_a_next_step(loaded, tmp_path):
    text = tools.confirm(loaded, "{not json", export_root=tmp_path)
    assert reason_of(text) is Reason.CONTRACT_INVALID
    assert "propose_dataset_contract" in text


def test_a_second_identical_confirmation_does_not_version(loaded, tmp_path):
    payload = _json_of(_settled(loaded))
    tools.confirm(loaded, payload, export_root=tmp_path)
    second = tools.confirm(loaded, payload, export_root=tmp_path)
    assert "version 1" in second
    assert len(store.history(loaded, "order_items")) == 1


def test_a_changed_contract_names_what_moved(loaded, tmp_path):
    tools.confirm(loaded, _json_of(_settled(loaded)), export_root=tmp_path)
    changed = _settled(
        loaded, measure_definitions={"price": "item price, INCLUDES freight"}
    )
    text = tools.confirm(loaded, _json_of(changed), export_root=tmp_path)
    assert "version 2" in text
    assert "Changed since v1: measures[price].definition" in text


def test_a_failed_export_does_not_lose_the_contract(loaded, tmp_path):
    """
    The database is authoritative. Losing the version-controlled copy is an
    inconvenience, not a reason to pretend the confirmation did not happen.
    """
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("I am a file")
    text = tools.confirm(loaded, _json_of(_settled(loaded)), export_root=blocked)
    assert "version 1" in text
    assert "could not be written" in text
    assert store.current(loaded, "order_items") is not None


# --------------------------------------------------------------------------
# run_analysis: the gate
# --------------------------------------------------------------------------

def test_analysis_without_a_contract_is_refused(loaded):
    text = tools.analyse(loaded, "order_items")
    assert reason_of(text) is Reason.NO_CONTRACT
    assert 'propose_dataset_contract(dataset_name="order_items")' in text


def test_the_refusal_names_the_state_it_found(loaded):
    text = tools.analyse(loaded, "order_items")
    assert "loaded (300 rows, 5 columns), no contract" in text


def test_analysis_after_confirming_is_not_a_second_refusal(loaded, tmp_path):
    """
    D4. An agent that clears one gate and is immediately blocked again, for a
    reason it cannot act on, apologises and retries -- the loop the Done-When
    exists to catch. What comes back is a state report, and it carries no
    reason code at all.
    """
    tools.confirm(loaded, _json_of(_settled(loaded)), export_root=tmp_path)
    text = tools.analyse(loaded, "order_items")
    assert reason_of(text) is None
    assert "BLOCKED" not in text


def test_the_report_names_the_contract_it_would_compute_under(loaded, tmp_path):
    tools.confirm(loaded, _json_of(_settled(loaded)), export_root=tmp_path)
    text = tools.analyse(loaded, "order_items")
    assert "Under contract v1 for order_items" in text
    assert "one row = one item on one order" in text
    assert "| price | none | item price in BRL, excludes freight |" in text


def test_the_report_lists_the_analyses_and_names_one_to_call(loaded, tmp_path):
    """Phase 8 arrived. The report named describe_dataset because there was
    nothing to compute; now it names the nine and one call that runs one.
    """
    tools.confirm(loaded, _json_of(_settled(loaded)), export_root=tmp_path)
    text = tools.analyse(loaded, "order_items")
    for name in ("summary_stats", "cross_tab", "pareto", "ranking_shift"):
        assert name in text
    assert 'compute_analysis(dataset_name="order_items"' in text
    assert "Phase 8" not in text


def test_the_question_is_echoed_when_one_is_asked(loaded, tmp_path):
    tools.confirm(loaded, _json_of(_settled(loaded)), export_root=tmp_path)
    text = tools.analyse(loaded, "order_items", question="revenue by seller")
    assert "revenue by seller" in text


def test_caveats_reach_the_report(loaded, tmp_path):
    tools.confirm(
        loaded,
        _json_of(_settled(loaded, caveats=["November is partial"])),
        export_root=tmp_path,
    )
    loaded.execute(
        """INSERT INTO order_items
           SELECT 'ORD-NEW-' || i::VARCHAR, 1, 'SELL-1', DATE '2024-11-01', 9.0
           FROM range(30) t(i)"""
    )
    text = tools.analyse(loaded, "order_items")
    assert "would have to carry" in text
    assert "gained 30 rows" in text
    assert "November is partial" in text


def test_a_stale_contract_refuses_the_analysis(loaded, tmp_path):
    tools.confirm(loaded, _json_of(_settled(loaded)), export_root=tmp_path)
    loaded.execute("ALTER TABLE order_items DROP COLUMN price")
    text = tools.analyse(loaded, "order_items")
    assert reason_of(text) is Reason.CONTRACT_STALE


# --------------------------------------------------------------------------
# workflow state
# --------------------------------------------------------------------------

def test_workflow_state_lists_what_is_loaded(loaded):
    text = tools.workflow_state(loaded)
    assert "order_items" in text
    assert 'propose_dataset_contract(dataset_name="order_items")' in text


def test_workflow_state_of_an_empty_workspace(con):
    assert "Nothing is loaded" in tools.workflow_state(con)


def test_the_whole_conversation_end_to_end(loaded, tmp_path):
    """
    Propose, get refused, answer, confirm, analyse. Five calls, and the shape
    the live run in Step 8 has to reproduce.
    """
    first = tools.propose(loaded, "order_items")
    assert "CANNOT BE CONFIRMED" in first

    answered = _settled(loaded)
    assert "```json" in answered

    stored = tools.confirm(loaded, _json_of(answered), export_root=tmp_path)
    assert "version 1" in stored

    result = tools.analyse(loaded, "order_items")
    assert reason_of(result) is None
    assert "Under contract v1" in result

    state = tools.workflow_state(loaded)
    assert "contract v1, ready" in state
