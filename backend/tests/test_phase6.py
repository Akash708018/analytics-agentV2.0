"""Phase 6 acceptance test. Run from the repo root:

    uv run python tests/test_phase6.py

Asserts the Done-When from the build guide:

    run on mixed_types.xlsx, approve 3 of 6, the ledger shows exactly those 3
    with correct counts, the other 3 provably did not run, and a text->decimal
    conversion succeeds (which it would not under UPDATE)

and four more the Done-When does not cover, each of which is a failure this
phase actually produced and then fixed:

    4. every refusal's NEXT STEP is a call that WORKS. Not that it parses --
       Refusal.__post_init__ already checks for parentheses. Two of the five
       refusals in clean/tools.py named a call that contradicted the message
       above it, and one of those was live for two steps.
    5. the suggested call in a proposal is one the tool accepts, and excludes
       both the lossy action and any conversion that would pre-empt a
       normalisation. The first fix for this returned the identical suggestion.
    6. a failed apply leaves the dataset AND the ledger exactly as they were.
    7. the proposal connection cannot write, asserted against the engine rather
       than against the code's intentions.

Clause 1 uses the real fixture, whose counts were established in Step 2 and are
pinned by tests/test_mixed_types_ground_truth.py. Clauses 4 to 7 build their own
table, so they run whether or not the xlsx is there.

Uses its own workspace, 'phase6_test', so nothing in 'local' is touched.

**And unlike Phase 5's acceptance test it holds no connection open.** That one
opened a `con` in main() and passed it to every clause. Here that deadlocks:
propose_cleaning_plan takes the workspace file through ATTACH (READ_ONLY), and
DuckDB permits one handle per file per process, so a script holding a writable
connection makes every proposal fail with `Unique file handle conflict`. The
guard this phase built is the reason its own acceptance test has a different
shape, which is worth knowing before wondering why `look()` exists.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.clean import apply as apply_module  # noqa: E402
from backend.engine.clean import detect, ledger, tools  # noqa: E402
from backend.engine.clean.plan import ActionKind, CleaningAction, latest  # noqa: E402
from backend.engine.contract.refusals import reason_of  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "phase6_test"
FIXTURE = Path("tests/fixtures/mixed_types.xlsx")
SERVER = Path("engine/server.py")

PASSED = 0
FAILED = 0
SKIPPED = 0

SYNTHETIC = """
CREATE OR REPLACE TABLE synth AS SELECT * FROM (VALUES
  ('ORD-01', 'North',  '10',  '9.50'),
  ('ORD-02', 'North ', '20',  '10.00'),
  ('ORD-03', 'north',  'n/a', '11.00'),
  ('ORD-04', 'South',  '40',  'not priced'),
  ('ORD-05', 'N/A',    '50',  '13.00'),
  ('ORD-06', 'South',  '60',  '14.00'),
  ('ORD-07', 'East',   '70',  '15.75'),
  ('ORD-08', 'East',   '80',  '16.00'),
  ('ORD-09', 'West',   '90',  '17.50'),
  ('ORD-10', 'West',   '11',  '18.00')
) t(order_id, region, units, unit_price)
"""


def look(query: str, params: list | None = None):
    """One query on a short-lived connection.

    Never held across a tool call: see the module docstring.
    """
    con = db.connect(WORKSPACE)
    try:
        return con.execute(query, params or []).fetchall()
    finally:
        con.close()


def scalar(query: str, params: list | None = None):
    rows = look(query, params)
    return rows[0][0] if rows else None


def run(statement: str) -> None:
    con = db.connect(WORKSPACE)
    try:
        con.execute(statement)
    finally:
        con.close()


def plan_of(dataset: str):
    con = db.connect(WORKSPACE)
    try:
        return latest(con, dataset)
    finally:
        con.close()


def ledger_of(dataset: str):
    con = db.connect(WORKSPACE)
    try:
        return ledger.entries(con, dataset), ledger.count(con, dataset)
    finally:
        con.close()


def types_of(table: str) -> dict[str, str]:
    return dict(look(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position", [table]))


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


def registered_tools() -> set[str]:
    """Every @mcp.tool in server.py, read from source rather than imported.

    Phase 5's trick, and it earned its place here: when the cleaning tools were
    registered, Claude Desktop could not see them because its server process
    predated the edit. Parsing the file answers "is it registered" without
    asking a running process anything.
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


