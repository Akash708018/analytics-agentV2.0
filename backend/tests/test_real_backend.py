"""The real backend: webapp/contract.Backend on the engine (Phase 14 Step 3).

Driven end to end on the repository's fixtures -- the bytes of a real CSV and a real merged-header
workbook go through save_upload, draft, confirm, contract and artifacts exactly as the UI sends
them. No Postgres, no network.
"""

from __future__ import annotations

import inspect
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.config import DEFAULT_WORKSPACE_ID  # noqa: E402
from backend.engine.webapp import contract as c  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend, refusal_from_text  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
ANSWERS = dict(
    grain="one row = one order", primary_key=["order_id"], date_column="order_date",
    measures=["units", "unit_price", "revenue"], dimensions=["region", "product", "channel"],
    aggregations={"units": "sum", "unit_price": "none", "revenue": "sum"},
    measure_definitions={"units": "items on the order", "unit_price": "price of one item",
                         "revenue": "units x unit_price"},
    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")


@pytest.fixture()
def be():
    return RealBackend()


@pytest.fixture()
def ws(be):
    wid = be.new_workspace_id()
    yield wid
    workspace.reset(wid)
    workspace.workspace_dir(wid).rmdir()


def _loaded(be, ws, name="clean_sales.csv"):
    path = be.save_upload(ws, name, (FIXTURES / name).read_bytes()).path
    d = be.draft_ingest(ws, path)
    assert d.refusal is None and d.unresolved == [], d.message
    assert be.confirm_ingest(ws, d.spec).ok
    return d


def test_every_protocol_method_exists_with_the_same_signature():
    want = {n: inspect.signature(f) for n, f in inspect.getmembers(c.Backend, inspect.isfunction)
            if not n.startswith("_")}
    for name, sig in want.items():
        have = inspect.signature(getattr(RealBackend, name))
        assert list(have.parameters) == list(sig.parameters), name


def test_workspace_ids_are_fresh_valid_and_never_claude_desktops(be):
    ids = {be.new_workspace_id() for _ in range(50)}
    assert len(ids) == 50 and DEFAULT_WORKSPACE_ID not in ids
    assert all(i.startswith("ws_") and len(i) == 15 for i in ids)


def test_an_upload_keeps_only_its_base_name_and_refuses_other_types(be, ws):
    ok = be.save_upload(ws, "../../etc/clean_sales.csv", b"a,b\n1,2\n")
    assert ok.verdict == "OK"
    assert Path(ok.path).parent == workspace.workspace_dir(ws) / "uploads"
    bad = be.save_upload(ws, "notes.txt", b"x")
    assert bad.verdict == "REFUSE" and bad.refusal.next_step.startswith("save_upload(")


def test_a_path_outside_the_uploads_directory_is_refused(be, ws):
    outside = str(FIXTURES / "clean_sales.csv")
    assert be.draft_ingest(ws, outside).refusal is not None
    assert not be.confirm_ingest(ws, {"path": outside}).ok


def test_the_csv_drafts_with_its_grid_and_loads(be, ws):
    d = _loaded(be, ws)
    assert d.grid.rows[0][:3] == ["order_id", "order_date", "region"]
    assert d.header_rows == [1] and d.columns[0].target_name == "order_id"
    [s] = be.list_datasets(ws)
    assert s.name == "clean_sales" and s.rows == 500 and s.stage == "loaded, no contract"
    assert s.next_step == 'propose_dataset_contract(dataset_name="clean_sales")'


def test_the_merged_header_workbook_shows_its_merges_and_loads(be, ws):
    d = _loaded(be, ws, "merged_multiheader.xlsx")
    assert d.header_rows == [1, 2] and d.grid.sheet_names == ["Sales"]
    assert d.grid.merged_ranges, "the merged header's ranges reach the editor"
    assert be.list_datasets(ws)[0].rows == 150


def test_an_unresolved_contract_field_reaches_the_form_blank(be, ws):
    """P14-D18. The engine holds the guess 'one row = one order_id' for an unresolved grain; a
    form prefilled with it would send the guess back as the person's answer."""
    _loaded(be, ws)
    d = be.draft_contract(ws, "clean_sales")
    assert "grain" in d.provisional and d.grain is None
    assert d.aggregations == {} and d.measure_definitions == {}
    assert d.analysis_window_start is None
    roles = {col.name: col.suggested_role for col in d.columns}
    assert roles["revenue"] == "measure" and roles["region"] == "dimension"


def test_a_provisional_contract_is_refused_and_an_answered_one_confirmed(be, ws):
    _loaded(be, ws)
    first = be.draft_contract(ws, "clean_sales")
    refused = be.confirm_contract(ws, first)
    assert not refused.ok and refused.refusal.next_step.startswith("draft_contract(")
    answered = be.draft_contract(ws, "clean_sales", **ANSWERS)
    assert answered.provisional == [], answered.provisional
    assert answered.grain == "one row = one order"
    result = be.confirm_contract(ws, answered)
    assert result.ok, result.message
    assert "ready" in be.list_datasets(ws)[0].stage


def test_a_web_contract_is_exported_into_the_workspace_not_the_repository(be, ws):
    """P14-D19: a browser session's export stays in its own (gitignored) workspace."""
    _loaded(be, ws)
    be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", **ANSWERS))
    assert (workspace.workspace_dir(ws) / "contracts" / "clean_sales.yaml").exists()
    assert not (REPO / "docs" / "contracts" / ws).exists()


def test_artifacts_are_listed_described_and_read_and_nothing_else_is(be, ws):
    _loaded(be, ws)
    be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", **ANSWERS))
    out = server.render_chart(dataset_name="clean_sales", analysis_type="frequency",
                              chart="bar", column="region", y="rows", workspace_id=ws)
    assert "Chart written" in out, out
    arts = be.list_artifacts(ws)
    chart = next(a for a in arts if a.kind == "chart")
    assert chart.path.startswith("charts/") and "row(s) analysed" in chart.description
    assert be.read_artifact(ws, chart.path)[:8] == b"\x89PNG\r\n\x1a\n"
    for bad in ("../../pyproject.toml", "session.duckdb", "uploads/clean_sales.csv",
                "charts/../session.duckdb"):
        with pytest.raises(ValueError):
            be.read_artifact(ws, bad)


def test_two_workspaces_do_not_see_each_other(be, ws):
    other = be.new_workspace_id()
    try:
        _loaded(be, ws)
        assert be.list_datasets(other) == []
        path = be.save_upload(ws, "clean_sales.csv", b"a\n1\n").path
        assert be.draft_ingest(other, path).refusal is not None  # ws's upload, not other's
    finally:
        workspace.reset(other)
        workspace.workspace_dir(other).rmdir()


def test_overlapping_calls_on_one_workspace_are_serialised_not_failed(be, ws):
    """P14-D2/D6: unlocked, a read-only attach beside an open handle raises. The lock means a
    burst of concurrent calls on one workspace all succeed."""
    _loaded(be, ws)
    errors: list[BaseException] = []
    start = threading.Barrier(6)

    def call(i: int) -> None:
        try:
            start.wait()
            if i % 3 == 0:
                be.draft_contract(ws, "clean_sales")
            elif i % 3 == 1:
                be.list_datasets(ws)
            else:
                be.list_artifacts(ws)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_reset_empties_the_workspace(be, ws):
    _loaded(be, ws)
    assert be.reset_workspace(ws).ok
    assert be.list_datasets(ws) == [] and be.list_artifacts(ws) == []


def test_merely_looking_creates_no_workspace(be):
    """P14-D21: the sidebar lists datasets on every page view; a first visit must not leave a
    directory and a database behind."""
    from backend.engine.config import WORKSPACE_ROOT
    wid = be.new_workspace_id()
    assert be.list_datasets(wid) == [] and be.list_artifacts(wid) == []
    assert not (WORKSPACE_ROOT / wid).exists()


def test_chat_with_no_model_configured_says_how_to_configure_one(be, ws, monkeypatch):
    """Providers stubbed to none: offline whatever .env holds (P14-D24)."""
    from backend.engine.webapp import agent
    monkeypatch.setattr(agent, "configured", lambda: [])
    turn = be.chat(ws, [], "hello")
    assert turn.error and "GEMINI_API_KEY" in turn.error


def test_both_refusal_shapes_convert():
    structured = ("BLOCKED: x.\nWHY: y.\nNEXT STEP: call run_analysis(dataset_name=\"d\")\n\n"
                  "reason: NO_CONTRACT")
    r = refusal_from_text(structured)
    assert (r.reason, r.what, r.why, r.next_step) == (
        "NO_CONTRACT", "x.", "y.", 'run_analysis(dataset_name="d")')
    loose = refusal_from_text("BLOCKED: no file at p.\nNEXT STEP: check the path.")
    assert loose.reason == "LOAD_REFUSED" and loose.next_step == "check the path."


# --- expiry of idle web workspaces (Phase 14 Step 5, P14-O1) -------------------------------------

DAY = 86_400


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """The sweep pointed at a private root, so no test can touch the real workspace/."""
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setenv("ANALYTICS_WORKSPACE_TTL_HOURS", "72")
    return tmp_path


def _aged(root, name, days):
    import os
    import time
    d = root / name
    (d / "uploads").mkdir(parents=True)
    (d / "uploads" / "a.csv").write_text("a\n1\n")
    (d / "session.duckdb").write_bytes(b"x")
    t = time.time() - days * DAY
    for p in [*d.rglob("*"), d]:
        os.utime(p, (t, t))
    return d


def test_an_idle_web_workspace_is_removed_and_a_fresh_one_kept(be, root):
    _aged(root, "ws_aaaaaaaaaaaa", 4)
    _aged(root, "ws_bbbbbbbbbbbb", 1)
    assert be.sweep() == ["ws_aaaaaaaaaaaa"]
    assert sorted(p.name for p in root.iterdir()) == ["ws_bbbbbbbbbbbb"]


def test_only_web_workspaces_are_ever_swept(be, root):
    for name in ("local", "ws_short", "ws_AAAAAAAAAAAA", "mine"):
        _aged(root, name, 400)
    assert be.sweep() == []
    assert len(list(root.iterdir())) == 4


def test_a_read_keeps_a_workspace_alive(be, root):
    """Reads write nothing else (step 5 M2), so the marker is what records them."""
    import time
    d = _aged(root, "ws_cccccccccccc", 4)
    be.list_datasets("ws_cccccccccccc")  # a read; the workspace exists so it is marked
    assert (d / workspace.LAST_USED).is_file()
    assert be.sweep() == [] and d.is_dir()
    assert be.sweep(now=time.time() + 4 * DAY) == ["ws_cccccccccccc"]


def test_looking_at_a_missing_workspace_still_creates_nothing(be, root):
    be.list_datasets("ws_dddddddddddd")
    assert not (root / "ws_dddddddddddd").exists()


def test_a_workspace_in_use_is_skipped(be, root):
    d = _aged(root, "ws_eeeeeeeeeeee", 9)
    lock = be._lock("ws_eeeeeeeeeeee")
    with lock:
        assert be.sweep() == [] and d.is_dir()
    assert be.sweep() == ["ws_eeeeeeeeeeee"]


def test_ttl_zero_turns_expiry_off(be, root, monkeypatch):
    monkeypatch.setenv("ANALYTICS_WORKSPACE_TTL_HOURS", "0")
    _aged(root, "ws_ffffffffffff", 400)
    assert be.sweep() == []


def test_a_new_visitor_sweeps_at_most_once_an_hour(be, root):
    _aged(root, "ws_111111111111", 5)
    be.new_workspace_id()
    assert not (root / "ws_111111111111").exists()
    _aged(root, "ws_222222222222", 5)
    be.new_workspace_id()  # within the hour: no second sweep
    assert (root / "ws_222222222222").exists()


# --- cleaning through the web backend (Phase 14 Step 5, P14-O2) ----------------------------------

def _merged_answers():
    """merged_multiheader's own column names ("Identifiers order_id" style under the default join
    are not used: the draft's target names are read back)."""
    return dict(ANSWERS)


def test_cleaning_is_proposed_as_structure_and_the_date_then_confirms(be, ws):
    """P14-O2's own case: order_date arrives as text, so its contract cannot name it as the date
    until a person approves the conversion -- now on a screen."""
    _loaded(be, ws, "merged_multiheader.xlsx")
    name = be.list_datasets(ws)[0].name
    before = be.confirm_contract(ws, be.draft_contract(ws, name, **_merged_answers()))
    assert not before.ok, "a text column confirmed as the date column"

    p = be.propose_cleaning(ws, name)
    assert p.refusal is None and p.row_count == 150
    assert [(s.action_id, s.kind, s.column) for s in p.steps] == [
        ("C001", "CONVERT_TYPE", "order_date")]
    step = p.steps[0]
    assert step.suggested and not step.lossy and step.rows_affected == 150
    assert "TRY_CAST" in step.sql

    done = be.apply_cleaning(ws, name, ["C001"])
    assert done.ok, done.message
    again = be.propose_cleaning(ws, name)
    assert again.steps == [] and again.refusal is None and again.message.startswith("Nothing")
    after = be.confirm_contract(ws, be.draft_contract(ws, name, **_merged_answers()))
    assert after.ok, after.message


def test_cleaning_refusals_come_back_as_refusals(be, ws):
    _loaded(be, ws, "merged_multiheader.xlsx")
    name = be.list_datasets(ws)[0].name
    be.propose_cleaning(ws, name)
    none = be.apply_cleaning(ws, name, [])
    assert not none.ok and none.refusal.reason == "NOTHING_APPROVED"
    unknown = be.apply_cleaning(ws, name, ["C999"])
    assert not unknown.ok and unknown.refusal.reason == "ACTION_NOT_IN_PLAN"
    missing = be.propose_cleaning(ws, "no_such_table")
    assert missing.refusal.reason == "DATASET_NOT_LOADED" and missing.steps == []


def test_a_lossy_step_is_never_suggested(be, ws):
    """mixed_types.xlsx read all-text, as phase 6 reads it: unit_price holds 'not priced', which
    no vocabulary declares missing, so converting it loses information."""
    path = be.save_upload(ws, "mixed_types.xlsx", (FIXTURES / "mixed_types.xlsx").read_bytes()).path
    d = be.draft_ingest(ws, path)
    assert d.refusal is None and not d.unresolved, d.message
    spec = dict(d.spec, columns=[dict(c, dtype="VARCHAR") for c in d.spec["columns"]])
    assert be.confirm_ingest(ws, spec).ok
    p = be.propose_cleaning(ws, d.dataset_name)
    lossy = [s for s in p.steps if s.lossy]
    assert lossy, p.message
    assert all(not s.suggested and s.sample for s in lossy)
    assert any(s.suggested for s in p.steps), "at least one lossless step is offered"


def test_cleaning_a_workspace_never_written_creates_nothing(be):
    from backend.engine.config import WORKSPACE_ROOT
    wid = be.new_workspace_id()
    assert be.propose_cleaning(wid, "x").refusal.reason == "DATASET_NOT_LOADED"
    assert not (WORKSPACE_ROOT / wid).exists()


# --- Explore: analyses with no model (Phase 14 Step 9, P14-D65) ----------------------------------

def test_the_analysis_menu_is_the_contract_gate(be, ws):
    _loaded(be, ws)
    menu = be.analysis_menu(ws, "clean_sales")
    assert menu.refusal is not None and menu.refusal.reason == "NO_CONTRACT" and not menu.analyses


def test_every_analysis_runs_from_its_form_defaults_and_charts_where_it_should(be, ws):
    """The form is what a demo clicks through: every one of the 27 must run as it first appears,
    draw where it has a chart, and show no server path."""
    from backend.engine.analysis import registry
    from backend.engine.config import WORKSPACE_ROOT
    _loaded(be, ws)
    assert be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", **ANSWERS)).ok
    menu = be.analysis_menu(ws, "clean_sales")
    assert [s.name for s in menu.analyses] == [n for n, a in registry.REGISTRY.items()
                                               if a.surface == "v1"]   # v2 B4 (D-B4-2)
    for spec in menu.analyses:
        run = be.run_analysis(ws, "clean_sales", spec.name,
                              {p.name: p.default for p in spec.params}, spec.chart)
        assert run.refusal is None, (spec.name, run.refusal and run.refusal.text)
        assert str(WORKSPACE_ROOT) not in run.text, spec.name
        if spec.chart:
            assert any(a.kind == "chart" for a in run.artifacts), spec.name


def test_a_refused_analysis_comes_back_as_the_engines_refusal(be, ws):
    _loaded(be, ws)
    be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", **ANSWERS))
    run = be.run_analysis(ws, "clean_sales", "top_n", {"dimension": "order_id",
                                                       "measure": "revenue"})
    assert run.refusal is not None and run.text == ""


def test_the_report_is_built_and_listed(be, ws):
    _loaded(be, ws)
    be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", **ANSWERS))
    rep = be.build_report(ws, "clean_sales", "What drives revenue?")
    assert rep.refusal is None and [a.kind for a in rep.artifacts] == ["report"]
    assert b"What drives revenue?" in be.read_artifact(ws, rep.artifacts[0].path)
