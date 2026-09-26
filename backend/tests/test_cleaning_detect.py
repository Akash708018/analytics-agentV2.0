"""clean/detect.py: what a table needs, found without touching it.

Phase 6, Step 5. Run from the repo root:

    uv run pytest tests/test_cleaning_detect.py -q

The fixture here is mixed_types.xlsx under all_text=True, rebuilt in SQL at
1/1000 scale with the same two failure modes Step 2 counted: 'n/a' in a numeric
column, which is a declared missing token, and 'not priced' in another, which is
not.

Three of these tests exist because DuckDB does something surprising and the
detector has to guard against it. They are the ones to read first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.clean import detect  # noqa: E402
from backend.engine.clean.plan import ActionKind  # noqa: E402

TOKENS = ["", "NA", "N/A", "-", "--", "null", "NULL", "None"]


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(
        """
        CREATE TABLE mixed AS SELECT * FROM (VALUES
          ('ORD-01', '2024-01-03 00:00:00', 'North',  '10',  '9.50',       'True'),
          ('ORD-02', '2024-01-04 00:00:00', 'North ', '20',  'not priced', 'False'),
          ('ORD-03', '2024-01-05 00:00:00', 'north',  'n/a', '11.00',      'True'),
          ('ORD-04', '2024-01-06 00:00:00', 'South',  '40',  '12.25',      'False'),
          ('ORD-05', '2024-01-07 00:00:00', 'N/A',    '50',  '13.00',      'True'),
          ('ORD-06', '2024-01-08 00:00:00', 'South',  '60',  '14.00',      'False'),
          ('ORD-07', '2024-01-09 00:00:00', 'East',   '70',  '15.75',      'True'),
          ('ORD-08', '2024-01-10 00:00:00', 'East',   '80',  '16.00',      'False'),
          ('ORD-09', '2024-01-11 00:00:00', 'West',   '90',  '17.50',      'True'),
          ('ORD-10', '2024-01-12 00:00:00', 'West',   '11',  '18.00',      'False'),
          ('ORD-11', '2024-01-13 00:00:00', 'South',  '12',  '19.25',      'True'),
          ('ORD-12', '2024-01-14 00:00:00', 'East',   '13',  '20.00',      'False')
        ) t(order_id, order_date, region, units, unit_price, is_return)
        """
    )
    yield c
    c.close()


def kinds(actions):
    return [(a.kind, a.column) for a in actions]


def by_column(actions, column, kind):
    return next(
        a for a in actions if a.column == column and a.kind is kind
    )


# --------------------------------------------------------------------------
# the three guards, each against something DuckDB actually does
# --------------------------------------------------------------------------


def test_a_price_column_is_never_proposed_as_an_integer(con):
    """TRY_CAST('9.50' AS BIGINT) is 10.

    So a column of prices parses 100% as BIGINT and rounds every value. "Does
    it parse" is not a signal, and this is the guard: BIGINT is only proposed
    when no value loses a fractional part.
    """
    assert con.execute(
        "SELECT TRY_CAST('9.50' AS BIGINT)"
    ).fetchone()[0] == 10  # the behaviour being guarded against
    assert detect.proposed_type(con, "mixed", "unit_price") == "DECIMAL(18,2)"


def test_an_integer_column_is_not_proposed_as_a_boolean(con):
    """TRY_CAST('1' AS BOOLEAN) is TRUE.

    An integer column of ones and zeroes parses perfectly as boolean, and
    proposing that would turn counts into flags. BOOLEAN needs the words.
    """
    assert con.execute("SELECT TRY_CAST('1' AS BOOLEAN)").fetchone()[0] is True
    con.execute("CREATE TABLE flags AS SELECT * FROM (VALUES ('1'),('0'),('1')) t(v)")
    assert detect.proposed_type(con, "flags", "v") == "BIGINT"
    assert detect.proposed_type(con, "mixed", "is_return") == "BOOLEAN"


def test_a_midnight_column_is_a_date_and_a_timed_one_is_a_timestamp(con):
    """TRY_CAST will produce a DATE from a timestamp string and drop the time.

    So DATE is only proposed when there is no time to drop.
    """
    assert detect.proposed_type(con, "mixed", "order_date") == "DATE"
    con.execute(
        "CREATE TABLE timed AS SELECT * FROM "
        "(VALUES ('2024-01-03 13:20:00'),('2024-01-04 00:00:00')) t(v)"
    )
    assert detect.proposed_type(con, "timed", "v") == "TIMESTAMP"


# --------------------------------------------------------------------------
# type proposal
# --------------------------------------------------------------------------


def test_an_identifier_column_is_left_alone(con):
    assert detect.proposed_type(con, "mixed", "order_id") is None


def test_a_column_of_words_is_left_alone(con):
    assert detect.proposed_type(con, "mixed", "region") is None


def test_the_declared_token_does_not_stop_a_conversion(con):
    """units holds 'n/a' once in five. Four fifths is below the threshold on
    this toy, so the share is checked directly rather than through detect()."""
    assert detect.CONVERT_MIN_SHARE == 0.90
    con.execute(
        "CREATE TABLE units_only AS SELECT * FROM "
        "(VALUES ('10'),('20'),('30'),('40'),('50'),('60'),('70'),('80'),"
        "('90'),('n/a')) t(v)"
    )
    assert detect.proposed_type(con, "units_only", "v") == "BIGINT"


def test_an_empty_column_proposes_nothing(con):
    con.execute("CREATE TABLE blank AS SELECT NULL::VARCHAR AS v")
    assert detect.proposed_type(con, "blank", "v") is None


def test_only_text_columns_are_looked_at(con):
    con.execute("CREATE TABLE typed AS SELECT 1 AS n, 'x' AS s")
    assert detect.text_columns(con, "typed") == ["s"]


# --------------------------------------------------------------------------
# detect(): the counts come from the rendering, and nothing is decided
# --------------------------------------------------------------------------


def test_the_loss_is_counted_only_for_undeclared_values(con):
    """Step 2's whole finding, arriving as two actions on one table."""
    actions = detect.detect(con, source="mixed", target="mixed_v2",
                            missing_tokens=TOKENS)
    price = by_column(actions, "unit_price", ActionKind.CONVERT_TYPE)
    assert price.values_lost == 1
    assert price.sample == ("not priced",)


