"""B6: v1 vs v2 on the same files and questions.

    cd backend && uv run python ../bench/compare.py        # writes bench/results/<date>.json

No network and no keys: both agents run against a SCRIPTED model, so what is compared is the
machinery -- LLM calls, tool calls, tokens sent (chars/4 of each request, v1's own estimator,
tool schemas included), checks passed, rule violations caught, wall time -- not a model's
wording. v1's own numbers come from v1's own code in this tree (seeded unchanged from 0ba324b)
and from v1's recorded docs, cited per figure.

  A. retail-like turn   v1's scripts/request_size.py scenario, re-run; v2 on the same table,
                        same question, answered through the fallback (no domain applies)
  B. marketing          v1's suggestion benches (marketing, marketing_v2): identical engine
                        code, so identical verdicts are the check; v2's six playbooks on the
                        trap files (v1 has no marketing tools: n/a, said so)
  C. SLA regression     v1's scripts/sla_bench.py, 19 checks
  D. interpretation     the nine rule-violating answers: what v1's figure check flags vs v2
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
REPO = BACKEND.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(BACKEND / "scripts"))

V1_COMMIT = "0ba324b"
V1_TREE = Path("/tmp/av1")          # v1's own checkout at V1_COMMIT (B0 step 2), when present
V1_RECORDED = {
    "retail_requests_tokens": {
        "value": [2488, 2746, 4172, 4584, 5194, 5989, 5976],
        "source": f"analytics-agent@{V1_COMMIT} docs/steps/step2_free_model_reliability.md:165-171"},
    "marketing_bench_v2_strong": {
        "value": "strong 28: 28 right, 0 wrong; strong coverage 80.0%; 1 unsure",
        "source": f"analytics-agent@{V1_COMMIT} docs/steps/marketing_bench.md:49"},
    "sla_bench": {"value": "19/19 checks right",
                  "source": f"analytics-agent@{V1_COMMIT} docs/steps/step3_provisional_metrics.md:95"},
}


def tok_n(chars: int) -> int:
    return -(-chars // 4)          # v1's rounding: characters / 4, up


def tok(text: str) -> int:
    return tok_n(len(text))


# --- A. retail ---------------------------------------------------------------------------------

def v1_retail() -> dict:
    """v1's own scenario, re-run: its Groq session with a scripted post, six tool calls."""
    import request_size as rs
    from backend.engine import workspace
    from backend.engine.webapp import agent, llm
    from backend.engine.webapp.real_backend import RealBackend
    t0 = time.perf_counter()
    be = RealBackend()
    ws = be.new_workspace_id()
    try:
        path = be.save_upload(ws, f"{rs.NAME}.csv", rs.build(30_000)).path
        assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
        draft = be.draft_contract(
            ws, rs.NAME, grain="order_id + line_no", primary_key=[], date_column="order_ts",
            measures=list(rs.MEASURES), dimensions=rs.DIMENSIONS,
            aggregations={k: v[0] for k, v in rs.MEASURES.items()},
            measure_definitions={k: v[1] for k, v in rs.MEASURES.items()},
            analysis_window_start="2023-01-01", analysis_window_end="2025-12-31",
            caveats=rs.CAVEATS)
        assert be.confirm_contract(ws, draft).ok
        script = [{"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": n, "arguments": json.dumps(a)}}]}}]}
            for i, (n, a) in enumerate(rs.CALLS, 1)]
        script.append({"choices": [{"message": {"role": "assistant",
                                                "content": "Answered from the replies above."}}]})
        sizes = []

        def post(body, **_):
            sizes.append(len(json.dumps(body, ensure_ascii=False)))
            return script.pop(0)
        groq = llm.Groq()
        groq.model = lambda: rs.MODEL
        groq.post = post
        turn = agent.answer(ws, [], rs.QUESTION, lock=lambda: be._workspace(ws),
                            list_artifacts=lambda: be.list_artifacts(ws), providers=[groq])
        replies = " ".join(c.result for c in turn.tool_calls)
        wall = time.perf_counter() - t0
        return {"llm_calls": len(sizes), "tool_calls": len(turn.tool_calls),
                "request_tokens": [tok_n(s) for s in sizes],
                "tokens_in": sum(tok_n(s) for s in sizes),
                "first_request_tokens": tok_n(sizes[0]),
                "answered": not turn.error, "replies": replies, "wall_s": round(wall, 2),
                "ws": ws, "csv": rs.build(30_000)}
    finally:
        workspace.reset(ws)


def region_revenue_2025(csv_bytes: bytes) -> dict[str, float]:
    """Independent of both engines: Python's csv module over the same file."""
    out = defaultdict(float)
    for r in csv.DictReader(io.StringIO(csv_bytes.decode())):
        if r["order_ts"].startswith("2025"):
            out[r["region"]] += float(r["line_revenue"])
    return dict(out)


