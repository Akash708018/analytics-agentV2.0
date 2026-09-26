"""
Tests for profile/tools.py -- the layer the three MCP tools call.

These assert on the STRINGS the agent will read, because that is the whole
interface. A tool that computes the right thing and says it unusably produces
F1, and a tool that hands back a path without saying what is in it produces F7.

Workspace-backed with the reset either side: profile_dataset writes, and the
results directory outlives the test.
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine import workspace
from backend.engine.contract.refusals import Reason, reason_of
from backend.engine.profile import tools
from backend.engine.util import results

WORKSPACE = "profile_tools_test"


@pytest.fixture
def ws():
    workspace.reset(WORKSPACE)
    try:
        yield WORKSPACE
    finally:
        workspace.reset(WORKSPACE)


@pytest.fixture
def con():
    c = duckdb.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def sales(con):
    con.execute(
        """
        CREATE TABLE sales AS SELECT
          'ORD-' || lpad(i::VARCHAR, 5, '0')                    AS order_id,
          CASE WHEN i % 40 = 0 THEN 'unknown'
               WHEN i % 17 = 0 THEN 'N/A'
               WHEN i % 3  = 0 THEN 'North' ELSE 'South' END    AS region,
          CASE WHEN i = 199 THEN 9999.0 ELSE ((i % 97) + 1) * 1.5 END AS revenue,
          DATE '2024-01-01' + ((i * 2)::INTEGER)                AS order_date
        FROM range(200) t(i)
        """
    )
    return con


# --------------------------------------------------------------------------
# profile_dataset
# --------------------------------------------------------------------------

def test_a_profile_comes_back_as_findings_not_an_object(ws, sales):
    text = tools.profile_dataset(sales, ws, "sales")
    assert "Profile of sales" in text
    assert "What this shows:" in text


def test_a_profile_writes_its_table_and_names_the_path(ws, sales):
    text = tools.profile_dataset(sales, ws, "sales")
    written = results.list_results(ws)
    assert len(written) == 1
    assert str(written[0]) in text
    assert "read_result_file(" in text


def test_an_unloaded_dataset_is_refused_as_text_not_raised(ws, sales):
    """
    A raised exception inside a FastMCP tool becomes a traceback, and a
    traceback is not an instruction: the agent reads it, learns nothing it can
    act on, and retries the same call.
    """
    text = tools.profile_dataset(sales, ws, "nope")
    assert reason_of(text) is Reason.DATASET_NOT_LOADED
    assert "NEXT STEP" in text


def test_a_refused_profile_writes_no_file(ws, sales):
    tools.profile_dataset(sales, ws, "nope")
    assert results.list_results(ws) == []


def test_a_successful_profile_carries_no_reason_code(ws, sales):
    """It is an answer. A reason code would send the agent looking for
    something to fix."""
    assert reason_of(tools.profile_dataset(sales, ws, "sales")) is None


def test_the_vocabulary_can_be_widened_for_one_call(ws, sales):
    """
    Frictionless' model: the list is a property of the data, not the tool. A
    feed that writes 'unknown' for absence is not a reason to change the
    default for every other source.
    """
    default = tools.profile_dataset(sales, ws, "sales")
    assert "region (11)" in default

    widened = tools.profile_dataset(
        sales, ws, "sales", missing_values=list(["N/A", "unknown"])
    )
    assert "region (16)" in widened


def test_an_empty_vocabulary_switches_detection_off(ws, sales):
    text = tools.profile_dataset(sales, ws, "sales", missing_values=[])
    assert "read as missing without being null" not in text


# --------------------------------------------------------------------------
# profile_column
# --------------------------------------------------------------------------

def test_a_column_comes_back_in_detail(ws, sales):
    text = tools.profile_column(sales, ws, "sales", "region")
    assert text.startswith("sales.region")
    assert "Most frequent value(s)" in text


def test_a_column_profile_writes_nothing(ws, sales):
    tools.profile_column(sales, ws, "sales", "revenue")
    assert results.list_results(ws) == []


def test_an_unknown_column_is_refused_as_text(ws, sales):
    text = tools.profile_column(sales, ws, "sales", "regionn")
    assert reason_of(text) is Reason.COLUMN_NOT_FOUND
    assert "region" in text


def test_an_unloaded_dataset_is_refused_by_the_column_tool_too(ws, sales):
    text = tools.profile_column(sales, ws, "nope", "region")
    assert reason_of(text) is Reason.DATASET_NOT_LOADED


def test_a_wider_value_list_can_be_asked_for(ws, sales):
    narrow = tools.profile_column(sales, ws, "sales", "revenue")
    wide = tools.profile_column(sales, ws, "sales", "revenue", top_n=50)
    assert "10 of 98 distinct" in narrow
    assert "50 of 98 distinct" in wide


def test_the_two_tools_report_the_same_numbers(ws, sales):
    """
    The reason profile_column computes the table profile once and hands it
    down. Two views of one column disagreeing inside a single conversation is
    unrecoverable for a reader -- neither number can be trusted after that.
    """
    table = tools.profile_dataset(sales, ws, "sales")
    column = tools.profile_column(sales, ws, "sales", "region")
    assert "N/A x11" in table
    assert "N/A x11" in column


# --------------------------------------------------------------------------
# read_result
# --------------------------------------------------------------------------

def test_a_written_profile_can_be_read_back(ws, sales):
    """
    The round trip the Done-When does not name: every path a tool returns must
    open here, in the same run. A well-formed envelope pointing at nothing is
    F7 with a clean conscience.
    """
    tools.profile_dataset(sales, ws, "sales")
    path = str(results.list_results(ws)[0])
    page = tools.read_result(ws, path)
    assert reason_of(page) is None
    assert "rows 1 to 4 of 4" in page
    assert "order_id" in page


def test_the_paging_call_in_the_envelope_actually_works(ws, con):
    """
    Not just that A path opens, but that THE CALL PRINTED IN THE ENVELOPE
    opens. Those drift apart when one of them is built by hand.
    """
    cols = ", ".join(f"(i % {c + 2})::INTEGER AS col_{c}" for c in range(60))
    con.execute(f"CREATE TABLE wide AS SELECT {cols} FROM range(500) t(i)")
    text = tools.profile_dataset(con, ws, "wide")

    call = next(l.strip() for l in text.splitlines() if "read_result_file(" in l)
    path = call.split('path="', 1)[1].split('"', 1)[0]
    start = int(call.split("start=", 1)[1].split(",", 1)[0])
    page = tools.read_result(ws, path, start=start)
    assert reason_of(page) is None
    assert f"rows {start:,} to" in page


def test_a_path_outside_the_workspace_is_refused(ws):
    text = tools.read_result(ws, "/etc/passwd")
    assert reason_of(text) is Reason.RESULT_OUT_OF_SCOPE


def test_a_missing_file_is_refused_with_what_exists(ws, sales):
    tools.profile_dataset(sales, ws, "sales")
    text = tools.read_result(ws, str(results.results_dir(ws) / "nope.csv"))
    assert reason_of(text) is Reason.RESULT_NOT_FOUND
    assert "profile_sales_" in text


def test_reading_past_the_end_is_not_a_refusal(ws, sales):
    tools.profile_dataset(sales, ws, "sales")
    text = tools.read_result(ws, str(results.list_results(ws)[0]), start=500)
    assert reason_of(text) is None
    assert "past the end" in text


# --------------------------------------------------------------------------
# the shape of the three together
# --------------------------------------------------------------------------

def test_every_tool_takes_the_connection_and_workspace_the_same_way(ws, sales):
    """
    profile_column does not use workspace_id and takes it anyway. A signature
    that varies by tool is a thing the agent has to remember rather than
    pattern-match, and remembering is where it invents.
    """
    import inspect

    for fn in (tools.profile_dataset, tools.profile_column):
        params = list(inspect.signature(fn).parameters)
        assert params[:3] == ["con", "workspace_id", "dataset_name"]


def test_the_whole_drill_down_end_to_end(ws, sales):
    """
    Profile the table, spot the column with something odd in it, look at it,
    then read the full table of counts. Four calls, and the shape a live run
    has to reproduce.
    """
    table = tools.profile_dataset(sales, ws, "sales")
    assert "read as missing without being null: region (11)" in table

    column = tools.profile_column(sales, ws, "sales", "region")
    assert "unknown" in column
    assert "values that MEAN absent" in column

    page = tools.read_result(ws, str(results.list_results(ws)[0]))
    assert "region" in page
    assert reason_of(page) is None
