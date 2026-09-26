"""
Unit tests for profile/table_profile.py.

Bare in-memory connections, as in test_evidence.py and test_propose.py:
profiling stores nothing, so a workspace connection would only put a file on
disk. The day one of these fails on a missing `_agent_datasets`, the profiler
has started writing.

The missing-value assertions are the ones to read first. The vocabulary here is
the pandas/pyarrow intersection, and several of these tests exist to pin what
is deliberately NOT in it -- an earlier version reasoned its way to a wider list
and reported a six-row fixture as 83% missing when the defensible figure was
67%.
"""

from __future__ import annotations

import duckdb
import pytest

from backend.engine.contract import ContractRefused
from backend.engine.profile.table_profile import (
    MISSING_VALUES,
    MOSTLY_MISSING,
    OUTLIER_K,
    TYPE_MISMATCH_SHARE,
    OutlierSummary,
    TableProfile,
    profile_table,
)


@pytest.fixture
def con():
    c = duckdb.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def items(con):
    """Clean line items: no nulls, no blanks, no duplicates."""
    con.execute(
        """
        CREATE TABLE order_items AS
        SELECT
          'ORD-' || lpad(((i // 3) + 1)::VARCHAR, 6, '0') AS order_id,
          ((i % 3) + 1)::INTEGER                          AS order_item_id,
          'SELL-' || ((i % 40) + 1)::VARCHAR              AS seller_id,
          (DATE '2024-01-01' + ((i % 300)::INTEGER))      AS order_date,
          ((i % 97) + 1)::DOUBLE                          AS price
        FROM range(300) t(i)
        """
    )
    return con


@pytest.fixture
def gaps(con):
    """
    The shape of gaps_and_dupes.csv, small enough to count by hand.

    Six rows. region: 3 blank-or-whitespace, 1 'N/A', 1 'unknown', 1 'North'.
    revenue: 2 null. Rows 4 and 5 are exact duplicates including their nulls.
    """
    con.execute(
        """
        CREATE TABLE gaps AS SELECT * FROM (VALUES
          ('ORD-1', 39, 'North',   CAST(5841.66 AS DECIMAL(10,2))),
          ('ORD-2',  4, 'N/A',     CAST(2829.54 AS DECIMAL(10,2))),
          ('ORD-3',  7, '  ',      CAST( 100.00 AS DECIMAL(10,2))),
          ('ORD-4', 11, '',        NULL),
          ('ORD-4', 11, '',        NULL),
          ('ORD-5', 11, 'unknown', CAST(  50.00 AS DECIMAL(10,2)))
        ) v(order_id, units, region, revenue)
        """
    )
    return con


# --------------------------------------------------------------------------
# the scan, and what it reuses
# --------------------------------------------------------------------------

def test_the_row_count_comes_from_evidence(items):
    assert profile_table(items, "order_items").row_count == 300


def test_every_column_appears_once_in_table_order(items):
    p = profile_table(items, "order_items")
    assert [c.name for c in p.columns] == [
        "order_id", "order_item_id", "seller_id", "order_date", "price",
    ]


def test_the_null_counts_are_evidences_own(gaps):
    """
    Not recomputed. Two implementations of 'how many nulls in revenue' would
    eventually disagree inside one chat, with nothing to say which is lying.
    """
    from backend.engine.contract.evidence import column_stats

    p = profile_table(gaps, "gaps")
    by_name = {c.name: c for c in column_stats(gaps, "gaps")}
    for c in p.columns:
        assert c.evidence.null_count == by_name[c.name].null_count


def test_a_numeric_column_gets_a_summary(items):
    assert profile_table(items, "order_items").column("price").numeric is not None


def test_a_text_column_gets_no_numeric_summary(items):
    assert profile_table(items, "order_items").column("seller_id").numeric is None


def test_a_date_column_gets_no_numeric_summary(items):
    assert profile_table(items, "order_items").column("order_date").numeric is None


