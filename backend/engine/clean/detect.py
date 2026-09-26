"""What a table needs, found without touching it.

Phase 6, Step 5. Turns a loaded table into a list of `CleaningAction`s, each
already carrying the SQL that will run and the counts that justify it.

**P6-D5, resolved by P6-D2 rather than by preference.** The open question was
whether detection should read a `TableProfile` or compute its own counts. It
cannot read one: `profile_dataset` records a run in `_agent_profiles`, and the
proposal path holds the workspace through `ATTACH ... (READ_ONLY)`, which
refuses `INSERT` and `CREATE` by statement type. A read-only path cannot invoke
a tool that writes. So detection does its own reading, and everything it reads
is a `SELECT`.

That turns out to be the better answer anyway. A profile taken forty minutes ago
describes the table as it was; a proposal has to describe the table as it is,
and the numbers it prints are the numbers its own statement will produce because
both come from the same expression in `clean/sql.py`.

**Detection proposes. It never decides.** Every action here goes into a plan and
waits for an id to be approved. Nothing in this module writes.

**EXCLUDE_COLUMN is not detected.** Dropping a column is not something a table's
contents can suggest -- a column that is 100% null may be the one column that
matters and the feed is broken. It arrives from the contract (P6-D8) or from a
person, and `clean/sql.py` renders it when asked.
"""

from __future__ import annotations

import math

from .plan import ActionKind, CleaningAction, next_action_id
from . import sql

# A column is only offered as a type conversion when nearly all of it already
# parses. Below this it is not a column of the wrong type, it is a mixed
# column, and merging two meanings into one is a decision no threshold should
# make. Measured against the fixture: mixed_types' units parses 5,993/6,000
# (0.9988) and unit_price 5,997/6,000 (0.9995), so the fixture sits far above
# this and the number is not tuned to pass it.
CONVERT_MIN_SHARE = 0.90

# Values that mean true/false and nothing else. Deliberately NOT {'1','0'}:
# TRY_CAST('1' AS BOOLEAN) is TRUE in DuckDB, so an integer column of ones and
# zeroes parses perfectly as boolean, and proposing that would silently turn
# counts into flags.
BOOLEAN_WORDS = ("TRUE", "FALSE")


def _scalar(con, query: str):
    return con.execute(query).fetchone()[0]


def text_columns(con, table: str) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? AND data_type = 'VARCHAR' "
            "ORDER BY ordinal_position",
            [table],
        ).fetchall()
    ]


def _non_null(con, table: str, column: str) -> int:
    c, t = sql.ident(column), sql.ident(table)
    return _scalar(con, f"SELECT count(*) FROM {t} WHERE {c} IS NOT NULL")


def proposed_type(con, table: str, column: str) -> str | None:
    """The narrowest type this column can be read as without losing anything.

    Order and guards both matter, and both are measured rather than assumed:

    - `TRY_CAST('9.50' AS BIGINT)` is **10**. A column of prices parses 100%
      as BIGINT and rounds every value. So "does it parse" is not a signal;
      BIGINT is only proposed when no value loses a fractional part, which is
      asked directly rather than inferred from the text.
    - `TRY_CAST('1' AS BOOLEAN)` is **TRUE**. So BOOLEAN is only proposed when
      every value is literally true or false.
    - `TRY_CAST` on a timestamp string will happily produce a DATE and drop the
      time. TIMESTAMP is proposed; DATE only when every value is midnight.
    """
    t, c = sql.ident(table), sql.ident(column)
    total = _non_null(con, table, column)
    if total == 0:
        return None
    # A value with a zero before another digit is a code: '000435' becomes 435, and SKUs that
    # were six characters stop being what anybody wrote. Retail C003 offered exactly that as
    # discarding nothing and recommended it (Cleanup Step 12, RF-O5).
    padded = _scalar(con, f"SELECT count(*) FROM {t} WHERE regexp_matches({c}, '^[+-]?0[0-9]')")

    def reaches(to_type: str) -> bool:
        return _reaches(con, table, column, f"TRY_CAST({c} AS {to_type}) IS NOT NULL")

    if reaches("BOOLEAN"):
        words = ", ".join(sql.literal(w) for w in BOOLEAN_WORDS)
        odd = _scalar(
            con,
            f"SELECT count(*) FROM {t} WHERE {c} IS NOT NULL "
            f"AND upper(trim({c})) NOT IN ({words})",
        )
        if odd == 0:
            return "BOOLEAN"

    # A padded code is not read as a number at all (Cleanup Step 12, RF-O5); the sampled test
    # decides the rest (P14-O15).
    if not padded and reaches("DOUBLE"):
        rounded = _scalar(
            con,
            f"SELECT count(*) FROM {t} WHERE TRY_CAST({c} AS DOUBLE) IS NOT NULL "
            f"AND TRY_CAST({c} AS DOUBLE) <> TRY_CAST({c} AS BIGINT)",
        )
        if rounded == 0 and reaches("BIGINT"):
            return "BIGINT"
        return "DECIMAL(18,2)" if _fits_two_places(con, table, column) else "DOUBLE"

    # A two-digit year casts, year FIRST: '01/01/24' becomes 0001-01-24 (measured, P14-D60).
    # Such a column goes to _date_conversion, which reads the year last.
    # Asked only of a column that reads as timestamps: the answer is the same, the scan is not.
    if reaches("TIMESTAMP") and not _scalar(
            con, f"SELECT count(*) FROM {t} WHERE "
                 f"regexp_full_match(trim({c}), {sql.literal(_TWO_DIGIT_YEAR)})"):
        with_time = _scalar(
            con,
            f"SELECT count(*) FROM {t} WHERE TRY_CAST({c} AS TIMESTAMP) IS NOT NULL "
            f"AND TRY_CAST({c} AS TIMESTAMP) <> "
            f"date_trunc('day', TRY_CAST({c} AS TIMESTAMP))",
        )
        return "TIMESTAMP" if with_time else "DATE"

    return None


