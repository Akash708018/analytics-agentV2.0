"""Phase K: aggregate the checkpoints into the result files, then write summary.md and print the
console summary FROM THOSE FILES. No figure in either is typed by hand: every number is read
from benchmark.json or its subordinate JSON files (section 34).

Classification of a call that tested behaviour rather than arithmetic (section 13), extended by
two documented classes: PASS_ACCEPTED (a valid edge call answered) and
FAIL_UNNECESSARY_REJECTION (a valid call refused).
"""

from __future__ import annotations

import json
import math
import re
import statistics as st
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import config as C
from .domains import DOMAINS
from .matrix import cases

EXPLAINS = re.compile(r"Declared|Available|accepted|one of|Columns:|expected|must|needs|takes|"
                      r"is not a|cannot|only", re.I)
WARNS = re.compile(r"cannot|could not|not |lost|PROVISIONAL|WARN|no rows|undated|outside|"
                   r"ambiguous|No spec proposed|CONVERT_TYPE|would be discarded", re.I)
# An answer that states why it did not compute (Step 13 run 2: "No test: g has 1 group(s)",
# "both coefficients are undefined rather than zero", "no spread and no interval", a rank test
# chosen "because the parametric test has no denominator").
DIAGNOSES = re.compile(r"No test|undefined rather than|no spread and no interval|"
                       r"Method chosen, not requested|No assessment|cannot be computed|"
                       r"has no (?:spread|variance)", re.I)
SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _load_jsonl(p: Path) -> list[dict]:
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def _pct(vals, q):
    s = sorted(vals)
    if not s:
        return None
    k = max(0, math.ceil(q * len(s)) - 1)
    return s[k]


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "min": min(vals), "median": st.median(vals), "mean": st.fmean(vals),
            "max": max(vals), "p95": _pct(vals, 0.95) if len(vals) >= 10 else None,
            "stdev": st.stdev(vals) if len(vals) > 1 else 0.0}


# --------------------------------------------------------------------------- collection


def collect() -> dict:
    units = {}
    for d in sorted(C.CHECKPOINT.iterdir()) if C.CHECKPOINT.exists() else []:
        if not d.is_dir():
            continue
        calls, notes = [], []
        for p in sorted(d.glob("calls*.jsonl")):
            for r in _load_jsonl(p):
                (notes if r.get("kind") == "note" else calls).append(r)
        derived = json.loads((d / "derived.json").read_text()) if (d / "derived.json").exists() \
            else {}
        units[d.name] = {"calls": calls, "notes": notes, "derived": derived}
    state = json.loads((C.CHECKPOINT / "state.json").read_text()) \
        if (C.CHECKPOINT / "state.json").exists() else {}
    repro = json.loads((C.CHECKPOINT / "J_repro.json").read_text()) \
        if (C.CHECKPOINT / "J_repro.json").exists() else []
    env = json.loads((C.OUT / "environment.json").read_text()) \
        if (C.OUT / "environment.json").exists() else {}
    return {"units": units, "state": state, "repro": repro, "env": env}


def classify(call: dict, follow: dict | None) -> dict:
    """The behaviour class of one call that had an expectation."""
    expect = call.get("expect")
    text = (call.get("response_full") or call.get("response_head") or "")
    mention = call.get("must_mention") or []
    names = all(m.lower() in text.lower() for m in mention) if mention else None
    st_ = call["status"]
    if st_ in ("EXCEPTION", "TIMEOUT"):
        cls = "FAIL_EXCEPTION"
    elif expect == "reject":
        cls = (("PASS_USEFUL_WARNING" if DIAGNOSES.search(text) else "FAIL_SILENT_ERROR")
               if st_ == "OK" else
               "PASS_SAFE_REJECTION" if names in (None, True) else "FAIL_MISLEADING_RESPONSE")
    elif expect == "accept":
        cls = "PASS_ACCEPTED" if st_ == "OK" else "FAIL_UNNECESSARY_REJECTION"
    elif expect == "warn":
        cls = ("PASS_SAFE_REJECTION" if st_ == "REFUSED" else
               "PASS_USEFUL_WARNING" if WARNS.search(text) else "FAIL_SILENT_ERROR")
    elif expect == "trivial":
        cls = "PASS_AUTOCORRECTED" if st_ == "OK" else "PASS_SAFE_REJECTION"
    else:  # either
        cls = ("PASS_SAFE_REJECTION" if st_ == "REFUSED" and names in (None, True) else
               "FAIL_MISLEADING_RESPONSE" if st_ == "REFUSED" else
               "PASS_USEFUL_WARNING" if DIAGNOSES.search(text) else "PASS_AUTOCORRECTED")
    quality = None
    if st_ == "REFUSED":
        nxt = call.get("next_step") or ""
        quality = {
            "identifies_problem": bool(re.search(r"^(BLOCKED|WHY)", text, re.M)),
            "names_offending_parameter": names,
            "explains_expected": bool(EXPLAINS.search(text)),
            "suggests_correction": bool(nxt) or "NEXT STEP" in text,
            "next_step": nxt or None,
            "next_step_outcome": (follow or {}).get("outcome"),
            "next_step_parse": (follow or {}).get("parse"),
            "avoids_irrelevant_next_step": (follow or {}).get("outcome") not in (
                "REFUSED_SAME_REASON",),
        }
    return {"classification": cls, "message_quality": quality}


