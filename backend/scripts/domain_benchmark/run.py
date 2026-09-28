"""The cross-domain benchmark: phases A to K (section 51), checkpointed unit by unit.

    uv run python scripts/domain_benchmark/run.py            # everything not yet done
    uv run python scripts/domain_benchmark/run.py --only C   # one phase
    uv run python scripts/domain_benchmark/run.py --report   # phase K from what is on disk

Each unit writes its worker's JSONL and a derived JSON under docs/benchmark/domain_benchmark/
checkpoint/<unit>/ and is marked done in checkpoint/state.json (atomically), so a process that
dies at 10M rows leaves every earlier result intact. A unit is never re-run over its own
evidence: a rerun gets a new run directory, and the earlier one is kept.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

from domain_benchmark import config as C  # noqa: E402
from domain_benchmark import fixtures as F  # noqa: E402
from domain_benchmark import oracle, verify  # noqa: E402
from domain_benchmark.domains import DOMAINS, contract_kwargs, write_dataset  # noqa: E402
from domain_benchmark.matrix import cases  # noqa: E402

WORKER = HERE.parent / "worker.py"
STATE = C.CHECKPOINT / "state.json"
LOG = C.OUT / "benchmark.log"
WORKSPACE_ROOT = C.ROOT / "workspace"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"done": {}, "runs": {}}


def mark(unit: str, info: dict) -> None:
    s = state()
    s["done"][unit] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **info}
    atomic_json(STATE, s)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def mem_available_mb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return 0.0


def reset_ws(ws: str) -> None:
    d = WORKSPACE_ROOT / ws
    if d.exists():
        shutil.rmtree(d)


# --------------------------------------------------------------------------- the worker


def run_worker(job: dict, timeout: float) -> dict:
    """Run one worker job to completion or death. Returns how it ended, with the child's peak
    RSS from the kernel (wait4's ru_maxrss), which no per-call reset can hide."""
    jpath = Path(job["out"]).with_suffix(".job.json")
    atomic_json(jpath, job)
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, str(WORKER), str(jpath)], cwd=C.ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    killed = False
    ru = None
    status = None
    while True:
        pid, status, ru = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            break
        if time.time() - t0 > timeout:
            proc.kill()
            killed = True
            pid, status, ru = os.wait4(proc.pid, 0)
            break
        time.sleep(0.5)
    out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
    code = os.waitstatus_to_exitcode(status) if status is not None else None
    return {"returncode": code, "killed_by_timeout": killed, "elapsed_s": time.time() - t0,
            "child_peak_rss_mb": round(ru.ru_maxrss / 1024, 1) if ru else None,
            "stdout_tail": out[-3000:]}


def crash_record(job: dict, end: dict, unit: str) -> dict | None:
    """A worker that died: what it was doing, from the marker it left."""
    if end["returncode"] == 0 and not end["killed_by_timeout"]:
        return None
    marker = Path(job["out"] + ".current")
    inflight = json.loads(marker.read_text()) if marker.exists() else {}
    rec = {"unit": unit, "domain": job.get("domain"), "rows": job.get("rows"),
           "dataset_file": job.get("csv"), "tool": inflight.get("tool"),
           "analysis": inflight.get("analysis"), "parameters": inflight.get("parameters"),
           "stage": inflight.get("stage"),
           "exception_type": ("WorkerTimeout" if end["killed_by_timeout"] else
                              f"ProcessDied(exit {end['returncode']})"),
           "exception_message": end["stdout_tail"][-1500:],
           "elapsed_before_failure_s": (time.time() - inflight["started"]) if inflight.get(
               "started") else end["elapsed_s"],
           "memory_before_mb": (inflight.get("rss_before_kb") or 0) / 1024 or None,
           "child_peak_rss_mb": end["child_peak_rss_mb"],
           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "CRASH",
           "process_usable_afterwards": None}
    cdir = C.OUT / "crashes"
    cdir.mkdir(parents=True, exist_ok=True)
    atomic_json(cdir / f"{unit}_{int(time.time())}.json", rec)
    if marker.exists():
        marker.rename(marker.with_suffix(".crashed"))
    return rec


def run_job_with_recovery(job: dict, unit: str, timeout: float) -> tuple[list[dict], list[dict]]:
    """Run, and after a death restart past the stage (or step) it died in. At most 3 times."""
    ends, crashes = [], []
    for attempt in range(4):
        end = run_worker(job, timeout)
        ends.append(end)
        crash = crash_record(job, end, unit)
        if not crash:
            break
        crashes.append(crash)
        log(f"  CRASH in {unit}: {crash['exception_type']} at {crash['stage']} "
            f"{crash['tool']} {crash['analysis']}")
        recs = verify.load(Path(job["out"]))
        if job["mode"] == "principal":
            started = {r["stage"] for r in recs if r.get("event") == "stage_start"}
            job = {**job, "skip_stages": sorted(started)}
        elif job["mode"] == "steps":
            dead = crash.get("stage")
            done_ids = {r.get("variant") for r in recs if r.get("kind") != "note"}
            keep = [s for s in job["steps"] if s.get("id") not in done_ids
                    or s.get("stage") == "setup"]
            if crash.get("tool"):
                keep = [s for s in keep if not (s.get("tool") == crash["tool"] and
                                                s.get("id") not in done_ids and
                                                keep.index(s) == 0)]
            job = {**job, "steps": keep}
        else:
            break
        crashes[-1]["process_usable_afterwards"] = "restarted a fresh worker"
    return ends, crashes


def preserve(unit: str, records: list[dict]) -> int:
    """Copy every result file a record names out of the workspace before it is reset, so a
    verification can be repeated from evidence (section 49)."""
    dest = C.EVIDENCE / unit
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in records:
        for key in ("result_path", "path"):
            p = r.get(key)
            if p and str(p).endswith(".csv") and Path(p).exists():
                shutil.copy2(p, dest / Path(p).name)
                n += 1
    return n


