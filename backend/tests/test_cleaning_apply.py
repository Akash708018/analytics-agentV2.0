"""clean/apply.py: all of them or none, with the before kept.

Phase 6, Step 6. Run from the repo root:

    uv run pytest tests/test_cleaning_apply.py -q

Two properties carry this module and both are tested against a real table:
a failed action leaves the dataset exactly as it was, and the previous contents
survive somewhere a person can still read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.clean import apply as ap  # noqa: E402
from backend.engine.clean import detect  # noqa: E402
from backend.engine.clean.plan import ActionKind, CleaningAction  # noqa: E402

TOKENS = ["", "NA", "N/A", "-", "--", "null", "NULL", "None"]


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(
        """
        CREATE TABLE mixed AS SELECT * FROM (VALUES
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
        ) t(order_id, region, units, unit_price)
        """
    )
    yield c
    c.close()


def proposals(con, table="mixed"):
    return detect.detect(con, source=table, target=table, missing_tokens=TOKENS)


def pick(actions, kind, column=None):
    return next(
        a for a in actions if a.kind is kind and a.column == column
    )


def tables(con):
    return sorted(
        r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    )


# --------------------------------------------------------------------------
# P6-D1: the name stays, the previous contents move behind the prefix
# --------------------------------------------------------------------------


def test_the_dataset_keeps_its_name(con):
    actions = proposals(con)
    ap.apply(con, dataset_name="mixed",
             actions=[pick(actions, ActionKind.CONVERT_TYPE, "units")])
    assert "mixed" in tables(con)


def test_the_snapshot_is_bookkeeping_and_hidden_by_the_underscore(con):
    """db.user_tables filters `table_name NOT LIKE '\\_%'` in SQL, so this name
    is already excluded from every listing without a new filter."""
    result = ap.apply(
        con, dataset_name="mixed",
        actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")],
    )
    assert result.history_table == "_agent_history_mixed_v1"
    assert result.history_table.startswith("_")
    visible = [
        r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' "
            "AND table_name NOT LIKE '\\_%' ESCAPE '\\'"
        ).fetchall()
    ]
    assert visible == ["mixed"]


def test_the_snapshot_holds_the_data_as_it_was(con):
    ap.apply(con, dataset_name="mixed",
             actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")])
    assert con.execute(
        "SELECT units FROM _agent_history_mixed_v1 WHERE order_id = 'ORD-03'"
    ).fetchone()[0] == "n/a"
    assert con.execute(
        "SELECT units FROM mixed WHERE order_id = 'ORD-03'"
    ).fetchone()[0] is None


def test_versions_count_up(con):
    ap.apply(con, dataset_name="mixed",
             actions=[pick(proposals(con), ActionKind.TRIM_WHITESPACE, "region")])
    ap.apply(con, dataset_name="mixed",
             actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")])
    assert ap.history_tables(con, "mixed") == [
        "_agent_history_mixed_v1", "_agent_history_mixed_v2"
    ]


def test_the_snapshot_is_readable_because_nothing_else_can_read_it(con):
    """A before-table nobody can open is not evidence. run_sql is Phase 8."""
    ap.apply(con, dataset_name="mixed",
             actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")])
    rows = ap.read_history(con, "_agent_history_mixed_v1", limit=3)
    assert len(rows) == 3
    with pytest.raises(ValueError, match="not a cleaning snapshot"):
        ap.read_history(con, "mixed")


# --------------------------------------------------------------------------
# P6-D11: a conversion claims its column
# --------------------------------------------------------------------------


def test_a_conversion_and_a_missing_normalisation_on_one_column_are_refused(con):
    """Measured in Step 5: the second meets a BIGINT column and DuckDB says
    `No function matches ... 'trim(BIGINT)'`, after the first has already run."""
    actions = proposals(con)
    pair = [
        pick(actions, ActionKind.CONVERT_TYPE, "units"),
        pick(actions, ActionKind.NORMALISE_MISSING, "units"),
    ]
    problems = ap.conflicts(pair)
    assert len(problems) == 1
    assert "both change units" in problems[0]
    with pytest.raises(ValueError, match="both change units"):
        ap.apply(con, dataset_name="mixed", actions=pair)


def test_the_refusal_offers_an_order_rather_than_telling_you_to_drop_one(con):
    """Step 7's live run is why this changed.

    "Approve one" still disposes of the declared tokens, and the ledger then
    records them as casualties of a cast rather than as a normalisation
    somebody named. Two calls end with the same table and a better record, and
    an agent worked that out unprompted before the refusal said it.
    """
    actions = proposals(con)
    normalise = pick(actions, ActionKind.NORMALISE_MISSING, "units")
    pair = [pick(actions, ActionKind.CONVERT_TYPE, "units"), normalise]
    message = ap.conflicts(pair)[0]
    assert f"Approve {normalise.action_id} on its own first" in message
    assert "propose again" in message
    assert "same table" in message
    assert "Approve one" not in message


def test_two_text_actions_on_one_column_are_fine(con):
    """Trim then case-fold is two text functions on a VARCHAR, in that order,
    and both survive. The rule is about type changes, not about columns."""
    actions = proposals(con)
    pair = [
        pick(actions, ActionKind.TRIM_WHITESPACE, "region"),
        pick(actions, ActionKind.NORMALISE_CASE, "region"),
    ]
    assert ap.conflicts(pair) == []
    result = ap.apply(con, dataset_name="mixed", actions=pair)
    assert len(result.applied) == 2


def test_actions_on_different_columns_do_not_conflict(con):
    actions = proposals(con)
    assert ap.conflicts([
        pick(actions, ActionKind.CONVERT_TYPE, "units"),
        pick(actions, ActionKind.NORMALISE_MISSING, "region"),
    ]) == []


# --------------------------------------------------------------------------
# all or nothing
# --------------------------------------------------------------------------


def test_a_failing_action_leaves_the_dataset_exactly_as_it_was(con):
    """DuckDB's DDL is transactional -- measured in Step 6, a CREATE OR REPLACE
    inside a transaction rolls back and a table created inside one disappears.

    conflicts() is the better guard, because being told which id to drop beats
    a rollback that says nothing. This is what happens when the guard misses.
    """
    good = pick(proposals(con), ActionKind.CONVERT_TYPE, "units")
    broken = CleaningAction(
        action_id="C099", kind=ActionKind.TRIM_WHITESPACE, column="units",
        intent="a statement that cannot run",
        sql='CREATE OR REPLACE TABLE "mixed" AS SELECT * REPLACE '
            '(no_such_function("units") AS "units") FROM "mixed"',
    )
    with pytest.raises(Exception):
        ap.apply(con, dataset_name="mixed", actions=[good, broken])

    assert con.execute(
        "SELECT units FROM mixed WHERE order_id = 'ORD-03'"
    ).fetchone()[0] == "n/a"
    assert con.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'mixed' AND column_name = 'units'"
    ).fetchone()[0] == "VARCHAR"


def test_a_rollback_takes_the_snapshot_with_it(con):
    """No half-cleaned dataset AND no orphan snapshot describing a clean that
    never happened."""
    good = pick(proposals(con), ActionKind.CONVERT_TYPE, "units")
    broken = CleaningAction(
        action_id="C099", kind=ActionKind.TRIM_WHITESPACE, column="unit_price",
        intent="a statement that cannot run",
        sql='CREATE OR REPLACE TABLE "mixed" AS SELECT * REPLACE '
            '(no_such_function("unit_price") AS "unit_price") FROM "mixed"',
    )
    with pytest.raises(Exception):
        ap.apply(con, dataset_name="mixed", actions=[good, broken])
    assert ap.history_tables(con, "mixed") == []


# --------------------------------------------------------------------------
# the guards on what gets run
# --------------------------------------------------------------------------


def test_approving_nothing_is_refused(con):
    with pytest.raises(ValueError, match="nothing was approved"):
        ap.apply(con, dataset_name="mixed", actions=[])


def test_a_statement_that_writes_elsewhere_is_refused(con):
    """A stored plan is data. Data that names its own write target gets checked
    against the target the caller asked for rather than trusted."""
    rogue = CleaningAction(
        action_id="C100", kind=ActionKind.CONVERT_TYPE, column="units",
        intent="write somewhere else",
        sql='CREATE OR REPLACE TABLE "somewhere_else" AS SELECT * FROM "mixed"',
    )
    with pytest.raises(ValueError, match="does not write to mixed"):
        ap.apply(con, dataset_name="mixed", actions=[rogue])
    assert "somewhere_else" not in tables(con)


# --------------------------------------------------------------------------
# what the result says
# --------------------------------------------------------------------------


def test_the_result_reports_rows_before_and_after(con):
    con.execute("INSERT INTO mixed SELECT * FROM mixed ORDER BY order_id LIMIT 2")
    dupe = next(
        a for a in proposals(con) if a.kind is ActionKind.DROP_DUPLICATE_ROWS
    )
    result = ap.apply(con, dataset_name="mixed", actions=[dupe])
    assert result.rows_before == 12
    assert result.rows_after == 10
    assert result.applied[0].rows_removed == 2


def test_each_applied_action_carries_the_statement_that_ran(con):
    action = pick(proposals(con), ActionKind.CONVERT_TYPE, "units")
    result = ap.apply(con, dataset_name="mixed", actions=[action])
    assert result.applied[0].statement == action.sql


def test_the_result_line_names_the_snapshot(con):
    result = ap.apply(
        con, dataset_name="mixed",
        actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")],
    )
    line = result.line()
    assert "_agent_history_mixed_v1" in line
    assert "1 action(s) applied to mixed" in line


def test_a_conversion_does_not_change_the_row_count(con):
    result = ap.apply(
        con, dataset_name="mixed",
        actions=[pick(proposals(con), ActionKind.CONVERT_TYPE, "units")],
    )
    assert result.rows_before == result.rows_after == 10
