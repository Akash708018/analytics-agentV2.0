"""One benchmark worker: runs tool calls in its own process and records every one.

    python scripts/domain_benchmark/worker.py JOB.json

The job names a mode:
  principal  the full workflow on one generated dataset (section 7), stage by stage
  steps      an explicit list of steps (fixtures, dirty data, wrong calls, repeats)

Every call is appended to the job's JSONL file the moment it returns (flushed and fsynced), so
a worker killed mid-run loses only the call in flight -- and that call is named in the marker
file `<out>.current`, which the parent reads to record the crash. Nothing is caught and
discarded: an exception becomes a record with its type, message and stack trace (the trace
also written to crashes/), and the worker carries on with the next independent call.

Memory per call: VmRSS before and after, and the call's own peak -- VmHWM after the kernel's
high-water mark is reset through /proc/self/clear_refs ("5") just before the call.
"""

from __future__ import annotations

import json
import os
import re
import signal
import struct
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[3]))
os.environ.setdefault("ANALYTICS_WORKSPACE_TTL_HOURS", "0")

from domain_benchmark import config as C  # noqa: E402
from domain_benchmark.domains import DOMAINS, contract_kwargs  # noqa: E402
from domain_benchmark.matrix import cases, chart_plan  # noqa: E402

from backend.engine import server, workspace  # noqa: E402
from backend.engine.analysis import registry  # noqa: E402
from backend.engine.contract import store  # noqa: E402


class CallTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise CallTimeout("per-call timeout")


def _status_kb(field: str) -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def _reset_peak() -> bool:
    try:
        Path("/proc/self/clear_refs").write_text("5")
        return True
    except OSError:
        return False


def refused(text: str) -> bool:
    return text.startswith("BLOCKED") or "\nreason: " in text or text.startswith("[REFUSE")


def result_path(text: str) -> str | None:
    m = re.search(r"written to\s*\n\s*(\S+\.csv)", text)
    return m.group(1) if m else None


def png_facts(path: str | None) -> dict:
    """Existence, size, signature and IHDR dimensions of a PNG, read without an image library."""
    if not path:
        return {"exists": False}
    p = Path(path)
    if not p.exists():
        return {"exists": False, "path": path}
    b = p.read_bytes()[:32]
    ok = b[:8] == b"\x89PNG\r\n\x1a\n"
    w = h = None
    if ok and len(b) >= 24:
        w, h = struct.unpack(">II", b[16:24])
    return {"exists": True, "path": path, "bytes": p.stat().st_size, "png_signature": ok,
            "width": w, "height": h, "zero_byte": p.stat().st_size == 0}


def dir_facts(ws: str) -> dict:
    d = workspace.WORKSPACE_ROOT / ws
    files = [p for p in d.rglob("*") if p.is_file()] if d.exists() else []
    by = {"results": 0, "charts": 0, "reports": 0, "other": 0}
    for p in files:
        part = p.relative_to(d).parts[0] if len(p.relative_to(d).parts) > 1 else "other"
        by[part if part in by else "other"] += 1
    return {"workspace_bytes": sum(p.stat().st_size for p in files), "files": len(files), **by}


