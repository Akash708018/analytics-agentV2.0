"""`validate_dataset`, and the two things it refuses on.

Phase 7, Step 7. Returns a string: either the report, or a `Refusal.to_text()`
carrying a reason code and the exact call to make next.

**P7-D11: this does NOT go through `require_contract`.** The gate is Phase 4's
and it refuses a dataset whose primary key does not hold, with reason
KEY_NOT_UNIQUE. Routed through it, `validate_dataset` on `broken_sales.csv`
would refuse before running a single check -- the tool that exists to explain
why the data is broken, blocked by the data being broken. Validation is what you
reach for *after* the gate has refused, so it reads the contract rather than
asking permission to use it.

That is not a hole in the gate. Nothing here computes a number: every path
returns prose about the table, the read handle cannot write, and no caller can
turn a report into an analysis. Locked decision 12 is about numbers leaving the
building without an agreement behind them, and this ships no numbers of that
kind.

It also means a refusal from the gate and a report from here describe the same
table differently on purpose. The gate names the first thing that stopped it.
The report names everything it found.

**Two refusals, and only two.** No dataset, and no contract -- the two states
where there is nothing to validate *against*. A stale contract does not refuse:
a check whose column has gone reports NOT RUN and says which column, which is
more use than a refusal naming the same column and running nothing else.

**Read-only, in the second connection.** `contract.store.current` runs
CREATE TABLE IF NOT EXISTS and cannot be called on a read-only attach, so the
contract is read on a writable handle which is then closed, and the checks run
on a handle the engine will not let write. Two connections in sequence, never
open at once -- the shape `clean/tools.py` established and the reason
`connect_read_only` now lives in `util/db.py`.
"""

from __future__ import annotations

from datetime import date

from ..contract import store as contract_store
from ..contract.refusals import Reason, Refusal
from ..util import db
from . import report, rules, runs
from .rules import Outcome


def _not_loaded(con, dataset_name: str) -> str:
    available = ", ".join(db.user_tables(con)) or "(none loaded)"
    return Refusal(
        reason=Reason.DATASET_NOT_LOADED,
        what=f"there is no dataset called '{dataset_name}' in this workspace.",
        why="validation checks a loaded table, and nothing is read from disk here.",
        state=f"loaded: {available}",
        detail="list_datasets() shows what is already here.",
        next_call="list_datasets()",
    ).to_text()


def _no_contract(dataset_name: str, rows: int, cols: int) -> str:
    return Refusal(
        reason=Reason.NO_CONTRACT,
        what=(
            f"'{dataset_name}' has no confirmed Dataset Contract, so there is "
            f"nothing to validate it against."
        ),
        why=(
            "every check here compares the table to something somebody agreed: "
            "the key that identifies a row, the column a row is dated by, the "
            "window the analysis covers, the row count the definitions were "
            "written against. With no contract there is no claim to test, and "
            "a report saying so would be a page of NOT RUN."
        ),
        state=f"loaded ({rows:,} rows, {cols} columns), no contract",
        detail=(
            "Show the draft to the user, then call confirm_dataset_contract once "
            "they agree."
        ),
        next_call=f'propose_dataset_contract(dataset_name="{dataset_name}")',
    ).to_text()