def test_the_quartiles_are_where_they_should_be(con):
    con.execute("CREATE TABLE t AS SELECT i::DOUBLE AS n FROM range(1, 101) s(i)")
    n = profile_table(con, "t").column("n").numeric
    assert n.median == pytest.approx(50.5)
    assert n.mean == pytest.approx(50.5)
    assert n.q1 == pytest.approx(25.75)
    assert n.q3 == pytest.approx(75.25)


def test_the_iqr_is_the_distance_between_the_quartiles(con):
    con.execute("CREATE TABLE t AS SELECT i::DOUBLE AS n FROM range(1, 101) s(i)")
    n = profile_table(con, "t").column("n").numeric
    assert n.iqr == pytest.approx(n.q3 - n.q1)


def test_an_all_null_numeric_column_has_no_statistics(con):
    """
    None, not zero. A zero here is a number nobody computed, and it would sit
    in a table looking exactly like a real one.
    """
    con.execute("CREATE TABLE t AS SELECT CAST(NULL AS DOUBLE) AS n FROM range(5)")
    n = profile_table(con, "t").column("n").numeric
    assert n.mean is None and n.median is None
    assert n.q1 is None and n.q3 is None and n.iqr is None


def test_a_single_row_has_a_mean_and_no_standard_deviation(con):
    """stddev_samp of one value is undefined and DuckDB returns NULL for it."""
    con.execute("CREATE TABLE t AS SELECT 7.0::DOUBLE AS n")
    n = profile_table(con, "t").column("n").numeric
    assert n.mean == pytest.approx(7.0)
    assert n.stddev is None


def test_the_role_comes_from_evidence_not_from_here(items):
    p = profile_table(items, "order_items")
    assert p.column("order_id").role == "identifier"
    assert p.column("order_id").role_reason


def test_an_empty_table_profiles_without_crashing(con):
    con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
    p = profile_table(con, "t")
    assert p.row_count == 0
    assert p.column_count == 2
    assert p.duplicate_rows == 0


def test_a_dataset_that_is_not_loaded_is_refused(con):
    con.execute("CREATE TABLE other AS SELECT 1 AS a")
    with pytest.raises(ContractRefused):
        profile_table(con, "nope")


def test_profiling_writes_nothing(items):
    before = {r[0] for r in items.execute("SHOW TABLES").fetchall()}
    profile_table(items, "order_items")
    assert {r[0] for r in items.execute("SHOW TABLES").fetchall()} == before


# --------------------------------------------------------------------------
# missing data a null count cannot see
# --------------------------------------------------------------------------

def test_blanks_are_counted(gaps):
    """Empty strings and whitespace-only, together: both are absence."""
    assert profile_table(gaps, "gaps").column("region").blank_count == 3


def test_a_standard_token_is_counted(gaps):
    assert profile_table(gaps, "gaps").column("region").missing_token_count == 1


def test_the_breakdown_names_which_token_appeared(gaps):
    """'N/A' and '#REF!' are not the same problem and should not read alike."""
    assert profile_table(gaps, "gaps").column("region").missing_token_breakdown == {
        "N/A": 1
    }


def test_matching_is_case_insensitive(con):
    """
    The one deliberate widening. pandas ships 'NA', 'n/a' and 'nan' as separate
    entries, implying exact matching, so 'Null' slips through it.
    """
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES ('NULL'), ('null'), ('Null'), "
        "('n/a'), ('real')) v(a)"
    )
    assert profile_table(con, "t").column("a").missing_token_count == 4


def test_unknown_is_not_treated_as_missing(gaps):
    """
    The finding this file exists to pin. 'unknown' is in neither pandas' nor
    pyarrow's default, and a status really can be unknown. Counting it reported
    this fixture as 83% missing when the defensible figure is 67%.
    """
    p = profile_table(gaps, "gaps")
    assert "UNKNOWN" not in p.column("region").missing_token_breakdown
    assert p.column("region").missing_count == 4
    assert p.column("region").missing_ratio == pytest.approx(4 / 6)


