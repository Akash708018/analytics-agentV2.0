"""The SQL a cleaning action will run, and the SQL that counts what it does.

Phase 6, Step 4. Renders both from ONE expression, which is the whole reason
this module exists rather than the counting living in detection and the
statement living in apply.

**The failure this prevents.** A proposal says "3 values will not convert" and
an apply statement converts a column. If the count came from one expression and
the rebuild from another, the number is a guess about a statement it has never
met -- and it will be a good guess right up until somebody edits one of them.
Phase 5 spent a whole acceptance clause on the same shape: the read_result_file
call printed in an envelope is the call that gets executed, not a path built
some other way. This is that clause with a write at the end of it.

So every renderer here builds one `expression`, and the statement, the affected
count, the loss count and the sample are all assembled from it. A test asserts
the expression appears verbatim in all four.

**Literal SQL, not parameters.** These strings are shown to a person who then
approves them, so they cannot carry placeholders that get filled in later --
what is shown has to be what runs. Values are escaped rather than bound.

**One statement per action, not one composed rebuild.** Three approved actions
rebuild the table three times. That is more work than composing them into a
single `SELECT * REPLACE (a) REPLACE (b)`, and it is the only way the ledger's
per-action row counts are verifiable: a count attributed to C002 is only
honest if C002 ran alone. At the sizes this agent meets the cost is
milliseconds; if that ever stops being true the answer is to say the rebuild
was batched, not to attribute a composed count to an action.

**Naming is not decided here.** `source` and `target` are parameters. Whether a
clean writes `sales` with `_history_sales_v1` beside it or writes `sales_v2` is
P6-D1, and it belongs to the layer that writes tables.
"""

from __future__ import annotations

from dataclasses import dataclass

from .plan import ActionKind

# Kinds whose rendering can destroy something that was not already declared
# absent. Used by detection to decide whether a sample is required.
LOSSY_KINDS = frozenset(
    {ActionKind.CONVERT_TYPE, ActionKind.DROP_DUPLICATE_ROWS,
     ActionKind.NORMALISE_CASE, ActionKind.EXCLUDE_COLUMN, ActionKind.NULL_NON_FINITE}
)

# Samples are ordered: LIMIT alone returned whichever rows a parallel scan reached first, so
# one plan proposed twice quoted different examples (Step 14 regression, D15).
SAMPLE_LIMIT = 5


def ident(name: str) -> str:
    """Quote an identifier. A column called `order date` is not hypothetical --
    Phase 3's header assembly produces them."""
    return '"' + name.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def token_list(tokens: list[str]) -> str:
    """Declared missing tokens as an upper-cased IN list.

    Upper-cased because DEFAULT_NA_VALUES holds 'NA' and 'N/A' while the data
    holds 'n/a' -- Step 2 measured that the match has to be case-insensitive or
    the whole declared/undeclared distinction collapses.
    """
    return ", ".join(literal(t.upper()) for t in tokens)


@dataclass(frozen=True)
class Rendering:
    """One action's SQL, all of it derived from `expression`.

    `lost_sql` is None when the action cannot destroy anything undeclared.
    `sample_sql` is None with it: there is nothing to show.
    """

    expression: str
    statement: str
    affected_sql: str
    lost_sql: str | None = None
    sample_sql: str | None = None

    @property
    def is_lossy(self) -> bool:
        return self.lost_sql is not None


def _rebuild(target: str, source: str, projection: str) -> str:
    return (
        f"CREATE OR REPLACE TABLE {ident(target)} AS\n"
        f"SELECT {projection}\nFROM {ident(source)}"
    )


def _replace_one(target: str, source: str, column: str, expression: str) -> str:
    """`* REPLACE (expr AS col)`, never `* EXCLUDE (col), expr AS col`.

    P5/P6-D3, measured in Step 1: EXCLUDE moves the rebuilt column to the end
    of the table, and column ORDER is identity to Binding.fingerprint, so under
    EXCLUDE every value-level conversion reads downstream as structural drift.
    """
    return _rebuild(
        target, source, f"* REPLACE ({expression} AS {ident(column)})"
    )


