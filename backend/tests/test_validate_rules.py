"""CheckResult's invariants, and the first two checks.

Phase 7, Step 4. Run from the repo root:

    uv run pytest tests/test_validate_rules.py -q

The counts asserted against broken_sales.csv are the ones
tests/test_broken_sales_ground_truth.py pins against the file itself, so a
failure here means the rule is wrong rather than the fixture having drifted.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.validate import rules
from backend.engine.validate.rules import CheckResult, Outcome

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "broken_sales.csv"


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


def by_id(results):
    return {r.check_id: r for r in results}


# --------------------------------------------------------------------------
# the invariant
# --------------------------------------------------------------------------


def test_the_three_counts_must_sum_to_the_row_count():
    """P7-D4, made structural.

    A check that reports only what its predicate saw describes some of its
    rows and calls the rest a pass. The constructor is where that stops being
    possible, for the same reason Refusal checks its next_call for parentheses
    rather than trusting every caller to remember.
    """
    with pytest.raises(ValueError, match="P7-D4"):
        CheckResult(check_id="x", title="t", subject="c",
                    rows=100, passed=90, failed=5, not_checked=0)


def test_the_counts_that_do_sum_are_accepted():
    r = CheckResult(check_id="x", title="t", subject="c",
                    rows=100, passed=90, failed=5, not_checked=5)
    assert r.outcome is Outcome.FAIL


def test_a_check_that_did_not_run_counts_nothing():
    with pytest.raises(ValueError, match="did not run counts nothing"):
        CheckResult(check_id="x", title="t", subject="c", rows=100,
                    passed=100, not_run_because="no key declared")


def test_evidence_is_capped():
    with pytest.raises(ValueError, match="cap is"):
        CheckResult(check_id="x", title="t", subject="c", rows=1, passed=1,
                    evidence=tuple(str(i) for i in range(rules.EVIDENCE_LIMIT + 1)),
                    evidence_total=99)


def test_the_remainder_cannot_be_smaller_than_what_is_shown():
    with pytest.raises(ValueError, match="below the"):
        CheckResult(check_id="x", title="t", subject="c", rows=1, passed=1,
                    evidence=("a", "b"), evidence_total=1)


def test_a_check_with_no_failures_passes():
    assert CheckResult(check_id="x", title="t", subject="c",
                       rows=10, passed=10).outcome is Outcome.PASS


def test_a_check_that_could_not_run_is_not_a_check_that_passed():
    """P7-D7. PASS on an unrun check is a lie told in the safest-looking
    direction, and it is the one a reader is least likely to question."""
    r = CheckResult.not_run("key.unique", "Primary key is unique", "-",
                            "the contract states no primary key")
    assert r.outcome is Outcome.NOT_RUN
    assert r.outcome is not Outcome.PASS
    assert "Not run:" in r.sentence()


def test_the_sentence_names_the_rows_that_were_not_checked():
    r = CheckResult(check_id="x", title="t", subject="c", rows=10, passed=7,
                    failed=1, not_checked=2, detail="1 row(s) failed")
    assert "2 row(s) could not be checked" in r.sentence()
    assert "not counted as passing" in r.sentence()


# --------------------------------------------------------------------------
# the key checks, against the fixture
# --------------------------------------------------------------------------


def test_uniqueness_splits_the_fixture_into_178_6_and_2(sales):
    """Six rows repeat a key, two have none, and the two null rows are NOT
    counted as passing -- they were never comparable."""
    r = by_id(rules.key_checks(sales, "sales", ["order_id"]))["key.unique"]
    assert (r.rows, r.passed, r.failed, r.not_checked) == (186, 178, 6, 2)
    assert r.outcome is Outcome.FAIL


def test_uniqueness_names_the_keys_that_repeat(sales):
    """P7-D1's whole argument for the grouping form: a count sends somebody to
    write the query, the values answer the question."""
    r = by_id(rules.key_checks(sales, "sales", ["order_id"]))["key.unique"]
    assert r.evidence_total == 3
    assert len(r.evidence) == 3
    assert all("appears 2 times" in line for line in r.evidence)
    assert "3 key value(s) repeat, across 6 row(s)" in r.sentence()


def test_completeness_is_a_separate_finding(sales):
    """P7-D6 in a new place: "the key repeats" and "the key is missing" have
    different fixes, and one number cannot carry both."""
    r = by_id(rules.key_checks(sales, "sales", ["order_id"]))["key.complete"]
    assert (r.rows, r.passed, r.failed, r.not_checked) == (186, 184, 2, 0)
    assert r.outcome is Outcome.FAIL


def test_a_clean_key_passes_both_checks(con):
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('a'),('b')) v(k)")
    results = by_id(rules.key_checks(con, "t", ["k"]))
    assert results["key.unique"].outcome is Outcome.PASS
    assert results["key.complete"].outcome is Outcome.PASS
    assert results["key.unique"].evidence == ()


def test_a_composite_key_is_checked_as_a_whole(con):
    """The row-constructor path. Neither column is unique alone; the pair is."""
    con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        ('a', 1), ('a', 2), ('b', 1)) v(k, n)""")
    results = by_id(rules.key_checks(con, "t", ["k", "n"]))
    assert results["key.unique"].outcome is Outcome.PASS
    assert results["key.unique"].subject == "k + n"


