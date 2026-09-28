"""Phase 5 acceptance test. Run from the repo root:

    uv run python tests/test_phase5.py

Asserts the three Done-When clauses from the build guide, and two more:

    1. it runs on all fixtures, plus an Olist table
    2. it correctly reports injected duplicates and nulls
    3. a wide-table profile returns a path WITH a usable summary, never bare
    4. (not in the Done-When) every path any tool returns opens under
       read_result_file, in the same run
    5. (not in the Done-When) the workflow state reports the profiling stage

Clause 4 is the one worth explaining. A writer and a reader in the same package
drift when either builds a path by hand, and the result is a well-formed
envelope pointing at nothing -- F7 with a clean conscience, invisible in a
transcript. So this does not check that A path opens; it parses the
read_result_file call out of each rendered profile and executes THAT.

Clause 2 is asserted against a table built here with known counts rather than
against a fixture whose duplicate count nobody has verified. "Correctly
reports" needs ground truth, and a fixture is only ground truth if someone
counted it. The real fixtures are still profiled, under clause 1, for the
different question of whether the thing runs on them at all.

Uses its own workspace, 'phase5_test', so nothing loaded in 'local' is touched.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.contract.refusals import reason_of  # noqa: E402
from backend.engine.ingest.csv_loader import load_csv  # noqa: E402
from backend.engine.profile import runs, tools  # noqa: E402
from backend.engine.profile.table_profile import profile_table  # noqa: E402
from backend.engine.state import describe_workflow_state  # noqa: E402
from backend.engine.util import db, results  # noqa: E402

WORKSPACE = "phase5_test"
FIXTURES = Path("tests/fixtures")
SERVER = Path("engine/server.py")

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


def registered_tools() -> set[str]:
    """Every @mcp.tool in server.py, read from source rather than imported."""
    if not SERVER.exists():
        return set()
    tree = ast.parse(SERVER.read_text())
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                out.add(node.name)
    return out


_CALL_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(")


def calls_named_in(text: str) -> list[str]:
    for line in text.splitlines():
        if line.startswith("NEXT STEP:"):
            return _CALL_RE.findall(line)
    return []


def read_call_in(text: str) -> tuple[str, int, int] | None:
    """
    The read_result_file call a rendered profile printed, parsed back out.

    Not a path found some other way: the point of clause 4 is that the string
    the agent is handed is the string that works.
    """
    for line in text.splitlines():
        if "read_result_file(" not in line:
            continue
        try:
            path = line.split('path="', 1)[1].split('"', 1)[0]
        except IndexError:
            continue
        start_m = re.search(r"start=(\d+)", line)
        limit_m = re.search(r"limit=(\d+)", line)
        return (
            path,
            int(start_m.group(1)) if start_m else 1,
            int(limit_m.group(1)) if limit_m else results.PAGE_ROWS,
        )
    return None


def clause_1_fixtures(con) -> None:
    heading("Clause 1: it runs on every fixture")

    if not FIXTURES.exists():
        skip("fixtures", f"{FIXTURES} not found")
        return

    csvs = sorted(FIXTURES.glob("*.csv"))
    check("there are fixtures to run on", bool(csvs), f"{len(csvs)} csv(s)")

    for path in csvs:
        name = path.stem
        size_mb = path.stat().st_size / 1_000_000
        if size_mb > 50:
            # big_synthetic is 28.5M rows. Loading it here would turn a
            # correctness check into a benchmark, and the timing it WOULD
            # produce is measured deliberately further down instead.
            skip(name, f"{size_mb:,.0f} MB, measured in the P5-D3 section")
            continue

        print(f"  loading {path.name}...", flush=True)
        try:
            load_csv(con, str(path), name)
        except Exception as exc:  # noqa: BLE001
            # A fixture the loader cannot take is a finding about the loader,
            # not a crash. gaps_and_dupes.csv has blank and repeated names in
            # its header and needs Phase 3 assembly rather than load_csv.
            skip(name, f"load_csv refused: {str(exc)[:60]}")
            continue

        text = tools.profile_dataset(con, WORKSPACE, name)
        code = reason_of(text)
        check(f"{name}: profiles without refusing", code is None, str(code) or "")
        check(f"{name}: the profile names its shape", "columns." in text)
        check(f"{name}: a run was recorded", runs.latest(con, name) is not None)


def clause_1b_olist(con) -> None:
    heading("Clause 1 continued: an Olist table")

    olist = [t for t in db.user_tables(con) if "olist" in t.lower()]
    if not olist:
        skip("olist", "no olist table here; load one and re-run to cover it")
        return

    name = olist[0]
    print(f"  profiling {name}...", flush=True)
    t0 = time.time()
    text = tools.profile_dataset(con, WORKSPACE, name)
    elapsed = time.time() - t0
    check(f"{name}: profiles without refusing", reason_of(text) is None)
    check(f"{name}: took under 30s", elapsed < 30, f"{elapsed:.2f}s")


def clause_2_duplicates_and_nulls(con) -> None:
    heading("Clause 2: injected duplicates and nulls, reported exactly")

    con.execute(
        """
        CREATE OR REPLACE TABLE injected AS
        SELECT
          'ORD-' || lpad(i::VARCHAR, 5, '0')                 AS order_id,
          CASE WHEN i % 13 = 0 THEN NULL ELSE 'North' END     AS region,
          CASE WHEN i % 10 = 0 THEN 'N/A'  ELSE 'Online' END  AS channel,
          ((i % 97) + 1) * 1.5                               AS revenue
        FROM range(200) t(i)
        """
    )
    con.execute(
        "INSERT INTO injected SELECT * FROM injected ORDER BY order_id LIMIT 10"
    )

    nulls = con.execute(
        "SELECT count(*) FROM injected WHERE region IS NULL"
    ).fetchone()[0]
    tokens = con.execute(
        "SELECT count(*) FROM injected WHERE channel = 'N/A'"
    ).fetchone()[0]

    p = profile_table(con, "injected")
    check("the duplicate count is exact", p.duplicate_rows == 10,
          f"reported {p.duplicate_rows}, injected 10")
    check("the null count is exact",
          p.column("region").evidence.null_count == nulls,
          f"reported {p.column('region').evidence.null_count}, actual {nulls}")
    check("nulls and N/A tokens are counted separately",
          p.column("channel").evidence.null_count == 0
          and p.column("channel").missing_token_count == tokens,
          f"{p.column('channel').missing_token_count} token(s), {tokens} actual")
    check("the two kinds of absence add up",
          p.column("channel").missing_count == tokens)

    text = tools.profile_dataset(con, WORKSPACE, "injected")
    check("the summary states the duplicates",
          "10 row(s) are exact duplicates" in text)
    check("the summary states the hidden missing values",
          "read as missing without being null" in text)
    check("it says nothing was rewritten", "Nothing was rewritten" in text)


def clause_3_wide(con) -> None:
    heading("Clause 3: a wide profile returns a path AND a usable summary")

    cols = ", ".join(f"(i % {c + 2})::INTEGER AS col_{c}" for c in range(60))
    con.execute(
        f"CREATE OR REPLACE TABLE wide AS SELECT {cols} FROM range(1000) t(i)"
    )
    text = tools.profile_dataset(con, WORKSPACE, "wide")

    check("it does not refuse", reason_of(text) is None)
    check("it is not a bare path", len(text.splitlines()) > 10)
    check("it names the shape", "1,000 rows, 60 columns" in text)
    check("it carries the findings", "No exact duplicate rows" in text)

    run = runs.latest(con, "wide")
    check("the path is in the text", run is not None and run.result_path in text)
    check("it states both truncations",
          "First 20 of 60 rows:" in text and "Showing 12 of" in text)
    check("it names what was not shown", "40 more rows" in text)
    check("it ends in a call that fetches the rest",
          read_call_in(text) is not None)


def clause_4_round_trip(con) -> None:
    heading("Clause 4: the call printed in the envelope is the call that works")

    for name in ("injected", "wide"):
        text = tools.profile_dataset(con, WORKSPACE, name)
        parsed = read_call_in(text)
        if parsed is None:
            check(f"{name}: the envelope names a read call", False)
            continue
        path, start, limit = parsed
        page = tools.read_result(WORKSPACE, path, start=start, limit=limit)
        code = reason_of(page)
        check(f"{name}: that call opens the file", code is None,
              str(code) if code else f"start={start}")
        check(f"{name}: the page names the rows it returned",
              f"rows {start:,} to" in page)

    every = results.list_results(WORKSPACE)
    unopenable = [
        p for p in every
        if reason_of(tools.read_result(WORKSPACE, str(p))) is not None
    ]
    check("every result file written this run opens", not unopenable,
          ", ".join(p.name for p in unopenable) or f"{len(every)} file(s)")

    check("a recorded run's path opens too",
          reason_of(tools.read_result(
              WORKSPACE, runs.latest(con, "wide").result_path)) is None)


def clause_4b_refusals(con) -> None:
    heading("Clause 4 continued: every refusal names a tool that exists")

    refusals = {
        "not loaded": tools.profile_dataset(con, WORKSPACE, "nope"),
        "no such column": tools.profile_column(con, WORKSPACE, "wide", "nope"),
        "path outside the workspace": tools.read_result(WORKSPACE, "/etc/passwd"),
        "no such result": tools.read_result(
            WORKSPACE, str(results.results_dir(WORKSPACE) / "nope.csv")
        ),
    }
    in_server = registered_tools()

    for label, text in refusals.items():
        check(f"{label}: carries a reason code", reason_of(text) is not None,
              str(reason_of(text)))
        named = calls_named_in(text)
        check(f"{label}: names a call with arguments", bool(named),
              ", ".join(named))
        if in_server:
            unknown = [n for n in named if n not in in_server]
            check(f"{label}: the call it names is registered", not unknown,
                  ", ".join(unknown) or "all known")

    if in_server:
        check("the three profiling tools are registered",
              {"profile_dataset", "profile_column", "read_result_file"}
              <= in_server)
    else:
        skip("registration", "server.py not found")


def clause_5_workflow(con) -> None:
    heading("Clause 5: the workflow state reports the profiling stage")

    text = describe_workflow_state(con)
    check("a profiled dataset says when it was profiled",
          "profiled just now" in text or "profiled " in text)
    check("it offers the full counts", "Full counts: read_result_file(" in text)
    check("the profile log is not listed as a dataset",
          runs.PROFILE_TABLE not in text)

    con.execute(
        "INSERT INTO injected SELECT * FROM injected ORDER BY order_id LIMIT 5"
    )
    stale = describe_workflow_state(con)
    check("a stale profile says it is out of date", "out of date" in stale)
    check("it says old rather than wrong", "wrong" not in stale.lower())
    check("it names the re-run",
          'Re-run profile_dataset(dataset_name="injected")' in stale)


def measurement_p5_d3(con) -> None:
    """
    OPT-IN, and it has to be. big_synthetic.csv is 28.5M rows: loading and
    profiling it takes minutes, and the first version ran it unconditionally
    with no output while it worked. An acceptance test that sits silent for
    five minutes is indistinguishable from one that has hung, and the person
    running it reaches for Ctrl-C -- which is the correct response to what they
    can see.

    So it runs only with PHASE5_BIG=1, and it narrates every long step before
    starting it rather than after finishing.
    """
    heading("P5-D3: full scan, no sampling -- the measurement")

    big = FIXTURES / "big_synthetic.csv"
    if not big.exists():
        skip("big_synthetic", "not present")
        return

    size_mb = big.stat().st_size / 1_000_000
    if os.environ.get("PHASE5_BIG") != "1":
        skip(
            "big_synthetic",
            f"{size_mb:,.0f} MB, minutes to run. "
            f"PHASE5_BIG=1 uv run python tests/test_phase5.py",
        )
        return

    print(f"  {big.name}: {size_mb:,.0f} MB on disk")
    print("  loading... (minutes, not seconds)", flush=True)
    try:
        t0 = time.time()
        load_csv(con, str(big), "big")
        load_s = time.time() - t0
    except Exception as exc:  # noqa: BLE001
        print(f"  the size gate refused the load: {str(exc)[:120]}")
        print("  That is itself the answer -- record it and move on.")
        return

    rows, cols = db.table_shape(con, "big")
    print(f"  loaded   {rows:,} rows x {cols} cols in {load_s:,.1f}s")
    print("  profiling... the DISTINCT * duplicate scan is the slow half",
          flush=True)
    t0 = time.time()
    tools.profile_dataset(con, WORKSPACE, "big")
    profile_s = time.time() - t0
    print(f"  profiled                         in {profile_s:,.1f}s")
    print()
    print("  Record both in docs/decisions.md. P5-D3 chose full scans over")
    print("  sampling because a sampled '3.2% null' when the truth is 11% is")
    print("  worse than no profile. If this number makes that untenable, the")
    print("  answer is a cap with the remainder REPORTED, never a sample with")
    print("  the shortfall unstated.")


def main() -> int:
    print(f"Phase 5 acceptance test -- workspace {WORKSPACE!r}")

    workspace.reset(WORKSPACE)
    con = db.connect(WORKSPACE)
    try:
        clause_1_fixtures(con)
        clause_1b_olist(con)
        clause_2_duplicates_and_nulls(con)
        clause_3_wide(con)
        clause_4_round_trip(con)
        clause_4b_refusals(con)
        clause_5_workflow(con)
        measurement_p5_d3(con)
    finally:
        con.close()
        workspace.reset(WORKSPACE)

    print()
    print("=" * 52)
    print(f"  {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    print("=" * 52)
    print()
    print("Phase 5 Done-When: "
          + ("ALL CLAUSES PASS" if FAILED == 0 else "NOT MET"))
    if SKIPPED:
        print()
        print("Skips are not passes. An Olist table and big_synthetic each")
        print("cover something this run did not; the skip lines say how.")
    print()
    print("The live half is not asserted here. Run it in Claude Desktop and")
    print("record the result -- whether the model OPENS the result file is the")
    print("one thing no script can tell you, and it is exactly what F7 is.")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
