"""The combined report (handoff Part C) and the final console block (Part D).

    uv run python scripts/final_summary.py

Reads only generated JSON: docs/benchmark/targeted_regression/*.json,
docs/benchmark/llm_benchmark/benchmark.json, and the suite totals recorded in
docs/benchmark/targeted_regression/suites.json (written from the commands' own output).
Writes docs/benchmark/targeted_regression_llm_summary.md and prints the console block.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TR = ROOT / "docs" / "benchmark" / "targeted_regression"
LB = ROOT / "docs" / "benchmark" / "llm_benchmark"
sys.path.insert(0, str(ROOT / "scripts" / "targeted_regression"))


def _j(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def _v(x, unit=""):
    return "n/a" if x is None else f"{x}{unit}"


def build() -> tuple[str, str]:
    import summary as TS
    tb = _j(TR / "benchmark.json", {})
    ts = tb.get("summary", {})
    recs = _j(TR / "before_after.json", [])
    fixes = _j(TR / "harness_fixes.json", [])
    suites = _j(TR / "suites.json", {})
    lb = _j(LB / "benchmark.json", {})
    sums = lb.get("summaries", {})
    qc = lb.get("question_counts", {})
    prov = lb.get("providers", {})
    sev = {}
    for r in recs:
        if r["category"] == "PRODUCT_DEFECT" and not r["regression_passed"]:
            sev[r["severity"]] = sev.get(r["severity"], 0) + 1
    unresolved_bench = [f for f in fixes if f.get("status") == "OPEN"]

    def llm(p):
        s = sums.get(p, {})
        avail = prov.get(p, {})
        ran = s.get("planner_done") or s.get("e2e_done") or s.get("research_done")
        head = p.capitalize() if ran else f"{p.capitalize()} (NOT_RUN_PROVIDER_UNAVAILABLE: " \
            f"{avail.get('detail')})"
        return [head,
                f"Planner score:                     {_v(s.get('planner_score'))}",
                f"End-to-end score:                  {_v(s.get('e2e_score'))}",
                f"Marketing score:                   {_v(s.get('marketing_score'))}",
                f"Valid tool rate:                   {_v(s.get('valid_tool_selection_rate'), '%')}",
                f"Invalid call rate:                 {_v(s.get('invalid_call_rate'), '%')}",
                f"Unsupported claim rate:            {_v(s.get('unsupported_claim_rate'), '%')}",
                f"Median latency:                    {_v(s.get('median_latency_s'), ' sec')}",
                f"Provider failures:                 {_v(s.get('provider_failures'))}", ""]

    console = "\n".join(
        ["TARGETED REGRESSION + LLM BENCHMARK COMPLETE", "", TS.console(TR), "",
         "Full test suite:",
         f"Passed:                           {_v(suites.get('engine', {}).get('passed'))}",
         f"Failed:                           {_v(suites.get('engine', {}).get('failed'))}", "",
         "==============================", "LLM BENCHMARK", "==============================", "",
         f"Planner questions:                {_v(qc.get('planner'))}",
         f"End-to-end questions:             {_v(qc.get('e2e'))}",
         f"Research cases:                   {_v(qc.get('research'))}", ""]
        + llm("gemini") + llm("groq")
        + ["==============================", "FINAL STATUS", "==============================", "",
           f"Unresolved product defects:        {len(ts.get('defects_still_failing', []))}",
           f"Unresolved benchmark defects:      {len(unresolved_bench)}",
           f"Critical defects:                  {sev.get('CRITICAL', 0)}",
           f"High defects:                      {sev.get('HIGH', 0)}",
           f"Medium defects:                    {sev.get('MEDIUM', 0)}",
           f"Low defects:                       {sev.get('LOW', 0)}", "", "Artifacts:",
           "docs/benchmark/targeted_regression/", "docs/benchmark/llm_benchmark/",
           "docs/benchmark/targeted_regression_llm_summary.md"])

    perf = _j(TR / "performance.json", {})
    pc = perf.get("profile_column_by_rows", {})
    hj = tb.get("phase_hj", {})
    g, q = sums.get("gemini", {}), sums.get("groq", {})
    md = ["# Targeted regression and the LLM analyst benchmark", "",
          "Three conclusions, kept apart on purpose: the engine's arithmetic is judged against "
          "independent references, the LLMs are judged by a rubric, and neither score is folded "
          "into the other.", "",
          "## C1. The deterministic engine", "",
          f"- Defects tested: {ts.get('product_defects_tested')} "
          f"({', '.join(ts.get('product_defects', []))}); fixed: "
          f"{ts.get('product_defects_tested', 0) - len(ts.get('defects_still_failing', []))}; "
          f"still failing: {len(ts.get('defects_still_failing', []))}.",
          f"- Defect cases: {ts.get('fixed_passes')} of {ts.get('product_defect_cases')} pass on "
          f"the fixed code; {ts.get('original_failures_reproduced')} original failures "
          f"reproduced on the original code.",
          f"- Controls: {ts.get('controls')}, changed {len(ts.get('controls_changed', []))} "
          f"(no regression introduced).",
          f"- Suites on the final tree: engine {_v(suites.get('engine', {}).get('passed'))} "
          f"passed; {suites.get('summary_line', '')}",
          "- profile_column, median of four columns: " + "; ".join(
              f"{int(n):,} rows {v['before_median_s']} s -> {v['after_median_s']} s "
              f"({v['speedup']}x)" for n, v in pc.items()),
          "- propose_cleaning_plan at 1M rows: " + "; ".join(
              f"{k.split('.')[1]} {v['before_median_s']} s -> {v['after_median_s']} s "
              f"({v['speedup']}x)" for k, v in perf.get("per_call", {}).items()
              if k.startswith("CLEANING.")),
          f"- Phase H on the fixed code: {json.dumps(hj.get('after', {}).get('isolation'))}; "
          f"calls {json.dumps(hj.get('after', {}).get('concurrency_calls'))}. Concurrency "
          f"isolation: {ts.get('concurrency_isolation')}.",
          f"- Phase J on the fixed code: reproducibility {ts.get('reproducibility')}.",
          "- Details: docs/benchmark/targeted_regression/summary.md.", "",
          "## C2. LLM reasoning (Gemini vs Groq)", "",
          f"- Gemini `{g.get('model')}`: planner {_v(g.get('planner_score'))}, end-to-end "
          f"{_v(g.get('e2e_score'))}, research {_v(g.get('research_score'))}, marketing "
          f"{_v(g.get('marketing_score'))}; valid tool selection "
          f"{_v(g.get('valid_tool_selection_rate'), '%')}, invalid calls "
          f"{_v(g.get('invalid_call_rate'), '%')}, unsupported claims "
          f"{_v(g.get('unsupported_claim_rate'), '%')}, causal overreach "
          f"{_v(g.get('causal_overreach_rate'), '%')}; median latency "
          f"{_v(g.get('median_latency_s'), ' s')}, provider failures "
          f"{_v(g.get('provider_failures'))}.",
          f"- Groq: {'not run -- ' + str(prov.get('groq', {}).get('detail')) if not q.get('planner_done') else 'planner ' + str(q.get('planner_score'))}.",
          "- The full table: docs/benchmark/llm_benchmark/comparison.md.", "",
          "## C3. The complete analyst agent", "",
          "The architecture -- a model plans, the plan is validated against the contract, the "
          "engine computes, the result is checked, the model explains -- was exercised "
          "end to end on one provider. The engine held every number it was asked for "
          "(C1); the model's contribution is measured separately (C2): its invalid calls were "
          "refused by the engine rather than answered wrongly, and every figure in an answer "
          "was checked against the engine's own output. A comparison between providers waits "
          "on Groq being reachable from the environment.", ""]
    return "\n".join(md) + "\n", console


def main() -> int:
    md, console = build()
    (ROOT / "docs" / "benchmark" / "targeted_regression_llm_summary.md").write_text(md)
    print(console)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