def test_no_declared_key_is_not_run_rather_than_passed(con):
    con.execute("CREATE TABLE t AS SELECT 1 AS n")
    for r in rules.key_checks(con, "t", []):
        assert r.outcome is Outcome.NOT_RUN
        assert "no primary key" in r.not_run_because


def test_a_key_naming_a_missing_column_is_not_run_and_says_which(con):
    """The contract names a column the table does not have. That is a stale
    contract, not a failed check, and reporting FAIL would send somebody to
    fix the data."""
    con.execute("CREATE TABLE t AS SELECT 1 AS n")
    for r in rules.key_checks(con, "t", ["nope"]):
        assert r.outcome is Outcome.NOT_RUN
        assert "nope" in r.not_run_because


def test_the_evidence_is_capped_with_the_remainder_counted(con):
    """P5-D3 again. Twelve repeated keys, ten shown, two counted."""
    con.execute("""CREATE TABLE t AS
        SELECT ('K' || (i % 12)) AS k FROM range(24) t(i)""")
    r = by_id(rules.key_checks(con, "t", ["k"]))["key.unique"]
    assert r.evidence_total == 12
    assert len(r.evidence) == rules.EVIDENCE_LIMIT
    assert r.evidence_withheld == 2
    assert (r.rows, r.passed, r.failed, r.not_checked) == (24, 0, 24, 0)


