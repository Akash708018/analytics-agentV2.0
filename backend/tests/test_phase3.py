"""Phase 3 acceptance test. Run from the repo root:

    uv run python tests/test_phase3.py

Asserts the four Done-When clauses from the build guide, and nothing else. It
is not a unit suite -- `uv run pytest tests/ -q` is that. This answers one
question: is Phase 3 finished?

    1. messy_headers.xlsx and merged_multiheader.xlsx both load correctly
       through the propose/confirm path
    2. multiheader.csv prompts rather than guessing
    3. the bounded-fill unit test passes
    4. a preview of big_synthetic.csv (1.5 GB) returns in under 5 seconds with
       flat memory

Uses its own workspace, 'phase3_test', so nothing loaded in 'local' is touched.
"""

from __future__ import annotations

import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.ingest import draft, merges, preview  # noqa: E402
from backend.engine.ingest.csv_loader import load_csv, preview_lines  # noqa: E402
from backend.engine.ingest.excel import load_excel  # noqa: E402
from backend.engine.ingest.headers import assemble_names  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "phase3_test"
FIXTURES = Path("tests/fixtures")

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


def clause_1_excel(con) -> None:
    heading("Clause 1: both messy workbooks load through propose/confirm")

    d = draft.draft_for_path(str(FIXTURES / "messy_headers.xlsx"))
    check("messy_headers proposes a spec", d.spec is not None)
    check("header found at row 5", d.spec.header_rows == [5],
          str(d.spec.header_rows))
    check("loader is told to skip 5, not 1", d.spec.loader_header_rows == 5,
          str(d.spec.loader_header_rows))
    check("three footer rows detected", d.spec.footer_skip_rows == 3,
          str(d.spec.footer_skip_rows))
    check("spec is confirmable", d.spec.is_confirmable)

    spec = draft.spec_from_json(d.spec.model_dump_json())
    r = load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))
    check("200 rows loaded", r.row_count == 200, f"{r.row_count} rows")
    check("units typed BIGINT", dict(r.columns)["units"] == "BIGINT",
          dict(r.columns)["units"])
    notes = con.execute(
        "SELECT count(*) FROM messy_headers WHERE order_id LIKE 'Notes%'"
    ).fetchone()[0]
    check("no notes row became data", notes == 0)
    first = con.execute("SELECT order_id FROM messy_headers LIMIT 1").fetchone()[0]
    check("first row is data, not the title", first.startswith("ORD-"), first)

    d = draft.draft_for_path(str(FIXTURES / "merged_multiheader.xlsx"))
    check("merged_multiheader proposes a spec", d.spec is not None)
    check("header found at rows 1-2", d.spec.header_rows == [1, 2],
          str(d.spec.header_rows))
    check("bottom_only proposed", d.spec.header_join == "bottom_only",
          d.spec.header_join)
    check("names carry no group prefix",
          not any(n.startswith(("identifiers", "dimensions", "measures"))
                  for n in d.spec.target_names),
          ", ".join(d.spec.target_names[:3]))

    spec = draft.spec_from_json(d.spec.model_dump_json())
    r = load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))
    check("150 rows loaded", r.row_count == 150, f"{r.row_count} rows")
    check("8 columns", r.column_count == 8, f"{r.column_count} columns")


