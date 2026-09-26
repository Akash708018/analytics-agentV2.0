"""Phase 7 acceptance test. Run from the repo root:

    uv run python tests/test_phase7.py

Asserts the Done-When from the build guide:

    a clean pass/fail table, and a correct failure on a deliberately broken
    fixture

and three more the Done-When does not cover, each of which is a decision this
phase had to make and could get wrong silently:

    3. confirming a contract does not check the data -- the GATE refuses a key
       that does not hold and the REPORT explains it, and both are correct at
       once. P7-D11 is only true if this holds.
    4. every refusal carries a reason code and names a registered tool.
    5. validating changes nothing, asserted against the engine rather than
       against the code's intentions.

**Both clauses build a REAL confirmed contract** -- proposed values, a binding,
`store.confirm` -- rather than patching `contract_store.current` the way the
unit tests do. A pass/fail table rendered from a stand-in object proves the
renderer works and says nothing about the path a person actually takes. It also
exercises Phase 4 for free: a contract that cannot be confirmed cannot be
validated, and this script would fail at the confirm rather than at the check.

Every number clause 2 asserts is pinned twice already: against the file by
tests/test_broken_sales_ground_truth.py, against the rules by
tests/test_validate_rules.py. What this adds is the third thing -- that those
numbers reach the page a person reads.

Uses its own workspace, 'phase7_test', so nothing in 'local' is touched.

**It holds no connection open across a tool call**, for the reason Phase 6's
script records: validate_dataset takes the workspace file through
ATTACH (READ_ONLY), and DuckDB permits one handle per file per process.
"""

from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.contract import ContractRefused, store  # noqa: E402
from backend.engine.contract.dataset_contract import (  # noqa: E402
    AnalysisWindow,
    Binding,
    DatasetContract,
    ForeignKey,
    Measure,
)
from backend.engine.contract.refusals import Reason, reason_of  # noqa: E402
from backend.engine.ingest.csv_loader import load_csv  # noqa: E402
from backend.engine.profile.runs import is_bookkeeping  # noqa: E402
from backend.engine.state import describe_workflow_state, require_contract  # noqa: E402
from backend.engine.util import db  # noqa: E402
from backend.engine.validate import runs, tools  # noqa: E402

WORKSPACE = "phase7_test"
CLEAN = Path("tests/fixtures/clean_sales.csv")
BROKEN = Path("tests/fixtures/broken_sales.csv")
SERVER = Path("engine/server.py")

# Fixed so "in the future" means the same thing on every run. The fixtures stop
# in 2025; anything later would do.
TODAY = date(2026, 1, 1)
WINDOW = AnalysisWindow(start=date(2024, 1, 1), end=date(2024, 12, 31))

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


def look(query: str, params: list | None = None):
    """One query on a short-lived connection. Never held across a tool call."""
    con = db.connect(WORKSPACE)
    try:
        return con.execute(query, params or []).fetchall()
    finally:
        con.close()


def scalar(query: str, params: list | None = None):
    rows = look(query, params)
    return rows[0][0] if rows else None


def registered_tools() -> set[str]:
    """Every @mcp.tool in server.py, read from source rather than imported.

    Phase 5's trick and Phase 6's reason for keeping it: when a tool is
    registered, a running Claude Desktop cannot see it until the process
    restarts. Parsing the file answers "is it registered" without asking a
    running process anything.
    """
    if not SERVER.exists():
        return set()
    out = set()
    for node in ast.walk(ast.parse(SERVER.read_text())):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                out.add(node.name)
    return out


def load(fixture: Path, dataset_name: str) -> None:
    con = db.connect(WORKSPACE)
    try:
        load_csv(con, str(fixture), dataset_name)
    finally:
        con.close()


