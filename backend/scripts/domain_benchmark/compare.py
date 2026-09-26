"""Before and after: the original run against the post-fix re-run (Phase 14 Step 13).

    uv run python scripts/domain_benchmark/compare.py BEFORE_DIR AFTER_DIR

Both directories are report outputs (benchmark.json, warnings.json, errors.json, performance.json).
Only units present in both are compared call by call, so a partial re-run is compared on what
it re-ran. Writes AFTER_DIR/before_after.json and AFTER_DIR/before_after.md, and every figure in
the .md is read from the .json.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

TIMED_TOOLS = ("propose_cleaning_plan", "profile_column", "profile_dataset",
               "propose_dataset_contract", "confirm_dataset_contract")


def _load(d: Path, name: str):
    p = d / name
    return json.loads(p.read_text()) if p.exists() else None


def _units(calls):
    return {c["unit"] for c in calls}


def _exceptions(calls, units):
    """[(unit, tool, analysis, type, calls)]: each distinct failure with how many calls met it."""
    n = Counter((c["unit"], c["tool"], c.get("analysis") or "", c.get("exception_type") or "")
                for c in calls if c["unit"] in units and c["status"] in ("EXCEPTION", "CRASH"))
    return [[*k, v] for k, v in sorted(n.items())]


def _medians(calls, units):
    by = defaultdict(list)
    for c in calls:
        if c["unit"] in units and c["status"] in ("OK", "REFUSED") and c.get("run_kind") != "cold" \
                and c["tool"] in TIMED_TOOLS:
            by[(c["tool"], c["rows"])].append(c["wall_time_seconds"])
    return {f"{t} @ {r:>10,}": round(statistics.median(v), 4) for (t, r), v in sorted(by.items())}


def _max_chars(calls, units, tool):
    v = [c.get("response_characters") or 0 for c in calls if c["unit"] in units
         and c["tool"] == tool]
    return max(v) if v else None


def _warnings(ws, units):
    return dict(Counter(w["category"] for w in ws or [] if w.get("unit") in units
                        or w.get("unit") is None))


def _classes(beh, units):
    recs = (beh or {}).get("records", [])
    return dict(Counter(r["classification"] for r in recs if r.get("unit") in units))


def _correct(calls, units):
    att = sum(c.get("checks_total") or 0 for c in calls if c["unit"] in units)
    bad = sum(c.get("checks_failed") or 0 for c in calls if c["unit"] in units)
    return {"checks": att, "failed": bad}


def compare(before: Path, after: Path) -> dict:
    b, a = _load(before, "benchmark.json"), _load(after, "benchmark.json")
    shared = _units(b["calls"]) & _units(a["calls"])
    side = {}
    for tag, d, bench in (("before", before, b), ("after", after, a)):
        calls = bench["calls"]
        side[tag] = {
            "commit": bench.get("environment", {}).get("git", {}).get("commit"),
            "calls": sum(1 for c in calls if c["unit"] in shared),
            "correctness": _correct(calls, shared),
            "exceptions": _exceptions(calls, shared),
            "behaviour_classes": _classes(_load(d, "behaviour.json"), shared),
            "warnings": _warnings(_load(d, "warnings.json"), shared),
            "median_seconds": _medians(calls, shared),
            "max_chart_reply_characters": _max_chars(calls, shared, "render_chart"),
        }
    out = {"shared_units": sorted(shared), **side}
    (after / "before_after.json").write_text(json.dumps(out, indent=1))
    return out


def markdown(out: dict) -> str:
    b, a = out["before"], out["after"]
    lines = ["# Before and after the Step 13 fixes", "",
             f"Units compared (present in both runs): {len(out['shared_units'])}", "",
             "| measure | before | after |", "|---|---|---|",
             f"| calls | {b['calls']:,} | {a['calls']:,} |",
             f"| checks | {b['correctness']['checks']:,} | {a['correctness']['checks']:,} |",
             f"| checks failed | {b['correctness']['failed']:,} | "
             f"{a['correctness']['failed']:,} |",
             f"| calls that raised | {sum(e[-1] for e in b['exceptions'])} | "
             f"{sum(e[-1] for e in a['exceptions'])} |",
             f"| largest chart reply (chars) | {b['max_chart_reply_characters']} | "
             f"{a['max_chart_reply_characters']} |"]
    for k in sorted(set(b["behaviour_classes"]) | set(a["behaviour_classes"])):
        lines.append(f"| behaviour {k} | {b['behaviour_classes'].get(k, 0)} | "
                     f"{a['behaviour_classes'].get(k, 0)} |")
    for k in sorted(set(b["warnings"]) | set(a["warnings"])):
        lines.append(f"| warning {k} | {b['warnings'].get(k, 0)} | {a['warnings'].get(k, 0)} |")
    lines += ["", "Median seconds (measured runs):", "", "| tool @ rows | before | after |",
              "|---|---|---|"]
    for k in sorted(set(b["median_seconds"]) | set(a["median_seconds"])):
        lines.append(f"| {k} | {b['median_seconds'].get(k, '')} | "
                     f"{a['median_seconds'].get(k, '')} |")
    def listed(es):
        return [f"- {' / '.join(map(str, e[:4]))}: {e[4]} call(s)" for e in es] or ["- none"]
    lines += ["", "Exceptions before:", ""] + listed(b["exceptions"])
    lines += ["", "Exceptions after:", ""] + listed(a["exceptions"])
    return "\n".join(lines) + "\n"


def main() -> int:
    before, after = Path(sys.argv[1]), Path(sys.argv[2])
    out = compare(before, after)
    (after / "before_after.md").write_text(markdown(out))
    print(markdown(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