def test_the_other_judgement_calls_are_out_too(con):
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES ('none'), ('missing'), ('-'), "
        "('--'), ('?'), ('nil')) v(a)"
    )
    assert profile_table(con, "t").column("a").missing_token_count == 0


def test_the_vocabulary_is_the_published_intersection():
    """
    Pinned so a later widening is a deliberate edit rather than a drift. These
    are pandas 3.0.2's defaults intersected with pyarrow 25.0.1's, upper-cased,
    without the empty string.
    """
    assert set(MISSING_VALUES) == {
        "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NAN",
        "1.#IND", "1.#QNAN", "N/A", "NA", "NAN", "NULL",
    }


def test_missing_is_nulls_plus_everything_that_reads_as_one(gaps):
    region = profile_table(gaps, "gaps").column("region")
    assert region.evidence.null_count == 0
    assert region.hidden_missing == 4
    assert region.missing_count == 4


def test_hidden_missing_excludes_the_nulls(gaps):
    """revenue is genuinely null twice and holds no tokens."""
    revenue = profile_table(gaps, "gaps").column("revenue")
    assert revenue.evidence.null_count == 2
    assert revenue.hidden_missing == 0
    assert revenue.missing_count == 2


def test_a_wider_vocabulary_can_be_declared(con):
    """
    Frictionless' model: the list is a property of the data, not of the tool.
    A feed that writes NR is not a reason to widen the default for everyone.
    """
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('NR'), ('x')) v(a)")
    assert profile_table(con, "t").column("a").missing_token_count == 0
    widened = profile_table(con, "t", missing_values=["NR"])
    assert widened.column("a").missing_token_count == 1
    assert widened.missing_values == ("NR",)


def test_an_empty_vocabulary_switches_detection_off(con):
    """
    Frictionless again: [] means no conversion on any value, NOT 'use the
    default'. The zero that comes back is a real count.
    """
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('N/A'), ('x')) v(a)")
    p = profile_table(con, "t", missing_values=[])
    assert p.column("a").missing_token_count == 0
    assert p.missing_values == ()


def test_a_token_containing_a_quote_does_not_break_the_query(con):
    """The vocabulary can come from a person, so it is escaped, not trusted."""
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('it''s'), ('x')) v(a)")
    p = profile_table(con, "t", missing_values=["it's"])
    assert p.column("a").missing_token_count == 1


def test_numeric_columns_are_not_scanned_for_tokens(items):
    """A DOUBLE cannot hold 'N/A'. Scanning it would cost a pass for nothing."""
    assert profile_table(items, "order_items").column("price").blank_count == 0


def test_the_note_says_what_is_deliberately_not_counted(gaps):
    note = " ".join(profile_table(gaps, "gaps").notes)
    assert "'unknown'" in note
    assert "NOT counted" in note
    assert "Nothing was rewritten" in note


# --------------------------------------------------------------------------
# duplicate rows -- the third null rule
# --------------------------------------------------------------------------

def test_exact_duplicates_are_counted(gaps):
    assert profile_table(gaps, "gaps").duplicate_rows == 1


def test_a_duplicate_carrying_nulls_still_counts(con):
    """
    The whole reason this uses DISTINCT * rather than a count of DISTINCT
    columns. count(DISTINCT c) drops nulls; DISTINCT * does not, and two rows
    identical including their nulls collapse to one.
    """
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES (1, NULL), (1, NULL), (2, NULL)) v(a, b)"
    )
    assert profile_table(con, "t").duplicate_rows == 1


def test_a_clean_table_has_no_duplicates(items):
    assert profile_table(items, "order_items").duplicate_rows == 0


