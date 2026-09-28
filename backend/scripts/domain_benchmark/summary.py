"""summary.md and the console summary, generated ONLY from the JSON result files.

Nothing here computes a benchmark figure from anything but benchmark.json and its subordinate
files; tests/test_domain_benchmark.py changes a total in the JSON and checks the summary moves
with it.
"""

from __future__ import annotations

import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

from . import config as C

DOMAIN_ORDER = ["financial", "hr", "sales", "marketing", "crm", "ecommerce", "logistics",
                "healthcare", "manufacturing", "education"]
TITLES = {"financial": "Financial", "hr": "HR", "sales": "Sales", "marketing": "Marketing",
          "crm": "CRM", "ecommerce": "E-commerce", "logistics": "Logistics",
          "healthcare": "Healthcare (synthetic)", "manufacturing": "Manufacturing",
          "education": "Education"}


def _j(out: Path, name: str, default=None):
    p = out / name
    return json.loads(p.read_text()) if p.exists() else default


def _f(x, nd=3):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:,.{nd}f}"
    if isinstance(x, int):
        return f"{x:,}"
    return str(x)


def _table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_f(c) if not isinstance(c, str) else c.replace("|", "/")
                              for c in r) + " |" for r in rows]
    return out


def write(out: Path) -> None:
    b = _j(out, "benchmark.json", {})
    s = b.get("summary", {})
    env = b.get("environment", {})
    corr = _j(out, "correctness.json", {})
    perf = _j(out, "performance.json", {"groups": []})
    scal = _j(out, "scaling.json", {"pairs": [], "median_time_growth": {}})
    mem = _j(out, "memory.json", {})
    errors = _j(out, "errors.json", [])
    warns = _j(out, "warnings.json", [])
    beh = _j(out, "behaviour.json", {"classes": {}, "records": []})
    charts = _j(out, "charts.json", [])
    cleaning = _j(out, "cleaning.json", [])
    iso = _j(out, "concurrency_isolation.json", {})
    repro = _j(out, "reproducibility.json", [])
    calls = b.get("calls", [])
    L: list[str] = []
    L += ["# Cross-domain benchmark: summary", "",
          "Generated from `benchmark.json` and the files beside it by "
          "`scripts/domain_benchmark/summary.py`; no figure below is typed by hand. "
          "Healthcare data is entirely synthetic; nothing here is medical advice.", ""]
    ct = s.get("correctness", {})
    ka = s.get("known_answers", {})
    L += ["## Executive summary", ""]
    L += _table(["measure", "value"], [
        ["domains", f"{len(s.get('domains_run', []))} / 10"],
        ["principal datasets", s.get("principal_datasets")],
        ["stress datasets run", s.get("stress_datasets")],
        ["calls executed", s.get("calls_executed")],
        ["correctness checks attempted", ct.get("checks_attempted")],
        ["checks passed", ct.get("checks_passed")],
        ["checks failed", ct.get("checks_failed")],
        ["verified calls skipped (no oracle / resource)", ct.get("calls_skipped")],
        ["known-answer checks passed", f"{ka.get('passed')} / {ka.get('total')}"],
        ["warnings", s.get("warnings")], ["errors", s.get("errors")],
        ["worker crashes", s.get("crashes")], ["exceptions escaping a tool",
                                               s.get("exceptions")],
        ["timeouts", s.get("timeouts")],
        ["total benchmark runtime (s)", s.get("total_runtime_seconds")],
        ["peak worker memory (MiB)", s.get("peak_worker_memory_mb")],
        ["largest workspace (MiB)", s.get("max_workspace_mb")],
    ])
    L += ["", f"Machine: {env.get('cpu_model')} x {env.get('cpu_cores')}, "
              f"{env.get('ram_total_mb')} MiB RAM, {env.get('swap')}; {env.get('os')}; "
              f"Python {(str(env.get('python') or '-').split() or ['-'])[0]}; commit "
              f"{str(env.get('git_commit', ''))[:12]} on {env.get('git_branch')}.", ""]
    sev = Counter(e["severity"] for e in errors)
    L += ["Defects by severity: " + ", ".join(f"{k} {sev.get(k, 0)}" for k in
                                             ("CRITICAL", "HIGH", "MEDIUM", "LOW")), ""]
    wrong = [e for e in errors if e.get("kind") == "wrong_result" or e["severity"] ==
             "CRITICAL"]
    L += ["### Wrong answers (worse than crashes)", ""]
    if wrong:
        L += _table(["#", "domain", "rows", "analysis", "expected", "observed"],
                    [[e["error_number"], e.get("domain"), e.get("size"),
                      f"{e.get('analysis')} {e.get('variant') or ''}",
                      str(e.get("expected_behavior"))[:80], str(e.get("observed_behavior"))[:90]]
                     for e in wrong[:60]])
        if len(wrong) > 60:
            L.append(f"... and {len(wrong) - 60} more in errors.json")
    else:
        L.append("None observed.")
    L.append("")

    L += ["## Correctness", "", "By domain and scale (a skipped check is not a pass):", ""]
    L += _table(["domain", "rows", "calls verified", "checks", "passed", "failed",
                 "calls skipped", "correct %"],
                [[r["domain"], r["rows"], r.get("calls_verified", 0),
                  r.get("checks_attempted", 0), r.get("checks_passed", 0),
                  r.get("checks_failed", 0), r.get("calls_skipped", 0),
                  r.get("correctness_percent")] for r in corr.get("summary", {}).get(
                      "by_domain_size", [])])
    tot = corr.get("summary", {}).get("total", {})
    L += ["", f"Total: {_f(tot.get('checks_passed'))} of {_f(tot.get('checks_attempted'))} "
              f"checks passed, {_f(tot.get('checks_failed'))} failed, "
              f"{_f(tot.get('calls_skipped'))} verified calls skipped.", ""]
    fails = [r for r in corr.get("records", []) if r.get("status") == "FAIL"]
    by_an = Counter(r["analysis"] for r in fails)
    L += ["Analyses with correctness failures: " + (", ".join(f"{a} ({n})" for a, n in
                                                         by_an.most_common()) or "none"), ""]
    kn = corr.get("known_answers", [])
    L += [f"Known-answer fixtures: {sum(1 for k in kn if k['status'] == 'PASS')} of "
          f"{len(kn)} passed."]
    for k in kn:
        if k["status"] == "FAIL":
            L.append(f"- FAIL {k['case']} {k['what']}: expected {k['expected']}, got "
                     f"{str(k['actual'])[:120]}")
    order = corr.get("temporal_order_independence", [])
    L += ["", "Sorted vs shuffled input (identical tables required): " +
          ", ".join(f"{o['case']} {o['status']}" for o in order), ""]

    L += ["## Performance", ""]
    groups = perf.get("groups", [])
    for n in (1_000, 100_000, 1_000_000) + tuple(C.STRESS_SIZES):
        g = [x for x in groups if x["rows"] == n and x["median_seconds"] is not None]
        if not g:
            continue
        by_tool = defaultdict(list)
        for x in g:
            by_tool[x["tool"] if x["tool"] != "compute_analysis" else
                    f"compute_analysis:{x['analysis']}"].append(x["median_seconds"])
        L += [f"### {n:,} rows: median seconds per call, across domains", ""]
        rows = sorted(((k, min(v), st.median(v), max(v), len(v)) for k, v in by_tool.items()),
                      key=lambda r: -r[3])
        L += _table(["tool / analysis", "fastest domain", "median domain", "slowest domain",
                     "groups"], [list(r) for r in rows])
        L.append("")
    L += ["### The same analysis across domains at 1,000,000 rows (median seconds)", ""]
    g1 = [x for x in groups if x["rows"] == 1_000_000 and x["median_seconds"] is not None]
    keyset = sorted({(x["tool"], x["analysis"]) for x in g1})
    rows = []
    for tool, an in keyset:
        rows.append([f"{tool}:{an}" if an else tool] +
                    [next((x["median_seconds"] for x in g1 if x["domain"] == d and
                           x["tool"] == tool and x["analysis"] == an and x["variant"] in
                           ("", "all", "t", "count", "g")), None) for d in DOMAIN_ORDER])
    L += _table(["tool"] + DOMAIN_ORDER, rows)
    L.append("")
    pc = [c for c in calls if c["tool"] == "profile_column" and c["rows"] == 1_000_000 and
          c["run_kind"] == "warm"]
    if pc:
        L += ["### profile_column at 1,000,000 rows by column type (seconds)", ""]
        bytype = defaultdict(list)
        types = {}
        for name in DOMAIN_ORDER:
            from .domains import DOMAINS
            types.update({(name, k): v for k, v in DOMAINS[name].types.items()})
        for c in pc:
            col = (c.get("parameters") or {}).get("column")
            bytype[types.get((c["domain"], col), "?")].append(c["wall_time_seconds"])
        L += _table(["column type", "calls", "median", "max"],
                    [[k, len(v), st.median(v), max(v)] for k, v in sorted(bytype.items())])
        slow = sorted(pc, key=lambda c: -c["wall_time_seconds"])[:10]
        L += ["", "Slowest columns:", ""]
        L += _table(["domain", "column", "seconds"],
                    [[c["domain"], (c.get("parameters") or {}).get("column"),
                      c["wall_time_seconds"]] for c in slow])
        L.append("")

    L += ["## Scaling", "", "Median time growth over every tool and analysis measured at both "
                            "sizes:", ""]
    L += _table(["rows", "data growth", "median time growth"],
                [[k, float(k.split("->")[1]) / float(k.split("->")[0]), v]
                 for k, v in scal.get("median_time_growth", {}).items() if v is not None])
    worst = sorted(scal.get("pairs", []), key=lambda x: -x["TIME_GROWTH_FACTOR"] /
                   x["DATA_GROWTH_FACTOR"])[:10]
    L += ["", "Ten worst scaling (time growth relative to data growth):", ""]
    L += _table(["domain", "tool", "analysis", "rows", "data x", "time x", "s before", "s after"],
                [[w["domain"], w["tool"], f"{w['analysis']} {w['variant']}",
                  f"{w['from_rows']:,}->{w['to_rows']:,}", w["DATA_GROWTH_FACTOR"],
                  w["TIME_GROWTH_FACTOR"], w["from_seconds"], w["to_seconds"]] for w in worst])
    L.append("")

    L += ["## Memory", ""]
    for n, runs in (mem.get("worker_peak_by_size") or {}).items():
        v = [r["child_peak_rss_mb"] for r in runs if r["child_peak_rss_mb"]]
        if v:
            L.append(f"- {int(n):,} rows: worker peak RSS median {st.median(v):,.0f} MiB, max "
                     f"{max(v):,.0f} MiB over {len(v)} worker run(s)")
    drift = sorted(mem.get("rss_drift_per_worker", []), key=lambda d: -d["growth_mb"])[:10]
    L += ["", "RSS growth from the first to the last call of a worker (accumulation):", ""]
    L += _table(["unit", "calls", "first MiB", "last MiB", "max MiB", "growth MiB"],
                [[d["unit"], d["calls"], d["first_after_mb"], d["last_after_mb"],
                  d["max_after_mb"], d["growth_mb"]] for d in drift])
    tp = mem.get("tracemalloc_peaks_mb") or []
    if tp:
        L += ["", f"Python-tracked allocations (tracemalloc, repeated mixed analyses): peak per "
                  f"call min {min(tp):.2f} MiB, max {max(tp):.2f} MiB over {len(tp)} calls."]
    L.append("")

    L += ["## Errors", ""]
    kinds = Counter((e["severity"], e.get("tool"), e.get("kind") or
                     (e.get("observed_behavior") or "")[:40]) for e in errors)
    L += _table(["severity", "tool", "what", "count"],
                [[k[0], k[1], k[2], v] for k, v in sorted(kinds.items(), key=lambda x: (
                    {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(x[0][0], 4), -x[1]))])
    L += ["", "## Crashes", ""]
    exc = [e for e in errors if e.get("exception")]
    if exc:
        L += _table(["#", "domain", "rows", "tool", "analysis", "exception"],
                    [[e["error_number"], e.get("domain"), e.get("size"), e.get("tool"),
                      e.get("analysis"), str(e.get("observed_behavior"))[:110]]
                     for e in exc[:60]])
    else:
        L.append("No exception escaped a tool and no worker process died.")
    L += ["", "## Warnings", ""]
    L += _table(["category", "count"], [[k, v] for k, v in
                                        Counter(w["category"] for w in warns).most_common()])
    L += ["", "## Behaviour under wrong and edge calls", ""]
    L += _table(["classification", "calls"], [[k, v] for k, v in sorted(
        beh.get("classes", {}).items())])
    fam = defaultdict(Counter)
    for r in beh.get("records", []):
        fam[r.get("family") or "other"][r["classification"]] += 1
    L += ["", "By family:", ""]
    L += _table(["family", "classes"], [[k, ", ".join(f"{c} {n}" for c, n in v.most_common())]
                                        for k, v in sorted(fam.items())])
    q = [r["message_quality"] for r in beh.get("records", []) if r.get("message_quality")]
    if q:
        L += ["", f"Refusal message quality over {len(q)} refusals: "
                  f"identifies the problem {sum(1 for x in q if x['identifies_problem'])}, "
                  f"names the parameter {sum(1 for x in q if x['names_offending_parameter'])} "
                  f"(of {sum(1 for x in q if x['names_offending_parameter'] is not None)} with "
                  f"a named parameter), explains what was expected "
                  f"{sum(1 for x in q if x['explains_expected'])}, suggests a correction "
                  f"{sum(1 for x in q if x['suggests_correction'])}; NEXT STEP followed: "
                  + ", ".join(f"{k} {v}" for k, v in Counter(
                      x.get("next_step_outcome") or "not followed" for x in q).most_common())]
    L += ["", "## Charts", ""]
    L += _table(["verdict", "count"], [[k, v] for k, v in Counter(c["verdict"] for c in
                                                                    charts).most_common()])
    L += ["", "## Cleaning plans", ""]
    verdicts = Counter()
    for p in cleaning:
        q = p["plan_quality"]
        items = q if isinstance(q, list) else [a for v in q.values() for a in v["actions"]]
        verdicts.update(a["verdict"] for a in items)
    L += _table(["verdict", "actions"], [[k, v] for k, v in verdicts.most_common()])
    for p in cleaning:
        if isinstance(p["plan_quality"], dict):
            for tag, v in p["plan_quality"].items():
                L.append(f"- dirty variant {tag}: expected {v['expect']}; actions "
                         f"{len(v['actions'])}; injected defects with no action: "
                         f"{', '.join(v['injected_without_action']) or 'none'}")
    L += ["", "## Concurrency and state isolation", ""]
    for bt in iso.get("batches", []):
        L.append(f"- {bt['mode']} x{bt['width']}: statuses "
                 f"{dict(Counter(bt['statuses']))}; distinct result files "
                 f"{len(set(p for p in bt['result_paths'] if p))} of "
                 f"{sum(1 for p in bt['result_paths'] if p)}")
    I = iso.get("isolation") or {}
    for part in ("reports", "ledgers", "charts"):
        v = I.get(part, [])
        L.append(f"- isolation {part}: {sum(1 for x in v if x['status'] == 'PASS')} of "
                 f"{len(v)} pass")
    ic = I.get("correctness", [])
    L.append(f"- isolation correctness: {sum(1 for x in ic if x['status'] == 'PASS')} of "
             f"{len(ic)} verified calls pass")
    L += ["", "## Reproducibility", ""]
    L += _table(["domain", "rows", "dataset identical", "results identical", "differ", "status"],
                [[r["domain"], r["rows"], str(r["dataset_identical"]),
                  len(r["results_identical"]), len(r["results_different"]), r["status"]]
                 for r in repro])
    L += ["", "## Domain observations", ""]
    for name in DOMAIN_ORDER:
        dr = _j(out, f"domain_results/{name}.json", {})
        L += [f"### {TITLES[name]}", ""]
        for r in dr.get("correctness", []):
            L.append(f"- {r['rows']:,} rows: {r.get('checks_passed', 0):,} of "
                     f"{r.get('checks_attempted', 0):,} checks pass, "
                     f"{r.get('checks_failed', 0):,} fail")
        e = dr.get("errors", [])
        L.append(f"- errors: {len(e)} (" + ", ".join(f"{k} {v}" for k, v in Counter(
            x["severity"] for x in e).most_common()) + ")")
        for x in e[:8]:
            L.append(f"  - {x['severity']} {x.get('tool')} {x.get('analysis') or ''} "
                     f"{x.get('size') or ''}: {str(x.get('observed_behavior'))[:140]}")
        slow = sorted((g for g in dr.get("performance", []) if g["rows"] == 1_000_000 and
                       g["median_seconds"]), key=lambda g: -g["median_seconds"])[:3]
        if slow:
            L.append("- slowest at 1M rows: " + "; ".join(
                f"{g['tool']} {g['analysis']} {g['median_seconds']:.2f}s" for g in slow))
        refused = [c for c in (dr.get("matrix") or []) if any(
            v == "REFUSED" for v in c.get("outcome_by_rows", {}).values())
            and c["class"] != "NOT_APPLICABLE"]
        if refused:
            L.append("- refused although classed REQUIRED/VALID/EDGE: " + ", ".join(
                f"{c['analysis']}{'/' + c['variant'] if c['variant'] else ''} ({c['class']})"
                for c in refused))
        L.append("")
    L += ["## Bottlenecks (measured)", ""]
    real = [c for c in calls if c["wall_time_seconds"] is not None]
    L += ["Ten slowest calls:", ""]
    L += _table(["seconds", "tool", "analysis", "domain", "rows"],
                [[c["wall_time_seconds"], c["tool"], c.get("analysis") or "", c.get("domain"),
                  c.get("rows")] for c in sorted(real, key=lambda c: -c["wall_time_seconds"])[:10]])
    L += ["", "Ten largest responses:", ""]
    L += _table(["characters", "tool", "analysis", "domain", "rows"],
                [[c["response_characters"], c["tool"], c.get("analysis") or "", c.get("domain"),
                  c.get("rows")] for c in sorted(real, key=lambda c: -(c["response_characters"]
                                                                        or 0))[:10]])
    L += ["", "Ten largest per-call memory peaks:", ""]
    L += _table(["peak MiB", "tool", "analysis", "unit", "rows"],
                [[r["peak_memory_mb"], r["tool"], r.get("analysis") or "", r["unit"], r["rows"]]
                 for r in (mem.get("top_calls_by_peak") or [])])
    et = Counter(e.get("tool") for e in errors)
    ed = Counter(e.get("domain") for e in errors)
    wt = Counter(w.get("tool") for w in warns if w.get("tool"))
    L += ["", "Tools with most errors: " + ", ".join(f"{k} {v}" for k, v in et.most_common(8)),
          "Domains with most errors: " + ", ".join(f"{k} {v}" for k, v in ed.most_common(8)),
          "Tools with most warnings: " + ", ".join(f"{k} {v}" for k, v in wt.most_common(8)),
          ""]
    L += ["## Regression candidates", "",
          "Every case below failed or guards a property the benchmark measured; each is small "
          "enough to run in the suite:", ""]
    seen = set()
    for e in sorted(errors, key=lambda e: {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2,
                                           "LOW": 3}.get(e["severity"], 4)):
        key = (e.get("tool"), e.get("analysis"), e.get("case") or e.get("workflow_step"),
               (e.get("exception") or e.get("kind") or "")[:30])
        if key in seen:
            continue
        seen.add(key)
        L.append(f"- [{e['severity']}] {e.get('tool')} {e.get('analysis') or ''} "
                 f"({e.get('case') or e.get('workflow_step')}): "
                 f"{str(e.get('observed_behavior'))[:120]}")
    L += ["- [guard] the known-answer fixture (perfect +1/-1/0 correlation, arithmetic trend, "
          "changepoint, 80/20 Pareto, ranking, retention grid, identical and separated groups, "
          "imbalance, 10% missing, 4 duplicates)",
          "- [guard] sorted vs shuffled input gives identical tables for every temporal "
          "analysis", ""]
    L += ["## Final technical findings", "",
          "Generated from the figures above; what they mean is discussed in "
          "docs/steps/phase14_step13_domain_benchmark.md.", ""]
    tot = corr.get("summary", {}).get("total", {})
    done = [k for k, v in (s.get("stress_tiers") or {}).items()
            if not str(v.get("status", "")).startswith("SKIPPED")]
    skipped = {k: v for k, v in (s.get("stress_tiers") or {}).items()
               if str(v.get("status", "")).startswith("SKIPPED")}
    sizes_ok = sorted({int(k.rsplit("_", 1)[1]) for k in done} | ({1_000_000} if any(
        r.get("rows") == 1_000_000 for r in corr.get("summary", {}).get("by_domain_size", []))
        else set()))
    L += [f"- Correctness: {_f(tot.get('checks_passed'))} of {_f(tot.get('checks_attempted'))} "
          f"independent checks passed; {_f(tot.get('checks_failed'))} failed "
          f"({', '.join(f'{a} {n}' for a, n in by_an.most_common()) or 'none'}).",
          f"- Defects: " + ", ".join(f"{k} {sev.get(k, 0)}" for k in ("CRITICAL", "HIGH",
                                                                      "MEDIUM", "LOW")) + ".",
          f"- Largest dataset processed to completion on this machine: "
          f"{max(sizes_ok):,} rows" if sizes_ok else "- No size completed.",
          f"- Stress tiers skipped for resources: " + (", ".join(
              f"{k} (needed ~{v.get('estimated_memory_mb')} MiB, had "
              f"{v.get('available_memory_mb')} MiB)" for k, v in skipped.items()) or "none"),
          f"- Median time growth 100k -> 1M: "
          f"{_f((s.get('median_time_growth') or {}).get('100000->1000000'), 2)}x for 10x rows.",
          f"- Peak worker memory: {s.get('peak_worker_memory_mb')} MiB.", ""]
    out.joinpath("summary.md").write_text("\n".join(L) + "\n")


def console(out: Path) -> None:
    b = _j(out, "benchmark.json", {})
    s = b.get("summary", {})
    ct = s.get("correctness", {})
    perf = _j(out, "performance.json", {"groups": []})
    corr = _j(out, "correctness.json", {"records": []})
    calls = b.get("calls", [])
    ms = s.get("median_call_seconds_by_rows", {})
    tg = s.get("median_time_growth", {})
    by_tool = defaultdict(list)
    for c in calls:
        if c.get("wall_time_seconds") is not None and c.get("rows") == 1_000_000:
            by_tool[c["tool"] if c["tool"] != "compute_analysis" else
                    f"compute_analysis:{c['analysis']}"].append(c["wall_time_seconds"])
    slow_tools = sorted(((k, st.median(v)) for k, v in by_tool.items()), key=lambda x: -x[1])[:3]
    fails = Counter(r["analysis"] for r in corr.get("records", []) if r.get("status") == "FAIL")
    t = s.get("call_time", {})
    lines = [
        "CROSS-DOMAIN BENCHMARK COMPLETE", "",
        f"Domains:               {len(s.get('domains_run', []))} / 10",
        f"Principal datasets:    {s.get('principal_datasets')}",
        f"Stress datasets:       {s.get('stress_datasets')}", "",
        f"Calls executed:        {s.get('calls_executed')}",
        f"Correctness checks:    {ct.get('checks_attempted')}",
        f"Correct:               {ct.get('checks_passed')}",
        f"Incorrect:             {ct.get('checks_failed')}",
        f"Skipped:               {ct.get('calls_skipped')} verified calls", "",
        f"Warnings:              {s.get('warnings')}",
        f"Errors:                {s.get('errors')}",
        f"Crashes:               {s.get('crashes')} worker, {s.get('exceptions')} exceptions", "",
        f"Fastest call:          {_f(t.get('fastest'), 5)} s",
        f"Median call:           {_f(t.get('median'), 4)} s",
        f"P95 call:              {_f(t.get('p95'), 3)} s",
        f"Slowest call:          {_f(t.get('slowest'), 2)} s", "",
        f"Peak memory:           {s.get('peak_worker_memory_mb')} MiB",
        f"Maximum workspace:     {s.get('max_workspace_mb')} MiB", "",
        f"1k median:             {_f(ms.get('1000'), 4)} sec",
        f"100k median:           {_f(ms.get('100000'), 4)} sec",
        f"1M median:             {_f(ms.get('1000000'), 4)} sec", "",
        "10x scaling median:",
        f"100k -> 1M             {_f(tg.get('100000->1000000'), 2)}x", "",
        "Slowest tools (1M rows, median):"]
    lines += [f"{k + 1}. {n} {v:.2f}s" for k, (n, v) in enumerate(slow_tools)]
    lines += ["", "Correctness failures:"]
    lines += [f"{k + 1}. {a} ({n} call(s))" for k, (a, n) in enumerate(fails.most_common())] or [
        "none"]
    lines += ["", "See:", "docs/benchmark/domain_benchmark/benchmark.json",
              "docs/benchmark/domain_benchmark/summary.md",
              "docs/benchmark/domain_benchmark/errors.json"]
    print("\n".join(lines))