def call_named_in(text: str) -> tuple[str, list[str]] | None:
    """The apply call a refusal or proposal printed, parsed back out.

    Not a call built some other way: the point of clause 4 is that the string
    the agent is handed is the string that works.
    """
    for line in text.splitlines():
        if "approved_action_ids=[" not in line:
            continue
        dataset = line.split('dataset_name="', 1)[1].split('"', 1)[0]
        ids = line.split("approved_action_ids=[", 1)[1].split("]", 1)[0]
        return dataset, [p.strip(' "') for p in ids.split(",") if p.strip(' "')]
    return None


# --------------------------------------------------------------------------
# Clause 1 to 3: the Done-When
# --------------------------------------------------------------------------


def clause_1_the_done_when() -> None:
    heading("Clauses 1-3: propose six, approve three, and prove the other three")

    if not FIXTURE.exists():
        skip("mixed_types.xlsx", f"{FIXTURE} not found; the Done-When names it")
        return

    from backend.engine.ingest.excel import load_excel

    print(f"  loading {FIXTURE.name} with all_text...", flush=True)
    con = db.connect(WORKSPACE)
    try:
        load_excel(con, str(FIXTURE), "mixed", all_text=True)
    finally:
        con.close()

    before = types_of("mixed")
    check("every column loads as text", set(before.values()) == {"VARCHAR"},
          ", ".join(sorted(set(before.values()))))

    text = tools.propose_cleaning_plan(WORKSPACE, "mixed")
    plan = plan_of("mixed")
    check("six changes are proposed", plan is not None and len(plan.actions) == 6,
          f"{len(plan.actions) if plan else 0}")
    check("nothing is changed by proposing", "Nothing has been changed" in text)

    price = next((a for a in plan.actions if a.column == "unit_price"), None)
    check("the price column is offered as a decimal, not an integer",
          price is not None and "DECIMAL" in price.intent,
          price.intent if price else "")
    check("its three undeclared values are counted and shown",
          price is not None and price.values_lost == 3
          and price.sample == ("not priced",),
          f"{price.values_lost if price else '-'} lost")

    units = next((a for a in plan.actions
                  if a.column == "units"
                  and a.kind is ActionKind.CONVERT_TYPE), None)
    check("the declared tokens in units cost nothing",
          units is not None and units.values_lost == 0)

    approved = [a.action_id for a in plan.actions
                if a.column in ("order_date", "unit_price", "is_return")]
    check("three of the six are approvable together", len(approved) == 3,
          ", ".join(approved))

    applied_text = tools.apply_cleaning_plan(WORKSPACE, "mixed", approved)
    check("the apply does not refuse", reason_of(applied_text) is None,
          str(reason_of(applied_text) or ""))

    entries, _ = ledger_of("mixed")
    check("the ledger holds exactly the three approved",
          {e.action_id for e in entries} == set(approved),
          ", ".join(sorted(e.action_id for e in entries)))
    check("every ledger row names the table as it was before",
          all(e.history_table == "_agent_history_mixed_v1" for e in entries))
    check("the row count is unchanged and the ledger says so",
          all(e.rows_before == e.rows_after == 6000 for e in entries))

    after = types_of("mixed")
    check("the text to decimal conversion succeeded",
          after["unit_price"].startswith("DECIMAL"), after["unit_price"])
    check("the three that were not approved left no trace",
          after["units"] == "VARCHAR" and after["region"] == "VARCHAR",
          f"units {after['units']}, region {after['region']}")
    check("the summary names the three that did not run",
          "3 action(s) were not approved" in applied_text)

    kept = scalar("SELECT count(*) FROM _agent_history_mixed_v1 "
                  "WHERE unit_price = 'not priced'")
    check("the discarded values are still readable in the snapshot", kept == 3,
          f"{kept} row(s)")


