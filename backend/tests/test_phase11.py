"""Phase 11 acceptance test. Run from the repo root:

    uv run python tests/test_phase11.py

Charts, end to end, through the tool a client actually calls -- and the forwarding clause that
P11-O1 asked for.

**Why this file calls server.py rather than the tools layer.** C83: eight of twenty-seven
analyses could not be called at all through compute_analysis, because six parameters were never
declared and three were declared and dropped on the way through. Three green acceptance runs and
1,698 unit tests said nothing, because every one of them reached the analysis through the
registry or through analysis/tools.py, and the only path a real client takes is the one nothing
walked. tests/test_phase8.py clause four walks it for Phase 8's nine (P8-D76); those take none of
the nine parameters C83 named. This walks it for the rest.

**It runs on the CSV fixture, not Olist.** clean_sales declares three measures with three
different aggregates and three dimensions, which is enough to reach every tier, and it needs no
Postgres -- so this script is portable in a way test_phase9.py and test_phase10.py are not.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.contract import store  # noqa: E402
from backend.engine.contract.dataset_contract import (  # noqa: E402
    AnalysisWindow,
    Binding,
    DatasetContract,
    Measure,
)
from backend.engine.contract.refusals import reason_of  # noqa: E402
from backend.engine.ingest.csv_loader import load_csv  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "phase11_test"
DATASET = "clean_sales"
CLEAN = Path("tests/fixtures/clean_sales.csv")
WINDOW = AnalysisWindow(start=date(2024, 1, 1), end=date(2024, 12, 31))

PERIOD, BASELINE, GRAIN = "2024-10", "2024-01", "month"

# The nine parameters C83 named, each on an analysis that requires it. A call that comes back
# without a refusal is one whose argument arrived under the right name; before c1826b2 every
# row of this table returned ANALYSIS_PARAMS_INVALID.
FORWARDING = (
    ("correlation", {"measure": "revenue", "against": "units"}, "against"),
    ("bivariate", {"measure": "revenue", "against": "units"}, "against"),
    ("period_compare",
     {"measure": "revenue", "period": PERIOD, "baseline": BASELINE, "grain": GRAIN},
     "period and baseline"),
    ("growth_decomposition",
     {"measure": "revenue", "dimension": "region", "period": PERIOD,
      "baseline": BASELINE, "grain": GRAIN},
     "period and baseline"),
    ("mix_shift",
     {"measure": "revenue", "dimension": "region", "period": PERIOD,
      "baseline": BASELINE, "grain": GRAIN},
     "period and baseline"),
    ("repeat_behaviour", {"entity": "region"}, "entity"),
    ("hypothesis_test", {"dimension": "region", "measure": "revenue", "method": "auto"},
     "method"),
    ("effect_size", {"dimension": "region", "second_dimension": "channel"},
     "second_dimension"),
    ("confidence_interval", {"measure": "revenue", "confidence": 0.99}, "confidence"),
    ("sample_adequacy",
     {"dimension": "region", "measure": "revenue", "power": 0.8, "alpha": 0.05},
     "power and alpha"),
)

# The eight kinds the guide names at line 943, each pointed at a result of the right shape.
# Single-measure kinds are given y, which is the usage the refusal for a two-measure result
# tells a caller to reach for. The pairings here are structural, not editorial -- the guide
# pairs waterfall with mix_shift for meaning, and this clause is about the surface working.
CHARTS = (
    ("line", "frequency", {"column": "region", "y": "rows"}),
    ("bar", "frequency", {"column": "region", "y": "rows"}),
    ("histogram", "frequency", {"column": "region", "y": "rows"}),
    ("waterfall", "frequency", {"column": "region", "y": "rows"}),
    ("grouped_bar", "summary_stats", {}),
    ("scatter", "summary_stats", {}),
    ("box", "summary_stats", {}),
    # Was summary_stats until Phase 14 Step 12: a heatmap shades every column on one colour
    # scale, and summary_stats' n, nulls, total and mean are not one quantity (P14-O20). A
    # cross_tab's cells are.
    ("heatmap", "cross_tab", {"rows": "channel", "columns": "region", "measure": "units"}),
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


def first_line(text: str) -> str:
    return text.splitlines()[0][:95] if text else "(no reply)"


def mount() -> bool:
    """Load the fixture and confirm a real contract through the real store."""
    if not CLEAN.exists():
        skip("the fixture", f"{CLEAN} is not on disk; run from the repository root")
        return False
    con = db.connect(WORKSPACE)
    try:
        load_csv(con, str(CLEAN), DATASET)
        pairs = [
            (r[0], r[1])
            for r in con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [DATASET],
            ).fetchall()
        ]
        rows = con.execute(f'SELECT count(*) FROM "{DATASET}"').fetchone()[0]
        store.confirm(con, DatasetContract(
            dataset_name=DATASET,
            grain="one row = one order",
            primary_key=["order_id"],
            date_column="order_date",
            analysis_window=WINDOW,
            measures=[
                Measure(name="revenue", agg="sum", definition="units x unit_price", unit="GBP"),
                Measure(name="units", agg="sum", definition="items on the order"),
                Measure(name="unit_price", agg="none",
                        definition="price of one item; summing it means nothing", unit="GBP"),
            ],
            dimensions=["region", "channel", "product"],
            bound_to=Binding.from_pairs(pairs, rows),
        ))
        return True
    finally:
        con.close()


def clause_one(server) -> None:
    """P11-O1. Every parameter C83 named, through the tool a client calls."""
    heading("Clause 1: the nine parameters C83 named reach their analyses")

    for analysis_type, params, names in FORWARDING:
        text = server.compute_analysis(
            dataset_name=DATASET, analysis_type=analysis_type,
            workspace_id=WORKSPACE, **params,
        )
        ok = reason_of(text) is None
        check(f"{names} reach {analysis_type}", ok, "" if ok else first_line(text))


def clause_two(server) -> list[str]:
    """All eight kinds, drawn through render_chart rather than through charts/render.py."""
    heading("Clause 2: all eight chart kinds render through the registered tool")

    replies: list[str] = []
    for kind, analysis_type, params in CHARTS:
        text = server.render_chart(
            dataset_name=DATASET, analysis_type=analysis_type, chart=kind,
            workspace_id=WORKSPACE, **params,
        )
        ok = reason_of(text) is None
        check(f"{kind} renders from {analysis_type}", ok, "" if ok else first_line(text))
        if ok:
            replies.append(text)
    return replies


def clause_three(replies: list[str]) -> None:
    """Rule 4: the file is real, and the reply describes it rather than naming it."""
    heading("Clause 3: a chart says what is in it, because nobody can open it")

    if not replies:
        skip("Rule 4", "no chart rendered, so there is nothing to describe")
        return

    text = replies[0]
    named = [ln.split("Chart written: ", 1)[1].strip()
             for ln in text.splitlines() if ln.startswith("Chart written: ")]
    check("the reply names exactly one file", len(named) == 1, str(named))
    if len(named) != 1:
        return

    path = Path(named[0])
    check("the file it names exists", path.exists(), str(path))
    if path.exists():
        check("it is really a PNG", path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
              f"{path.stat().st_size:,} bytes")
        check("it landed in the workspace charts directory", path.parent.name == "charts",
              str(path.parent))

    check("the reply says the image cannot be seen", "You cannot see this image" in text)
    check("it says how many points were drawn of how many", "point(s) drawn" in text)
    check("it gives the numbers off the axes", "lowest" in text and "highest" in text)
    check("it carries the contract it was drawn under",
          text.startswith(f"Under contract v1 for {DATASET}:"), first_line(text))


def main() -> int:
    print("Phase 11 acceptance: charts, and the forwarding P11-O1 asked for")
    workspace.reset(WORKSPACE)
    try:
        if not mount():
            print()
            print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
            return 1 if FAILED else 0
        try:
            from backend.engine import server
        except Exception as exc:  # noqa: BLE001  -- FastMCP missing or broken
            skip("everything", f"server.py did not import: {type(exc).__name__}: {exc}")
            print()
            print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
            return 1 if FAILED else 0

        clause_one(server)
        replies = clause_two(server)
        clause_three(replies)
    finally:
        workspace.reset(WORKSPACE)

    print()
    print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    if SKIPPED:
        print("A skip is an outstanding clause, not a passing one.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
