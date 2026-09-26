"""The benchmark's negatives (Phase 14 Step 11, P14-O13 to O26), one test each or more.

docs/benchmark/BENCHMARK.md holds each one with its measurement; scripts/benchmark.py re-measures
all of them. N10 and N12 are recorded decisions, not fixes (P14-D77).
"""

from __future__ import annotations

import csv
import datetime as dt
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.contract import store  # noqa: E402
from backend.engine.ingest import postgres  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

CONTRACT = dict(
    grain="one row = one order", primary_key=["order_id"], date_column="order_date",
    measures=["units", "revenue"], dimensions=["region", "channel", "customer_id"],
    aggregations={"units": "sum", "revenue": "sum"},
    measure_definitions={"units": "items", "revenue": "money"},
    analysis_window_start="2023-01-01", analysis_window_end="2024-12-31")


def _write(path: Path, n: int = 400) -> None:
    """Two years with March 2024 empty and a few rows in January 2025, past the window."""
    rng = random.Random(4)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "order_date", "customer_id", "region", "channel", "units",
                    "revenue"])
        for i in range(n):
            if i % 50 == 0:
                d = dt.date(2025, 1, 1 + i % 28)
            else:
                while True:
                    d = dt.date(2023, 1, 1) + dt.timedelta(days=rng.randrange(731))
                    if (d.year, d.month) != (2024, 3):
                        break
            u = rng.randint(1, 9)
            w.writerow([f"O{i:05d}", d.isoformat(), f"C{rng.randrange(60):03d}",
                        rng.choice(["North", "South", "East", "West"]),
                        rng.choice(["web", "store"]), u, u * 10])


def _block(text: str) -> str:
    return text.split("```json", 1)[1].split("```", 1)[0]


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "EXPORT_DIR", tmp_path / "contracts")
    wid = "benchfix"
    workspace.reset(wid)
    path = tmp_path / "sales.csv"
    _write(path)
    server.confirm_ingest_spec(spec_json=_block(server.propose_ingest_spec(
        path=str(path), dataset_name="sales")), workspace_id=wid)
    server.confirm_dataset_contract(contract_json=_block(server.propose_dataset_contract(
        dataset_name="sales", workspace_id=wid, **CONTRACT)), workspace_id=wid)
    yield wid
    workspace.reset(wid)
    workspace.workspace_dir(wid).rmdir()


def _next(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith("NEXT STEP"))


# --- N1: load_excel on a file that is not a workbook ---------------------------------------


def test_n1_load_excel_refuses_a_csv_instead_of_raising(tmp_path):
    path = tmp_path / "sales.csv"
    _write(path, 20)
    out = server.load_excel(path=str(path), dataset_name="x", workspace_id="benchfix_x")
    workspace.reset("benchfix_x")
    workspace.workspace_dir("benchfix_x").rmdir()
    assert out.startswith("BLOCKED") and "not a readable .xlsx workbook" in out
    assert "propose_ingest_spec(path=" in _next(out)


# --- N2: Explore's period defaults stay inside the window ----------------------------------


def test_n2_explore_defaults_run_when_data_runs_past_the_window(tmp_path, monkeypatch):
    be = RealBackend()
    wid = be.new_workspace_id()
    try:
        path = tmp_path / "sales.csv"
        _write(path)
        up = be.save_upload(wid, "sales.csv", path.read_bytes())
        be.confirm_ingest(wid, be.draft_ingest(wid, up.path).spec)
        be.confirm_contract(wid, be.draft_contract(wid, "sales", **CONTRACT))
        menu = {a.name: a for a in be.analysis_menu(wid, "sales").analyses}
        for name in ("period_compare", "growth_decomposition", "mix_shift"):
            params = {f.name: f.default for f in menu[name].params if f.default is not None}
            assert params["period"] == "2024-12" and params["baseline"] == "2024-11"
            run = be.run_analysis(wid, "sales", name, params)
            assert run.refusal is None, (name, run.refusal)
    finally:
        workspace.reset(wid)
        workspace.workspace_dir(wid).rmdir()


# --- N4, N5, N6: a NEXT STEP that does what its WHY says -----------------------------------


def test_n4_an_unknown_analysis_points_at_the_list(ws):
    out = server.compute_analysis(dataset_name="sales", analysis_type="regression",
                                  workspace_id=ws)
    assert _next(out) == 'NEXT STEP: call run_analysis(dataset_name="sales")'


