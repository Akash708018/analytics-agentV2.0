"""The cross-domain benchmark's own tests (Phase 14 Step 13, section 47).

Generator reproducibility, the oracle against hand-computed figures, the per-call record schema,
failure logging and continuation, crash attribution, the behaviour classes, and that the summary
is generated from the JSON (a changed total in the JSON changes the summary).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT.parent))

from domain_benchmark import fixtures as F  # noqa: E402
from domain_benchmark import oracle, report, summary  # noqa: E402
from domain_benchmark.domains import DOMAINS, write_dataset  # noqa: E402

REQUIRED_FIELDS = ("domain", "dataset", "rows", "tool", "analysis", "parameters", "status",
                   "wall_time_seconds", "memory_before_mb", "memory_after_mb", "peak_memory_mb",
                   "response_characters", "exception_type", "exception_message",
                   "stack_trace_file")


# --- generators -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(DOMAINS))
def test_same_seed_same_bytes_and_a_different_seed_differs(tmp_path, name):
    a, b, c = tmp_path / "a.csv", tmp_path / "b.csv", tmp_path / "c.csv"
    ma = write_dataset(DOMAINS[name], 500, a, 7)
    write_dataset(DOMAINS[name], 500, b, 7)
    write_dataset(DOMAINS[name], 500, c, 8)
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() != c.read_bytes()
    lines = a.read_text().splitlines()[1:]
    adjacent = sum(1 for x, y in zip(lines, lines[1:]) if x == y)
    assert adjacent == ma["anomalies"]["duplicate_rows"]
    assert len(lines) == ma["anomalies"]["rows_written"]


def test_every_domain_declares_the_roles_the_matrix_needs():
    for d in DOMAINS.values():
        for role in ("dims_low", "dim_mid", "dim_hi", "binary", "m_sum", "m_mean", "m_x", "m_y",
                     "m_indep", "entity"):
            assert d.roles.get(role), (d.name, role)
        for m in (d.roles["m_sum"], d.roles["m_mean"], d.roles["m_x"], d.roles["m_y"]):
            assert m in d.measures, (d.name, m)
        assert set(d.types) == set(d.columns)


def test_known_answer_fixture_is_deterministic(tmp_path):
    a = F.known_answer(tmp_path / "a", "w")
    b = F.known_answer(tmp_path / "b", "w")
    assert Path(a["path"]).read_bytes() == Path(b["path"]).read_bytes()


# --- the oracle against numbers worked by hand ---------------------------------------------


def test_aggregates_and_quantiles_by_hand():
    assert oracle.aggregate([1, 2, None, 4], "sum") == 7
    assert oracle.aggregate([1, 2, None, 4], "count") == 3
    assert oracle.aggregate(["a", "b", "a", None], "count_distinct") == 2
    assert oracle.aggregate([None], "sum") is None
    assert oracle.quantile_cont([1, 2, 3, 4], 0.25) == 1.75          # linear, as numpy's default
    assert oracle.eta_sq([[1, 1], [3, 3]]) == 1.0
    assert oracle.eta_sq([[1, 3], [1, 3]]) == 0.0


def test_hedges_g_and_power_by_hand():
    a, b = [1.0, 2.0, 3.0], [2.0, 3.0, 4.0]
    sp = 1.0
    d = 1.0 / sp
    assert math.isclose(oracle._hedges_g(a, b), d * (1 - 3 / (4 * 6 - 9)))
    assert oracle._power(0.2, 394, 394) >= 0.8 > oracle._power(0.2, 393, 393)


def test_a_printed_figure_is_judged_to_its_rounding():
    c = oracle.Checker()
    assert c.value("x", "1,234.57", 1234.566)
    assert not c.value("x", "1,234.57", 1234.58)
    assert c.value("share", "25.7%", 0.2574)
    assert not c.value("n", "10", 11, exact=True)
    assert c.value("blank", "", None)
    assert [k.status for k in c.checks] == ["PASS", "FAIL", "PASS", "FAIL", "PASS"]
    assert c.checks[1].absolute_error is not None and c.checks[1].tolerance == pytest.approx(
        0.005 + 1234.58e-9 + 1e-12)


def test_the_oracle_reads_the_csv_itself(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("id,d,v\n1,2024-01-02,5\n1,2024-01-02,5\n2,2025-01-01,7\n3,,9\n")
    d = oracle.Data(p, {"id": "int", "d": "date", "v": "float"}, "d",
                    ("2024-01-01", "2024-12-31"))
    assert (d.raw_rows, d.duplicates_dropped, len(d.scope), d.outside, d.undated) == (4, 1, 1, 1,
                                                                                     1)
    assert "backend.engine" not in Path(oracle.__file__).read_text().split("\"\"\"", 2)[2]


# --- the worker's records ------------------------------------------------------------------


def _recorder(tmp_path):
    from domain_benchmark import worker
    job = {"id": "t", "out": str(tmp_path / "calls.jsonl"), "crashes_dir": str(tmp_path / "c"),
           "domain": "x", "dataset": "y", "rows": 1000, "phase": "T"}
    return worker, worker.Recorder(job), job


def test_every_call_record_carries_the_required_fields_and_a_time(tmp_path):
    worker, rec, job = _recorder(tmp_path)
    rec.call("s", "ping", {})
    line = json.loads(Path(job["out"]).read_text().splitlines()[0])
    for f in REQUIRED_FIELDS:
        assert f in line, f
    assert line["wall_time_seconds"] > 0 and line["status"] == "OK"


def test_an_exception_is_recorded_with_its_trace_and_the_next_call_still_runs(tmp_path,
                                                                             monkeypatch):
    worker, rec, job = _recorder(tmp_path)

    def boom():
        raise RuntimeError("deliberate")

    monkeypatch.setattr(worker.server, "boom_tool", boom, raising=False)
    r = rec.call("s", "boom_tool", {})
    rec.call("s", "ping", {})
    lines = [json.loads(x) for x in Path(job["out"]).read_text().splitlines()]
    assert lines[0]["status"] == "EXCEPTION" and lines[0]["exception_type"] == "RuntimeError"
    assert Path(lines[0]["stack_trace_file"]).read_text().count("deliberate") >= 1
    assert lines[1]["status"] == "OK" and r["status"] == "EXCEPTION"


def test_a_dead_worker_is_attributed_to_the_call_in_flight(tmp_path, monkeypatch):
    from domain_benchmark import run
    out = tmp_path / "calls.jsonl"
    Path(str(out) + ".current").write_text(json.dumps({
        "tool": "compute_analysis", "analysis": "trend", "stage": "analyses",
        "parameters": {"measure": "m"}, "started": 0, "rss_before_kb": 2048}))
    job = {"out": str(out), "domain": "sales", "rows": 1000, "csv": "x.csv"}
    end = {"returncode": -9, "killed_by_timeout": False, "elapsed_s": 3.0,
           "child_peak_rss_mb": 100.0, "stdout_tail": "Killed"}
    monkeypatch.setattr(run.C, "OUT", tmp_path)
    rec = run.crash_record(job, end, "u")
    assert rec["tool"] == "compute_analysis" and rec["analysis"] == "trend"
    assert rec["exception_type"] == "ProcessDied(exit -9)" and rec["status"] == "CRASH"
    assert list((tmp_path / "crashes").glob("u_*.json"))


# --- behaviour classes ---------------------------------------------------------------------


@pytest.mark.parametrize("status,expect,text,want", [
    ("REFUSED", "reject", "BLOCKED: 'zzz' is not a column\nWHY: x", "PASS_SAFE_REJECTION"),
    ("REFUSED", "reject", "BLOCKED: something else\nWHY: x", "FAIL_MISLEADING_RESPONSE"),
    ("OK", "reject", "a table", "FAIL_SILENT_ERROR"),
    ("EXCEPTION", "reject", "", "FAIL_EXCEPTION"),
    ("OK", "accept", "a table", "PASS_ACCEPTED"),
    ("REFUSED", "accept", "BLOCKED", "FAIL_UNNECESSARY_REJECTION"),
    ("OK", "warn", "rows could not be read", "PASS_USEFUL_WARNING"),
    ("OK", "warn", "a table", "FAIL_SILENT_ERROR"),
])
def test_classification(status, expect, text, want):
    got = report.classify({"status": status, "expect": expect, "response_full": text,
                           "must_mention": ["zzz"]}, None)
    assert got["classification"] == want


# --- the summary comes from the JSON -------------------------------------------------------


def _minimal(out: Path, passed: int):
    out.mkdir(parents=True, exist_ok=True)
    s = {"domains_run": ["sales"], "principal_datasets": 1, "stress_datasets": 0,
         "calls_executed": 3, "correctness": {"checks_attempted": 10, "checks_passed": passed,
                                              "checks_failed": 10 - passed, "calls_skipped": 0},
         "known_answers": {"passed": 1, "total": 1}, "warnings": 0, "errors": 0, "crashes": 0,
         "exceptions": 0, "timeouts": 0, "call_time": {}, "median_call_seconds_by_rows": {},
         "median_time_growth": {}}
    (out / "benchmark.json").write_text(json.dumps({"summary": s, "calls": [],
                                                    "environment": {}}))


def test_summary_numbers_move_with_the_json(tmp_path):
    _minimal(tmp_path / "a", 7)
    _minimal(tmp_path / "b", 9)
    summary.write(tmp_path / "a")
    summary.write(tmp_path / "b")
    a = (tmp_path / "a" / "summary.md").read_text()
    b = (tmp_path / "b" / "summary.md").read_text()
    assert "| checks passed | 7 |" in a and "| checks passed | 9 |" in b


def test_the_console_summary_reads_the_json(tmp_path, capsys):
    _minimal(tmp_path, 4)
    summary.console(tmp_path)
    out = capsys.readouterr().out
    assert "Correct:               4" in out and "Incorrect:             6" in out
