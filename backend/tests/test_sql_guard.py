"""What caller-supplied SQL text is allowed to be.

Phase 8, Step 2. Run from the repo root:

    uv run pytest tests/test_sql_guard.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.util.sql_guard import (  # noqa: E402
    UnsafeSQL,
    bind_predicate,
    check_predicate,
    negate,
    quote_identifier,
)

# The four rows Step 1 measured on: one the rule excludes, one it cannot judge.
EX = """SELECT * FROM (VALUES
    (1,'cancelled',10),(2,'shipped',20),(3,NULL,30),(4,'delivered',40)
) v(id, status, amt)"""


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE ex AS {EX}")
    yield c
    c.close()


# --- quote_identifier


def test_a_plain_name_is_quoted():
    assert quote_identifier("region") == '"region"'


def test_a_name_carrying_a_quote_has_it_doubled():
    """DuckDB 1.5.5 has no quote_identifier and format() does not quote."""
    assert quote_identifier('we"ird') == '"we""ird"'


def test_an_empty_name_is_refused():
    with pytest.raises(UnsafeSQL):
        quote_identifier("")


def test_a_quoted_name_survives_a_round_trip(con):
    con.execute('CREATE TABLE "odd name" AS SELECT 1 AS x')
    assert con.execute(
        f"SELECT count(*) FROM {quote_identifier('odd name')}"
    ).fetchall() == [(1,)]


# --- check_predicate


def test_an_ordinary_rule_passes():
    assert check_predicate("status = 'cancelled'") == "status = 'cancelled'"


def test_the_injection_step_1_measured_is_refused():
    """P8-D7. This exact string dropped a table through con.execute."""
    with pytest.raises(UnsafeSQL) as e:
        check_predicate("1=1); DROP TABLE ex; --")
    assert "statements" in str(e.value)


def test_a_trailing_statement_is_refused():
    with pytest.raises(UnsafeSQL):
        check_predicate("status = 'x'; DELETE FROM ex")


def test_a_line_comment_cannot_swallow_the_wrapper():
    """The wrapper parenthesises, so a rule ending in -- fails to parse."""
    with pytest.raises(UnsafeSQL):
        check_predicate("status = 'x' --")


def test_unparseable_text_is_refused():
    with pytest.raises(UnsafeSQL):
        check_predicate(")(")


def test_an_empty_rule_is_refused():
    with pytest.raises(UnsafeSQL):
        check_predicate("   ")


def test_nothing_is_executed_while_checking(con):
    """The guard parses. A table named in a refused rule is still there."""
    with pytest.raises(UnsafeSQL):
        check_predicate("1=1); DROP TABLE ex; --")
    assert con.execute("SELECT count(*) FROM ex").fetchall() == [(4,)]


# --- bind_predicate


def test_a_rule_that_binds_is_returned(con):
    assert bind_predicate(con, "ex", "status = 'cancelled'") == "status = 'cancelled'"


def test_a_rule_naming_a_column_that_is_gone_is_refused(con):
    with pytest.raises(UnsafeSQL) as e:
        bind_predicate(con, "ex", "statuss = 'cancelled'")
    assert "statuss" in str(e.value)


def test_a_rule_that_is_not_a_test_is_refused(con):
    """G9: WHERE NOT (amt) binds and returns zero rows -- a filter that removes
    everything and reports nothing wrong."""
    with pytest.raises(UnsafeSQL) as e:
        bind_predicate(con, "ex", "amt")
    assert "type INTEGER, not BOOLEAN" in str(e.value)


def test_an_empty_table_types_nothing_and_refuses_nothing(con):
    con.execute("CREATE TABLE empty_ex AS SELECT * FROM ex WHERE false")
    assert bind_predicate(con, "empty_ex", "status = 'cancelled'")


def test_binding_reaches_no_connection_until_the_parse_passes(con):
    with pytest.raises(UnsafeSQL):
        bind_predicate(con, "ex", "1=1); DROP TABLE ex; --")
    assert con.execute("SELECT count(*) FROM ex").fetchall() == [(4,)]


# --- negate


def test_the_obvious_negation_loses_the_row_nobody_judged(con):
    """P8-D6, the measurement this function exists for."""
    kept = con.execute(
        "SELECT count(*) FROM ex WHERE NOT (status = 'cancelled')"
    ).fetchall()[0][0]
    assert kept == 2


def test_the_null_safe_negation_keeps_it(con):
    rule = "status = 'cancelled'"
    kept = con.execute(f"SELECT count(*) FROM ex WHERE {negate(rule)}").fetchall()[0][0]
    assert kept == 3


def test_the_three_counts_add_up_to_the_rows(con):
    """P7-D4 on a new problem: excluded, unjudgeable and kept sum to the total."""
    rule = "status = 'cancelled'"
    rows = con.execute("SELECT count(*) FROM ex").fetchall()[0][0]
    excluded = con.execute(
        f"SELECT count(*) FROM ex WHERE coalesce(({rule}), false)"
    ).fetchall()[0][0]
    unjudged = con.execute(
        f"SELECT count(*) FROM ex WHERE ({rule}) IS NULL"
    ).fetchall()[0][0]
    kept = con.execute(f"SELECT count(*) FROM ex WHERE {negate(rule)}").fetchall()[0][0]
    assert (excluded, unjudged, kept) == (1, 1, 3)
    assert excluded + kept == rows
    assert unjudged <= kept


# --- subqueries


def test_a_rule_that_opens_a_second_relation_is_refused():
    """Measured in Step 2: this is ONE statement, it parses, it binds, and it
    types as BOOLEAN. Every other check in the file passes it."""
    with pytest.raises(UnsafeSQL) as e:
        check_predicate(
            "1=1) OR (SELECT count(*) FROM read_csv('/etc/passwd')) > 0 AND (1=1"
        )
    assert "subquery" in str(e.value)


def test_a_subquery_against_the_same_table_is_refused_too(con):
    """Harmless here, and the guard cannot tell the difference -- the rule that
    reads /etc/passwd has the same shape."""
    with pytest.raises(UnsafeSQL):
        bind_predicate(con, "ex", "status = (SELECT max(status) FROM ex)")


def test_a_literal_containing_the_word_is_not_a_subquery(con):
    """The test walks the parse tree. A substring match on the serialized JSON
    would refuse this, and it is an ordinary rule."""
    assert bind_predicate(con, "ex", "status = 'SUBQUERY'")


def test_a_function_call_is_still_allowed(con):
    assert bind_predicate(con, "ex", "lower(status) = 'cancelled'")


def test_a_refusal_does_not_show_the_reader_a_query_they_did_not_write(con):
    """The binder's message carries the internal typeof() query with a caret
    under it. Naming a line nobody typed invites a correction to it."""
    with pytest.raises(UnsafeSQL) as e:
        bind_predicate(con, "ex", "statuss = 'cancelled'")
    message = str(e.value)
    assert "typeof" not in message
    assert "LINE 1" not in message
    assert "Candidate bindings" in message