class Recorder:
    def __init__(self, job: dict):
        self.job = job
        self.out = Path(job["out"])
        self.marker = Path(str(self.out) + ".current")
        self.crashes = Path(job["crashes_dir"])
        self.crashes.mkdir(parents=True, exist_ok=True)
        self.fh = self.out.open("a")
        self.seq = 0
        self.timeout = C.CALL_TIMEOUT_S.get(job.get("rows", 1000), 600)
        signal.signal(signal.SIGALRM, _alarm)
        import threading
        self.lock = threading.Lock()
        self.traced = bool(job.get("tracemalloc"))
        if self.traced:
            import tracemalloc
            tracemalloc.start()

    def write(self, rec: dict) -> None:
        with self.lock:
            self.fh.write(json.dumps(rec, default=str) + "\n")
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def call(self, stage: str, tool: str, kwargs: dict, *, analysis: str | None = None,
             variant: str = "", run: int = 0, run_kind: str = "measured", extra=None,
             threaded: bool = False) -> dict:
        with self.lock:
            self.seq += 1
            seq = self.seq
        fn = getattr(server, tool)
        base = {"job": self.job["id"], "phase": self.job.get("phase"),
                "domain": self.job.get("domain"), "dataset": self.job.get("dataset"),
                "rows": self.job.get("rows"), "stage": stage, "seq": seq, "tool": tool,
                "analysis": analysis, "variant": variant,
                "parameters": {k: v for k, v in kwargs.items() if k != "workspace_id"},
                "run": run, "run_kind": run_kind, "cache": "CACHE_MISS",
                "cache_note": "no application cache exists (grep: none in src/)"}
        if not threaded:
            self.marker.write_text(json.dumps({**base, "started": time.time(),
                                               "rss_before_kb": _status_kb("VmRSS")},
                                              default=str))
        rss0 = _status_kb("VmRSS")
        # Under threads the process peak is shared, so a per-call peak would be a lie.
        peak_reset = _reset_peak() if not threaded else False
        if self.traced:
            import tracemalloc
            tracemalloc.reset_peak()
        exc_type = exc_msg = trace_file = None
        text = ""
        status = "OK"
        if not threaded:
            signal.alarm(int(self.timeout))
        t0 = time.perf_counter()
        try:
            text = fn(**kwargs)
            if not isinstance(text, str):
                text = str(text)
        except CallTimeout:
            status = "TIMEOUT"
            exc_type, exc_msg = "CallTimeout", f"exceeded {self.timeout}s"
        except BaseException as exc:  # noqa: BLE001 - every exception is a finding, recorded
            status = "EXCEPTION"
            exc_type, exc_msg = type(exc).__name__, str(exc)[:2000]
            trace_file = self.crashes / f"{self.job['id']}_{seq:05d}_{tool}.txt"
            trace_file.write_text(
                f"job {self.job['id']}\ntool {tool}\nanalysis {analysis}\n"
                f"parameters {json.dumps(base['parameters'], default=str)}\n"
                f"domain {self.job.get('domain')} rows {self.job.get('rows')}\n"
                f"time {time.strftime('%Y-%m-%dT%H:%M:%S')}\nrss_before_kb {rss0}\n\n"
                + traceback.format_exc())
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            if not threaded:
                signal.alarm(0)
        secs = time.perf_counter() - t0
        rss1 = _status_kb("VmRSS")
        peak = _status_kb("VmHWM") if peak_reset else None
        if status == "OK" and refused(text):
            status = "REFUSED"
        rec = {**base, "status": status, "wall_time_seconds": round(secs, 6),
               "speed_class": C.speed_class(secs),
               "memory_before_mb": round(rss0 / 1024, 2) if rss0 else None,
               "memory_after_mb": round(rss1 / 1024, 2) if rss1 else None,
               "peak_memory_mb": round(peak / 1024, 2) if peak else None,
               "response_characters": len(text), "response_bytes": len(text.encode()),
               "response_tokens_approx": len(text) // 4,
               "exception_type": exc_type, "exception_message": exc_msg,
               "stack_trace_file": str(trace_file) if trace_file else None,
               "result_path": result_path(text), "response_head": text[:600],
               "next_step": next((ln for ln in text.splitlines() if ln.startswith("NEXT STEP")),
                                 None),
               "reason_code": (re.search(r"\nreason: (\w+)", text) or [None, None])[1]}
        if self.traced:
            import tracemalloc
            cur, pk = tracemalloc.get_traced_memory()
            rec["python_traced_current_mb"] = round(cur / 2 ** 20, 3)
            rec["python_traced_peak_mb"] = round(pk / 2 ** 20, 3)
        if threaded:
            import threading
            rec["thread"] = threading.current_thread().name
            rec["peak_memory_note"] = "not per call under threads"
        if tool == "render_chart":
            m = re.search(r"Chart written: (\S+\.png)", text)
            rec["chart"] = png_facts(m.group(1) if m else None)
            rec["chart_reply"] = text[:1500]
        if extra:
            rec.update(extra)
        if status in ("EXCEPTION", "TIMEOUT") or (self.job.get("keep_full") and text):
            rec["response_full"] = text[:20000]
        self.write(rec)
        if not threaded:
            self.marker.unlink(missing_ok=True)
        rec["_text"] = text
        return rec

    def timed(self, stage, tool, kwargs, runs, **kw) -> dict:
        """One cold run, then `runs` measured runs. Returns the last record."""
        last = self.call(stage, tool, kwargs, run=0, run_kind="cold", **kw)
        for k in range(1, runs + 1):
            last = self.call(stage, tool, kwargs, run=k, run_kind="warm", **kw)
        return last

    def note(self, stage: str, **fields) -> None:
        self.write({"job": self.job["id"], "phase": self.job.get("phase"),
                    "domain": self.job.get("domain"), "rows": self.job.get("rows"),
                    "stage": stage, "kind": "note", **fields})


