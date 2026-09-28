"""That a validation ran, and what get_workflow_state says about it.

Phase 7, Step 8. Run from the repo root:

    uv run pytest tests/test_validate_runs.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.profile.runs import is_bookkeeping  # noqa: E402
from backend.engine.util import db  # noqa: E402
from backend.engine.validate import runs  # noqa: E402

NOW = datetime(2026, 9, 7, 11, 48)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def a_run(con, **kw):
    defaults = dict(
        dataset_name="sales", contract_version=3, row_count=186,
        checks_total=6, checks_failed=4, checks_not_run=0, now=NOW,
    )
    return runs.record(con, **{**defaults, **kw})


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------


def test_the_log_is_bookkeeping():
    assert runs.VALIDATION_TABLE == "_agent_validations"
    assert is_bookkeeping(runs.VALIDATION_TABLE)


def test_it_is_not_listed_as_a_dataset(con):
    runs.ensure_table(con)
    assert runs.VALIDATION_TABLE not in db.user_tables(con)


def test_a_run_is_appended_not_replaced(con):
    a_run(con)
    a_run(con, now=NOW + timedelta(hours=1), checks_failed=2)
    assert len(runs.history(con, "sales")) == 2
    assert runs.latest(con, "sales").checks_failed == 2


def test_runs_are_kept_per_dataset(con):
    a_run(con)
    a_run(con, dataset_name="other")
    assert len(runs.history(con, "sales")) == 1
    assert runs.latest(con, "other").dataset_name == "other"


def test_passed_is_derived_rather_than_stored(con):
    """Three numbers that must add to the total. Storing the fourth is a
    chance for them to disagree."""
    run = a_run(con, checks_total=6, checks_failed=4, checks_not_run=1)
    assert run.checks_passed == 1


# --------------------------------------------------------------------------
# what it says
# --------------------------------------------------------------------------


def test_a_dataset_nobody_validated_produces_no_lines(con):
    """Empty, not "not validated" -- the renderer decides what an absence
    means, the same way profile and cleaning notes leave it there."""
    assert runs.state_notes(con, "sales", 186) == []


def test_it_names_the_time_the_contract_and_the_counts(con):
    a_run(con)
    line = runs.state_notes(con, "sales", 186, now=NOW + timedelta(minutes=12))[0]
    assert "validated 12 minutes ago (2026-09-07 11:48)" in line
    assert "against contract v3" in line
    assert "4 of 6 failed, 2 passed" in line


def test_the_verdict_is_counted_never_collapsed(con):
    """P7-D10 in the state line, which is read faster than the report."""
    a_run(con, checks_failed=0, checks_not_run=4)
    line = runs.state_notes(con, "sales", 186, now=NOW)[0]
    assert "2 of 6 passed, 4 could not run" in line
    assert "PASS" not in line


def test_a_stale_validation_says_old_rather_than_wrong(con):
    """The distinction profile/runs.py draws in the same words: an agent told
    "stale" re-runs, an agent told "wrong" apologises."""
    a_run(con)
    lines = runs.state_notes(con, "sales", 190, now=NOW)
    assert "out of date" in lines[1]
    assert "gained 4 row(s)" in lines[1]
    assert "nothing has checked the rows that arrived after it" in lines[1]
    assert "wrong" not in " ".join(lines).lower()


def test_a_shrunken_table_says_the_rows_are_gone(con):
    a_run(con)
    lines = runs.state_notes(con, "sales", 180, now=NOW)
    assert "lost 6 row(s)" in lines[1]
    assert "no longer there" in lines[1]


def test_an_unchanged_table_offers_the_report_instead(con):
    a_run(con)
    lines = runs.state_notes(con, "sales", 186, now=NOW)
    assert len(lines) == 2
    assert 'Full report: validate_dataset(dataset_name="sales")' in lines[1]
    assert "out of date" not in lines[1]


def test_the_age_phrase_carries_both_the_interval_and_the_stamp(con):
    run = a_run(con)
    for delta, expected in (
        (timedelta(seconds=30), "validated just now"),
        (timedelta(minutes=40), "validated 40 minutes ago"),
        (timedelta(hours=5), "validated 5 hours ago"),
        (timedelta(days=3), "validated 3 days ago"),
    ):
        phrase = run.age_phrase(NOW + delta)
        assert phrase.startswith(expected)
        assert "2026-09-07 11:48" in phrase


# --------------------------------------------------------------------------
# Step 8b: the other way a validation goes out of date
# --------------------------------------------------------------------------


def test_a_contract_that_moved_since_makes_the_validation_stale(con):
    """Step 8 shipped one kind of staleness and this is the other.

    Validate at v2, confirm v3, and without this the section reads "validated
    against contract v2" directly above "Contract v3", with nothing saying the
    check predates the agreement it is quoted against. The row count cannot
    catch it: a table that gained no rows reads as freshly validated.
    """
    a_run(con, contract_version=2)
    lines = runs.state_notes(con, "sales", 186, 3, now=NOW)
    assert "out of date" in lines[1]
    assert "the contract has moved to v3 since (v2 then)" in lines[1]
    assert "nobody is working to now" in lines[1]
    assert "Re-run validate_dataset" in lines[1]


def test_both_kinds_of_staleness_share_one_instruction(con):
    """Two reasons, one sentence, one re-run. Two "re-run this" lines for one
    stale run is noise, and the reader acts on the first either way."""
    a_run(con, contract_version=2)
    line = runs.state_notes(con, "sales", 190, 3, now=NOW)[1]
    assert "the contract has moved to v3" in line
    assert "gained 4 row(s)" in line
    assert line.count("Re-run validate_dataset") == 1


def test_an_unchanged_contract_is_not_reported_as_moved(con):
    a_run(con, contract_version=2)
    lines = runs.state_notes(con, "sales", 186, 2, now=NOW)
    assert "out of date" not in lines[1]
    assert "Full report" in lines[1]


def test_an_unknown_version_skips_the_comparison(con):
    """None means the caller does not know, not "it matches"."""
    a_run(con, contract_version=2)
    lines = runs.state_notes(con, "sales", 186, None, now=NOW)
    assert "Full report" in lines[1]
