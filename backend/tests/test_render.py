"""
Unit tests for profile/render.py.

Workspace-backed, reset either side, because this is the first profiling module
that writes: the results directory is real and it outlives the test.

The invariant these exist to defend is one line long -- wherever a path appears,
the shape, the findings and the next call appear with it -- and it is asserted
against the OUTPUT rather than against which function produced it. F7 does not
care how the string was assembled.
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine import workspace
from backend.engine.contract.refusals import reason_of
from backend.engine.profile.render import INLINE_COLUMN_LIMIT, render_profile
from backend.engine.profile.table_profile import TableProfile, profile_table
from backend.engine.util import results

WORKSPACE = "render_test"


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
def narrow(con):
    con.execute(
        """
        CREATE TABLE sales AS SELECT
          'ORD-' || lpad(i::VARCHAR, 5, '0')                       AS order_id,
          ((i % 7) + 1)::INTEGER                                   AS units,
          CASE WHEN i % 40 = 0 THEN 'N/A' ELSE 'North' END         AS region,
          CASE WHEN i = 199 THEN 99999.0 ELSE ((i % 97) + 1) * 1.5 END AS revenue
        FROM range(200) t(i)
        """
    )
    return con


@pytest.fixture
def wide(con):
    cols = ", ".join(f"(i % {c + 2})::INTEGER AS col_{c}" for c in range(60))
    con.execute(f"CREATE TABLE wide AS SELECT {cols} FROM range(1000) t(i)")
    return con


def _rendered(ws, con, name):
    return render_profile(ws, profile_table(con, name))


# --------------------------------------------------------------------------
# the file is always written
# --------------------------------------------------------------------------

def test_a_narrow_profile_still_writes_a_file(ws, narrow):
    """
    The tempting design was to skip the file for a small table. It makes the
    agent reason about which case it is in, and an agent that has learned a
    profile comes with a path produces one when it is missing.
    """
    _rendered(ws, narrow, "sales")
    assert len(results.list_results(ws)) == 1


def test_a_wide_profile_writes_a_file(ws, wide):
    _rendered(ws, wide, "wide")
    assert len(results.list_results(ws)) == 1


def test_the_file_is_named_after_the_dataset(ws, narrow):
    _rendered(ws, narrow, "sales")
    assert results.list_results(ws)[0].name.startswith("profile_sales_")


def test_the_file_holds_one_row_per_column(ws, narrow):
    text = _rendered(ws, narrow, "sales")
    path = results.list_results(ws)[0]
    page = results.read_result_file(ws, str(path), start=1, limit=50)
    assert "rows 1 to 4 of 4" in page
    assert "order_id" in page and "revenue" in page


def test_two_profiles_of_one_dataset_do_not_overwrite(ws, narrow):
    _rendered(ws, narrow, "sales")
    _rendered(ws, narrow, "sales")
    assert len(results.list_results(ws)) == 2


# --------------------------------------------------------------------------
# the invariant -- a path never travels alone
# --------------------------------------------------------------------------

def _asserts_envelope(text, profile, ws):
    path = str(results.list_results(ws)[0])
    assert path in text, "the path is missing"
    assert f"{profile.row_count:,} rows" in text, "the shape is missing"
    assert "read_result_file(" in text, "no call to read the rest"
    first = profile.summary_lines()[0]
    assert first in text, "the findings are missing"


def test_a_narrow_render_carries_the_whole_envelope(ws, narrow):
    p = profile_table(narrow, "sales")
    _asserts_envelope(render_profile(ws, p), p, ws)


def test_a_wide_render_carries_the_whole_envelope(ws, wide):
    p = profile_table(wide, "wide")
    _asserts_envelope(render_profile(ws, p), p, ws)


def test_the_render_is_never_a_bare_path(ws, narrow):
    text = _rendered(ws, narrow, "sales")
    assert len(text.splitlines()) > 10
    assert not text.strip().startswith("/")


def test_nothing_here_reads_as_a_refusal(ws, narrow):
    """A profile is an answer. A reason code on it would send the agent
    looking for something to fix."""
    assert reason_of(_rendered(ws, narrow, "sales")) is None


# --------------------------------------------------------------------------
# narrow: sentences, because they say more per line than the table does
# --------------------------------------------------------------------------

def test_a_narrow_profile_describes_every_column_in_prose(ws, narrow):
    text = _rendered(ws, narrow, "sales")
    assert "Columns:" in text
    for name in ("order_id", "units", "region", "revenue"):
        assert f"  - {name} (" in text


def test_the_narrow_form_leads_with_the_findings(ws, narrow):
    text = _rendered(ws, narrow, "sales")
    assert text.index("What this shows:") < text.index("Columns:")


def test_the_narrow_form_does_not_also_print_the_table(ws, narrow):
    """
    Sentences and a 12-field preview of the same four columns is the Phase 3
    Step 8 shape: two renderings of one thing in one result, and a reader who
    cannot tell whether they are different.
    """
    text = _rendered(ws, narrow, "sales")
    assert "| column | dtype |" not in text
    assert text.count("order_id") < 4


def test_a_column_at_the_limit_still_gets_sentences(ws, con):
    cols = ", ".join(f"i AS col_{c}" for c in range(INLINE_COLUMN_LIMIT))
    con.execute(f"CREATE TABLE edge AS SELECT {cols} FROM range(10) t(i)")
    text = render_profile(ws, profile_table(con, "edge"))
    assert "Columns:" in text
    assert "too many to describe" not in text


# --------------------------------------------------------------------------
# wide: the table's own preview, with both truncations stated
# --------------------------------------------------------------------------

def test_a_wide_profile_switches_to_the_table(ws, wide):
    text = _rendered(ws, wide, "wide")
    assert "too many to describe one line each" in text
    assert "Columns:" not in text


def test_the_wide_form_states_both_truncations(ws, wide):
    text = _rendered(ws, wide, "wide")
    assert "First 20 of 60 rows:" in text
    assert f"Showing 12 of {len(TableProfile.HEADERS)} columns" in text


def test_the_wide_form_says_how_much_was_not_shown(ws, wide):
    text = _rendered(ws, wide, "wide")
    assert "40 more rows" in text
    assert "NOT shown above" in text


def test_the_wide_form_ends_in_the_call_for_the_rest(ws, wide):
    text = _rendered(ws, wide, "wide")
    path = str(results.list_results(ws)[0])
    assert f'read_result_file(path="{path}", start=21' in text


def test_one_column_past_the_limit_switches_form(ws, con):
    cols = ", ".join(f"i AS col_{c}" for c in range(INLINE_COLUMN_LIMIT + 1))
    con.execute(f"CREATE TABLE over AS SELECT {cols} FROM range(10) t(i)")
    text = render_profile(ws, profile_table(con, "over"))
    assert "too many to describe" in text
    assert "Columns:" not in text


def test_the_limit_is_the_previews_own_cap(ws):
    """
    Not a second constant chosen separately. A threshold above PREVIEW_ROWS
    would promise a sentence for every column while the preview had already
    truncated the rows those sentences describe.
    """
    assert INLINE_COLUMN_LIMIT == results.PREVIEW_ROWS


def test_the_limit_can_be_overridden(ws, narrow):
    text = render_profile(ws, profile_table(narrow, "sales"), inline_column_limit=2)
    assert "too many to describe" in text


# --------------------------------------------------------------------------
# the findings survive the trip
# --------------------------------------------------------------------------

def test_the_summary_reaches_both_forms(ws, narrow, wide):
    narrow_text = render_profile(ws, profile_table(narrow, "sales"))
    assert "read as missing without being null: region" in narrow_text

    wide_text = render_profile(ws, profile_table(wide, "wide"))
    assert "No exact duplicate rows" in wide_text


def test_the_notes_reach_both_forms(ws, narrow, wide):
    narrow_p = profile_table(narrow, "sales")
    assert narrow_p.notes
    assert "Notes:" in render_profile(ws, narrow_p)

    wide_p = profile_table(wide, "wide")
    text = render_profile(ws, wide_p)
    assert ("Notes:" in text) == bool(wide_p.notes)


def test_the_notes_come_last(ws, narrow):
    """
    Whatever sits at the bottom is what gets read last and remembered. The
    caveats belong there rather than above the numbers they qualify.
    """
    text = _rendered(ws, narrow, "sales")
    assert text.index("Notes:") > text.index("read_result_file(")


def test_an_empty_table_renders_without_crashing(ws, con):
    con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
    text = render_profile(ws, profile_table(con, "t"))
    assert "0 rows, 2 columns" in text
    assert "read_result_file(" in text