def test_a_composite_key_with_nulls_is_examined_rather_than_skipped(con):
    """The case that would have broken the invariant.

    A null-bearing tuple IS a comparable value under the row constructor, so
    every row was examined and not_checked is zero -- while `key.complete`
    still fails on the same two rows. That split is the whole reason there are
    two checks: one number could not have said both.
    """
    con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        ('a', 1), ('b', NULL), ('b', NULL)) v(k, n)""")
    results = by_id(rules.key_checks(con, "t", ["k", "n"]))
    unique = results["key.unique"]
    assert (unique.rows, unique.passed, unique.failed, unique.not_checked) == (3, 1, 2, 0)
    assert unique.evidence == ("b + (null) appears 2 times",)
    complete = results["key.complete"]
    assert (complete.passed, complete.failed) == (1, 2)


def test_a_one_column_key_with_nulls_is_not_examined_for_uniqueness(con):
    """The mirror image, and P7-D6's denominator in a second place.

    `count(DISTINCT x)` drops nulls, so the two null rows were never
    comparable. They are not counted as passing and they are not called
    duplicates -- which is exactly the sentence Step 2 corrected.
    """
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('a'),(NULL),(NULL)) v(k)")
    unique = by_id(rules.key_checks(con, "t", ["k"]))["key.unique"]
    assert (unique.rows, unique.passed, unique.failed, unique.not_checked) == (3, 1, 0, 2)
    assert unique.outcome is Outcome.PASS
    assert unique.evidence == ()


def test_a_key_column_called_n_does_not_zero_the_count(con):
    """The regression detector for a collision that reported nothing wrong.

    The totals subquery once aliased its count `AS n` while also projecting
    the key columns. On a table whose key column is called `n`, the subquery
    had two columns of that name, `sum(n)` summed the grouping column, and
    every duplicate row became zero rows -- a clean PASS over a broken key,
    with nothing raised. The subquery now projects only the count.
    """
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1),(1),(2)) v(n)")
    r = by_id(rules.key_checks(con, "t", ["n"]))["key.unique"]
    assert (r.failed, r.evidence_total) == (2, 1)
    assert r.outcome is Outcome.FAIL


# --------------------------------------------------------------------------
# the date checks
# --------------------------------------------------------------------------

WINDOW = (date(2024, 1, 1), date(2024, 12, 31))


def test_the_window_splits_the_fixture_into_171_9_and_6(sales):
    r = by_id(rules.date_checks(sales, "sales", "order_ts", WINDOW))["date.in_window"]
    assert (r.rows, r.passed, r.failed, r.not_checked) == (186, 171, 9, 6)
    assert r.passed + r.failed + r.not_checked == r.rows


def test_the_window_bound_includes_the_last_second_of_the_last_day(sales):
    """P7-D2, as a number this test fails on.

    One row sits at 2024-12-31 23:59:59. Written `< end + INTERVAL 1 DAY` the
    window holds 171 rows; written `<= end` the bound casts to midnight and it
    holds 170. If that is ever rewritten the obvious way, this drops by one.
    """
    r = by_id(rules.date_checks(sales, "sales", "order_ts", WINDOW))["date.in_window"]
    assert r.passed == 171, "the inclusive upper bound lost the last day"


def test_the_window_evidence_names_the_earliest_and_the_latest(sales):
    r = by_id(rules.date_checks(sales, "sales", "order_ts", WINDOW))["date.in_window"]
    assert len(r.evidence) == 2
    assert "4 row(s) before 2024-01-01" in r.evidence[0]
    assert "2023-04-04" in r.evidence[0]
    assert "5 row(s) after 2024-12-31" in r.evidence[1]
    assert "2025-06-25" in r.evidence[1]


def test_undated_rows_are_their_own_finding(sales):
    """Six rows have no date. They are `failed` for date.present and
    `not_checked` for date.in_window -- the same rows, counted differently
    because the two checks ask different questions."""
    results = by_id(rules.date_checks(sales, "sales", "order_ts", WINDOW))
    present = results["date.present"]
    assert (present.rows, present.passed, present.failed) == (186, 180, 6)
    assert results["date.in_window"].not_checked == 6


def test_a_missing_window_silences_only_the_window_check(sales):
    """A contract may name a date column and declare no window. That is a
    legitimate state, and it has nothing to do with whether rows are dated."""
    results = by_id(rules.date_checks(sales, "sales", "order_ts", None))
    assert results["date.in_window"].outcome is Outcome.NOT_RUN
    assert "no analysis window" in results["date.in_window"].not_run_because
    assert results["date.present"].outcome is Outcome.FAIL
    assert results["date.not_future"].outcome is not Outcome.NOT_RUN


def test_no_date_column_is_not_run_rather_than_passed(con):
    con.execute("CREATE TABLE t AS SELECT 1 AS n")
    for r in rules.date_checks(con, "t", None):
        assert r.outcome is Outcome.NOT_RUN
        assert "no date column" in r.not_run_because


def test_a_date_column_the_table_lacks_is_not_run_and_says_which(con):
    con.execute("CREATE TABLE t AS SELECT 1 AS n")
    for r in rules.date_checks(con, "t", "ordered_at"):
        assert r.outcome is Outcome.NOT_RUN
        assert "ordered_at" in r.not_run_because


def test_a_future_date_is_counted_against_an_injected_today(con):
    """`today` is injectable for the reason store.confirm's `now` is: a test
    that reads the wall clock asserts something different every day."""
    con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        (TIMESTAMP '2024-06-01 09:00:00'),
        (TIMESTAMP '2030-01-01 00:00:00')) v(ts)""")
    r = by_id(rules.date_checks(con, "t", "ts", today=date(2026, 1, 1)))["date.not_future"]
    assert (r.passed, r.failed) == (1, 1)
    assert "dated after 2026-01-01" in r.detail