SEVERITY = {"FAIL_WRONG_RESULT": "CRITICAL", "FAIL_PROCESS_CRASH": "HIGH",
            "FAIL_EXCEPTION": "HIGH", "FAIL_SILENT_ERROR": "HIGH",
            "FAIL_MISLEADING_RESPONSE": "MEDIUM", "FAIL_UNNECESSARY_REJECTION": "MEDIUM"}


# --------------------------------------------------------------------------- aggregation


def aggregate(raw: dict) -> dict:
    units = raw["units"]
    per_call, corr_all, charts, paging, plans, follows = [], [], [], [], [], []
    manifests, worker_runs, crashes, behaviour = [], [], [], []
    known, order, isolation, batches, stat_edge = [], [], {}, [], []
    for uname, u in units.items():
        dv = u["derived"]
        follow_by = {f.get("of_seq"): f for f in u["notes"] if f.get("event") == "follow"}
        # A dirty variant is one case of several steps: whether it warned is judged on all of
        # them (the load may be silent and the plan name the defect).
        case_text = defaultdict(str)
        if uname.startswith("F_dirty"):
            for r in u["calls"]:
                case_text[r.get("case")] += "\n" + (r.get("response_full") or "")
        corr_by_seq = {c.get("call_seq"): c for c in dv.get("correctness", [])}
        for c in dv.get("correctness", []):
            corr_all.append({"unit": uname, **{k: v for k, v in c.items() if k != "checks"},
                             "checks": c.get("checks", [])})
        for x in dv.get("isolation", {}).get("correctness", []):
            corr_all.append({"unit": uname, "isolation": True,
                             **{k: v for k, v in x.items() if k != "checks"},
                             "checks": x.get("checks", [])})
        charts += dv.get("charts", [])
        paging += dv.get("paging", [])
        if dv.get("plan_quality") is not None:
            plans.append({"unit": uname, "domain": dv.get("domain"), "rows": dv.get("rows"),
                          "plan_quality": dv["plan_quality"]})
        follows += [{"unit": uname, **f} for f in dv.get("follows", [])]
        if dv.get("manifest"):
            manifests.append(dv["manifest"])
        for e in dv.get("worker_runs", []):
            worker_runs.append({"unit": uname, **{k: v for k, v in e.items()
                                                   if k != "stdout_tail"}})
        crashes += [{"unit": uname, **c} for c in dv.get("crashes", [])]
        known += dv.get("known_checks", [])
        order += dv.get("order_independence", [])
        if dv.get("isolation"):
            isolation = dv["isolation"]
        batches += dv.get("batches", [])
        for r in u["calls"]:
            cr = corr_by_seq.get(r.get("seq")) if r.get("run_kind") != "cold" else None
            first_fail = (cr or {}).get("failed_checks", [None])[0] if cr and cr.get(
                "failed_checks") else None
            rec = {
                "unit": uname, "phase": r.get("phase"), "domain": r.get("domain"),
                "dataset": r.get("dataset"), "rows": r.get("rows"), "stage": r.get("stage"),
                "tool": r.get("tool"), "analysis": r.get("analysis"),
                "variant": r.get("variant"), "parameters": r.get("parameters"),
                "status": r.get("status"), "run": r.get("run"), "run_kind": r.get("run_kind"),
                "cache": r.get("cache"),
                "correct": (None if cr is None else cr["status"] == "PASS"),
                "correctness_status": (cr or {}).get("status"),
                "checks_total": (cr or {}).get("checks_total"),
                "checks_failed": (cr or {}).get("checks_failed"),
                "expected": (first_fail or {}).get("expected"),
                "actual": (first_fail or {}).get("actual"),
                "absolute_error": (first_fail or {}).get("absolute_error"),
                "relative_error": (first_fail or {}).get("relative_error"),
                "tolerance": (first_fail or {}).get("tolerance"),
                "wall_time_seconds": r.get("wall_time_seconds"),
                "speed_class": r.get("speed_class"),
                "memory_before_mb": r.get("memory_before_mb"),
                "memory_after_mb": r.get("memory_after_mb"),
                "peak_memory_mb": r.get("peak_memory_mb"),
                "python_traced_peak_mb": r.get("python_traced_peak_mb"),
                "response_characters": r.get("response_characters"),
                "response_bytes": r.get("response_bytes"),
                "response_tokens_approx": r.get("response_tokens_approx"),
                "warning": None, "exception_type": r.get("exception_type"),
                "exception_message": r.get("exception_message"),
                "stack_trace_file": r.get("stack_trace_file"),
                "reason_code": r.get("reason_code"), "next_step": r.get("next_step"),
                "class": r.get("class"), "thread": r.get("thread"), "seq": r.get("seq"),
                "response_head": (r.get("response_head") or "")[:300],
            }
            if r.get("expect") or r.get("class") == "NOT_APPLICABLE":
                if r.get("variant") != "follow":
                    rr = dict(r)
                    if case_text.get(r.get("case")):
                        rr["response_full"] = case_text[r["case"]]
                    b = classify({**rr, "expect": r.get("expect") or "either"},
                                 follow_by.get(r.get("seq")))
                    if r.get("expect") == "trivial" and cr is not None and cr["status"] == "FAIL":
                        b["classification"] = "FAIL_WRONG_RESULT"
                    behaviour.append({"unit": uname, "domain": r.get("domain"),
                                      "rows": r.get("rows"), "case": r.get("case") or r.get(
                                          "variant"), "family": r.get("family") or (
                                          "not_applicable" if r.get("class") == "NOT_APPLICABLE"
                                          else None),
                                      "tool": r.get("tool"), "analysis": r.get("analysis"),
                                      "parameters": r.get("parameters"),
                                      "expect": r.get("expect"), "status": r.get("status"),
                                      "exception_type": r.get("exception_type"),
                                      "message": (r.get("response_full") or r.get(
                                          "response_head") or "")[:3000], **b})
                    rec["behaviour_class"] = b["classification"]
            per_call.append(rec)
    return {"per_call": per_call, "correctness": corr_all, "charts": charts, "paging": paging,
            "plans": plans, "follows": follows, "manifests": manifests,
            "worker_runs": worker_runs, "crashes": crashes, "behaviour": behaviour,
            "known": known, "order": order, "isolation": isolation, "batches": batches}


