"""The targeted regression (Phase 14 Step 13, the handoff's Part A).

    uv run python scripts/targeted_regression/run.py cases [CASE ...]   # both revisions
    uv run python scripts/targeted_regression/run.py hj                 # H and J, fixed code
    uv run python scripts/targeted_regression/run.py report             # judge and write

Each case runs in its own process against the ORIGINAL revision's src (a detached worktree at
the commit the cross-domain benchmark measured) and then the FIXED revision's src. Raw
observations are kept under docs/benchmark/targeted_regression/raw/ and never rewritten by the
report. What each case must show is written below, in JUDGES, before any result is read.
"""

from __future__ import annotations

import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "benchmark" / "targeted_regression"
RAW = OUT / "raw"
ORIGINAL_SRC = Path(os.environ.get("TR_ORIGINAL_SRC", "/home/user/aa-orig/src"))
FIXED_SRC = Path(os.environ.get("TR_FIXED_SRC", str(ROOT / "src")))
BENCH_DATA = Path("/tmp/domain_benchmark_data")
HJ_DATA = Path("/tmp/tr_hj")
ORDER = ["D1", "D2", "D3", "D4", "D5", "D12", "D15", "D16", "RECS", "PROFILE_CORRECTNESS", "PROFILE_PERF",
         "CLEANING", "PRESCREEN", "COHORT", "WELCH", "PAGING", "CHART_ISOLATION"]

ESTIMATE = """ESTIMATED DURATION (this machine: 4 vCPU, 16 GB)
  targeted product regression, both revisions ....... 60-100 min
    (profile_column at 5M/10M on the original code alone is ~20 min, loading 10M twice ~6)
  corrected H and J on the fixed code ................ 10-15 min
  report ............................................. 1-2 min
  LLM benchmark ...................................... not run here: no provider key in this
                                                       container (NOT_RUN_PROVIDER_UNAVAILABLE)"""


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "run.log").open("a") as f:
        f.write(line + "\n")


def run_case(case: str, rev: str, src: Path, args: list[str] = ()) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / f"{case}_{rev}.json"
    if out.exists():
        log(f"{case} {rev}: already measured, kept ({out.name})")
        return json.loads(out.read_text())
    tmp = out.with_suffix(".partial")
    t0 = time.time()
    cmd = [sys.executable, str(Path(__file__).parent / "runner.py"), "--src", str(src),
           "--case", case, "--out", str(tmp)] + [x for a in args for x in ("--arg", a)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if not tmp.exists():
        tmp.write_text(json.dumps({"case": case, "harness_error": p.stderr[-4000:],
                                   "observations": []}))
    tmp.replace(out)
    d = json.loads(out.read_text())
    log(f"{case} {rev}: {len(d['observations'])} observation(s) in {time.time() - t0:.0f}s"
        + (f" HARNESS ERROR {d['harness_error'][-300:]}" if d.get("harness_error") else ""))
    return d


def cmd_cases(which: list[str]) -> None:
    print(ESTIMATE, flush=True)
    for case in which or ORDER:
        for rev, src in (("before", ORIGINAL_SRC), ("after", FIXED_SRC)):
            run_case(case, rev, src)


def cmd_hj() -> None:
    """Phases H and J of the cross-domain benchmark, on the fixed code, into their own data
    and output directories; the original code's corrected run is the benchmark's own."""
    env = dict(os.environ, DOMAIN_BENCH_DATA=str(HJ_DATA),
               DOMAIN_BENCH_OUT=str(OUT / "phase_hj"))
    for ph in ("H", "J"):
        t0 = time.time()
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "domain_benchmark" /
                                                "run.py"), "--only", ph], env=env,
                           capture_output=True, text=True)
        log(f"phase {ph} on the fixed code: exit {p.returncode} in {time.time() - t0:.0f}s")


# --------------------------------------------------------------------------- judging