def test_a_failed_duplicate_count_does_not_lose_the_rest(items):
    """
    No real type breaks DISTINCT * on 1.5.5 -- LIST, MAP and UNION all survive
    it -- so the failure is induced here rather than pretended. What matters is
    that one uncomputable number does not cost the other thirty.
    """
    class Refuses:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if "DISTINCT *" in sql:
                raise RuntimeError("cannot compare values of this type")
            return self._real.execute(sql, *args)

    p = profile_table(Refuses(items), "order_items")
    assert p.duplicate_rows is None
    assert p.row_count == 300
    assert p.column("price").numeric is not None
    assert any("were not counted" in n for n in p.notes)
    assert "could not be counted" in " ".join(p.summary_lines())


# --------------------------------------------------------------------------
# what a reader gets
# --------------------------------------------------------------------------

def test_one_row_per_column_matching_the_headers(gaps):
    p = profile_table(gaps, "gaps")
    rows = p.to_rows()
    assert len(rows) == p.column_count
    assert all(len(r) == len(TableProfile.HEADERS) for r in rows)


def test_the_missing_story_comes_before_the_numeric_one(gaps):
    """
    The envelope previews twelve columns. Which twelve is a decision: a first
    look at a table is for what is absent, so the numeric summary sits behind
    it in the file rather than in front of it on screen.
    """
    assert TableProfile.HEADERS[:12] == [
        "column", "dtype", "role", "nulls", "null_pct", "blank",
        "reads_missing", "missing_pct", "distinct", "distinct_pct",
        "min", "max",
    ]
    assert set(TableProfile.HEADERS[12:]) == {
        "mean", "median", "stddev", "q1", "q3",
        "reads_as", "reads_as_pct", "parse_failures",
        "outliers_below", "outliers_above", "fence_low", "fence_high",
    }


def test_the_summary_leads_with_shape_and_duplicates(gaps):
    lines = profile_table(gaps, "gaps").summary_lines()
    assert lines[0] == "6 rows, 4 columns."
    assert "1 row(s) are exact duplicates" in lines[1]
    assert "including their nulls" in lines[1]


def test_the_summary_names_columns_missing_in_hiding(gaps):
    text = " ".join(profile_table(gaps, "gaps").summary_lines())
    assert "read as missing without being null: region (4)" in text


def test_the_summary_names_a_mostly_missing_column(gaps):
    text = " ".join(profile_table(gaps, "gaps").summary_lines())
    assert f"at least {int(MOSTLY_MISSING * 100)}% missing" in text
    assert "region (67%)" in text


def test_the_summary_names_a_constant_column(con):
    con.execute("CREATE TABLE t AS SELECT 'X' AS flag, i AS n FROM range(10) s(i)")
    text = " ".join(profile_table(con, "t").summary_lines())
    assert "cannot be grouped by: flag" in text


def test_the_summary_does_not_call_a_unique_column_a_key(items):
    text = " ".join(profile_table(items, "order_items").summary_lines())
    assert "Unique and null-free" in text
    assert "not a key" in text


def test_a_columns_sentence_builds_on_evidences(gaps):
    """
    Extends rather than replaces, so the two never describe one column two
    different ways when evidence's wording changes.
    """
    p = profile_table(gaps, "gaps")
    region = p.column("region")
    assert region.sentence().startswith(region.evidence.sentence())
    assert "read as missing without being null" in region.sentence()


def test_a_clean_columns_sentence_is_evidences_unchanged(items):
    price = profile_table(items, "order_items").column("price")
    assert price.sentence() == price.evidence.sentence()


def test_to_text_reads_as_a_profile(gaps):
    text = profile_table(gaps, "gaps").to_text()
    assert text.startswith("Profile of gaps")
    assert "Columns:" in text
    assert "Notes:" in text
    assert text.count("region (VARCHAR)") == 1


# --------------------------------------------------------------------------
# what a text column actually holds -- Step 3
# --------------------------------------------------------------------------

def _one_column(con, values, name="t"):
    body = ", ".join("(NULL)" if v is None else f"('{v}')" for v in values)
    con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM (VALUES {body}) v(a)")
    return profile_table(con, name).column("a")