def performance(per_call: list[dict]) -> dict:
    groups = defaultdict(lambda: {"warm": [], "cold": [], "chars": [], "status": Counter()})
    for r in per_call:
        if r["phase"] not in ("C", "D", "E", "I") or r["wall_time_seconds"] is None:
            continue
        key = (r["domain"], r["rows"], r["tool"], r["analysis"] or "", r["variant"] or "")
        g = groups[key]
        g["status"][r["status"]] += 1
        g["chars"].append(r["response_characters"] or 0)
        (g["cold"] if r["run_kind"] == "cold" else g["warm"]).append(r["wall_time_seconds"])
    out = []
    for (dom, rows, tool, analysis, variant), g in groups.items():
        warm = g["warm"] or g["cold"]
        out.append({"domain": dom, "rows": rows, "tool": tool, "analysis": analysis,
                    "variant": variant, "measured": _stats(g["warm"]),
                    "cold_seconds": g["cold"][0] if g["cold"] else None,
                    "median_seconds": st.median(warm) if warm else None,
                    "speed_class": C.speed_class(st.median(warm)) if warm else None,
                    "max_response_characters": max(g["chars"]) if g["chars"] else 0,
                    "statuses": dict(g["status"]),
                    "runs_note": "cold = first call on the dataset; warm = the measured runs; "
                                 "no application cache, so all are CACHE_MISS"})
    return {"groups": out}