# Tools a NEXT STEP may be followed into: nothing that writes a table or deletes anything.
FOLLOWABLE = {"compute_analysis", "render_chart", "describe_dataset", "list_datasets",
              "run_analysis", "profile_dataset", "profile_column", "validate_dataset",
              "get_workflow_state", "propose_dataset_contract", "propose_cleaning_plan",
              "read_result_file", "get_cleaning_ledger", "check_file", "preview_file",
              "propose_ingest_spec", "show_limits", "list_sources"}


def parse_call(next_step: str | None):
    """(tool, kwargs, note) from 'NEXT STEP: call tool(a="x", b=[...])'; None when the line
    holds no executable call (prose, a placeholder, a tool that writes)."""
    import ast
    if not next_step:
        return None, None, "no NEXT STEP"
    m = re.search(r"call (\w+)\((.*)\)", next_step)
    if not m:
        m2 = re.search(r"call (\w+)\b", next_step)
        return (m2.group(1) if m2 else None), None, "names a tool without arguments"
    tool, args = m.group(1), m.group(2)
    if "..." in args or "YYYY" in args or "<" in args:
        return tool, None, "a template with placeholders, not a call"
    try:
        node = ast.parse(f"f({args})", mode="eval").body
        kwargs = {k.arg: ast.literal_eval(k.value) for k in node.keywords}
    except (SyntaxError, ValueError):
        return tool, None, "arguments do not parse"
    return tool, kwargs, "executable"


def follow(rec: "Recorder", r: dict, ws: str) -> dict | None:
    """Execute a refusal's NEXT STEP when it is a read-only call, and record what it does."""
    tool, kwargs, note = parse_call(r.get("next_step"))
    verdict = {"of_seq": r["seq"], "next_step": r.get("next_step"), "parse": note, "tool": tool}
    if kwargs is None or tool not in FOLLOWABLE or not hasattr(server, tool):
        verdict["outcome"] = "NOT_EXECUTED"
        rec.note(r["stage"], event="follow", **verdict)
        return None
    import inspect
    if "workspace_id" in inspect.signature(getattr(server, tool)).parameters:
        kwargs["workspace_id"] = ws
    f = rec.call(r["stage"], tool, kwargs, analysis=kwargs.get("analysis_type"),
                 variant="follow", run=1, extra={"follows_seq": r["seq"]})
    same = (f["status"] == "REFUSED" and f.get("reason_code") == r.get("reason_code"))
    verdict["outcome"] = ("WORKED" if f["status"] == "OK" else
                          "REFUSED_SAME_REASON" if same else f["status"])
    rec.note(r["stage"], event="follow", **verdict)
    return f


def json_block(text: str) -> str | None:
    return text.split("```json", 1)[1].split("```", 1)[0] if "```json" in text else None



# --------------------------------------------------------------------------- principal


