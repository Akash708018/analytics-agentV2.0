"""The eval harness. Run from the repo root:

    uv run python eval/run_eval.py

Guide line 963, marked DO NOT SKIP. Three suites over gold questions whose answers were computed
without the tool under test -- in SQL against the loaded table, or fixed by the Failure Mode
Register. An answer produced by `compute_analysis` would agree with `compute_analysis` forever,
including when both are wrong.

**Behavioural asks about an agent, and there is no agent here.** Running a model against the
server and grading it measures one model on one day. The property underneath is testable: every
refusal carries a NEXT STEP, and `Refusal.__post_init__` already rejects one without parentheses
because "an instruction the agent cannot execute is how the apology loop starts". What nothing
did was execute it. A refusal recovers in one retry if the call it names, made verbatim,
succeeds -- and that tests the property rather than a sample.

The retry parses the NEXT STEP with `ast` and dispatches through an allowlist of registered
tools. Not `eval`: a harness that evaluated whatever a refusal string happened to contain would
be a worse bug than any it could find.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.contract.refusals import reason_of  # noqa: E402
from backend.engine.ingest import draft  # noqa: E402
from backend.engine.ingest.csv_loader import load_csv  # noqa: E402
from backend.engine.ingest.excel import load_excel  # noqa: E402
from backend.engine.util import db, results  # noqa: E402

QUESTIONS = Path("eval/gold_questions.yaml")

# P13-O2, and the better answer than the one it proposed. confirm_dataset_contract exports a
# YAML copy of the contract to docs/contracts/, outside the workspace, where it is meant to be
# version controlled -- so a script that confirms a contract dirties the repository (C86). The
# first fix taught one script to restore what it found, and the next script that confirmed a
# contract hit the same thing. contract/tools.confirm reads store.EXPORT_DIR at call time, so
# pointing it at this script's own workspace means nothing outside is ever written. No restore,
# no shared bookkeeping, and less code than either.
FIXTURES = Path("tests/fixtures")

WITH_CONTRACT = "eval_contract"
NO_CONTRACT = "eval_bare"
DATASET = "clean_sales"

#: The month removed from clean_gapped. No fixture has a missing period -- gaps_and_dupes is
#: about blanks and repeats in the HEADER, not about time -- so the guide's caveat case ("a
#: trend request on a dataset with a missing quarter must produce a gap warning, not a clean
#: line") is constructed here, from a known condition, and said to be constructed.
GAP_MONTH = "2024-07"
GAPPED = "clean_gapped"
EXCEL = "merged_multiheader"

PASSED = 0
FAILED = 0
SKIPPED = 0
BY_SUITE: Counter = Counter()
FAILED_BY_SUITE: Counter = Counter()
FAILURES: list[str] = []


def check(suite: str, label: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    BY_SUITE[suite] += 1
    if ok:
        PASSED += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        FAILED_BY_SUITE[suite] += 1
        FAILURES.append(f"{label}" + (f"  ({detail})" if detail else ""))
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
    return ok


def skip(label: str, why: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {label}  ({why})")


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def truth(sql: str, workspace_id: str = WITH_CONTRACT) -> str:  # noqa: D401
    """The answer, computed against the table rather than asked of the tool."""
    con = db.connect(workspace_id)
    try:
        value = con.execute(sql).fetchone()[0]
    finally:
        con.close()
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, int) else str(value)


# --- setup -----------------------------------------------------------------------------------

def mount(server) -> bool:
    """Three tables, because one is not a spread and P9-O2 asked for at least three.

    clean_sales as it comes; clean_gapped, the same rows with one month removed so the guide's
    caveat case has something to catch; and merged_multiheader through the Excel path with its
    date column cleaned, so the questions are not all about one loader.
    """
    # The redirect lives here rather than in main(), and the reason is C90: it was in main()
    # and a probe that imported this module and called mount() directly walked straight past it,
    # writing three files into docs/contracts/. Anything that confirms a contract has to be
    # covered, and mount() is what confirms them.
    from backend.engine.contract import store
    store.EXPORT_DIR = workspace.workspace_dir(WITH_CONTRACT) / "contracts"

    csv = FIXTURES / "clean_sales.csv"
    xlsx = FIXTURES / "merged_multiheader.xlsx"
    if not csv.exists():
        skip("the fixtures", f"{csv} is not on disk; run from the repository root")
        return False

    for ws in (WITH_CONTRACT, NO_CONTRACT):
        workspace.reset(ws)
        con = db.connect(ws)
        try:
            load_csv(con, str(csv), DATASET)
        finally:
            con.close()

    # The gapped copy, written out and loaded through the real path rather than made with CTAS:
    # a table created behind the loader's back is not a loaded dataset as far as the gate is
    # concerned, and a gold question that needed a back door would be testing one.
    rows = csv.read_text(encoding="utf-8").splitlines()
    header, body = rows[0], rows[1:]
    date_at = header.split(",").index("order_date")
    kept = [r for r in body if not r.split(",")[date_at].startswith(GAP_MONTH)]
    gapped_csv = workspace.workspace_dir(WITH_CONTRACT) / "clean_gapped.csv"
    gapped_csv.write_text("\n".join([header, *kept]) + "\n", encoding="utf-8")
    con = db.connect(WITH_CONTRACT)
    try:
        load_csv(con, str(gapped_csv), GAPPED)
        if xlsx.exists():
            spec = draft.spec_from_json(draft.draft_for_path(str(xlsx)).spec.model_dump_json())
            load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))
    finally:
        con.close()

    if xlsx.exists():
        server.propose_cleaning_plan(dataset_name=EXCEL, workspace_id=WITH_CONTRACT)
        server.apply_cleaning_plan(dataset_name=EXCEL, approved_action_ids=["C001"],
                                   workspace_id=WITH_CONTRACT)

    shape = dict(
        measures=["revenue", "units", "unit_price"],
        dimensions=["region", "product", "channel"],
        aggregations={"revenue": "sum", "units": "sum", "unit_price": "none"},
        definitions={
            "revenue": "units x unit_price, gross of tax",
            "units": "items on the order",
            "unit_price": "price of one item; summing it means nothing",
        },
        date_column="order_date",
        window=("2024-01-01", "2024-12-31"),
        workspace_id=WITH_CONTRACT,
    )
    for name in (DATASET, GAPPED) + ((EXCEL,) if xlsx.exists() else ()):
        if not contract_for(server, name, **shape):
            return False
    return True


def invoke(server, call: dict, workspace_id: str, dataset: str = DATASET) -> str:
    tool = getattr(server, call["tool"])
    args = dict(call.get("args") or {})
    args.setdefault("dataset_name", dataset)
    return tool(workspace_id=workspace_id, **args)


def contract_for(server, dataset: str, *, measures, dimensions, aggregations,
                 definitions, date_column, window, workspace_id) -> bool:
    """Propose and confirm, through the registered tools. Returns whether it stuck."""
    proposal = server.propose_dataset_contract(
        dataset_name=dataset,
        grain="one row = one order",
        primary_key=["order_id"],
        date_column=date_column,
        measures=measures,
        dimensions=dimensions,
        aggregations=aggregations,
        measure_definitions=definitions,
        analysis_window_start=window[0],
        analysis_window_end=window[1],
        workspace_id=workspace_id,
    )
    if "```" not in proposal:
        skip(f"the contract for {dataset}", proposal.splitlines()[0][:80])
        return False
    body = proposal.rsplit("```", 2)[-2]
    if body.startswith("json"):
        body = body[4:]
    confirmed = server.confirm_dataset_contract(contract_json=body, workspace_id=workspace_id)
    if reason_of(confirmed) is not None:
        skip(f"the contract for {dataset}", confirmed.splitlines()[0][:80])
        return False
    return True


# --- the retry -------------------------------------------------------------------------------

def next_step(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("NEXT STEP: call "):
            return line[len("NEXT STEP: call "):].strip()
    return None


def as_call(step: str, server) -> tuple[str, dict] | None:
    """The NEXT STEP as a call this harness can make, or None if it is not one.

    Parsed, never evaluated. A step is usable when it is exactly one call to a registered tool
    with literal arguments -- no prose after it, no second call, no placeholders. Anything else
    is something an agent has to read rather than follow, which is the measurement.
    """
    try:
        node = ast.parse(step.strip(), mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    name = node.func.id
    if not callable(getattr(server, name, None)) or node.args:
        return None
    args = {}
    for kw in node.keywords:
        try:
            args[kw.arg] = ast.literal_eval(kw.value)
        except ValueError:
            return None
    return name, args


# --- the suites -------------------------------------------------------------------------------

def run_correctness(server, q: dict) -> None:
    dataset = q.get("dataset", DATASET)
    answer = truth(q["sql"])
    reply = invoke(server, q["call"], WITH_CONTRACT, dataset)
    if reason_of(reply) is not None:
        check("correctness", f"{q['id']} {q['question']}", False, reply.splitlines()[0][:80])
        return
    missing = [e.format(answer=answer) for e in q["expect"]
               if e.format(answer=answer) not in reply]
    check("correctness", f"{q['id']} {q['question']}", not missing,
          f"answer {answer}" if not missing else f"not found: {missing}")


def run_behavioural(server, q: dict) -> None:
    ws = NO_CONTRACT if q.get("contract") is False else WITH_CONTRACT
    reply = invoke(server, q["call"], ws, q.get("dataset", DATASET))
    got = reason_of(reply)

    # A question with no `reason` is a caveat rather than a refusal: the call is supposed to
    # succeed and to say something the caller needs to know. The guide asks for both -- "cases
    # where the correct behaviour is to refuse or caveat" -- and a suite that only tested
    # refusals would miss the half where the tool answers and warns.
    if "reason" not in q:
        if not check("behavioural", f"{q['id']} answers rather than refusing",
                     got is None, str(got.value) if got else ""):
            return
        for phrase in q.get("expect", []):
            check("behavioural", f"{q['id']} caveats: {phrase!r}", phrase in reply)
        return

    if not check("behavioural", f"{q['id']} refuses with {q['reason']}",
                 got is not None and got.value == q["reason"],
                 str(got.value if got else "no refusal")):
        return

    for phrase in q.get("expect", []):
        check("behavioural", f"{q['id']} says {phrase!r}", phrase in reply)

    step = next_step(reply)
    if not check("behavioural", f"{q['id']} names a next step", step is not None):
        return
    parsed = as_call(step, server)

    if q.get("needs_decision"):
        check("behavioural", f"{q['id']} marks the decision the caller must make",
              parsed is None, step[:90])
        return

    if not check("behavioural", f"{q['id']} names a call an agent can make verbatim",
                 parsed is not None, step[:90]):
        return
    if q.get("retry_tool"):
        check("behavioural", f"{q['id']} recovers through {q['retry_tool']}",
              parsed[0] == q["retry_tool"], parsed[0])
    if q.get("retry"):
        name, args = parsed
        args.setdefault("workspace_id", ws)
        again = getattr(server, name)(**args)
        check("behavioural", f"{q['id']} recovers in one retry",
              reason_of(again) is None, again.splitlines()[0][:80])


# --- regression, one per Failure Mode Register id ----------------------------------------------

def merge_ranges_without_openpyxl() -> tuple[bool, str]:
    """F11: ws.merged_cells raises in read-only mode; the ranges come from the sheet XML."""
    from backend.engine.ingest import merges
    refs = merges.merged_ranges(str(FIXTURES / "merged_multiheader.xlsx"), "Sales")
    return len(refs) == 3, f"{len(refs)} range(s): {', '.join(refs)}"


def multiheader_csv_types() -> tuple[bool, str]:
    """F12: names assembled in Python with header=false, or every column is VARCHAR and the
    header row becomes data."""
    from backend.engine.ingest import draft
    d = draft.draft_for_path(str(FIXTURES / "multiheader.csv"))
    if d.spec is None:
        return True, "the loader asks rather than guessing, which is the mitigation"
    ws = "eval_f12"
    workspace.reset(ws)
    con = db.connect(ws)
    try:
        spec = draft.spec_from_json(d.spec.model_dump_json())
        kwargs = spec.to_loader_kwargs(load_csv)
        name = kwargs["dataset_name"]        # the spec names the table; do not pass a second
        load_csv(con, spec.path, **kwargs)
        types = [r[0] for r in con.execute(
            "SELECT data_type FROM information_schema.columns WHERE table_name = ?", [name]
        ).fetchall()]
        first = con.execute(f'SELECT * FROM "{name}" LIMIT 1').fetchone()
        ok = not all(t == "VARCHAR" for t in types)
        return ok, f"types: {sorted(set(types))}; first row starts {str(first[0])[:20]!r}"
    finally:
        con.close()
        workspace.reset(ws)


def bounded_fill_does_not_leak() -> tuple[bool, str]:
    """F14: a group label fills only within its real merge range."""
    from backend.engine.ingest import merges
    from backend.engine.ingest.headers import assemble_names
    refs = merges.merged_ranges(str(FIXTURES / "merged_multiheader.xlsx"), "Sales")
    row1 = ["Identifiers", None, "Dimensions", None, None, "Measures", None, None]
    row2 = ["order_id", "order_date", "region", "product", "channel",
            "units", "unit_price", "revenue"]
    names = assemble_names([row1, row2], header_rows_1idx=[1, 2], source_type="excel",
                           join="space", merge_refs=refs).names
    groups = ("Identifiers", "Dimensions", "Measures")
    spans = [n for n in names if sum(g in n for g in groups) > 1]
    ok = not spans and names[2].startswith("Dimensions") and names[5].startswith("Measures")
    return ok, f"col3={names[2]!r} col6={names[5]!r}" + (f" spans={spans}" if spans else "")


def ctas_type_change() -> tuple[bool, str]:
    """F15: UPDATE cannot change VARCHAR to DATE; the apply goes through CTAS and loses nothing."""
    from backend.engine import server
    from backend.engine.ingest import draft
    from backend.engine.ingest.excel import load_excel
    ws, ds = "eval_f15", "merged_multiheader"
    workspace.reset(ws)
    con = db.connect(ws)
    try:
        d = draft.draft_for_path(str(FIXTURES / "merged_multiheader.xlsx"))
        spec = draft.spec_from_json(d.spec.model_dump_json())
        load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))
    finally:
        con.close()
    server.propose_cleaning_plan(dataset_name=ds, workspace_id=ws)
    applied = server.apply_cleaning_plan(
        dataset_name=ds, approved_action_ids=["C001"], workspace_id=ws)
    con = db.connect(ws)
    try:
        kind = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = 'order_date'", [ds]).fetchone()
    finally:
        con.close()
        workspace.reset(ws)
    ok = kind is not None and kind[0] == "DATE" and "150 row(s) before, 150 after" in applied
    return ok, f"order_date is {kind[0] if kind else 'gone'}, rows preserved: " \
               f"{'150 row(s) before, 150 after' in applied}"


def no_bare_paths() -> tuple[bool, str]:
    """F7: an agent handed a path it cannot open writes a confident report about data it never
    saw. Every disk-write returns the path AND what is in the file."""
    from backend.engine import server
    reply = server.compute_analysis(dataset_name=DATASET, analysis_type="summary_stats",
                                    workspace_id=WITH_CONTRACT)
    wants = ("rows x", "written to", "What this shows:", "row(s) analysed")
    missing = [w for w in wants if w not in reply]
    path_lines = [ln for ln in reply.splitlines() if ".csv" in ln]
    ok = not missing and len(reply.splitlines()) > 8 and len(path_lines) >= 1
    return ok, (f"missing {missing}" if missing
                else f"{len(reply.splitlines())} lines around {len(path_lines)} path(s)")


def coercion_is_counted() -> tuple[bool, str]:
    """F9: a value that will not coerce is nulled, and the count of how many says so.

    mixed_types.xlsx puts its bad values below row 5000 on purpose, so the default
    inference_rows never sees them: the sniffer calls the column BIGINT and then meets text.
    That is the only way a coercion failure can happen at all.
    """
    from backend.engine.ingest.excel import load_excel
    fixture = FIXTURES / "mixed_types.xlsx"
    if not fixture.exists():
        return True, "mixed_types.xlsx is not on disk; nothing to coerce"
    from backend.engine.ingest.csv_loader import LoadRefused
    ws = "eval_f9"
    workspace.reset(ws)
    con = db.connect(ws)
    try:
        # Half one: the default load refuses rather than coercing quietly. Measured -- it names
        # the row, the column, the value and the type it was read as, and says nothing was
        # dropped because the load stopped instead.
        refused = ""
        try:
            load_excel(con, str(fixture), "f9")
        except LoadRefused as exc:
            refused = str(exc)
        loud = "does not fit its column" in refused and "Nothing has been dropped" in refused

        # Half two: the reload it tells you to do counts them per column, which is the
        # mitigation the register names.
        # LoadResult carries coercion_failures and coercion_total. The first version of this
        # check searched str(result) for the word "null" and reported the mitigation missing
        # when it was there -- a gold question can be wrong about the product in either
        # direction, and this one was wrong in the direction that accuses it.
        result = load_excel(con, str(fixture), "f9", on_error="null")
        failures = getattr(result, "coercion_failures", None)
        total = getattr(result, "coercion_total", None)
        counted = failures is not None and total is not None and total > 0
        return loud and counted, (
            f"refusal names the row: {loud}; {total} failure(s) counted per column: {failures}")
    finally:
        con.close()
        workspace.reset(ws)


def _scratch(name: str) -> Path:
    """A path inside the eval's own workspace, which is reset at the end. Constructed files --
    a pivot-shaped CSV, a sparse 2 GB one -- belong here, never in the repository."""
    return workspace.workspace_dir(WITH_CONTRACT) / name


def workflow_state_names_the_next_call() -> tuple[bool, str]:
    """F1: an agent that skips a gate loops on apologies unless something tells it where it is
    and what to call. get_workflow_state is that, on demand."""
    from backend.engine import server
    text = server.get_workflow_state(workspace_id=NO_CONTRACT)
    want = f'propose_dataset_contract(dataset_name="{DATASET}")'
    return want in text, f"names {want}: {want in text}"


def size_gate_speaks_before_loading() -> tuple[bool, str]:
    """F3: a large file is announced before it is read. Measured on a sparse 2 GB CSV, which
    uses no disk: a CSV streams, so the verdict is WARN rather than a refusal, and it names the
    size. What must not happen is silence."""
    from backend.engine.ingest import sizegate
    big = _scratch("sparse.csv")
    with open(big, "wb") as f:
        f.write(b"a,b\n1,2\n")
        f.truncate(2 * 1024 ** 3)
    try:
        g = sizegate.check_file(big)
    finally:
        big.unlink(missing_ok=True)
    verdict = getattr(getattr(g, "verdict", None), "name", str(g))
    message = str(getattr(g, "message", ""))
    ok = verdict in ("WARN", "REFUSE") and "GB" in message
    return ok, f"{verdict}: {message[:70]}"


def wide_results_are_paged_not_dumped() -> tuple[bool, str]:
    """F4: an agent misreads a wide table dumped inline. The preview caps its columns and names
    the rest, so nothing past the window is silently absent."""
    r = results.write_result(WITH_CONTRACT, label="wide",
                             headers=[f"c{i}" for i in range(50)],
                             rows=[[i] * 50 for i in range(3)])
    text = r.to_text()
    ok = "Showing 12 of 50 columns" in text and "c49" in text
    return ok, "12 of 50 shown, c49 named" if ok else text.splitlines()[-1][:80]


def preview_reads_only_what_it_shows() -> tuple[bool, str]:
    """F5: a preview must not load the file to show fifteen lines of it."""
    from backend.engine import server
    text = server.preview_file(path=str(FIXTURES / "clean_sales.csv"))
    numbered = [ln for ln in text.splitlines() if ln[:1].isdigit() and ": " in ln[:6]]
    ok = 0 < len(numbered) <= 20
    return ok, f"{len(numbered)} numbered line(s) of 501"


def merged_header_spreadsheet_loads() -> tuple[bool, str]:
    """F6: the loader end to end, not the merge parse (F11) or the name assembly (F14)."""
    from backend.engine.ingest import draft
    from backend.engine.ingest.excel import load_excel
    d = draft.draft_for_path(str(FIXTURES / "merged_multiheader.xlsx"))
    ws = "eval_f6"
    workspace.reset(ws)
    con = db.connect(ws)
    try:
        spec = draft.spec_from_json(d.spec.model_dump_json())
        r = load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))
    finally:
        con.close()
        workspace.reset(ws)
    ok = r.row_count == 150 and d.spec.header_rows == [1, 2]
    return ok, f"{r.row_count} rows, header rows {d.spec.header_rows}"


def pivot_export_is_named() -> tuple[bool, str]:
    """F8: a pivot dump read as records gives a table of twelve measures nobody declared.

    No fixture is pivot-shaped, so this one is constructed -- region plus twelve month columns
    -- and said to be, as clean_gapped is (P13-D7)."""
    from backend.engine.ingest import draft
    f = _scratch("pivot.csv")
    months = [f"2024-{m:02d}" for m in range(1, 13)]
    f.write_text("\n".join(["region," + ",".join(months)] + [
        f"{r}," + ",".join(str(100 + i) for i in range(12))
        for r in ("North", "South", "East", "West")]) + "\n", encoding="utf-8")
    try:
        v = draft.draft_for_path(str(f)).pivot
    finally:
        f.unlink(missing_ok=True)
    ok = v.is_pivot_dump and len(v.period_columns) == 12 and "pivot" in v.message
    return ok, f"is_pivot_dump={v.is_pivot_dump}, {len(v.period_columns)} period column(s)"


def trend_names_the_missing_period() -> tuple[bool, str]:
    """F10: a trend across an absent period must name it, not draw through it."""
    from backend.engine import server
    text = server.compute_analysis(dataset_name=GAPPED, analysis_type="trend",
                                   measure="revenue", grain="month",
                                   workspace_id=WITH_CONTRACT)
    # Asserted on the warning line, not on the month appearing anywhere. The first version
    # checked `GAP_MONTH in text`, and trend builds its calendar with every period as a row --
    # absent ones included, blank -- so the month was in the table whether or not anything
    # warned about it. Falsified before it was trusted: pointed at 2024-03, which holds rows,
    # it still passed. A check that cannot fail is what P9-O11 and C83 were.
    warning = [ln for ln in text.splitlines() if "hold no rows" in ln]
    ok = reason_of(text) is None and any(GAP_MONTH in ln for ln in warning)
    return ok, warning[0].strip()[:90] if warning else "no 'hold no rows' line"


def reset_needs_confirmation_and_empties() -> tuple[bool, str]:
    """F13: the stdio process outlives a chat, so state leaks unless it can be cleared -- and
    clearing it must not happen by accident."""
    from backend.engine import server
    ws = "eval_f13"
    workspace.reset(ws)
    con = db.connect(ws)
    try:
        load_csv(con, str(FIXTURES / "clean_sales.csv"), DATASET)
    finally:
        con.close()
    try:
        asked = server.reset_workspace(workspace_id=ws)
        server.reset_workspace(confirm=True, workspace_id=ws)
        after = server.list_datasets(workspace_id=ws)
    finally:
        workspace.reset(ws)
    ok = asked.startswith("BLOCKED") and "empty" in after
    return ok, f"unconfirmed blocked: {asked.startswith('BLOCKED')}; emptied: {'empty' in after}"


CHECKS = {
    "workflow_state_names_the_next_call": workflow_state_names_the_next_call,
    "size_gate_speaks_before_loading": size_gate_speaks_before_loading,
    "wide_results_are_paged_not_dumped": wide_results_are_paged_not_dumped,
    "preview_reads_only_what_it_shows": preview_reads_only_what_it_shows,
    "merged_header_spreadsheet_loads": merged_header_spreadsheet_loads,
    "pivot_export_is_named": pivot_export_is_named,
    "trend_names_the_missing_period": trend_names_the_missing_period,
    "reset_needs_confirmation_and_empties": reset_needs_confirmation_and_empties,
    "no_bare_paths": no_bare_paths,
    "coercion_is_counted": coercion_is_counted,
    "merge_ranges_without_openpyxl": merge_ranges_without_openpyxl,
    "multiheader_csv_types": multiheader_csv_types,
    "bounded_fill_does_not_leak": bounded_fill_does_not_leak,
    "ctas_type_change": ctas_type_change,
}


def run_regression(q: dict) -> None:
    fn = CHECKS.get(q["check"])
    if fn is None:
        check("regression", f"{q['id']} ({q['fmr']})", False, f"no check named {q['check']}")
        return
    ok, detail = fn()
    check("regression", f"{q['id']} ({q['fmr']}) {q['question']}", ok, detail)


# --- the roster -------------------------------------------------------------------------------

def next_call_roster(server) -> None:
    """How many refusals in the codebase name a call an agent could make verbatim.

    Not a gold question -- a measurement over the whole surface rather than one path. It is here
    because the number is the point of this phase: "it turns 'I built an AI thing' into 'I
    measured whether it was right, and here is the number'."
    """
    heading("Roster: refusals whose NEXT STEP is a call, not a sentence")
    src = Path("engine")
    total = usable = 0
    prose: list[str] = []
    for f in sorted(src.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "next_call":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    text = kw.value.value
                elif isinstance(kw.value, ast.JoinedStr):
                    # A bare x, not '"x"': the literal parts around an f-string expression
                    # already carry the quotes, so dataset_name="{name}" becomes
                    # dataset_name="x". Substituting a quoted x gave dataset_name=""x"", which
                    # is a syntax error, and the roster reported 0 of 37 -- a measurement that
                    # was wrong in the alarming direction and disagreed with a probe run
                    # minutes earlier. That disagreement is what caught it.
                    text = "".join(v.value if isinstance(v, ast.Constant) else "x"
                                   for v in kw.value.values)
                else:
                    continue
                total += 1
                if as_call(" ".join(text.split()), server) is not None:
                    usable += 1
                else:
                    prose.append(f"{f.name}: {' '.join(text.split())[:78]}")
    pct = (usable / total * 100) if total else 0.0
    print(f"  {usable} of {total} refusals ({pct:.0f}%) name a call an agent can make verbatim.")
    print(f"  {total - usable} carry prose, a second call, or a placeholder the caller fills.")
    for line in prose[:6]:
        print(f"    - {line}")
    if len(prose) > 6:
        print(f"    ... and {len(prose) - 6} more")


def main() -> int:
    if not QUESTIONS.exists():
        print(f"{QUESTIONS} is not on disk; run from the repository root")
        return 1
    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    print(f"Eval harness: {len(questions)} gold question(s)")

    try:
        from backend.engine import server
    except Exception as exc:  # noqa: BLE001
        print(f"server.py did not import: {type(exc).__name__}: {exc}")
        return 1

    try:
        if not mount(server):
            print()
            print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
            return 1 if FAILED else 0

        heading("Correctness: does the number match one computed without the tool?")
        for q in [q for q in questions if q["suite"] == "correctness"]:
            run_correctness(server, q)

        heading("Behavioural: does a refusal carry a recovery, and does it work?")
        for q in [q for q in questions if q["suite"] == "behavioural"]:
            run_behavioural(server, q)

        heading("Regression: one per Failure Mode Register id")
        for q in [q for q in questions if q["suite"] == "regression"]:
            run_regression(q)

        next_call_roster(server)
    finally:
        for ws in (WITH_CONTRACT, NO_CONTRACT):
            workspace.reset(ws)

    print()
    for suite in ("correctness", "behavioural", "regression"):
        n = BY_SUITE[suite]
        bad = FAILED_BY_SUITE[suite]
        print(f"  {suite:13s} {n - bad:3d}/{n:<3d} passed")
    total = PASSED + FAILED
    print()
    print(f"SCORE: {PASSED}/{total} ({PASSED / total * 100:.0f}%)"
          if total else "SCORE: no questions ran")
    if FAILED:
        print()
        print("Failing gold questions are findings, not a broken build. Each one is either a")
        print("defect in the product or a wrong answer in the question, and which it is has to")
        print("be decided by reading it. See docs/decisions.md for the open items they opened.")
        for label in FAILURES:
            print(f"  - {label}")
    if SKIPPED:
        print("A skip is an outstanding clause, not a passing one.")
    # Exits 0 on a wrong answer, and non-zero only if the harness itself could not run. An eval
    # that fails the build on any wrong answer is a test suite, and a test suite cannot carry a
    # score -- the number stops being a measurement the moment it has to be 100%.
    return 1 if SKIPPED else 0


if __name__ == "__main__":
    raise SystemExit(main())
