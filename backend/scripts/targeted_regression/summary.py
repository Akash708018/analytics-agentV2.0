"""summary.md and the console block for the targeted regression, read only from its JSON."""

from __future__ import annotations

import json
from pathlib import Path


def _j(out: Path, name: str, default):
    p = out / name
    return json.loads(p.read_text()) if p.exists() else default


def _num(x, unit=""):
    return "n/a" if x is None else f"{x}{unit}"


def markdown(out: Path) -> str:
    b = _j(out, "benchmark.json", {})
    s, env = b.get("summary", {}), b.get("environment", {})
    recs = _j(out, "before_after.json", [])
    perf = _j(out, "performance.json", {})
    fixes = _j(out, "harness_fixes.json", [])
    hj = b.get("phase_hj", {})
    lines = ["# Targeted regression: the Step 13 defects, before and after", "",
             f"Original revision `{env.get('original_commit')}` (the code the cross-domain "
             f"benchmark measured); fixed revision `{env.get('fixed_commit')}`"
             + (f", with uncommitted src files {env.get('fixed_src_dirty')}"
                if env.get("fixed_src_dirty") else "") + ". "
             f"Python {env.get('python')}, DuckDB {env.get('duckdb')}.", "",
             "| measure | value |", "|---|---|",
             f"| regression cases | {s.get('regression_cases')} |",
             f"| product defects tested | {s.get('product_defects_tested')} "
             f"({', '.join(s.get('product_defects', []))}) |",
             f"| defect cases whose original failure reproduced | "
             f"{s.get('original_failures_reproduced')} of {s.get('product_defect_cases')} |",
             f"| defect cases passing on the fixed code | {s.get('fixed_passes')} of "
             f"{s.get('product_defect_cases')} |",
             f"| controls | {s.get('controls')}, changed: {len(s.get('controls_changed', []))} |",
             f"| cases failing | {len(s.get('cases_failing', []))} |",
             f"| concurrency isolation (H, fixed code) | {s.get('concurrency_isolation')} |",
             f"| reproducibility (J, fixed code) | {s.get('reproducibility')} |", ""]
    if s.get("cases_failing"):
        lines += ["Failing cases:", ""] + [f"- {c}" for c in s["cases_failing"]] + [""]
    lines += ["## By case", "", "| case | category | before | after | passed | expected |",
              "|---|---|---|---|---|---|"]
    for r in recs:
        lines.append(f"| {r['case_id']} | {r['category']} | {r['before']['status']}"
                     f"{' ' + r['before']['exception_type'] if r['before']['exception_type'] else ''}"
                     f" | {r['after']['status']} | {'yes' if r['regression_passed'] else 'NO'} | "
                     f"{r['expected_behavior']} |")
    pc = perf.get("profile_column_by_rows", {})
    lines += ["", "## profile_column, median seconds over four columns", "",
              "| rows | before | after | speedup |", "|---:|---:|---:|---:|"]
    for n, v in pc.items():
        lines.append(f"| {int(n):,} | {v['before_median_s']} | {v['after_median_s']} | "
                     f"{v['speedup']}x |")
    g = perf.get("profile_column_time_growth", {})
    if g:
        lines += ["", "Time growth between sizes (before -> after): " + "; ".join(
            f"{k}: {v['before']}x -> {v['after']}x" for k, v in g.items())]
    cl = {k: v for k, v in perf.get("per_call", {}).items() if k.startswith("CLEANING.")}
    if cl:
        lines += ["", "## propose_cleaning_plan", "", "| case | before s | after s | speedup |",
                  "|---|---:|---:|---:|"]
        for k, v in cl.items():
            lines.append(f"| {k} | {v['before_median_s']} | {v['after_median_s']} | "
                         f"{v['speedup']}x |")
    lines += ["", "## Phases H and J", "", "```", json.dumps(hj, indent=1)[:4000], "```", "",
              "## Benchmark (harness) defects, kept apart from product defects", ""]
    for f in fixes:
        lines.append(f"- {f['id']} {f['category']}: {f['finding'][:300]}"
                     + (f" ({f['units']} units; failed checks {f['checks_failed_first_judgement']}"
                        f" at first judgement, {f['checks_failed_now']} of {f['checks_now']:,} "
                        f"now)" if f["id"] == "reverify" else ""))
    return "\n".join(lines) + "\n"


def console(out: Path) -> str:
    b = _j(out, "benchmark.json", {})
    s = b.get("summary", {})
    perf = _j(out, "performance.json", {})
    pc = perf.get("profile_column_by_rows", {})
    cl = [v for k, v in perf.get("per_call", {}).items() if k.startswith("CLEANING.plan_dirty")]
    recs = _j(out, "before_after.json", [])
    co = {r["rows"]: r for r in recs if r["case_id"].startswith("COHORT.")}
    big = co.get(100_000) or next(iter(co.values()), None)

    def pcv(n, k):
        return _num(pc.get(str(n), {}).get(k))
    lines = [
        "==============================", "PRODUCT REGRESSION", "==============================",
        f"Regression cases:                 {s.get('regression_cases')}",
        f"Product defects tested:           {s.get('product_defects_tested')}",
        f"Product defects fixed:            "
        f"{s.get('product_defects_tested', 0) - len(s.get('defects_still_failing', []))}",
        f"Still failing:                    {len(s.get('defects_still_failing', []))}",
        f"New regressions:                  {len(s.get('controls_changed', []))}",
        "", "Dedicated before/after tests:",
        f"Original failures:                {s.get('original_failures_reproduced')}",
        f"Fixed passes:                     {s.get('fixed_passes')}", "",
        "profile_column",
        f"1M before:                        {pcv(1000000, 'before_median_s')} sec",
        f"1M after:                         {pcv(1000000, 'after_median_s')} sec",
        f"1M speedup:                       {pcv(1000000, 'speedup')}x", "",
        f"10M before:                       {pcv(10000000, 'before_median_s')} sec",
        f"10M after:                        {pcv(10000000, 'after_median_s')} sec",
        f"10M speedup:                      {pcv(10000000, 'speedup')}x", "",
        "propose_cleaning_plan",
        f"1M before:                        {_num(cl[0]['before_median_s'] if cl else None)} sec",
        f"1M after:                         {_num(cl[0]['after_median_s'] if cl else None)} sec",
        f"Speedup:                          {_num(cl[0]['speedup'] if cl else None)}x", "",
        "Cohort response:",
        f"Before:                           "
        f"{_num((big or {}).get('before_extra', {}).get('characters'))} chars",
        f"After:                            "
        f"{_num((big or {}).get('after_extra', {}).get('characters'))} chars", "",
        f"Concurrency isolation:            {s.get('concurrency_isolation')}",
        f"Reproducibility:                  {s.get('reproducibility')}"]
    return "\n".join(lines)