# A value a numeric conversion reads but changes: '00311' casts to 311 and the zeros, which are
# part of a zip code or an account number, are gone (P14-O4, B2).
LEADING_ZERO = "regexp_matches(trim({col}), '^[+-]?0[0-9]')"
ZERO_DATE = r"0000-00-00([ T]00:00:00)?"
#: What a number written for people carries that a number does not: currency signs, thousands
#: separators, spaces. Removed before the cast when a conversion is offered as currency text.
CURRENCY_CHARS = "[₹$€£¥,\\s]"


def convert_type(
    *, source: str, target: str, column: str, to_type: str,
    missing_tokens: list[str], expression: str | None = None, numeric: bool = False,
    strip_currency: bool = False,
) -> Rendering:
    """Read a text column as `to_type`.

    The loss count excludes declared missing tokens, which is Step 2's finding
    made executable: `units` holds 'n/a' seven times and loses nothing by
    becoming NULL, because 'n/a' already meant absent. `unit_price` holds
    'not priced' three times, which is not declared anywhere, and converting it
    destroys the only record that a price was withheld rather than missing.
    """
    col = ident(column)
    # Currency text (Cleanup Step 12, A3): '₹10,846.00' is a number once the sign and the
    # separator are gone, and the statement shown for approval says so in its own SQL.
    source_value = (f"regexp_replace({col}, '{CURRENCY_CHARS}', '', 'g')"
                    if strip_currency else col)
    # `expression` is the conversion when TRY_CAST alone cannot read the text: a decimal comma,
    # a currency sign, a percent sign, several date formats (P14-O10). The loss is counted from
    # it exactly as from a plain cast. `numeric` counts a value with leading zeros as lost too.
    expression = expression or f"TRY_CAST({source_value} AS {to_type})"
    fails = f"{expression} IS NULL"
    if numeric:
        fails = f"({fails} OR {LEADING_ZERO.format(col=col)})"
    if to_type in ("DATE", "TIMESTAMP"):
        # '0000-00-00' is how MySQL writes "no date": absent, not information (P14-D62).
        fails = f"({fails} AND NOT regexp_full_match(trim({col}), {literal(ZERO_DATE)}))"
    undeclared = (
        f"{col} IS NOT NULL AND {fails}"
        + (
            f" AND upper(trim({col})) NOT IN ({token_list(missing_tokens)})"
            if missing_tokens
            else ""
        )
    )
    return Rendering(
        expression=expression,
        statement=_replace_one(target, source, column, expression),
        affected_sql=(
            f"SELECT count(*) FROM {ident(source)} WHERE {col} IS NOT NULL"
        ),
        lost_sql=f"SELECT count(*) FROM {ident(source)} WHERE {undeclared}",
        sample_sql=(
            f"SELECT DISTINCT {col} FROM {ident(source)} WHERE {undeclared} "
            f"ORDER BY 1 LIMIT {SAMPLE_LIMIT}"
        ),
    )


def normalise_missing(
    *, source: str, target: str, column: str, tokens: list[str]
) -> Rendering:
    """Turn declared missing tokens into real NULLs.

    Never lossy by construction: the only values it touches are ones already
    declared to mean absent. If that list is widened to something that is not
    a missing token, this becomes a different action and the caller has lied to
    it -- which is why the tokens are rendered into the statement in plain
    sight rather than hidden behind a config lookup at apply time.
    """
    if not tokens:
        raise ValueError("normalise_missing with no tokens changes nothing")
    col = ident(column)
    match = f"upper(trim({col})) IN ({token_list(tokens)})"
    expression = f"CASE WHEN {match} THEN NULL ELSE {col} END"
    return Rendering(
        expression=expression,
        statement=_replace_one(target, source, column, expression),
        affected_sql=f"SELECT count(*) FROM {ident(source)} WHERE {match}",
    )


def trim_whitespace(*, source: str, target: str, column: str) -> Rendering:
    """Strip leading and trailing whitespace.

    Not lossy: nothing becomes NULL and no two distinct values merge that were
    not already the same value wearing different padding. A value that trims to
    the empty string stays the empty string rather than becoming NULL, because
    turning '' into NULL is normalise_missing's decision and it has its own
    approval.
    """
    col = ident(column)
    expression = f"trim({col})"
    return Rendering(
        expression=expression,
        statement=_replace_one(target, source, column, expression),
        affected_sql=(
            f"SELECT count(*) FROM {ident(source)} "
            f"WHERE {col} IS NOT NULL AND {col} <> {expression}"
        ),
    )


