"""
Unit tests for util/results.py.

Workspace-backed, reset either side, for the reason test_state.py documents:
the workspace directory is a real directory on disk and it outlives the test.
Without the reset, `list_results` in one test sees what another wrote, and the
failure lands on whichever test happens to count files rather than on the one
that leaked.

What is being defended here is F7 -- the agent gets a path, never opens it, and
reports on data it did not see. Most of these assert on the STRING a tool
returns, because that string is the entire defence.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from backend.engine import workspace
from backend.engine.contract.refusals import Reason, reason_of
from backend.engine.util import results

WORKSPACE = "results_test"
T1 = datetime(2026, 9, 2, 11, 30, 0)


@pytest.fixture
def ws():
    workspace.reset(WORKSPACE)
    try:
        yield WORKSPACE
    finally:
        workspace.reset(WORKSPACE)


def _rows(n: int, cols: int = 3) -> list[list[object]]:
    return [[f"r{i}c{c}" for c in range(cols)] for i in range(n)]


def _headers(cols: int = 3) -> list[str]:
    return [f"col_{c}" for c in range(cols)]


def _written(ws_id: str, n: int = 100, cols: int = 3, **extra) -> results.Result:
    return results.write_result(
        ws_id, label="profile", headers=_headers(cols), rows=_rows(n, cols),
        **extra,
    )


# --------------------------------------------------------------------------
# the file itself
# --------------------------------------------------------------------------

def test_a_result_is_written_where_the_workspace_can_find_it(ws):
    r = _written(ws)
    assert r.path.exists()
    assert r.path.parent == results.results_dir(ws)


def test_the_file_is_csv_with_a_header_row(ws):
    r = _written(ws, n=5)
    with r.path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == _headers()
    assert len(rows) == 6


def test_every_row_reaches_the_file(ws):
    r = _written(ws, n=137)
    assert r.row_count == 137
    with r.path.open(newline="", encoding="utf-8") as fh:
        assert sum(1 for _ in csv.reader(fh)) == 138


def test_none_is_written_as_empty_not_as_the_word_none(ws):
    """
    'None' in a CSV cell reads back as the four-character string, and a null
    that has become a value is undetectable downstream.
    """
    r = results.write_result(
        ws, label="nulls", headers=["a", "b"], rows=[[1, None], [None, 2]]
    )
    body = r.path.read_text(encoding="utf-8")
    assert "None" not in body
    assert "1," in body


def test_a_comma_inside_a_value_survives_the_round_trip(ws):
    r = results.write_result(
        ws, label="commas", headers=["name", "n"],
        rows=[["Pune, Maharashtra", 3]],
    )
    with r.path.open(newline="", encoding="utf-8") as fh:
        assert list(csv.reader(fh))[1] == ["Pune, Maharashtra", "3"]


def test_a_newline_inside_a_value_survives_the_round_trip(ws):
    r = results.write_result(
        ws, label="newlines", headers=["memo"], rows=[["line one\nline two"]]
    )
    with r.path.open(newline="", encoding="utf-8") as fh:
        assert list(csv.reader(fh))[1] == ["line one\nline two"]


def test_nothing_is_overwritten_within_one_second(ws):
    """
    Two runs in the same second must not share a name. The second write would
    otherwise destroy the evidence the first was written to preserve.
    """
    a = _written(ws, now=T1)
    b = _written(ws, now=T1)
    assert a.path != b.path
    assert a.path.exists() and b.path.exists()


def test_the_name_carries_the_label_and_a_timestamp(ws):
    r = _written(ws, now=T1)
    assert r.path.name.startswith("profile_20260902-113000")
    assert r.path.suffix == ".csv"


def test_a_label_that_is_not_a_filename_is_refused(ws):
    with pytest.raises(ValueError) as exc:
        results.write_result(
            ws, label="../etc/passwd", headers=["a"], rows=[[1]]
        )
    assert "becomes a filename" in str(exc.value)


def test_a_result_with_no_columns_is_refused(ws):
    with pytest.raises(ValueError):
        results.write_result(ws, label="empty", headers=[], rows=[])


def test_an_empty_result_is_still_a_file(ws):
    """Zero rows is an answer. It is not an error and it is not a missing file."""
    r = results.write_result(ws, label="none_matched", headers=["a"], rows=[])
    assert r.path.exists()
    assert r.row_count == 0
    assert "0 rows" in r.to_text()


# --------------------------------------------------------------------------
# the envelope -- locked decision 20, failure mode F7
# --------------------------------------------------------------------------

def test_the_envelope_carries_the_shape(ws):
    text = _written(ws, n=4_000).to_text()
    assert "4,000 rows x 3 columns" in text


def test_the_envelope_carries_the_path(ws):
    r = _written(ws)
    assert str(r.path) in r.to_text()


def test_the_envelope_previews_the_first_rows(ws):
    text = _written(ws, n=100).to_text()
    assert "First 20 of 100 rows:" in text
    assert "r0c0" in text
    assert "r19c0" in text
    assert "r20c0" not in text


def test_the_envelope_says_how_much_was_not_shown(ws):
    text = _written(ws, n=100).to_text()
    assert "80 more rows" in text
    assert "NOT shown above" in text


def test_the_envelope_ends_in_a_call_not_a_suggestion(ws):
    """
    'See the file for more' is not executable. F7 is what the agent does when
    the next step is an intention rather than a call.
    """
    r = _written(ws, n=100)
    text = r.to_text()
    assert f'read_result_file(path="{r.path}", start=21' in text


def test_a_short_result_has_no_paging_hint(ws):
    """Nothing is outstanding, so nothing is offered. Asking for page two of a
    six-row file is a call that returns nothing and reads as a failure."""
    text = _written(ws, n=6).to_text()
    assert "more rows" not in text
    assert "read_result_file" not in text


def test_the_summary_reaches_the_envelope(ws):
    text = _written(
        ws, summary=["3 columns are more than half null", "0 duplicate rows"]
    ).to_text()
    assert "  - 3 columns are more than half null" in text
    assert "  - 0 duplicate rows" in text


def test_the_dataset_name_reaches_the_envelope(ws):
    assert "order_items" in _written(ws, dataset_name="order_items").to_text()


def test_a_wide_result_says_how_many_columns_it_is_hiding(ws):
    """
    format_table caps at 50 columns of its own accord. If a 60-column preview
    reached it, the cap would decide what the model sees and the model would
    not be told. So the slice happens here and the count is stated.
    """
    text = _written(ws, n=30, cols=60).to_text()
    assert "Showing 12 of 60 columns" in text
    assert "col_59" in text  # named in the full list, even though not in the table


def test_a_narrow_result_says_nothing_about_columns(ws):
    assert "Showing" not in _written(ws, n=5, cols=3).to_text()


def test_the_preview_never_reaches_the_formatters_own_caps(ws):
    """
    The invariant behind the two tests above: whatever format_table does at 50
    rows or 50 columns, this module never gets there.
    """
    from backend.engine.util import formatting

    assert results.PREVIEW_ROWS < formatting.MAX_ROWS
    assert results.PREVIEW_COLS < formatting.MAX_COLS
    assert results.PAGE_ROWS <= formatting.MAX_ROWS


# --------------------------------------------------------------------------
# reading it back -- the round trip the Done-When depends on
# --------------------------------------------------------------------------

def test_every_path_a_write_returns_opens_on_a_read(ws):
    """
    The check the Done-When does not name. A writer and a reader in the same
    module still drift, because the workspace root reaches them by different
    routes, and the result is a well-formed envelope pointing at nothing.
    """
    for cols in (1, 3, 60):
        r = _written(ws, n=25, cols=cols)
        text = results.read_result_file(ws, str(r.path))
        assert reason_of(text) is None, f"{cols} columns: {text[:200]}"
        assert "r0c0" in text


def test_a_page_states_which_rows_it_is(ws):
    r = _written(ws, n=100)
    text = results.read_result_file(ws, str(r.path), start=21, limit=10)
    assert "rows 21 to 30 of 100" in text
    assert "r20c0" in text
    assert "r30c0" not in text


def test_a_page_offers_the_next_one(ws):
    r = _written(ws, n=100)
    text = results.read_result_file(ws, str(r.path), start=21, limit=10)
    assert f'read_result_file(path="{r.path}", start=31, limit=10)' in text


def test_the_last_page_says_it_is_the_last(ws):
    r = _written(ws, n=30)
    text = results.read_result_file(ws, str(r.path), start=21, limit=50)
    assert "That is the end of the file." in text
    assert "rows after this page" not in text


def test_a_page_past_the_end_is_not_a_refusal(ws):
    """
    Asking for row 500 of a 30-row file is a wrong guess, not a broken call.
    It carries no reason code and it names the page that does exist -- an
    agent handed a BLOCKED here would start apologising.
    """
    r = _written(ws, n=30)
    text = results.read_result_file(ws, str(r.path), start=500)
    assert reason_of(text) is None
    assert "past the end" in text
    assert "start=1" in text


def test_the_page_size_is_capped(ws):
    r = _written(ws, n=200)
    text = results.read_result_file(ws, str(r.path), start=1, limit=10_000)
    assert f"rows 1 to {results.PAGE_ROWS} of 200" in text


def test_start_zero_is_refused_rather_than_nudged_to_one(ws):
    """
    An off-by-one that silently becomes row 1 returns a page that does not
    begin where it was asked to, and nothing in the output says so.
    """
    r = _written(ws, n=30)
    text = results.read_result_file(ws, str(r.path), start=0)
    assert reason_of(text) is Reason.RESULT_OUT_OF_SCOPE
    assert "numbered from 1" in text


# --------------------------------------------------------------------------
# a path from a model is not trusted
# --------------------------------------------------------------------------

def test_a_path_outside_the_results_directory_is_refused(ws):
    text = results.read_result_file(ws, "/etc/passwd")
    assert reason_of(text) is Reason.RESULT_OUT_OF_SCOPE
    assert "not a result file in this workspace" in text


def test_a_traversal_is_refused_after_resolution_not_before(ws):
    """
    Refusing on the literal '..' catches the obvious spelling and misses
    everything else. The check is on the resolved path.
    """
    r = _written(ws)
    sneaky = str(r.path.parent / ".." / ".." / "etc" / "passwd")
    assert reason_of(results.read_result_file(ws, sneaky)) is Reason.RESULT_OUT_OF_SCOPE


def test_another_workspaces_results_are_refused(ws):
    """
    F13: the server outlives the chat, and another workspace's files are on
    the same disk. Same shape of file, still not this workspace's business.
    """
    other = "results_test_other"
    workspace.reset(other)
    try:
        theirs = results.write_result(
            other, label="profile", headers=["a"], rows=[[1]]
        )
        text = results.read_result_file(ws, str(theirs.path))
        assert reason_of(text) is Reason.RESULT_OUT_OF_SCOPE
    finally:
        workspace.reset(other)


def test_a_missing_file_names_what_is_actually_there(ws):
    _written(ws, now=T1)
    text = results.read_result_file(
        ws, str(results.results_dir(ws) / "profile_19990101-000000.csv")
    )
    assert reason_of(text) is Reason.RESULT_NOT_FOUND
    assert "profile_20260902-113000.csv" in text


def test_a_refusal_names_a_call_with_parentheses(ws):
    """
    Refusal enforces this in its constructor; this asserts the refusals in
    this module actually reach it rather than being built as plain strings.
    """
    for text in (
        results.read_result_file(ws, "/etc/passwd"),
        results.read_result_file(ws, str(results.results_dir(ws) / "nope.csv")),
    ):
        assert "NEXT STEP:" in text
        assert "read_result_file(" in text


# --------------------------------------------------------------------------
# retention -- P5-D4
# --------------------------------------------------------------------------

def test_results_live_inside_the_workspace(ws):
    assert results.results_dir(ws).parent == workspace.workspace_dir(ws)


def test_reset_clears_the_results(ws):
    """
    The whole of the retention answer: nothing auto-deletes, and
    reset_workspace is what removes them. If results lived outside the
    workspace directory this passes nowhere and a reset leaves them behind.
    """
    r = _written(ws)
    assert r.path.exists()
    workspace.reset(ws)
    assert not r.path.exists()
    assert results.list_results(ws) == []


def test_results_are_listed_newest_first(ws):
    old = _written(ws, now=datetime(2026, 9, 1, 8, 0, 0))
    new = _written(ws, now=datetime(2026, 9, 2, 8, 0, 0))
    assert results.list_results(ws)[0].name == new.path.name
    assert old.path.name in [p.name for p in results.list_results(ws)]


def test_the_results_directory_is_created_on_demand(ws):
    workspace.reset(ws)
    assert results.list_results(ws) == []
    assert results.results_dir(ws).is_dir()


def test_the_inline_caps_have_one_home():
    """P8-O14: config's pair is deleted; formatting's is the source of truth."""
    from backend.engine import config
    from backend.engine.util import formatting

    assert [n for n in dir(config) if n.startswith("MAX_INLINE")] == []
    assert (formatting.MAX_ROWS, formatting.MAX_COLS) == (50, 50)
# --------------------------------------------------------------------------
# paging by column -- P8-O15
# --------------------------------------------------------------------------

def test_a_wide_page_says_which_columns_it_is(ws):
    r = _written(ws, n=3, cols=50)
    text = results.read_result_file(ws, str(r.path))
    assert "columns 1 to 12 of 50" in text


def test_the_columns_past_the_window_are_reachable(ws):
    """P8-O15, measured before this existed: a 50-column result returned twelve
    cells and the other thirty-eight columns were in the file and returnable by
    no call at all. Row paging existed; column paging did not."""
    r = _written(ws, n=3, cols=50)
    first = results.read_result_file(ws, str(r.path))
    assert "r0c20" not in first
    later = results.read_result_file(ws, str(r.path), start_col=13)
    assert "columns 13 to 24 of 50" in later
    assert "r0c20" in later


def test_a_wide_page_offers_the_next_columns(ws):
    r = _written(ws, n=3, cols=50)
    text = results.read_result_file(ws, str(r.path))
    assert "38 more columns" in text
    assert f'read_result_file(path="{r.path}", start=1, limit=50, start_col=13)' in text


def test_the_last_column_page_offers_nothing_further(ws):
    r = _written(ws, n=3, cols=50)
    text = results.read_result_file(ws, str(r.path), start_col=49)
    assert "columns 49 to 50 of 50" in text
    assert "more columns" not in text


def test_a_narrow_result_is_not_told_about_columns(ws):
    r = _written(ws, n=3, cols=3)
    text = results.read_result_file(ws, str(r.path))
    assert "columns 1 to 3 of 3" in text
    assert "more columns" not in text


def test_a_column_past_the_end_is_not_a_refusal(ws):
    """The same judgement as a row past the end: a wrong guess, not a broken
    call, so it carries no reason code and names the page that exists."""
    r = _written(ws, n=3, cols=50)
    text = results.read_result_file(ws, str(r.path), start_col=99)
    assert reason_of(text) is None
    assert "past the end" in text
    assert "start_col=39" in text


def test_start_col_zero_is_refused_like_start_zero(ws):
    r = _written(ws, n=3, cols=50)
    text = results.read_result_file(ws, str(r.path), start_col=0)
    assert reason_of(text) is Reason.RESULT_OUT_OF_SCOPE
    assert "numbered from 1" in text


def test_the_column_page_is_capped_like_the_row_page(ws):
    """Asking for fifty columns at once would hand format_table its own cap to
    enforce, and it does not say when it truncates -- the failure this module
    exists to prevent."""
    r = _written(ws, n=3, cols=50)
    text = results.read_result_file(ws, str(r.path), col_limit=10_000)
    assert f"columns 1 to {results.PREVIEW_COLS} of 50" in text


def test_the_page_size_constant_has_one_home():
    """P8-D48 again: config defined RESULT_PAGE_ROWS = 100 with no readers,
    where results.PAGE_ROWS says 50."""
    from backend.engine import config

    assert not hasattr(config, "RESULT_PAGE_ROWS")
    assert results.PAGE_ROWS == 50