def norm(text: str) -> str:
    """A reply without what legitimately differs between two runs: paths, times, file names."""
    text = re.sub(r"/\S*/workspace/\S*", "<path>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?", "<time>", text)
    text = re.sub(r"\d{8}-\d{6}(_\d+)?", "<ts>", text)
    # Welch's df is printed differently on purpose (D11: "1.569e+04" became "15,690.3"): the
    # value is compared, to three significant figures, rather than the text
    text = re.sub(r"df ([\d.,eE+]+?)(?=, p)",
                  lambda m: f"df {float(m.group(1).replace(',', '')):.3g}", text)
    return text


def by_sub(d: dict) -> dict:
    return {o["sub"]: o for o in d.get("observations", [])}


def _judge_d1(sub, b, a):
    if a["extra"].get("role") == "defect":
        ok = (a["status"] == "REFUSED" and "ANALYSIS_NOT_POSSIBLE" in a["response"]
              and "customer_id" in a["response"] and "VARCHAR" in a["response"]
              and a["extra"].get("executable"))
        return ok, "a refusal naming the column, its type and a runnable next step", \
            b["status"] == "EXCEPTION"
    return (a["status"] == "OK" and b["status"] == "OK"
            and norm(a["response"]) == norm(b["response"])), \
        "valid before and after, same answer", True


def _judge_d2(sub, b, a):
    if b["status"] == "EXCEPTION":
        return (a["status"] == "OK" and a["extra"]["states_zero_variance"]
                and not a["extra"]["p_value_printed"]), \
            "a stated 'no test' with no manufactured p-value", True
    return (a["status"] == b["status"] == "OK" and norm(a["response"]) == norm(b["response"])), \
        "unchanged, and a real result", True


def _judge_d3(sub, b, a):
    if a["extra"]["role"] == "defect":
        ok = (a["status"] == "REFUSED" and a["extra"]["rejected_at"] == "propose_dataset_contract"
              and a["extra"]["analysis_status"] is None and "propose_cleaning_plan"
              in a["response"])
        return ok, "rejected at the contract, never reaching an analysis", \
            b["extra"].get("analysis_status") in ("EXCEPTION",) or b["status"] == "EXCEPTION"
    return (a["extra"]["rejected_at"] is None and a["extra"]["analysis_status"] == "OK"
            and b["extra"]["analysis_status"] == "OK"), "accepted and analysed, before and after",\
        True


def _judge_d4(sub, b, a):
    x = a["extra"]
    if sub.startswith("site_"):
        return (a["status"] == "OK" and x["retries"] >= 1), \
            "the losing creator retries and the table exists once", b["status"] == "EXCEPTION"
    return (a["status"] == "OK" and x["distinct_answers"] == 1
            and x["analysis_run_rows"] == x["threads"]), \
        "every thread answers, one answer, one run row each", True


def _judge_d5(sub, b, a):
    x = a["extra"]
    ok = a["status"] == "OK" and x["statistic_matches"] and x["p_matches"]
    if x["role"] == "control":
        return ok and b["status"] == "OK" and b["extra"]["statistic_matches"], \
            "matches scipy before and after", True
    return ok, "matches scipy's statistic and p", b["status"] == "EXCEPTION"


def _judge_d12(sub, b, a):
    return a["status"] == "OK", "16 threads, 3,200 parses, none swapped or raised", \
        b["status"] in ("EXCEPTION", "WRONG")


def _judge_recs(sub, b, a):
    x, y = a["extra"], b["extra"]
    fol = x.get("followed") or {}
    if x["role"] == "control":
        return (a["status"] == b["status"] and x.get("next_step") == y.get("next_step")), \
            "unchanged refusal and next step", True
    if sub == "dup_rows_key":
        return (x.get("next_tool") == "propose_cleaning_plan" and x["executable"]
                and fol.get("status") == "OK"), "names the cleaning plan, runnable", \
            not y.get("executable")
    if sub == "correlation_one_measure":
        return (not x["self_correlation"] and x.get("next_tool") == "propose_dataset_contract"
                and fol.get("status") == "OK"), "asks for a contract, not v against v", \
            y["self_correlation"] or not y.get("executable")
    if sub == "correlation_two_measures":
        return (not x["self_correlation"] and 'against="w"' in (x.get("next_step") or "")
                and fol.get("status") == "OK"), "measure v against w, runnable", \
            y["self_correlation"] or not y.get("executable") or fol.get("status") != "OK"
    if sub == "corrupt_xlsx":
        return (a["status"] == "REFUSED" and not x["same_file_suggested"]), \
            "does not send the same file back", y["same_file_suggested"]
    if sub == "only_result_file":
        return (x["executable"] and fol.get("status") == "OK"), "the runnable path", \
            not y.get("executable")
    return False, "unjudged", False


