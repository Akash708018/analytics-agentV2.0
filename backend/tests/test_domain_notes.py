"""What propose_dataset_contract says about a controlled vocabulary.

Phase 7, Step 14. Run from the repo root:

    uv run pytest tests/test_domain_notes.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.contract.propose import VOCABULARY_LIMIT, propose_contract  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "domain_notes_test"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "broken_sales.csv"


@pytest.fixture()
def ws():
    workspace.reset(WORKSPACE)
    yield WORKSPACE
    workspace.reset(WORKSPACE)


def notes_for(ws, sql: str, name: str = "t", **kw) -> list[str]:
    con = db.connect(ws)
    try:
        con.execute(f'CREATE TABLE "{name}" AS {sql}')
        return [
            n for n in propose_contract(con, name, **kw).notes
            if "Declare domains=" in n
        ]
    finally:
        con.close()


# Every region repeats. A column that appears once per row is an identifier,
# not a vocabulary, and the rule excludes it on exactly that ground.
FIVE = """SELECT * FROM (VALUES
    ('a', 'North', 1), ('b', 'South', 2), ('c', 'East', 3),
    ('d', 'West', 4), ('e', 'Central', 5), ('f', 'North', 6),
    ('g', 'South', 7), ('h', 'East', 8)) v(k, region, n)"""


def test_a_low_cardinality_column_has_its_values_reported(ws):
    note = next(n for n in notes_for(ws, FIVE) if n.startswith("region"))
    assert "region holds 5 distinct value(s)" in note
    assert "Central, East, North, South, West" in note


def test_the_note_asks_rather_than_declares(ws):
    """analysis_window's rule on a second field: reported, never proposed."""
    note = next(n for n in notes_for(ws, FIVE) if n.startswith("region"))
    assert 'Declare domains={"region": [...]}' in note
    assert "another value is legal and merely absent" in note


def test_nothing_is_written_into_the_contract(ws):
    """A domain read off the column it constrains validates the column against
    itself and passes by construction."""
    con = db.connect(ws)
    try:
        con.execute(f"CREATE TABLE t AS {FIVE}")
        contract = propose_contract(con, "t").contract
        assert contract.domains == {}
        assert "domains" not in contract.unresolved
    finally:
        con.close()


def test_a_declared_column_is_not_reported_again(ws):
    notes = notes_for(ws, FIVE, domains={"region": ["North", "South"]})
    assert not any(n.startswith("region") for n in notes)


def test_a_high_cardinality_column_is_left_alone(ws):
    values = ", ".join(
        f"('k{i}', 'v{i % (VOCABULARY_LIMIT + 3)}', {i})"
        for i in range((VOCABULARY_LIMIT + 3) * 2)
    )
    notes = notes_for(ws, f"SELECT * FROM (VALUES {values}) v(k, label, n)")
    assert not any(n.startswith("label") for n in notes)


def test_a_column_that_never_repeats_is_an_identifier_not_a_vocabulary(ws):
    """Five rows, five regions. Counting cannot tell a vocabulary nobody has
    repeated yet from a name for a row, so it says nothing."""
    notes = notes_for(ws, """SELECT * FROM (VALUES
        ('a', 'North', 1), ('b', 'South', 2), ('c', 'East', 3)) v(k, region, n)""")
    assert not any(n.startswith("region") for n in notes)


def test_a_numeric_column_is_left_alone(ws):
    """evidence.suggest_role already says a 1-5 integer could be a rating to
    average or a bucket to group by, and only a person knows which."""
    notes = notes_for(ws, """SELECT * FROM (VALUES
        ('a', 3, 1), ('b', 5, 2), ('c', 3, 3), ('d', 5, 4)) v(k, rating, n)""")
    assert not any(n.startswith("rating") for n in notes)


def test_a_constant_column_is_left_alone(ws):
    """One value is not a vocabulary, it is a fact about the load."""
    notes = notes_for(ws, """SELECT * FROM (VALUES
        ('a', 'GBP', 1), ('b', 'GBP', 2)) v(k, currency, n)""")
    assert not any(n.startswith("currency") for n in notes)


def test_the_note_shows_the_errors_mixed_in_with_the_vocabulary(ws):
    """The argument for reporting rather than proposing, demonstrated by the
    fixture rather than asserted.

    On broken_sales.csv the note lists Nord, Nrth-West and Souh beside the four
    real regions. Anybody who pasted that list back would declare the
    misspellings legal, and the check built on it would pass over them.
    """
    if not FIXTURE.exists():
        pytest.skip(f"{FIXTURE.name} not generated; see make_fixtures.py")
    con = db.connect(ws)
    try:
        con.execute(
            f"CREATE TABLE sales AS SELECT * FROM read_csv_auto('{FIXTURE}')"
        )
        note = next(
            n for n in propose_contract(con, "sales").notes
            if n.startswith("region holds")
        )
    finally:
        con.close()
    assert "Nord" in note and "Souh" in note and "Nrth-West" in note
    assert "North" in note and "South" in note
