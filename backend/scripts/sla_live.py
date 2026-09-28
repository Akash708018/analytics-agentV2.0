"""The SLA question asked of a real model, on the user's last-mile file (step 3).

    uv run python scripts/sla_live.py ["question"]

Needs keys in the gitignored .env. Loads tests/fixtures/logistics_sla.csv, approves the Clean
screen's suggested plan and a contract as scripts/sla_bench.py does, then asks. If the model
proposes a metric, approves it (as the person would with the button) and asks again. Prints what
ran, the waits, the check, and the answer.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "scripts"))

import sla_bench as bench  # noqa: E402
from backend.engine import server, workspace  # noqa: E402
from backend.engine.webapp import agent  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

QUESTION = ("Which hub has the worst SLA compliance, and what factors are associated with it? "
            "Use valid delivered orders only.")


def main() -> int:
    import re

    question = sys.argv[1] if len(sys.argv) > 1 else QUESTION
    be = RealBackend()
    ws = be.new_workspace_id()
    try:
        path = be.save_upload(ws, bench.FIXTURE.name, bench.FIXTURE.read_bytes()).path
        assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
        plan = server.propose_cleaning_plan(bench.NAME, workspace_id=ws)
        server.apply_cleaning_plan(bench.NAME, workspace_id=ws, approved_action_ids=sorted(
            set(re.findall(r"\b(C\d{3})\s+[A-Z_]+", plan))))
        m = bench.MEASURES
        assert be.confirm_contract(ws, be.draft_contract(
            ws, bench.NAME, grain="one row = one order", primary_key=[],
            date_column="order_date", measures=m, dimensions=bench.DIMS,
            aggregations={x: "sum" if x.endswith("_inr") else "mean" for x in m},
            measure_definitions={x: x.replace("_", " ") for x in m},
            analysis_window_start="2026-08-01", analysis_window_end="2026-08-31")).ok
        history: list[dict] = []
        for turn_no in (1, 2):
            t0 = time.time()
            turn = agent.answer(ws, history, question, lock=lambda: be._workspace(ws),
                                list_artifacts=lambda: be.list_artifacts(ws),
                                progress=lambda e: e.kind != "step" and print(f"  [{e.kind}] "
                                                                              f"{e.text}"))
            print(f"\n=== turn {turn_no}: {time.time() - t0:.0f} s")
            for c in turn.tool_calls:
                print(f"  {c.name}({c.arguments}){'  REFUSED' if c.refused else ''}")
            print(f"  error: {turn.error}\n  check: {turn.verification}\n")
            print(turn.reply)
            history += [{"role": "user", "content": question},
                        {"role": "assistant", "content": turn.reply or turn.error or ""}]
            pending = be.pending_metrics(ws)
            if not pending:
                break
            for p in pending:
                print(f"\n>>> approving {p.name}: {p.formula}")
                be.decide_metric(ws, p.id, True)
            question = "Approved. Please answer the question now."
        return 0
    finally:
        workspace.reset(ws)
        workspace.workspace_dir(ws).rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
