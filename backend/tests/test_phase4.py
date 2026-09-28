"""Phase 4 acceptance test. Run from the repo root:

    uv run python tests/test_phase4.py

Asserts the three Done-When clauses from the build guide, and nothing else. It
is not a unit suite -- `uv run pytest tests/ -q` is that. This answers one
question: is Phase 4 finished?

    1. run_analysis with no contract returns a refusal naming the exact next
       call
    2. get_workflow_state shows every dataset with load timestamps
    3. the live agent recovers on its first retry, with no apology loop

Clause 3 cannot be asserted by a script -- it is about what a model does when
it reads a string, and the guide's own warning applies: test recovery with the
actual AI, not the Inspector. What IS asserted here is everything the recovery
depends on, so that a live failure can be attributed. If all of clause 3's
machinery passes and the agent still loops, the problem is the wording; if the
machinery fails, the wording never had a chance.

One of those machinery checks is worth naming: the refusal's NEXT STEP must
name a tool that is actually registered in server.py. A refusal that instructs
the agent to call something which does not exist is the worst available
failure, because it looks like a well-formed refusal and is unrecoverable.
That check is not in the Done-When. It should be.

Uses its own workspace, 'phase4_test', so nothing loaded in 'local' is touched.
"""

from __future__ import annotations

import ast
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.contract import store, tools  # noqa: E402
from backend.engine.contract.refusals import Reason, reason_of  # noqa: E402
from backend.engine.ingest.csv_loader import load_csv  # noqa: E402
from backend.engine.state import dataset_states  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "phase4_test"
FIXTURES = Path("tests/fixtures")
SERVER = Path("engine/server.py")

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def registered_tools() -> set[str]:
    """Every @mcp.tool in server.py, read from source rather than imported."""
    if not SERVER.exists():
        return set()
    tree = ast.parse(SERVER.read_text())
    out = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                out.add(node.name)
    return out


