"""The report: nine sections, and what each says when it has nothing to say.

The assertion this file exists for is that a section with no record still appears. A report that
drops "cleaning ledger" because nothing was cleaned reads as a report of a dataset that needed
no cleaning, and those are different claims.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.analysis import runs as analysis_runs  # noqa: E402
from backend.engine.contract import store  # noqa: E402
from backend.engine.contract.dataset_contract import (  # noqa: E402
    AnalysisWindow,
    Binding,
    DatasetContract,
    Measure,
)
from backend.engine.report import assemble  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "report_test"
DATASET = "sales"
T1 = datetime(2026, 9, 21, 12, 0, 0)
T2 = datetime(2026, 9, 21, 12, 5, 0)

ROWS = """SELECT * FROM (VALUES
  ('o1', DATE '2024-02-01', 'north', 10.0::DOUBLE),
  ('o2', DATE '2024-03-01', 'south', 20.0::DOUBLE),
  ('o3', DATE '2024-04-01', 'north', 30.0::DOUBLE)
) v(order_id, order_date, region, revenue)"""


@pytest.fixture()
def con():
    workspace.reset(WORKSPACE)
    c = db.connect(WORKSPACE)
    c.execute(f'CREATE TABLE "{DATASET}" AS {ROWS}')
    try:
        yield c
    finally:
        c.close()
        workspace.reset(WORKSPACE)


def confirm(con):
    pairs = [
        (r[0], r[1])
        for r in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position", [DATASET]).fetchall()
    ]
    return store.confirm(con, DatasetContract(
        dataset_name=DATASET,
        grain="one row = one order",
        primary_key=["order_id"],
        date_column="order_date",
        analysis_window=AnalysisWindow(start=date(2024, 1, 1), end=date(2024, 12, 31)),
        measures=[Measure(name="revenue", agg="sum", definition="what it came to", unit="GBP")],
        dimensions=["region"],
        bound_to=Binding.from_pairs(pairs, 3),
    ))


def build(con, question="Which region earns most?", **kw):
    return assemble.assemble(con, WORKSPACE, dataset_name=DATASET, question=question, **kw)


def headings_in(path: Path) -> list[str]:
    return [ln[3:] for ln in path.read_text().splitlines()
            if ln.startswith("## ") and ln != "## Contents"]


# --- the nine sections ---------------------------------------------------------------------

def test_all_nine_sections_are_present_in_the_guides_order(con):
    report = build(con, now=T1)
    assert headings_in(report.path) == list(assemble.SECTIONS)
    assert len(assemble.SECTIONS) == 9


def test_a_dataset_with_no_records_at_all_still_gets_nine_sections(con):
    """The whole point. Nothing has been profiled, cleaned, validated or analysed here."""
    report = build(con, now=T1)
    assert headings_in(report.path) == list(assemble.SECTIONS)
    for name in ("Data quality", "Cleaning ledger", "Validation results", "Findings",
                 "Method notes", "Reproduction appendix"):
        assert name in report.empty_sections, f"{name} should be reported empty"


def test_an_empty_section_says_why_rather_than_sitting_blank(con):
    text = build(con, now=T1).path.read_text()
    assert "has not been profiled" in text
    assert "Nothing has been cleaned" in text
    assert "has not been validated" in text
    assert "No analysis has been run" in text


def test_the_question_is_carried_into_the_report(con):
    report = build(con, question="Which region earns most?", now=T1)
    assert "Which region earns most?" in report.path.read_text()


def test_no_question_is_itself_reported(con):
    """P12-D2: nothing records why a dataset was loaded, so a blank question is a gap and not
    a default."""
    report = build(con, question="   ", now=T1)
    assert "The question asked" in report.empty_sections
    assert "No question was recorded" in report.path.read_text()


def test_the_grain_comes_from_the_confirmed_contract(con):
    confirm(con)
    text = build(con, now=T1).path.read_text()
    assert "one row = one order" in text
    assert "`revenue` (sum), GBP" in text
    assert "contract v1" in text


def test_with_no_contract_the_grain_section_says_so(con):
    report = build(con, now=T1)
    assert "Dataset and grain" in report.empty_sections
    assert "No contract has been confirmed" in report.path.read_text()


# --- findings and the appendix ---------------------------------------------------------------

def add_run(con, **kw):
    kw.setdefault("dataset_name", DATASET)
    kw.setdefault("analysis_type", "top_n")
    kw.setdefault("now", T1)
    return analysis_runs.record(con, **kw)


def test_the_appendix_lists_one_retypeable_call_per_run_in_order(con):
    add_run(con, analysis_type="summary_stats", params={}, now=T1)
    add_run(con, analysis_type="top_n",
            params={"dimension": "region", "measure": "revenue", "n": 3}, now=T2)
    text = build(con, now=T2).path.read_text()
    first = 'compute_analysis(dataset_name="sales", analysis_type="summary_stats")'
    second = ('compute_analysis(dataset_name="sales", analysis_type="top_n", '
              'dimension="region", measure="revenue", n=3)')
    assert first in text and second in text
    assert text.index(first) < text.index(second), "the appendix is not in the order things ran"


def test_a_chart_is_named_beside_the_analysis_that_drew_it(con):
    """The link nothing had before analysis/runs.py: a PNG's filename carries a label and a
    timestamp, and two charts of one analysis are indistinguishable by it."""
    add_run(con, analysis_type="frequency", chart_kind="bar",
            chart_path=f"/w/{WORKSPACE}/charts/frequency_x.png", params={"column": "region"})
    report = build(con, now=T1)
    text = report.path.read_text()
    assert report.chart_count == 1
    assert "Chart (bar): `frequency_x.png`" in text
    assert 'render_chart(dataset_name="sales"' in text


def test_the_method_note_reaches_the_method_notes_section(con):
    add_run(con, summary=["3 of 3 row(s) analysed.", "Something else."])
    text = build(con, now=T1).path.read_text()
    assert "`top_n`: 3 of 3 row(s) analysed." in text


def test_a_repeated_method_note_is_listed_once(con):
    add_run(con, summary=["3 of 3 row(s) analysed."], now=T1)
    add_run(con, summary=["3 of 3 row(s) analysed."], now=T2)
    text = build(con, now=T2).path.read_text()
    assert text.count("`top_n`: 3 of 3 row(s) analysed.") == 1


# --- Rule 4 ----------------------------------------------------------------------------------

def test_the_reply_carries_the_contents_and_the_key_findings(con):
    add_run(con, summary=["3 of 3 row(s) analysed."])
    text = build(con, now=T1).to_text()
    assert "Report written:" in text
    assert "Contents:" in text
    for name in assemble.SECTIONS:
        assert name in text
    assert "Key findings:" in text
    assert "top_n: 3 of 3 row(s) analysed." in text


def test_the_reply_names_the_sections_that_had_nothing_to_report(con):
    text = build(con, now=T1).to_text()
    assert "Sections present with nothing to report" in text
    assert "Cleaning ledger" in text


def test_a_report_lands_in_the_workspace_reports_directory(con):
    report = build(con, now=T1)
    assert report.path.parent == assemble.reports_dir(WORKSPACE)
    assert report.path.parent.name == "reports"
    assert report.path.suffix == ".md"


def test_two_reports_in_one_second_do_not_share_a_name(con):
    a = build(con, now=T1)
    b = build(con, now=T1)
    assert a.path != b.path
    assert a.path.exists() and b.path.exists()


def test_there_is_no_accessor_that_returns_a_bare_path():
    """The promise Result and Chart both make, for Rule 4's reason."""
    names = [n for n in dir(assemble.Report) if not n.startswith("_")]
    assert "to_text" in names
    assert not any(n in names for n in ("path_text", "as_path", "filename", "location"))