def scaling(perf: dict) -> dict:
    by = defaultdict(dict)
    for g in perf["groups"]:
        if g["median_seconds"] is None:
            continue
        by[(g["domain"], g["tool"], g["analysis"], g["variant"])][g["rows"]] = g["median_seconds"]
    pairs = [(1_000, 100_000), (100_000, 1_000_000), (1_000_000, 2_000_000),
             (2_000_000, 5_000_000), (5_000_000, 10_000_000)]
    out = []
    for key, t in by.items():
        for a, b in pairs:
            if a in t and b in t and t[a] > 0:
                data_f, time_f = b / a, t[b] / t[a]
                out.append({"domain": key[0], "tool": key[1], "analysis": key[2],
                            "variant": key[3], "from_rows": a, "to_rows": b,
                            "DATA_GROWTH_FACTOR": data_f, "TIME_GROWTH_FACTOR": round(time_f, 3),
                            "from_seconds": t[a], "to_seconds": t[b],
                            "suspicious": time_f > data_f and t[b] >= 0.05})
    return {"pairs": out,
            "median_time_growth": {f"{a}->{b}": (st.median(x["TIME_GROWTH_FACTOR"] for x in out
                                                           if x["from_rows"] == a
                                                           and x["to_rows"] == b)
                                                 if any(x["from_rows"] == a and x["to_rows"] == b
                                                        for x in out) else None)
                                   for a, b in pairs}}


def worker_peaks(per_call) -> dict:
    """Each worker's own peak, from its per-call VmHWM / VmRSS records."""
    peaks = defaultdict(float)
    for r in per_call:
        for k in ("peak_memory_mb", "memory_after_mb", "memory_before_mb"):
            if r.get(k):
                peaks[r["unit"]] = max(peaks[r["unit"]], r[k])
    return peaks


def memory(per_call, worker_runs, units) -> dict:
    by_size = defaultdict(list)
    peaks = worker_peaks(per_call)
    for w in worker_runs:
        m = re.match(r"[CDEI]_(\w+?)(?:_(\d+))?$", w["unit"])
        rows = {"C": 1_000, "D": 100_000, "E": 1_000_000}.get(w["unit"][0])
        if w["unit"].startswith("I_"):
            rows = int(w["unit"].rsplit("_", 1)[1])
        if rows and m:
            by_size[rows].append({"unit": w["unit"], "child_peak_rss_mb": peaks.get(w["unit"]),
                                  "wait4_ru_maxrss_mb_inherited": w["child_peak_rss_mb"]})
    top_calls = sorted((r for r in per_call if r.get("peak_memory_mb")),
                       key=lambda r: -r["peak_memory_mb"])[:10]
    drift = []
    for uname, u in units.items():
        seq = [c for c in u["calls"] if c.get("memory_after_mb") and not c.get("thread")]
        if len(seq) > 10:
            drift.append({"unit": uname, "first_after_mb": seq[0]["memory_after_mb"],
                          "last_after_mb": seq[-1]["memory_after_mb"],
                          "max_after_mb": max(c["memory_after_mb"] for c in seq),
                          "growth_mb": round(seq[-1]["memory_after_mb"] -
                                             seq[0]["memory_after_mb"], 2), "calls": len(seq)})
    rep = units.get("H_repeat", {}).get("calls", [])
    series = defaultdict(list)
    for c in rep:
        series[c.get("variant", "")[:6]].append(c.get("memory_after_mb"))
    traced = [c.get("python_traced_peak_mb") for c in units.get("H_traced", {}).get("calls", [])
              if c.get("python_traced_peak_mb") is not None]
    return {"worker_peak_by_size": {str(k): v for k, v in sorted(by_size.items())},
            "top_calls_by_peak": [{k: r[k] for k in ("unit", "tool", "analysis", "variant",
                                                     "rows", "peak_memory_mb",
                                                     "memory_before_mb", "memory_after_mb")}
                                  for r in top_calls],
            "rss_drift_per_worker": drift,
            "repeat_series_mb": {k: [v for v in vals if v is not None] for k, vals in
                                 series.items()},
            "tracemalloc_peaks_mb": traced,
            "note": "peak_memory_mb is VmHWM after a reset through /proc/self/clear_refs just "
                    "before each call; a worker's peak is the largest of those. wait4's "
                    "ru_maxrss is kept for the record but is NOT the worker's peak: Linux carries "
                    "the parent's high-water mark across fork+exec"}