def test_a_text_column_of_integers_reads_as_bigint(con):
    t = _one_column(con, ["1", "2", "3", "40"]).type_reading
    assert t.best == "BIGINT"
    assert t.is_total
    assert t.ratio == 1.0


def test_a_decimal_string_does_not_read_as_an_integer(con):
    """
    TRY_CAST('4.5' AS BIGINT) returns 5. It succeeds, it rounds, and without a
    guard this module would report a decimal column as an integer one.
    """
    t = _one_column(con, ["1", "2", "4.5"]).type_reading
    assert t.best == "DOUBLE"
    assert t.is_total
    assert t.ratios.get("BIGINT", 0) < 1.0


def test_padded_and_exponent_integers_still_read_as_integers(con):
    """The guard must not be so strict it rejects things that are integers."""
    t = _one_column(con, ["007", " 7 ", "1e3", "42"]).type_reading
    assert t.best == "BIGINT"
    assert t.is_total


def test_a_timestamp_does_not_read_as_a_date(con):
    """
    TRY_CAST('2024-01-01 10:30:00' AS DATE) returns 2024-01-01 -- it drops the
    time. Same shape of trap as the integer one, one type along.
    """
    t = _one_column(con, ["2024-01-01 10:30:00", "2024-06-05 09:15:00"]).type_reading
    assert t.best == "TIMESTAMP"
    assert t.ratios.get("DATE", 0) == 0.0


def test_a_midnight_timestamp_does_read_as_a_date(con):
    """Nothing is lost, so the narrower reading is honest."""
    t = _one_column(con, ["2024-01-01 00:00:00", "2024-06-05"]).type_reading
    assert t.best == "DATE"


def test_plain_dates_read_as_dates(con):
    assert _one_column(con, ["2024-01-01", "2024-06-05"]).type_reading.best == "DATE"


def test_a_tie_goes_to_the_narrowest_reading(con):
    """
    '1' and '0' parse as BIGINT and as BOOLEAN. CAST_CANDIDATES is ordered
    narrowest first and the integer reading assumes less about intent.
    """
    t = _one_column(con, ["1", "0", "1", "0"]).type_reading
    assert t.ratios["BIGINT"] == 1.0 and t.ratios["BOOLEAN"] == 1.0
    assert t.best == "BIGINT"


def test_a_mostly_numeric_column_reports_the_share_and_the_exceptions(con):
    values = [str(i) for i in range(19)] + ["n/a-ish junk"]
    t = _one_column(con, values).type_reading
    assert t.best == "BIGINT"
    assert not t.is_total
    assert t.ratio == pytest.approx(0.95)
    assert t.examples == ["n/a-ish junk"]


def test_missing_tokens_are_not_counted_as_parse_failures(con):
    """
    'N/A' is already reported as missing. Counting it again as a cast failure
    describes one problem twice and understates the ratio.
    """
    t = _one_column(con, ["1", "2", "3", "N/A", "", "  ", None]).type_reading
    assert t.considered == 3
    assert t.is_total and t.best == "BIGINT"


def test_a_genuinely_textual_column_reads_as_nothing(con):
    t = _one_column(con, ["North", "South", "East"]).type_reading
    assert t.best is None
    assert not t.is_worth_naming


def test_a_column_below_the_threshold_is_not_named_but_is_still_counted(con):
    """
    The threshold governs the summary, not the truth. The ratio reaches the
    table either way.
    """
    values = [str(i) for i in range(5)] + ["a", "b", "c", "d", "e"]
    c = _one_column(con, values)
    assert c.type_reading.ratio == pytest.approx(0.5)
    assert c.type_reading.ratio < TYPE_MISMATCH_SHARE
    assert not c.type_reading.is_worth_naming


def test_numeric_columns_are_not_given_a_type_reading(items):
    assert profile_table(items, "order_items").column("price").type_reading is None