def principal(rec: Recorder, job: dict) -> None:
    dom = DOMAINS[job["domain"]]
    n = job["rows"]
    ws, name, path = job["ws"], job["dataset"], job["csv"]
    done = set(job.get("skip_stages", []))
    heavy = C.HEAVY_MEASURED_RUNS_1M if n >= 1_000_000 else C.MEASURED_RUNS
    if n > 1_000_000:
        heavy = C.STRESS_MEASURED_RUNS
    runs = C.STRESS_MEASURED_RUNS if n > 1_000_000 else C.MEASURED_RUNS
    pcol_runs = C.PROFILE_COLUMN_MEASURED_RUNS_1M if n >= 1_000_000 else C.MEASURED_RUNS
    W = {"workspace_id": ws}

    def stage(s):
        if s in done:
            return False
        rec.note(s, event="stage_start", **dir_facts(ws))
        return True

    if stage("ingest"):
        rec.timed("ingest", "check_file", {"path": path}, runs)
        rec.timed("ingest", "preview_file", {"path": path}, runs)
        out = rec.timed("ingest", "propose_ingest_spec", {"path": path, "dataset_name": name},
                        runs)
        spec = json_block(out["_text"])
        r = rec.call("ingest", "confirm_ingest_spec", {"spec_json": spec, **W}, run=1,
                     run_kind="cold", extra={"runs_note": "a load is timed once"})
        rec.note("ingest", event="stage_end", loaded=r["status"] == "OK", **dir_facts(ws))
    if stage("profile"):
        rec.timed("profile", "profile_dataset", {"dataset_name": name, **W}, heavy)
        for col in dom.columns:
            rec.timed("profile", "profile_column", {"dataset_name": name, "column": col, **W},
                      pcol_runs, extra={"column": col, "column_type": dom.types[col]})
    if stage("clean"):
        plan = rec.timed("clean", "propose_cleaning_plan", {"dataset_name": name, **W}, heavy)
        rec.note("clean", event="plan", plan_text=plan["_text"][:30000])
        ids = re.findall(r"^\s+(C\d{3})\s+DROP_DUPLICATE_ROWS", plan["_text"], re.M)
        if ids:
            rec.call("clean", "apply_cleaning_plan",
                     {"dataset_name": name, "approved_action_ids": ids, **W}, run=1,
                     run_kind="cold", extra={"approved": ids})
        rec.call("clean", "get_cleaning_ledger", {"dataset_name": name, **W}, run=1)
    if stage("contract"):
        rec.call("contract", "propose_dataset_contract",
                 {"dataset_name": name, **contract_kwargs(dom, dom.business_key), **W}, run=1,
                 extra={"expect": "reject: the business key is duplicated by construction"})
        out = rec.call("contract", "propose_dataset_contract",
                       {"dataset_name": name, **contract_kwargs(dom, dom.key), **W}, run=1)
        block = json_block(out["_text"])
        rec.call("contract", "confirm_dataset_contract", {"contract_json": block or "", **W},
                 run=1, extra={"provisional": "PROVISIONAL" in out["_text"]})
    if stage("state"):
        for tool, kw in (("list_datasets", {}), ("describe_dataset", {"dataset_name": name}),
                         ("get_workflow_state", {}), ("validate_dataset", {"dataset_name": name}),
                         ("run_analysis", {"dataset_name": name})):
            rec.timed("state", tool, {**kw, **W}, runs)
    if stage("analyses"):
        for case in cases(dom, list(registry.REGISTRY)):
            if case["class"] == "NOT_APPLICABLE":
                continue
            params = dict(case["params"] or {})
            if case["class"] == "UNCLASSIFIED":
                params = {}
            r = rec.timed("analyses", "compute_analysis",
                          {"dataset_name": name, "analysis_type": case["analysis"], **params, **W},
                          runs, analysis=case["analysis"], variant=case["variant"],
                          extra={"class": case["class"]})
            if r["status"] == "OK":
                rec.note("analyses", event="verify", analysis=case["analysis"],
                         variant=case["variant"], params=params, result_path=r["result_path"],
                         text=r["_text"][:12000], seq=r["seq"], class_=case["class"])
    if stage("not_applicable"):
        for case in cases(dom, list(registry.REGISTRY)):
            if case["class"] != "NOT_APPLICABLE":
                continue
            r = rec.call("not_applicable", "compute_analysis",
                         {"dataset_name": name, "analysis_type": case["analysis"],
                          **case["params"], **W}, run=1, analysis=case["analysis"],
                         variant=case["variant"],
                         extra={"class": "NOT_APPLICABLE", "expect": case["expect"],
                                "why": case["reason"]})
            if r["status"] == "REFUSED":
                follow(rec, r, ws)
            if r["status"] == "OK":
                rec.note("not_applicable", event="verify", analysis=case["analysis"],
                         variant=case["variant"], params=case["params"],
                         result_path=r["result_path"], text=r["_text"][:12000], seq=r["seq"],
                         class_="NOT_APPLICABLE")
    if stage("charts"):
        for k, (analysis, chart, params) in enumerate(chart_plan(dom)):
            params = dict(params)
            if params.get("column") == "_constant_":
                if not dom.roles.get("dim_const"):
                    rec.note("charts", event="skipped", status="SKIPPED_NOT_APPLICABLE",
                             analysis=analysis, why="this domain declares no one-value column")
                    continue
                params["column"] = dom.roles["dim_const"]
            rec.timed("charts", "render_chart",
                      {"dataset_name": name, "analysis_type": analysis, "chart": chart,
                       **params, **W}, 1 if k >= 8 else runs, analysis=analysis,
                      variant=f"chart{k}", extra={"chart_kind": chart,
                                                  "edge": k >= 8})
    if stage("read_result"):
        out = rec.call("read_result", "compute_analysis",
                       {"dataset_name": name, "analysis_type": "frequency",
                        "column": dom.roles["dim_hi"], "limit": 1000, **W}, run=1,
                       analysis="frequency", variant="for_paging")
        p = out["result_path"]
        if p:
            for start, limit in ((1, 50), (51, 50), (901, 200), (10 ** 7, 10)):
                r = rec.timed("read_result", "read_result_file",
                              {"path": p, "start": start, "limit": limit, **W}, runs,
                              extra={"page": [start, limit]})
                rec.note("read_result", event="page", path=p, start=start, limit=limit,
                         text=r["_text"][:20000])
    if stage("high_card"):
        dh = dom.roles["dim_hi"]
        ms = dom.roles["m_sum"]
        for nval in list(C.HIGH_CARD_NS) + [10 ** 7]:
            r = rec.call("high_card", "compute_analysis",
                         {"dataset_name": name, "analysis_type": "top_n", "dimension": dh,
                          "measure": ms, "n": nval, **W}, run=1, analysis="top_n",
                         variant=f"n{nval}")
            if r["status"] == "OK":
                rec.note("high_card", event="verify", analysis="top_n", variant=f"n{nval}",
                         params={"dimension": dh, "measure": ms, "n": nval},
                         result_path=r["result_path"], text=r["_text"][:12000], seq=r["seq"],
                         class_="EDGE_CASE")
        for analysis, kw in (("frequency", {"column": dh, "limit": 1000}),
                             ("concentration", {"dimension": dh, "measure": ms}),
                             ("pareto", {"dimension": dh, "measure": ms}),
                             ("group_compare", {"dimension": dh, "measure": ms})):
            r = rec.call("high_card", "compute_analysis",
                         {"dataset_name": name, "analysis_type": analysis, **kw, **W}, run=1,
                         analysis=analysis, variant="hi")
            if r["status"] == "OK":
                rec.note("high_card", event="verify", analysis=analysis, variant="hi",
                         params=kw, result_path=r["result_path"], text=r["_text"][:12000],
                         seq=r["seq"], class_="EDGE_CASE")
    if stage("report"):
        rec.timed("report", "build_report",
                  {"dataset_name": name, "question": f"What drives {dom.roles['m_sum']}?", **W},
                  runs)
    rec.note("end", event="stage_end", **dir_facts(ws))


