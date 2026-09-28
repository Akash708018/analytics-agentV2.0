"""A narrowed Postgres load says it is narrowed, everywhere its numbers go (C95).

Measured before it was fixed: geolocation loaded with limit=1000 -- 1,000 of 1,000,163 rows, all
from one state -- produced a report reading "1,000 of 1,000 row(s) analysed" and "1 distinct
value(s) of geolocation_state", with nothing anywhere saying the table was a subset. The limit
was recorded in _agent_datasets.notes and read by nothing.

These tests need no Postgres: the clean_sales fixture is loaded through the real CSV path and
its load record is then rewritten as the Postgres loader writes one, which is the only thing
the disclosure reads.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.contract import store  # noqa: E402
from backend.engine.ingest.csv_loader import load_csv  # noqa: E402
from backend.engine.state import require_contract  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "narrowed_test"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "clean_sales.csv"
SOURCE = "olist:public.sales"


def record(notes: str, rows: int = 500) -> db.DatasetRecord:
    return db.DatasetRecord(
        dataset_name="sales", source_type="postgres", source_detail=SOURCE,
        row_count=rows, column_count=8, loaded_at=_dt.datetime.now(), notes=notes)


# --- the sentence -----------------------------------------------------------------------------

def test_a_limit_names_both_counts_and_that_it_is_not_a_sample():
    text = record("where=-, limit=500, source_rows=1000163").narrowing()
    assert "500 of 1,000,163 rows" in text
    assert SOURCE in text and "limit=500" in text
    assert "not a random sample" in text


def test_a_where_names_its_filter_and_claims_no_ordering():
    text = record("where=state = 'SP', limit=-, source_rows=1000163").narrowing()
    assert "500 of 1,000,163 rows" in text
    assert "where=state = 'SP'" in text
    assert "random" not in text


def test_a_where_holding_a_comma_still_parses():
    text = record("where=state IN ('SP', 'RJ'), limit=100, source_rows=9000").narrowing()
    assert "where=state IN ('SP', 'RJ')" in text and "limit=100" in text


def test_a_limit_the_table_never_reached_is_not_a_narrowing():
    assert record("where=-, limit=5000, source_rows=500").narrowing() is None


def test_an_unfiltered_load_is_not_a_narrowing():
    assert record("where=-, limit=-, source_rows=500").narrowing() is None


def test_a_record_written_before_source_rows_still_discloses():
    text = record("where=-, limit=1000", rows=1000).narrowing()
    assert "1,000 rows" in text and "limit=1000" in text
    assert "not recorded" in text


def test_a_csv_load_is_never_a_narrowing():
    r = dataclasses.replace(record("header_rows=1, footer_skip_rows=0, on_error=abort"),
                            source_type="csv")
    assert r.narrowing() is None


# --- where it reaches -------------------------------------------------------------------------

@pytest.fixture()
def narrowed(monkeypatch):
    workspace.reset(WORKSPACE)
    monkeypatch.setattr(store, "EXPORT_DIR", workspace.workspace_dir(WORKSPACE) / "contracts")
    con = db.connect(WORKSPACE)
    try:
        load_csv(con, str(FIXTURE), "sales")
        db.register_dataset(con, dataset_name="sales", source_type="postgres",
                            source_detail=SOURCE, row_count=500, column_count=8,
                            notes="where=-, limit=500, source_rows=1000163")
    finally:
        con.close()
    proposal = server.propose_dataset_contract(
        dataset_name="sales", grain="one row = one order", primary_key=["order_id"],
        date_column="order_date", measures=["revenue"], dimensions=["region"],
        aggregations={"revenue": "sum"}, measure_definitions={"revenue": "order value"},
        analysis_window_start="2024-01-01", analysis_window_end="2024-12-31",
        workspace_id=WORKSPACE)
    body = proposal.rsplit("```", 2)[-2]
    body = body[4:] if body.startswith("json") else body
    confirmed = server.confirm_dataset_contract(contract_json=body, workspace_id=WORKSPACE)
    assert "version 1" in confirmed, confirmed
    yield
    workspace.reset(WORKSPACE)


def test_the_gate_carries_it_first(narrowed):
    con = db.connect(WORKSPACE)
    try:
        caveats = require_contract(con, "sales").caveats
    finally:
        con.close()
    assert caveats and "500 of 1,000,163 rows" in caveats[0]


def test_an_analysis_says_it(narrowed):
    text = server.compute_analysis(dataset_name="sales", analysis_type="frequency",
                                   column="region", workspace_id=WORKSPACE)
    assert "500 of 1,000,163 rows" in text


def test_a_chart_says_it(narrowed):
    text = server.render_chart(dataset_name="sales", analysis_type="frequency", chart="bar",
                               column="region", y="rows", workspace_id=WORKSPACE)
    assert "500 of 1,000,163 rows" in text


def test_the_report_leads_its_caveats_with_it(narrowed):
    server.compute_analysis(dataset_name="sales", analysis_type="frequency", column="region",
                            workspace_id=WORKSPACE)
    reply = server.build_report(dataset_name="sales", question="q", workspace_id=WORKSPACE)
    path = next(ln.split(": ", 1)[1].strip() for ln in reply.splitlines()
                if ln.startswith("Report written: "))
    body = Path(path).read_text(encoding="utf-8")
    caveats = body.split("## Caveats and exclusions", 1)[1].split("\n## ", 1)[0]
    assert caveats.strip().splitlines()[0] == "**Loaded as a subset**"
    assert "500 of 1,000,163 rows" in caveats


def test_workflow_state_says_it(narrowed):
    text = server.get_workflow_state(workspace_id=WORKSPACE)
    assert "500 of 1,000,163 rows" in text