def _judge_profile_corr(sub, b, a):
    x = a["extra"]
    same = norm(a["response"]) == norm(b["response"])
    fields = x.get("field_differences")
    want = "REFUSED" if sub.endswith(".nope") else "OK"
    return (same and a["status"] == b["status"] == want and not fields), \
        "same reply as the whole-table path, 0 field differences", True


def _judge_perf(sub, b, a):
    return a["status"] == "OK" and a["wall_time_seconds"] < b["wall_time_seconds"], \
        "faster, same reply head", True


def _plan_key(x):
    # the quoted examples are left out: the original code chose them at random (D15), so no
    # fixed text could equal them; D15's own case holds the fixed code to one text
    return x["kind"], x["column"], re.sub(r"'[^']*'(?:, '[^']*')*", "<examples>", x["text"])


def _judge_cleaning(sub, b, a):
    same = [_plan_key(x) for x in a["extra"]["actions"]] == \
        [_plan_key(x) for x in b["extra"]["actions"]]
    real = a["status"] == b["status"] == "OK" and a["extra"]["actions"]
    return bool(real) and same and a["wall_time_seconds"] < b["wall_time_seconds"], \
        "the same actions, word for word, in less time", True


def _judge_prescreen(sub, b, a):
    c = a["extra"].get("corpora", {})
    return (a["status"] == "OK" and all(v["false_negatives"] == 0 for v in c.values())), \
        "0 parseable values screened out", True


def _judge_cohort(sub, b, a):
    return (a["extra"]["characters"] < 8000 and a["extra"]["points"] == b["extra"]["points"]
            and a["extra"]["png_bytes"]), "under 8,000 characters, same points drawn", \
        b["extra"]["characters"] >= 8000 if sub.endswith("100000") else True


def _judge_welch(sub, b, a):
    x, y = a["extra"], b["extra"]
    same = y["df_value"] and x["df_value"] and abs(x["df_value"] - y["df_value"]) <= \
        abs(y["df_value"]) * 1e-3
    return (bool(same) and "e+" not in (x["df_text"] or "e+")), \
        "the same df, written as a number", True


def _judge_paging(sub, b, a):
    x, y = a["extra"], b["extra"]
    same = x["range"] == y["range"] and x["rows_after"] == y["rows_after"]
    if x["role"] == "control":
        return same and not x["states_cap"], "unchanged, no cap note", True
    return same and x["states_cap"], "the same page, and the cap stated", not y["states_cap"]


def _judge_iso(sub, b, a):
    x = a["extra"]
    clean = not x["other_values_in_chart_text"] and not x["other_values_in_table"] \
        and not x["names_other_dataset"]
    return clean and x["names_own_dataset"], "no value of the other dataset anywhere", True


def _judge_d16(sub, b, a):
    return a["status"] == "OK", "24 distinct files, each holding its own writer's rows", \
        b["status"] in ("WRONG", "EXCEPTION")


def _judge_d15(sub, b, a):
    return a["status"] == "OK", "the same plan text, examples included, twice", \
        b["status"] == "WRONG"


JUDGES = {"D15": _judge_d15, "D16": _judge_d16, "D1": _judge_d1, "D2": _judge_d2, "D3": _judge_d3, "D4": _judge_d4, "D5": _judge_d5,
          "D12": _judge_d12, "RECS": _judge_recs, "PROFILE_CORRECTNESS": _judge_profile_corr,
          "PROFILE_PERF": _judge_perf, "CLEANING": _judge_cleaning,
          "PRESCREEN": _judge_prescreen, "COHORT": _judge_cohort, "WELCH": _judge_welch,
          "PAGING": _judge_paging, "CHART_ISOLATION": _judge_iso}