def confirm_contract(
    dataset_name: str,
    *,
    primary_key: list[str],
    date_column: str,
    row_count: int | None = None,
    foreign_keys: list | None = None,
    domains: dict | None = None,
):
    """A real contract, confirmed through the real store.

    `row_count` overrides what the binding records, which is how clause 2
    reaches the table.row_count check without waiting for somebody to delete
    rows: the contract is agreed at a count the table does not hold.
    """
    con = db.connect(WORKSPACE)
    try:
        pairs = [
            (r[0], r[1])
            for r in con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [dataset_name],
            ).fetchall()
        ]
        rows = con.execute(f'SELECT count(*) FROM "{dataset_name}"').fetchone()[0]
        contract = DatasetContract(
            dataset_name=dataset_name,
            grain=f"one row = one {dataset_name} order",
            primary_key=primary_key,
            date_column=date_column,
            analysis_window=WINDOW,
            measures=[
                Measure(
                    name="revenue",
                    agg="sum",
                    definition="units x unit_price, gross of tax",
                    unit="GBP",
                )
            ],
            dimensions=["region", "channel"],
            bound_to=Binding.from_pairs(pairs, row_count or rows),
            foreign_keys=list(foreign_keys or []),
            domains=dict(domains or {}),
        )
        return store.confirm(con, contract)
    finally:
        con.close()


def table_row(text: str, title: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"| {title} |"):
            return line
    return ""


# --------------------------------------------------------------------------
# Clause 1: a clean pass/fail table
# --------------------------------------------------------------------------


def clause_one() -> str:
    heading("Clause 1: a clean dataset produces a clean pass/fail table")

    if not CLEAN.exists():
        skip("clean_sales", f"{CLEAN} not generated; see make_fixtures.py")
        return ""

    load(CLEAN, "clean_sales")
    stored = confirm_contract(
        "clean_sales", primary_key=["order_id"], date_column="order_date"
    )
    check("a contract confirms against the fixture", stored.version == 1,
          f"v{stored.version}, {stored.row_count:,} rows, {stored.fingerprint}")

    text = tools.validate_dataset(WORKSPACE, "clean_sales", today=TODAY)

    check("it does not refuse", reason_of(text) is None)
    check("the headline says every check passed",
          "clean_sales: all 6 check(s) passed." in text,
          "six, not eight: this contract declares no reference and no domain")
    check("it names the contract it checked against",
          "Checked against contract v1." in text)
    check("the table has a header row",
          "| check | on | result | rows | passed | failed | not checked |" in text)

    for title in (
        "Primary key is unique",
        "Primary key is present",
        "Every row is dated",
        "Dates fall in the analysis window",
        "No row is dated in the future",
        "Row count matches the contract",
    ):
        check(f"a row for {title.lower()}", bool(table_row(text, title)))

    key_row = table_row(text, "Primary key is unique")
    check("every row is accounted for", "| 500 | 500 | 0 | 0 |" in key_row,
          key_row.strip())
    check("nothing was left unchecked", " 0 |" in key_row and "PASS" in key_row)

    check("a table-level check prints dashes rather than zeros",
          table_row(text, "Row count matches the contract").endswith(
              "| - | - | - | - |"))

    check("it points at analysis",
          'NEXT STEP: call run_analysis(dataset_name="clean_sales")' in text)
    check("it does not point at the contract",
          "propose_dataset_contract" not in text)
    return text


# --------------------------------------------------------------------------
# Clause 2: a correct failure on the broken fixture
# --------------------------------------------------------------------------