def remap(records: list[dict], unit: str) -> list[dict]:
    """Point result paths at the preserved copies (for a re-verification)."""
    dest = C.EVIDENCE / unit
    out = []
    for r in records:
        r = dict(r)
        for key in ("result_path", "path"):
            if r.get(key) and str(r[key]).endswith(".csv"):
                r[key] = str(dest / Path(r[key]).name)
        out.append(r)
    return out


def job_for(unit: str, mode: str, **kw) -> dict:
    d = C.CHECKPOINT / unit
    d.mkdir(parents=True, exist_ok=True)
    run = len(list(d.glob("calls*.jsonl")))
    out = d / (f"calls_{run}.jsonl" if run else "calls.jsonl")
    return {"id": unit, "mode": mode, "out": str(out), "crashes_dir": str(C.OUT / "crashes"),
            "export_dir": str(C.DATA / "exports" / unit), **kw}


# --------------------------------------------------------------------------- phase A


def phase_a() -> None:
    def sh(cmd):
        try:
            return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  cwd=C.ROOT, timeout=60).stdout.strip()
        except Exception as exc:  # noqa: BLE001
            return f"unavailable: {exc}"

    import duckdb
    import matplotlib
    import openpyxl
    import scipy
    import statsmodels
    env = {
        "os": platform.platform(), "architecture": platform.machine(),
        "cpu_model": sh("lscpu | grep 'Model name' | sed 's/.*: *//'"),
        "cpu_cores": os.cpu_count(),
        "ram_total_mb": int(Path("/proc/meminfo").read_text().split()[1]) // 1024,
        "ram_available_mb_at_start": round(mem_available_mb()),
        "swap": sh("grep SwapTotal /proc/meminfo"),
        "disk_free_gb_at_start": round(shutil.disk_usage(C.ROOT).free / 2 ** 30, 1),
        "python": sys.version, "libraries": {
            "duckdb": duckdb.__version__, "scipy": scipy.__version__,
            "statsmodels": statsmodels.__version__, "matplotlib": matplotlib.__version__,
            "openpyxl": openpyxl.__version__},
        "git_commit": sh("git rev-parse HEAD"), "git_branch": sh("git branch --show-current"),
        "git_status_at_start": sh("git status --short"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": C.as_dict(),
        "env": {k: v for k, v in os.environ.items()
                if k.startswith(("ANALYTICS_", "DUCKDB_", "OMP_", "DOMAIN_BENCH"))},
        "application_cache": "none: grep finds no lru_cache/cache in src/ outside the web "
                             "agent's model list, so every call is a CACHE_MISS",
    }
    baseline = Path(os.environ.get("DOMAIN_BENCH_BASELINE", "")) if os.environ.get(
        "DOMAIN_BENCH_BASELINE") else None
    if baseline and baseline.exists():
        env["baseline_tests"] = baseline.read_text()
    atomic_json(C.OUT / "environment.json", env)
    mark("A", {"git_commit": env["git_commit"]})


# --------------------------------------------------------------------------- phases C, D, E, I


class Stream:
    """Ground truth accumulated while a stress dataset is written: the full oracle needs every
    row in memory, which a 10M-row table does not allow on this machine."""

    def __init__(self, dom):
        import datetime as dt
        self.dom = dom
        self.lo, self.hi = (dt.date.fromisoformat(x) for x in dom.window)
        r = dom.roles
        self.ms, self.d0 = r["m_sum"], r["dims_low"][0]
        self.n_scope = 0
        self.m_n = self.m_nulls = 0
        self.m_sum = 0.0
        self.by_dim: dict = {}
        self.by_month: dict = {}
        import math
        self._fsum = math.fsum
        self.parts_sum: list[float] = []

    def __call__(self, row):
        d = row.get(self.dom.date)
        if d is None:
            return
        if not (self.lo <= d <= self.hi):
            return
        if any(e["py"](row) for e in self.dom.exclusions):
            return
        self.n_scope += 1
        v = row.get(self.ms)
        k = oracle.label(row.get(self.d0))
        month = f"{d.year:04d}-{d.month:02d}"
        g = self.by_dim.setdefault(k, [0.0, 0])
        mm = self.by_month.setdefault(month, [0.0, 0])
        g[1] += 1
        mm[1] += 1
        if v is None:
            self.m_nulls += 1
        else:
            self.m_n += 1
            self.m_sum += v
            g[0] += v
            mm[0] += v


def stream_checks(stream: Stream, records: list[dict]) -> list[dict]:
    out = []
    for n in records:
        if n.get("event") != "verify" or n.get("variant") != "":
            continue
        a = n["analysis"]
        rows = verify.result_rows(n.get("result_path"))
        c = oracle.Checker()
        if a == "summary_stats" and rows:
            r = next((x for x in rows if x["measure"] == stream.ms), None)
            if r:
                c.value(f"{stream.ms} n", r["n"], stream.m_n, exact=True)
                c.value(f"{stream.ms} nulls", r["nulls"], stream.m_nulls, exact=True)
                c.value(f"{stream.ms} total", r["total"], stream.m_sum, rel=1e-9)
        elif a == "top_n" and rows and n["params"].get("dimension") == stream.d0:
            vcol = next(h for h in rows[0] if h.startswith(stream.ms + " ("))
            for r in rows:
                k = oracle.norm_label(r["value"])
                if k in stream.by_dim:
                    c.value(f"{k} {vcol}", r[vcol], stream.by_dim[k][0], rel=1e-9)
                    c.value(f"{k} rows", r["rows"], stream.by_dim[k][1], exact=True)
        elif a == "trend" and rows and n["params"].get("measure") == stream.ms:
            vcol = next(h for h in rows[0] if h.startswith(stream.ms + " ("))
            for r in rows:
                s = stream.by_month.get(r["period"])
                if s:
                    c.value(f"{r['period']} {vcol}", r[vcol], s[0], rel=1e-9)
                    c.value(f"{r['period']} rows", r["rows"], s[1], exact=True)
        else:
            out.append({"analysis": a, "variant": n.get("variant"), "status":
                        "SKIPPED_RESOURCE_LIMIT", "checks_total": 0, "checks_passed": 0,
                        "checks_failed": 0, "skipped": [
                            "the full oracle holds every row as Python objects (~1.5 KB a row "
                            "measured at 1M); a streaming reference covers summary_stats, "
                            "top_n and trend only"], "checks": [], "failed_checks": [],
                        "parameters": n.get("params"), "class": n.get("class_")})
            continue
        checks = [x.as_dict() for x in c.checks]
        failed = [x for x in checks if x["status"] == "FAIL"]
        out.append({"analysis": a, "variant": n.get("variant"),
                    "status": "FAIL" if failed else ("PASS" if checks else
                                                     "SKIPPED_RESOURCE_LIMIT"),
                    "checks_total": len(checks), "checks_passed": len(checks) - len(failed),
                    "checks_failed": len(failed), "skipped": [], "checks": checks,
                    "failed_checks": failed, "parameters": n.get("params"),
                    "class": n.get("class_"), "oracle": "streaming"})
    return out


def principal_unit(unit: str, phase: str, domain: str, n: int) -> None:
    dom = DOMAINS[domain]
    csv_path = C.DATA / f"{domain}_{n}.csv"
    stress = n > 1_000_000
    stream = Stream(dom) if stress else None
    t0 = time.perf_counter()
    manifest = write_dataset(dom, n, csv_path, C.SEED, on_row=stream)
    manifest["generation_seconds"] = round(time.perf_counter() - t0, 2)
    manifest["sha256"] = sha256(csv_path)
    ws = f"dbench_{domain}_{n}"
    reset_ws(ws)
    job = job_for(unit, "principal", phase=phase, domain=domain, dataset=domain, rows=n,
                  csv=str(csv_path), ws=ws)
    log(f"{unit}: {n:,} rows generated in {manifest['generation_seconds']}s "
        f"({manifest['bytes']:,} bytes); running the workflow")
    ends, crashes = run_job_with_recovery(job, unit, C.WORKER_TIMEOUT_S.get(n, 7200))
    records = []
    for p in sorted((C.CHECKPOINT / unit).glob("calls*.jsonl")):
        records += verify.load(p)
    preserve(unit, records)
    ctx = {"domain": domain, "rows": n, "unit": unit, "phase": phase}
    t1 = time.perf_counter()
    if stress:
        corr = [{**ctx, **x} for x in stream_checks(stream, records)]
        oracle_note = {"oracle": "streaming", "load_seconds": 0}
    else:
        data = oracle.Data(csv_path, dom.types, dom.date, dom.window, dom.exclusions)
        oracle_note = {"oracle": "full", "load_seconds": round(time.perf_counter() - t1, 2),
                       "raw_rows": data.raw_rows, "duplicates_dropped": data.duplicates_dropped,
                       "rows_in_scope": len(data.scope), "excluded": data.excluded,
                       "undated": data.undated, "outside_window": data.outside}
        corr = verify.correctness(records, data, dom, context=ctx)
        del data
    oracle_note["verify_seconds"] = round(time.perf_counter() - t1, 2)
    plan_notes = [r for r in records if r.get("event") == "plan"]
    plan = verify.plan_quality(plan_notes[0]["plan_text"], manifest) if plan_notes else []
    ws_end = next((r for r in reversed(records) if r.get("event") == "stage_end"), {})
    derived = {"unit": unit, "phase": phase, "domain": domain, "rows": n, "manifest": manifest,
               "worker_runs": ends, "crashes": crashes, "oracle": oracle_note,
               "correctness": corr, "charts": verify.charts(records, ctx),
               "paging": verify.paging(records, ctx), "plan_quality": plan,
               "follows": [r for r in records if r.get("event") == "follow"],
               "workspace_end": {k: ws_end.get(k) for k in ("workspace_bytes", "files",
                                                            "results", "charts", "reports")}}
    atomic_json(C.CHECKPOINT / unit / "derived.json", derived)
    npass = sum(x["checks_passed"] for x in corr)
    ntot = sum(x["checks_total"] for x in corr)
    log(f"{unit}: done in {ends[-1]['elapsed_s']:.0f}s, worker peak "
        f"{ends[-1]['child_peak_rss_mb']} MiB, checks {npass}/{ntot}, crashes {len(crashes)}")
    reset_ws(ws)
    shutil.rmtree(C.DATA / "exports" / unit, ignore_errors=True)
    csv_path.unlink(missing_ok=True)
    mark(unit, {"checks": [npass, ntot], "crashes": len(crashes)})


def worker_peak_mb(unit: str) -> float:
    """The worker's own peak: the largest per-call VmHWM (reset before each call) or VmRSS.
    NOT wait4's ru_maxrss, which Linux carries across fork+exec from the parent -- it reported
    the parent's oracle memory (830.8 MiB for every late-D unit, 2,001.4 for E_hr and E_sales),
    found in run 2."""
    best = 0.0
    for p in sorted((C.CHECKPOINT / unit).glob("calls*.jsonl")):
        for r in verify.load(p):
            for k in ("peak_memory_mb", "memory_after_mb", "memory_before_mb"):
                if r.get(k):
                    best = max(best, r[k])
    return best


def stress_gate(domain: str, n: int) -> dict:
    """Section 54: estimate from the LARGEST completed run of this domain (1M, 2M or 5M),
    scaled linearly to n, times the margin. The first version always scaled from 1M, which
    asked 16 GB for 10M rows on a 1.08 GB 1M peak; a larger reference is better evidence."""
    refs = [(1_000_000, f"E_{domain}")] + [(k, f"I_{domain}_{k}") for k in C.STRESS_SIZES
                                           if k < n]
    usable = [(k, u) for k, u in refs if (C.CHECKPOINT / u / "derived.json").exists()]
    if not usable:
        return {"ok": False, "why": "no completed reference run to estimate from"}
    base, unit = max(usable)
    ref = C.CHECKPOINT / unit / "derived.json"
    d = json.loads(ref.read_text())
    f = n / base
    peak = worker_peak_mb(unit)
    csv_b = d["manifest"]["bytes"]
    ws_b = d["workspace_end"].get("workspace_bytes") or 0
    need_mem = peak * f * C.SAFETY_MARGIN
    need_disk = (csv_b + ws_b) * f * C.SAFETY_MARGIN / 2 ** 20
    have_mem = mem_available_mb()
    have_disk = shutil.disk_usage(C.ROOT).free / 2 ** 20
    ok = need_mem < have_mem and need_disk < have_disk
    return {"ok": ok, "estimated_memory_mb": round(need_mem), "available_memory_mb":
            round(have_mem), "estimated_disk_mb": round(need_disk),
            "available_disk_mb": round(have_disk), "basis": f"{unit} x rows/{base:,} x margin "
                                                            f"{C.SAFETY_MARGIN}",
            "reference_peak_mb": round(peak)}


# --------------------------------------------------------------------------- steps units


def steps_unit(unit: str, phase: str, fx: dict, extra_ctx=None, timeout=3600,
               tracemalloc=False) -> list[dict]:
    reset_ws(fx["ws"])
    job = job_for(unit, "steps", phase=phase, domain=None, dataset=fx["name"], rows=0,
                  ws=fx["ws"], steps=fx["steps"], tracemalloc=tracemalloc, keep_full=True)
    ends, crashes = run_job_with_recovery(job, unit, timeout)
    records = []
    for p in sorted((C.CHECKPOINT / unit).glob("calls*.jsonl")):
        records += verify.load(p)
    preserve(unit, records)
    derived = {"unit": unit, "phase": phase, "fixture": {k: v for k, v in fx.items()
                                                         if k != "steps"},
               "worker_runs": ends, "crashes": crashes,
               "follows": [r for r in records if r.get("event") == "follow"],
               **(extra_ctx or {})}
    atomic_json(C.CHECKPOINT / unit / "derived.json", derived)
    return records


def phase_b() -> None:
    root = C.DATA / "fixtures"
    ka = F.known_answer(root, "dbench_known")
    recs = steps_unit("B_known", "B", ka)
    kc = known_checks(recs)
    se = F.stat_edges(root, "dbench_stat")
    steps_unit("B_stat", "B", se)
    te = F.temporal_edges(root, "dbench_temporal")
    trecs = steps_unit("B_temporal", "B", te)
    order = temporal_order_check(trecs)
    d = json.loads((C.CHECKPOINT / "B_known" / "derived.json").read_text())
    d["known_checks"] = kc
    atomic_json(C.CHECKPOINT / "B_known" / "derived.json", d)
    d = json.loads((C.CHECKPOINT / "B_temporal" / "derived.json").read_text())
    d["order_independence"] = order
    atomic_json(C.CHECKPOINT / "B_temporal" / "derived.json", d)
    for ws in ("dbench_known", "dbench_stat", "dbench_temporal"):
        reset_ws(ws)
    mark("B", {"known_checks": [sum(1 for k in kc if k["status"] == "PASS"), len(kc)]})


def known_checks(records: list[dict]) -> list[dict]:
    """Known answers compared with the numbers written in fixtures.py, not with the oracle."""
    out = []
    notes = {r["variant"]: r for r in records if r.get("event") == "verify"}
    calls = {r["variant"]: r for r in records if r.get("kind") != "note"}

    def add(case, what, expected, actual, ok):
        out.append({"case": case, "what": what, "expected": expected, "actual": actual,
                    "status": "PASS" if ok else "FAIL"})

    def val(x):
        try:
            return oracle.num(x)[0]
        except Exception:  # noqa: BLE001
            return None

    for vid, n in notes.items():
        known = n.get("known") or {}
        rows = verify.result_rows(n.get("result_path")) or []
        text = n.get("text", "")
        if vid in ("ka_corr_pos", "ka_corr_neg", "ka_corr_zero"):
            for k, want in known.items():
                got = val(rows[0][k]) if rows else None
                add(vid, k, want, rows[0][k] if rows else None,
                    got is not None and abs(got - want) <= 0.0005)
        elif vid == "ka_trend":
            for k, r in enumerate(rows):
                add(vid, r["period"], 100 + 10 * k, r["tr (sum)"],
                    val(r["tr (sum)"]) == 100 + 10 * k)
        elif vid == "ka_cp":
            add(vid, "top split", "2024-06", rows[0]["split after"] if rows else None,
                bool(rows) and rows[0]["split after"] == "2024-06")
            add(vid, "difference", 100, rows[0]["difference"] if rows else None,
                bool(rows) and val(rows[0]["difference"]) == 100)
        elif vid == "ka_season":
            dec = next((r for r in rows if r["position"] == "Dec"), None)
            col = next((h for h in (rows[0] if rows else {}) if h.startswith("mean ")), None)
            add(vid, "December mean", 150, dec[col] if dec else None,
                bool(dec) and val(dec[col]) == 150)
            add(vid, "other months' mean", 100, [r[col] for r in rows if r["position"] != "Dec"][:3],
                all(val(r[col]) == 100 for r in rows if r["position"] != "Dec"))
        elif vid == "ka_pareto":
            second = rows[1]["running share"] if len(rows) > 1 else None
            add(vid, "top two groups' running share", 0.8, second,
                second is not None and abs(val(second) - 0.8) < 0.0005)
        elif vid == "ka_top":
            order = [oracle.norm_label(r["value"]) for r in rows]
            add(vid, "ranking", known["order"], order, order == known["order"])
            add(vid, "g0 total", 240, rows[0]["rk (sum)"] if rows else None,
                bool(rows) and val(rows[0]["rk (sum)"]) == 240)
        elif vid == "ka_growth":
            got = {r["period"]: val(r["tr (sum)"]) for r in rows}
            add(vid, "monthly sums", known, got, got == known)
        elif vid == "ka_ident_t":
            m = __import__("re").search(r"Statistic ([\d.,-]+).*p ([\d.eE+-]+)", text)
            add(vid, "t and p for identical groups", "t 0, p 1", m.groups() if m else text[:200],
                bool(m) and val(m.group(1)) == 0 and abs(float(m.group(2).rstrip(".")) - 1) < 1e-6)
        elif vid == "ka_ident_g":
            m = __import__("re").search(r"Hedges' g ([\d.]+)", text)
            add(vid, "Hedges' g for identical groups", 0.0, m.group(1) if m else text[:200],
                bool(m) and val(m.group(1)) == 0)
        elif vid == "ka_diff_t":
            m = __import__("re").search(r"p (<?\s?[\d.eE+-]*\d)", text)
            p = m.group(1) if m else None
            add(vid, "p for groups 100 apart", "< 1e-10", p,
                bool(p) and float(p.lstrip("< ")) < 1e-10)
        elif vid == "ka_imb":
            got = {oracle.norm_label(r["value"]): val(r["rows"]) for r in rows}
            add(vid, "class counts", {"true": 3, "false": 237}, got,
                got.get("true") == 3 and got.get("false") == 237)
        elif vid == "ka_miss":
            r = next((x for x in rows if x["measure"] == "miss"), {})
            add(vid, "nulls", 24, r.get("nulls"), val(r.get("nulls")) == 24)
            add(vid, "n", 216, r.get("n"), val(r.get("n")) == 216)
            r9 = next((x for x in rows if x["measure"] == "nine"), {})
            add(vid, "mean of 1..9 cycles", known["nine_mean"], r9.get("mean"),
                val(r9.get("mean")) == known["nine_mean"])
            add(vid, "median", 5.0, r9.get("median"), val(r9.get("median")) == 5.0)
        elif vid == "ka_retention":
            r = next((x for x in rows if x["cohort"].startswith("2023-01")), {})
            for k, want in known["2023-01"].items():
                add(vid, f"2023-01 {k}", want, r.get(k), val(r.get(k)) == want)
        elif vid == "ka_outliers":
            t = next((x for x in rows if x["method"] == "Tukey's fence"), {})
            add(vid, "Tukey flagged on 0..239", 0, t.get("flagged"), val(t.get("flagged")) == 0)
    plan = calls.get("ka_plan")
    if plan:
        m = __import__("re").search(r"([\d,]+) exactly duplicated", plan.get("response_full", ""))
        add("ka_plan", "duplicate rows found", 4, m.group(1) if m else None,
            bool(m) and m.group(1) == "4")
    missing = [v for v in ("ka_corr_pos", "ka_corr_neg", "ka_corr_zero", "ka_trend", "ka_cp",
                           "ka_season", "ka_pareto", "ka_top", "ka_growth", "ka_ident_t",
                           "ka_ident_g", "ka_diff_t", "ka_imb", "ka_miss", "ka_retention",
                           "ka_outliers") if v not in notes]
    for v in missing:
        c = calls.get(v, {})
        add(v, "the known-answer call produced a result", "a result",
            f"{c.get('status')}: {(c.get('response_head') or '')[:200]}", False)
    return out


def temporal_order_check(records: list[dict]) -> list[dict]:
    """The same analysis on sorted and shuffled copies must give identical tables."""
    out = []
    notes = {r["variant"]: r for r in records if r.get("event") == "verify"}
    for sid in ("trend_day", "trend_week", "trend_month", "season_month", "season_week",
                "season_flat", "cp_ramp", "coverage_day"):
        a, b = notes.get(f"te_sorted_{sid}"), notes.get(f"te_shuffled_{sid}")
        if not (a and b):
            out.append({"case": sid, "status": "SKIPPED_UNSUPPORTED",
                        "why": "one or both calls did not produce a result"})
            continue
        ta = Path(a["result_path"]).read_text().splitlines() if a.get("result_path") else []
        tb = Path(b["result_path"]).read_text().splitlines() if b.get("result_path") else []
        out.append({"case": sid, "status": "PASS" if ta == tb else "FAIL",
                    "rows": len(ta)})
    return out


# --------------------------------------------------------------------------- phases F G H J


def phase_f() -> None:
    root = C.DATA / "dirty"
    clean = root / "financial_5000.csv"
    manifest = write_dataset(DOMAINS["financial"], 5000, clean, C.SEED)
    dv = F.dirty_variants(root, clean, "dbench_dirty")
    recs = steps_unit("F_dirty", "F", dv)
    quality = {}
    for tag, v in dv["variants"].items():
        plan = next((r for r in recs if r.get("variant") == f"dv_{tag}_plan"
                     and r.get("kind") != "note"), None)
        text = (plan or {}).get("response_full", "")
        # the duplicates a plan should find are the variant file's own exact repeats: noise
        # applied per row turns most of the clean file's duplicates into distinct rows (run 2
        # judged every variant against 0 and called a correct C001 dangerous)
        with open(v["path"], newline="") as fh:
            body = list(csv.reader(fh))[1:]
        dups = len(body) - len(set(map(tuple, body)))
        quality[tag] = {"expect": v["expect"], "injected": v["injected"],
                        "duplicate_rows_in_file": dups,
                        "actions": verify.plan_quality(text, {"anomalies": {"duplicate_rows": dups}},
                                                       v["injected"]),
                        "injected_without_action": [c for c in v["injected"]
                                                    if c not in text]}
    fm = F.format_cases(root, clean, "dbench_formats")
    steps_unit("F_formats", "F", fm)
    d = json.loads((C.CHECKPOINT / "F_dirty" / "derived.json").read_text())
    d["plan_quality"] = quality
    d["clean_manifest"] = manifest
    atomic_json(C.CHECKPOINT / "F_dirty" / "derived.json", d)
    for ws in ("dbench_dirty", "dbench_formats"):
        reset_ws(ws)
    mark("F", {})


def phase_g() -> None:
    root = C.DATA / "adv"
    clean = root / "financial_2000.csv"
    write_dataset(DOMAINS["financial"], 2000, clean, C.SEED)
    wc = F.wrong_calls(root, clean, "dbench_adv", "dbench_fresh")
    reset_ws("dbench_fresh")
    steps_unit("G_wrong", "G", wc)
    for ws in ("dbench_adv", "dbench_fresh"):
        reset_ws(ws)
    mark("G", {})


def phase_h() -> None:
    root = C.DATA / "repeat"
    dom = DOMAINS["sales"]
    path = root / "sales_100000.csv"
    write_dataset(dom, 100_000, path, C.SEED)
    ws = "dbench_repeat"
    r = dom.roles
    steps = F.ingest_steps("rp", str(path), "sales", ws)
    steps += F.dedupe_steps("rp", "sales", ws)
    steps += F.contract_steps("rp", "sales", ws, **contract_kwargs(dom, dom.key))
    mix = [c for c in cases(dom, list(_registry())) if c["class"] not in ("NOT_APPLICABLE",
                                                                         "UNCLASSIFIED")][:10]
    steps.append({"id": "same50", "tool": "compute_analysis", "stage": "repeat", "repeat": 50,
                  "analysis": "top_n", "kwargs": {"dataset_name": "sales", "workspace_id": ws,
                                                  "analysis_type": "top_n",
                                                  "dimension": r["dims_low"][0],
                                                  "measure": r["m_sum"]}})
    for k in range(10):
        for c in mix:
            steps.append({"id": f"mix{k}_{c['analysis']}_{c['variant']}", "tool":
                          "compute_analysis", "stage": "repeat_mixed", "analysis": c["analysis"],
                          "kwargs": {"dataset_name": "sales", "workspace_id": ws,
                                     "analysis_type": c["analysis"], **c["params"]}})
    steps.append({"id": "profile10", "tool": "profile_dataset", "stage": "repeat", "repeat": 10,
                  "kwargs": {"dataset_name": "sales", "workspace_id": ws}})
    steps.append({"id": "chart20", "tool": "render_chart", "stage": "repeat", "repeat": 20,
                  "analysis": "trend", "kwargs": {"dataset_name": "sales", "workspace_id": ws,
                                                  "analysis_type": "trend", "chart": "line",
                                                  "measure": r["m_sum"], "grain": "month"}})
    steps.append({"id": "report10", "tool": "build_report", "stage": "repeat", "repeat": 10,
                  "kwargs": {"dataset_name": "sales", "workspace_id": ws, "question": "q"}})
    steps_unit("H_repeat", "H", {"name": "repeat", "steps": steps, "ws": ws})
    tm = F.ingest_steps("tm", str(path), "sales", "dbench_traced")
    tm += F.dedupe_steps("tm", "sales", "dbench_traced")
    tm += F.contract_steps("tm", "sales", "dbench_traced", **contract_kwargs(dom, dom.key))
    for k in range(3):
        for c in mix:
            tm.append({"id": f"tm{k}_{c['analysis']}_{c['variant']}", "tool": "compute_analysis",
                       "stage": "traced", "analysis": c["analysis"],
                       "kwargs": {"dataset_name": "sales", "workspace_id": "dbench_traced",
                                  "analysis_type": c["analysis"], **c["params"]}})
    steps_unit("H_traced", "H", {"name": "traced", "steps": tm, "ws": "dbench_traced"},
               tracemalloc=True)
    reset_ws(ws)
    reset_ws("dbench_traced")
    # concurrency and isolation: ten 10k datasets, each in its own workspace and all in one
    setup, datasets = [], []
    csvs = {}
    for name, d in DOMAINS.items():
        p = root / f"{name}_10000.csv"
        write_dataset(d, 10_000, p, C.SEED)
        csvs[name] = p
        for mode, wsid in (("separate", f"dbench_c_{name}"), ("shared", "dbench_shared")):
            reset_ws(wsid)
            setup += F.ingest_steps(f"c_{mode}_{name}", str(p), name, wsid)
            setup += F.dedupe_steps(f"c_{mode}_{name}", name, wsid)
            setup += F.contract_steps(f"c_{mode}_{name}", name, wsid,
                                      **contract_kwargs(d, "record_id"))
            datasets.append({"ws": wsid, "name": name, "domain": name, "mode": mode,
                             "measure": d.roles["m_sum"], "dimension": d.roles["dims_low"][0]})
    job = job_for("H_concurrency", "concurrent", phase="H", domain=None, dataset="many", rows=0,
                  ws="dbench_shared", setup=setup, datasets=datasets, steps=[])
    ends, crashes = run_job_with_recovery(job, "H_concurrency", 3600)
    recs = []
    for p in sorted((C.CHECKPOINT / "H_concurrency").glob("calls*.jsonl")):
        recs += verify.load(p)
    oracles = {n: oracle.Data(p, DOMAINS[n].types, DOMAINS[n].date, DOMAINS[n].window,
                              DOMAINS[n].exclusions) for n, p in csvs.items()}
    corr = []
    for note in (r for r in recs if r.get("event") == "verify"):
        dname = note["target_domain"]
        corr += verify.correctness([note], oracles[dname], DOMAINS[dname],
                                   context={"domain": dname, "rows": 10_000,
                                            "unit": "H_concurrency", "phase": "H",
                                            "batch": note["variant"]})
    batches = [r for r in recs if r.get("event") == "batch"]
    isolation = isolation_unit(csvs, oracles)
    atomic_json(C.CHECKPOINT / "H_concurrency" / "derived.json",
                {"unit": "H_concurrency", "worker_runs": ends, "crashes": crashes,
                 "correctness": corr, "batches": batches, "isolation": isolation})
    for name in DOMAINS:
        reset_ws(f"dbench_c_{name}")
    reset_ws("dbench_shared")
    reset_ws("dbench_iso")
    mark("H", {})


def _registry():
    sys.path.insert(0, str(C.ROOT.parent))
    from backend.engine.analysis import registry
    return registry.REGISTRY


def isolation_unit(csvs: dict, oracles: dict) -> dict:
    """Section 28 in one workspace holding all ten: every dataset's figures come from its own
    rows, its report names only itself, cleaning one leaves the others' ledgers empty, and a
    chart reply names its own measure."""
    ws = "dbench_iso"
    reset_ws(ws)
    steps = []
    for name, p in csvs.items():
        d = DOMAINS[name]
        steps += F.ingest_steps(f"iso_{name}", str(p), name, ws)
        steps += F.dedupe_steps(f"iso_{name}", name, ws)
        steps += F.contract_steps(f"iso_{name}", name, ws, **contract_kwargs(d, "record_id"))
    for name in csvs:
        d = DOMAINS[name]
        r = d.roles
        steps += [
            F.call(f"iso_{name}_top", "top_n", {"dimension": r["dims_low"][0],
                                                "measure": r["m_sum"]}, ws, name, stage="iso"),
            F.call(f"iso_{name}_trend", "trend", {"measure": r["m_sum"], "grain": "month"}, ws,
                   name, stage="iso"),
            {"id": f"iso_{name}_ledger", "tool": "get_cleaning_ledger", "stage": "iso",
             "kwargs": {"dataset_name": name, "workspace_id": ws}},
            {"id": f"iso_{name}_chart", "tool": "render_chart", "stage": "iso",
             "analysis": "trend", "kwargs": {"dataset_name": name, "analysis_type": "trend",
                                             "chart": "line", "measure": r["m_sum"],
                                             "grain": "month", "workspace_id": ws}},
            {"id": f"iso_{name}_report", "tool": "build_report", "stage": "iso",
             "kwargs": {"dataset_name": name, "question": f"iso {name}", "workspace_id": ws}},
        ]
    recs = steps_unit("H_isolation", "H", {"name": "isolation", "steps": steps, "ws": ws})
    calls = {r["variant"]: r for r in recs if r.get("kind") != "note"}
    dups = {n: write_dataset(DOMAINS[n], 10_000, p, C.SEED)["anomalies"]["duplicate_rows"]
            for n, p in csvs.items()}
    out = {"correctness": [], "reports": [], "ledgers": [], "charts": []}
    for note in (r for r in recs if r.get("event") == "verify"):
        name = note["variant"].split("_")[1]
        out["correctness"] += verify.correctness([note], oracles[name], DOMAINS[name], context={
            "domain": name, "rows": 10_000, "unit": "H_isolation", "phase": "H"})
    for name in csvs:
        rep = calls.get(f"iso_{name}_report", {})
        text = rep.get("response_full", "")
        m = __import__("re").search(r"Report written: (\S+\.md)", text)
        body = Path(m.group(1)).read_text() if m and Path(m.group(1)).exists() else ""
        others = [o for o in csvs if o != name and f"dataset        {o}" in body]
        out["reports"].append({"dataset": name, "report_file": m.group(1) if m else None,
                               "names_itself": name in body[:3000],
                               "names_another_as_its_dataset": others,
                               "status": "PASS" if body and not others else "FAIL"})
        # every dataset was deduplicated in the one workspace: each ledger must hold exactly its
        # own action, with its own count, and name no other dataset
        led = calls.get(f"iso_{name}_ledger", {}).get("response_full", "")
        lines = [x for x in led.splitlines() if "DROP_DUPLICATE_ROWS" in x]
        foreign = [o for o in csvs if o != name and any(f"  {o}  " in x for x in lines)]
        own = len(lines) == 1 and f"  {name}  " in lines[0] and \
            f": {dups[name]:,} row(s) removed" in lines[0]
        out["ledgers"].append({"dataset": name, "entries": len(lines),
                               "expected_removed": dups[name], "names_another": foreign,
                               "status": "PASS" if own and not foreign else "FAIL"})
        ch = calls.get(f"iso_{name}_chart", {})
        reply = ch.get("chart_reply", "") or ""
        ms = DOMAINS[name].roles["m_sum"]
        foreign = [o for o in csvs if o != name and f"for {o}" in reply]
        out["charts"].append({"dataset": name, "names_its_measure": ms in reply,
                              "names_another_dataset": foreign,
                              "status": "PASS" if ms in reply and not foreign else "FAIL"})
    return out


def phase_j() -> None:
    out = []
    for name in ("financial", "crm", "logistics"):
        for n in (1_000, 100_000):
            a = C.DATA / "repro" / f"{name}_{n}_a.csv"
            b = C.DATA / "repro" / f"{name}_{n}_b.csv"
            write_dataset(DOMAINS[name], n, a, C.SEED)
            write_dataset(DOMAINS[name], n, b, C.SEED)
            ref = C.CHECKPOINT / f"{'C' if n == 1000 else 'D'}_{name}" / "derived.json"
            principal_sha = json.loads(ref.read_text())["manifest"]["sha256"] \
                if ref.exists() else None
            ha, hb = sha256(a), sha256(b)
            results = []
            for tag in ("r1", "r2"):
                ws = f"dbench_repro_{tag}"
                reset_ws(ws)
                d = DOMAINS[name]
                st = F.ingest_steps(f"j{tag}", str(a), name, ws)
                st += F.dedupe_steps(f"j{tag}", name, ws)
                st += F.contract_steps(f"j{tag}", name, ws, **contract_kwargs(d, "record_id"))
                for c in cases(d, list(_registry()))[:12]:
                    if c["class"] in ("NOT_APPLICABLE", "UNCLASSIFIED"):
                        continue
                    st.append(F.call(f"{c['analysis']}_{c['variant']}", c["analysis"],
                                     c["params"], ws, name, stage="repro"))
                recs = steps_unit(f"J_{name}_{n}_{tag}", "J", {"name": "repro", "steps": st,
                                                               "ws": ws})
                results.append({r["variant"]: (Path(r["result_path"]).read_text()
                                               if r.get("result_path") and Path(
                                                   r["result_path"]).exists() else None)
                                for r in recs if r.get("event") == "verify"})
                reset_ws(ws)
            same = sorted(k for k in results[0] if results[0][k] == results[1].get(k))
            diff = sorted(k for k in results[0] if results[0][k] != results[1].get(k))
            out.append({"domain": name, "rows": n, "sha_a": ha, "sha_b": hb,
                        "sha_principal": principal_sha,
                        "dataset_identical": ha == hb and (principal_sha in (None, ha)),
                        "results_identical": same, "results_different": diff,
                        "status": "PASS" if ha == hb and not diff and (
                            principal_sha in (None, ha)) else "FAIL"})
            a.unlink(missing_ok=True)
            b.unlink(missing_ok=True)
    atomic_json(C.CHECKPOINT / "J_repro.json", out)
    mark("J", {"cases": len(out)})


# --------------------------------------------------------------------------- main


def reverify(unit: str) -> None:
    """Recompute a principal unit's verification from its preserved evidence: the worker JSONL,
    the copied result files, and the dataset regenerated from its seed (checked by sha256).
    The tools are not called again; the earlier derived.json is kept as derived_<time>.json."""
    d = C.CHECKPOINT / unit
    old = json.loads((d / "derived.json").read_text())
    dom = DOMAINS[old["domain"]]
    n = old["rows"]
    csv_path = C.DATA / f"{old['domain']}_{n}.csv"
    write_dataset(dom, n, csv_path, C.SEED)
    if sha256(csv_path) != old["manifest"]["sha256"]:
        raise RuntimeError(f"{unit}: regenerated dataset differs from the one benchmarked")
    records = []
    for p in sorted(d.glob("calls*.jsonl")):
        records += verify.load(p)
    records = remap(records, unit)
    ctx = {"domain": old["domain"], "rows": n, "unit": unit, "phase": old["phase"]}
    data = oracle.Data(csv_path, dom.types, dom.date, dom.window, dom.exclusions)
    shutil.copy2(d / "derived.json", d / f"derived_{int(time.time())}.json")
    old["correctness"] = verify.correctness(records, data, dom, context=ctx)
    old["paging"] = verify.paging(records, ctx)
    old["reverified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    atomic_json(d / "derived.json", old)
    csv_path.unlink(missing_ok=True)
    log(f"{unit}: reverified, checks {sum(x['checks_passed'] for x in old['correctness'])}/"
        f"{sum(x['checks_total'] for x in old['correctness'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reverify", nargs="*", default=None)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--domains", nargs="*", default=list(DOMAINS))
    # stress units to run, as name:rows (default: every STRESS_DOMAINS x STRESS_SIZES)
    ap.add_argument("--stress", nargs="*", default=None)
    args = ap.parse_args()
    C.OUT.mkdir(parents=True, exist_ok=True)
    C.CHECKPOINT.mkdir(parents=True, exist_ok=True)
    C.DATA.mkdir(parents=True, exist_ok=True)
    if args.report:
        from domain_benchmark import report
        report.main()
        return 0
    if args.reverify is not None:
        for u in args.reverify:
            reverify(u)
        return 0
    want = set(args.only) if args.only else set("ABCDEFGHIJK")
    done = state()["done"]
    t_start = time.time()

    def unit(name, fn, *a):
        if name in state()["done"]:
            log(f"{name}: already done, skipped (evidence kept)")
            return
        try:
            fn(*a)
        except Exception as exc:  # noqa: BLE001 - a harness fault is logged and the run goes on
            log(f"{name}: HARNESS ERROR {type(exc).__name__}: {exc}")
            (C.OUT / "crashes").mkdir(parents=True, exist_ok=True)
            (C.OUT / "crashes" / f"harness_{name}_{int(time.time())}.txt").write_text(
                traceback.format_exc())
            s = state()
            s.setdefault("harness_errors", []).append({"unit": name, "error": str(exc),
                                                       "at": time.strftime("%H:%M:%S")})
            atomic_json(STATE, s)

    if "A" in want:
        unit("A", phase_a)
    if "B" in want:
        unit("B", phase_b)
    for phase, n in (("C", 1_000), ("D", 100_000), ("E", 1_000_000)):
        if phase in want:
            for name in args.domains:
                unit(f"{phase}_{name}", principal_unit, f"{phase}_{name}", phase, name, n)
    if "F" in want:
        unit("F", phase_f)
    if "G" in want:
        unit("G", phase_g)
    if "H" in want:
        unit("H", phase_h)
    if "I" in want:
        pairs = ([(s.split(":")[0], int(s.split(":")[1])) for s in args.stress]
                 if args.stress is not None else
                 [(name, n) for name in C.STRESS_DOMAINS for n in C.STRESS_SIZES])
        for name, n in pairs:
                u = f"I_{name}_{n}"
                if u in state()["done"]:
                    continue
                gate = stress_gate(name, n)
                log(f"{u}: resource gate {gate}")
                if not gate["ok"]:
                    mark(u, {"status": "SKIPPED_RESOURCE_LIMIT", **gate})
                    continue
                unit(u, principal_unit, u, "I", name, n)
                s = state()
                if u in s["done"]:
                    s["done"][u]["gate"] = gate
                    atomic_json(STATE, s)
    if "J" in want:
        unit("J", phase_j)
    s = state()
    s.setdefault("runs", {})[time.strftime("%Y%m%dT%H%M%S")] = {
        "phases": sorted(want), "seconds": round(time.time() - t_start, 1)}
    atomic_json(STATE, s)
    if "K" in want:
        from domain_benchmark import report
        report.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