def currency_type(con, table: str, column: str) -> str | None:
    """The number type a column of currency text reads as, or None (Cleanup Step 12, A3).

    Offered only where the plain cast fails and most values parse once the currency sign, the
    thousands separators and spaces are removed -- and at least one value holds such a character,
    so a column of plain numbers is never routed here.
    """
    t, c = sql.ident(table), sql.ident(column)
    total = _non_null(con, table, column)
    if total == 0:
        return None
    stripped = f"regexp_replace({c}, '{sql.CURRENCY_CHARS}', '', 'g')"
    marked = _scalar(con, f"SELECT count(*) FROM {t} WHERE regexp_matches({c}, '[₹$€£¥,]')")
    parsed = _scalar(con, f"SELECT count(*) FROM {t} WHERE {c} IS NOT NULL "
                          f"AND TRY_CAST({stripped} AS DOUBLE) IS NOT NULL")
    if not marked or parsed / total < CONVERT_MIN_SHARE:
        return None
    lost = _scalar(con, f"SELECT count(*) FROM {t} WHERE TRY_CAST({stripped} AS DOUBLE) IS NOT NULL "
                        f"AND (TRY_CAST({stripped} AS DECIMAL(18,2)) IS NULL OR "
                        f"TRY_CAST({stripped} AS DECIMAL(18,2)) <> TRY_CAST({stripped} AS DOUBLE))")
    return "DECIMAL(18,2)" if lost == 0 else "DOUBLE"


def _fits_two_places(con, table: str, column: str) -> bool:
    """DECIMAL(18,2) over DOUBLE where it fits.

    Money in a DOUBLE is how a total ends in .9999999999998, and the Olist
    revenue figure this project verified to the cent is the reason to care.
    """
    t, c = sql.ident(table), sql.ident(column)
    lost = _scalar(
        con,
        f"SELECT count(*) FROM {t} WHERE TRY_CAST({c} AS DOUBLE) IS NOT NULL "
        f"AND (TRY_CAST({c} AS DECIMAL(18,2)) IS NULL "
        f"     OR TRY_CAST({c} AS DECIMAL(18,2)) <> TRY_CAST({c} AS DOUBLE))",
    )
    return lost == 0


NUMERIC_TYPES = ("BIGINT", "DOUBLE", "DECIMAL(18,2)")

# Numbers written for people rather than parsers (P14-O10, B8/B9). RE2, full-match. A value like
# '1,234' fits both conventions -- a thousand and twenty-four in one, one point two in the other --
# so a convention is chosen only by values that fit it and not the other.
_US_NUMBER = r"[-+]?[₹$€£¥]?\s?[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?"
_EU_NUMBER = r"[-+]?[₹$€£¥]?\s?[-+]?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?"
_PERCENT = r"[-+]?\d+([.,]\d+)?\s?%"
_CURRENCY_AND_SPACE = "[₹$€£¥\\s]"

# Date formats tried in order for a text column no single cast reads (P14-O10, B12). The numeric
# day/month orders are added only when a value settles which comes first (a part above 12).
_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d",
                 "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%Y%m%d")
