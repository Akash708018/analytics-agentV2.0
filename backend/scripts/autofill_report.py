"""How the model has been choosing, counted from confirmed contracts (25/09/2026).

    uv run python scripts/autofill_report.py [--root workspace]

Every contract confirmed from a model's fill carries `provenance`: for each field, whether the
model and the data agreed, whether the data overruled the model, whether only the model decided,
and whether the person changed it afterwards (webapp/autofill.py, contract/llm_filter.py). This
reads the current contract of every dataset in every workspace and counts them -- by verdict, by
the rule that judged, and by model -- so "is the model getting better or worse, and where does it
go wrong" is a table rather than an impression.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from backend.engine.contract import store  # noqa: E402


def collect(root: Path) -> list[dict]:
    """Current contracts read straight from the store's table, read-only: store.current() creates
    its table when missing, which a read-only connection refuses -- and the first version of this
    report swallowed that and found nothing (25/09/2026)."""
    rows = []
    for path in sorted(root.glob("*/session.duckdb")):
        try:
            con = duckdb.connect(str(path), read_only=True)
        except duckdb.Error:
            continue  # held by a running app; read it another time
        try:
            try:
                stored = con.execute(
                    f"SELECT dataset_name, contract_json FROM {store.CONTRACT_TABLE} "
                    f"WHERE is_current").fetchall()
            except duckdb.Error:
                continue  # no contract was ever confirmed here
            for dataset, text in stored:
                for field, p in (json.loads(text).get("provenance") or {}).items():
                    rows.append({"workspace": path.parent.name, "dataset": dataset,
                                 "field": field, **p})
        finally:
            con.close()
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT / "workspace")
    args = ap.parse_args(argv)
    rows = collect(args.root)
    if not rows:
        print(f"No confirmed contract under {args.root} was filled by a model yet.")
        return 0
    kinds = lambda r: r["field"].rsplit(".", 1)[-1] if "." in r["field"] else r["field"]
    print(f"{len(rows)} field(s) in {len({(r['workspace'], r['dataset']) for r in rows})} "
          f"contract(s)\n")
    print("by verdict:  " + ", ".join(f"{k} {v}" for k, v in
                                      Counter(r["status"] for r in rows).most_common()))
    print("by model:    " + ", ".join(f"{k or '-'} {v}" for k, v in
                                      Counter(r.get("model") for r in rows).most_common()))
    print("\nwhere the data overruled the model, by rule:")
    for rule, n in Counter(r.get("rule") or "-" for r in rows
                           if r["status"] == "overruled").most_common():
        print(f"  {rule:<6} {n}")
    print("\nwhat people changed afterwards, by field kind:")
    for kind, n in Counter(kinds(r) for r in rows if r["status"] == "user").most_common():
        print(f"  {kind:<12} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
