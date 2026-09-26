"""The scenario matrix's five bugs (Phase 14 Step 10), one test each.

S1 a confirmed contract re-drafted from a fresh session; S2 unknown analysis parameters; S3 markup
in the report; S4 a file that is not a workbook; S5 paths the operating system cannot look up.
The full matrix, with its lifecycle, hostile-input, tool and concurrency families, is
scripts/scenario_matrix.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.report.assemble import defang  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ANSWERS = dict(
    grain="one row = one order", primary_key=["order_id"], date_column="order_date",
    measures=["units", "unit_price", "revenue"], dimensions=["region", "product", "channel"],
    aggregations={"units": "sum", "unit_price": "none", "revenue": "sum"},
    measure_definitions={"units": "items", "unit_price": "one item's price",
                         "revenue": "units x unit_price"},
    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31", caveats=["a caveat"])


@pytest.fixture()
def be():
    return RealBackend()


@pytest.fixture()
def ws(be):
    wid = be.new_workspace_id()
    yield wid
    workspace.reset(wid)
    workspace.workspace_dir(wid).rmdir()


def _contracted(be, ws):
    path = be.save_upload(ws, "clean_sales.csv", (FIXTURES / "clean_sales.csv").read_bytes()).path
    assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
    assert be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", **ANSWERS)).ok


def test_s1_a_confirmed_contract_redrafts_as_itself(be, ws):
    """A page reload asks for a draft with no answers; it showed a blank, PROVISIONAL form."""
    _contracted(be, ws)
    d = be.draft_contract(ws, "clean_sales")
    assert d.provisional == [] and d.grain == ANSWERS["grain"]
    assert d.aggregations == ANSWERS["aggregations"]
    assert d.measure_definitions == ANSWERS["measure_definitions"]
    assert (d.analysis_window_start, d.analysis_window_end) == ("2024-01-01", "2024-12-31")
    assert d.caveats == ["a caveat"] and "v1 is in force" in d.message


def test_s1_with_no_contract_the_draft_is_the_evidence_only_one(be, ws):
    path = be.save_upload(ws, "clean_sales.csv", (FIXTURES / "clean_sales.csv").read_bytes()).path
    be.confirm_ingest(ws, be.draft_ingest(ws, path).spec)
    d = be.draft_contract(ws, "clean_sales")
    assert "grain" in d.provisional and d.grain is None


def test_s1_after_drift_the_stored_contract_is_not_passed_off_as_current(be, ws):
    _contracted(be, ws)
    be.confirm_ingest(ws, be.draft_ingest(ws, be.save_upload(
        ws, "clean_sales.csv", b"order_id,city,amount\nA,Paris,1\nB,Rome,2\n").path).spec)
    d = be.draft_contract(ws, "clean_sales")
    assert "in force" not in d.message


def test_s2_an_unknown_parameter_is_a_refusal(be, ws):
    _contracted(be, ws)
    run = be.run_analysis(ws, "clean_sales", "top_n",
                          {"dimension": "region", "measure": "revenue", "colour": "red"})
    assert run.refusal is not None and run.refusal.reason == "ANALYSIS_PARAMS_INVALID"
    assert "colour" in run.refusal.what


def test_s3_the_report_carries_the_question_but_no_runnable_markup(be, ws):
    _contracted(be, ws)
    q = "<script>alert(1)</script> is p < 0.05? [x](javascript:alert(2))"
    rep = be.build_report(ws, "clean_sales", q)
    body = be.read_artifact(ws, rep.artifacts[0].path).decode()
    assert "<script" not in body and "](javascript:" not in body
    assert "&lt;script>alert(1)" in body and "is p < 0.05?" in body  # the words, as given


def test_s3_defang_leaves_ordinary_markdown_alone():
    text = "## Head\n\n| a | b |\n|---|---|\n| 1 < 2 | [ok](#anchor) |\n\n> quote\n"
    assert defang(text) == text


@pytest.mark.parametrize("name,data", [
    ("broken.xlsx", b"PK\x03\x04 not a workbook" * 10),
    ("sales.xlsx", (FIXTURES / "clean_sales.csv").read_bytes()),
])
def test_s4_a_file_that_is_not_a_workbook_is_refused(be, ws, name, data):
    up = be.save_upload(ws, name, data)
    d = be.draft_ingest(ws, up.path) if up.refusal is None else None
    assert up.refusal is not None or (d.refusal is not None
                                      and "not a readable .xlsx" in d.refusal.what)


@pytest.mark.parametrize("path", ["x" * 5000, "a\x00b.csv"])
def test_s5_path_tools_answer_an_unusable_path_with_text(path):
    for tool in (server.check_file, server.preview_file, server.propose_ingest_spec):
        out = tool(path=path)
        assert out.startswith("BLOCKED: that is not a usable path"), (tool.__name__, out[:80])
    assert server.read_result_file(path=path).startswith("BLOCKED")