# Two-digit years first: %Y reads '31/12/24' as the year 0024, and %y refuses a four-digit year
# (both measured, P14-D60), so this order reads each value by the one format that fits it.
_DAY_FIRST = ("%d/%m/%y", "%d.%m.%y", "%d-%m-%y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y")
_MONTH_FIRST = ("%m/%d/%y", "%m.%d.%y", "%m-%d-%y", "%m/%d/%Y", "%m.%d.%Y", "%m-%d-%Y")
_NUMERIC_DATE = r"(\d{1,2})[/.-](\d{1,2})[/.-](?:\d{4}|\d{2})"
_TWO_DIGIT_YEAR = r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2}"
# What a value any date format above parses must open with (tests/test_date_prescreen_facts.py).
DATE_PREFIX = r"^[\s\x0b\-+0-9]|^(?i)(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"


def _share_where(con, table: str, column: str, condition: str) -> float:
    total = _non_null(con, table, column)
    if not total:
        return 0.0
    t, c = sql.ident(table), sql.ident(column)
    n = _scalar(con, f"SELECT count(*) FROM {t} WHERE {c} IS NOT NULL AND ({condition})")
    return n / total


def _reaches(con, table: str, column: str, condition: str) -> bool:
    """Whether `condition` holds for CONVERT_MIN_SHARE of the non-null values -- the only use
    any share here is put to.

    A sample is read first. Failures in a sample are failures of the column, so when the sample
    alone holds more than the column may have and still reach the share, the answer is exactly
    no, without the full scan. The full count decides everything else, as it always did. This is
    what keeps the mixed-date test (up to 16 TRY_STRPTIME formats per value) off every value
    of an id column: 0.9 s per column at 100,000 rows (P14-O15).
    """
    total = _non_null(con, table, column)
    if not total:
        return False
    need = math.ceil(CONVERT_MIN_SHARE * total)
    while need > 0 and (need - 1) / total >= CONVERT_MIN_SHARE:
        need -= 1
    while need / total < CONVERT_MIN_SHARE:
        need += 1
    max_fails = total - need
    k = (max_fails + 1) * 3 // 2 + 1
    if k < total:
        t, c = sql.ident(table), sql.ident(column)
        fails = _scalar(con, f"SELECT count(*) FROM (SELECT {c} FROM {t} WHERE {c} IS NOT NULL "
                             f"LIMIT {k}) WHERE NOT coalesce(({condition}), false)")
        if fails > max_fails:
            return False
    return _share_where(con, table, column, condition) >= CONVERT_MIN_SHARE


def _numeric_type_of(con, table: str, text: str) -> str:
    """BIGINT, DECIMAL(18,2) or DOUBLE for a cleaned text expression, by the same rules as
    proposed_type: never round a fraction away, keep cents exact."""
    t = sql.ident(table)
    as_double = f"TRY_CAST({text} AS DOUBLE)"
    fractional = _scalar(con, f"SELECT count(*) FROM {t} WHERE {as_double} IS NOT NULL "
                              f"AND {as_double} <> round({as_double})")
    if not fractional:
        return "BIGINT"
    off_cents = _scalar(con, f"SELECT count(*) FROM {t} WHERE {as_double} IS NOT NULL "
                             f"AND round({as_double}, 2) <> {as_double}")
    return "DOUBLE" if off_cents else "DECIMAL(18,2)"


