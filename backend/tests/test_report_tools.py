"""build_report's caller-facing behaviour: what it refuses, and what it does not.

The decision under test is that a dataset with no contract is reported rather than refused.
validate_dataset refuses that case and is right to; a report is the opposite, because the
absence of an agreed grain is the most important thing a reader could be told.
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
from backend.engine.contract.refusals import Reason, reason_of  # noqa: E402
from backend.engine.report import tools as report_tools  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "report_tools_test"
DATASET = "sales"
T1 = datetime(2026, 9, 21, 12, 0, 0)

ROWS = """SELECT * FROM (VALUES
  ('o1', DATE '2024-02-01', 'north', 10.0::DOUBLE),
  ('o2', DATE '2024-03-01', 'south', 20.0::DOUBLE)
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
        bound_to=Binding.from_pairs(pairs, 2),
    ))


def build(con, dataset_name=DATASET, question="Which region earns most?"):
    return report_tools.build_report(con, WORKSPACE, dataset_name, question, now=T1)


def test_a_dataset_that_is_not_loaded_is_refused_and_told_what_is(con):
    text = build(con, dataset_name="nowhere")
    assert reason_of(text) is Reason.DATASET_NOT_LOADED
    assert "sales" in text
    assert "list_datasets()" in text


def test_a_dataset_with_no_contract_is_reported_rather_than_refused(con):
    """The decision this file exists for. validate_dataset refuses this case; a report must not,
    because 'no grain was agreed' is the finding."""
    text = build(con)
    assert reason_of(text) is None
    assert "Report written:" in text
    assert "Dataset and grain" in text


def test_the_missing_contract_is_named_in_the_report_itself(con):
    text = build(con)
    path = Path(text.splitlines()[0].split("Report written: ", 1)[1].strip())
    assert "No contract has been confirmed" in path.read_text()


def test_a_contract_is_used_when_there_is_one(con):
    confirm(con)
    text = build(con)
    path = Path(text.splitlines()[0].split("Report written: ", 1)[1].strip())
    body = path.read_text()
    assert "one row = one order" in body
    assert "Dataset and grain" not in text.split("nothing to report")[-1]


def test_the_envelope_carries_the_contents_and_the_findings(con):
    analysis_runs.record(con, dataset_name=DATASET, analysis_type="top_n",
                         params={"dimension": "region", "measure": "revenue"},
                         summary=["2 of 2 row(s) analysed."], now=T1)
    text = build(con)
    assert "Contents:" in text
    assert "Reproduction appendix" in text
    assert "Key findings:" in text
    assert "top_n: 2 of 2 row(s) analysed." in text


def test_the_question_reaches_the_report(con):
    text = build(con, question="Why did March fall?")
    path = Path(text.splitlines()[0].split("Report written: ", 1)[1].strip())
    assert "Why did March fall?" in path.read_text()


def test_building_twice_writes_two_reports(con):
    """It computes nothing, so running it again is cheap and must not overwrite the first."""
    a = Path(build(con).splitlines()[0].split("Report written: ", 1)[1].strip())
    b = Path(build(con).splitlines()[0].split("Report written: ", 1)[1].strip())
    assert a != b and a.exists() and b.exists()
