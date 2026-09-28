"""clean/plan.py: the action model and the plan store.

Phase 6, Step 3. Run from the repo root:

    uv run pytest tests/test_cleaning_plan.py -q

The two things worth testing here are the two things that make
apply_cleaning_plan(approved_action_ids=[...]) safe: an id resolves to a stored
action, and a stored plan knows when it has gone stale. Everything else is
storage.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.clean.plan import (  # noqa: E402
    PLAN_TABLE,
    ActionKind,
    CleaningAction,
    CleaningPlan,
    history,
    latest,
    next_action_id,
    record,
)
from backend.engine.profile.runs import is_bookkeeping  # noqa: E402

FP = "facdb163024e"


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def convert(action_id="C001", **kw):
    base = dict(
        action_id=action_id,
        kind=ActionKind.CONVERT_TYPE,
        column="units",
        intent="read units as a whole number",
        sql="SELECT * REPLACE (TRY_CAST(units AS BIGINT) AS units) FROM t",
        rows_affected=6000,
    )
    base.update(kw)
    return CleaningAction(**base)


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------


def test_the_plan_table_is_bookkeeping():
    """Built from the shared prefix, so the existing filters already hide it."""
    assert PLAN_TABLE == "_agent_cleaning_plans"
    assert is_bookkeeping(PLAN_TABLE)
    assert PLAN_TABLE.startswith("_")  # state.py's _loadable_tables filter


def test_an_action_without_sql_is_refused():
    with pytest.raises(ValueError, match="carries no SQL"):
        convert(sql="   ")


def test_a_lossy_action_without_a_sample_is_refused():
    """The P6-D4 rule, enforced by the constructor rather than by a docstring.

    An action that will turn undeclared values into NULL and cannot show one of
    them is not a proposal, and the model refuses to be one.
    """
    with pytest.raises(ValueError, match="is not a proposal"):
        convert(values_lost=3)


def test_a_lossy_action_with_a_sample_is_allowed():
    a = convert(values_lost=3, sample=("not priced",))
    assert a.is_lossy
    assert a.values_lost == 3


def test_a_lossless_action_needs_no_sample():
    """units' seven 'n/a' values are declared missing, so nothing is lost and
    nothing has to be shown."""
    a = convert(values_lost=0)
    assert not a.is_lossy


def test_the_line_states_the_loss_and_names_it_as_information():
    a = convert(values_lost=3, sample=("not priced", "not priced"))
    line = a.line()
    assert "3 value(s) will be discarded" in line
    assert "not declared missing" in line
    assert "information, not" in line


def test_the_loss_unit_is_named_rather_than_assumed_to_be_null():
    """P6-D10. Case folding nulls nothing and still destroys something, so the
    sentence cannot say "becomes NULL" for every kind."""
    merged = CleaningAction(
        action_id="C004", kind=ActionKind.NORMALISE_CASE, column="region",
        intent="fold region to upper case",
        sql="SELECT * REPLACE (upper(region) AS region) FROM t",
        rows_affected=2, values_lost=1, loss_unit="distinct value",
        sample=("North", "north"),
    )
    assert "1 distinct value(s) will be discarded" in merged.line()
    assert "NULL" not in merged.line()


def test_the_loss_unit_survives_storage(con):
    record(con, dataset_name="mixed", row_count=6, fingerprint=FP,
           actions=[convert(values_lost=1, loss_unit="row", sample=("x",))])
    assert latest(con, "mixed").actions[0].loss_unit == "row"


def test_a_lossless_line_does_not_threaten():
    line = convert().line()
    assert "NULL" not in line
    assert "6,000 row(s)" in line


def test_negative_counts_are_refused():
    with pytest.raises(ValueError, match="negative"):
        convert(rows_affected=-1)


def test_action_ids_are_short_and_typable():
    assert next_action_id(0) == "C001"
    assert next_action_id(9) == "C010"


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


def test_a_plan_round_trips(con):
    written = record(
        con, dataset_name="mixed", row_count=6000, fingerprint=FP,
        actions=[convert(), convert("C002", column="unit_price",
                                    values_lost=3, sample=("not priced",))],
    )
    read = latest(con, "mixed")
    assert read is not None
    assert read.plan_id == written.plan_id
    assert read.fingerprint == FP
    assert [a.action_id for a in read.actions] == ["C001", "C002"]


def test_the_sample_survives_storage(con):
    record(con, dataset_name="mixed", row_count=6000, fingerprint=FP,
           actions=[convert(values_lost=2, sample=("not priced", "tbc"))])
    got = latest(con, "mixed").action("C001")
    assert got.sample == ("not priced", "tbc")


def test_the_kind_comes_back_as_an_enum(con):
    record(con, dataset_name="mixed", row_count=6000, fingerprint=FP,
           actions=[convert()])
    assert latest(con, "mixed").actions[0].kind is ActionKind.CONVERT_TYPE


def test_latest_returns_the_newest_plan_only(con):
    old = datetime.now() - timedelta(hours=2)
    record(con, dataset_name="mixed", row_count=10, fingerprint="old",
           actions=[convert()], now=old)
    record(con, dataset_name="mixed", row_count=6000, fingerprint=FP,
           actions=[convert(), convert("C002")])
    plan = latest(con, "mixed")
    assert plan.fingerprint == FP
    assert len(plan.actions) == 2


def test_nothing_proposed_is_none_rather_than_empty(con):
    assert latest(con, "never") is None


def test_history_keeps_both_and_never_overwrites(con):
    record(con, dataset_name="mixed", row_count=10, fingerprint="old",
           actions=[convert()], now=datetime.now() - timedelta(hours=1))
    record(con, dataset_name="mixed", row_count=20, fingerprint=FP,
           actions=[convert()])
    plans = history(con, "mixed")
    assert len(plans) == 2
    assert plans[0].fingerprint == FP  # newest first


def test_plans_for_other_datasets_are_not_returned(con):
    record(con, dataset_name="a", row_count=1, fingerprint=FP,
           actions=[convert()])
    record(con, dataset_name="b", row_count=1, fingerprint=FP,
           actions=[convert()])
    assert latest(con, "a").dataset_name == "a"
    assert len(history(con, "a")) == 1


# --------------------------------------------------------------------------
# resolution and staleness -- what makes approved_action_ids safe
# --------------------------------------------------------------------------


def plan_of(*actions, rows=6000, fp=FP) -> CleaningPlan:
    return CleaningPlan(
        plan_id="p", dataset_name="mixed", proposed_at=datetime.now(),
        row_count=rows, fingerprint=fp, actions=tuple(actions),
    )


def test_resolve_returns_the_unknown_ids_too():
    """A caller that only gets the hits cannot tell the user what it ignored."""
    found, unknown = plan_of(convert(), convert("C002")).resolve(
        ["C001", "C009"]
    )
    assert [a.action_id for a in found] == ["C001"]
    assert unknown == ["C009"]


def test_approving_nothing_resolves_to_nothing():
    found, unknown = plan_of(convert()).resolve([])
    assert found == [] and unknown == []


def test_an_unchanged_table_is_not_stale():
    assert plan_of(convert()).staleness(6000, FP) is None


def test_a_row_count_change_is_stale_and_says_which_way():
    msg = plan_of(convert()).staleness(6010, FP)
    assert "gained 10 row(s)" in msg
    assert "propose_cleaning_plan" in msg


def test_a_fingerprint_change_reads_harder_than_a_row_count():
    """The F13 case, and the one a row count cannot catch.

    Same size, different table. drift_phrase in profile/runs.py returns None
    here, which is the hole Phase 5's live run found; this returns the
    strongest message it has.
    """
    msg = plan_of(convert()).staleness(6000, "0000deadbeef")
    assert "structure has changed" in msg
    assert "different columns" in msg


def test_the_fingerprint_wins_when_both_changed():
    msg = plan_of(convert()).staleness(1, "0000deadbeef")
    assert "structure has changed" in msg
    assert "row(s) since" not in msg


def test_staleness_never_says_the_plan_is_wrong():
    """Same discipline as the profile stage: a plan describing a table that has
    moved is OLD. An agent told 'stale' re-proposes; one told 'wrong'
    apologises."""
    for msg in (
        plan_of(convert()).staleness(6010, FP),
        plan_of(convert()).staleness(6000, "0000deadbeef"),
    ):
        assert "wrong" not in msg.lower()