# --------------------------------------------------------------------------
# Clause 4: a refusal's NEXT STEP is a call that works
# --------------------------------------------------------------------------


def fresh_synth() -> None:
    """A clean table and a fresh plan for it."""
    run(SYNTHETIC)
    tools.propose_cleaning_plan(WORKSPACE, "synth")


def clause_4_refusals_recommend_working_calls() -> None:
    heading("Clause 4: every refusal names a call that works")

    for label, ids_of in (
        ("nothing approved", lambda p: []),
        ("conflicting ids",
         lambda p: [a.action_id for a in p.actions if a.column == "units"]),
    ):
        fresh_synth()
        text = tools.apply_cleaning_plan(
            WORKSPACE, "synth", ids_of(plan_of("synth"))
        )
        code = reason_of(text)
        check(f"{label}: carries a reason code", code is not None, str(code))
        parsed = call_named_in(text)
        if parsed is None:
            check(f"{label}: names an apply call", False)
            continue
        _, ids = parsed
        check(f"{label}: names ids", bool(ids), ", ".join(ids))
        result = tools.apply_cleaning_plan(WORKSPACE, "synth", ids)
        check(f"{label}: the call it names is not itself refused",
              reason_of(result) is None, str(reason_of(result) or ""))

    fresh_synth()
    plan = plan_of("synth")
    units = [a.action_id for a in plan.actions if a.column == "units"]
    conflict = tools.apply_cleaning_plan(WORKSPACE, "synth", units)
    named = call_named_in(conflict)
    normalise = next(a.action_id for a in plan.actions
                     if a.column == "units"
                     and a.kind is ActionKind.NORMALISE_MISSING)
    check("the conflict refusal recommends the id its own message names",
          named is not None and named[1] == [normalise],
          f"named {named[1] if named else '-'}, message says {normalise}")


# --------------------------------------------------------------------------
# Clause 5: the suggestion is safe and accepted
# --------------------------------------------------------------------------


def clause_5_the_suggestion() -> None:
    heading("Clause 5: the suggested call is one the tool accepts")

    run(SYNTHETIC)
    text = tools.propose_cleaning_plan(WORKSPACE, "synth")
    plan = plan_of("synth")
    parsed = call_named_in(text)
    if parsed is None:
        check("the proposal suggests a call", False)
        return

    _, ids = parsed
    lossy = {a.action_id for a in plan.actions if a.is_lossy}
    normalised = {a.column for a in plan.actions
                  if a.kind is ActionKind.NORMALISE_MISSING}
    pre_empting = {a.action_id for a in plan.actions
                   if a.kind is ActionKind.CONVERT_TYPE and a.column in normalised}

    check("it suggests nothing that discards", not (set(ids) & lossy),
          ", ".join(sorted(set(ids) & lossy)) or "none")
    check("it suggests no conversion that pre-empts a normalisation",
          not (set(ids) & pre_empting),
          ", ".join(sorted(set(ids) & pre_empting)) or "none")
    check("a deferred action is named rather than silently dropped",
          not pre_empting or "left for the next round" in text)

    result = tools.apply_cleaning_plan(WORKSPACE, "synth", ids)
    check("the tool accepts its own suggestion", reason_of(result) is None,
          str(reason_of(result) or ""))


# --------------------------------------------------------------------------
# Clause 6: a failed apply changes nothing at all
# --------------------------------------------------------------------------


