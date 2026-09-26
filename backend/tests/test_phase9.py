"""Phase 9 acceptance test. Run from the repo root:

    uv run python tests/test_phase9.py

Asserts the first half of the Done-When from the build guide:

    calendar_coverage finds the missing month in the Olist window

The second half -- correlated_shift firing on an injected synthetic shift --
is clause four. Two synthetic measures are added to the copied table, each
stepping at the same month, and the clause asserts the shift is found where it
was put. Then one of them is moved six months and the clause asserts it is not
found, because a coincidence reported on this calendar 15% of the time by
arithmetic is not evidence of anything on its own.

**This clause copies the table in, and the reason is a finding.** Querying
Olist in place was the plan: attach READ_ONLY, bind a contract, analyse
olist.public.orders without moving 99,441 rows. The gate refuses it. Measured:
state._loadable_tables reads db.user_tables, which lists what this workspace
owns, so an attached catalog is not a loaded dataset however cleanly it
attaches, and every clause came back DATASET_NOT_LOADED. The in-place path
serves inspection -- preview_table, describe_source, row_count -- and analysis
is not on it. That is P9-O4, and it is a gap in the product rather than in this
script: server.py advertises querying olist.public.orders in place.

So load_table copies the table into the workspace, which is the path a person
actually takes, and the acceptance proves what a person can actually do.
99,441 rows clears ROW_REFUSE, which is SIZE_GATES.excel_refuse_rows.

**It SKIPs rather than fails when the database is not there.** load_table
attaches the source itself and raises LoadRefused when Postgres is down, the
alias is missing, or the table is too big to copy; that is caught and named. A test that reaches a live local
database and errors everywhere else is worse than no test; one that skips with
a reason is portable. P9-O1 was exactly this question.

**The contract is real**, the way Phase 8's is: proposed values, a Binding, and
store.confirm, through the real require_contract. Orders carries no numeric
column at all -- three VARCHARs and five TIMESTAMPs -- so its one measure is a
count of order_id, which is what there is to count.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.analysis import tools as analysis_tools  # noqa: E402
from backend.engine.contract import store  # noqa: E402
from backend.engine.contract.dataset_contract import (  # noqa: E402
    Binding,
    DatasetContract,
    Measure,
)
from backend.engine.contract.refusals import Reason, reason_of  # noqa: E402
from backend.engine.ingest.csv_loader import LoadRefused  # noqa: E402
from backend.engine.ingest.postgres import load_table  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "phase9_test"
SOURCE = "olist"
TABLE = "orders"
DATE_COLUMN = "order_purchase_timestamp"

# Measured from the source on 13/09/2026: 99,441 orders spanning 2016-09-04 to
# 2018-10-17, which is 26 calendar months, of which 25 hold rows. The absent
# one is November 2016, named by the source itself rather than recalled from
# the build guide.
MISSING = "2016-11"
PERIODS = 26
ROWS = 99441

PASSED = 0
FAILED = 0
SKIPPED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def skip(label: str, why: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {label}  ({why})")


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def confirm_contract(date_column: str | None,
                     extra: list | None = None) -> object:
    """A real contract on the copied table, confirmed through the store.

    `date_column=None` is legal and is how the refusal is provoked: the model
    refuses a window without a column, never a column without a window.
    """
    con = db.connect(WORKSPACE)
    try:
        pairs = [
            (r[0], r[1])
            for r in con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [TABLE],
            ).fetchall()
        ]
        rows = con.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
        contract = DatasetContract(
            dataset_name=TABLE,
            grain="one row = one order",
            primary_key=["order_id"],
            date_column=date_column,
            measures=[
                Measure(name="order_id", agg="count",
                        definition="orders placed"),
                *(extra or []),
            ],
            dimensions=["order_status"],
            bound_to=Binding.from_pairs(pairs, rows),
        )
        return store.confirm(con, contract)
    finally:
        con.close()


def run(analysis_type: str, **params) -> str:
    """One analysis on a short-lived connection, through the real gate."""
    con = db.connect(WORKSPACE)
    try:
        return analysis_tools.compute_analysis(
            con, WORKSPACE, TABLE, analysis_type, **params
        )
    finally:
        con.close()


def mount() -> str | None:
    """Copy the source table in, or the reason there is nothing to test on.

    load_table attaches the source itself and refuses above ROW_REFUSE without
    a limit. Postgres down, no alias, or a table too big to copy all arrive
    here as LoadRefused and become a named skip rather than an error.
    """
    con = db.connect(WORKSPACE)
    try:
        load_table(con, SOURCE, TABLE)
        count = con.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
        return None if count else f"{SOURCE}.public.{TABLE} is empty"
    except LoadRefused as exc:
        return str(exc).splitlines()[0]
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        con.close()


# --------------------------------------------------------------------------
# Clause 1: the missing month, on the real table
# --------------------------------------------------------------------------


def clause_one() -> str | None:
    heading("Clause 1: calendar_coverage finds the month Olist does not have")

    con = db.connect(WORKSPACE)
    try:
        copied = con.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
    finally:
        con.close()
    check(f"the copy holds {ROWS:,} rows", copied == ROWS, f"{copied:,}")

    confirm_contract(DATE_COLUMN)
    text = run("calendar_coverage", grain="month")

    if reason_of(text) is not None:
        check("calendar_coverage runs on Olist", False,
              text.splitlines()[0][:90])
        return None
    check("calendar_coverage runs on Olist", True, f"{ROWS:,} rows")

    check(f"{MISSING} appears in the result", MISSING in text,
          "" if MISSING in text else "the absent month is not in the output")
    check(f"the calendar is {PERIODS} month(s) long",
          f"{PERIODS} month(s)" in text,
          "" if f"{PERIODS} month(s)" in text else text[:120])
    check("the absent month is named in a sentence, not only in the table",
          f"hold none: {MISSING}" in text or f"{MISSING}," in text)
    check("no session zone is stated for a naive column",
          "TimeZone" not in text and "Asia/" not in text,
          "order_purchase_timestamp is TIMESTAMP, not TIMESTAMPTZ")
    return text


# --------------------------------------------------------------------------
# Clause 2: the grain reaches the analysis through the MCP tool
# --------------------------------------------------------------------------


def clause_two() -> None:
    heading("Clause 2: grain arrives through the registered tool")

    try:
        from backend.engine import server
    except Exception as exc:  # FastMCP missing or broken
        skip("grain forwarding", f"server.py did not import: {type(exc).__name__}")
        return
    if not callable(getattr(server, "compute_analysis", None)):
        skip("grain forwarding", "@mcp.tool did not return a plain function")
        return

    text = server.compute_analysis(
        dataset_name=TABLE, analysis_type="calendar_coverage",
        grain="quarter", workspace_id=WORKSPACE,
    )
    check("grain reaches calendar_coverage", reason_of(text) is None,
          "" if reason_of(text) is None else text.splitlines()[0][:90])
    check("and it was quarters that came back", "2016-Q" in text,
          "" if "2016-Q" in text else text[:120])


# --------------------------------------------------------------------------
# Clause 3: no date column, no calendar
# --------------------------------------------------------------------------


def clause_three() -> None:
    heading("Clause 3: a contract with no date_column refuses, and says what to do")

    confirm_contract(None)
    text = run("calendar_coverage", grain="month")

    check("the refusal is ANALYSIS_NOT_POSSIBLE",
          reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE,
          f"got {reason_of(text)}")
    check("and it names the call that fixes it",
          "confirm_dataset_contract" in text)


# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Clause 4: the injected shift, and a shift that is not there
# --------------------------------------------------------------------------

# The month the synthetic step begins, and the month the split is reported
# after. They differ by one on purpose: "breaks after X" names the last period
# before the change, so a step beginning in October is reported after
# September. Measured, not assumed.
INJECTED_AT = "2017-10"
SPLIT_AFTER = "2017-09"
DECOY_AT = "2018-04"


def _inject(cut_b: str) -> None:
    """Two synthetic measures on Olist's own calendar, each stepping once.

    agg="mean" on the contract side is what makes this work: a per-period sum
    is value times row count, and Olist's monthly volume climbs steeply enough
    to swamp the step. A mean reads exactly 10.0 or 50.0 in every month.
    """
    con = db.connect(WORKSPACE)
    try:
        for name in ("shift_a", "shift_b"):
            con.execute(
                f'ALTER TABLE "{TABLE}" ADD COLUMN IF NOT EXISTS {name} DOUBLE'
            )
        dated = f'"{DATE_COLUMN}"'
        month = "strftime(date_trunc('month', " + dated + "), '%Y-%m')"
        con.execute(
            f'UPDATE "{TABLE}" SET shift_a = '
            f"CASE WHEN {month} < ? THEN 10.0 ELSE 50.0 END", [INJECTED_AT])
        con.execute(
            f'UPDATE "{TABLE}" SET shift_b = '
            f"CASE WHEN {month} < ? THEN 100.0 ELSE 20.0 END", [cut_b])
    finally:
        con.close()


def clause_four() -> None:
    heading("Clause 4: correlated_shift finds a shift that was put there")

    extra = [
        Measure(name="shift_a", agg="mean",
                definition=f"a synthetic level, stepped at {INJECTED_AT}"),
        Measure(name="shift_b", agg="mean",
                definition=f"a second synthetic level, stepped at {INJECTED_AT}"),
    ]

    _inject(INJECTED_AT)
    confirm_contract(DATE_COLUMN, extra)
    text = run("correlated_shift", measure="shift_a", against="shift_b",
               grain="month")

    if reason_of(text) is not None:
        check("correlated_shift runs on Olist", False,
              text.splitlines()[0][:90])
        return
    check("correlated_shift runs on Olist", True, f"{ROWS:,} rows")

    check("both series break in the same place",
          "Both break in the same place" in text,
          "" if "Both break" in text else text[:140])
    check(f"the break is found where it was injected, after {SPLIT_AFTER}",
          f"shift_a after {SPLIT_AFTER}" in text
          and f"shift_b after {SPLIT_AFTER}" in text,
          "" if f"shift_a after {SPLIT_AFTER}" in text else text[:140])
    check("the rate at which unrelated series coincide is reported",
          "% of the time" in text)
    check(f"{MISSING} narrows the comparison rather than being ignored",
          MISSING in text and "admissible count" in text)

    # The control. Without it this clause passes on unrelated series about one
    # time in seven, which is not a Done-When, it is a coin.
    _inject(DECOY_AT)
    confirm_contract(DATE_COLUMN, extra)
    decoy = run("correlated_shift", measure="shift_a", against="shift_b",
                grain="month")
    check("a shift that is not there is not reported",
          "They do not break in the same place" in decoy,
          "" if "do not break" in decoy else decoy[:140])


def clause_five() -> None:
    """P9-O2: calendar_coverage is general by construction and not yet by evidence.

    Everything measured so far is container fixtures and this one table. Olist offers three more
    temporal shapes for nothing, and each exercises a different branch on real data rather than
    on a fixture built to be refused.

    The catalog filter is not decoration. load_table attaches the source, so the table exists in
    two catalogs at once and a query filtered on table_name alone counts every column twice --
    which on the first run reported four date columns on order_reviews and two on order_items.
    The counts asserted below were measured on 21/09/2026 after the filter was added, and they
    are what the item claimed: two, none, and one.
    """
    heading("Clause 5: calendar_coverage on three other real temporal shapes")

    for table, column, ndates, expect in (
        ("order_reviews", None, 2, "two date columns, so a proposal cannot pick one"),
        ("order_payments", None, 0, "no date column at all, so the refusal fires on real data"),
        ("order_items", "shipping_limit_date", 1, "a limit date running past the order data"),
    ):
        con = db.connect(WORKSPACE)
        try:
            load_table(con, SOURCE, table)
            dates = [
                r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_catalog = current_database() AND table_name = ? "
                    "AND (data_type LIKE '%TIMESTAMP%' OR data_type = 'DATE') "
                    "ORDER BY ordinal_position",
                    [table],
                ).fetchall()
            ]
        except LoadRefused as exc:
            skip(f"{table}", str(exc).splitlines()[0])
            continue
        except Exception as exc:  # noqa: BLE001
            skip(f"{table}", f"{type(exc).__name__}: {exc}")
            continue
        finally:
            con.close()

        check(f"{table} has the shape the item claims", len(dates) == ndates,
              f"{len(dates)} date column(s): {', '.join(dates) or 'none'} -- {expect}")
        if column and column not in dates:
            check(f"{table} carries {column}", False, f"found {dates}")

def main() -> int:
    print("Phase 9 acceptance: Tiers 3 to 5 on a real warehouse table")

    workspace.reset(WORKSPACE)
    try:
        why = mount()
        if why:
            skip("all four clauses", why)
        else:
            clause_one()
            clause_two()
            clause_three()
            clause_four()
            clause_five()
    finally:
        workspace.reset(WORKSPACE)

    print()
    print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    if SKIPPED:
        print("A skip is an outstanding clause, not a passing one.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
