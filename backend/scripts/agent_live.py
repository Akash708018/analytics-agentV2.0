"""One real question through the agent loop, against a real model. Run from the repository root:

    uv run python scripts/agent_live.py ["your question"]

Needs GEMINI_API_KEY and/or GROQ_API_KEY in the environment or in .env (gitignored). Loads the
clean_sales fixture into a throwaway workspace under a confirmed contract, asks, prints what ran,
and deletes the workspace. Prints key NAMES only, never values. Not a pytest file: it needs the
network and a key, and a suite must not.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from backend.engine import workspace  # noqa: E402
from backend.engine.webapp import llm  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

QUESTION = "Which region has the most revenue? Show it as a chart."
ANSWERS = dict(
    grain="one row = one order", primary_key=["order_id"], date_column="order_date",
    measures=["units", "unit_price", "revenue"], dimensions=["region", "product", "channel"],
    aggregations={"units": "sum", "unit_price": "none", "revenue": "sum"},
    measure_definitions={"units": "items on the order", "unit_price": "price of one item",
                         "revenue": "units x unit_price"},
    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")


def main() -> int:
    print("keys loaded from .env:", llm.load_env() or "(none; using the environment)")
    providers = llm.configured()
    if not providers:
        print("No provider has a key. Put GEMINI_API_KEY=... in .env at the repository root.")
        return 1
    for p in providers:
        try:
            print(f"provider {p.name}: model {p.model()}")
        except llm.ProviderError as exc:
            print(f"provider {p.name}: model discovery failed -- {exc}")
    be = RealBackend()
    ws = be.new_workspace_id()
    try:
        path = be.save_upload(ws, "clean_sales.csv",
                              (ROOT / "tests/fixtures/clean_sales.csv").read_bytes()).path
        assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
        assert be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", **ANSWERS)).ok
        question = sys.argv[1] if len(sys.argv) > 1 else QUESTION
        print(f"\nQ: {question}\n")
        turn = be.chat(ws, [], question)
        for i, call in enumerate(turn.tool_calls, 1):
            args = {k: v for k, v in call.arguments.items() if v is not None}
            print(f"  {i}. {call.name}({args}){'  REFUSED' if call.refused else ''}")
            print("     " + call.result.splitlines()[0][:110] if call.result else "")
        print(f"\nA: {turn.reply}" if turn.reply else "")
        print(f"artifacts: {[a.path for a in turn.artifacts]}")
        for p in providers:  # which model answered: the ladder may have moved past spent ones
            spent = sorted(getattr(p, "_spent", {}))
            try:
                now = p.model()
            except llm.ProviderError as exc:
                now = f"none left ({exc.summary})"
            print(f"{p.name}: now on {now}" + (f"; spent today: {spent}" if spent else ""))
        if turn.error:
            print(f"ERROR: {turn.error}")
        return 1 if turn.error else 0
    finally:
        workspace.reset(ws)
        workspace.workspace_dir(ws).rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
