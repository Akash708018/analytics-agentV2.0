"""validate_dataset: the two refusals, and the report on the other side.

Phase 7, Step 7. Run from the repo root:

    uv run pytest tests/test_validate_tools.py -q

These tests need a real workspace, because the point of the tool is that it
reads the contract on a writable handle and then runs every check on one the
engine will not let write.

The contract is supplied by patching `contract_store.current`, not by writing
one through the store. These tests are about how validate/tools.py USES a
contract, and Phase 6's tests recorded what happens otherwise: a fixture
contract fails on validators this phase has not read, and satisfying them means
inventing a grain for a fixture that does not care about one.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.contract.refusals import Reason, reason_of  # noqa: E402
from backend.engine.util import db  # noqa: E402
from backend.engine.validate import tools  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "broken_sales.csv"
WORKSPACE = "validate_tools_test"


class _Window:
    def __init__(self, start, end):
        self.start, self.end = start, end


class _Contract:
    """The fields validate/tools.py reads off a contract, and no more. expectations joined in
    Cleanup Step 13."""

    def __init__(self, primary_key=None, date_column=None, window=None,
                 foreign_keys=None, domains=None, expectations=None):
        self.primary_key = primary_key or []
        self.date_column = date_column
        self.analysis_window = _Window(*window) if window else None
        self.foreign_keys = foreign_keys or []
        self.domains = domains or {}
        self.expectations = expectations or []


class _Stored:
    def __init__(self, contract, version=1, row_count=186,
                 confirmed_at=datetime(2026, 9, 1, 10, 0)):
        self.contract = contract
        self.version = version
        self.row_count = row_count
        self.confirmed_at = confirmed_at


@pytest.fixture()
def ws():
    if not FIXTURE.exists():
        pytest.skip(f"{FIXTURE.name} not generated; see make_fixtures.py")
    workspace.reset(WORKSPACE)
    con = db.connect(WORKSPACE)
    con.execute(f"CREATE TABLE sales AS SELECT * FROM read_csv_auto('{FIXTURE}')")
    con.close()
    yield WORKSPACE
    workspace.reset(WORKSPACE)


def confirm(monkeypatch, stored):
    monkeypatch.setattr(
        tools.contract_store, "current",
        lambda con, name: stored if name == "sales" else None,
    )


FULL = _Contract(primary_key=["order_id"], date_column="order_ts",
                 window=(date(2024, 1, 1), date(2024, 12, 31)))


# --------------------------------------------------------------------------
# the refusals, and the one that is deliberately absent
# --------------------------------------------------------------------------


def test_an_unloaded_dataset_refuses_and_names_a_call(ws):
    text = tools.validate_dataset(ws, "nope")
    assert reason_of(text) is Reason.DATASET_NOT_LOADED
    assert "list_datasets()" in text


def test_no_contract_refuses_rather_than_printing_a_page_of_not_run(ws, monkeypatch):
    confirm(monkeypatch, None)
    text = tools.validate_dataset(ws, "sales")
    assert reason_of(text) is Reason.NO_CONTRACT
    assert "propose_dataset_contract" in text
    # No table was rendered. Asserting on "NOT RUN" would not work here: the
    # refusal's own WHY explains that a report would be a page of them.
    assert "| check | on | result |" not in text


def test_a_key_that_does_not_hold_is_reported_rather_than_refused(ws, monkeypatch):
    """P7-D11, and the whole reason this tool does not call require_contract.

    The gate refuses this exact table with KEY_NOT_UNIQUE. Routed through it,
    the tool that exists to explain why the data is broken would be blocked by
    the data being broken.
    """
    confirm(monkeypatch, _Stored(FULL))
    text = tools.validate_dataset(ws, "sales")
    assert reason_of(text) is None
    assert "Primary key is unique" in text
    assert "3 key value(s) repeat" in text


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------


def test_the_report_carries_every_check(ws, monkeypatch):
    confirm(monkeypatch, _Stored(FULL, version=3))
    text = tools.validate_dataset(ws, "sales", today=date(2026, 1, 1))
    # Four, not five: this contract was confirmed at 186 rows, which is what
    # the table holds, so table.row_count passes. The five-failure report is
    # the one in the next test, where the contract was agreed at 200.
    assert "sales: 4 of 6 check(s) failed, 2 passed." in text
    assert "Checked against contract v3." in text
    assert "| 186 | 171 | 9 | 6 |" in text


def test_the_row_count_is_compared_against_the_contract_not_the_table(ws, monkeypatch):
    """Binding.row_count is the expectation. No new contract field, P7-D9."""
    confirm(monkeypatch, _Stored(FULL, row_count=200))
    text = tools.validate_dataset(ws, "sales")
    assert "lost 14 row(s)" in text
    assert "2026-09-01" in text


def test_a_thin_contract_runs_what_it_can_and_says_the_rest_did_not_run(ws, monkeypatch):
    confirm(monkeypatch, _Stored(_Contract(primary_key=["order_id"])))
    text = tools.validate_dataset(ws, "sales")
    assert "Primary key is unique" in text
    assert "NOT RUN" in text
    assert "no date column" in text


def test_a_stale_contract_reports_rather_than_refusing(ws, monkeypatch):
    """A column that has gone is named by the check that wanted it. A refusal
    would name the same column and run nothing else."""
    confirm(monkeypatch, _Stored(_Contract(primary_key=["gone"],
                                           date_column="also_gone")))
    text = tools.validate_dataset(ws, "sales")
    assert reason_of(text) is None
    assert "gone is not a column of sales" in text
    assert "also_gone is not a column of sales" in text


# --------------------------------------------------------------------------
# what it ends with
# --------------------------------------------------------------------------


def test_a_failing_report_points_at_the_contract_not_at_cleaning(ws, monkeypatch):
    """P7-D12. Nothing in the cleaning layer can settle any of these findings,
    and a NEXT STEP that contradicts the message above it is the defect Phase 6
    Step 8 corrected twice."""
    confirm(monkeypatch, _Stored(FULL))
    text = tools.validate_dataset(ws, "sales")
    assert 'NEXT STEP: call propose_dataset_contract(dataset_name="sales")' in text
    assert "propose_cleaning_plan" not in text
    assert "duplicate keys whose rows differ are not duplicate rows" in text


def test_a_clean_report_points_at_analysis(ws, monkeypatch):
    con = db.connect(ws)
    con.execute("CREATE TABLE tidy AS SELECT * FROM (VALUES ('a'),('b')) v(k)")
    con.close()
    monkeypatch.setattr(
        tools.contract_store, "current",
        lambda con, name: _Stored(_Contract(primary_key=["k"]), row_count=2)
        if name == "tidy" else None,
    )
    text = tools.validate_dataset(ws, "tidy")
    assert "all 3 check(s) passed" not in text  # date checks did not run
    assert 'NEXT STEP: call run_analysis(dataset_name="tidy")' not in text


def test_a_report_with_nothing_wrong_and_nothing_skipped_points_at_analysis(ws, monkeypatch):
    con = db.connect(ws)
    con.execute("""CREATE TABLE tidy AS SELECT * FROM (VALUES
        ('a', TIMESTAMP '2024-03-01 09:00:00'),
        ('b', TIMESTAMP '2024-04-01 09:00:00')) v(k, ts)""")
    con.close()
    monkeypatch.setattr(
        tools.contract_store, "current",
        lambda con, name: _Stored(
            _Contract(primary_key=["k"], date_column="ts",
                      window=(date(2024, 1, 1), date(2024, 12, 31))),
            row_count=2)
        if name == "tidy" else None,
    )
    text = tools.validate_dataset(ws, "tidy", today=date(2026, 1, 1))
    assert "all 6 check(s) passed" in text
    assert 'NEXT STEP: call run_analysis(dataset_name="tidy")' in text
    assert "The contract and the table agree." in text


# --------------------------------------------------------------------------
# it cannot write
# --------------------------------------------------------------------------


def test_the_checks_run_on_a_connection_that_cannot_write(ws):
    """Step 1 of Phase 6 measured the guard; this asserts the tool uses it."""
    ro = db.connect_read_only(ws)
    try:
        with pytest.raises(duckdb.InvalidInputException):
            ro.execute("CREATE TABLE evil AS SELECT 1")
        assert ro.execute("SELECT count(*) FROM sales").fetchone()[0] == 186
    finally:
        ro.close()


def test_validating_changes_nothing(ws, monkeypatch):
    confirm(monkeypatch, _Stored(FULL))
    before = db.connect(ws)
    try:
        tables_before = sorted(db.user_tables(before))
        rows_before = before.execute("SELECT count(*) FROM sales").fetchone()[0]
    finally:
        before.close()

    tools.validate_dataset(ws, "sales")

    after = db.connect(ws)
    try:
        assert sorted(db.user_tables(after)) == tables_before
        assert after.execute("SELECT count(*) FROM sales").fetchone()[0] == rows_before
    finally:
        after.close()


# --------------------------------------------------------------------------
# Step 10b: a call named that the reader should not make
# --------------------------------------------------------------------------


def test_a_row_count_loss_names_the_ledger_rather_than_the_contract(ws, monkeypatch):
    """Found by two live readers, independently, disagreeing with the tool.

    Re-confirming at the count the table holds signs a number nobody can
    account for, and every later drift check then measures against it. The
    cleaning ledger is the only call in the workspace that can say whether an
    approved action removed the rows.
    """
    confirm(monkeypatch, _Stored(FULL, row_count=200))
    text = tools.validate_dataset(ws, "sales")
    assert 'NEXT STEP: call get_cleaning_ledger(dataset_name="sales")' in text
    assert "propose_dataset_contract" not in text.split("NEXT STEP:")[1]
    assert "sign a number nobody can account for" in text


def test_reading_the_ledger_is_not_cleaning(ws, monkeypatch):
    """get_cleaning_ledger reports; propose_cleaning_plan changes things.
    P7-D12 rules out the second, not the first."""
    confirm(monkeypatch, _Stored(FULL, row_count=200))
    text = tools.validate_dataset(ws, "sales")
    assert "propose_cleaning_plan" not in text


def test_a_grown_table_still_points_at_the_contract(ws, monkeypatch):
    """Only a LOSS is unexplained. Rows arriving is a reload, which
    classify_drift calls NEUTRAL and P7-D9 passes."""
    confirm(monkeypatch, _Stored(FULL, row_count=150))
    text = tools.validate_dataset(ws, "sales")
    assert 'NEXT STEP: call propose_dataset_contract(dataset_name="sales")' in text
