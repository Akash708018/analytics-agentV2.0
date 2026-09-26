"""clean/ledger.py and clean/tools.py: what ran, and the gate in front of it.

Phase 6, Step 7. Run from the repo root:

    uv run pytest tests/test_cleaning_ledger.py -q

The ledger tests use a plain in-memory connection. The tool tests need a real
workspace, because the whole point of propose_cleaning_plan is that it opens the
workspace file read-only and cannot write to it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.clean import apply as ap  # noqa: E402
from backend.engine.clean import detect, ledger, tools  # noqa: E402
from backend.engine.clean.plan import ActionKind  # noqa: E402
from backend.engine.contract.refusals import Reason, reason_of  # noqa: E402
from backend.engine.profile.runs import is_bookkeeping  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "cleaning_tools_test"
TOKENS = ["", "NA", "N/A", "-", "--", "null", "NULL", "None"]

ROWS = """
  ('ORD-01', 'North',  '10',  '9.50'),
  ('ORD-02', 'North ', '20',  '10.00'),
  ('ORD-03', 'north',  'n/a', '11.00'),
  ('ORD-04', 'South',  '40',  '12.25'),
  ('ORD-05', 'N/A',    '50',  '13.00'),
  ('ORD-06', 'South',  '60',  '14.00'),
  ('ORD-07', 'East',   '70',  '15.75'),
  ('ORD-08', 'East',   '80',  '16.00'),
  ('ORD-09', 'West',   '90',  '17.50'),
  ('ORD-10', 'West',   '11',  '18.00')
