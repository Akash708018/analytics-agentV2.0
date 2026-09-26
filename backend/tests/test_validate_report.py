"""The pass/fail table, and the headline that refuses to be one word.

Phase 7, Step 6. Run from the repo root:

    uv run pytest tests/test_validate_report.py -q

The counts rendered here come from tests/test_validate_rules.py, which gets
them from broken_sales.csv, which tests/test_broken_sales_ground_truth.py pins
against the file. This file asserts what the reader is shown, not what the
checks computed.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.validate import report, rules  # noqa: E402
from backend.engine.validate.rules import CheckResult, Outcome  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "broken_sales.csv"
WINDOW = (date(2024, 1, 1), date(2024, 12, 31))


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


@pytest.fixture()
def sales(con):
    if not FIXTURE.exists():
        pytest.skip(f"{FIXTURE.name} not generated; see make_fixtures.py")
    con.execute(f"CREATE TABLE sales AS SELECT * FROM read_csv_auto('{FIXTURE}')")
    return con


@pytest.fixture()
def broken(sales):
    return (
        rules.key_checks(sales, "sales", ["order_id"])
        + rules.date_checks(sales, "sales", "order_ts", WINDOW,
                            today=date(2026, 1, 1))
        + [rules.row_count_check(sales, "sales", 200)]
    )


def passing(con):
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('a'),('b')) v(k)")
    return rules.key_checks(con, "t", ["k"])


# --------------------------------------------------------------------------
# the headline
# --------------------------------------------------------------------------


def test_the_headline_counts_rather_than_verdicts(broken):
    """P7-D10. Five failed and one passed is not "FAIL", and the single word
    is available, shorter, and throws away what P7-D7 was written to keep."""
    line = report.headline("sales", broken)
    assert line == "sales: 5 of 6 check(s) failed, 1 passed."


def test_a_clean_run_says_so_plainly(con):
    assert report.headline("t", passing(con)) == "t: all 2 check(s) passed."


def test_a_run_where_nothing_could_be_checked_is_not_a_pass(con):
    """The sentence a thin contract earns. It must not be readable as
    success, because the reader who skims one line is the reader this is
    for."""
    con.execute("CREATE TABLE t AS SELECT 1 AS n")
    nothing = rules.key_checks(con, "t", []) + rules.date_checks(con, "t", None)
    line = report.headline("t", nothing)
    assert "no check could run" in line
    assert "none of them is a pass" in line
    assert "PASS" not in line


def test_passes_beside_unrun_checks_claim_nothing_about_the_rest(con):
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('a'),('b')) v(k)")
    mixed = rules.key_checks(con, "t", ["k"]) + rules.date_checks(con, "t", None)
    line = report.headline("t", mixed)
    assert "2 of 5 check(s) passed, 3 could not run" in line
    assert "nothing is claimed about what was not checked" in line


def test_the_denominator_appears_once(broken):
    """"5 of 6 failed, 1 of 6 passed" says the same thing three times. The
    total attaches to whichever count is named first."""
    assert report.headline("sales", broken).count("of 6") == 1


def test_no_checks_at_all_says_nothing_was_checked():
    assert "nothing was checked" in report.headline("sales", [])
    assert "nothing was checked" in report.render("sales", [])


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------


def test_every_check_is_a_row(broken):
    text = report.render("sales", broken)
    for r in broken:
        assert r.title in text


def test_the_counts_are_the_ones_the_checks_computed(broken):
    text = report.render("sales", broken)
    assert "| 186 | 178 | 6 | 2 |" in text      # key.unique
    assert "| 186 | 171 | 9 | 6 |" in text      # date.in_window


def test_a_table_scoped_check_prints_dashes_where_the_counts_go(broken):
    """P7-D8 reaching the page. Zeros would read as "nothing failed"."""
    row = next(
        line for line in report.render("sales", broken).splitlines()
        if "Row count matches" in line
    )
    assert row.endswith("| - | - | - | - |")
    assert "| 0 |" not in row


def test_a_check_that_did_not_run_prints_dashes_too(con):
    con.execute("CREATE TABLE t AS SELECT 1 AS n")
    text = report.render("t", rules.key_checks(con, "t", []))
    assert "NOT RUN" in text
    assert "| - | - | - | - |" in text


def test_the_dash_is_explained_only_when_there_is_one(con, broken):
    assert "A dash means" in report.render("sales", broken)
    assert "A dash means" not in report.render("t", passing(con))


def test_the_contract_version_is_named_when_it_is_known(broken):
    assert "Checked against contract v3." in report.render(
        "sales", broken, contract_version=3
    )
    assert "contract v" not in report.render("sales", broken)


# --------------------------------------------------------------------------
# what sits under the table
# --------------------------------------------------------------------------


def test_every_sentence_reaches_the_page(broken):
    text = report.render("sales", broken)
    for r in broken:
        assert r.sentence() in text


def test_the_evidence_is_shown_under_its_own_check(broken):
    text = report.render("sales", broken)
    assert "- ORD-00011 appears 2 times" in text
    assert "- 4 row(s) before 2024-01-01" in text


def test_withheld_evidence_is_counted_on_the_page(con):
    con.execute("""CREATE TABLE t AS
        SELECT ('K' || (i % 12)) AS k FROM range(24) t(i)""")
    text = report.render("t", rules.key_checks(con, "t", ["k"]))
    assert "(2 more not shown)" in text


def test_unexaminable_rows_are_never_added_up(broken):
    """The same six undated rows are not_checked for two different checks.
    Twelve of a hundred and eighty-six would be a number nothing measured."""
    text = report.render("sales", broken)
    assert "never added up" in text
    assert "12 row" not in text


def test_that_note_is_absent_when_every_row_was_examined(con):
    assert "never added up" not in report.render("t", passing(con))


def test_the_next_step_is_supplied_rather_than_guessed(broken):
    """The renderer does not know what workspace it is in or which tools are
    registered, and a call it cannot verify exists is exactly what a refusal
    is forbidden to name."""
    assert "NEXT STEP" not in report.render("sales", broken)
    assert "NEXT STEP: call get_workflow_state()" in report.render(
        "sales", broken, next_call="get_workflow_state()"
    )