def test_the_summary_separates_total_from_partial(con):
    con.execute(
        """CREATE TABLE t AS SELECT
             i::VARCHAR AS all_ints,
             CASE WHEN i < 19 THEN i::VARCHAR ELSE 'oops' END AS mostly_ints
           FROM range(20) s(i)"""
    )
    text = " ".join(profile_table(con, "t").summary_lines())
    assert "hold nothing but values of another type: all_ints -> BIGINT" in text
    assert "mostly another type with exceptions: mostly_ints -> BIGINT (95.0%)" in text
    assert "The exceptions are the interesting part." in text


def test_nothing_is_converted_and_the_note_says_so(con):
    con.execute("CREATE TABLE t AS SELECT i::VARCHAR AS a FROM range(20) s(i)")
    p = profile_table(con, "t")
    assert p.column("a").dtype == "VARCHAR"
    assert any("was NOT converted" in n for n in p.notes)
    assert any("Phase 6" in n for n in p.notes)


# --------------------------------------------------------------------------
# outliers -- Tukey, and where it breaks down
# --------------------------------------------------------------------------

def test_a_value_far_above_the_others_is_counted(con):
    con.execute(
        "CREATE TABLE t AS SELECT CASE WHEN i = 99 THEN 10000.0 ELSE i::DOUBLE END "
        "AS n FROM range(100) s(i)"
    )
    o = profile_table(con, "t").column("n").outliers
    assert o.above == 1 and o.below == 0
    assert o.total == 1


def test_a_value_far_below_the_others_is_counted(con):
    con.execute(
        "CREATE TABLE t AS SELECT CASE WHEN i = 0 THEN -10000.0 ELSE i::DOUBLE END "
        "AS n FROM range(100) s(i)"
    )
    assert profile_table(con, "t").column("n").outliers.below == 1


def test_the_fences_are_tukeys(con):
    con.execute("CREATE TABLE t AS SELECT i::DOUBLE AS n FROM range(1, 101) s(i)")
    c = profile_table(con, "t").column("n")
    n, o = c.numeric, c.outliers
    assert o.lower_fence == pytest.approx(n.q1 - OUTLIER_K * n.iqr)
    assert o.upper_fence == pytest.approx(n.q3 + OUTLIER_K * n.iqr)


def test_evenly_spread_data_has_no_outliers(con):
    con.execute("CREATE TABLE t AS SELECT i::DOUBLE AS n FROM range(1, 101) s(i)")
    assert profile_table(con, "t").column("n").outliers.total == 0


def test_a_zero_iqr_suppresses_the_count_rather_than_flagging_everything(con):
    """
    A column that is 95% one value has coincident quartiles, so the fences
    collapse to a point and every other row falls outside them. Tukey says
    that; nobody should act on it. Measured: 5 of 100 flagged, and the five
    were the values 1, 2 and 3.
    """
    con.execute(
        "CREATE TABLE t AS SELECT CASE WHEN i < 95 THEN 0.0 ELSE ((i % 3) + 1)::DOUBLE "
        "END AS n FROM range(100) s(i)"
    )
    o = profile_table(con, "t").column("n").outliers
    assert o.suppressed is not None
    assert "collapse to a point" in o.suppressed
    assert o.total == 0


def test_an_all_null_column_gets_no_outlier_count(con):
    con.execute("CREATE TABLE t AS SELECT CAST(NULL AS DOUBLE) AS n FROM range(5)")
    o = profile_table(con, "t").column("n").outliers
    assert o.suppressed is not None
    assert "no quantiles" in o.suppressed


def test_text_columns_get_no_outlier_count(items):
    assert profile_table(items, "order_items").column("seller_id").outliers is None