_CALL_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(")


def calls_named_in(text: str) -> list[str]:
    """Function-call-looking names in a refusal's NEXT STEP line."""
    for line in text.splitlines():
        if line.startswith("NEXT STEP:"):
            return _CALL_RE.findall(line)
    return []


ANSWERS = dict(
    grain="one row = one order",
    measure_definitions={
        "units": "items ordered on this line",
        "unit_price": "price per item in INR, excludes tax",
        "revenue": "units x unit_price, excludes tax and freight",
    },
    aggregations={"units": "sum", "unit_price": "none", "revenue": "sum"},
    analysis_window_start="2024-01-01",
    analysis_window_end="2024-12-30",
)


def clause_1_gate(con) -> None:
    heading("Clause 1: run_analysis refuses, naming the exact next call")

    refusal = tools.analyse(con, "clean_sales", question="revenue by region")
    check("it refuses", reason_of(refusal) is Reason.NO_CONTRACT,
          str(reason_of(refusal)))
    check("the refusal says what was blocked", refusal.startswith("BLOCKED:"))
    check("it explains why it matters", "double-counts" in refusal)
    check("it reports the current state",
          "loaded (500 rows, 8 columns), no contract" in refusal)

    named = calls_named_in(refusal)
    check("NEXT STEP names a call", bool(named), ", ".join(named))
    check("that call is propose_dataset_contract",
          "propose_dataset_contract" in named)
    check("the call carries the dataset name",
          'dataset_name="clean_sales"' in refusal)

    tools_in_server = registered_tools()
    if tools_in_server:
        unknown = [n for n in named if n not in tools_in_server]
        check("every call it names is a registered tool", not unknown,
              ", ".join(unknown) or "all known")
    else:
        print("  SKIP  server.py not found; cannot check the named tool exists")

    check("an unloaded dataset refuses differently",
          reason_of(tools.analyse(con, "nope")) is Reason.DATASET_NOT_LOADED)


def clause_2_state(con) -> None:
    heading("Clause 2: get_workflow_state shows every dataset with load times")

    # The load time is whatever db.get_dataset(...).age_phrase() returns, and
    # its wording belongs to util/db.py. Asserting on the literal word
    # "Loaded" hard-codes another module's prose into this file -- which is
    # exactly what it did on the first run, and it failed on a machine where
    # that phrase is worded differently. What Phase 4 is responsible for is
    # that the phrase REACHES the report, whatever it says.
    #
    # Recomputed once if they disagree: both calls ask the clock, so a phrase
    # like "3 minutes ago" can tick over between them. Twice is enough; a
    # genuine failure fails both times.
    for _ in range(2):
        states = dataset_states(con)
        text = tools.workflow_state(con)
        if all(s.age_phrase in text for s in states):
            break

    check("both datasets are listed",
          "clean_sales" in text and "leftover" in text)
    no_record = [s.dataset_name for s in states if "No load record" in s.age_phrase]
    check("every dataset has a load record", not no_record,
          ", ".join(no_record) or "all recorded")
    check("each dataset's load time reaches the report",
          all(s.age_phrase in text for s in states),
          " | ".join(s.age_phrase for s in states))
    check("each dataset names where it came from",
          all(s.source in text for s in states))
    check("an uncontracted dataset names the next call",
          'propose_dataset_contract(dataset_name="clean_sales")' in text)
    check("the summary counts what needs attention",
          "have no contract" in text)
    check("bookkeeping tables are not listed as datasets",
          store.CONTRACT_TABLE not in text and db.METADATA_TABLE not in text)


def clause_2b_after_confirming(con) -> None:
    heading("Clause 2 continued: the report tracks a contract once it exists")

    proposal = tools.propose(con, "clean_sales")
    check("a first proposal cannot be confirmed",
          "CANNOT BE CONFIRMED" in proposal)
    check("it asks about the grain",
          any("what one row MEANS" in q for q in proposal.splitlines()))
    check("it does not hand back JSON to confirm", "```json" not in proposal)

    answered = tools.propose(con, "clean_sales", **ANSWERS)
    confirmable = "```json" in answered
    check("answering makes it confirmable", confirmable)
    if not confirmable:
        # Print what is still outstanding rather than crashing on the missing
        # JSON block. The first version indexed straight into the split and
        # raised IndexError, which says nothing about WHY -- and this check
        # failing means the answers did not cover everything the proposal
        # asked, which is precisely the thing worth seeing.
        print()
        print("        still provisional. What it is still asking:")
        asking = False
        for line in answered.splitlines():
            if line.startswith("PROVISIONAL"):
                print(f"          {line.strip()}")
            if line.startswith("Put these to the user:"):
                asking = True
                continue
            if asking:
                if not line.strip():
                    break
                print(f"          {line.strip()}")
        print()
        return
    payload = answered.split("```json")[1].split("```")[0]

    stored = tools.confirm(con, payload, export_root=Path("docs/contracts"))
    check("it stores as version 1", "version 1" in stored, stored.splitlines()[0])

    text = tools.workflow_state(con)
    check("the report shows the contract version", "contract v1, ready" in text)
    check("and points at run_analysis",
          'run_analysis(dataset_name="clean_sales"' in text)

    result = tools.analyse(con, "clean_sales", question="revenue by region")
    check("run_analysis no longer refuses", reason_of(result) is None)
    check("it names the contract it would compute under",
          "Under contract v1 for clean_sales" in result)
    check("it is honest that nothing was computed",
          "No analysis has been computed" in result)
    check("the aggregation the user chose survived",
          "| unit_price | none |" in result)
    check("an unstated aggregation would have blocked it",
          "measures[unit_price].agg" in tools.propose(con, "clean_sales"))


def clause_3_machinery(con) -> None:
    heading("Clause 3: everything a first-retry recovery depends on")

    refusals = {
        "no contract": tools.analyse(con, "leftover_from_another_chat"),
        "not loaded": tools.analyse(con, "nope"),
        "bad window": tools.propose(
            con, "clean_sales", analysis_window_start="2024-01-01"
        ),
        "key that does not hold": tools.propose(
            con, "clean_sales", primary_key=["region"]
        ),
    }
    tools_in_server = registered_tools()

    for label, text in refusals.items():
        code = reason_of(text)
        check(f"{label}: carries a reason code", code is not None,
              str(code))
        check(f"{label}: names a call with arguments",
              bool(calls_named_in(text)))
        if tools_in_server:
            unknown = [
                n for n in calls_named_in(text) if n not in tools_in_server
            ]
            check(f"{label}: the call it names exists", not unknown,
                  ", ".join(unknown) or "all known")

    check("a successful result carries no reason code",
          reason_of(tools.analyse(con, "clean_sales")) is None)
    check("the four contract tools are registered",
          {"propose_dataset_contract", "confirm_dataset_contract",
           "get_workflow_state", "run_analysis"} <= tools_in_server
          if tools_in_server else False,
          "server.py not found" if not tools_in_server else "")


def clause_3b_history(con) -> None:
    heading("Clause 3 continued: a changed definition supersedes, never replaces")

    changed = dict(ANSWERS)
    changed["measure_definitions"] = dict(ANSWERS["measure_definitions"])
    changed["measure_definitions"]["revenue"] = "units x unit_price, INCLUDES tax"
    answered = tools.propose(con, "clean_sales", **changed)
    if "```json" not in answered:
        check("the revised contract is confirmable", False,
              "see Clause 2 continued")
        return
    payload = answered.split("```json")[1].split("```")[0]
    text = tools.confirm(con, payload, export_root=Path("docs/contracts"))

    check("it stores as version 2", "version 2" in text)
    check("it names what changed",
          "measures[revenue].definition" in text)

    history = store.history(con, "clean_sales")
    check("both versions are kept", len(history) == 2, f"{len(history)} rows")
    check("exactly one is current",
          sum(1 for h in history if h.is_current) == 1)
    check("version 1 keeps its span",
          history[1].valid_to is not None and history[1].valid_to == history[0].valid_from)

    export = Path("docs/contracts/clean_sales.yaml")
    check("the export exists for version control", export.exists(), str(export))
    if export.exists():
        body = export.read_text()
        check("it says not to edit it", "DO NOT EDIT" in body)
        check("it carries the current definition", "INCLUDES tax" in body)


def main() -> int:
    print(f"Phase 4 acceptance test -- workspace {WORKSPACE!r}")

    fixture = FIXTURES / "clean_sales.csv"
    if not fixture.exists():
        print("\nBLOCKED: fixtures are missing.\n"
              "NEXT STEP: uv run python tests/fixtures/make_fixtures.py")
        return 1

    workspace.reset(WORKSPACE)
    con = db.connect(WORKSPACE)
    try:
        load_csv(con, str(fixture), "clean_sales")
        load_csv(con, str(fixture), "leftover_from_another_chat")

        clause_1_gate(con)
        clause_2_state(con)
        clause_2b_after_confirming(con)
        clause_3_machinery(con)
        clause_3b_history(con)
    finally:
        con.close()
        workspace.reset(WORKSPACE)

    print()
    print("=" * 46)
    print(f"  {PASSED} passed, {FAILED} failed")
    print("=" * 46)
    print()
    print("Phase 4 Done-When: "
          + ("ALL CLAUSES PASS" if FAILED == 0 else "NOT MET"))
    print()
    print("Clause 3's live half is not asserted here. Run it in Claude "
          "Desktop\nand record the result in the step guide -- the Inspector "
          "cannot show\nyou whether the model reads a refusal and does the "
          "right thing next.")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
