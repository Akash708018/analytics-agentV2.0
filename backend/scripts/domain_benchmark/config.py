"""Configuration of the cross-domain benchmark (Phase 14 Step 13). Stored with the results."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# A post-fix re-run writes beside the original rather than over it (DOMAIN_BENCH_OUT).
OUT = Path(os.environ.get("DOMAIN_BENCH_OUT", ROOT / "docs" / "benchmark" / "domain_benchmark"))
# Generated datasets are large and reproducible from their seed: they live outside the repository.
DATA = Path(os.environ.get("DOMAIN_BENCH_DATA", "/tmp/domain_benchmark_data"))
# Raw evidence (every worker's JSONL, copies of the result files, the job files) is kept out of
# the repository: it is ~1 MB of JSONL per 1M-row unit. The aggregated JSON is committed.
CHECKPOINT = DATA / "checkpoint"
EVIDENCE = DATA / "evidence"

SEED = 20260924
PRINCIPAL_SIZES = (1_000, 100_000, 1_000_000)
STRESS_SIZES = (2_000_000, 5_000_000, 10_000_000)
STRESS_DOMAINS = ("sales", "logistics")          # two schemas, one wide and one date-heavy

# Repetitions: one cold run, then MEASURED_RUNS. Expensive whole-table calls at 1M rows and
# above get fewer, and every record carries its own run count.
MEASURED_RUNS = 3
HEAVY_MEASURED_RUNS_1M = 2
PROFILE_COLUMN_MEASURED_RUNS_1M = 1
STRESS_MEASURED_RUNS = 1

# Timeouts. A per-call alarm is delivered when control returns to Python (DuckDB checks for
# signals inside long queries); the worker's wall-clock limit is enforced by the parent.
CALL_TIMEOUT_S = {1_000: 60, 100_000: 180, 1_000_000: 600, 2_000_000: 900,
                  5_000_000: 1800, 10_000_000: 3600}
WORKER_TIMEOUT_S = {1_000: 900, 100_000: 2400, 1_000_000: 7200, 2_000_000: 7200,
                    5_000_000: 10800, 10_000_000: 14400}

# Slow-call classes (section 38).
SPEED_CLASSES = ((0.5, "FAST"), (2.0, "MODERATE"), (5.0, "SLOW"), (10.0, "VERY_SLOW"),
                 (float("inf"), "SEVERE"))
AGENT_READ = 8_000             # webapp/agent.py RESULT_CHARS: what the web agent reads of a reply
LARGE_RESPONSE = AGENT_READ

# Resource safety (section 54): a stress tier runs only if this much is free, as estimated from
# the 1M run of the same domain scaled linearly with a margin.
SAFETY_MARGIN = 1.5
HIGH_CARD_NS = (5, 10, 100, 200, 1000)


def speed_class(seconds: float) -> str:
    return next(label for limit, label in SPEED_CLASSES if seconds < limit)


def as_dict() -> dict:
    return {k: (list(v) if isinstance(v, tuple) else str(v) if isinstance(v, Path) else v)
            for k, v in globals().items()
            if k.isupper() and not k.startswith("_") and k != "SPEED_CLASSES"} | {
        "SPEED_CLASSES": [[str(a), b] for a, b in SPEED_CLASSES]}