def errors_and_warnings(agg: dict, perf: dict, scal: dict) -> tuple[list, list]:
    errors, warnings = [], []

    def err(**kw):
        errors.append({"error_number": len(errors) + 1, **kw})

    for r in agg["per_call"]:
        if r["status"] in ("EXCEPTION", "TIMEOUT"):
            err(timestamp=None, domain=r["domain"], size=r["rows"], file=r["dataset"],
                workflow_step=r["stage"], tool=r["tool"], analysis=r["analysis"],
                supplied_arguments=r["parameters"], expected_behavior="a result or a refusal",
                observed_behavior=f"{r['exception_type']}: {r['exception_message']}",
                exception=r["exception_type"], stack_trace=r["stack_trace_file"],
                severity="HIGH", recovered=True,
                subsequent_operations_valid="the worker continued with the next call",
                unit=r["unit"], run_kind=r["run_kind"])
    for c in agg["crashes"]:
        err(timestamp=c.get("timestamp"), domain=c.get("domain"), size=c.get("rows"),
            file=c.get("dataset_file"), workflow_step=c.get("stage"), tool=c.get("tool"),
            analysis=c.get("analysis"), supplied_arguments=c.get("parameters"),
            expected_behavior="the process survives", observed_behavior=c.get("exception_type"),
            exception=c.get("exception_type"), stack_trace=c.get("exception_message"),
            severity="HIGH", recovered=c.get("process_usable_afterwards") is not None,
            subsequent_operations_valid=c.get("process_usable_afterwards"), unit=c.get("unit"))
    for c in agg["correctness"]:
        if c.get("status") in ("FAIL", "ORACLE_ERROR"):
            first = (c.get("failed_checks") or [{}])[0]
            err(timestamp=None, domain=c.get("domain"), size=c.get("rows"), file=None,
                workflow_step="analyses", tool="compute_analysis", analysis=c.get("analysis"),
                supplied_arguments=c.get("parameters"),
                expected_behavior=f"{first.get('what')} = {first.get('expected')}",
                observed_behavior=f"{first.get('actual')} ({c.get('checks_failed')} of "
                                  f"{c.get('checks_total')} checks failed)",
                exception=None, stack_trace=None,
                severity="CRITICAL" if c.get("status") == "FAIL" else "MEDIUM",
                recovered=True, subsequent_operations_valid=True, unit=c.get("unit"),
                kind="wrong_result" if c.get("status") == "FAIL" else "oracle_error",
                variant=c.get("variant"), klass=c.get("class"))
    for b in agg["behaviour"]:
        if b["classification"].startswith("FAIL"):
            err(timestamp=None, domain=b.get("domain"), size=b.get("rows"), file=None,
                workflow_step=b.get("family"), tool=b["tool"], analysis=b.get("analysis"),
                supplied_arguments=b.get("parameters"), expected_behavior=b.get("expect"),
                observed_behavior=f"{b['classification']}: {b['message'][:400]}",
                exception=b.get("exception_type"), stack_trace=None,
                severity=SEVERITY.get(b["classification"], "LOW"), recovered=True,
                subsequent_operations_valid=True, unit=b["unit"], case=b.get("case"))
    for ch in agg["charts"]:
        if ch["verdict"] == "FAIL":
            err(timestamp=None, domain=ch.get("domain"), size=ch.get("rows"), file=None,
                workflow_step="charts", tool="render_chart", analysis=ch.get("analysis"),
                supplied_arguments=ch.get("parameters"),
                expected_behavior="a valid PNG that says what it drew",
                observed_behavior=json.dumps(ch.get("checks")), exception=None, stack_trace=None,
                severity="MEDIUM", recovered=True, subsequent_operations_valid=True,
                unit=ch.get("unit"))
    for p in agg["paging"]:
        if p["status"] == "FAIL":
            err(timestamp=None, domain=p.get("domain"), size=p.get("rows"), file=p["path"],
                workflow_step="read_result", tool="read_result_file", analysis=None,
                supplied_arguments={"start": p["start"], "limit": p["limit"]},
                expected_behavior=f"{p['expected_rows']} rows", observed_behavior=
                f"{p['found_rows']} found", exception=None, stack_trace=None, severity="HIGH",
                recovered=True, subsequent_operations_valid=True, unit=p.get("unit"))
    for pl in agg["plans"]:
        q = pl["plan_quality"]
        items = q if isinstance(q, list) else [a for v in q.values() for a in v["actions"]]
        for a in items:
            if a["verdict"] in ("dangerous", "potentially_destructive"):
                err(timestamp=None, domain=pl.get("domain"), size=pl.get("rows"), file=None,
                    workflow_step="clean", tool="propose_cleaning_plan", analysis=None,
                    supplied_arguments={"action": a["id"], "kind": a["kind"],
                                        "column": a["column"]},
                    expected_behavior="only actions that fix inserted defects",
                    observed_behavior=f"{a['verdict']}: {a['why']} -- {a['text'][:200]}",
                    exception=None, stack_trace=None,
                    severity="HIGH" if a["verdict"] == "dangerous" else "MEDIUM",
                    recovered=True, subsequent_operations_valid=True, unit=pl["unit"])
    for k in agg["known"]:
        if k["status"] == "FAIL":
            err(timestamp=None, domain="known_answer", size=240, file="known_answer.csv",
                workflow_step="known", tool="compute_analysis", analysis=k["case"],
                supplied_arguments=None, expected_behavior=f"{k['what']} = {k['expected']}",
                observed_behavior=str(k["actual"])[:400], exception=None, stack_trace=None,
                severity="CRITICAL", recovered=True, subsequent_operations_valid=True,
                unit="B_known")
    iso = agg["isolation"] or {}
    for part in ("reports", "ledgers", "charts"):
        for x in iso.get(part, []):
            if x["status"] == "FAIL":
                err(timestamp=None, domain=x["dataset"], size=10_000, file=None,
                    workflow_step="isolation", tool=part, analysis=None,
                    supplied_arguments=None, expected_behavior="only its own dataset",
                    observed_behavior=json.dumps(x)[:400], exception=None, stack_trace=None,
                    severity="CRITICAL", recovered=True, subsequent_operations_valid=None,
                    unit="H_isolation")
    for o in agg["order"]:
        if o["status"] == "FAIL":
            err(timestamp=None, domain="temporal", size=730, file=None,
                workflow_step="temporal", tool="compute_analysis", analysis=o["case"],
                supplied_arguments=None, expected_behavior="identical tables sorted or shuffled",
                observed_behavior="tables differ", exception=None, stack_trace=None,
                severity="CRITICAL", recovered=True, subsequent_operations_valid=True,
                unit="B_temporal")
    # warnings
    for r in agg["per_call"]:
        if r["speed_class"] in ("SLOW", "VERY_SLOW", "SEVERE"):
            warnings.append({"category": "slow_call", "unit": r["unit"], "tool": r["tool"],
                             "analysis": r["analysis"], "rows": r["rows"],
                             "seconds": r["wall_time_seconds"], "class": r["speed_class"],
                             "domain": r["domain"]})
        if (r["response_characters"] or 0) > C.LARGE_RESPONSE:
            warnings.append({"category": "excessive_response_size", "unit": r["unit"],
                             "tool": r["tool"], "analysis": r["analysis"], "rows": r["rows"],
                             "characters": r["response_characters"], "domain": r["domain"]})
        if r["status"] == "REFUSED" and r["stage"] == "analyses" and r["class"] in (
                "REQUIRED", "VALID") and r["run_kind"] == "cold":
            warnings.append({"category": "unsupported_operation", "unit": r["unit"],
                             "tool": r["tool"], "analysis": r["analysis"], "variant":
                             r["variant"], "rows": r["rows"], "domain": r["domain"],
                             "class": r["class"], "reason_code": r["reason_code"],
                             "why": next((ln for ln in r["response_head"].splitlines()
                                          if ln.startswith("WHY")), r["response_head"][:200])})
    for f in agg["follows"]:
        if f.get("outcome") in ("REFUSED_SAME_REASON",) or (
                f.get("outcome") == "NOT_EXECUTED" and f.get("parse") != "executable"
                and f.get("parse") != "names a tool without arguments"):
            warnings.append({"category": "irrelevant_or_unexecutable_recommendation",
                             "unit": f["unit"], "next_step": f.get("next_step"),
                             "outcome": f.get("outcome"), "parse": f.get("parse")})
    for s in scal["pairs"]:
        if s["suspicious"]:
            warnings.append({"category": "suspicious_scaling", **s})
    for c in agg["correctness"]:
        if c.get("status") in ("SKIPPED_UNSUPPORTED", "SKIPPED_RESOURCE_LIMIT") or c.get(
                "skipped"):
            warnings.append({"category": "questionable_or_unverified_result",
                             "unit": c.get("unit"), "analysis": c.get("analysis"),
                             "variant": c.get("variant"), "status": c.get("status"),
                             "skipped": c.get("skipped")[:3]})
    return errors, warnings


