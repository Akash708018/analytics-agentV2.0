"""The targeted regression's cases (Phase 14 Step 13, the handoff's Parts 2-9).

Each case runs inside one revision: `runner.py --src <that revision's src>` puts it first on
sys.path, so `analytics_agent` here is whichever revision is being measured. A case returns
observations -- what was called, what came back, how long it took -- and never a verdict: the
parent judges before against after (run.py), so a case cannot grade its own revision.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import random
import re
import statistics
import threading
import time
from pathlib import Path

import duckdb

from analytics_agent import server, workspace
from domain_benchmark import config as BC
from domain_benchmark.domains import DOMAINS, contract_kwargs, write_dataset

DATA = Path("/tmp/tr_data")
SEED = BC.SEED


# --------------------------------------------------------------------------- plumbing


def call(fn, **kw) -> dict:
    t = time.perf_counter()
    try:
        out, exc = fn(**kw), None
    except Exception as e:  # noqa: BLE001 - an exception is what several cases measure
        out, exc = "", e
    dt = time.perf_counter() - t
    text = out if isinstance(out, str) else repr(out)
    status = ("EXCEPTION" if exc else "REFUSED" if text.lstrip().startswith("BLOCKED")
              else "OK")
    return {"status": status, "wall_time_seconds": round(dt, 6), "response": text,
            "exception_type": type(exc).__name__ if exc else None,
            "exception_message": str(exc)[:600] if exc else None}


def obs(sub, tool, result, *, analysis=None, dataset=None, rows=None, parameters=None,
        **extra) -> dict:
    return {"sub": sub, "tool": tool, "analysis": analysis, "dataset": dataset, "rows": rows,
            "parameters": parameters or {}, **result, "extra": extra}


def block(text: str) -> str:
    return text.split("```json", 1)[1].split("```", 1)[0]


def fresh(ws: str) -> str:
    workspace.reset(ws)
    return ws


def ingest(ws, path, name):
    out = server.confirm_ingest_spec(spec_json=block(server.propose_ingest_spec(
        path=str(path), dataset_name=name)), workspace_id=ws)
    if not out.startswith("Loaded"):
        raise RuntimeError(f"setup: load of {name} failed: {out[:300]}")
    return out


def dedupe(ws, name):
    server.propose_cleaning_plan(dataset_name=name, workspace_id=ws)
    return server.apply_cleaning_plan(dataset_name=name, approved_action_ids=["C001"],
                                      workspace_id=ws)


def contract(ws, name, **kw) -> str:
    out = server.propose_dataset_contract(dataset_name=name, workspace_id=ws, **kw)
    if out.lstrip().startswith("BLOCKED"):
        return out
    return server.confirm_dataset_contract(contract_json=block(out), workspace_id=ws)


def dataset(domain: str, n: int) -> Path:
    p = DATA / f"{domain}_{n}.csv"
    if not p.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        write_dataset(DOMAINS[domain], n, p, SEED)
    return p


def tiny(name: str, header: list[str], rows: list[list]) -> Path:
    p = DATA / "tiny" / f"{name}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return p


NEXT = re.compile(r"NEXT STEP: call (\w+)\((.*?)\)(?:\s|$|--|\.)", re.S)


def next_step(text: str) -> dict:
    """The NEXT STEP's call, and whether it can be run as written (no placeholder in it)."""
    m = NEXT.search(text)
    if not m:
        return {"next_step": None, "executable": False}
    tool, args = m.group(1), m.group(2)
    placeholder = bool(re.search(r"\.\.\.|<[^>]*>|YYYY|\[\.\.\.\]", args))
    kwargs = {}
    if not placeholder:
        for k, v in re.findall(r"(\w+)=(\"[^\"]*\"|\[[^\]]*\]|[-\w.]+)", args):
            try:
                kwargs[k] = json.loads(v)
            except json.JSONDecodeError:
                kwargs[k] = v
    return {"next_step": f"{tool}({args})", "next_tool": tool, "kwargs": kwargs,
            "executable": not placeholder and hasattr(server, tool)}


def follow(ns: dict, ws: str) -> dict | None:
    """Run the NEXT STEP as written, when it can be; its outcome shows whether it helps."""
    if not ns.get("executable"):
        return None
    kw = dict(ns["kwargs"])
    if "workspace_id" in server.__dict__[ns["next_tool"]].__code__.co_varnames:
        kw.setdefault("workspace_id", ws)
    r = call(getattr(server, ns["next_tool"]), **kw)
    return {"status": r["status"], "head": r["response"][:400],
            "exception_type": r["exception_type"]}


def reason(text: str) -> str | None:
    m = re.search(r"reason: (\w+)", text)
    return m.group(1) if m else None


FIN = DOMAINS["financial"]


def fin_contract(**over):
    kw = dict(grain="one row", primary_key=["record_id"], date_column="transaction_date",
              measures=list(FIN.measures), dimensions=list(FIN.dimensions),
              aggregations=dict(FIN.measures),
              measure_definitions={m: m for m in FIN.measures},
              analysis_window_start="2023-01-01", analysis_window_end="2024-12-31")
    kw.update(over)
    return kw


# --------------------------------------------------------------------------- D1