def test_a_declared_token_costs_nothing(con):
    con.execute(
        "CREATE TABLE units_only AS SELECT * FROM "
        "(VALUES ('10'),('20'),('30'),('40'),('50'),('60'),('70'),('80'),"
        "('90'),('n/a')) t(v)"
    )
    actions = detect.detect(con, source="units_only", target="x",
                            missing_tokens=TOKENS)
    convert = by_column(actions, "v", ActionKind.CONVERT_TYPE)
    assert convert.values_lost == 0
    assert convert.sample == ()


def test_declared_tokens_get_their_own_action(con):
    actions = detect.detect(con, source="mixed", target="mixed_v2",
                            missing_tokens=TOKENS)
    a = by_column(actions, "region", ActionKind.NORMALISE_MISSING)
    assert "1 declared missing token" in a.intent
    assert a.values_lost == 0


def test_padding_is_found(con):
    actions = detect.detect(con, source="mixed", target="mixed_v2",
                            missing_tokens=TOKENS)
    a = by_column(actions, "region", ActionKind.TRIM_WHITESPACE)
    assert a.rows_affected == 1


def test_case_folding_reports_its_loss_in_distinct_values(con):
    """P6-D10 arriving where it was predicted. Nothing becomes NULL and the
    action still destroys something, so the unit is not "value"."""
    actions = detect.detect(con, source="mixed", target="mixed_v2",
                            missing_tokens=TOKENS)
    a = by_column(actions, "region", ActionKind.NORMALISE_CASE)
    assert a.loss_unit == "distinct value"
    assert a.values_lost == 1
    assert "NULL" not in a.line()
    assert "1 distinct value(s) will be discarded" in a.line()


def test_duplicates_are_a_table_level_action_counted_in_rows(con):
    con.execute("INSERT INTO mixed SELECT * FROM mixed ORDER BY order_id LIMIT 2")
    actions = detect.detect(con, source="mixed", target="mixed_v2",
                            missing_tokens=TOKENS)
    dupe = next(a for a in actions if a.kind is ActionKind.DROP_DUPLICATE_ROWS)
    assert dupe.column is None
    assert dupe.loss_unit == "row"
    assert dupe.values_lost == 2


def test_a_clean_table_proposes_nothing(con):
    con.execute(
        "CREATE TABLE tidy AS SELECT * FROM "
        "(VALUES ('a','x'),('b','y')) t(k, v)"
    )
    assert detect.detect(con, source="tidy", target="t2",
                         missing_tokens=TOKENS) == []


def test_excluding_a_column_is_never_proposed(con):
    """A column that is entirely null may be the one that matters and the feed
    is broken. Nothing in a table's contents can suggest dropping it."""
    con.execute("ALTER TABLE mixed ADD COLUMN note VARCHAR")
    actions = detect.detect(con, source="mixed", target="mixed_v2",
                            missing_tokens=TOKENS)
    assert ActionKind.EXCLUDE_COLUMN not in {a.kind for a in actions}


# --------------------------------------------------------------------------
# the properties a plan depends on
# --------------------------------------------------------------------------


def test_ids_are_sequential_and_unique(con):
    actions = detect.detect(con, source="mixed", target="mixed_v2",
                            missing_tokens=TOKENS)
    ids = [a.action_id for a in actions]
    assert ids == [f"C{i:03d}" for i in range(1, len(ids) + 1)]


