"""comparison.md and summary.md for the LLM benchmark, every figure read from benchmark.json."""

from __future__ import annotations

ROWS = [("Planner score", "planner_score", ""), ("End-to-end score", "e2e_score", ""),
        ("Intent accuracy", "intent_accuracy", "%"), ("Metric accuracy", "metric_accuracy", "%"),
        ("Dimension accuracy", "dimension_accuracy", "%"),
        ("Time-period accuracy", "time_accuracy", "%"),
        ("Valid tool-selection rate", "valid_tool_selection_rate", "%"),
        ("Planner step validity", "planner_step_validity", "%"),
        ("Invalid call rate", "invalid_call_rate", "%"),
        ("Tool efficiency", "tool_efficiency", ""),
        ("Redundant-call rate", "redundant_call_rate", "%"),
        ("Premature-stop rate", "premature_stop_rate", "%"),
        ("Over-analysis rate", "overanalysis_rate", "%"),
        ("Unsupported-claim rate", "unsupported_claim_rate", "%"),
        ("Causal-overreach rate", "causal_overreach_rate", "%"),
        ("Research reasoning score", "research_score", ""),
        ("Marketing subset score", "marketing_score", ""),
        ("Median latency (s)", "median_latency_s", ""), ("P95 latency (s)", "p95_latency_s", ""),
        ("Provider/rate-limit failures", "provider_failures", ""),
        ("JSON validity (planner)", "json_valid_rate", "%"),
        ("Items done: planner / e2e / research", None, "")]


def _v(s: dict, key, unit):
    if key is None:
        return f"{s['planner_done']} / {s['e2e_done']} / {s['research_done']}"
    v = s.get(key)
    return "not run" if v is None else f"{v}{unit}"


def comparison(b: dict) -> str:
    g, q = b["summaries"]["gemini"], b["summaries"]["groq"]
    pv = b["providers"]
    head = ["# Gemini vs Groq on the analytics engine", "",
            "The handoff named 'Grok'; the product is configured for Groq (api.groq.com), and the "
            "user chose Groq on 24/09/2026. No other model is included.", "",
            f"- Gemini: model `{g['model']}`, served {', '.join(g['served_models']) or '-'}; "
            f"now: {'available' if pv['gemini']['available_now'] else 'NOT_RUN_PROVIDER_UNAVAILABLE'}"
            f" ({pv['gemini']['detail']})",
            f"- Groq: model `{q['model']}`, served {', '.join(q['served_models']) or '-'}; "
            f"now: {'available' if pv['groq']['available_now'] else 'NOT_RUN_PROVIDER_UNAVAILABLE'}"
            f" ({pv['groq']['detail']})", "",
            "| Metric | Gemini | Groq |", "|---|---:|---:|"]
    rows = [f"| {n} | {_v(g, k, u)} | {_v(q, k, u)} |" for n, k, u in ROWS]
    tail = ["", "No winner is declared on writing style; every row above is a measured rate or a "
            "rubric score from scripts/llm_benchmark/run.py. Rate limits and provider errors are "
            "counted in their own row, not as reasoning failures."]
    return "\n".join(head + rows + tail) + "\n"


def summary(b: dict) -> str:
    qc = b["question_counts"]
    lines = ["# LLM analyst benchmark", "",
             f"Questions: {qc['planner']} planner, {qc['e2e']} end-to-end, "
             f"{qc['research']} frozen research cases, {qc['marketing']} in the marketing subset.",
             f"Fairness: temperature {b['fairness']['temperature']}, at most "
             f"{b['fairness']['max_calls']} analytical calls, results cut at "
             f"{b['fairness']['result_chars']} characters, retry policy "
             f"{b['fairness']['retry_policy']}.", ""]
    for p in ("gemini", "groq"):
        s = b["summaries"][p]
        lines += [f"## {p}", "",
                  f"- model {s['model']}; done {s['planner_done']}/{qc['planner']} planner, "
                  f"{s['e2e_done']}/{qc['e2e']} end-to-end, "
                  f"{s['research_done']}/{qc['research']} research",
                  f"- planner {s['planner_score']}, end-to-end {s['e2e_score']}, research "
                  f"{s['research_score']}, marketing {s['marketing_score']}",
                  f"- provider failures {s['provider_failures']} (rate limits "
                  f"{s['rate_limit_failures']})", ""]
    return "\n".join(lines) + "\n" + comparison(b)