def clause_two() -> str:
    heading("Clause 2: the broken fixture fails, in the exact places it is broken")

    if not BROKEN.exists():
        skip("broken_sales", f"{BROKEN} not generated; see make_fixtures.py")
        return ""

    load(BROKEN, "broken_sales")
    load(Path("tests/fixtures/region_lookup.csv"), "region_lookup")
    # Agreed at 200 rows against a table holding 186, so the row-count check
    # has something to find. Every other count comes from the fixture.
    stored = confirm_contract(
        "broken_sales", primary_key=["order_id"], date_column="order_ts",
        row_count=200,
        foreign_keys=[ForeignKey(columns=["region"],
                                 references="region_lookup")],
        domains={"channel": ["Online", "Retail", "Wholesale"]},
    )
    check("a contract confirms against a broken table", stored.version == 1,
          "confirming does not look at the data")

    text = tools.validate_dataset(WORKSPACE, "broken_sales", today=TODAY)

    check("it does not refuse", reason_of(text) is None)
    check("the headline counts rather than collapsing",
          "broken_sales: 7 of 8 check(s) failed, 1 passed." in text)
    check("it does not say FAIL as a verdict",
          not text.startswith("FAIL"))

    check("duplicate keys: 178 passed, 6 failed, 2 not checked",
          "| 186 | 178 | 6 | 2 |" in table_row(text, "Primary key is unique"))
    check("missing keys: 184 passed, 2 failed",
          "| 186 | 184 | 2 | 0 |" in table_row(text, "Primary key is present"))
    check("undated rows: 180 passed, 6 failed",
          "| 186 | 180 | 6 | 0 |" in table_row(text, "Every row is dated"))
    check("the window: 171 in, 9 out, 6 undated",
          "| 186 | 171 | 9 | 6 |" in table_row(
              text, "Dates fall in the analysis window"))
    check("nothing is dated in the future",
          "PASS" in table_row(text, "No row is dated in the future"))

    check("the last second of the last day is inside the window",
          "| 186 | 171 |" in table_row(text, "Dates fall in the analysis window"),
          "170 would mean the bound was written <= end")

    check("orphan references: 171 passed, 7 failed, 8 not checked",
          "| 186 | 171 | 7 | 8 |" in table_row(
              text, "Every reference points at a row"))
    check("values outside the declared set: 177 passed, 9 failed",
          "| 186 | 177 | 9 | 0 |" in table_row(
              text, "Values are inside the declared set"))

    check("it names the regions the lookup does not have",
          "Nord (3 row(s)) is not in region_lookup" in text)
    check("it names the channels the contract does not list",
          "Partner (5 row(s))" in text)

    check("it names the keys that repeat", "ORD-00011 appears 2 times" in text)
    check("it names the earliest date outside the window",
          "before 2024-01-01, earliest 2023-04-04" in text)
    check("the row count check found the difference",
          "lost 14 row(s)" in text, "agreed at 200, holds 186")

    check("a check that passed still reports what it could not examine",
          "6 row(s) could not be checked and are not counted as passing" in text)
    check("uncheckable rows are not added up", "never added up" in text)

    check("a row-count loss points at the ledger before the contract",
          'NEXT STEP: call get_cleaning_ledger(dataset_name="broken_sales")'
          in text,
          "re-confirming would sign a count nobody can account for")
    check("it does not recommend cleaning", "propose_cleaning_plan" not in text)
    return text


# --------------------------------------------------------------------------
# Clause 3: the gate refuses and the report explains, both correctly
# --------------------------------------------------------------------------


def clause_three() -> None:
    heading("Clause 3: the gate refuses the same table the report explains")

    if not BROKEN.exists():
        skip("broken_sales", "not generated")
        return

    con = db.connect(WORKSPACE)
    try:
        try:
            require_contract(con, "broken_sales")
            refusal = ""
        except ContractRefused as exc:
            refusal = str(exc)
    finally:
        con.close()

    check("the gate refuses this dataset", bool(refusal),
          str(reason_of(refusal)))
    check("it refuses on the key", reason_of(refusal) is Reason.KEY_NOT_UNIQUE)
    check("the refusal does not call a null key a duplicate",
          "repeat" not in refusal or "2 row(s) repeat" not in refusal,
          "P7-D6")

    text = tools.validate_dataset(WORKSPACE, "broken_sales", today=TODAY)
    check("the report does not refuse the same dataset",
          reason_of(text) is None, "P7-D11")
    check("and it reports more than one finding",
          text.count("FAIL") >= 7,
          "the gate names the first thing that stopped it")


# --------------------------------------------------------------------------
# Clause 4: reason codes, and calls that exist
# --------------------------------------------------------------------------