def test_the_summary_names_measures_only(con):
    """
    A rating of 1 to 5 with one 5 is not an outlier story. evidence already
    calls a low-cardinality numeric column a dimension; this follows it.
    """
    con.execute(
        """CREATE TABLE t AS SELECT
             CASE WHEN i = 99 THEN 10000.0 ELSE i::DOUBLE END AS revenue,
             ((i % 5) + 1)::INTEGER                           AS rating
           FROM range(100) s(i)"""
    )
    p = profile_table(con, "t")
    assert p.column("revenue").role == "measure"
    line = next(l for l in p.summary_lines() if "outside" in l)
    assert "revenue (1)" in line
    assert "rating" not in line


def test_the_summary_reports_what_it_could_not_count(con):
    con.execute("CREATE TABLE t AS SELECT 5.0::DOUBLE AS flat FROM range(20)")
    text = " ".join(profile_table(con, "t").summary_lines())
    assert "got no outlier count: flat" in text


def test_the_note_says_outside_a_fence_is_not_wrong(con):
    con.execute(
        "CREATE TABLE t AS SELECT CASE WHEN i = 99 THEN 10000.0 ELSE i::DOUBLE END "
        "AS n FROM range(100) s(i)"
    )
    note = " ".join(profile_table(con, "t").notes)
    assert "is not an error" in note
    assert "Phase 9" in note


def test_the_method_is_named_in_the_output(con):
    con.execute(
        "CREATE TABLE t AS SELECT CASE WHEN i = 99 THEN 10000.0 ELSE i::DOUBLE END "
        "AS n FROM range(100) s(i)"
    )
    assert f"k={OUTLIER_K}" in OutlierSummary.method
    assert OutlierSummary.method in profile_table(con, "t").column("n").sentence()


def test_the_new_findings_reach_the_table(con):
    con.execute(
        """CREATE TABLE t AS SELECT
             i::VARCHAR                                       AS looks_numeric,
             CASE WHEN i = 99 THEN 10000.0 ELSE i::DOUBLE END AS n
           FROM range(100) s(i)"""
    )
    p = profile_table(con, "t")
    row = dict(zip(TableProfile.HEADERS, p.to_rows()[0]))
    assert row["reads_as"] == "BIGINT" and row["reads_as_pct"] == 100.0
    row = dict(zip(TableProfile.HEADERS, p.to_rows()[1]))
    assert row["outliers_above"] == 1
    assert row["fence_high"] is not None


def test_a_suppressed_count_leaves_the_table_blank_not_zero(con):
    """A zero here would read as 'checked, none found'. Nothing was checked."""
    con.execute("CREATE TABLE t AS SELECT 5.0::DOUBLE AS flat FROM range(20)")
    row = dict(zip(TableProfile.HEADERS, profile_table(con, "t").to_rows()[0]))
    assert row["outliers_above"] is None
    assert row["fence_low"] is None


# --- codes and currency, said (Cleanup Step 12) ---------------------------------------------------

@pytest.fixture
def coded(con):
    con.execute("""CREATE TABLE coded AS SELECT * FROM (VALUES
      ('000435', '₹1,234.00'), ('000021', '₹990.00'), ('001200', '₹10,846.50'),
      ('000009', '₹58.00')) t(sku, list_price)""")
    return con


def test_a_code_that_parses_as_a_number_says_its_leading_zeros(coded):
    s = profile_table(coded, "coded").column("sku").sentence()
    assert "4 have leading zeros a number would drop" in s


def test_numbers_behind_a_currency_sign_are_named(coded):
    s = profile_table(coded, "coded").column("list_price").sentence()
    assert "read as numbers once the currency sign and thousands separators are removed" in s


def test_the_breakdown_names_a_token_as_the_column_writes_it():
    """Retail rating (recheck, 25/09/2026): 6,833 'n/a' reported as 'N/A x6,833'."""
    import duckdb as _duckdb
    c = _duckdb.connect(":memory:")
    c.execute("CREATE TABLE r AS SELECT * FROM (VALUES ('4'),('n/a'),('n/a'),('N/A')) t(rating)")
    assert profile_table(c, "r").column("rating").missing_token_breakdown == {"n/a": 2, "N/A": 1}
