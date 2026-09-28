"""Phase 12 acceptance test. Run from the repo root:

    uv run python tests/test_phase12.py

The build guide's Done-When at line 953: **one end-to-end run on merged_multiheader.xlsx
produces a complete document.** This is the whole pipeline in one process -- ingest spec, load,
profile, clean, contract, validate, analyse, chart, report -- and it is the only script here
that runs all of it.

**What "complete" means.** Nine sections with something in each. P12-D11 makes a report with
empty sections still complete in structure, which is right for work half-done; but this script
runs every step, so an empty section means a step did not happen. `Report.empty_sections` is the
assertion, and it should be empty at the end.

**Why it goes through server.py.** C83: eight analyses were uncallable through the MCP surface
while every test passed, because every test reached the analysis through the registry or the
tools layer. A Done-When that did the same would prove the pipeline works for callers who do not
exist. Everything below the load goes through the registered tools.

It needs no Postgres. The fixture is on disk and everything else is DuckDB in the workspace.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.contract.refusals import reason_of  # noqa: E402
from backend.engine.ingest import draft  # noqa: E402
from backend.engine.ingest.excel import load_excel  # noqa: E402
from backend.engine.util import db  # noqa: E402

# P13-O2, and the better answer than the one it proposed. confirm_dataset_contract exports a
# YAML copy of the contract to docs/contracts/, outside the workspace, where it is meant to be
# version controlled -- so a script that confirms a contract dirties the repository (C86). The
# first fix taught one script to restore what it found, and the next script that confirmed a
# contract hit the same thing. contract/tools.confirm reads store.EXPORT_DIR at call time, so
# pointing it at this script's own workspace means nothing outside is ever written. No restore,
# no shared bookkeeping, and less code than either.
WORKSPACE = "phase12_test"
DATASET = "merged_multiheader"
FIXTURE = Path("tests/fixtures/merged_multiheader.xlsx")
QUESTION = "Which region earns the most, and did that change between January and March?"

# Two months inside the data, for the temporal tiers.
BASELINE, PERIOD, GRAIN = "2024-01", "2024-03", "month"

# Every analysis this run makes, in the order a person would. growth_decomposition is here on
# purpose: revenue is DOUBLE, and until C85 this analysis refused every float measure.
ANALYSES = (
    ("summary_stats", {}),
    ("frequency", {"column": "region"}),
    ("top_n", {"dimension": "region", "measure": "revenue", "n": 3}),
    ("calendar_coverage", {"grain": GRAIN}),
    ("correlation", {"measure": "revenue", "against": "units"}),
    ("period_compare", {"measure": "revenue", "period": PERIOD,
                        "baseline": BASELINE, "grain": GRAIN}),
    ("growth_decomposition", {"measure": "revenue", "dimension": "region",
                              "period": PERIOD, "baseline": BASELINE, "grain": GRAIN}),
)

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


def first(text: str) -> str:
    return text.splitlines()[0][:95] if text else "(no reply)"


def column_type(name: str) -> str | None:
    con = db.connect(WORKSPACE)
    try:
        row = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?", [DATASET, name]).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def clause_one() -> bool:
    """The spreadsheet, through the propose/confirm path Phase 3 built."""
    heading("Clause 1: a merged multi-header spreadsheet loads through its ingest spec")

    if not FIXTURE.exists():
        skip("the fixture", f"{FIXTURE} is not on disk; run from the repository root")
        return False

    d = draft.draft_for_path(str(FIXTURE))
    check("a spec is proposed rather than guessed at", d.spec is not None)
    if d.spec is None:
        return False
    check("the header is found across two rows", d.spec.header_rows == [1, 2],
          str(d.spec.header_rows))

    spec = draft.spec_from_json(d.spec.model_dump_json())
    con = db.connect(WORKSPACE)
    try:
        r = load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))
    finally:
        con.close()
    check("150 rows load", r.row_count == 150, f"{r.row_count} rows")
    check("the date arrives as text, which is a fact about spreadsheets",
          column_type("order_date") == "VARCHAR", str(column_type("order_date")))
    return r.row_count == 150


def clause_two(server) -> None:
    heading("Clause 2: profiling, and a cleaning plan the data actually needs")

    text = server.profile_dataset(dataset_name=DATASET, workspace_id=WORKSPACE)
    check("the dataset profiles", reason_of(text) is None, first(text))
    check("the profile names its shape", "150 rows, 8 columns" in text)

    plan = server.propose_cleaning_plan(dataset_name=DATASET, workspace_id=WORKSPACE)
    check("a plan is proposed", reason_of(plan) is None, first(plan))
    check("it offers to read the date column as a date",
          "CONVERT_TYPE on order_date" in plan,
          [ln.strip() for ln in plan.splitlines() if "C001" in ln][:1])

    applied = server.apply_cleaning_plan(
        dataset_name=DATASET, approved_action_ids=["C001"], workspace_id=WORKSPACE)
    check("the approved action applies", reason_of(applied) is None, first(applied))
    check("no row was lost converting a type", "150 row(s) before, 150 after" in applied,
          first(applied))
    check("order_date is now a DATE", column_type("order_date") == "DATE",
          str(column_type("order_date")))


def clause_three(server) -> bool:
    heading("Clause 3: a contract somebody agreed, and validation against it")

    proposal = server.propose_dataset_contract(
        dataset_name=DATASET,
        grain="one row = one order",
        primary_key=["order_id"],
        date_column="order_date",
        measures=["revenue", "units", "unit_price"],
        dimensions=["region", "product", "channel"],
        aggregations={"revenue": "sum", "units": "sum", "unit_price": "none"},
        measure_definitions={
            "revenue": "units x unit_price, gross of tax",
            "units": "items on the order",
            "unit_price": "price of one item; summing it means nothing",
        },
        analysis_window_start="2024-01-01",
        analysis_window_end="2024-12-31",
        caveats=["Loaded from a spreadsheet whose header spans two rows; the group row was "
                 "dropped and the bottom row kept."],
        workspace_id=WORKSPACE,
    )
    check("a contract is proposed", reason_of(proposal) is None, first(proposal))
    check("it is not provisional once the aggregates are stated",
          "PROVISIONAL" not in proposal,
          [ln for ln in proposal.splitlines() if "PROVISIONAL" in ln][:1])

    if "```" not in proposal:
        check("the proposal offers JSON to confirm", False, "no fenced block")
        return False
    body = proposal.rsplit("```", 2)[-2]
    if body.startswith("json"):
        body = body[4:]

    confirmed = server.confirm_dataset_contract(contract_json=body, workspace_id=WORKSPACE)
    check("the contract confirms", reason_of(confirmed) is None, first(confirmed))

    validated = server.validate_dataset(dataset_name=DATASET, workspace_id=WORKSPACE)
    check("the dataset validates against it", reason_of(validated) is None, first(validated))
    return reason_of(confirmed) is None


def clause_four(server) -> None:
    heading("Clause 4: seven analyses and a chart, through the registered tools")

    for name, params in ANALYSES:
        text = server.compute_analysis(
            dataset_name=DATASET, analysis_type=name, workspace_id=WORKSPACE, **params)
        check(f"{name} runs", reason_of(text) is None, first(text))

    chart = server.render_chart(
        dataset_name=DATASET, analysis_type="frequency", chart="bar",
        column="region", workspace_id=WORKSPACE)
    check("a chart renders", reason_of(chart) is None, first(chart))
    check("the chart says what a reader cannot see",
          "You cannot see this image" in chart)


def clause_five(server) -> None:
    """The Done-When itself."""
    heading("Clause 5: one complete document")

    text = server.build_report(
        dataset_name=DATASET, question=QUESTION, workspace_id=WORKSPACE)
    check("the report is written", reason_of(text) is None, first(text))
    if reason_of(text) is not None:
        return

    path = Path(text.splitlines()[0].split("Report written: ", 1)[1].strip())
    check("the file it names exists", path.exists(), str(path))
    if not path.exists():
        return
    body = path.read_text(encoding="utf-8")

    from backend.engine.report.assemble import SECTIONS
    written = [ln[3:] for ln in body.splitlines()
               if ln.startswith("## ") and ln != "## Contents"]
    check("all nine sections are present, in the guide's order",
          written == list(SECTIONS), f"{len(written)} section(s)")

    # Asserted on the phrase to_text() prints, not by searching for section names inside a
    # slice of it -- the first version of this check looked for names after the words "nothing
    # to report" and would have passed vacuously if that wording ever changed. A check that
    # cannot fail is what P9-O11 and C83 were both about.
    marker = "Sections present with nothing to report"
    complete = marker not in text
    check("no section had nothing to report", complete,
          "none" if complete else text.split(marker)[-1].strip()[:110])

    check("the question is the user's words", QUESTION in body)
    check("the grain is the one that was agreed", "one row = one order" in body)
    check("the cleaning ledger names the action that ran", "CONVERT_TYPE" in body)
    check("validation results are reported", "Checks passed" in body)
    check("a chart is named beside its analysis", ".png" in body)
    check("the appendix lists calls that can be retyped",
          'compute_analysis(dataset_name="merged_multiheader"' in body
          and 'render_chart(dataset_name="merged_multiheader"' in body)
    check("growth_decomposition reached the report on a DOUBLE measure",
          "growth_decomposition" in body,
          "C85: it refused every float measure until 21/09/2026")
    check("the reply carries the contents and the key findings",
          "Contents:" in text and "Key findings:" in text)
    print()
    print(f"  the document: {path}  ({path.stat().st_size:,} bytes)")


def main() -> int:
    print("Phase 12 acceptance: the whole pipeline, and one complete document")
    workspace.reset(WORKSPACE)
    from backend.engine.contract import store
    store.EXPORT_DIR = workspace.workspace_dir(WORKSPACE) / "contracts"
    try:
        if not clause_one():
            print()
            print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
            return 1 if FAILED else 0
        try:
            from backend.engine import server
        except Exception as exc:  # noqa: BLE001
            skip("everything after the load", f"server.py did not import: {type(exc).__name__}")
            print()
            print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
            return 1 if FAILED else 0

        clause_two(server)
        if clause_three(server):
            clause_four(server)
            clause_five(server)
    finally:
        workspace.reset(WORKSPACE)

    print()
    print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    if SKIPPED:
        print("A skip is an outstanding clause, not a passing one.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
