"""
Unit tests for profile/runs.py -- the record that a profile happened.

Two fixtures, on purpose. Most of these need only a bare connection, because
the log is a table and nothing else. The last group needs a workspace, because
it goes through `tools.profile_dataset` and asserts the recorded path is the
path that was actually written -- a record naming a file that does not exist is
worse than no record, since the workflow state would then offer a
`read_result_file` call that refuses.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import pytest

from backend.engine import workspace
from backend.engine.contract.refusals import reason_of
from backend.engine.profile import runs, tools
from backend.engine.util import results

WORKSPACE = "profile_runs_test"
T1 = datetime(2026, 9, 2, 9, 0, 0)


@pytest.fixture
def con():
    c = duckdb.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws():
    workspace.reset(WORKSPACE)
    try:
        yield WORKSPACE
    finally:
        workspace.reset(WORKSPACE)


@pytest.fixture
def sales(con):
    con.execute(
        """
        CREATE TABLE sales AS SELECT
          'ORD-' || lpad(i::VARCHAR, 5, '0')                 AS order_id,
          CASE WHEN i % 17 = 0 THEN 'N/A' ELSE 'North' END   AS region,
          ((i % 97) + 1) * 1.5                               AS revenue
        FROM range(200) t(i)
        """
    )
    return con


def _record(con, name="sales", rows=500, at=T1, **extra):
    return runs.record(
        con, dataset_name=name, row_count=rows, column_count=8,
        result_path=f"/tmp/results/profile_{name}.csv", now=at, **extra,
    )


# --------------------------------------------------------------------------
# the log itself
# --------------------------------------------------------------------------

def test_a_run_comes_back_as_it_went_in(con):
    _record(con, duplicate_rows=3, columns_missing=2)
    run = runs.latest(con, "sales")
    assert run.dataset_name == "sales"
    assert run.row_count == 500
    assert run.duplicate_rows == 3
    assert run.columns_missing == 2
    assert run.run_at == T1


def test_a_dataset_never_profiled_has_no_run(con):
    assert runs.latest(con, "nothing") is None


def test_reading_a_log_that_does_not_exist_yet_is_not_an_error(con):
    """
    Created lazily, like _agent_contracts. A workspace that has never profiled
    anything should not carry an empty table, and every read path here creates
    it before looking.
    """
    assert runs.latest(con, "sales") is None
    assert runs.all_latest(con) == {}


def test_ensure_table_can_be_called_repeatedly(con):
    runs.ensure_table(con)
    runs.ensure_table(con)
    _record(con)
    assert len(runs.history(con, "sales")) == 1


def test_a_second_run_appends_rather_than_replacing(con):
    """
    A profile supersedes nothing. Three runs in an hour are three facts, not
    three drafts of one -- which is why there is no `is_current` here and why
    contract/store.py has one.
    """
    _record(con, rows=500, at=T1)
    _record(con, rows=530, at=T1 + timedelta(hours=1))
    assert len(runs.history(con, "sales")) == 2


def test_the_latest_run_is_the_newest_one(con):
    _record(con, rows=500, at=T1)
    _record(con, rows=530, at=T1 + timedelta(hours=1))
    assert runs.latest(con, "sales").row_count == 530


def test_history_comes_back_newest_first(con):
    _record(con, rows=500, at=T1)
    _record(con, rows=530, at=T1 + timedelta(hours=1))
    assert [r.row_count for r in runs.history(con, "sales")] == [530, 500]


def test_all_latest_gives_one_run_per_dataset(con):
    _record(con, name="sales", rows=500, at=T1)
    _record(con, name="sales", rows=530, at=T1 + timedelta(hours=1))
    _record(con, name="customers", rows=50, at=T1)
    latest = runs.all_latest(con)
    assert set(latest) == {"sales", "customers"}
    assert latest["sales"].row_count == 530


def test_the_log_is_a_bookkeeping_table(con):
    """
    _agent_contracts was once listed as a dataset because the filter that hides
    bookkeeping tables predated it. A prefix test keeps working when a fifth
    arrives; a list of three names does not.
    """
    assert runs.is_bookkeeping(runs.PROFILE_TABLE)
    assert runs.is_bookkeeping("_agent_datasets")
    assert runs.is_bookkeeping("_agent_contracts")
    assert not runs.is_bookkeeping("sales")


# --------------------------------------------------------------------------
# how long ago, and what moved under it
# --------------------------------------------------------------------------

def test_a_fresh_profile_reads_as_just_now(con):
    run = _record(con, at=T1)
    assert "just now" in run.age_phrase(now=T1 + timedelta(seconds=30))


def test_an_hour_old_profile_reads_in_minutes(con):
    run = _record(con, at=T1)
    assert "40 minutes ago" in run.age_phrase(now=T1 + timedelta(minutes=40))


def test_a_day_old_profile_reads_in_hours_then_days(con):
    run = _record(con, at=T1)
    assert "10 hours ago" in run.age_phrase(now=T1 + timedelta(hours=10))
    assert "3 days ago" in run.age_phrase(now=T1 + timedelta(days=3))


def test_the_phrase_carries_the_timestamp_as_well(con):
    """
    '12 minutes ago' is what a person reads and it is meaningless in a
    transcript read tomorrow. Both, always.
    """
    run = _record(con, at=T1)
    assert "2026-09-02 09:00" in run.age_phrase(now=T1 + timedelta(minutes=5))


def test_an_unchanged_table_has_no_drift(con):
    assert _record(con, rows=500).drift_phrase(500) is None


def test_a_grown_table_says_the_profile_describes_less(con):
    phrase = _record(con, rows=500).drift_phrase(530)
    assert "gained 30 row(s)" in phrase
    assert "500 then, 530 now" in phrase


def test_a_shrunk_table_says_the_profile_describes_rows_that_are_gone(con):
    phrase = _record(con, rows=500).drift_phrase(480)
    assert "lost 20 row(s)" in phrase
    assert "no longer there" in phrase


# --------------------------------------------------------------------------
# what the workflow state should say
# --------------------------------------------------------------------------

def test_an_unprofiled_dataset_produces_no_lines(con):
    """
    Empty rather than a sentence, because only the caller knows whether never
    having profiled matters yet -- it does not, for a dataset loaded ten
    seconds ago.
    """
    assert runs.state_notes(con, "sales", 500) == []


def test_a_current_profile_offers_the_full_counts(con):
    _record(con, rows=500, at=T1)
    lines = runs.state_notes(con, "sales", 500, now=T1 + timedelta(minutes=5))
    assert "profiled 5 minutes ago" in lines[0]
    assert "read_result_file(" in lines[1]


def test_a_stale_profile_says_so_and_names_the_re_run(con):
    """
    Old and wrong need different words. An agent told 'stale' re-profiles; an
    agent told 'wrong' apologises.
    """
    _record(con, rows=500, at=T1)
    lines = runs.state_notes(con, "sales", 530, now=T1 + timedelta(hours=2))
    joined = " ".join(lines)
    assert "out of date" in joined
    assert "gained 30 row(s)" in joined
    assert 'profile_dataset(dataset_name="sales")' in joined
    assert "read_result_file(" not in joined


# --------------------------------------------------------------------------
# through the tool, where the path has to be real
# --------------------------------------------------------------------------

def test_profiling_records_the_run(ws, sales):
    tools.profile_dataset(sales, ws, "sales")
    run = runs.latest(sales, "sales")
    assert run is not None
    assert run.row_count == 200
    assert run.column_count == 3


def test_the_recorded_path_is_the_file_that_was_written(ws, sales):
    tools.profile_dataset(sales, ws, "sales")
    run = runs.latest(sales, "sales")
    assert run.result_path == str(results.list_results(ws)[0])


def test_the_recorded_path_actually_opens(ws, sales):
    """
    A record naming a file that does not exist is worse than no record: the
    workflow state would offer a read_result_file call that refuses, and the
    agent would have been told to make it.
    """
    tools.profile_dataset(sales, ws, "sales")
    run = runs.latest(sales, "sales")
    page = tools.read_result(ws, run.result_path)
    assert reason_of(page) is None
    assert "region" in page


def test_the_findings_are_recorded_beside_the_shape(ws, sales):
    tools.profile_dataset(sales, ws, "sales")
    run = runs.latest(sales, "sales")
    assert run.duplicate_rows == 0
    assert run.columns_missing == 1  # region holds the N/As


def test_a_refused_profile_records_nothing(ws, sales):
    tools.profile_dataset(sales, ws, "nope")
    assert runs.all_latest(sales) == {}


def test_profiling_twice_leaves_both_runs(ws, sales):
    tools.profile_dataset(sales, ws, "sales")
    tools.profile_dataset(sales, ws, "sales")
    assert len(runs.history(sales, "sales")) == 2
    assert len(results.list_results(ws)) == 2


def test_the_state_notes_go_stale_when_the_table_grows(ws, sales):
    tools.profile_dataset(sales, ws, "sales")
    sales.execute(
        "INSERT INTO sales SELECT 'ORD-NEW-' || i::VARCHAR, 'South', 1.0 "
        "FROM range(30) t(i)"
    )
    joined = " ".join(runs.state_notes(sales, "sales", 230))
    assert "out of date" in joined
    assert "gained 30 row(s)" in joined