def v2_retail(csv_bytes: bytes) -> dict:
    import request_size as rs
    from fastapi.testclient import TestClient
    from backend.api.app import create_app
    from backend.engine import workspace
    from backend.llm.provider import ScriptedLLM

    top = {"analysis": "top_n", "params": {"dimension": "region", "n": 10, "period": "2025",
                                           "grain": "year"}}
    from backend.tests.test_playbooks import explainer
    seen = []
    explain = explainer()          # the same explainer the playbook runs use

    def reply(system, user):
        seen.append(system + user)
        if system.startswith("You answer with tools"):
            return json.dumps({"calls": [
                {"call": "core_analyze", "params": {**top, "params": {
                    **top["params"], "measure": "line_revenue"}}},
                {"call": "core_analyze", "params": {**top, "params": {
                    **top["params"], "measure": "line_cost"}}}], "then": "answer"})
        return explain(system, user)
    t0 = time.perf_counter()
    c = TestClient(create_app(state_dir=tempfile.mkdtemp(), llm=ScriptedLLM(reply)))
    ws = f"ws_{secrets.token_hex(6)}"
    try:
        did = c.post(f"/workspaces/{ws}/uploads", files={"file": (f"{rs.NAME}.csv",
                                                                   csv_bytes)}).json()
        did = did["dataset_id"]
        r = c.post(f"/datasets/{did}/contract/confirm", json={"contract": {
            "grain": "order_id + line_no", "primary_key": [], "date_column": "order_ts",
            "measures": list(rs.MEASURES), "dimensions": rs.DIMENSIONS,
            "aggregations": {k: v[0] for k, v in rs.MEASURES.items()},
            "measure_definitions": {k: v[1] for k, v in rs.MEASURES.items()},
            "analysis_window_start": "2023-01-01", "analysis_window_end": "2025-12-31",
            "caveats": rs.CAVEATS}, "fork_choices": {
                "tax_basis": "gross_incl_gst", "fiscal_year": "april", "timezone": "ist"}})
        assert r.status_code == 200, r.text
        t = _turn(c, did, rs.QUESTION)
        a = t["answer"]
        return {"llm_calls": a["usage"]["llm_calls"], "tool_calls": a["usage"]["tool_calls"],
                "request_tokens": [tok(s) for s in seen], "tokens_in": sum(tok(s) for s in seen),
                "first_request_tokens": tok(seen[0]), "answered": t["status"] == "done",
                "results": a["results"], "flags": a["flags"],
                "wall_s": round(time.perf_counter() - t0, 2)}
    finally:
        workspace.reset(ws)


def _turn(c, did, question):
    sid = c.post("/sessions", json={}).json()["sid"]
    tid = c.post("/turns", json={"sid": sid, "dataset_id": did,
                                 "question": question}).json()["turn_id"]
    for _ in range(3000):
        t = c.get(f"/turns/{tid}").json()
        if t["status"] in ("done", "failed"):
            return t
        time.sleep(0.02)
    raise TimeoutError(tid)


def retail() -> dict:
    v1 = v1_retail()
    expect = region_revenue_2025(v1.pop("csv"))
    v2 = v2_retail(__import__("request_size").build(30_000))
    v1_hits = sum(1 for v in expect.values() if f"{v:,.2f}" in v1["replies"]
                  or f"{round(v, 2):,}" in v1["replies"])
    v2_rev = {f["name"].split(": ", 1)[1]: f["value"] for r in v2["results"]
              for f in r["figures"] if str(f.get("unit")).startswith("line_revenue")}
    v2_hits = sum(1 for k, v in expect.items() if k in v2_rev
                  and abs(float(v2_rev[k]) - v) < 0.01)
    checks = lambda d, hits: {  # noqa: E731
        "answered": d["answered"],
        "every request under Groq's 8,000 TPM": max(d["request_tokens"]) <= 8000,
        f"revenue by region 2025 matches the file (of {len(expect)})": hits}
    v1.pop("replies")
    return {"question": __import__("request_size").QUESTION,
            "v1 own tree (baseline)": v1_tree_request_size(),
            "v1": {**v1, "checks": checks(v1, v1_hits)},
            "v2": {**{k: v for k, v in v2.items() if k != "results"},
                   "checks": checks(v2, v2_hits)}}


# --- B. marketing ------------------------------------------------------------------------------

def run_script(name: str, tree: Path = BACKEND, *args) -> str:
    py = sys.executable if tree == BACKEND else str(tree / ".venv" / "bin" / "python")
    p = subprocess.run([py, str(tree / "scripts" / name), *args], cwd=tree,
                       capture_output=True, text=True, timeout=1800)
    return p.stdout + p.stderr


_NOISE = re.compile(r"\d+\.\d+s\b|ws_[0-9a-f]{12}|/[\w./-]*workspace[\w./-]*|in \d+:\d+|"
                    r"\d+(\.\d+)? ?(ms|seconds)")


_SECONDS_COL = re.compile(r"^(\S+\s+[\d,]+\s+)\d+\.\d+(\s)")   # bench tables: name, rows, s


def _clean(line: str) -> str:
    return _NOISE.sub("#", _SECONDS_COL.sub(r"\1#\2", line))