def normalise_case(
    *, source: str, target: str, column: str, mode: str = "upper"
) -> Rendering:
    """Fold a column to one case.

    LOSSY, and in a way the word "lost" does not usually cover: nothing becomes
    NULL, but distinct values merge. 'North' and 'NORTH' become one value and
    the record that the source wrote them differently is gone. `lost_sql`
    counts the DISTINCT VALUES that disappear, not rows, and the sample shows
    which ones.
    """
    if mode not in ("upper", "lower"):
        raise ValueError("mode is 'upper' or 'lower'")
    col = ident(column)
    expression = f"{mode}({col})"
    src = ident(source)
    return Rendering(
        expression=expression,
        statement=_replace_one(target, source, column, expression),
        affected_sql=(
            f"SELECT count(*) FROM {src} "
            f"WHERE {col} IS NOT NULL AND {col} <> {expression}"
        ),
        lost_sql=(
            f"SELECT count(DISTINCT {col}) - count(DISTINCT {expression}) "
            f"FROM {src}"
        ),
        sample_sql=(
            f"SELECT DISTINCT {col} FROM {src} WHERE {expression} IN ("
            f"  SELECT {expression} FROM {src} GROUP BY {expression} "
            f"  HAVING count(DISTINCT {col}) > 1) ORDER BY 1 LIMIT {SAMPLE_LIMIT}"
        ),
    )


def drop_duplicate_rows(*, source: str, target: str) -> Rendering:
    """Keep one of each exact duplicate row.

    LOSSY, and the reason is multiplicity: two identical rows may be one row
    recorded twice or two real events that happen to match on every column
    this table carries. Nothing in the row distinguishes them, which is exactly
    the judgement the profiler declines to make and this action must therefore
    state rather than assume.

    The sample shows the duplicated rows as JSON WITH their multiplicity, since
    the count is the thing being destroyed. A first draft returned no sample at
    all and CleaningAction refused to be constructed from it -- the rule from
    Step 3 catching a gap in Step 4 without anybody looking for it.
    """
    src = ident(source)
    # The rebuild keeps each row's first occurrence IN FILE ORDER. SELECT DISTINCT stores the
    # rows in whatever order its parallel hash left them -- four orders in 20 rebuilds of one
    # file, and a DOUBLE sum over them came out 19 different ways (3,032,008,136.98 against
    # ...136.9801 in two identical benchmark runs: Step 13, J, D14). One order, one sum. The
    # counts are taken from the same grouped projection: GROUP BY ALL keeps one row per
    # distinct row, NULLs matching as DISTINCT matches them (measured before the change).
    expression = "*, min(rowid) AS __aa_first_row"
    grouped = f"(SELECT {expression} FROM {src} GROUP BY ALL)"
    return Rendering(
        expression=expression,
        statement=(
            f"CREATE OR REPLACE TABLE {ident(target)} AS\n"
            f"SELECT * EXCLUDE (__aa_first_row) FROM {grouped}\n"
            f"ORDER BY __aa_first_row"
        ),
        affected_sql=f"SELECT count(*) - (SELECT count(*) FROM {grouped}) FROM {src}",
        lost_sql=f"SELECT count(*) - (SELECT count(*) FROM {grouped}) FROM {src}",
        sample_sql=(
            # Ordered, so a proposal reads the same twice (Cleanup Step 16: the re-run's two
            # plans showed different sample rows for one table).
            f"SELECT to_json(d)::VARCHAR AS j FROM (SELECT *, count(*) AS "
            f"duplicate_count FROM {src} GROUP BY ALL HAVING count(*) > 1) d "
            f"ORDER BY j LIMIT {SAMPLE_LIMIT}"
        ),
    )