def alternative_conversion(con, table: str, column: str) -> list[tuple[str, str, str]]:
    """[(to_type, expression, how)] for text a plain cast cannot read -- empty, one, or two.

    Two when every value reads both ways ('1,234' is a thousand and twenty-four with a
    thousands comma, one point two three four with a decimal comma): both are offered, neither is
    suggested, and approving both is refused (P14-D50).

    Tried only after proposed_type found nothing. Each candidate must read CONVERT_MIN_SHARE of
    the column, like any conversion, and what it cannot read is counted as lost from the same
    expression when the action is built.
    """
    c = sql.ident(column)
    # Accounting writes a negative as (1,234.56): read it as -1,234.56 before any pattern, so
    # every reading below handles it (P14-D59).
    # SAP writes it 1234.56- (P14-D61).
    trimmed = (f"(CASE WHEN regexp_full_match(trim({c}), '\\(.*\\)') "
               f"THEN '-' || trim(trim({c}), '()') "
               f"WHEN regexp_full_match(trim({c}), '[^-].*[0-9.,]-') "
               f"THEN '-' || rtrim(trim({c}), '-') ELSE trim({c}) END)")

    def full(pattern: str) -> str:
        return f"regexp_full_match({trimmed}, {sql.literal(pattern)})"

    # A zero-padded code is not offered as a number by any reading (Cleanup Step 12, RF-O5):
    # '000435' matched both separator conventions here and came back as two conversions at the
    # merge of main (25/09/2026). Its dates are still looked for.
    if _scalar(con, f"SELECT count(*) FROM {sql.ident(table)} "
                    f"WHERE regexp_matches({c}, '^[+-]?0[0-9]')"):
        date = _date_conversion(con, table, column)
        return [date] if date else []

    if _reaches(con, table, column, full(_PERCENT)):
        text = f"replace(rtrim({trimmed}, '%'), ',', '.')"
        to_type = _numeric_type_of(con, table, text)
        return [(to_type, f"TRY_CAST({text} AS {to_type})",
                 "the number before its % sign, so '12.5%' becomes 12.5")]

    us = _reaches(con, table, column, full(_US_NUMBER))
    eu = _reaches(con, table, column, full(_EU_NUMBER))
    if us or eu:
        only_us = _share_where(con, table, column,
                               f"{full(_US_NUMBER)} AND NOT {full(_EU_NUMBER)}")
        only_eu = _share_where(con, table, column,
                               f"{full(_EU_NUMBER)} AND NOT {full(_US_NUMBER)}")
        bare = f"regexp_replace({trimmed}, {sql.literal(_CURRENCY_AND_SPACE)}, '', 'g')"
        us_reading = (f"replace({bare}, ',', '')",
                      "with its currency signs and thousands separators removed "
                      "('$1,234.56' is 1234.56)")
        eu_reading = (f"replace(replace({bare}, '.', ''), ',', '.')",
                      "with a decimal comma ('1.234,56' is 1234.56)")
        if us and only_us and not only_eu:
            readings = [us_reading]
        elif eu and only_eu and not only_us:
            readings = [eu_reading]
        elif us and eu and not (only_us or only_eu):
            # Every value reads both ways: which one is meant is the person's to say.
            readings = [(t, how + " -- IF that is this file's convention; the other reading is "
                            "offered beside it, and only one can be approved")
                        for t, how in (us_reading, eu_reading)]
        else:
            return []
        out = []
        for text, how in readings:
            to_type = _numeric_type_of(con, table, text)
            out.append((to_type, f"TRY_CAST({text} AS {to_type})", how))
        return out

    date = _date_conversion(con, table, column)
    return [date] if date else []


def _date_conversion(con, table: str, column: str) -> tuple[str, str, str] | None:
    t, c = sql.ident(table), sql.ident(column)
    trimmed = f"trim({c})"
    # A column of plain digits is ids or amounts, however many of them look like 20240131.
    if _reaches(con, table, column, f"regexp_full_match({trimmed}, '\\d+')"):
        return None
    first = f"TRY_CAST(regexp_extract({trimmed}, {sql.literal(_NUMERIC_DATE)}, 1) AS INTEGER)"
    second = f"TRY_CAST(regexp_extract({trimmed}, {sql.literal(_NUMERIC_DATE)}, 2) AS INTEGER)"
    day_first = _scalar(con, f"SELECT count(*) FROM {t} WHERE {first} > 12")
    month_first = _scalar(con, f"SELECT count(*) FROM {t} WHERE {second} > 12")
    # The settled numeric order goes FIRST: '%Y/%m/%d' in the general list reads '01/01/24' as
    # 0001-01-24, and '%d/%m/%y' cannot mistake a four-digit year (P14-D60).
    formats: list[str] = []
    order = ""
    if day_first and not month_first:
        formats += _DAY_FIRST
        order = "; numeric dates read day first, as values such as 31/01 show"
    elif month_first and not day_first:
        formats += _MONTH_FIRST
        order = "; numeric dates read month first, as values such as 01/31 show"
    formats += _DATE_FORMATS
    parsed = "COALESCE(" + ", ".join(
        f"TRY_STRPTIME({trimmed}, {sql.literal(f)})" for f in formats) + ")"
    # Every format opens with a number or a month name, and strptime skips leading whitespace
    # (trim() keeps a tab): a value opening otherwise parses by none of them. The CASE keeps
    # the formats off such values -- id and label columns, 1.2 s each at 1M rows (Step 13).
    screened = (f"CASE WHEN regexp_matches({trimmed}, {sql.literal(DATE_PREFIX)}) "
                f"THEN {parsed} IS NOT NULL ELSE false END")
    if not _reaches(con, table, column, screened):
        return None
    with_time = _scalar(con, f"SELECT count(*) FROM {t} WHERE {parsed} IS NOT NULL "
                             f"AND {parsed} <> date_trunc('day', {parsed})")
    to_type = "TIMESTAMP" if with_time else "DATE"
    expression = parsed if with_time else f"CAST({parsed} AS DATE)"
    two_digit = _scalar(con, f"SELECT count(*) FROM {t} WHERE "
                             f"regexp_full_match({trimmed}, {sql.literal(_TWO_DIGIT_YEAR)})")
    if two_digit:
        order += ("; a two-digit year 69-99 is read as 19xx and 00-68 as 20xx, the POSIX rule "
                  "-- say so if this file means otherwise")
    return to_type, expression, f"from the date formats it is written in{order}"