DEFECT_OF = {"D15": "D15", "D16": "D16", "D1": "D1", "D2": "D2", "D3": "D3", "D4": "D4", "D5": "D5", "D12": "D12",
             "RECS": "D6-D9", "COHORT": "D10", "WELCH": "D11", "PAGING": "D13",
             "PROFILE_CORRECTNESS": "P1", "PROFILE_PERF": "P1", "CLEANING": "P2",
             "PRESCREEN": "P2", "CHART_ISOLATION": "H chart (harness)"}
SEVERITY = {"D1": "HIGH", "D2": "HIGH", "D3": "HIGH", "D4": "HIGH", "D5": "HIGH",
            "D12": "HIGH", "D15": "LOW", "D16": "CRITICAL", "D6-D9": "MEDIUM", "D10": "MEDIUM", "D11": "LOW", "D13": "LOW",
            "P1": "MEDIUM", "P2": "MEDIUM", "H chart (harness)": "CRITICAL-if-real"}


def _side(o: dict | None) -> dict:
    if not o:
        return {"status": "MISSING", "wall_time_seconds": None, "response": "",
                "exception_type": None, "exception_message": None}
    return {"status": o["status"], "wall_time_seconds": o["wall_time_seconds"],
            "response": o["response"][:1500], "exception_type": o["exception_type"],
            "exception_message": o["exception_message"]}


def judge_all() -> tuple[list[dict], dict]:
    records, meta = [], {}
    for case in ORDER:
        fb, fa = RAW / f"{case}_before.json", RAW / f"{case}_after.json"
        if not (fb.exists() and fa.exists()):
            continue
        db, da = json.loads(fb.read_text()), json.loads(fa.read_text())
        meta[case] = {"before_commit": db.get("commit"), "after_commit": da.get("commit"),
                      "after_src_dirty": da.get("src_dirty"),
                      "harness_errors": [x for x in (db.get("harness_error"),
                                                     da.get("harness_error")) if x]}
        bb, aa = by_sub(db), by_sub(da)
        for sub in list(dict.fromkeys(list(bb) + list(aa))):
            b, a = bb.get(sub), aa.get(sub)
            if not (a and b):
                ok, expected, reproduced = False, "observed in both revisions", False
            else:
                ok, expected, reproduced = JUDGES[case](sub, b, a)
            role = (a or b)["extra"].get("role", "")
            if sub.startswith("setup"):
                continue                                 # fixtures, not cases
            records.append({
                "case_id": f"{case}.{sub}",
                "category": "PRODUCT_DEFECT" if role == "defect" else
                {"control": "CONTROL", "performance": "PERFORMANCE",
                 "correctness": "CORRECTNESS", "ground_fact": "GROUND_FACT"}.get(role, role),
                "defect": DEFECT_OF[case], "severity": SEVERITY[DEFECT_OF[case]],
                "original_commit": db.get("commit"), "fixed_commit": da.get("commit"),
                "tool": (a or b)["tool"], "analysis": (a or b)["analysis"],
                "dataset": (a or b)["dataset"], "rows": (a or b)["rows"],
                "parameters": (a or b)["parameters"],
                "before": _side(b), "after": _side(a),
                "before_extra": (b or {}).get("extra"), "after_extra": (a or {}).get("extra"),
                "expected_behavior": expected,
                "original_failure_reproduced": bool(reproduced) if role == "defect" else None,
                "correctness_equal_where_required": ok if role in ("control", "correctness")
                else None,
                "regression_passed": bool(ok)})
    return records, meta