def null_non_finite(*, source: str, target: str, column: str) -> Rendering:
    """Turn NaN, Infinity and -Infinity in a DOUBLE column into NULL.

    DuckDB reads the text 'NaN' and 'Infinity' as those floats, and they poison every mean and
    spread (P14-O6, B4). LOSSY, because an infinity may be a sensor's way of saying "off the
    scale" -- the person decides, with the values shown.
    """
    col = ident(column)
    expression = f"CASE WHEN isfinite({col}) THEN {col} END"
    hit = f"{col} IS NOT NULL AND ({expression}) IS NULL"
    return Rendering(
        expression=expression,
        statement=_replace_one(target, source, column, expression),
        affected_sql=f"SELECT count(*) FROM {ident(source)} WHERE {hit}",
        lost_sql=f"SELECT count(*) FROM {ident(source)} WHERE {hit}",
        sample_sql=(f"SELECT DISTINCT CAST({col} AS VARCHAR) FROM {ident(source)} "
                    f"WHERE {hit} ORDER BY 1 LIMIT {SAMPLE_LIMIT}"),
    )


def header_match(columns: list[str], min_match: int) -> str:
    """A predicate true for a row whose values repeat the column names in `min_match` columns."""
    parts = []
    for c in columns:
        names = {c.lower(), c.lower().replace("_", " ")}
        listed = ", ".join(literal(n) for n in sorted(names))
        parts.append(f"CASE WHEN lower(trim(CAST({ident(c)} AS VARCHAR))) IN ({listed}) "
                     f"THEN 1 ELSE 0 END")
    return f"({' + '.join(parts)}) >= {min_match}"


def drop_header_rows(*, source: str, target: str, columns: list[str],
                     min_match: int) -> Rendering:
    """Remove rows that are copies of the header -- two exports pasted together (P14-O11, B11).

    Not lossy: such a row holds the column names and nothing else, and it is what turned every
    numeric column to text.
    """
    expression = header_match(columns, min_match)
    return Rendering(
        expression=expression,
        statement=_rebuild(target, source, "*") + f"\nWHERE NOT ({expression})",
        affected_sql=f"SELECT count(*) FROM {ident(source)} WHERE {expression}",
    )


def exclude_column(*, source: str, target: str, column: str) -> Rendering:
    """Drop a column.

    The one action EXCLUDE is right for, and the reorder it causes is not a lie
    here: a table with a column removed IS structurally different, and the
    fingerprint changing is the correct report rather than a false alarm.

    LOSSY without qualification. There is no sample worth showing -- the thing
    destroyed is a whole column, and naming it is the sample.
    """
    src = ident(source)
    col = ident(column)
    expression = f"* EXCLUDE ({col})"
    return Rendering(
        expression=expression,
        statement=_rebuild(target, source, expression),
        affected_sql=f"SELECT count(*) FROM {src}",
        lost_sql=f"SELECT count(*) FROM {src} WHERE {col} IS NOT NULL",
        sample_sql=f"SELECT DISTINCT {col} FROM {src} WHERE {col} IS NOT NULL "
                   f"ORDER BY 1 LIMIT {SAMPLE_LIMIT}",
    )


_RENDERERS = {
    ActionKind.CONVERT_TYPE: convert_type,
    ActionKind.NORMALISE_MISSING: normalise_missing,
    ActionKind.TRIM_WHITESPACE: trim_whitespace,
    ActionKind.NORMALISE_CASE: normalise_case,
    ActionKind.DROP_DUPLICATE_ROWS: drop_duplicate_rows,
    ActionKind.EXCLUDE_COLUMN: exclude_column,
    ActionKind.NULL_NON_FINITE: null_non_finite,
    ActionKind.DROP_HEADER_ROWS: drop_header_rows,
}


def render(kind: ActionKind, **kwargs) -> Rendering:
    """Dispatch on kind. Every member of ActionKind is here, which a test
    asserts -- a kind detection can produce and this module cannot render is a
    proposal that dies at apply time."""
    try:
        renderer = _RENDERERS[kind]
    except KeyError:  # pragma: no cover - guarded by test_every_kind_renders
        raise ValueError(f"no renderer for {kind}") from None
    return renderer(**kwargs)


__all__ = [
    "LOSSY_KINDS",
    "SAMPLE_LIMIT",
    "Rendering",
    "convert_type",
    "drop_duplicate_rows",
    "drop_header_rows",
    "null_non_finite",
    "exclude_column",
    "ident",
    "literal",
    "normalise_case",
    "normalise_missing",
    "render",
    "token_list",
    "trim_whitespace",
]