def clause_four() -> None:
    heading("Clause 4: every refusal carries a code and names a registered tool")

    known = registered_tools()
    if known:
        check("validate_dataset is registered", "validate_dataset" in known,
              f"{len(known)} tool(s) in server.py")
    else:
        skip("the tool surface", f"{SERVER} not found; run from the repo root")

    not_loaded = tools.validate_dataset(WORKSPACE, "nope")
    check("not loaded: carries a reason code",
          reason_of(not_loaded) is Reason.DATASET_NOT_LOADED,
          str(reason_of(not_loaded)))
    check("not loaded: names a call with arguments",
          "NEXT STEP: call list_datasets()" in not_loaded)
    if known:
        check("not loaded: the call it names is registered",
              "list_datasets" in known, "all known")

    load(CLEAN, "uncontracted") if CLEAN.exists() else None
    no_contract = tools.validate_dataset(WORKSPACE, "uncontracted")
    check("no contract: carries a reason code",
          reason_of(no_contract) is Reason.NO_CONTRACT,
          str(reason_of(no_contract)))
    check("no contract: names propose_dataset_contract",
          "propose_dataset_contract" in no_contract)
    if known:
        check("no contract: the call it names is registered",
              "propose_dataset_contract" in known, "all known")
    check("no contract: it does not render an empty table",
          "| check | on | result |" not in no_contract,
          "a page of NOT RUN is not a report")


# --------------------------------------------------------------------------
# Clause 5: it records the run, reports the stage, and changes nothing
# --------------------------------------------------------------------------


def clause_five() -> None:
    heading("Clause 5: the run is recorded, reported, and nothing is written")

    if not BROKEN.exists():
        skip("broken_sales", "not generated")
        return

    before_tables = sorted(t[0] for t in look(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'"))
    before_rows = scalar("SELECT count(*) FROM broken_sales")

    tools.validate_dataset(WORKSPACE, "broken_sales", today=TODAY)

    after_tables = sorted(t[0] for t in look(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'"))
    check("no user table was added or removed",
          [t for t in after_tables if not is_bookkeeping(t)]
          == [t for t in before_tables if not is_bookkeeping(t)])
    check("the table still holds every row it held",
          scalar("SELECT count(*) FROM broken_sales") == before_rows,
          f"{before_rows:,} rows")

    con = db.connect(WORKSPACE)
    try:
        run = runs.latest(con, "broken_sales")
    finally:
        con.close()
    check("the run was recorded", run is not None)
    if run:
        check("it recorded the contract version", run.contract_version == 1)
        check("it recorded the row count it ran at", run.row_count == 186,
              f"{run.row_count} rows")
        check("it recorded the verdict as counts",
              (run.checks_total, run.checks_failed, run.checks_not_run)
              == (8, 7, 0),
              run.verdict_phrase())

    con = db.connect(WORKSPACE)
    try:
        state = describe_workflow_state(con)
        listed = db.user_tables(con)
    finally:
        con.close()
    check("the workflow state reports the validated stage",
          "validated just now" in state)
    check("it does not call a dataset the gate refuses ready",
          "BLOCKED" in state
          and 'run_analysis(dataset_name="broken_sales"' not in state,
          "Step 10b: the state asks the gate")
    check("and it points at the report instead",
          'validate_dataset(dataset_name="broken_sales")' in state)
    check("it names the contract the validation ran against",
          "against contract v1" in state)
    check("it offers the full report",
          'Full report: validate_dataset(dataset_name="broken_sales")' in state)

    check("the validation log is bookkeeping",
          is_bookkeeping(runs.VALIDATION_TABLE), runs.VALIDATION_TABLE)
    check("and is not listed as a dataset",
          runs.VALIDATION_TABLE not in listed,
          ", ".join(listed))


def main() -> int:
    print(f"Phase 7 acceptance test -- workspace {WORKSPACE!r}")
    workspace.reset(WORKSPACE)
    try:
        clause_one()
        clause_two()
        clause_three()
        clause_four()
        clause_five()
    finally:
        workspace.reset(WORKSPACE)

    print()
    print("=" * 54)
    print(f"  {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    print("=" * 54)
    print()
    print("Phase 7 Done-When: "
          + ("ALL CLAUSES PASS" if FAILED == 0 else "NOT MET"))
    if SKIPPED:
        print()
        print("Skips are not passes. A fixture this run did not have covers "
              "something nothing else does; the skip lines say which.")
    print()
    print("The live half is not asserted here. Whether an agent reads a "
          "validation report and acts on the right side of the disagreement -- "
          "the data or the agreement -- is the one thing no script can tell "
          "you, and it is what P7-D12 is about.")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