def test_today_itself_is_not_the_future(con):
    """The same bound as P7-D2, for the same reason: a row stamped this
    afternoon is not tomorrow's data."""
    con.execute("""CREATE TABLE t AS SELECT * FROM (VALUES
        (TIMESTAMP '2026-01-01 23:59:59')) v(ts)""")
    r = by_id(rules.date_checks(con, "t", "ts", today=date(2026, 1, 1)))["date.not_future"]
    assert r.outcome is Outcome.PASS


# --------------------------------------------------------------------------
# the row count
# --------------------------------------------------------------------------


def test_the_same_count_passes_and_counts_no_rows(sales):
    """P7-D8: the finding is about the table, so the four counts stay at zero
    rather than being invented."""
    r = rules.row_count_check(sales, "sales", 186)
    assert r.outcome is Outcome.PASS
    assert r.scope is rules.Scope.TABLE
    assert (r.rows, r.passed, r.failed, r.not_checked) == (0, 0, 0, 0)


def test_a_grown_table_passes_and_says_by_how_much(sales):
    """classify_drift calls this NEUTRAL -- proceed, and say so."""
    r = rules.row_count_check(sales, "sales", 150)
    assert r.outcome is Outcome.PASS
    assert "gained 36 row(s)" in r.detail


def test_a_shrunken_table_fails(sales):
    """Rows that were there when the agreement was made are gone, so every
    number computed under it describes rows that are no longer there --
    runs.drift_phrase's distinction, in its words."""
    r = rules.row_count_check(sales, "sales", 200)
    assert r.outcome is Outcome.FAIL
    assert "lost 14 row(s)" in r.detail
    assert "no longer there" in r.detail


def test_a_table_scoped_check_may_not_count_rows():
    with pytest.raises(ValueError, match="P7-D8"):
        CheckResult(check_id="x", title="t", subject="s",
                    scope=rules.Scope.TABLE, table_ok=True, rows=10, passed=10)


def test_a_table_scoped_check_needs_a_verdict():
    with pytest.raises(ValueError, match="needs table_ok"):
        CheckResult(check_id="x", title="t", subject="s", scope=rules.Scope.TABLE)


# --------------------------------------------------------------------------
# Step 12: the two declarations, checked
# --------------------------------------------------------------------------

LOOKUP = Path(__file__).resolve().parent / "fixtures" / "region_lookup.csv"
CHANNELS = ["Online", "Retail", "Wholesale"]


@pytest.fixture()
def joined(sales):
    if not LOOKUP.exists():
        pytest.skip(f"{LOOKUP.name} not generated; see make_fixtures.py")
    sales.execute(
        f"CREATE TABLE region_lookup AS SELECT * FROM read_csv_auto('{LOOKUP}')"
    )
    return sales


def fk(columns=("region",), references="region_lookup", referenced=None):
    from backend.engine.contract.dataset_contract import ForeignKey
    return ForeignKey(columns=list(columns), references=references,
                      referenced_columns=list(referenced or []))


