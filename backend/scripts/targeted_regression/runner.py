"""Run one targeted-regression case against one revision, in its own process.

    python scripts/targeted_regression/runner.py --src /path/to/revision/src --case D1 --out f.json

The revision's src goes first on sys.path and the import is checked, so a case cannot silently
measure the other revision (the editable install points at the main tree).
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--arg", action="append", default=[])
    a = ap.parse_args()
    src = Path(a.src).resolve()
    sys.path.insert(0, str(src))
    sys.path.insert(1, str(HERE.parent))
    import analytics_agent
    got = Path(analytics_agent.__file__).resolve()
    if src not in got.parents:
        raise SystemExit(f"imported {got}, not from {src}")
    import duckdb

    import cases  # noqa: E402 - after the revision is on the path
    tree = src.parent
    commit = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(tree), "status", "--porcelain", "--", "src"],
                           capture_output=True, text=True).stdout.strip()
    kwargs = {}
    for kv in a.arg:
        k, v = kv.split("=", 1)
        kwargs[k] = json.loads(v)
    t0 = time.perf_counter()
    try:
        observations, error = cases.CASES[a.case](**kwargs), None
    except Exception:  # noqa: BLE001 - a harness fault is recorded, not raised past the parent
        observations, error = [], traceback.format_exc()
    Path(a.out).write_text(json.dumps({
        "case": a.case, "src": str(src), "commit": commit, "src_dirty": dirty.splitlines(),
        "python": platform.python_version(), "duckdb": duckdb.__version__,
        "seconds": round(time.perf_counter() - t0, 2), "harness_error": error,
        "observations": observations}, indent=1, default=str))
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