def _more_dates(con, table: str, column: str, to_type: str | None
                ) -> tuple[str, str, str] | None:
    """The formats reading of a date column, when it reads more values than the plain cast.

    proposed_type accepts a DATE cast that reads CONVERT_MIN_SHARE of a column and counts the
    rest as lost. The retail fixture's delivery_date is 98.1% yyyy-mm-dd and 3,030 dd/mm/yyyy:
    the plan offered the cast discarding all 3,030, while _date_conversion reads both formats
    (recheck, 25/09/2026). None when the cast already reads as much, or the column is no date.
    """
    if to_type not in ("DATE", "TIMESTAMP"):
        return None
    dated = _date_conversion(con, table, column)
    if dated is None:
        return None
    t, c = sql.ident(table), sql.ident(column)
    plain = _scalar(con, f"SELECT count(*) FROM {t} WHERE TRY_CAST({c} AS {to_type}) IS NOT NULL")
    formats = _scalar(con, f"SELECT count(*) FROM {t} WHERE ({dated[1]}) IS NOT NULL")
    return dated if formats > plain else None


def _action(
    con, *, action_id: str, kind: ActionKind, rendering: sql.Rendering,
    intent: str, column: str | None, loss_unit: str,
) -> CleaningAction:
    """Take the counts from the rendering's own queries. Never from anywhere
    else -- that is the whole argument of clean/sql.py."""
    lost, sample = 0, ()
    if rendering.lost_sql:
        lost = _scalar(con, rendering.lost_sql) or 0
        if lost and rendering.sample_sql:
            sample = tuple(
                str(r[0]) for r in con.execute(rendering.sample_sql).fetchall()
            )
    return CleaningAction(
        action_id=action_id,
        kind=kind,
        column=column,
        intent=intent,
        sql=rendering.statement,
        rows_affected=_scalar(con, rendering.affected_sql) or 0,
        values_lost=lost,
        loss_unit=loss_unit,
        sample=sample,
    )