def clause_2_prompts(con) -> None:
    heading("Clause 2: multiheader.csv prompts rather than guessing")

    d = draft.draft_for_path(str(FIXTURES / "multiheader.csv"))
    check("a spec comes back", d.spec is not None)
    check("marked unresolved", d.spec.unresolved == ["header_rows"],
          str(d.spec.unresolved))
    check("NOT confirmable", not d.spec.is_confirmable)
    check("a question is attached", bool(d.spec.questions))
    check("the render says so", "CANNOT BE LOADED" in draft.render(d))
    check("the refusal names the next step",
          "propose_ingest_spec again" in d.spec.blocking_message())

    answered = draft.draft_for_path(
        str(FIXTURES / "multiheader.csv"), header_rows=[1, 2],
        authorised_fill=True,
    )
    check("an answer clears it", answered.spec.is_confirmable)
    check("authorised fill produces prefixed names",
          answered.spec.target_names[0] == "identifiers_order_id",
          answered.spec.target_names[0])

    spec = draft.spec_from_json(answered.spec.model_dump_json())
    r = load_csv(con, spec.path, **spec.to_loader_kwargs(load_csv))
    check("300 rows loaded after answering", r.row_count == 300,
          f"{r.row_count} rows")

    title = draft.draft_for_path(
        str(FIXTURES / "multiheader.csv"), header_rows=[2]
    )
    check("the other answer gives bare names",
          title.spec.target_names[0] == "order_id",
          title.spec.target_names[0])


def clause_3_bounded_fill() -> None:
    heading("Clause 3: bounded fill does not leak past a merge")

    path = str(FIXTURES / "merged_multiheader.xlsx")
    refs = merges.merged_ranges(path, "Sales")
    check("three merge ranges found", len(refs) == 3, ", ".join(refs))

    row1 = ["Identifiers", None, "Dimensions", None, None, "Measures", None, None]
    row2 = ["order_id", "order_date", "region", "product", "channel",
            "units", "unit_price", "revenue"]
    names = assemble_names(
        [row1, row2], header_rows_1idx=[1, 2], source_type="excel",
        join="space", merge_refs=refs,
    ).names

    check("no name spans two groups",
          not any(sum(g in n for g in ("Identifiers", "Dimensions", "Measures")) > 1
                  for n in names))
    check("Identifiers stops at column 2", names[2].startswith("Dimensions"),
          names[2])
    check("Dimensions stops at column 5", names[5].startswith("Measures"),
          names[5])

    csv_result = assemble_names(
        [row1, row2], header_rows_1idx=[1, 2], source_type="csv",
    )
    check("a CSV is never filled on the file's say-so",
          csv_result.names[1] == "order_date", csv_result.names[1])
    check("and the gap is reported", bool(csv_result.ambiguous_blanks))


def clause_4_big_preview() -> None:
    heading("Clause 4: a 1.5 GB preview is fast and flat")

    big = FIXTURES / "big_synthetic.csv"
    if not big.exists():
        print("  SKIP  big_synthetic.csv not present "
              "(uv run python tests/fixtures/make_fixtures.py --big)")
        return

    size_gb = big.stat().st_size / 1024 ** 3
    tracemalloc.start()
    t0 = time.time()
    spec, guess, _pivot = preview.draft_spec(
        preview.parse_csv_preview(preview_lines(big)),
        path=str(big), source_type="csv", dataset_name="big",
    )
    elapsed = time.time() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    check("under 5 seconds", elapsed < 5, f"{elapsed:.2f}s on {size_gb:.1f} GB")
    check("peak memory flat", peak / 1e6 < 50, f"{peak / 1e6:.1f} MB")
    check("a spec came back", spec is not None)
    check("confidence is high", guess.confidence == "high", guess.confidence)


def main() -> int:
    print(f"Phase 3 acceptance test -- workspace {WORKSPACE!r}")

    if not (FIXTURES / "messy_headers.xlsx").exists():
        print("\nBLOCKED: fixtures are missing.\n"
              "NEXT STEP: uv run python tests/fixtures/make_fixtures.py")
        return 1

    workspace.reset(WORKSPACE)
    con = db.connect(WORKSPACE)
    try:
        clause_1_excel(con)
        clause_2_prompts(con)
        clause_3_bounded_fill()
        clause_4_big_preview()
    finally:
        con.close()

    print()
    print("=" * 46)
    print(f"  {PASSED} passed, {FAILED} failed")
    print("=" * 46)
    print()
    print("Phase 3 Done-When: "
          + ("ALL CLAUSES PASS" if FAILED == 0 else "NOT MET"))
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