def speedups(records: list[dict]) -> dict:
    out = {}
    for r in records:
        if r["case_id"].startswith(("PROFILE_PERF.", "CLEANING.")) and \
                r["before"]["wall_time_seconds"] and r["after"]["wall_time_seconds"]:
            out[r["case_id"]] = {
                "rows": r["rows"], "before_median_s": r["before"]["wall_time_seconds"],
                "after_median_s": r["after"]["wall_time_seconds"],
                "before_min_s": (r["before_extra"] or {}).get("min_seconds"),
                "before_max_s": (r["before_extra"] or {}).get("max_seconds"),
                "after_min_s": (r["after_extra"] or {}).get("min_seconds"),
                "after_max_s": (r["after_extra"] or {}).get("max_seconds"),
                "speedup": round(r["before"]["wall_time_seconds"]
                                 / r["after"]["wall_time_seconds"], 2)}
    by = {}
    for k, v in out.items():
        if k.startswith("PROFILE_PERF."):
            by.setdefault(v["rows"], []).append(v)
    agg = {str(n): {"before_median_s": round(statistics.median(x["before_median_s"]
                                                                for x in v), 4),
                    "after_median_s": round(statistics.median(x["after_median_s"]
                                                               for x in v), 4)}
           for n, v in sorted(by.items())}
    for v in agg.values():
        v["speedup"] = round(v["before_median_s"] / v["after_median_s"], 2)
    sizes = sorted(by)
    growth = {f"{a:,}->{b:,}": {
        "before": round(agg[str(b)]["before_median_s"] / agg[str(a)]["before_median_s"], 2),
        "after": round(agg[str(b)]["after_median_s"] / agg[str(a)]["after_median_s"], 2)}
        for a, b in zip(sizes, sizes[1:])}
    return {"per_call": out, "profile_column_by_rows": agg, "profile_column_time_growth": growth}


def _hj() -> dict:
    """H and J before (the benchmark's corrected rerun on the original code) and after."""
    def load(base: Path) -> dict:
        cp = base / "checkpoint"
        r = {}
        d = cp / "H_concurrency" / "derived.json"
        if d.exists():
            x = json.loads(d.read_text())
            from collections import Counter
            r["concurrency_correctness"] = dict(Counter(c["status"] for c in x["correctness"]))
            r["isolation"] = {k: dict(Counter(i["status"] for i in x["isolation"][k]))
                              for k in ("correctness", "reports", "ledgers", "charts")}
            calls = []
            for f in sorted((cp / "H_concurrency").glob("calls*.jsonl")):
                calls += [json.loads(line) for line in f.read_text().splitlines()]
            r["concurrency_calls"] = dict(Counter(c.get("status") for c in calls
                                                  if c.get("kind") != "note"))
            r["concurrency_exceptions"] = sorted({f"{c.get('exception_type')}: "
                                                  f"{str(c.get('exception_message'))[:80]}"
                                                  for c in calls
                                                  if c.get("status") == "EXCEPTION"})
        for u in ("H_repeat", "H_traced", "H_isolation"):
            calls = []
            for f in sorted((cp / u).glob("calls*.jsonl")):
                calls += [json.loads(line) for line in f.read_text().splitlines()]
            if calls:
                from collections import Counter
                r[u] = dict(Counter(c.get("status") for c in calls if c.get("kind") != "note"))
        j = cp / "J_repro.json"
        if j.exists():
            x = json.loads(j.read_text())
            r["reproducibility"] = [{"domain": c["domain"], "rows": c["rows"],
                                     "status": c["status"],
                                     "results_identical": len(c["results_identical"]),
                                     "results_different": c["results_different"]} for c in x]
        return r
    return {"before": load(BENCH_DATA), "after": load(HJ_DATA)}