def case_d1() -> list[dict]:
    """G's exact wrong call: count_distinct over customer_id (text), fed to the four analyses
    that raised BinderException; then the controls that must keep working."""
    ws = fresh("tr_d1")
    path = dataset("financial", 2000)
    ingest(ws, path, "adv_cd")
    dedupe(ws, "adv_cd")
    c = contract(ws, "adv_cd", **fin_contract(
        measures=["revenue", "customer_id"],
        aggregations={"revenue": "sum", "customer_id": "count_distinct"},
        measure_definitions={"revenue": "revenue", "customer_id": "distinct customers"}))
    out = [obs("setup_contract", "confirm_dataset_contract", call(lambda: c))]
    exact = (("mix_shift", {"dimension": "payment_method", "period": "2024-12",
                            "baseline": "2024-11"}),
             ("outlier_detection", {}), ("correlation", {"against": "revenue"}),
             ("driver_analysis", {}))
    for analysis, kw in exact:
        p = {"measure": "customer_id", **kw}
        r = call(server.compute_analysis, dataset_name="adv_cd", analysis_type=analysis,
                 workspace_id=ws, **p)
        out.append(obs(f"exact_{analysis}", "compute_analysis", r, analysis=analysis,
                       dataset="adv_cd", rows=2000, parameters=p, role="defect",
                       **next_step(r["response"])))
    # controls, one contract each so every aggregation is declared as asked
    ctl = [("text_count", {"customer_id": "count"}, "top_n",
            {"dimension": "payment_method", "measure": "customer_id"}),
           ("text_count_distinct", {"customer_id": "count_distinct"}, "top_n",
            {"dimension": "payment_method", "measure": "customer_id"}),
           ("numeric_sum", {"revenue": "sum"}, "mix_shift",
            {"dimension": "payment_method", "measure": "revenue", "period": "2024-12",
             "baseline": "2024-11"}),
           ("numeric_mean", {"revenue": "mean"}, "mix_shift",
            {"dimension": "payment_method", "measure": "revenue", "period": "2024-12",
             "baseline": "2024-11"}),
           ("numeric_median", {"revenue": "median"}, "top_n",
            {"dimension": "payment_method", "measure": "revenue"})]
    for tag, aggs, analysis, p in ctl:
        name = f"ctl_{tag}"
        ingest(ws, path, name)
        dedupe(ws, name)
        cc = contract(ws, name, **fin_contract(
            measures=list(aggs), aggregations=aggs, measure_definitions={m: m for m in aggs}))
        r = call(server.compute_analysis, dataset_name=name, analysis_type=analysis,
                 workspace_id=ws, **p)
        out.append(obs(f"control_{tag}", "compute_analysis", r, analysis=analysis,
                       dataset=name, rows=2000, parameters={**p, "aggregations": aggs},
                       role="control", contract_status="stored" if "stored" in cc else cc[:200]))
    workspace.reset(ws)
    return out


# --------------------------------------------------------------------------- D2