def test_orphans_and_null_references_are_counted_apart(joined):
    """7 and 8. Both are unmatched and only one is a broken reference: a NULL
    foreign key points at nothing on purpose."""
    r = rules.reference_checks(joined, "sales", [fk()])[0]
    assert (r.rows, r.passed, r.failed, r.not_checked) == (186, 171, 7, 8)
    assert r.outcome is Outcome.FAIL


def test_the_orphans_are_named_with_their_row_counts(joined):
    """Three misspellings across seven rows -- two different numbers, and the
    report says both."""
    r = rules.reference_checks(joined, "sales", [fk()])[0]
    assert r.evidence_total == 3
    assert "7 row(s) point at 3 value(s)" in r.detail
    assert any("Nord (3 row(s)) is not in region_lookup" in e for e in r.evidence)


def test_not_in_would_have_reported_a_clean_pass(joined):
    """P7-D3's regression detector, asserted against the rule rather than
    against DuckDB.

    region_lookup carries a blank row, so `NOT IN` returns UNKNOWN for every
    comparison. The rule finds 7; the query nobody should write finds 0.
    """
    r = rules.reference_checks(joined, "sales", [fk()])[0]
    naive = joined.execute(
        "SELECT count(*) FROM sales "
        "WHERE region NOT IN (SELECT region FROM region_lookup)"
    ).fetchone()[0]
    assert (r.failed, naive) == (7, 0)


def test_a_parent_that_is_not_loaded_is_not_run(sales):
    """The contract is not wrong; the workspace is thin. P7-D7."""
    r = rules.reference_checks(sales, "sales", [fk(references="nowhere")])[0]
    assert r.outcome is Outcome.NOT_RUN
    assert "nowhere is not loaded" in r.not_run_because
    assert "the workspace is thin" in r.not_run_because


def test_a_join_that_cannot_be_made_is_not_run(joined):
    r = rules.reference_checks(
        joined, "sales", [fk(referenced=["nope"])])[0]
    assert r.outcome is Outcome.NOT_RUN
    assert "no column called nope" in r.not_run_because


def test_every_reference_matching_passes(joined):
    joined.execute("CREATE TABLE tidy AS SELECT 'North' AS region")
    r = rules.reference_checks(joined, "tidy", [fk()])[0]
    assert r.outcome is Outcome.PASS
    assert r.evidence == ()


def test_values_outside_the_declared_set_are_counted(sales):
    r = rules.domain_checks(sales, "sales", {"channel": CHANNELS})[0]
    assert (r.rows, r.passed, r.failed, r.not_checked) == (186, 177, 9, 0)
    assert r.evidence_total == 2
    assert any("Partner (5 row(s))" in e for e in r.evidence)


def test_a_null_is_absence_rather_than_a_violation(sales):
    """The same rule the reference check follows, and the same reason: a
    predicate does not see a NULL, so a check that failed them would be
    reporting something it never measured."""
    r = rules.domain_checks(sales, "sales", {"region": ["North", "South"]})[0]
    assert r.not_checked == 8
    assert r.passed + r.failed + r.not_checked == 186


def test_a_domain_on_a_column_the_table_lacks_is_not_run(sales):
    r = rules.domain_checks(sales, "sales", {"nope": ["a"]})[0]
    assert r.outcome is Outcome.NOT_RUN
    assert "not a column of sales" in r.not_run_because


def test_an_empty_declared_set_is_not_run_rather_than_failing_everything(sales):
    """Declaring no allowed values is a mistake, not a constraint. Failing
    every row would be a report full of findings about a typo."""
    r = rules.domain_checks(sales, "sales", {"channel": []})[0]
    assert r.outcome is Outcome.NOT_RUN
    assert "would fail every row" in r.not_run_because


def test_a_dataset_declaring_neither_gets_neither_check(sales):
    assert rules.reference_checks(sales, "sales", []) == []
    assert rules.domain_checks(sales, "sales", {}) == []
