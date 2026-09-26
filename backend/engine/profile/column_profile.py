"""
One column, in the detail a table of columns has no room for.

`table_profile.py` gives every column a line. This gives one column a page: what
its values actually are, how they are distributed, how long they are, and where
the dates stop. It is the drill-down you reach for after a table profile says
something looks wrong.

**This is where `unknown` comes back.** Step 2 deliberately left `unknown`,
`none`, `-` and `?` out of the missing-value vocabulary, because no reference
implementation includes them and counting them reported a fixture as 83% missing
when 67% was defensible. The promise made in exchange was that such values would
surface in the column's frequency table, where a person decides rather than a
constant. The top-values list is that table, and the note under it says what to
look for -- without naming candidates, because naming them is the list again.

**Nothing here writes a file, and that is a different rule from `render.py`'s.**
A table profile is unbounded in the dimension that matters -- one row per column,
and tables have sixty columns -- so it always writes and Step 4 explains why. A
column profile is bounded by construction: ten values, ten bins, a handful of
counts. Nothing is truncated except the value list, and that truncation ends in
a call rather than a path:

    profile_column(dataset_name="sales", column="region", top_n=50)

A truncation with no way to see the rest is the dead end Step 1 exists to
prevent. A truncation with an executable next step is not one, and it does not
need a file to avoid being one.

**The built-ins do the binning.** `equi_width_bins(lo, hi, n, true)` produces
rounded boundaries and `histogram(col, bins)` fills them, both verified on 1.5.5
including the cases that would otherwise crash: a constant column returns a
single bin rather than failing, and an all-null column returns NULL rather than
an empty map. Hand-rolled bucketing would have got the empty cases wrong and
disagreed with DuckDB's own rounding on everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.engine.contract import ContractRefused
from backend.engine.contract.evidence import _is_numeric, _is_temporal, _is_text, _q
from backend.engine.contract.refusals import Reason, Refusal
from backend.engine.profile.table_profile import (
    ColumnProfile,
    TableProfile,
    profile_table,
)

# Values listed by default. Ten is what a frequency table is for: enough to see
# what a column holds, short enough to read. More is one argument away.
TOP_VALUES = 10

# The ceiling on that argument. Past this it stops being a frequency table and
# becomes an export, which is a different tool.
MAX_TOP_VALUES = 200

# Histogram bins. matplotlib's default is 10 and so is this; the boundaries are
# DuckDB's rounded ones rather than raw min/max divisions.
HISTOGRAM_BINS = 10

# A value shown in full before it is cut. Long strings turn a frequency table
# into a wall.
VALUE_DISPLAY_CHARS = 60


@dataclass(frozen=True)
class ValueCount:
    """One value and how often it appears."""

    value: str | None
    count: int
    share: float

    def display(self) -> str:
        if self.value is None:
            return "(null)"
        text = str(self.value)
        if text.strip() == "":
            return f"(blank, {len(text)} char(s))"
        if len(text) > VALUE_DISPLAY_CHARS:
            return text[:VALUE_DISPLAY_CHARS] + "..."
        return text


@dataclass(frozen=True)
class Histogram:
    """Equal-width bins over the column's range, or the reason there are none."""

    bins: list[tuple[float, float, int]] = field(default_factory=list)
    suppressed: str | None = None

    @property
    def peak(self) -> int:
        return max((c for _, _, c in self.bins), default=0)


@dataclass(frozen=True)
class LengthSummary:
    """How long the strings are. Truncation and padding both show up here."""

    shortest: int | None = None
    longest: int | None = None
    mean: float | None = None

    @property
    def is_fixed_width(self) -> bool:
        return (
            self.shortest is not None
            and self.shortest == self.longest
        )


@dataclass(frozen=True)
class DateCoverage:
    """
    Which days the column actually covers, bucketed to whole days.

    The cheap ancestor of Phase 9's `calendar_coverage` (F10). It reports gaps
    and says nothing about what they mean: a missing weekend is a business
    calendar, a missing fortnight is usually a feed outage, and no query can
    tell those apart.
    """

    first: str | None = None
    last: str | None = None
    days_in_span: int = 0
    days_present: int = 0
    days_missing: int = 0
    longest_gap: int = 0

    @property
    def coverage(self) -> float:
        return self.days_present / self.days_in_span if self.days_in_span else 0.0


@dataclass
class ColumnDetail:
    """One column's page."""

    dataset_name: str
    column: ColumnProfile
    top: list[ValueCount] = field(default_factory=list)
    distinct_not_shown: int = 0
    histogram: Histogram | None = None
    lengths: LengthSummary | None = None
    coverage: DateCoverage | None = None
    top_n: int = TOP_VALUES

    @property
    def name(self) -> str:
        return self.column.name


def _refuse_unknown_column(dataset_name: str, column: str, known: list[str]) -> str:
    return Refusal(
        reason=Reason.COLUMN_NOT_FOUND,
        what=f"'{dataset_name}' has no column called '{column}'.",
        why=(
            "the column list comes from the loaded table, so a name that is "
            "not in it cannot be profiled -- including a name that was correct "
            "before the table was reloaded."
        ),
        state=f"columns: {', '.join(known)}",
        next_call=f'profile_dataset(dataset_name="{dataset_name}")',
    ).to_text()