# --------------------------------------------------------------------------- explicit steps


def steps(rec: Recorder, job: dict) -> None:
    """Explicit steps. `$json:<id>` is replaced by the JSON block of step <id>'s reply,
    `$result:<id>` by its result path."""
    outs: dict[str, dict] = {}

    def subst(v):
        if isinstance(v, str) and v.startswith("$json:"):
            return json_block(outs.get(v[6:], {}).get("_text", "")) or ""
        if isinstance(v, str) and v.startswith("$result:"):
            return outs.get(v[8:], {}).get("result_path") or "/no/such/result.csv"
        if isinstance(v, dict):
            return {k: subst(x) for k, x in v.items()}
        if isinstance(v, list):
            return [subst(x) for x in v]
        return v

    for st in job["steps"]:
        kwargs = subst(st.get("kwargs", {}))
        extra = {k: st[k] for k in ("expect", "why", "case", "must_mention", "family",
                                    "known", "class") if k in st}
        n = st.get("repeat", 1)
        r = None
        for k in range(n):
            r = rec.call(st.get("stage", "steps"), st["tool"], kwargs, analysis=st.get("analysis"),
                         variant=st.get("id", ""), run=k,
                         run_kind="cold" if k == 0 else "warm", extra=extra)
        outs[st.get("id", str(len(outs)))] = r
        if r and r["status"] == "REFUSED" and st.get("follow", True):
            ws = kwargs.get("workspace_id") or job.get("ws")
            if ws:
                follow(rec, r, ws)
        if st.get("verify") and r and r["status"] == "OK":
            rec.note(st.get("stage", "steps"), event="verify", analysis=st.get("analysis"),
                     variant=st.get("id", ""), params=kwargs, result_path=r["result_path"],
                     text=r["_text"][:12000], seq=r["seq"], class_=st.get("class", "EDGE_CASE"),
                     known=st.get("known"))


