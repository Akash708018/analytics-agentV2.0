"""The run record, which is what makes a reproduction appendix possible.

P12-D3 measured what a written result keeps: its table, and nothing about the call that made it.
These tests are mostly about `call()`, because an appendix line nobody can retype is the same
failure as no appendix.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import runs  # noqa: E402
from backend.engine.profile.runs import is_bookkeeping  # noqa: E402

T1 = datetime(2026, 9, 21, 12, 0, 0)
T2 = datetime(2026, 9, 21, 12, 5, 0)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def add(con, **kw):
    kw.setdefault("dataset_name", "sales")
    kw.setdefault("analysis_type", "top_n")
    kw.setdefault("now", T1)
    return runs.record(con, **kw)


# --- the table -----------------------------------------------------------------------------

def test_the_table_is_bookkeeping_so_it_is_not_a_dataset():
    """state.py:197 filters an underscore prefix out of the dataset list. A run log offered as
    a dataset, and then reported as having no contract, is the absurdity profile/runs.py's own
    comment warns about."""
    assert runs.ANALYSIS_TABLE.startswith("_agent_")
    assert is_bookkeeping(runs.ANALYSIS_TABLE)


def test_reading_an_empty_log_gives_nothing_rather_than_raising(con):
    assert runs.history(con, "sales") == []
    assert runs.latest(con, "sales") is None
    assert runs.count(con) == 0


def test_runs_come_back_oldest_first(con):
    """Unlike results.list_results, which is newest first. This is read as a sequence of steps
    somebody repeats, and steps have an order."""
    add(con, analysis_type="summary_stats", now=T1)
    add(con, analysis_type="top_n", now=T2)
    assert [r.analysis_type for r in runs.history(con, "sales")] == ["summary_stats", "top_n"]
    assert runs.latest(con, "sales").analysis_type == "top_n"


def test_a_run_is_scoped_to_its_dataset(con):
    add(con, dataset_name="sales")
    add(con, dataset_name="other")
    assert len(runs.history(con, "sales")) == 1
    assert runs.count(con) == 2
    assert runs.count(con, "sales") == 1


# --- the call renderer ---------------------------------------------------------------------

def test_a_call_can_be_retyped(con):
    add(con, analysis_type="top_n",
        params={"dimension": "region", "measure": "revenue", "n": 3})
    assert runs.latest(con, "sales").call() == (
        'compute_analysis(dataset_name="sales", analysis_type="top_n", '
        'dimension="region", measure="revenue", n=3)'
    )


def test_a_chart_run_renders_render_chart_with_its_kind(con):
    add(con, analysis_type="frequency", chart_kind="line", chart_path="/w/charts/f.png",
        params={"column": "region", "y": "rows"})
    run = runs.latest(con, "sales")
    assert run.tool == "render_chart"
    assert run.call() == (
        'render_chart(dataset_name="sales", analysis_type="frequency", chart="line", '
        'column="region", y="rows")'
    )


def test_parameters_keep_their_types_through_the_round_trip(con):
    add(con, analysis_type="sample_adequacy",
        params={"dimension": "region", "power": 0.8, "alpha": 0.05, "n": 3})
    p = runs.latest(con, "sales").params
    assert p == {"dimension": "region", "power": 0.8, "alpha": 0.05, "n": 3}
    assert isinstance(p["power"], float) and isinstance(p["n"], int)
    assert "power=0.8" in runs.latest(con, "sales").call()


def test_parameters_are_rendered_in_a_stable_order(con):
    """Two runs of the same call must produce the same appendix line, or a reader comparing two
    reports sees a difference that is not one."""
    add(con, params={"measure": "revenue", "dimension": "region"})
    add(con, params={"dimension": "region", "measure": "revenue"}, now=T2)
    a, b = runs.history(con, "sales")
    assert a.call() == b.call()


def test_a_quote_in_a_value_does_not_break_the_line(con):
    add(con, params={"column": 'od"d'})
    assert runs.latest(con, "sales").call().endswith(r'column="od\"d")')


def test_a_run_with_no_parameters_still_renders(con):
    add(con, analysis_type="summary_stats", params={})
    assert runs.latest(con, "sales").call() == (
        'compute_analysis(dataset_name="sales", analysis_type="summary_stats")'
    )


# --- what a report reads -------------------------------------------------------------------

def test_the_method_note_survives_the_call_that_produced_it(con):
    """P12-D3: it did not, before this. Every analysis leads its summary with it and tools.py
    refuses a result that does not, so the first line is it."""
    add(con, summary=["500 of 500 row(s) analysed.", "Something else."])
    assert runs.latest(con, "sales").method_note() == "500 of 500 row(s) analysed."


def test_a_run_with_no_summary_has_no_method_note(con):
    add(con, summary=[])
    assert runs.latest(con, "sales").method_note() is None


def test_charts_are_the_runs_that_drew_something(con):
    """The link a PNG otherwise does not have. 'Findings with charts' needs to know which
    analysis produced which image, and the filename does not say."""
    add(con, analysis_type="top_n", result_path="/w/results/t.csv")
    add(con, analysis_type="frequency", chart_kind="bar", chart_path="/w/charts/f.png", now=T2)
    drew = runs.charts(con, "sales")
    assert [r.analysis_type for r in drew] == ["frequency"]
    assert drew[0].drew is True
    assert runs.history(con, "sales")[0].drew is False


def test_all_runs_spans_datasets_in_order(con):
    add(con, dataset_name="a", now=T1)
    add(con, dataset_name="b", now=T2)
    assert [r.dataset_name for r in runs.all_runs(con)] == ["a", "b"]


def test_the_contract_version_is_kept_beside_the_call(con):
    """An appendix line that reproduces nothing because the contract has moved is worse than
    none. The version says which contract the numbers were computed under."""
    add(con, contract_version=2)
    assert runs.latest(con, "sales").contract_version == 2