def test_two_runs_against_an_unchanged_table_give_the_same_ids(con):
    """An id that moves between proposals is an id nobody can approve."""
    first = detect.detect(con, source="mixed", target="v2", missing_tokens=TOKENS)
    second = detect.detect(con, source="mixed", target="v2", missing_tokens=TOKENS)
    assert kinds(first) == kinds(second)
    assert [a.action_id for a in first] == [a.action_id for a in second]


def test_every_action_carries_runnable_sql(con):
    actions = detect.detect(con, source="mixed", target="mixed_v2",
                            missing_tokens=TOKENS)
    assert actions
    for a in actions:
        con.execute(a.sql)
        assert con.execute("SELECT count(*) FROM mixed_v2").fetchone()[0] > 0


def test_detection_writes_nothing(con):
    before = sorted(r[0] for r in con.execute("SHOW TABLES").fetchall())
    detect.detect(con, source="mixed", target="mixed_v2", missing_tokens=TOKENS)
    after = sorted(r[0] for r in con.execute("SHOW TABLES").fetchall())
    assert before == after
    assert con.execute("SELECT count(*) FROM mixed").fetchone()[0] == 12


def test_switching_the_vocabulary_off_turns_a_token_into_a_loss(con):
    """missing_values=[] one layer down does the same thing, deliberately."""
    actions = detect.detect(con, source="mixed", target="v2", missing_tokens=[])
    assert not any(a.kind is ActionKind.NORMALISE_MISSING for a in actions)
    price = by_column(actions, "unit_price", ActionKind.CONVERT_TYPE)
    assert price.values_lost == 1


# --- codes and currency (Cleanup Step 12, RF-O5 and A3) ------------------------------------------

@pytest.fixture()
def coded():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE coded AS SELECT * FROM (VALUES
      ('000435', '₹1,234.00', '12'), ('000021', '₹990.00', '7'), ('001200', '₹10,846.50', '3'),
      ('000009', '₹58.00', '41')) t(sku, list_price, qty)""")
    yield c
    c.close()


def test_a_zero_padded_code_is_not_offered_as_a_number(coded):
    """Retail C003: sku '000435' offered as BIGINT, 'discards nothing', and recommended."""
    acts = detect.detect(coded, source="coded", target="coded", missing_tokens=[])
    assert (ActionKind.CONVERT_TYPE, "sku") not in kinds(acts)
    assert (ActionKind.CONVERT_TYPE, "qty") in kinds(acts), "a plain integer column still is"


def test_a_currency_column_is_offered_with_the_symbols_removed(coded):
    acts = detect.detect(coded, source="coded", target="coded", missing_tokens=[])
    price = next(a for a in acts if a.kind is ActionKind.CONVERT_TYPE and a.column == "list_price")
    assert "currency" in price.intent and "regexp_replace" in price.sql
    assert price.values_lost == 0
    coded.execute(price.sql)   # the statement a person approves, run as they would approve it
    got = [float(r[0]) for r in coded.execute("SELECT list_price FROM coded").fetchall()]
    assert got == [1234.0, 990.0, 10846.5, 58.0]


def test_a_date_column_in_two_formats_is_read_by_both():
    """Retail C004 (recheck, 25/09/2026): delivery_date was 98.1% yyyy-mm-dd and 3,030 dd/mm/yyyy,
    and the plan offered the plain DATE cast discarding all 3,030 -- while the formats reading,
    never tried once the cast passed its share, reads every one of them."""
    c = duckdb.connect(":memory:")
    iso = ", ".join(f"('2024-01-{d:02d}')" for d in range(1, 31))
    c.execute(f"CREATE TABLE d AS SELECT * FROM (VALUES {iso}, ('25/12/2024'), ('01/12/2024')) "
              f"t(delivered)")
    acts = detect.detect(c, source="d", target="d", missing_tokens=[])
    conv = [a for a in acts if a.kind is ActionKind.CONVERT_TYPE and a.column == "delivered"]
    assert len(conv) == 1 and "date formats" in conv[0].intent and "day first" in conv[0].intent
    assert conv[0].values_lost == 0
    c.execute(conv[0].sql)
    got = {str(r[0]) for r in c.execute("SELECT delivered FROM d").fetchall()}
    assert {"2024-12-25", "2024-12-01", "2024-01-30"} <= got and None not in got


def test_a_date_column_the_cast_reads_whole_keeps_the_cast():
    c = duckdb.connect(":memory:")
    iso = ", ".join(f"('2024-01-{d:02d}')" for d in range(1, 31))
    c.execute(f"CREATE TABLE d AS SELECT * FROM (VALUES {iso}) t(delivered)")
    acts = detect.detect(c, source="d", target="d", missing_tokens=[])
    conv = [a for a in acts if a.kind is ActionKind.CONVERT_TYPE and a.column == "delivered"]
    assert len(conv) == 1 and conv[0].intent == "read delivered as DATE"