def correctness_summary(corr: list[dict], known: list[dict]) -> dict:
    by = defaultdict(lambda: Counter())
    for c in corr:
        dom, rows = c.get("domain") or "?", c.get("rows")
        k = by[(dom, rows)]
        k["checks_attempted"] += c.get("checks_total") or 0
        k["checks_passed"] += c.get("checks_passed") or 0
        k["checks_failed"] += c.get("checks_failed") or 0
        k["calls_verified"] += 1
        k[f"calls_{c.get('status')}"] += 1
        if c.get("status", "").startswith("SKIPPED"):
            k["calls_skipped"] += 1
    rows = []
    for (dom, n), k in sorted(by.items(), key=lambda x: (str(x[0][0]), x[0][1] or 0)):
        att = k["checks_attempted"]
        rows.append({"domain": dom, "rows": n, **dict(k),
                     "correctness_percent": round(100 * k["checks_passed"] / att, 4)
                     if att else None})
    tot = Counter()
    for r in rows:
        for key in ("checks_attempted", "checks_passed", "checks_failed", "calls_verified",
                    "calls_skipped"):
            tot[key] += r.get(key, 0)
    kn = Counter(k["status"] for k in known)
    return {"by_domain_size": rows, "total": dict(tot),
            "known_answers": {"passed": kn["PASS"], "failed": kn["FAIL"], "total": len(known)}}