def clause_6_all_or_nothing() -> None:
    heading("Clause 6: a failed apply leaves the table and the ledger alone")

    run(SYNTHETIC)
    con = db.connect(WORKSPACE)
    try:
        actions = detect.detect(con, source="synth", target="synth",
                                missing_tokens=["", "NA", "N/A", "null", "None"])
    finally:
        con.close()
    good = next(a for a in actions
                if a.kind is ActionKind.CONVERT_TYPE and a.column == "units")
    broken = CleaningAction(
        action_id="C099", kind=ActionKind.TRIM_WHITESPACE, column="unit_price",
        intent="a statement that cannot run",
        sql='CREATE OR REPLACE TABLE "synth" AS SELECT * REPLACE '
            '(no_such_function("unit_price") AS "unit_price") FROM "synth"',
    )
    _, before_ledger = ledger_of("synth")
    before_type = types_of("synth")["units"]
    con = db.connect(WORKSPACE)
    try:
        # Not zero: clauses 4 and 5 applied cleans in this same workspace, so
        # snapshots already exist. The claim is that a FAILED apply adds none,
        # which is a different assertion from "there are none".
        before_snapshots = apply_module.history_tables(con, "synth")
    finally:
        con.close()

    failed = False
    con = db.connect(WORKSPACE)
    try:
        apply_module.apply(con, dataset_name="synth", actions=[good, broken],
                           plan_id="acceptance")
    except Exception:  # noqa: BLE001
        failed = True
    finally:
        con.close()

    check("the apply failed", failed)
    after_type = types_of("synth")["units"]
    check("the table is exactly as it was", after_type == before_type,
          f"{before_type} -> {after_type}")
    con = db.connect(WORKSPACE)
    try:
        snapshots = apply_module.history_tables(con, "synth")
    finally:
        con.close()
    check("the failed apply left no new snapshot", snapshots == before_snapshots,
          f"{len(before_snapshots)} before, {len(snapshots)} after")
    _, after_ledger = ledger_of("synth")
    check("the ledger records nothing", after_ledger == before_ledger)


# --------------------------------------------------------------------------
# Clause 7: the proposal path cannot write
# --------------------------------------------------------------------------


def clause_7_read_only() -> None:
    heading("Clause 7: the proposal connection cannot write")

    ro = tools.connect_read_only(WORKSPACE)
    try:
        rows = ro.execute("SELECT count(*) FROM synth").fetchone()[0]
        check("it can read", rows > 0, f"{rows} row(s)")
        for statement in (
            "CREATE TABLE evil AS SELECT 1",
            "INSERT INTO synth VALUES ('x','y','z','w')",
            "DROP TABLE synth",
        ):
            refused = False
            try:
                ro.execute(statement)
            except Exception:  # noqa: BLE001
                refused = True
            check(f"it refuses {statement.split()[0]}", refused)
    finally:
        ro.close()


def clause_7b_registration() -> None:
    heading("Clause 7 continued: the three tools are registered")

    in_server = registered_tools()
    if not in_server:
        skip("registration", "server.py not found")
        return
    expected = {"propose_cleaning_plan", "apply_cleaning_plan",
                "get_cleaning_ledger"}
    missing = sorted(expected - in_server)
    check("all three are declared in server.py", not missing,
          ", ".join(missing) or "all three")

    docs = Path("tests/test_tool_docs.py")
    if docs.exists():
        text = docs.read_text()
        absent = sorted(t for t in expected if f'"{t}"' not in text)
        check("all three are in the closed set", not absent,
              ", ".join(absent) or "all three")
    else:
        skip("closed set", "tests/test_tool_docs.py not found")


def main() -> int:
    print(f"Phase 6 acceptance test -- workspace {WORKSPACE!r}")

    workspace.reset(WORKSPACE)
    try:
        clause_1_the_done_when()
        clause_4_refusals_recommend_working_calls()
        clause_5_the_suggestion()
        clause_6_all_or_nothing()
        clause_7_read_only()
        clause_7b_registration()
    finally:
        workspace.reset(WORKSPACE)

    print()
    print("=" * 56)
    print(f"  {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    print("=" * 56)
    print()
    print("Phase 6 Done-When: "
          + ("ALL CLAUSES PASS" if FAILED == 0 else "NOT MET"))
    if SKIPPED:
        print()
        print("Skips are not passes. mixed_types.xlsx carries the Done-When's")
        print("own numbers; without it clauses 1 to 3 are unasserted.")
    print()
    print("The live half is not asserted here. Whether the model reads the")
    print("discard warning before approving is the one thing no script can")
    print("tell you, and it is what the approval gate exists for.")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
