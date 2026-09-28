"""clean/sql.py: the SQL shown is the SQL run.

Phase 6, Step 4. Run from the repo root:

    uv run pytest tests/test_cleaning_sql.py -q

Every renderer is executed against a real table rather than string-matched.
A statement that parses is not the claim; the claim is that the count and the
change agree, so the counts are taken, the statement is run, and the result is
compared against what the count promised.

The table mimics mixed_types.xlsx under all_text=True at 1/1000 scale, with the
same two failure modes Step 2 measured: a declared missing token in one numeric
column and an undeclared value in another.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.clean import sql  # noqa: E402
from backend.engine.clean.plan import ActionKind  # noqa: E402

TOKENS = ["", "NA", "N/A", "-", "--", "null", "NULL", "None"]


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(
        """
        CREATE TABLE src AS SELECT * FROM (VALUES
          ('ORD-1', 'North',  '10',         '9.50'),
          ('ORD-2', 'NORTH ', '20',         'not priced'),
          ('ORD-3', 'north',  'n/a',        '11.00'),
          ('ORD-4', 'South',  '40',         '12.25'),
          ('ORD-5', 'South',  '50',         'not priced'),
          ('ORD-5', 'South',  '50',         'not priced')
        ) t(order_id, region, units, unit_price)
        """
    )
    yield c
    c.close()


def one(con, query: str):
    return con.execute(query).fetchone()[0]


# --------------------------------------------------------------------------
# the property this module exists for
# --------------------------------------------------------------------------


ALL_RENDERINGS = [
    ("convert", dict(kind=ActionKind.CONVERT_TYPE, column="units",
                     to_type="BIGINT", missing_tokens=TOKENS)),
    ("missing", dict(kind=ActionKind.NORMALISE_MISSING, column="region",
                     tokens=["N/A"])),
    ("trim", dict(kind=ActionKind.TRIM_WHITESPACE, column="region")),
    ("case", dict(kind=ActionKind.NORMALISE_CASE, column="region")),
    ("dedupe", dict(kind=ActionKind.DROP_DUPLICATE_ROWS)),
    ("exclude", dict(kind=ActionKind.EXCLUDE_COLUMN, column="region")),
]


@pytest.mark.parametrize("label,kw", ALL_RENDERINGS, ids=[a for a, _ in ALL_RENDERINGS])
def test_the_expression_appears_in_the_statement_it_builds(label, kw):
    """The whole point. A count built from a different expression than the
    statement is a guess about a statement it has never met.

    EXCLUDE_COLUMN is the one kind where the loss query cannot share the
    projection -- `* EXCLUDE (col)` is what runs, and what is destroyed is
    counted on the column itself. It is called out here rather than excused
    by a looser assertion.
    """
    kw = dict(kw)
    kind = kw.pop("kind")
    r = sql.render(kind, source="src", target="tgt", **kw)
    assert r.expression in r.statement
    if r.lost_sql and kind is not ActionKind.EXCLUDE_COLUMN:
        assert r.expression in r.lost_sql


@pytest.mark.parametrize("label,kw", ALL_RENDERINGS, ids=[a for a, _ in ALL_RENDERINGS])
def test_every_rendered_string_actually_executes(con, label, kw):
    kw = dict(kw)
    kind = kw.pop("kind")
    r = sql.render(kind, source="src", target="tgt", **kw)
    assert isinstance(one(con, r.affected_sql), int)
    if r.lost_sql:
        assert isinstance(one(con, r.lost_sql), int)
    if r.sample_sql:
        con.execute(r.sample_sql).fetchall()
    con.execute(r.statement)
    assert one(con, "SELECT count(*) FROM tgt") > 0


def test_every_action_kind_has_a_renderer():
    """A kind detection can produce and this module cannot render is a
    proposal that dies at apply time."""
    for kind in ActionKind:
        assert kind in sql._RENDERERS


# --------------------------------------------------------------------------
# convert_type -- Step 2's finding, executable
# --------------------------------------------------------------------------


def test_convert_counts_the_undeclared_loss_only(con):
    """units holds 'n/a', which is declared. Nothing is lost."""
    r = sql.convert_type(source="src", target="tgt", column="units",
                         to_type="BIGINT", missing_tokens=TOKENS)
    assert one(con, r.lost_sql) == 0
    assert one(con, r.affected_sql) == 6


def test_convert_counts_an_undeclared_value_as_lost(con):
    """unit_price holds 'not priced', which is not declared. Three rows."""
    r = sql.convert_type(source="src", target="tgt", column="unit_price",
                         to_type="DECIMAL(18,2)", missing_tokens=TOKENS)
    assert one(con, r.lost_sql) == 3
    assert [x[0] for x in con.execute(r.sample_sql).fetchall()] == ["not priced"]


def test_the_loss_the_count_promised_is_the_loss_the_statement_causes(con):
    """The assertion this file exists for, end to end."""
    r = sql.convert_type(source="src", target="tgt", column="unit_price",
                         to_type="DECIMAL(18,2)", missing_tokens=TOKENS)
    promised = one(con, r.lost_sql)
    before = one(con, "SELECT count(*) FROM src WHERE unit_price IS NOT NULL")
    con.execute(r.statement)
    after = one(con, "SELECT count(*) FROM tgt WHERE unit_price IS NOT NULL")
    assert before - after == promised == 3


def test_convert_without_a_vocabulary_counts_every_failure_as_loss(con):
    """missing_tokens=[] switches the distinction off, and then 'n/a' is
    information like anything else. Phase 5's `missing_values=[]` does the
    same thing one layer down, deliberately."""
    r = sql.convert_type(source="src", target="tgt", column="units",
                         to_type="BIGINT", missing_tokens=[])
    assert one(con, r.lost_sql) == 1


def test_convert_preserves_column_position(con):
    """P6-D3, measured in Step 1. EXCLUDE would move units to the end and the
    fingerprint would read a value conversion as structural drift."""
    r = sql.convert_type(source="src", target="tgt", column="units",
                         to_type="BIGINT", missing_tokens=TOKENS)
    con.execute(r.statement)
    cols = [c[0] for c in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'tgt' ORDER BY ordinal_position").fetchall()]
    assert cols == ["order_id", "region", "units", "unit_price"]


# --------------------------------------------------------------------------
# the others
# --------------------------------------------------------------------------


def test_normalise_missing_is_never_lossy(con):
    r = sql.normalise_missing(source="src", target="tgt", column="region",
                              tokens=["North"])
    assert r.lost_sql is None
    # 'North', 'NORTH ' and 'north' all fold to NORTH once trimmed and upper-cased.
    # Three, not two -- the trim in the match is doing work the eye misses.
    assert one(con, r.affected_sql) == 3


def test_normalise_missing_with_no_tokens_is_refused():
    with pytest.raises(ValueError, match="changes nothing"):
        sql.normalise_missing(source="src", target="tgt", column="region",
                              tokens=[])


def test_trim_counts_only_the_padded_rows(con):
    r = sql.trim_whitespace(source="src", target="tgt", column="region")
    assert one(con, r.affected_sql) == 1
    assert r.lost_sql is None


def test_case_folding_reports_the_distinct_values_it_destroys(con):
    """Nothing becomes NULL and something is still lost: 'North', 'north' and
    'NORTH ' are three values in the source and fewer afterwards."""
    r = sql.normalise_case(source="src", target="tgt", column="region")
    assert one(con, r.lost_sql) == 1  # North/north merge; 'NORTH ' keeps its space
    assert set(x[0] for x in con.execute(r.sample_sql).fetchall()) == {
        "North", "north"
    }


def test_case_mode_is_checked():
    with pytest.raises(ValueError, match="'upper' or 'lower'"):
        sql.normalise_case(source="src", target="tgt", column="region",
                           mode="Title")


def test_dedupe_counts_the_rows_it_will_remove(con):
    r = sql.drop_duplicate_rows(source="src", target="tgt")
    assert one(con, r.affected_sql) == 1
    assert one(con, r.lost_sql) == 1
    con.execute(r.statement)
    assert one(con, "SELECT count(*) FROM tgt") == 5


def test_exclude_is_the_one_place_EXCLUDE_is_right(con):
    r = sql.exclude_column(source="src", target="tgt", column="region")
    assert "EXCLUDE" in r.statement
    con.execute(r.statement)
    cols = [c[0] for c in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'tgt' ORDER BY ordinal_position").fetchall()]
    assert cols == ["order_id", "units", "unit_price"]


# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------


def test_identifiers_are_quoted(con):
    """Phase 3's header assembly produces column names with spaces in them."""
    con.execute('CREATE TABLE odd AS SELECT \'x\' AS "order date"')
    r = sql.trim_whitespace(source="odd", target="tgt", column="order date")
    con.execute(r.statement)
    assert one(con, 'SELECT count(*) FROM tgt WHERE "order date" = \'x\'') == 1


def test_a_quote_in_a_token_does_not_break_the_statement(con):
    r = sql.normalise_missing(source="src", target="tgt", column="region",
                              tokens=["it's missing"])
    assert "'IT''S MISSING'" in r.statement
    con.execute(r.statement)


def test_tokens_are_matched_case_insensitively(con):
    """DEFAULT_NA_VALUES holds 'N/A' and the data holds 'n/a'. Step 2 measured
    that the whole declared/undeclared distinction rests on this."""
    assert sql.token_list(["n/a"]) == "'N/A'"
    r = sql.convert_type(source="src", target="tgt", column="units",
                         to_type="BIGINT", missing_tokens=["N/A"])
    assert one(con, r.lost_sql) == 0