def _top_values(con, dataset_name: str, column: str, top_n: int) -> list[ValueCount]:
    col = _q(column)
    rows = con.execute(
        f"SELECT {col} AS v, count(*) AS n FROM {_q(dataset_name)} "
        f"GROUP BY 1 ORDER BY n DESC, 1 LIMIT {top_n}"
    ).fetchall()
    total = con.execute(f"SELECT count(*) FROM {_q(dataset_name)}").fetchone()[0]
    return [
        ValueCount(value=v, count=n, share=(n / total if total else 0.0))
        for v, n in rows
    ]


def _histogram(con, dataset_name: str, col: ColumnProfile, bins: int) -> Histogram:
    """
    Equal-width bins, boundaries from DuckDB rather than from arithmetic here.

    `equi_width_bins(lo, hi, n, true)` rounds the boundaries to readable
    numbers, which is why a histogram of prices breaks at 0, 20, 40 rather than
    at 1.5, 21.3, 41.1. Verified on 1.5.5: a constant column returns a single
    bin rather than failing, and `histogram` over an all-null column returns
    NULL rather than an empty map.
    """
    n = col.numeric
    if n is None or col.evidence.non_null == 0:
        return Histogram(suppressed="the column holds no values")
    lo, hi = col.evidence.min_value, col.evidence.max_value
    try:
        boundaries = con.execute(
            "SELECT equi_width_bins(?::DOUBLE, ?::DOUBLE, ?, true)", [lo, hi, bins]
        ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return Histogram(suppressed=f"bin boundaries could not be computed: {exc}")
    if not boundaries:
        return Histogram(suppressed="the column has no range to divide")

    counts = con.execute(
        f"SELECT histogram({_q(col.name)}::DOUBLE, ?::DOUBLE[]) "
        f"FROM {_q(dataset_name)}",
        [boundaries],
    ).fetchone()[0]
    if not counts:
        return Histogram(suppressed="the column holds no values")

    out: list[tuple[float, float, int]] = []
    previous = float(lo)
    for boundary in boundaries:
        out.append((previous, float(boundary), counts.get(boundary, 0)))
        previous = float(boundary)
    return Histogram(bins=out)


def _lengths(con, dataset_name: str, column: str) -> LengthSummary:
    col = _q(column)
    shortest, longest, mean = con.execute(
        f"SELECT min(length({col})), max(length({col})), "
        f"avg(length({col}))::DOUBLE FROM {_q(dataset_name)}"
    ).fetchone()
    return LengthSummary(shortest=shortest, longest=longest, mean=mean)


def _coverage(con, dataset_name: str, column: str) -> DateCoverage:
    """
    Days present and days missing across the column's own span.

    Bucketed to whole days on purpose: a TIMESTAMP column has a distinct value
    almost every row, and "3,412 distinct timestamps" answers nothing about
    whether a week is missing.
    """
    col = _q(column)
    table = _q(dataset_name)
    row = con.execute(
        f"""
        WITH present AS (
            SELECT DISTINCT {col}::DATE AS day FROM {table} WHERE {col} IS NOT NULL
        ),
        span AS (SELECT min(day) AS lo, max(day) AS hi FROM present),
        days AS (
            SELECT unnest(generate_series(lo, hi, INTERVAL 1 DAY))::DATE AS day
            FROM span WHERE lo IS NOT NULL
        ),
        missing AS (
            SELECT d.day FROM days d
            LEFT JOIN present p ON p.day = d.day
            WHERE p.day IS NULL
        ),
        runs AS (
            SELECT day - (row_number() OVER (ORDER BY day))::INTEGER AS anchor
            FROM missing
        )
        SELECT
            (SELECT lo FROM span),
            (SELECT hi FROM span),
            (SELECT count(*) FROM days),
            (SELECT count(*) FROM present),
            (SELECT count(*) FROM missing),
            COALESCE((SELECT max(n) FROM (
                SELECT count(*) AS n FROM runs GROUP BY anchor
            )), 0)
        """
    ).fetchone()
    first, last, in_span, present, missing, longest = row
    return DateCoverage(
        first=None if first is None else str(first),
        last=None if last is None else str(last),
        days_in_span=in_span or 0,
        days_present=present or 0,
        days_missing=missing or 0,
        longest_gap=longest or 0,
    )


def profile_column(
    con,
    dataset_name: str,
    column: str,
    *,
    top_n: int = TOP_VALUES,
    bins: int = HISTOGRAM_BINS,
    table: TableProfile | None = None,
) -> ColumnDetail:
    """
    Everything about one column. Reads; writes and stores nothing.

    `table` lets a caller that already has a `TableProfile` pass it in rather
    than paying for a second scan of every column. When it is omitted this
    computes one, because the column's nulls, missing tokens, type reading and
    outliers must be the SAME numbers the table profile reports -- recomputing
    them here with slightly different SQL is how two views of one column start
    disagreeing.
    """
    top_n = max(1, min(top_n, MAX_TOP_VALUES))
    profile = table if table is not None else profile_table(con, dataset_name, only=column)

    try:
        col = profile.column(column)
    except KeyError:
        raise ContractRefused(
            _refuse_unknown_column(
                dataset_name, column, [c.name for c in profile.columns]
            )
        ) from None

    top = _top_values(con, dataset_name, column, top_n)
    detail = ColumnDetail(
        dataset_name=dataset_name,
        column=col,
        top=top,
        # NULL is a row of the list and not a distinct value: rating's list read "7 of 6
        # distinct" and hid one value too few (recheck, 25/09/2026).
        distinct_not_shown=max(0, col.evidence.distinct
                               - sum(1 for v in top if v.value is not None)),
        top_n=top_n,
    )

    if _is_numeric(col.dtype):
        detail.histogram = _histogram(con, dataset_name, col, bins)
    if _is_text(col.dtype):
        detail.lengths = _lengths(con, dataset_name, column)
    if _is_temporal(col.dtype):
        detail.coverage = _coverage(con, dataset_name, column)
    return detail


def _num(value: float) -> str:
    """
    A bin boundary as a person reads it.

    `.4g` turns 10000 into `1e+04`, which is correct and unreadable in a table
    of prices. Six significant figures with a thousands separator covers every
    magnitude a bin boundary realistically has, and anything past that is
    genuinely scientific.
    """
    return f"{value:,.6g}"


def _bar(count: int, peak: int, width: int = 30) -> str:
    if peak <= 0:
        return ""
    return "#" * max(1 if count else 0, round(count / peak * width))


def render_column(detail: ColumnDetail) -> str:
    """
    One column as a person reads it.

    No file, no path: everything here is bounded, and the one truncation --
    the value list -- ends in a call that widens it.
    """
    col = detail.column
    out = [
        f"{detail.dataset_name}.{detail.name}",
        "",
        f"  {col.sentence()}",
    ]

    if detail.top and col.evidence.is_unique:
        out += [
            "",
            f"Every one of the {col.evidence.row_count:,} values occurs exactly "
            f"once, so there is no frequency table to show. What a unique "
            f"column has to say is below.",
        ]
    elif detail.top:
        shown = sum(1 for v in detail.top if v.value is not None)
        nulls = " and (null)" if shown < len(detail.top) else ""
        out += ["", f"Most frequent value(s), {shown} of "
                    f"{col.evidence.distinct:,} distinct{nulls}:", ""]
        width = max(len(v.display()) for v in detail.top)
        for v in detail.top:
            out.append(
                f"  {v.display():<{width}}  {v.count:>9,}  {v.share * 100:5.1f}%"
            )
        if detail.distinct_not_shown:
            out += [
                "",
                f"{detail.distinct_not_shown:,} further distinct value(s) are "
                f"not shown. To widen the list:",
                f'  profile_column(dataset_name="{detail.dataset_name}", '
                f'column="{detail.name}", top_n={min(detail.top_n * 5, MAX_TOP_VALUES)})',
            ]
        if _is_text(col.dtype):
            out += [
                "",
                "Read that list for values that MEAN absent. This profile "
                "counts only the published missing-value vocabulary "
                "(pandas/pyarrow), which deliberately excludes judgement "
                "calls, so anything of that kind is a decision for you rather "
                "than for a constant.",
            ]

    if detail.histogram is not None:
        if detail.histogram.suppressed:
            out += ["", f"No distribution: {detail.histogram.suppressed}."]
        else:
            peak = detail.histogram.peak
            out += ["", "Distribution:", ""]
            for lo, hi, count in detail.histogram.bins:
                out.append(
                    f"  {_num(lo):>14} to {_num(hi):<14} {count:>9,}  "
                    f"{_bar(count, peak)}"
                )

    if detail.lengths is not None and detail.lengths.shortest is not None:
        lengths = detail.lengths
        line = (
            f"Value length: {lengths.shortest} to {lengths.longest} characters"
            f", mean {lengths.mean:.1f}"
        )
        if lengths.is_fixed_width:
            line = (
                f"Value length: every value is {lengths.shortest} characters. "
                f"Fixed width usually means a code rather than free text."
            )
        out += ["", line]

    if detail.coverage is not None:
        c = detail.coverage
        if c.first is None:
            out += ["", "No dates present, so nothing to cover."]
        else:
            out += [
                "",
                f"Calendar: {c.first} to {c.last}, {c.days_present:,} of "
                f"{c.days_in_span:,} days present "
                f"({c.coverage * 100:.1f}%).",
            ]
            if c.days_missing:
                out.append(
                    f"  {c.days_missing:,} day(s) have no rows; the longest "
                    f"unbroken gap is {c.longest_gap:,} day(s). Whether that "
                    f"is a closed weekend or a broken feed is not something "
                    f"the data says."
                )
    return "\n".join(out)


__all__ = [
    "HISTOGRAM_BINS",
    "MAX_TOP_VALUES",
    "TOP_VALUES",
    "ColumnDetail",
    "DateCoverage",
    "Histogram",
    "LengthSummary",
    "ValueCount",
    "profile_column",
    "render_column",
]