@pytest.mark.parametrize("kw,analysis", [
    (dict(analysis_type="trend", measure="revenue", grain="month", bins=5), "trend"),
    (dict(analysis_type="trend", measure="revenue", grain="fortnight"), "trend"),
    (dict(analysis_type="period_compare", measure="revenue", period="2025-01",
          baseline="2024-12", grain="month"), "period_compare"),
])
def test_n4_a_wrong_argument_repeats_the_same_analysis(ws, kw, analysis):
    out = server.compute_analysis(dataset_name="sales", workspace_id=ws, **kw)
    nxt = _next(out)
    assert f'analysis_type="{analysis}"' in nxt and "summary_stats" not in nxt, nxt
    assert "..." not in nxt


def test_n5_a_group_cap_points_at_top_n_as_its_why_says(ws):
    # concentration lost its cap in main's Cleanup Step 14 ("no needless cap"); group_compare
    # keeps one -- the (all) row must fit -- so it carries the check since the merge of 25/09.
    out = server.compute_analysis(dataset_name="sales", analysis_type="group_compare",
                                  dimension="customer_id", measure="revenue", workspace_id=ws)
    assert "top_n on customer_id" in out
    assert _next(out) == ('NEXT STEP: call compute_analysis(dataset_name="sales", '
                          'analysis_type="top_n", dimension="customer_id", measure="revenue")')
    again = server.compute_analysis(dataset_name="sales", analysis_type="top_n",
                                    dimension="customer_id", measure="revenue", workspace_id=ws)
    assert not again.startswith("BLOCKED")


def test_n5_a_column_that_does_not_exist_is_not_a_contract_question(ws):
    out = server.compute_analysis(dataset_name="sales", analysis_type="top_n",
                                  dimension="region", measure="profit", workspace_id=ws)
    assert _next(out) == 'NEXT STEP: call describe_dataset(dataset_name="sales")'


def test_n5_an_existing_undeclared_column_still_needs_the_contract(ws):
    out = server.compute_analysis(dataset_name="sales", analysis_type="hypothesis_test",
                                  dimension="order_id", measure="units", workspace_id=ws)
    assert _next(out) == 'NEXT STEP: call propose_dataset_contract(dataset_name="sales")'


def test_n6_profiling_a_missing_dataset_points_at_the_list(ws):
    out = server.profile_dataset(dataset_name="nope", workspace_id=ws)
    assert _next(out) == "NEXT STEP: call list_datasets()"


# --- N7: the ledger of a dataset that is not there -----------------------------------------


def test_n7_the_ledger_refuses_a_dataset_that_is_not_loaded(ws):
    out = server.get_cleaning_ledger(dataset_name="nope", workspace_id=ws)
    assert out.startswith("BLOCKED: no dataset called 'nope'") and "Loaded: sales" in out


def test_n7_an_uncleaned_dataset_names_itself_in_the_next_step(ws):
    out = server.get_cleaning_ledger(dataset_name="sales", workspace_id=ws)
    assert "Nothing has been cleaned for sales" in out
    assert 'propose_cleaning_plan(dataset_name="sales")' in out and '"..."' not in out


# --- N8: a heatmap only of one quantity, every cell in its own place -----------------------


def test_n8_a_heatmap_of_a_trend_is_refused(ws):
    out = server.render_chart(dataset_name="sales", analysis_type="trend", chart="heatmap",
                              measure="revenue", grain="month", workspace_id=ws)
    assert out.startswith("BLOCKED") and "one colour scale" in out
    assert _next(out) == ('NEXT STEP: call render_chart(dataset_name="sales", analysis_type="trend",'
                          ' chart="line", grain="month", measure="revenue", y="revenue (sum)")')


def test_n8_a_cohort_heatmap_keeps_every_cell(ws):
    out = server.render_chart(dataset_name="sales", analysis_type="cohort_retention",
                              chart="heatmap", entity="customer_id", workspace_id=ws)
    assert "Chart written:" in out
    table = server.compute_analysis(dataset_name="sales", analysis_type="cohort_retention",
                                    entity="customer_id", workspace_id=ws)
    path = next(ln.strip() for ln in table.splitlines() if ln.strip().endswith(".csv"))
    with open(path, newline="") as f:
        rows = list(csv.reader(f))[1:]
    cells = sum(1 for r in rows for v in r[1:] if v != "")
    assert f"{cells} of" in out or f"{cells:,} of" in out, out[:600]