def same_as_v1(script: str) -> dict:
    """The script run in v1's own tree and in v2's; outputs compared line by line, with
    timings, workspace ids and paths blanked."""
    v2 = run_script(script)
    if not (V1_TREE / "scripts" / script).exists():
        return {"v2 lines": len(v2.splitlines()), "v1": "v1 tree not present"}
    v1 = run_script(script, V1_TREE)
    a = [_clean(ln) for ln in v1.splitlines()]
    b = [_clean(ln) for ln in v2.splitlines()]
    diff = [f"v1: {x} | v2: {y}" for x, y in zip(a, b) if x != y]
    only = sorted(set(a) ^ set(b))
    return {"lines": [len(a), len(b)], "identical": a == b,
            "same_lines_any_order": sorted(a) == sorted(b), "differing": diff[:10],
            "lines_in_only_one": only[:10]}


def marketing_benches() -> dict:
    return {s: same_as_v1(s) for s in ("marketing_bench.py", "marketing_bench_v2.py",
                                        "suggest_bench.py")}


def playbooks() -> dict:
    """The six playbooks on the trap files, through real turns, scripted planner + explainer."""
    import pytest  # noqa: F401  -- the test module imports it
    from backend.tests import test_playbooks as T
    results = {}
    tests = {"why_roas_dropped": T.test_why_roas_dropped,
             "where_is_spend_wasted": T.test_where_is_spend_wasted,
             "did_the_campaign_work": T.test_did_the_campaign_work_is_worded_as_association,
             "funnel_leak": T.test_funnel_leak, "email_health": T.test_email_health,
             "why_organic_traffic_dropped": T.test_why_organic_traffic_dropped}
    for name, fn in tests.items():
        gen = T.make_env(Path(tempfile.mkdtemp()))
        env = next(gen)
        t0 = time.perf_counter()
        try:
            fn(env)
            ok = True
        except AssertionError as e:
            ok = f"FAILED: {e}"
        finally:
            with contextlib.suppress(StopIteration):
                next(gen)
        cost = T.COSTS.get(name, {})
        results[name] = {"checks_passed": ok is True, "detail": ok,
                         **cost, "wall_s": round(time.perf_counter() - t0, 2),
                         "v1": "n/a -- v1 has no marketing tools or playbooks"}
    return results


# --- C. SLA ------------------------------------------------------------------------------------

def sla() -> dict:
    text = run_script("sla_bench.py")
    m = re.findall(r"(\d+)/(\d+) checks right", text)
    return {"v2 run": f"{m[-1][0]}/{m[-1][1]} checks right" if m else text[-400:],
            "v1 recorded": V1_RECORDED["sla_bench"], "vs v1 tree": same_as_v1("sla_bench.py")}


def v1_tree_request_size() -> dict:
    """v1's own checkout, its own script: the baseline for A (the recorded step-2 list predates
    v1's step 3, which grew its schemas)."""
    if not (V1_TREE / "scripts" / "request_size.py").exists():
        return {"v1 tree": "not present"}
    text = run_script("request_size.py", V1_TREE)
    toks = [int(x.replace(",", "")) for x in re.findall(r"~([\d,]+) tokens", text)]
    return {"request_tokens": toks, "tokens_in": sum(toks), "llm_calls": len(toks),
            "answered": "turn: answered" in text}


# --- D. interpretation -------------------------------------------------------------------------

def interpretation() -> dict:
    from backend.engine.webapp.verify import verify
    from backend.rules import interpret
    from backend.tests.test_interpret import CASES
    rows = []
    for rule, bad, trace, _, _ in CASES:
        replies = ["\n".join(trace[0]["caveats"])] if trace else []
        v1_flag = not verify(bad, replies).clean
        v2 = [v.rule for v in interpret.check(bad, trace, {rule})]
        rows.append({"rule": rule, "v1 figure check flags it": v1_flag,
                     "v2 names the rule": rule in v2})
    return {"cases": rows, "v1 caught": sum(r["v1 figure check flags it"] for r in rows),
            "v2 caught": sum(r["v2 names the rule"] for r in rows), "of": len(rows)}


def main() -> None:
    out = {"date": date.today().isoformat(), "v1_commit": V1_COMMIT,
           "estimator": "tokens = characters / 4 of each request (v1's CHARS_PER_TOKEN)"}
    for name, fn in (("retail", retail), ("marketing_benches", marketing_benches),
                     ("playbooks", playbooks), ("sla", sla), ("interpretation", interpretation)):
        t0 = time.perf_counter()
        out[name] = fn()
        print(f"{name}: done in {time.perf_counter() - t0:.1f}s", flush=True)
    dest = REPO / "bench" / "results" / f"{out['date']}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {dest}")
    print(json.dumps({k: v for k, v in out.items() if k != "playbooks"}, indent=1,
                     default=str)[:6000])
    print(json.dumps(out["playbooks"], indent=1)[:3000])


if __name__ == "__main__":
    main()