"""
CREATE = (
    f"CREATE OR REPLACE TABLE mixed AS SELECT * FROM (VALUES {ROWS}) "
    f"t(order_id, region, units, unit_price)"
)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(CREATE)
    yield c
    c.close()


@pytest.fixture()
def ws():
    workspace.reset(WORKSPACE)
    c = db.connect(WORKSPACE)
    c.execute(CREATE)
    c.close()
    yield WORKSPACE
    workspace.reset(WORKSPACE)


def proposals(con):
    return detect.detect(con, source="mixed", target="mixed",
                         missing_tokens=TOKENS)


def pick(actions, kind, column=None):
    return next(a for a in actions if a.kind is kind and a.column == column)


# --------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------


def test_the_ledger_table_is_bookkeeping():
    assert ledger.LEDGER_TABLE == "_agent_cleaning_ledger"
    assert is_bookkeeping(ledger.LEDGER_TABLE)


def test_an_empty_ledger_says_nothing_has_been_cleaned(con):
    text = ledger.describe(con)
    assert "Nothing has been cleaned" in text
    assert "propose_cleaning_plan" in text


def test_one_row_per_action_not_per_apply(con):
    """The Done-When is "the ledger shows exactly those 3", which is a
    statement about actions. An apply-level row would have to summarise."""
    actions = proposals(con)
    pair = [
        pick(actions, ActionKind.TRIM_WHITESPACE, "region"),
        pick(actions, ActionKind.NORMALISE_CASE, "region"),
    ]
    ap.apply(con, dataset_name="mixed", actions=pair, plan_id="p1")
    assert ledger.count(con) == 2
    assert {e.action_id for e in ledger.entries(con)} == {
        a.action_id for a in pair
    }


def test_the_ledger_names_the_snapshot_for_each_action(con):
    ap.apply(con, dataset_name="mixed",
             actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")],
             plan_id="p1")
    entry = ledger.entries(con)[0]
    assert entry.history_table == "_agent_history_mixed_v1"
    assert "_agent_history_mixed_v1" in entry.line()


def test_the_ledger_carries_the_statement_that_ran(con):
    action = pick(proposals(con), ActionKind.CONVERT_TYPE, "units")
    ap.apply(con, dataset_name="mixed", actions=[action], plan_id="p1")
    assert ledger.entries(con)[0].statement == action.sql


def test_a_rolled_back_apply_leaves_no_ledger_entry(con):
    """The reason this is a table and not a JSONL file. A ledger written after
    the commit can fail after it; one written before a rollback records a clean
    that never happened. A row in the same transaction cannot disagree."""
    good = pick(proposals(con), ActionKind.CONVERT_TYPE, "units")
    from backend.engine.clean.plan import CleaningAction
    broken = CleaningAction(
        action_id="C099", kind=ActionKind.TRIM_WHITESPACE, column="unit_price",
        intent="cannot run",
        sql='CREATE OR REPLACE TABLE "mixed" AS SELECT * REPLACE '
            '(no_such_function("unit_price") AS "unit_price") FROM "mixed"',
    )
    with pytest.raises(Exception):
        ap.apply(con, dataset_name="mixed", actions=[good, broken], plan_id="p1")
    assert ledger.count(con) == 0


def test_the_ledger_filters_by_dataset(con):
    con.execute("CREATE TABLE other AS SELECT ' x ' AS c")
    ap.apply(con, dataset_name="mixed",
             actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")],
             plan_id="p1")
    ap.apply(
        con, dataset_name="other",
        actions=detect.detect(con, source="other", target="other",
                              missing_tokens=TOKENS),
        plan_id="p2",
    )
    assert ledger.count(con, "mixed") == 1
    assert {e.dataset_name for e in ledger.entries(con, "other")} == {"other"}


def test_a_long_ledger_is_capped_with_the_remainder_counted(con):
    """P5-D3's principle on a different problem: a cap with the remainder
    reported, never a sample with the shortfall unstated."""
    now = datetime.now()
    ledger.ensure_table(con)
    for i in range(ledger.INLINE_ENTRIES + 5):
        ledger.record_action(
            con, applied_at=now - timedelta(minutes=i), dataset_name="mixed",
            plan_id="p", action_id=f"C{i:03d}", kind="CONVERT_TYPE",
            column="units", rows_before=10, rows_after=10,
            history_table="_agent_history_mixed_v1", statement="SELECT 1",
        )
    text = ledger.describe(con, "mixed")
    assert "25 cleaning action(s) applied" in text
    assert "5 older one(s) are not shown" in text
    assert text.count("CONVERT_TYPE") == ledger.INLINE_ENTRIES


# --------------------------------------------------------------------------
# the tools
# --------------------------------------------------------------------------


def test_the_proposal_connection_cannot_write(ws):
    """Step 1 measured the guard; this asserts the tool actually uses it."""
    ro = tools.connect_read_only(ws)
    try:
        with pytest.raises(duckdb.InvalidInputException):
            ro.execute("CREATE TABLE evil AS SELECT 1")
        assert ro.execute("SELECT count(*) FROM mixed").fetchone()[0] == 10
    finally:
        ro.close()


def test_proposing_lists_the_actions_and_changes_nothing(ws):
    text = tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    assert "Nothing has been changed" in text
    assert "C001" in text
    assert "apply_cleaning_plan" in text
    con = db.connect(ws)
    try:
        assert con.execute(
            "SELECT units FROM mixed WHERE order_id = 'ORD-03'"
        ).fetchone()[0] == "n/a"
    finally:
        con.close()


def test_proposing_a_missing_dataset_refuses_with_a_reason(ws):
    text = tools.propose_cleaning_plan(ws, "nope")
    assert reason_of(text) is Reason.DATASET_NOT_LOADED
    assert "list_datasets()" in text


def test_applying_without_a_plan_refuses(ws):
    text = tools.apply_cleaning_plan(ws, "mixed", ["C001"])
    assert reason_of(text) is Reason.NO_CLEANING_PLAN
    assert "propose_cleaning_plan" in text


def test_approving_nothing_refuses_and_says_why(ws):
    tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    text = tools.apply_cleaning_plan(ws, "mixed", [])
    assert reason_of(text) is Reason.NOTHING_APPROVED
    assert "nothing is run unless it is named" in text


def test_an_unknown_id_refuses_rather_than_running_the_rest(ws):
    """Running the known ids and silently skipping the unknown one is the worst
    failure this tool has available."""
    tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    text = tools.apply_cleaning_plan(ws, "mixed", ["C001", "C099"])
    assert reason_of(text) is Reason.ACTION_NOT_IN_PLAN
    assert "C099" in text
    con = db.connect(ws)
    try:
        assert ledger.count(con, "mixed") == 0
    finally:
        con.close()


def test_conflicting_ids_refuse_and_name_one_to_drop(ws):
    tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        units = [a.action_id for a in latest(con, "mixed").actions
                 if a.column == "units"]
    finally:
        con.close()
    text = tools.apply_cleaning_plan(ws, "mixed", units)
    assert reason_of(text) is Reason.ACTIONS_CONFLICT
    assert "both change units" in text


def test_a_stale_plan_refuses_and_says_old_rather_than_wrong(ws):
    tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        con.execute("INSERT INTO mixed SELECT * FROM mixed LIMIT 2")
    finally:
        con.close()
    text = tools.apply_cleaning_plan(ws, "mixed", ["C001"])
    assert reason_of(text) is Reason.CLEANING_PLAN_STALE
    assert "wrong" not in text.lower()


def test_applying_runs_only_what_was_approved_and_says_what_it_skipped(ws):
    tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        stored = latest(con, "mixed")
        price = next(a.action_id for a in stored.actions
                     if a.column == "unit_price")
        total = len(stored.actions)
    finally:
        con.close()

    text = tools.apply_cleaning_plan(ws, "mixed", [price])
    assert "1 action(s) applied to mixed" in text
    assert f"{total - 1} action(s) were not approved" in text

    con = db.connect(ws)
    try:
        types = dict(con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'mixed'").fetchall())
        assert types["unit_price"] != "VARCHAR"
        assert types["units"] == "VARCHAR"  # not approved, no trace
        assert ledger.count(con, "mixed") == 1
    finally:
        con.close()


def test_the_ledger_tool_reports_what_ran(ws):
    tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        price = next(a.action_id for a in latest(con, "mixed").actions
                     if a.column == "unit_price")
    finally:
        con.close()
    tools.apply_cleaning_plan(ws, "mixed", [price])
    text = tools.get_cleaning_ledger(ws, "mixed")
    assert "1 cleaning action(s) applied for mixed" in text
    assert "_agent_history_mixed_v1" in text


def test_a_clean_table_proposes_nothing_and_says_so(ws):
    con = db.connect(ws)
    try:
        con.execute("CREATE TABLE tidy AS SELECT 'a' AS k, 1 AS n")
    finally:
        con.close()
    text = tools.propose_cleaning_plan(ws, "tidy", missing_values=TOKENS)
    assert "Nothing to clean in tidy" in text
    assert reason_of(text) is None


# --------------------------------------------------------------------------
# Step 8: the four things the live run found
# --------------------------------------------------------------------------


def test_the_suggested_call_excludes_the_lossy_action(ws):
    """Step 7's run watched the first-three slice land on the one action
    carrying a discard warning, one apply later, after the ids renumbered."""
    text = tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        lossy = [a.action_id for a in latest(con, "mixed").actions if a.is_lossy]
    finally:
        con.close()
    assert lossy, "the fixture no longer has a lossy action to exclude"
    suggestion = text.split("NEXT STEP:")[1]
    for action_id in lossy:
        assert action_id not in suggestion


def test_the_suggested_call_is_one_the_tool_would_accept(ws):
    """A suggestion the tool refuses is worse than no suggestion."""
    text = tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    suggestion = text.split("NEXT STEP:")[1]
    ids = [p.strip(' "') for p in
           suggestion.split("approved_action_ids=[")[1].split("]")[0].split(",")]
    applied = tools.apply_cleaning_plan(ws, "mixed", ids)
    assert reason_of(applied) is None, applied[:200]


def test_the_ledger_line_names_the_plan_an_id_belongs_to(ws):
    """Per-plan ids are right for approval and wrong for a permanent record."""
    tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        stored = latest(con, "mixed")
        first = stored.actions[0].action_id
    finally:
        con.close()
    tools.apply_cleaning_plan(ws, "mixed", [first])
    con = db.connect(ws)
    try:
        entry = ledger.entries(con, "mixed")[0]
        assert f"{first}/{entry.plan_id[:6]}" in entry.line()
    finally:
        con.close()


def test_approved_ids_are_applied_in_the_order_given(con):
    """Not plan order. An agent could not tell which it was and forced it with
    two calls rather than assume."""
    actions = proposals(con)
    a = pick(actions, ActionKind.TRIM_WHITESPACE, "region")
    b = pick(actions, ActionKind.NORMALISE_CASE, "region")
    result = ap.apply(con, dataset_name="mixed", actions=[b, a], plan_id="p1")
    assert [x.action_id for x in result.applied] == [b.action_id, a.action_id]


def test_the_suggestion_does_not_pre_empt_a_normalisation_with_a_conversion(ws):
    """The filter the first attempt at P6-D12 missed.

    conflicts() only fires when a conversion and a normalisation on one column
    are in the SAME call. Suggesting the conversion alone passes every check
    and still disposes of the declared tokens by cast rather than by a step
    somebody named -- which is exactly what this module's own refusal tells you
    to avoid. A suggestion that contradicts the refusal is not a smaller bug
    than one that gets refused.
    """
    text = tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        actions = latest(con, "mixed").actions
    finally:
        con.close()

    normalised = {a.column for a in actions
                  if a.kind is ActionKind.NORMALISE_MISSING}
    pre_empting = [a.action_id for a in actions
                   if a.kind is ActionKind.CONVERT_TYPE
                   and a.column in normalised]
    assert pre_empting, "the fixture no longer has a column with both"

    suggestion = text.split("NEXT STEP:")[1]
    approved = suggestion.split("approved_action_ids=[")[1].split("]")[0]
    for action_id in pre_empting:
        assert action_id not in approved
        # named in the prose, though: deferred is not the same as excluded, and
        # a list of ids cannot tell you which one it was.
        assert action_id in suggestion
    assert "left for the next round on purpose" in suggestion


def test_the_suggestion_says_why_it_held_one_back(ws):
    """Excluded and deferred look identical in a list of ids. They are not the
    same thing, and the difference is the whole argument for the two-call
    order."""
    text = tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    suggestion = text.split("NEXT STEP:")[1]
    assert "records which one disposed of them" in suggestion


# --------------------------------------------------------------------------
# P6-D8: the contract's vocabulary and its excluded columns
# --------------------------------------------------------------------------


class _Stored:
    """Stands in for StoredContract, which only needs its .contract here."""

    def __init__(self, contract):
        self.contract = contract


class _Declaration:
    """The two fields clean/tools.py reads off a contract, and nothing else.

    NOT a real DatasetContract, on purpose. The first version of these tests
    built one, and every one of them failed on a validator this phase had not
    read: a contract with a blank `grain` must list "grain" in `unresolved`,
    because "a contract either states what one row is, or says out loud that
    nobody has yet". Satisfying that would mean inventing a grain for a fixture
    these tests do not care about, and guessing which OTHER validators fire
    next.

    So the behaviour tests use this, and one separate test below asserts the
    real model actually has the two fields with the right defaults -- by
    reading `model_fields`, which needs no instance and runs no validator.
    """

    def __init__(self, missing_values=None, excluded_columns=None):
        self.missing_values = missing_values
        self.excluded_columns = excluded_columns or []


def _confirm(monkeypatch, contract):
    """Patch the boundary rather than write a contract through the store.

    These tests are about how clean/tools.py USES a contract, not about how one
    is stored, so they do not depend on confirm()'s signature either.
    """
    monkeypatch.setattr(
        tools.contract_store, "current",
        lambda con, name: _Stored(contract) if name == "mixed" else None,
    )


def test_the_contract_really_has_the_two_fields(ws):
    """P6-D8 on the real model, without instantiating it.

    model_fields is class-level: no validators run, so this says what the
    contract declares rather than what one particular contract passes.
    """
    from backend.engine.contract.dataset_contract import DatasetContract
    fields = DatasetContract.model_fields
    assert "missing_values" in fields
    assert "excluded_columns" in fields
    assert fields["missing_values"].default is None, (
        "unset must mean 'use the project vocabulary', not 'no tokens'"
    )
    assert fields["excluded_columns"].default_factory() == []


def test_with_no_contract_the_project_vocabulary_is_used(ws):
    text = tools.propose_cleaning_plan(ws, "mixed")
    assert "NORMALISE_MISSING on region" in text


def test_the_contract_vocabulary_is_used_when_no_argument_is_given(ws, monkeypatch):
    """Level 2 of three. Somebody confirmed it for this dataset."""
    _confirm(monkeypatch, _Declaration(missing_values=[]))
    text = tools.propose_cleaning_plan(ws, "mixed")
    assert "NORMALISE_MISSING" not in text


def test_an_explicit_argument_beats_the_contract(ws, monkeypatch):
    """Level 1. Somebody typed it for this call."""
    _confirm(monkeypatch, _Declaration(missing_values=[]))
    text = tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    assert "NORMALISE_MISSING on region" in text


def test_an_unset_contract_field_is_a_default_not_a_gap(ws, monkeypatch):
    """missing_values=None on the contract means "use the project vocabulary",
    not "no tokens". If it meant a gap the field would belong in `unresolved`,
    and every contract in the workspace would be unconfirmable the day it was
    added."""
    _confirm(monkeypatch, _Declaration(missing_values=None))
    text = tools.propose_cleaning_plan(ws, "mixed")
    assert "NORMALISE_MISSING on region" in text


def test_an_excluded_column_is_not_proposed_and_the_exclusion_is_stated(ws, monkeypatch):
    _confirm(monkeypatch, _Declaration(excluded_columns=["region"]))
    text = tools.propose_cleaning_plan(ws, "mixed")
    assert "on region" not in text
    assert "excluded by the contract and were not looked at: region" in text


def test_excluding_every_dirty_column_leaves_nothing_to_clean(ws, monkeypatch):
    _confirm(monkeypatch,
             _Declaration(excluded_columns=["region", "units", "unit_price"]))
    text = tools.propose_cleaning_plan(ws, "mixed")
    assert "Nothing to clean in mixed" in text
    assert "excluded by the contract" in text


def test_the_conflict_refusal_recommends_the_action_its_own_message_names(ws):
    """The NEXT STEP used to contradict the WHY two lines above it.

    The message says "approve C004 on its own first"; the first version's
    NEXT STEP then named C003, the conversion, because it used the first
    approved id in list order. A next_call that disagrees with the refusal it
    sits under is worse than no next_call.
    """
    tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        actions = latest(con, "mixed").actions
        units = [a for a in actions if a.column == "units"]
    finally:
        con.close()
    normalise = next(a for a in units
                     if a.kind is ActionKind.NORMALISE_MISSING)
    convert = next(a for a in units if a.kind is ActionKind.CONVERT_TYPE)

    text = tools.apply_cleaning_plan(ws, "mixed", [a.action_id for a in units])
    assert reason_of(text) is Reason.ACTIONS_CONFLICT
    next_step = text.split("NEXT STEP:")[1]
    assert f'["{normalise.action_id}"]' in next_step
    assert f'["{convert.action_id}"]' not in next_step
    assert f"Approve {normalise.action_id} on its own first" in text


def test_the_nothing_approved_refusal_does_not_recommend_a_lossy_action(ws):
    """Same rule, different refusal. It used actions[0]."""
    tools.propose_cleaning_plan(ws, "mixed", missing_values=[])
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        lossy = {a.action_id for a in latest(con, "mixed").actions if a.is_lossy}
    finally:
        con.close()
    assert lossy, "the fixture no longer has a lossy action"
    text = tools.apply_cleaning_plan(ws, "mixed", [])
    assert reason_of(text) is Reason.NOTHING_APPROVED
    next_step = text.split("NEXT STEP:")[1]
    for action_id in lossy:
        assert f'["{action_id}"]' not in next_step


def test_the_suggestion_offers_every_safe_action_not_the_first_three(ws):
    """The cap was inherited from a slice that no longer exists.

    `actions[:3]` capped at three because the set was arbitrary and the line
    had to stay short. Once the set became "lossless and compatible", the cap
    started dropping actions that met the stated test while the line claimed
    otherwise. A live agent spotted the omission and added the action back by
    hand, which is the correct outcome and not one to rely on.
    """
    text = tools.propose_cleaning_plan(ws, "mixed", missing_values=TOKENS)
    con = db.connect(ws)
    try:
        from backend.engine.clean.plan import latest
        actions = latest(con, "mixed").actions
    finally:
        con.close()

    safe = {a.action_id for a in tools._safe_suggestion(actions)}
    suggested = set(
        p.strip(' "') for p in
        text.split("approved_action_ids=[")[1].split("]")[0].split(",")
    )
    assert suggested == safe
    assert len(safe) > 3 or len(safe) == len(
        [a for a in actions if not a.is_lossy]
    ), "the fixture no longer distinguishes a cap from no cap"


# --------------------------------------------------------------------------
# the cleaned stage
# --------------------------------------------------------------------------


def test_an_uncleaned_dataset_produces_no_lines(con):
    """Empty, not a "not cleaned" sentence.

    describe_workflow_state already writes its own line when profile notes come
    back empty; a second module inventing its own absence message would put two
    different voices in one section.
    """
    assert ledger.state_notes(con, "mixed") == []


def test_a_cleaned_dataset_names_the_count_the_action_and_the_snapshot(con):
    actions = proposals(con)
    ap.apply(con, dataset_name="mixed", plan_id="p1",
             actions=[pick(actions, ActionKind.CONVERT_TYPE, "units")])
    notes = ledger.state_notes(con, "mixed")
    assert len(notes) == 3
    assert "1 action(s) applied" in notes[0]
    assert "CONVERT_TYPE on units" in notes[0]
    assert "_agent_history_mixed_v1" in notes[1]
    assert 'get_cleaning_ledger(dataset_name="mixed")' in notes[2]


def test_it_reports_the_newest_action_not_the_first(con):
    actions = proposals(con)
    ap.apply(con, dataset_name="mixed", plan_id="p1",
             actions=[pick(actions, ActionKind.TRIM_WHITESPACE, "region")])
    ap.apply(con, dataset_name="mixed", plan_id="p2",
             actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")])
    notes = ledger.state_notes(con, "mixed")
    assert "2 action(s) applied" in notes[0]
    assert "CONVERT_TYPE on units" in notes[0]
    assert "_agent_history_mixed_v2" in notes[1]


def test_the_snapshot_line_says_why_nothing_lists_it(con):
    """A reader who goes looking for that table in list_datasets will not find
    it, and should be told so rather than left to wonder."""
    ap.apply(con, dataset_name="mixed", plan_id="p1",
             actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")])
    assert "bookkeeping" in ledger.state_notes(con, "mixed")[1]
