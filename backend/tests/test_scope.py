"""Which rows an analysis may see.

Phase 8, Step 4. Run from the repo root:

    uv run pytest tests/test_scope.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis.base import (  # noqa: E402
    Scope, ScopeError, scope_for, window_clause,
)
from backend.engine.util.sql_guard import UnsafeSQL  # noqa: E402


# Stand-ins for the contract objects, so this file tests the arithmetic rather
# than pydantic. The real Gate is passed in production; only the attribute
# names matter here and they are the ones dataset_contract.py declares.
@dataclass
class FakeExclusion:
    rule: str
    reason: str = "not real revenue"
    row_count: int | None = None


@dataclass
class FakeWindow:
    start: date
    end: date


@dataclass
class FakeContract:
    dataset_name: str
    date_column: str | None = "ts"
    analysis_window: FakeWindow | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)


@dataclass
class FakeGate:
    contract: FakeContract


# Every awkward case at once: cancelled, NULL status, the last second of the
# window, a row just past it, and two rows with no date at all.
SEVEN = """SELECT * FROM (VALUES
 (1,'shipped',   TIMESTAMP '2024-06-01 10:00:00', 10),
 (2,'cancelled', TIMESTAMP '2024-06-02 10:00:00', 20),
 (3,NULL,        TIMESTAMP '2024-06-03 10:00:00', 30),
 (4,'shipped',   TIMESTAMP '2024-12-31 23:59:59', 40),
 (5,'shipped',   TIMESTAMP '2025-01-01 00:00:00', 50),
 (6,'shipped',   NULL,                            60),
 (7,'cancelled', NULL,                            70)
) v(id, status, ts, amt)"""


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE sales AS {SEVEN}")
    yield c
    c.close()


def gate(**kw) -> FakeGate:
    kw.setdefault("dataset_name", "sales")
    return FakeGate(contract=FakeContract(**kw))


FULL_YEAR = FakeWindow(start=date(2024, 1, 1), end=date(2024, 12, 31))
CANCELLED = FakeExclusion(rule="status = 'cancelled'")


def test_the_four_numbers_sum_to_the_row_count(con):
    s = scope_for(con, gate(analysis_window=FULL_YEAR, known_exclusions=[CANCELLED]))
    assert (s.rows, s.excluded, s.outside_window, s.no_date, s.analysed) == (7, 2, 1, 1, 3)
    assert s.excluded + s.outside_window + s.no_date + s.analysed == s.rows


def test_the_undated_row_is_counted_once(con):
    """The bug this module was written around. Written
    `NOT coalesce((window), false)` the undated row lands in outside_window AND
    in no_date, and seven rows sum to eight."""
    s = scope_for(con, gate(analysis_window=FULL_YEAR, known_exclusions=[CANCELLED]))
    assert s.outside_window == 1
    assert s.no_date == 1


def test_the_row_no_rule_could_judge_is_kept_and_counted(con):
    """P8-D6. Row 3's status is NULL; the naive negation drops it silently."""
    s = scope_for(con, gate(analysis_window=FULL_YEAR, known_exclusions=[CANCELLED]))
    assert s.rule_unknown == 1
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM sales WHERE {s.where} ORDER BY id").fetchall()]
    assert ids == [1, 3, 4]


def test_the_last_second_of_the_window_is_inside_it(con):
    """P7-D2. Written `<= end` row 4 disappears and nothing says so."""
    s = scope_for(con, gate(analysis_window=FULL_YEAR, known_exclusions=[CANCELLED]))
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM sales WHERE {s.where} ORDER BY id").fetchall()]
    assert 4 in ids


def test_a_contract_with_no_window_analyses_everything_it_kept(con):
    s = scope_for(con, gate(known_exclusions=[CANCELLED]))
    assert (s.excluded, s.outside_window, s.no_date, s.analysed) == (2, 0, 0, 5)


def test_a_contract_with_no_exclusions_excludes_nothing(con):
    s = scope_for(con, gate(analysis_window=FULL_YEAR))
    assert (s.excluded, s.rule_unknown) == (0, 0)
    assert s.analysed == 4


def test_two_exclusions_are_both_applied(con):
    s = scope_for(con, gate(
        analysis_window=FULL_YEAR,
        known_exclusions=[CANCELLED, FakeExclusion(rule="amt > 35", reason="test")],
    ))
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM sales WHERE {s.where} ORDER BY id").fetchall()]
    assert ids == [1, 3]


def test_an_injected_rule_never_reaches_the_connection(con):
    """sql_guard, wired in rather than assumed."""
    with pytest.raises(UnsafeSQL):
        scope_for(con, gate(known_exclusions=[
            FakeExclusion(rule="1=1); DROP TABLE sales; --")]))
    assert con.execute("SELECT count(*) FROM sales").fetchall() == [(7,)]


def test_a_rule_that_is_not_a_test_is_refused(con):
    with pytest.raises(UnsafeSQL):
        scope_for(con, gate(known_exclusions=[FakeExclusion(rule="amt")]))


def test_a_scope_that_loses_a_row_will_not_construct():
    with pytest.raises(ScopeError) as e:
        Scope(dataset_name="sales", where="true", rows=100,
              excluded=10, outside_window=5, no_date=0, analysed=80)
    assert "loses rows" in str(e.value)


def test_the_method_note_states_every_number_it_has(con):
    s = scope_for(con, gate(
        analysis_window=FULL_YEAR, known_exclusions=[CANCELLED],
        excluded_columns=["internal_note"]))
    note = s.method_note()
    assert "3 of 7 row(s) analysed" in note
    assert "2 excluded by the contract" in note
    assert "1 outside 2024-01-01 to 2024-12-31" in note
    assert "1 undated" in note
    assert "1 kept although an exclusion rule could not judge them" in note
    assert "internal_note" in note


def test_a_clean_scope_says_only_what_happened(con):
    con.execute("CREATE TABLE clean AS SELECT * FROM sales WHERE id IN (1, 4)")
    s = scope_for(con, gate(dataset_name="clean", analysis_window=FULL_YEAR))
    assert s.method_note() == "2 of 2 row(s) analysed."


def test_the_window_clause_quotes_its_column():
    clause = window_clause('we"ird', date(2024, 1, 1), date(2024, 12, 31))
    assert '"we""ird"' in clause
    assert "+ INTERVAL 1 DAY" in clause