def _harness_fixes() -> list[dict]:
    """Every benchmark defect by id, and the reverified evidence that settles it."""
    doc = (ROOT / "docs" / "steps" / "phase14_step13_domain_benchmark.md").read_text()
    items = re.findall(r"^\s*- (2\.\d+) (.+?)(?=\n\s*- 2\.\d+ |\n\n|\n###)", doc, re.M | re.S)
    fixes = [{"id": i, "category": "BENCHMARK_DEFECT", "finding": " ".join(t.split())[:600]}
             for i, t in items]
    units, first_fail, now_fail, now_total = 0, 0, 0, 0
    for d in sorted((BENCH_DATA / "checkpoint").glob("[CDE]_*/derived.json")):
        x = json.loads(d.read_text())
        earlier = sorted(d.parent.glob("derived_*.json"))
        if earlier:
            e = json.loads(earlier[0].read_text())
            first_fail += sum(c.get("checks_failed") or 0 for c in e.get("correctness", []))
        units += 1
        now_fail += sum(c.get("checks_failed") or 0 for c in x.get("correctness", []))
        now_total += sum(c.get("checks_total") or 0 for c in x.get("correctness", []))
    return fixes + [{"id": "reverify", "category": "BENCHMARK_DEFECT",
                     "finding": "preserved evidence re-judged with the corrected oracle, no tool "
                                "called again", "units": units,
                     "checks_failed_first_judgement": first_fail,
                     "checks_failed_now": now_fail, "checks_now": now_total}]


def cmd_report() -> None:
    records, meta = judge_all()
    sp = speedups(records)
    hj = _hj()
    fixes = _harness_fixes()
    defects = sorted({r["defect"] for r in records if r["category"] == "PRODUCT_DEFECT"})
    failing = sorted({r["defect"] for r in records if not r["regression_passed"]})
    env = {"python": platform.python_version(),
           "original_commit": next((m["before_commit"] for m in meta.values()), None),
           "fixed_commit": next((m["after_commit"] for m in meta.values()), None),
           "fixed_src_dirty": sorted({f for m in meta.values()
                                      for f in (m["after_src_dirty"] or [])})}
    try:
        import duckdb
        env["duckdb"] = duckdb.__version__
    except ImportError:
        pass
    iso_after = hj["after"].get("isolation", {})
    conc_after = hj["after"].get("concurrency_calls", {})
    rep_after = hj["after"].get("reproducibility", [])
    summary = {
        "regression_cases": len(records),
        "product_defects_tested": len(defects), "product_defects": defects,
        "product_defect_cases": sum(r["category"] == "PRODUCT_DEFECT" for r in records),
        "original_failures_reproduced": sum(bool(r["original_failure_reproduced"])
                                            for r in records),
        "fixed_passes": sum(r["regression_passed"] for r in records
                            if r["category"] == "PRODUCT_DEFECT"),
        "cases_failing": [r["case_id"] for r in records if not r["regression_passed"]],
        "defects_still_failing": failing,
        "controls": sum(r["category"] == "CONTROL" for r in records),
        "controls_changed": [r["case_id"] for r in records
                             if r["category"] == "CONTROL" and not r["regression_passed"]],
        "harness_errors": {k: m["harness_errors"] for k, m in meta.items()
                           if m["harness_errors"]},
        "concurrency_isolation": "PASS" if iso_after and all(
            set(v) <= {"PASS"} for v in iso_after.values()) and not conc_after.get(
            "EXCEPTION") else "FAIL" if iso_after else "NOT_RUN",
        "reproducibility": "PASS" if rep_after and all(
            c["status"] == "PASS" for c in rep_after) else "FAIL" if rep_after else "NOT_RUN",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    w = lambda n, o: (OUT / n).write_text(json.dumps(o, indent=1, default=str))  # noqa: E731
    w("benchmark.json", {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "environment": env,
                         "summary": summary, "cases": meta, "phase_hj": hj})
    w("before_after.json", records)
    w("correctness.json", [r for r in records if r["category"] in ("CORRECTNESS", "CONTROL",
                                                                    "GROUND_FACT")])
    w("performance.json", sp)
    w("errors.json", [r for r in records if not r["regression_passed"]])
    w("warnings.json", [r for r in records if r["regression_passed"] and r["category"] ==
                        "PRODUCT_DEFECT" and not r["original_failure_reproduced"]])
    w("harness_fixes.json", fixes)
    import summary as S
    (OUT / "summary.md").write_text(S.markdown(OUT))
    print(S.console(OUT))


def main() -> int:
    sys.path.insert(0, str(Path(__file__).parent))
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else "report"), sys.argv[2:]
    {"cases": lambda: cmd_cases(rest), "hj": cmd_hj, "report": cmd_report,
     "estimate": lambda: print(ESTIMATE)}[cmd]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