def _closing(dataset_name: str, results: list) -> tuple[str, str]:
    """The note and the call the report ends with.

    **P7-D12: a validation failure is settled in the contract, not in the
    data** -- with the tools this project has today. Every failure this phase
    can produce is one the cleaning layer cannot touch: duplicate KEYS whose
    rows differ are not duplicate ROWS, a missing id cannot be invented, a date
    outside the window cannot be moved into it, and rows lost since the
    contract was confirmed cannot be restored by rewriting the table. So
    `propose_cleaning_plan` would be the wrong call in every case, and naming
    it would be the failure Phase 6 Step 8 corrected twice: a NEXT STEP that
    contradicts the message above it.

    What CAN change is the agreement -- a different grain, a different key, a
    wider window, a re-confirmation at the count the table actually holds.
    That is `propose_dataset_contract`, and it is the honest call whether the
    fix is to change the agreement or to go and look at the data first.
    """
    if all(r.outcome is Outcome.PASS for r in results):
        return (
            "Every check passed. The contract and the table agree.",
            f'run_analysis(dataset_name="{dataset_name}")',
        )

    # A row-count LOSS is the one failure where re-confirming is the wrong
    # first move, and two live readers reached that unprompted: re-confirming
    # at the count the table holds signs a number nobody can account for, and
    # every later drift check then measures against it as if it meant
    # something. Incident practice everywhere says find the cause before
    # resetting the baseline. get_cleaning_ledger is the only call in this
    # workspace that can say whether an approved action removed the rows --
    # and reading the ledger is not cleaning, which is what P7-D12 rules out.
    lost = next(
        (r for r in results
         if r.check_id == "table.row_count" and "lost" in r.detail),
        None,
    )
    if lost is not None:
        return (
            "Rows the contract was agreed against are gone, and nothing here "
            "can say where. Re-confirming at the count the table holds would "
            "sign a number nobody can account for, and every later drift check "
            "would measure against it. Rule out the cleaning layer first: if "
            "no approved action removed them, they left by a route nobody "
            "recorded, and that is worth knowing before the agreement moves.",
            f'get_cleaning_ledger(dataset_name="{dataset_name}")',
        )

    note = (
        "A finding here is a disagreement between the table and the contract, "
        "and either side can move. Nothing in the cleaning layer can settle "
        "one: duplicate keys whose rows differ are not duplicate rows, a "
        "missing id cannot be invented, and a date outside the window cannot "
        "be moved into it. Changing the agreement -- a different grain, a "
        "wider window, a re-confirmation at the count the table holds -- is "
        "what propose_dataset_contract is for. Look at the rows first if the "
        "data is what should change."
    )
    return note, f'propose_dataset_contract(dataset_name="{dataset_name}")'


def validate_dataset(
    workspace_id: str, dataset_name: str, *, today: date | None = None
) -> str:
    """Check a loaded table against the contract in force for it."""
    con = db.connect(workspace_id)
    try:
        if dataset_name not in db.user_tables(con):
            return _not_loaded(con, dataset_name)
        stored = contract_store.current(con, dataset_name)
        if stored is None:
            rows, cols = db.table_shape(con, dataset_name)
            return _no_contract(dataset_name, rows, cols)
    finally:
        con.close()

    contract = stored.contract
    window = (
        (contract.analysis_window.start, contract.analysis_window.end)
        if contract.analysis_window
        else None
    )

    ro = db.connect_read_only(workspace_id)
    try:
        results = rules.key_checks(ro, dataset_name, list(contract.primary_key))
        results += rules.date_checks(
            ro, dataset_name, contract.date_column, window, today=today
        )
        # The two declared checks, after the four derived ones and before the
        # table-level one, so the report reads down the same order the contract
        # does: the key, the dates, what this table points at, what its columns
        # may hold, and finally the table against the count it was agreed at.
        results += rules.reference_checks(
            ro, dataset_name, contract.foreign_keys
        )
        results += rules.domain_checks(ro, dataset_name, contract.domains)
        results += rules.expectation_checks(
            ro, dataset_name, contract.expectations, list(contract.primary_key))
        results.append(
            rules.row_count_check(
                ro, dataset_name, stored.row_count,
                agreed_when=f"{stored.confirmed_at:%Y-%m-%d}",
            )
        )
    finally:
        ro.close()

    # Third connection, writable, after the read-only one is closed. Same
    # sequence clean/tools.py established: writable to read the contract,
    # read-only to run the checks, writable to record that they ran. Never two
    # at once -- one handle per file per process.
    con = db.connect(workspace_id)
    try:
        runs.record(
            con,
            dataset_name=dataset_name,
            contract_version=stored.version,
            row_count=next(
                (r.rows for r in results if r.scope is rules.Scope.ROWS), 0
            ),
            checks_total=len(results),
            checks_failed=sum(1 for r in results if r.outcome is Outcome.FAIL),
            checks_not_run=sum(
                1 for r in results if r.outcome is Outcome.NOT_RUN
            ),
        )
    finally:
        con.close()

    note, next_call = _closing(dataset_name, results)
    return report.render(
        dataset_name, results,
        contract_version=stored.version, note=note, next_call=next_call,
    )


__all__ = ["validate_dataset"]