def detect(
    con, *, source: str, target: str, missing_tokens: list[str]
) -> list[CleaningAction]:
    """Every action this table supports, in a stable order.

    Table-level first, then per column in ordinal order, so two runs against an
    unchanged table produce the same ids. An id that moves between proposals is
    an id nobody can approve.
    """
    actions: list[CleaningAction] = []

    def add(kind, rendering, intent, column, loss_unit="value"):
        actions.append(
            _action(
                con, action_id=next_action_id(len(actions)), kind=kind,
                rendering=rendering, intent=intent, column=column,
                loss_unit=loss_unit,
            )
        )

    dupes = _scalar(
        con,
        f"SELECT count(*) - (SELECT count(*) FROM "
        f"(SELECT DISTINCT * FROM {sql.ident(source)})) "
        f"FROM {sql.ident(source)}",
    )
    if dupes:
        add(
            ActionKind.DROP_DUPLICATE_ROWS,
            sql.drop_duplicate_rows(source=source, target=target),
            f"keep one of each of the {dupes:,} exactly duplicated row(s)",
            None,
            loss_unit="row",
        )

    columns = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ? "
        "ORDER BY ordinal_position", [source]).fetchall()]
    if len(columns) >= 2:
        need = max(2, (len(columns) + 1) // 2)
        copies = _scalar(con, f"SELECT count(*) FROM {sql.ident(source)} "
                              f"WHERE {sql.header_match(columns, need)}")
        if copies:
            add(
                ActionKind.DROP_HEADER_ROWS,
                sql.drop_header_rows(source=source, target=target, columns=columns,
                                     min_match=need),
                f"remove the {copies:,} row(s) that repeat the column names -- a header "
                f"pasted into the data, which is what keeps numeric columns as text",
                None,
                loss_unit="row",
            )

    for column in text_columns(con, source):
        t, c = sql.ident(source), sql.ident(column)

        to_type = proposed_type(con, source, column)
        dated = _more_dates(con, source, column, to_type)
        if to_type and not dated:
            numeric = to_type in NUMERIC_TYPES
            lead = numeric and _scalar(
                con, f"SELECT count(*) FROM {t} WHERE "
                     f"{sql.LEADING_ZERO.format(col=c)}")
            add(
                ActionKind.CONVERT_TYPE,
                sql.convert_type(source=source, target=target, column=column,
                                 to_type=to_type, missing_tokens=missing_tokens,
                                 numeric=numeric),
                f"read {column} as {to_type}"
                + (f" -- {lead:,} value(s) begin with a zero that a number cannot keep, "
                   f"as a zip code or an account number would" if lead else ""),
                column,
            )
        else:
            # The readings a plain cast misses -- separators of either convention, currency and
            # percent signs, mixed date formats (P14-O10) -- first: they tell a decimal comma
            # from a thousands separator. Main's currency reading (Cleanup Step 12) stands behind
            # them; ahead of them it read '12,5' as 125 (found at the merge, 25/09/2026).
            alternatives = [dated] if dated else alternative_conversion(con, source, column)
            for alt_type, expression, how in alternatives:
                add(
                    ActionKind.CONVERT_TYPE,
                    sql.convert_type(source=source, target=target, column=column,
                                     to_type=alt_type, missing_tokens=missing_tokens,
                                     expression=expression,
                                     numeric=alt_type in NUMERIC_TYPES),
                    f"read {column} as {alt_type}, {how}",
                    column,
                )
            money = None if alternatives else currency_type(con, source, column)
            if money:
                add(
                    ActionKind.CONVERT_TYPE,
                    sql.convert_type(source=source, target=target, column=column,
                                     to_type=money, missing_tokens=missing_tokens,
                                     strip_currency=True),
                    f"read {column} as {money}, removing the currency sign and thousands "
                    f"separators first (currency text)",
                    column,
                )

        if missing_tokens:
            words = sql.token_list(missing_tokens)
            hits = _scalar(
                con,
                f"SELECT count(*) FROM {t} WHERE upper(trim({c})) IN ({words})",
            )
            if hits:
                add(
                    ActionKind.NORMALISE_MISSING,
                    sql.normalise_missing(source=source, target=target,
                                          column=column, tokens=missing_tokens),
                    f"turn the {hits:,} declared missing token(s) in {column} "
                    f"into real nulls",
                    column,
                )

        padded = _scalar(
            con,
            f"SELECT count(*) FROM {t} WHERE {c} IS NOT NULL "
            f"AND {c} <> trim({c})",
        )
        if padded:
            add(
                ActionKind.TRIM_WHITESPACE,
                sql.trim_whitespace(source=source, target=target, column=column),
                f"strip padding from {padded:,} value(s) in {column}",
                column,
            )

        merged = _scalar(
            con,
            f"SELECT count(DISTINCT {c}) - count(DISTINCT upper({c})) FROM {t}",
        )
        if merged:
            add(
                ActionKind.NORMALISE_CASE,
                sql.normalise_case(source=source, target=target, column=column),
                f"fold {column} to one case, merging {merged:,} distinct "
                f"value(s) into others",
                column,
                loss_unit="distinct value",
            )

    for column in _float_columns(con, source):
        c = sql.ident(column)
        bad = _scalar(con, f"SELECT count(*) FROM {sql.ident(source)} "
                           f"WHERE {c} IS NOT NULL AND NOT isfinite({c})")
        if bad:
            add(
                ActionKind.NULL_NON_FINITE,
                sql.null_non_finite(source=source, target=target, column=column),
                f"turn the {bad:,} NaN / Infinity value(s) in {column} into nulls -- they "
                f"make every mean and spread over the column meaningless",
                column,
            )

    return actions


def _float_columns(con, table: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ? "
        "AND data_type IN ('DOUBLE', 'FLOAT', 'REAL') ORDER BY ordinal_position",
        [table]).fetchall()]


__all__ = [
    "alternative_conversion",
    "BOOLEAN_WORDS",
    "CONVERT_MIN_SHARE",
    "detect",
    "proposed_type",
    "text_columns",
]