def test_n8_a_cross_tab_heatmap_is_drawn(ws):
    out = server.render_chart(dataset_name="sales", analysis_type="cross_tab", chart="heatmap",
                              rows="channel", columns="region", measure="units",
                              workspace_id=ws)
    assert "Chart written:" in out, out[:400]


# --- N9: the measure the caller named is the caller's choice of y --------------------------


@pytest.mark.parametrize("analysis,kind,kw", [
    ("trend", "line", dict(measure="revenue", grain="month")),
    ("top_n", "bar", dict(dimension="region", measure="revenue")),
    ("period_compare", "bar", dict(measure="revenue", period="2024-12", baseline="2024-11",
                                   grain="month")),
])
def test_n9_one_column_carrying_the_named_measure_is_drawn_without_y(ws, analysis, kind, kw):
    out = server.render_chart(dataset_name="sales", analysis_type=analysis, chart=kind,
                              workspace_id=ws, **kw)
    assert "Chart written:" in out, out[:400]
    assert "revenue" in out.split("You cannot see this image", 1)[0]


def test_n9_a_real_choice_is_still_the_callers(ws):
    """ranking_shift offers revenue before and revenue after: P11-D13 stands."""
    out = server.render_chart(dataset_name="sales", analysis_type="ranking_shift", chart="bar",
                              dimension="region", measure="revenue",
                              before_start="2023-01-01", before_end="2023-12-31",
                              after_start="2024-01-01", after_end="2024-12-31", workspace_id=ws)
    assert out.startswith("BLOCKED") and "Name one with y" in out


# --- N11: key findings are findings --------------------------------------------------------


def test_n11_key_findings_skip_the_scope_line_and_list_a_repeat_once(ws):
    for _ in range(3):
        server.compute_analysis(dataset_name="sales", analysis_type="summary_stats",
                                workspace_id=ws)
    out = server.build_report(dataset_name="sales", question="q", workspace_id=ws)
    key = out.split("Key findings:", 1)[1].split("\n\n", 1)[0].strip().splitlines()
    stats = [k for k in key if k.strip().startswith("- summary_stats:")]
    assert len(stats) == 1
    assert "row(s) analysed" not in stats[0]


def test_n11_key_findings_are_capped_with_the_rest_counted(ws):
    for dim in ("region", "customer_id"):
        for n in range(1, 9):
            server.compute_analysis(dataset_name="sales", analysis_type="top_n", dimension=dim,
                                    measure="revenue", n=n, workspace_id=ws)
    out = server.build_report(dataset_name="sales", question="q", workspace_id=ws)
    key = out.split("Key findings:", 1)[1].split("\n\n", 1)[0].strip().splitlines()
    assert len(key) == 13 and "4 earlier finding(s)" in key[-1]


# --- N13: an offline machine is told exactly what to do ------------------------------------


def test_n13_a_named_extension_file_that_is_not_there_is_a_refusal(ws, monkeypatch, tmp_path):
    monkeypatch.setenv(postgres.EXTENSION_FILE_ENV, str(tmp_path / "missing.duckdb_extension"))
    import duckdb
    con = duckdb.connect()
    try:
        con.execute("LOAD postgres")
        pytest.skip("the postgres extension is cached here, so nothing is refused")
    except duckdb.Error:
        pass
    with pytest.raises(postgres.LoadRefused, match=postgres.EXTENSION_FILE_ENV):
        postgres._load_extension(con)


# --- N14: reset takes the workspace's contract exports with it -----------------------------


def test_n14_reset_workspace_removes_the_workspaces_exports(ws):
    exports = store.EXPORT_DIR / ws
    assert any(exports.rglob("*.yaml"))
    out = server.reset_workspace(confirm=True, workspace_id=ws)
    assert "exported contract file(s)" in out and not exports.exists()


def test_date_order_skips_a_time_of_day_column():
    """Stress round 4, 26/09/2026: M13 compared order_date with a TIME column and DuckDB refused."""
    import duckdb
    from backend.engine.contract.evidence import gather
    from backend.engine.contract.measured_caveats import _date_order
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT DATE '2024-01-01' + i::INTEGER AS d, TIME '10:00:00' AS tm "
                "FROM range(50) r(i)")
    assert _date_order(con, "t", gather(con, "t", probe_pairs=False)) == []


def test_a_time_of_day_is_not_a_date_but_a_timestamp_is():
    from backend.engine.contract.measured_caveats import _is_date
    assert _is_date("DATE") and _is_date("TIMESTAMP") and _is_date("DATETIME")
    assert not _is_date("TIME") and not _is_date("TIME WITH TIME ZONE")
