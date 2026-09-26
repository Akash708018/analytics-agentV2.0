"""Behaviour under test: lifecycles, hostile input, every MCP tool fuzzed, concurrency (Phase 14
Step 10).

    uv run python scripts/scenario_matrix.py            # every family
    uv run python scripts/scenario_matrix.py L H        # only these families

Each check states what must be true, in code. Outcomes: CRASH (an exception escaped), WRONG (the
expectation failed), OK. Writes docs/stress/scenario_matrix.json and prints every non-OK line.
A measurement, like the stress matrix: it exits zero whatever it finds.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
import os  # noqa: E402

os.environ["ANALYTICS_WORKSPACE_TTL_HOURS"] = "0"

from backend.engine import server, workspace  # noqa: E402
from backend.engine.config import WORKSPACE_ROOT  # noqa: E402
from backend.engine.util import db  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
OUT = ROOT / "docs" / "stress" / "scenario_matrix.json"
ANSWERS = dict(
    grain="one row = one order", primary_key=["order_id"], date_column="order_date",
    measures=["units", "unit_price", "revenue"], dimensions=["region", "product", "channel"],
    aggregations={"units": "sum", "unit_price": "none", "revenue": "sum"},
    measure_definitions={"units": "items on the order", "unit_price": "price of one item",
                         "revenue": "units x unit_price"},
    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
INJECTION = 'region"; DROP TABLE clean_sales; --'


@dataclass
class Rec:
    family: str
    check: str
    outcome: str
    detail: str = ""


RECS: list[Rec] = []
BE = RealBackend()
SPACES: list[str] = []


def check(family: str, name: str):
    """Decorator: run the check, classify, record. The function returns (ok, detail)."""
    def wrap(fn):
        def run():
            try:
                ok, detail = fn()
                RECS.append(Rec(family, name, "OK" if ok else "WRONG", str(detail)[:500]))
            except Exception as exc:  # noqa: BLE001 - the point is to catch what escapes
                tb = traceback.extract_tb(exc.__traceback__)
                where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb)
                              if "backend/engine" in f.filename or "/ui/" in f.filename), "?")
                RECS.append(Rec(family, name, "CRASH",
                                f"{type(exc).__name__}: {str(exc)[:300]} @ {where}"))
        run.family = family
        CHECKS.append(run)
        return run
    return wrap


CHECKS: list = []


def fresh() -> str:
    ws = BE.new_workspace_id()
    SPACES.append(ws)
    return ws


def load(ws: str, fixture: str = "clean_sales.csv", as_name: str | None = None,
         data: bytes | None = None):
    path = BE.save_upload(ws, as_name or fixture, data or (FIX / fixture).read_bytes()).path
    d = BE.draft_ingest(ws, path)
    r = BE.confirm_ingest(ws, d.spec)
    return d, r


def contracted(ws: str) -> None:
    load(ws)
    assert BE.confirm_contract(ws, BE.draft_contract(ws, "clean_sales", **ANSWERS)).ok


def stage(ws: str, name: str) -> str:
    return next(d.stage for d in BE.list_datasets(ws) if d.name == name)


def sql(ws: str, q: str):
    con = db.connect(ws)
    try:
        return con.execute(q).fetchall()
    finally:
        con.close()


# --- L: lifecycle --------------------------------------------------------------------------------

@check("L", "re-uploading the same file keeps the contract usable")
def _():
    ws = fresh()
    contracted(ws)
    load(ws)
    run = BE.run_analysis(ws, "clean_sales", "summary_stats", {})
    return run.refusal is None, (stage(ws, "clean_sales"), run.refusal and run.refusal.what)


@check("L", "re-uploading a DIFFERENT file under the same name blocks the old contract")
def _():
    ws = fresh()
    contracted(ws)
    other = b"order_id,city,amount\nA,Paris,1\nB,Rome,2\n"
    load(ws, as_name="clean_sales.csv", data=other)
    run = BE.run_analysis(ws, "clean_sales", "summary_stats", {})
    st = stage(ws, "clean_sales")
    return run.refusal is not None and ("BLOCKED" in st or "no contract" in st), \
        (st, run.refusal and run.refusal.reason)


@check("L", "cleaning after the contract: a type change is caught as drift")
def _():
    ws = fresh()
    path = BE.save_upload(ws, "mixed_types.xlsx", (FIX / "mixed_types.xlsx").read_bytes()).path
    d = BE.draft_ingest(ws, path)
    spec = dict(d.spec, columns=[dict(c, dtype="VARCHAR") for c in d.spec["columns"]])
    BE.confirm_ingest(ws, spec)
    name = d.dataset_name
    dr = BE.draft_contract(ws, name)
    cols = [c.name for c in dr.columns]
    ok = BE.confirm_contract(ws, BE.draft_contract(
        ws, name, grain="one row = one line", primary_key=[], date_column=None,
        measures=[], dimensions=cols[:2], aggregations={}, measure_definitions={}))
    if not ok.ok:
        return False, f"setup: contract refused: {ok.refusal and ok.refusal.why}"
    p = BE.propose_cleaning(ws, name)
    free = [s.action_id for s in p.steps if s.suggested and s.kind == "CONVERT_TYPE"]
    if not free:
        return True, "no free conversion to apply (nothing to test)"
    BE.apply_cleaning(ws, name, free[:1])
    run = BE.run_analysis(ws, name, "frequency", {"column": cols[0]})
    st = stage(ws, name)
    return ("BLOCKED" in st or run.refusal is not None or "v1" in st), (st, run.refusal and
                                                                          run.refusal.reason)


@check("L", "two datasets: Explore is gated per dataset")
def _():
    ws = fresh()
    contracted(ws)
    load(ws, "merged_multiheader.xlsx")
    a = BE.analysis_menu(ws, "clean_sales")
    b = BE.analysis_menu(ws, "merged_multiheader")
    return a.refusal is None and b.refusal is not None, (a.refusal, b.refusal and b.refusal.reason)


@check("L", "reset in the middle leaves an empty, working workspace")
def _():
    ws = fresh()
    contracted(ws)
    BE.reset_workspace(ws)
    menu = BE.analysis_menu(ws, "clean_sales")
    again = load(ws)[1]
    return (BE.list_datasets(ws)[:1] and menu.refusal is not None and again.ok), \
        (menu.refusal and menu.refusal.reason, again.ok)


@check("L", "a confirmed contract re-drafts as itself (a reload shows it, not a blank form)")
def _():
    ws = fresh()
    contracted(ws)
    d = BE.draft_contract(ws, "clean_sales")  # what the Contract screen asks on a fresh session
    return (d.refusal is None and not d.provisional and d.grain == ANSWERS["grain"]
            and d.aggregations == ANSWERS["aggregations"]), (d.provisional, d.grain)


@check("L", "the report before any analysis still has nine sections")
def _():
    ws = fresh()
    contracted(ws)
    rep = BE.build_report(ws, "clean_sales", "Is anything there?")
    body = BE.read_artifact(ws, rep.artifacts[0].path).decode() if rep.artifacts else ""
    return rep.refusal is None and body.count("\n## ") >= 9, body[:200]


# --- H: hostile input ----------------------------------------------------------------------------

@check("H", "a spec whose path points into another workspace is refused")
def _():
    a, b = fresh(), fresh()
    d, _ = load(a)
    ok_b = load(b)[1]
    spec = dict(BE.draft_ingest(b, str(workspace.workspace_dir(b) / "uploads" / "clean_sales.csv"))
                .spec, path=d.spec["path"])
    r = BE.confirm_ingest(b, spec)
    return ok_b.ok and not r.ok, r.refusal and r.refusal.what


@check("H", "a column renamed to an injection string loads safely or is refused")
def _():
    ws = fresh()
    path = BE.save_upload(ws, "clean_sales.csv", (FIX / "clean_sales.csv").read_bytes()).path
    d = BE.draft_ingest(ws, path)
    cols = [dict(c) for c in d.spec["columns"]]
    cols[3]["target_name"] = INJECTION
    r = BE.confirm_ingest(ws, dict(d.spec, columns=cols))
    alive = sql(ws, "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = 'clean_sales'")[0][0]
    return alive == 1 or not r.ok, (r.ok, r.refusal and r.refusal.what)


@check("H", "two columns renamed to the same name are refused, not merged")
def _():
    ws = fresh()
    path = BE.save_upload(ws, "clean_sales.csv", (FIX / "clean_sales.csv").read_bytes()).path
    d = BE.draft_ingest(ws, path)
    cols = [dict(c) for c in d.spec["columns"]]
    cols[1]["target_name"] = cols[0]["target_name"]
    r = BE.confirm_ingest(ws, dict(d.spec, columns=cols))
    return not r.ok, r.refusal.what if r.refusal else r.message[:200]


@check("H", "an empty column name is refused")
def _():
    ws = fresh()
    path = BE.save_upload(ws, "clean_sales.csv", (FIX / "clean_sales.csv").read_bytes()).path
    d = BE.draft_ingest(ws, path)
    cols = [dict(c) for c in d.spec["columns"]]
    cols[2]["target_name"] = "   "
    r = BE.confirm_ingest(ws, dict(d.spec, columns=cols))
    return not r.ok, r.refusal.what if r.refusal else r.message[:200]


@check("H", "a forced type the data cannot hold is a refusal, not a crash")
def _():
    ws = fresh()
    path = BE.save_upload(ws, "clean_sales.csv", (FIX / "clean_sales.csv").read_bytes()).path
    d = BE.draft_ingest(ws, path)
    cols = [dict(c) for c in d.spec["columns"]]
    cols[3]["dtype"] = "DATE"  # region as a date
    r = BE.confirm_ingest(ws, dict(d.spec, columns=cols))
    return not r.ok and r.refusal is not None, r.refusal.what if r.refusal else "loaded"


@check("H", "a dataset name that is an injection string is refused")
def _():
    ws = fresh()
    path = BE.save_upload(ws, "clean_sales.csv", (FIX / "clean_sales.csv").read_bytes()).path
    d = BE.draft_ingest(ws, path)
    r = BE.confirm_ingest(ws, dict(d.spec, dataset_name='x"; DROP TABLE y; --'))
    return not r.ok, r.refusal and r.refusal.what


@check("H", "read_artifact refuses every traversal")
def _():
    ws = fresh()
    contracted(ws)
    bad = []
    for p in ("../session.duckdb", "charts/../session.duckdb", "/etc/passwd",
              "uploads/clean_sales.csv", "charts/../../local/session.duckdb", ""):
        try:
            BE.read_artifact(ws, p)
            bad.append(p)
        except ValueError:
            pass
    return not bad, f"served: {bad}"


@check("H", "analysis params carrying SQL are refused and the table survives")
def _():
    ws = fresh()
    contracted(ws)
    outs = []
    for params in ({"dimension": INJECTION, "measure": "revenue"},
                   {"dimension": "region", "measure": "revenue) FROM clean_sales; --"},
                   {"column": "' OR 1=1 --"}):
        kind = "frequency" if "column" in params else "top_n"
        outs.append(BE.run_analysis(ws, "clean_sales", kind, params).refusal is not None)
    alive = sql(ws, "SELECT count(*) FROM clean_sales")[0][0]
    return all(outs) and alive == 500, (outs, alive)


@check("H", "absurd numbers are refusals, not crashes or hangs")
def _():
    ws = fresh()
    contracted(ws)
    # Not here, by triage: n=10**12 returns every group (5), and threshold=7 is read as 7% and
    # says so -- both answers, not failures.
    cases = [("top_n", {"dimension": "region", "measure": "revenue", "n": -5}),
             ("distribution", {"measure": "revenue", "bins": 0}),
             ("distribution", {"measure": "revenue", "bins": 10**7}),
             ("pareto", {"dimension": "region", "measure": "revenue", "threshold": -1}),
             ("confidence_interval", {"measure": "revenue", "confidence": 1.5}),
             ("sample_adequacy", {"dimension": "region", "measure": "revenue", "alpha": 0,
                                  "power": 2}),
             ("frequency", {"column": "region", "limit": 0})]
    results = {}
    for kind, params in cases:
        run = BE.run_analysis(ws, "clean_sales", kind, params)
        results[f"{kind}{params}"] = "refused" if run.refusal else "ran"
    ran = [k for k, v in results.items() if v == "ran"]
    return not ran, f"ran instead of refusing: {ran}"


@check("H", "an unknown analysis and unknown parameters are refusals")
def _():
    ws = fresh()
    contracted(ws)
    a = BE.run_analysis(ws, "clean_sales", "drop_everything", {})
    b = BE.run_analysis(ws, "clean_sales", "top_n", {"dimension": "region", "measure": "revenue",
                                                     "colour": "red"})
    return a.refusal is not None and b.refusal is not None, (a.refusal and a.refusal.reason,
                                                             b.refusal and b.refusal.reason)


@check("H", "a period in the wrong shape is a refusal")
def _():
    ws = fresh()
    contracted(ws)
    run = BE.run_analysis(ws, "clean_sales", "period_compare", {
        "measure": "revenue", "period": "last month", "baseline": "2024-13", "grain": "month"})
    return run.refusal is not None, run.text[:200]


@check("H", "markup in the report question is written as text, not as markup")
def _():
    ws = fresh()
    contracted(ws)
    q = '<script>alert(1)</script> <img src=x onerror=alert(2)> **bold** [link](javascript:x)'
    rep = BE.build_report(ws, "clean_sales", q)
    body = BE.read_artifact(ws, rep.artifacts[0].path).decode()
    # A tag that can open, or a javascript: link. The words themselves stay ("onerror=" as text
    # behind an escaped '<' runs nothing): the report never paraphrases the question.
    import re
    raw = re.search(r"<(script|img)", body) or "](javascript:" in body
    i = body.find("The question asked\n")
    return not raw and "alert(1)" in body, body[i:i + 200]


@check("H", "a corrupt .xlsx is refused, not raised")
def _():
    ws = fresh()
    up = BE.save_upload(ws, "broken.xlsx", b"PK\x03\x04 this is not a workbook" * 10)
    if up.refusal:
        return True, up.refusal.what
    d = BE.draft_ingest(ws, up.path)
    return d.refusal is not None, d.message


@check("H", "a CSV renamed .xlsx is refused, not raised")
def _():
    ws = fresh()
    up = BE.save_upload(ws, "sales.xlsx", (FIX / "clean_sales.csv").read_bytes())
    if up.refusal:
        return True, up.refusal.what
    d = BE.draft_ingest(ws, up.path)
    return d.refusal is not None, d.message


@check("H", "binary content named .csv is refused or read without crashing")
def _():
    ws = fresh()
    up = BE.save_upload(ws, "image.csv", bytes(range(256)) * 50)
    if up.refusal:
        return True, up.refusal.what
    d = BE.draft_ingest(ws, up.path)
    if d.refusal or d.unresolved:
        return True, d.message
    r = BE.confirm_ingest(ws, d.spec)
    return True, r.message[:100]


@check("H", "file names that are SQL words or digits make valid datasets")
def _():
    ws = fresh()
    names = []
    for fname in ("select.csv", "2024.csv", "a b-c.csv", "ÉTÉ.csv", ".hidden.csv", "x.CSV"):
        up = BE.save_upload(ws, fname, (FIX / "clean_sales.csv").read_bytes())
        if up.refusal:
            names.append((fname, "refused: " + up.refusal.what))
            continue
        d = BE.draft_ingest(ws, up.path)
        r = BE.confirm_ingest(ws, d.spec)
        names.append((fname, d.dataset_name if r.ok else "load refused"))
    bad = [n for n in names if n[1] in ("load refused",) or n[1].startswith("refused")]
    return not bad, names


@check("H", "invalid workspace ids are refused everywhere")
def _():
    bad = []
    for wid in ("../local", "ws_ABC/../x", "", "a" * 65, "ws x", "local/..", "."):
        for call in (lambda w: BE.list_datasets(w), lambda w: BE.list_artifacts(w),
                     lambda w: BE.reset_workspace(w), lambda w: BE.analysis_menu(w, "x")):
            try:
                call(wid)
                bad.append(wid)
            except ValueError:
                pass
    return not bad, f"accepted: {sorted(set(bad))}"


@check("H", "apply_cleaning with ids from another dataset's plan is refused")
def _():
    ws = fresh()
    load(ws, "merged_multiheader.xlsx")
    load(ws)
    p = BE.propose_cleaning(ws, "merged_multiheader")
    r = BE.apply_cleaning(ws, "clean_sales", [s.action_id for s in p.steps])
    return not r.ok, r.refusal and r.refusal.reason


# --- T: every MCP tool, fuzzed ------------------------------------------------------------------

FUZZ_STR = ["", " ", "nope", INJECTION, "../../etc/passwd", "x" * 5000, "名前", "\x00"]


def _tool_fns():
    tools = asyncio.run(server.mcp.list_tools())
    return {t.name: getattr(server, t.name) for t in tools}


@check("T", "every tool answers hostile strings with text, never an exception")
def _():
    import inspect
    ws = fresh()
    contracted(ws)
    crashes = []
    for name, fn in _tool_fns().items():
        if name in ("reset_workspace",):
            continue
        sig = inspect.signature(fn)
        for value in FUZZ_STR:
            kwargs = {}
            for p in sig.parameters.values():
                if p.name == "workspace_id":
                    kwargs[p.name] = ws
                elif p.default is inspect.Parameter.empty or p.name in ("dataset_name",
                                                                         "analysis_type"):
                    ann = str(p.annotation)
                    kwargs[p.name] = ([value] if "list" in ann else
                                      (True if "bool" in ann else
                                       (1 if "int" in ann and "str" not in ann else value)))
            try:
                out = fn(**kwargs)
                if not isinstance(out, str):
                    crashes.append(f"{name}({value[:12]!r}) returned {type(out).__name__}")
            except Exception as exc:  # noqa: BLE001
                crashes.append(f"{name}({value[:12]!r}): {type(exc).__name__}: {str(exc)[:90]}")
    alive = sql(ws, "SELECT count(*) FROM clean_sales")[0][0]
    return not crashes and alive == 500, crashes[:12] + [f"({len(crashes)} in all)"]


@check("T", "wrong types through the MCP layer are validation errors, not crashes")
def _():
    # FastMCP validates arguments against the schema before the tool runs and reports a
    # pydantic ValidationError to the client as an error result (its log line: "Invalid
    # arguments for tool") -- the tool code never sees the wrong type. Either is a clean answer.
    from fastmcp.exceptions import ToolError, ValidationError  # FastMCP's own, measured
    bad = []
    for name, args in (("compute_analysis", {"dataset_name": 5, "analysis_type": ["x"]}),
                       ("render_chart", {"dataset_name": "x", "analysis_type": "top_n",
                                         "chart": 3}),
                       ("apply_cleaning_plan", {"dataset_name": "x",
                                                "approved_action_ids": "C001"}),
                       ("profile_dataset", {"dataset_name": None})):
        try:
            asyncio.run(server.mcp.call_tool(name, args))
        except (ToolError, ValidationError):
            continue
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name}: {type(exc).__name__}: {str(exc)[:100]}")
    return not bad, bad


@check("T", "a workspace_id that is a path is refused by every tool that takes one")
def _():
    import inspect
    bad = []
    for name, fn in _tool_fns().items():
        if "workspace_id" not in inspect.signature(fn).parameters or name == "reset_workspace":
            continue
        kwargs = {p: "x" for p, v in inspect.signature(fn).parameters.items()
                  if v.default is inspect.Parameter.empty}
        kwargs = {k: (["x"] if "list" in str(inspect.signature(fn).parameters[k].annotation)
                      else v) for k, v in kwargs.items()}
        try:
            fn(**kwargs, workspace_id="../../tmp/evil")
        except ValueError:
            continue
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name}: {type(exc).__name__}")
            continue
        if (WORKSPACE_ROOT.parent / "tmp" / "evil").exists() or Path("/tmp/evil").exists():
            bad.append(f"{name}: created a directory outside the root")
    return not bad, bad


# --- C: concurrency -----------------------------------------------------------------------------

@check("C", "two workspaces load and analyse at once, each sees only its own")
def _():
    a, b = fresh(), fresh()
    errors = []

    def work(ws, fixture):
        try:
            load(ws, fixture)
            for _ in range(5):
                BE.list_datasets(ws)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    t1 = threading.Thread(target=work, args=(a, "clean_sales.csv"))
    t2 = threading.Thread(target=work, args=(b, "merged_multiheader.xlsx"))
    t1.start(); t2.start(); t1.join(); t2.join()  # noqa: E702
    na = [d.name for d in BE.list_datasets(a)]
    nb = [d.name for d in BE.list_datasets(b)]
    return not errors and na == ["clean_sales"] and nb == ["merged_multiheader"], (errors, na, nb)


@check("C", "one workspace: parallel analyses and a reload do not corrupt it")
def _():
    ws = fresh()
    contracted(ws)
    errors, results = [], []

    def analyse():
        try:
            run = BE.run_analysis(ws, "clean_sales", "top_n",
                                  {"dimension": "region", "measure": "revenue"})
            results.append(run.refusal is None)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def reload_():
        try:
            load(ws)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    threads = [threading.Thread(target=analyse) for _ in range(6)] + \
        [threading.Thread(target=reload_)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    n = sql(ws, "SELECT count(*) FROM clean_sales")[0][0]
    return not errors and n == 500, (errors, results.count(True), n)


def main(argv: list[str]) -> int:
    chosen = [c for c in CHECKS if not argv or c.family in argv]
    try:
        for c in chosen:
            c()
    finally:
        for ws in SPACES:
            workspace.reset(ws)
            try:
                workspace.workspace_dir(ws).rmdir()
            except OSError:
                pass
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps([asdict(r) for r in RECS], indent=1, ensure_ascii=False))
    counts = {o: sum(r.outcome == o for r in RECS) for o in ("CRASH", "WRONG", "OK")}
    for r in RECS:
        if r.outcome != "OK":
            print(f"{r.outcome:5s} [{r.family}] {r.check}\n        {r.detail[:400]}")
    print(f"\n{len(RECS)} checks: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