def case_d2() -> list[dict]:
    ws = fresh("tr_d2")
    shapes = {
        "both_zero_variance": ([5] * 10, [3] * 10),
        "a_zero_variance_only": ([5] * 10, list(range(10))),
        "b_zero_variance_only": (list(range(10)), [3] * 10),
        "identical_groups": ([1, 2, 3, 4, 5] * 2, [1, 2, 3, 4, 5] * 2),
        "constant_same_value": ([4] * 10, [4] * 10),
        "tiny_two_each": ([1, 2], [3, 5]),
        "normal_valid": ([10, 12, 11, 13, 9, 14, 10, 12, 11, 13],
                         [20, 22, 19, 25, 21, 23, 18, 24, 22, 20]),
    }
    out = []
    for tag, (a, b) in shapes.items():
        rows = [[f"r{i}", "2024-01-05", "a", v] for i, v in enumerate(a)] + \
               [[f"s{i}", "2024-01-05", "b", v] for i, v in enumerate(b)]
        path = tiny(f"d2_{tag}", ["id", "d", "g", "v"], rows)
        ingest(ws, path, tag)
        contract(ws, tag, grain="row", primary_key=["id"], date_column="d", measures=["v"],
                 dimensions=["g"], aggregations={"v": "sum"}, measure_definitions={"v": "v"},
                 analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
        for analysis, extra in (("hypothesis_test", {}), ("hypothesis_test", {"method": "rank"}),
                                ("effect_size", {})):
            p = {"dimension": "g", "measure": "v", **extra}
            r = call(server.compute_analysis, dataset_name=tag, analysis_type=analysis,
                     workspace_id=ws, **p)
            text = r["response"]
            out.append(obs(f"{tag}_{analysis}{'_rank' if extra else ''}", "compute_analysis",
                           r, analysis=analysis, dataset=tag, rows=len(a) + len(b),
                           parameters=p, shape=tag,
                           p_value_printed=bool(re.search(r"\bp [<\d]", text)),
                           means=[statistics.fmean(a), statistics.fmean(b)],
                           role="defect" if tag in ("both_zero_variance", "constant_same_value")
                           else "control",
                           states_zero_variance="zero variance" in text or "constant" in text))
    workspace.reset(ws)
    return out


# --------------------------------------------------------------------------- D3


def case_d3() -> list[dict]:
    ws = fresh("tr_d3")
    # G's exact files: an all-empty column (loads as VARCHAR) and 'inf'/'nan' strings
    allnull = tiny("all_null", ["id", "d", "g", "v", "w"],
                   [[f"r{i}", f"2024-01-{1 + i % 28:02d}", "x", None, i] for i in range(20)])
    infs = tiny("inf_nan", ["id", "d", "g", "v"],
                [[f"r{i}", f"2024-01-{1 + i % 28:02d}", "x" if i % 2 else "y",
                  ["inf", "-inf", "nan", "NaN", str(i)][i % 5]] for i in range(40)])
    words = tiny("text_measure", ["id", "d", "g", "v", "w"],
                 [[f"r{i}", "2024-01-05", "x", f"name{i % 7}", i] for i in range(30)])
    ingest(ws, allnull, "all_null_col")
    ingest(ws, infs, "inf_nan")
    ingest(ws, words, "words")
    base = dict(grain="one row", primary_key=["id"], date_column="d", dimensions=["g"],
                analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    out = []
    for name, col, agg in (("all_null_col", "v", "sum"), ("inf_nan", "v", "sum"),
                           ("words", "v", "sum"), ("words", "v", "mean"),
                           ("words", "v", "median"), ("words", "v", "count"),
                           ("words", "v", "count_distinct"), ("words", "w", "sum"),
                           ("words", "w", "mean"), ("words", "w", "median")):
        kw = dict(base, measures=[col], aggregations={col: agg}, measure_definitions={col: col})
        r = call(server.propose_dataset_contract, dataset_name=name, workspace_id=ws, **kw)
        stage = "propose_dataset_contract"
        analysis = None
        if r["status"] == "OK":
            conf = call(server.confirm_dataset_contract, contract_json=block(r["response"]),
                        workspace_id=ws)
            stage = "confirm_dataset_contract" if conf["status"] != "OK" else "stored"
            if conf["status"] == "OK":
                # G's exact calls: summary_stats (every measure) and trend on this measure
                analysis = call(server.compute_analysis, dataset_name=name,
                                analysis_type="summary_stats", workspace_id=ws)
                trend = call(server.compute_analysis, dataset_name=name, analysis_type="trend",
                             measure=col, grain="month", workspace_id=ws)
                if trend["status"] == "EXCEPTION" or analysis["status"] == "EXCEPTION":
                    analysis = trend if trend["status"] == "EXCEPTION" else analysis
                elif trend["status"] != "OK":
                    analysis = trend
                if analysis["status"] != "OK":
                    stage = "analysis"
            r = conf if conf["status"] != "OK" else r
        out.append(obs(f"{name}_{col}_{agg}", "propose_dataset_contract", r, dataset=name,
                       parameters={"measure": col, "aggregation": agg},
                       rejected_at=stage if stage != "stored" else None,
                       analysis_status=analysis["status"] if analysis else None,
                       analysis_exception=analysis["exception_type"] if analysis else None,
                       role="control" if agg in ("count", "count_distinct") or col == "w"
                       else "defect", **next_step(r["response"])))
    workspace.reset(ws)
    return out


# --------------------------------------------------------------------------- D4


SITES = ("analysis.runs", "clean.ledger", "clean.plan", "contract.store", "profile.runs",
         "validate.runs")


class _Counting:
    """A connection that counts the executes that raised: the retries a site made."""

    def __init__(self, con):
        self.con, self.raised = con, 0

    def execute(self, *a, **k):
        try:
            return self.con.execute(*a, **k)
        except duckdb.TransactionException:
            self.raised += 1
            raise

    def __getattr__(self, n):
        return getattr(self.con, n)


def _ensure(site):
    import importlib
    mod = importlib.import_module(f"analytics_agent.{site}")
    return getattr(mod, "ensure_table", None) or getattr(mod, "_ensure_table")


def case_d4() -> list[dict]:
    out = []
    for site in SITES:
        path = DATA / "d4" / f"{site}.duckdb"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        a = duckdb.connect(str(path))
        b = _Counting(a.cursor())
        ensure = _ensure(site)
        a.execute("BEGIN")
        ensure(a)                                   # the first creator, uncommitted
        timer = threading.Timer(0.05, lambda: a.execute("COMMIT"))
        timer.start()
        r = call(ensure, con=b)
        timer.join()
        tables = [t[0] for t in a.execute(
            "SELECT table_name FROM information_schema.tables ORDER BY 1").fetchall()]
        out.append(obs(f"site_{site}", "ensure_table", r, dataset=site,
                       retries=b.raised, tables=tables, role="defect"))
        a.close()
    # practical: N first uses of one workspace at once
    path = dataset("financial", 2000)
    for n in (2, 5, 10):
        ws = fresh(f"tr_d4_{n}")
        ingest(ws, path, "fin")
        dedupe(ws, "fin")
        contract(ws, "fin", **contract_kwargs(FIN, "record_id"))
        barrier = threading.Barrier(n)
        results = [None] * n

        def work(k):
            barrier.wait()
            results[k] = call(server.compute_analysis, dataset_name="fin",
                              analysis_type="top_n", dimension="payment_method",
                              measure="revenue", workspace_id=ws)

        threads = [threading.Thread(target=work, args=(k,)) for k in range(n)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0
        con = duckdb.connect(str(workspace.duckdb_path(ws)), read_only=True)
        tables = [t[0] for t in con.execute(
            "SELECT table_name FROM information_schema.tables ORDER BY 1").fetchall()]
        run_rows = None
        for t in tables:
            if "analysis" in t and "run" in t:
                run_rows = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        con.close()
        # one answer: the replies with each thread's own file name and time taken out (since
        # D16 concurrent results are, rightly, written to distinct files)
        bodies = {re.sub(r"\S*/workspace/\S*|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "<x>",
                         r["response"]) for r in results if r["status"] == "OK"}
        agg = {"status": "OK" if all(r["status"] == "OK" for r in results) else "EXCEPTION"
               if any(r["status"] == "EXCEPTION" for r in results) else "REFUSED",
               "wall_time_seconds": round(elapsed, 6), "response": "",
               "exception_type": next((r["exception_type"] for r in results
                                       if r["exception_type"]), None),
               "exception_message": next((r["exception_message"] for r in results
                                          if r["exception_message"]), None)}
        out.append(obs(f"threads_{n}", "compute_analysis", agg, analysis="top_n",
                       dataset="fin", rows=2000, threads=n,
                       statuses=[r["status"] for r in results], tables=tables,
                       analysis_run_rows=run_rows, distinct_answers=len(bodies),
                       role="defect"))
        workspace.reset(ws)
    return out


# --------------------------------------------------------------------------- D5


def case_d5() -> list[dict]:
    from scipy import stats as sps

    from analytics_agent.analysis import inferential
    from types import SimpleNamespace
    out = []
    # 6M: sales 10M's rank test met 5,851,188 tied values and overflowed on the original code
    for n in (100_000, 1_000_000, 2_300_000, 6_000_000):
        tied = n * 22 // 23
        con = duckdb.connect()
        con.execute(f"CREATE TABLE t AS SELECT CASE WHEN i % 2 = 0 THEN 'a' ELSE 'b' END AS g, "
                    f"CASE WHEN i % 3 = 0 THEN 'x' WHEN i % 3 = 1 THEN 'y' ELSE 'z' END AS h, "
                    f"CASE WHEN i < {tied} THEN 0.0 ELSE i::DOUBLE END AS v "
                    f"FROM range({n}) r(i)")
        scope = SimpleNamespace(dataset_name="t", where="TRUE", source='"t"')
        rows = con.execute("SELECT g, h, v FROM t").fetchall()
        a = [v for g, _, v in rows if g == "a"]
        b = [v for g, _, v in rows if g == "b"]
        ref_mw = sps.mannwhitneyu(a, b, alternative="two-sided", use_continuity=True,
                                  method="asymptotic")
        groups = {k: [v for _, h, v in rows if h == k] for k in "xyz"}
        ref_kw = sps.kruskal(*groups.values())
        for test, fn, ref in (
                ("mann_whitney", lambda: inferential.mann_whitney(con, scope, "g", "v", "a",
                                                                  "b"), ref_mw),
                ("kruskal_wallis", lambda: inferential.kruskal_wallis(con, scope, "h", "v"),
                 ref_kw)):
            r = call(fn)
            res = None
            if r["status"] == "OK":
                try:
                    res = fn()
                except Exception:  # noqa: BLE001
                    res = None
            stat = float(res.statistic) if res else None
            p = float(res.p) if res else None
            out.append(obs(f"{test}_{n}", test, {**r, "response": r["response"][:300]},
                           analysis="hypothesis_test (rank)", dataset="ties", rows=n,
                           tied_values=tied, statistic=stat, p=p,
                           reference_statistic=float(ref.statistic),
                           reference_p=float(ref.pvalue),
                           statistic_matches=stat is not None and abs(
                               stat - ref.statistic) <= 1e-9 * max(1.0, abs(ref.statistic)),
                           p_matches=p is not None and abs(p - ref.pvalue) <= max(
                               1e-12, 1e-6 * abs(ref.pvalue)),
                           role="control" if n < 2_000_000 else "defect"))
        con.close()
    return out


# --------------------------------------------------------------------------- D12


def case_d12() -> list[dict]:
    from analytics_agent.util import sql_guard
    errors, wrong = [], []

    def work(k):
        for i in range(200):
            want = f"col_{k}_{i}"
            try:
                tree = sql_guard._ast(f"SELECT 1 WHERE {want} > {i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
                return
            if want not in json.dumps(tree):
                wrong.append(want)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(16)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r = {"status": "EXCEPTION" if errors else "WRONG" if wrong else "OK",
         "wall_time_seconds": round(time.perf_counter() - t0, 6), "response": "",
         "exception_type": errors[0].split(":")[0] if errors else None,
         "exception_message": errors[0] if errors else None}
    return [obs("parser_16_threads", "sql_guard._ast", r, threads=16, parses=3200,
                errors=len(errors), swapped_results=len(wrong), role="defect")]


# --------------------------------------------------------------------------- Part 3


def case_recs() -> list[dict]:
    ws = fresh("tr_recs")
    out = []
    path = dataset("financial", 2000)
    # D6: a key repeated only by exact duplicate rows
    ingest(ws, path, "dupkey")
    r = call(server.propose_dataset_contract, dataset_name="dupkey", workspace_id=ws,
             **contract_kwargs(FIN, "record_id"))
    ns = next_step(r["response"])
    out.append(obs("dup_rows_key", "propose_dataset_contract", r, dataset="dupkey",
                   rows=2000, why_reason=reason(r["response"]), **ns,
                   followed=follow(ns, ws), role="defect"))
    # D7: correlation, one declared measure (nothing to pair); and two, with the measure given
    tiny_rows = [[f"r{i}", f"2024-{1 + i % 12:02d}-05", "ab"[i % 2], i % 5, (i * 7) % 11]
                 for i in range(120)]
    tp = tiny("recs_small", ["id", "d", "g", "v", "w"], tiny_rows)
    for name, measures in (("one_measure", ["v"]), ("two_measures", ["v", "w"])):
        ingest(ws, tp, name)
        contract(ws, name, grain="row", primary_key=["id"], date_column="d", measures=measures,
                 dimensions=["g"], aggregations={m: "sum" for m in measures},
                 measure_definitions={m: m for m in measures},
                 analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
        r = call(server.compute_analysis, dataset_name=name, analysis_type="correlation",
                 measure="v", workspace_id=ws)
        ns = next_step(r["response"])
        self_corr = bool(re.search(r'measure="v".*against="v"|against="v"', ns.get(
            "next_step") or ""))
        out.append(obs(f"correlation_{name}", "compute_analysis", r, analysis="correlation",
                       dataset=name, parameters={"measure": "v"},
                       why_reason=reason(r["response"]), self_correlation=self_corr, **ns,
                       followed=follow(ns, ws), role="defect"))
    # D8: a corrupt workbook
    bad = DATA / "tiny" / "corrupt.xlsx"
    bad.write_bytes(b"PK\x03\x04 not really a workbook")
    r = call(server.load_excel, path=str(bad), dataset_name="bad", workspace_id=ws)
    ns = next_step(r["response"])
    out.append(obs("corrupt_xlsx", "load_excel", r, dataset="corrupt.xlsx",
                   same_file_suggested=str(bad) in (ns.get("next_step") or ""), **ns,
                   followed=follow(ns, ws), role="defect"))
    # D9: the only result file, asked for by a path outside the workspace
    ws9 = fresh("tr_recs_one")
    d = workspace.workspace_dir(ws9) / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / "only.csv").write_text("a\n1\n")
    r = call(server.read_result_file, path="/etc/passwd", workspace_id=ws9)
    ns = next_step(r["response"])
    out.append(obs("only_result_file", "read_result_file", r, **ns, followed=follow(ns, ws9),
                   role="defect"))
    workspace.reset(ws9)
    # controls: nonexistent dataset, invalid chart, a group cap
    ingest(ws, path, "fin")
    dedupe(ws, "fin")
    contract(ws, "fin", **contract_kwargs(FIN, "record_id"))
    for sub, fn, kw in (
            ("nonexistent_dataset", server.compute_analysis,
             {"dataset_name": "nope", "analysis_type": "top_n", "dimension": "x",
              "measure": "y"}),
            ("invalid_chart", server.render_chart,
             {"dataset_name": "fin", "analysis_type": "trend", "chart": "pie",
              "measure": "revenue"}),
            ("group_cap", server.compute_analysis,
             {"dataset_name": "fin", "analysis_type": "pareto", "dimension": "customer_id",
              "measure": "revenue"})):
        r = call(fn, workspace_id=ws, **kw)
        ns = next_step(r["response"])
        out.append(obs(sub, fn.__name__, r, analysis=kw.get("analysis_type"), dataset=kw[
            "dataset_name"], parameters=kw, why_reason=reason(r["response"]), **ns,
                       followed=follow(ns, ws), role="control"))
    workspace.reset(ws)
    return out


# --------------------------------------------------------------------------- Part 4


def _prof_fields(cp) -> dict:
    d = dataclasses.asdict(cp)
    return json.loads(json.dumps(d, default=str))


def case_profile_correctness() -> list[dict]:
    """Every column of financial 100k through profile_column, and (where the revision has the
    single-column path) the full-table profile against the single-column one, field by field."""
    from analytics_agent.profile import table_profile
    ws = fresh("tr_pc")
    path = dataset("financial", 100_000)
    ingest(ws, path, "fin")
    specials = tiny("profile_specials", ["id", "num", "cat", "when", "hi_text", "nully",
                                         "const"],
                    [[f"id{i:06d}", i * 1.5, "abc"[i % 3], f"2024-01-{1 + i % 28:02d}",
                      f"text value {i} {i * 31 % 997}", "x" if i % 10 == 0 else None, "k"]
                     for i in range(5000)])
    ingest(ws, specials, "specials")
    out = []
    # Every tool call first: a read-only connection held open meanwhile makes each call fail
    # to connect (first run of this case: ConnectionException on both revisions).
    replies = {}
    cols_of = {}
    for name in ("fin", "specials"):
        with duckdb.connect(str(workspace.duckdb_path(ws)), read_only=True) as c0:
            cols_of[name] = [r[0] for r in c0.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ? "
                "ORDER BY ordinal_position", [name]).fetchall()]
        for col in cols_of[name] + (["nope"] if name == "specials" else []):
            replies[(name, col)] = call(server.profile_column, dataset_name=name, column=col,
                                        workspace_id=ws)
    con = duckdb.connect(str(workspace.duckdb_path(ws)), read_only=True)
    has_only = "only" in table_profile.profile_table.__code__.co_varnames
    for name in ("fin", "specials"):
        full = {cp.evidence.name if hasattr(cp.evidence, "name") else cp.evidence.column: cp
                for cp in table_profile.profile_table(con, name).columns}
        for col in cols_of[name] + (["nope"] if name == "specials" else []):
            r = replies[(name, col)]
            diffs = None
            if has_only and col in full:
                one = table_profile.profile_table(con, name, only=col).columns
                a, b = _prof_fields(full[col]), _prof_fields(one[0]) if one else {}
                diffs = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            text = re.sub(r"\d{8}-\d{6}", "<ts>", r["response"])
            out.append(obs(f"{name}.{col}", "profile_column", {**r, "response": text},
                           dataset=name, parameters={"column": col},
                           field_differences=diffs, role="correctness"))
    con.close()
    workspace.reset(ws)
    return out


PERF_COLUMNS = ("record_id", "region", "net_sales", "order_date")


def case_profile_perf(sizes=(100_000, 1_000_000, 5_000_000, 10_000_000)) -> list[dict]:
    out = []
    for n in sizes:
        ws = fresh(f"tr_pp_{n}")
        path = DATA / f"sales_{n}.csv"
        if not path.exists():
            write_dataset(DOMAINS["sales"], n, path, SEED)
        t0 = time.perf_counter()
        ingest(ws, path, "sales")
        load_s = time.perf_counter() - t0
        runs = 3 if n <= 1_000_000 else 2
        for col in PERF_COLUMNS:
            times, first = [], None
            for k in range(runs):
                r = call(server.profile_column, dataset_name="sales", column=col,
                         workspace_id=ws)
                times.append(r["wall_time_seconds"])
                first = first or r
            measured = times[1:]
            out.append(obs(f"{n}_{col}", "profile_column",
                           {**first, "response": first["response"][:300],
                            "wall_time_seconds": statistics.median(measured)},
                           dataset="sales", rows=n, parameters={"column": col},
                           cold_seconds=times[0], measured_seconds=measured,
                           min_seconds=min(measured), max_seconds=max(measured),
                           load_seconds=round(load_s, 2), role="performance"))
        workspace.reset(ws)
    return out


# --------------------------------------------------------------------------- Part 5


def _noisy_financial(n: int) -> Path:
    """The format_noise dirty variant of the benchmark (fixtures.dirty_variants), at n rows."""
    p = DATA / f"financial_{n}_dirty.csv"
    if p.exists():
        return p
    clean = dataset("financial", n)
    rng = random.Random(3)
    with clean.open(newline="") as f, p.open("w", newline="") as g:
        rd, w = csv.reader(f), csv.writer(g)
        header = next(rd)
        ix = {h: k for k, h in enumerate(header)}
        w.writerow([("Amount (USD)" if h == "amount" else "risk score %" if h == "risk_score"
                     else h) for h in header] + ["notes"])
        for r in rd:
            if r[ix["category"]] and rng.random() < 0.3:
                r[ix["category"]] = f"  {r[ix['category']]} "
            if r[ix["payment_method"]]:
                r[ix["payment_method"]] = rng.choice([str.upper, str.lower, str.title])(
                    r[ix["payment_method"]])
            if r[ix["revenue"]]:
                r[ix["revenue"]] = f"${float(r[ix['revenue']]):,.2f}"
            if r[ix["risk_score"]]:
                r[ix["risk_score"]] = f"{float(r[ix['risk_score']]) * 100:.2f}%"
            if r[ix["balance"]]:
                r[ix["balance"]] = f"{float(r[ix['balance']]):,.2f}"
            if rng.random() < 0.05:
                r[ix["expense"]] = rng.choice(["NA", "N/A", "null", "None", "unknown", ""])
            if rng.random() < 0.02:
                r[ix["branch"]] = rng.choice(["Zürich-Ω", "São Paulo", "東京", "Łódź"])
            w.writerow(r + [""])
    return p


_ACTION = re.compile(r"^\s+(C\d{3})\s+(\w+)(?: on (\S+?))?:\s*(.*)$", re.M)


class _Timed:
    def __init__(self, con):
        self.con, self.log = con, []

    def execute(self, q, *a):
        t = time.perf_counter()
        r = self.con.execute(q, *a)
        self.log.append((time.perf_counter() - t, q))
        return r

    def __getattr__(self, n):
        return getattr(self.con, n)


def case_cleaning(n: int = 1_000_000) -> list[dict]:
    from analytics_agent.clean import detect, tools as clean_tools
    out = []
    for tag, path in (("dirty", _noisy_financial(n)), ("clean", dataset("financial", n))):
        ws = fresh(f"tr_cl_{tag}")
        ingest(ws, path, "fin")
        times, first = [], None
        for _ in range(3):
            r = call(server.propose_cleaning_plan, dataset_name="fin", workspace_id=ws)
            times.append(r["wall_time_seconds"])
            first = first or r
        actions = [{"id": a, "kind": k, "column": c, "text": t}
                   for a, k, c, t in _ACTION.findall(first["response"])]
        con = duckdb.connect(str(workspace.duckdb_path(ws)), read_only=True)
        timed = _Timed(con)
        t0 = time.perf_counter()
        detect.detect(timed, source="fin", target="fin",
                      missing_tokens=list(clean_tools.DEFAULT_NA_VALUES))
        detect_s = time.perf_counter() - t0
        con.close()
        date_q = [q for _, q in timed.log if "TRY_STRPTIME" in q]
        out.append(obs(f"plan_{tag}_{n}", "propose_cleaning_plan",
                       {**first, "wall_time_seconds": statistics.median(times[1:])},
                       dataset=f"financial_{tag}", rows=n, cold_seconds=times[0],
                       measured_seconds=times[1:], actions=actions,
                       detector_queries=len(timed.log), date_queries=len(date_q),
                       date_query_seconds=round(sum(dt for dt, q in timed.log
                                                    if "TRY_STRPTIME" in q), 3),
                       detector_seconds=round(detect_s, 3), role="performance"))
        workspace.reset(ws)
    return out


def case_date_prescreen() -> list[dict]:
    """The pre-screen's ground facts: exists only in the fixed revision."""
    from analytics_agent.clean import detect
    if not hasattr(detect, "DATE_PREFIX"):
        return [obs("prescreen", "detect.DATE_PREFIX",
                    {"status": "NOT_APPLICABLE", "wall_time_seconds": 0, "response":
                     "this revision has no date pre-screen", "exception_type": None,
                     "exception_message": None}, role="ground_fact")]
    fm = detect._DAY_FIRST + detect._MONTH_FIRST + detect._DATE_FORMATS
    parsed = "COALESCE(" + ", ".join(f"TRY_STRPTIME(trim(v), '{f}')" for f in fm) + ")"
    screen = f"regexp_matches(trim(v), '{detect.DATE_PREFIX}')"
    rng = random.Random(2)
    alpha = "0123456789-/., :TJanFebMrApyulgSOctNovDecjJAN+%x\t\n\r\x0b\x0c  _#"
    bases = ["2024-01-05", "05/01/2024", "Jan 05, 2024", "5 January 2024",
             "2024-01-05 10:11:12", "20240105", "05-Jan-2024", "12.31.24", "",
             "2024/01/05", "2024-01-05T10:11:12"]
    fuzz = []
    for _ in range(600_000):
        s = list(rng.choice(bases))
        for _ in range(rng.randint(0, 4)):
            op, pos = rng.random(), rng.randint(0, len(s))
            if op < .45:
                s.insert(pos, rng.choice(alpha))
            elif op < .7 and s:
                s.pop(min(pos, len(s) - 1))
            elif s:
                s[min(pos, len(s) - 1)] = rng.choice(alpha)
        fuzz.append("".join(s))
    extra = [" 2024-01-05", "\t2024-01-05", "\n2024-01-05", "2024-01-05\n", "Jan 5, 2024",
             "january 5, 2024", "SEPT 5, 2024", "5 Jan 2024", "05/01/2024", "01-31-2024",
             "31.01.2024", "2024/01/05", "2024-01-05 10:11:12", "2024-01-05T10:11:12",
             "20240105", "2024-13-45", "2024-02-30", "Janu 5 2024", "05//01/2024",
             "123e4567-e89b-12d3-a456-426614174000", "CUST-000123", "c000123", "P-00991",
             "SKU_4411", "Electronics", "Home & Garden", "  padded  ", "x" * 300, "",
             "N/A", "null", "TXN000123", "r0001", "Mon 5 Jan 2024", "May", "mar-05-2024"]
    con = duckdb.connect()
    t0 = time.perf_counter()
    res = {}
    for tag, values in (("fuzz", fuzz), ("extra", extra)):
        p, fn = con.execute(
            f"SELECT count(*) FILTER (WHERE {parsed} IS NOT NULL), "
            f"count(*) FILTER (WHERE {parsed} IS NOT NULL AND NOT {screen}) "
            f"FROM unnest(?) t(v)", [values]).fetchone()
        rej = con.execute(f"SELECT count(*) FILTER (WHERE NOT {screen}) FROM unnest(?) t(v)",
                          [values]).fetchone()[0]
        res[tag] = {"values": len(values), "parseable": p, "false_negatives": fn,
                    "screened_out": rej}
    ids = con.execute(f"SELECT v, {screen} FROM unnest(?) t(v)",
                      [["123e4567-e89b-12d3-a456-426614174000", "CUST-000123", "c000123",
                        "SKU_4411", "Electronics", "TXN000123"]]).fetchall()
    r = {"status": "OK" if all(v["false_negatives"] == 0 for v in res.values()) else "WRONG",
         "wall_time_seconds": round(time.perf_counter() - t0, 3), "response": "",
         "exception_type": None, "exception_message": None}
    return [obs("prescreen", "detect.DATE_PREFIX", r, corpora=res,
                ids_screened_out={v: not s for v, s in ids}, role="ground_fact")]


# --------------------------------------------------------------------------- Parts 6-9


def case_cohort() -> list[dict]:
    out = []
    for n in (1_000, 100_000):
        ws = fresh(f"tr_co_{n}")
        path = dataset("education", n)
        ingest(ws, path, "education")
        dedupe(ws, "education")
        contract(ws, "education", **contract_kwargs(DOMAINS["education"], "record_id"))
        p = {"analysis_type": "cohort_retention", "chart": "heatmap", "entity": "student_id"}
        r = call(server.render_chart, dataset_name="education", workspace_id=ws, **p)
        m = re.search(r"Chart written: (\S+\.png)", r["response"])
        png = Path(m.group(1)) if m else None
        drawn = re.search(r"([\d,]+) of ([\d,]+) point\(s\) drawn", r["response"])
        out.append(obs(f"heatmap_{n}", "render_chart", r, analysis="cohort_retention",
                       dataset="education", rows=n, parameters=p,
                       characters=len(r["response"]),
                       png_bytes=png.stat().st_size if png and png.exists() else None,
                       points=drawn.groups() if drawn else None,
                       series_described=len(re.findall(r"^  \+\d+: lowest", r["response"],
                                                       re.M)),
                       role="defect"))
        workspace.reset(ws)
    return out


def case_welch() -> list[dict]:
    ws = fresh("tr_welch")
    path = dataset("sales", 100_000)
    ingest(ws, path, "sales")
    dedupe(ws, "sales")
    contract(ws, "sales", **contract_kwargs(DOMAINS["sales"], "record_id"))
    out = []
    for k in range(2):
        p = {"dimension": "returned_flag", "measure": "net_sales"}
        r = call(server.compute_analysis, dataset_name="sales", analysis_type="hypothesis_test",
                 workspace_id=ws, **p)
        m = re.search(r"df ([\d.,eE+]+?), p", r["response"])
        df_text = m.group(1) if m else None
        df_value = float(df_text.replace(",", "")) if df_text else None
        out.append(obs(f"welch_run{k + 1}", "compute_analysis", r, analysis="hypothesis_test",
                       dataset="sales", rows=100_000, parameters=p, df_text=df_text,
                       df_value=df_value, role="defect"))
    workspace.reset(ws)
    return out


def case_paging() -> list[dict]:
    ws = fresh("tr_page")
    d = workspace.workspace_dir(ws) / "results"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "frequency_many.csv"
    f.write_text("k,v\n" + "".join(f"k{i},{i}\n" for i in range(984)))
    out = []
    for sub, start, limit in (("asked_200", 901, 200), ("asked_200_first", 1, 200),
                              ("asked_10_control", 1, 10), ("last_page", 951, 200)):
        r = call(server.read_result_file, path=str(f), start=start, limit=limit,
                 workspace_id=ws)
        text = r["response"]
        rng = re.search(r"rows ([\d,]+) to ([\d,]+) of ([\d,]+)", text)
        more = re.search(r"([\d,]+) rows after this page", text)
        out.append(obs(sub, "read_result_file", r, parameters={"start": start, "limit": limit},
                       requested_limit=limit,
                       states_cap="was asked; a page holds at most" in text,
                       range=rng.groups() if rng else None,
                       rows_after=more.group(1) if more else None,
                       has_more=bool(more), next_call=next_step("NEXT STEP: call " + (
                           re.search(r"read_result_file\(.*\)", text.split("rows after", 1)[-1])
                           .group(0) if more else ""))["next_step"] if more else None,
                       role="control" if limit <= 50 else "defect"))
    workspace.reset(ws)
    return out


def case_chart_isolation() -> list[dict]:
    ws = fresh("tr_iso")
    a = tiny("iso_a", ["id", "d", "g", "revenue"],
             [[f"a{i}", f"2024-0{i + 1}-05", "x", v] for i, v in enumerate((101, 102, 103))])
    b = tiny("iso_b", ["id", "d", "g", "revenue"],
             [[f"b{i}", f"2024-0{i + 1}-05", "x", v] for i, v in enumerate((9001, 9002, 9003))])
    out = []
    for name, path in (("ds_a", a), ("ds_b", b)):
        ingest(ws, path, name)
        contract(ws, name, grain="row", primary_key=["id"], date_column="d",
                 measures=["revenue"], dimensions=["g"], aggregations={"revenue": "sum"},
                 measure_definitions={"revenue": "revenue"},
                 analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    own = {"ds_a": ("101", "102", "103"), "ds_b": ("9,001", "9,002", "9,003")}
    other = {"ds_a": ("9,001", "9001", "9,002", "9,003"), "ds_b": ("101", "102", "103")}
    for name in ("ds_a", "ds_b"):
        p = {"analysis_type": "trend", "chart": "line", "measure": "revenue", "grain": "month"}
        rc = call(server.render_chart, dataset_name=name, workspace_id=ws, **p)
        ra = call(server.compute_analysis, dataset_name=name, analysis_type="trend",
                  measure="revenue", grain="month", workspace_id=ws)
        m = re.search(r"Result written: (\S+\.csv)|(\S+/results/\S+\.csv)", ra["response"])
        path = m.group(1) or m.group(2) if m else None
        table = Path(path).read_text() if path and Path(path).exists() else ""
        png = re.search(r"Chart written: (\S+\.png)", rc["response"])
        chart_txt = rc["response"].split("You cannot see this image", 1)[-1]
        out.append(obs(f"chart_{name}", "render_chart", rc, analysis="trend", dataset=name,
                       parameters=p, chart_path=png.group(1) if png else None,
                       names_own_dataset=f"for {name}" in rc["response"],
                       names_other_dataset=("ds_b" if name == "ds_a" else "ds_a")
                       in rc["response"],
                       own_values_in_chart_text=[v for v in own[name] if v in chart_txt],
                       other_values_in_chart_text=[v for v in other[name] if v in chart_txt],
                       own_values_in_table=[v for v in ("101", "102", "103", "9001", "9002",
                                                        "9003") if v in table
                                            and v.replace(",", "") in "".join(own[name])
                                            .replace(",", "")],
                       other_values_in_table=[v for v in ("101", "102", "103", "9001", "9002",
                                                          "9003") if v in table and v not in
                                              "".join(own[name]).replace(",", "")],
                       role="defect"))
    workspace.reset(ws)
    return out


def case_d16() -> list[dict]:
    """24 threads of one workspace writing a result in the same second (H_concurrency found a
    result file holding another dataset's rows)."""
    from datetime import datetime

    from analytics_agent.util import results
    ws = fresh("tr_d16")
    now = datetime(2026, 9, 24, 23, 10, 51)
    barrier = threading.Barrier(24)
    got, errs = [None] * 24, []

    def work(k):
        barrier.wait()
        try:
            got[k] = results.write_result(ws, label="top_n", headers=["who"],
                                          rows=[[f"t{k}"]] * 3, now=now)
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))

    threads = [threading.Thread(target=work, args=(k,)) for k in range(24)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    paths = [g.path for g in got if g]
    wrong = [k for k, g in enumerate(got) if g and g.path.read_text().splitlines()[1:]
             != [f"t{k}"] * 3]
    r = {"status": "EXCEPTION" if errs else "WRONG" if wrong or len(set(paths)) < 24 else "OK",
         "wall_time_seconds": round(time.perf_counter() - t0, 4), "response": "",
         "exception_type": errs[0].split("(")[0] if errs else None,
         "exception_message": errs[0] if errs else None}
    workspace.reset(ws)
    return [obs("write_result_24_threads", "write_result", r, threads=24,
                distinct_paths=len(set(paths)), files_with_another_writers_rows=len(wrong),
                role="defect")]


def case_d15() -> list[dict]:
    """One plan proposed twice on a table scanned in parallel: the same examples each time."""
    ws = fresh("tr_d15")
    rows = [[f"r{i}", f"2024-{1 + i % 12:02d}-05", ["Card", "card", "CARD", "Wire", "wire"][i % 5],
             i % 97] for i in range(300_000)]
    path = tiny("d15_plan", ["id", "d", "pay", "v"], rows + rows[:4000])
    texts, times = [], []
    for k in range(2):
        ingest(ws, path, f"p{k}")
        r = call(server.propose_cleaning_plan, dataset_name=f"p{k}", workspace_id=ws)
        texts.append(r["response"].replace(f"p{k}", "P").split("NEXT STEP", 1)[0])
        times.append(r["wall_time_seconds"])
    same = texts[0] == texts[1]
    out = {"status": "OK" if same else "WRONG", "wall_time_seconds": max(times),
           "response": texts[0][:1500], "exception_type": None, "exception_message": None}
    workspace.reset(ws)
    return [obs("plan_twice_300k", "propose_cleaning_plan", out, rows=304_000,
                identical_text=same, role="defect")]


CASES = {
    "D1": case_d1, "D2": case_d2, "D3": case_d3, "D4": case_d4, "D5": case_d5, "D12": case_d12,
    "RECS": case_recs, "PROFILE_CORRECTNESS": case_profile_correctness,
    "PROFILE_PERF": case_profile_perf, "CLEANING": case_cleaning,
    "PRESCREEN": case_date_prescreen, "COHORT": case_cohort, "WELCH": case_welch,
    "PAGING": case_paging, "CHART_ISOLATION": case_chart_isolation, "D16": case_d16,
    "D15": case_d15,
}