def concurrent(rec: Recorder, job: dict) -> None:
    """Threads calling the MCP tool functions at once (section 27) and the shared-workspace
    isolation checks (section 28). Datasets are already loaded and contracted by the setup
    steps of the same job."""
    from concurrent.futures import ThreadPoolExecutor

    steps(rec, {**job, "steps": job["setup"]})
    sets = job["datasets"]            # [{ws, name, domain, measure, dimension}]

    def one(task):
        k, ds, label = task
        r = rec.call("concurrency", "compute_analysis",
                     {"dataset_name": ds["name"], "analysis_type": "top_n",
                      "dimension": ds["dimension"], "measure": ds["measure"],
                      "workspace_id": ds["ws"]}, analysis="top_n", variant=label, run=k,
                     threaded=True, extra={"batch": label, "target_domain": ds["domain"]})
        if r["status"] == "OK":
            rec.note("concurrency", event="verify", analysis="top_n", variant=label,
                     params={"dimension": ds["dimension"], "measure": ds["measure"]},
                     result_path=r["result_path"], text=r["_text"][:12000], seq=r["seq"],
                     class_="VALID", target_domain=ds["domain"])
        return r

    for width in (2, 5, 10):
        for mode in ("separate", "shared"):
            pool = [d for d in sets if d["mode"] == mode][:width]
            tasks = [(k, pool[k % len(pool)], f"{mode}_x{width}") for k in range(width)]
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=width) as ex:
                rs = list(ex.map(one, tasks))
            rec.note("concurrency", event="batch", mode=mode, width=width,
                     wall_seconds=time.perf_counter() - t0,
                     statuses=[r["status"] for r in rs],
                     result_paths=[r["result_path"] for r in rs])
    # ten threads, one dataset, one analysis: the result files must not collide
    same = [d for d in sets if d["mode"] == "shared"][0]
    with ThreadPoolExecutor(max_workers=10) as ex:
        rs = list(ex.map(one, [(k, same, "same_x10") for k in range(10)]))
    rec.note("concurrency", event="batch", mode="same_dataset", width=10,
             statuses=[r["status"] for r in rs], result_paths=[r["result_path"] for r in rs])


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text())
    store.EXPORT_DIR = Path(job["export_dir"])
    rec = Recorder(job)
    rec.note("start", event="worker_start", pid=os.getpid(), rss_mb=(_status_kb("VmRSS") or 0) / 1024)
    if job["mode"] == "principal":
        principal(rec, job)
    elif job["mode"] == "concurrent":
        concurrent(rec, job)
    else:
        steps(rec, job)
    rec.note("end", event="worker_end", peak_rss_mb=(_status_kb("VmHWM") or 0) / 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