def matrix_out(per_call) -> dict:
    sys_names = None
    try:
        import sys
        sys.path.insert(0, str(C.ROOT.parent))
        from backend.engine.analysis import registry
        sys_names = list(registry.REGISTRY)
    except Exception:  # noqa: BLE001
        sys_names = []
    outcome = defaultdict(dict)
    for r in per_call:
        if r["phase"] in ("C", "D", "E") and r["tool"] == "compute_analysis" and r[
                "run_kind"] == "cold" and r["stage"] in ("analyses", "not_applicable"):
            outcome[(r["domain"], r["analysis"], r["variant"])][r["rows"]] = r["status"]
    out = {}
    for name, d in DOMAINS.items():
        out[name] = [{**c, "outcome_by_rows": outcome.get((name, c["analysis"], c["variant"]),
                                                          {})}
                     for c in cases(d, sys_names)]
    return {"registered_analyses": sys_names, "domains": out}


def main() -> None:
    t0 = time.time()
    raw = collect()
    agg = aggregate(raw)
    perf = performance(agg["per_call"])
    scal = scaling(perf)
    mem = memory(agg["per_call"], agg["worker_runs"], raw["units"])
    errors, warnings = errors_and_warnings(agg, perf, scal)
    csum = correctness_summary(agg["correctness"], agg["known"])
    mat = matrix_out(agg["per_call"])
    ws_max = max((m.get("workspace_bytes") or 0 for u in raw["units"].values()
                  for m in [u["derived"].get("workspace_end") or {}]), default=0)
    times = [r["wall_time_seconds"] for r in agg["per_call"] if r["wall_time_seconds"]
             is not None]
    run_secs = sum(v.get("seconds", 0) for v in raw["state"].get("runs", {}).values())
    size_medians = {}
    for n in (1_000, 100_000, 1_000_000) + C.STRESS_SIZES:
        v = [r["wall_time_seconds"] for r in agg["per_call"] if r["rows"] == n and
             r["phase"] in ("C", "D", "E", "I") and r["wall_time_seconds"] is not None]
        if v:
            size_medians[str(n)] = st.median(v)
    beh = Counter(b["classification"] for b in agg["behaviour"])
    summary = {
        "domains_run": sorted({r["domain"] for r in agg["per_call"] if r["phase"] in ("C", "D",
                                                                                      "E")
                               and r["domain"]}),
        "principal_datasets": len([u for u in raw["units"] if re.match(r"[CDE]_", u)]),
        "stress_datasets": len([u for u in raw["units"] if u.startswith("I_")]),
        "stress_tiers": {k: v for k, v in raw["state"].get("done", {}).items()
                         if k.startswith("I_")},
        "calls_executed": len(agg["per_call"]),
        "correctness": csum["total"], "known_answers": csum["known_answers"],
        "warnings": len(warnings), "errors": len(errors),
        "crashes": len(agg["crashes"]),
        "exceptions": sum(1 for r in agg["per_call"] if r["status"] == "EXCEPTION"),
        "timeouts": sum(1 for r in agg["per_call"] if r["status"] == "TIMEOUT"),
        "call_time": {"fastest": min(times) if times else None,
                      "median": st.median(times) if times else None,
                      "p95": _pct(times, 0.95), "slowest": max(times) if times else None},
        "median_call_seconds_by_rows": size_medians,
        "median_time_growth": scal["median_time_growth"],
        "peak_worker_memory_mb": round(max(worker_peaks(agg["per_call"]).values(), default=0),
                                       1),
        "max_workspace_mb": round(ws_max / 2 ** 20, 2),
        "total_runtime_seconds": run_secs,
        "behaviour_classes": dict(beh),
        "severity": dict(Counter(e["severity"] for e in errors)),
        "harness_errors": raw["state"].get("harness_errors", []),
    }
    OUT = C.OUT
    _atomic(OUT / "benchmark.json", {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "environment": raw["env"], "summary": summary,
                                     "calls": agg["per_call"]})
    # Every check of every call is 1.5 GB at 6M checks (run 2): that file stays with the
    # evidence, outside the repository. The committed one keeps each call's totals and every
    # check that did not pass.
    _atomic(C.DATA / "correctness_full.json", {"summary": csum, "records": agg["correctness"]})
    slim = [{**r, "checks": [k for k in r.get("checks", [])
                             if isinstance(k, dict) and k.get("status") != "PASS"],
             "checks_listed": "non-passing only; every check: correctness_full.json in the "
                              "benchmark's data directory"}
            for r in agg["correctness"]]
    _atomic(OUT / "correctness.json", {"summary": csum, "records": slim,
                                       "known_answers": agg["known"],
                                       "temporal_order_independence": agg["order"]})
    _atomic(OUT / "performance.json", perf)
    _atomic(OUT / "scaling.json", scal)
    _atomic(OUT / "memory.json", mem)
    _atomic(OUT / "errors.json", errors)
    _atomic(OUT / "warnings.json", warnings)
    _atomic(OUT / "compatibility_matrix.json", mat)
    _atomic(OUT / "dataset_manifest.json", agg["manifests"])
    _atomic(OUT / "behaviour.json", {"classes": dict(beh), "records": agg["behaviour"]})
    _atomic(OUT / "charts.json", agg["charts"])
    _atomic(OUT / "cleaning.json", agg["plans"])
    _atomic(OUT / "concurrency_isolation.json", {"batches": agg["batches"],
                                                 "isolation": agg["isolation"]})
    _atomic(OUT / "reproducibility.json", raw["repro"])
    for name in DOMAINS:
        _atomic(OUT / "domain_results" / f"{name}.json", {
            "correctness": [r for r in csum["by_domain_size"] if r["domain"] == name],
            "performance": [g for g in perf["groups"] if g["domain"] == name],
            "errors": [e for e in errors if e.get("domain") == name],
            "warnings": [w for w in warnings if w.get("domain") == name],
            "charts": [c for c in agg["charts"] if c.get("domain") == name],
            "matrix": mat["domains"].get(name)})
    from . import summary as S
    S.write(OUT)
    S.console(OUT)
    print(f"(aggregated in {time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
