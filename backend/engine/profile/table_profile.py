"""
What a table looks like, counted. No judgement, no fixes.

`evidence.py` counts the four things a contract needs: rows, non-null,
distinct, range. This adds the four a person reading a table for the first time
asks for and a contract does not -- what the numbers average out to, where the
middle is, how far they spread, and how much of this is missing in ways a null
count cannot see.

It reuses `column_stats` rather than recomputing. There is exactly one
implementation of "how many nulls in region" in this codebase, and the day
there are two they will disagree inside the same chat with nothing to tell a
reader which is lying.

Three things here are worth reading before trusting the output.

**A null count is not a missing-data count.** `gaps_and_dupes.csv` opens with

    ORD-00002,4,N/A,10,Online,2829.54

That column has zero nulls and is missing a value. A profile reporting "0%
null" is technically true and practically a lie, and it is the kind of
true-and-wrong that survives into a chart. So blanks and the standard missing
tokens are counted separately and reported beside the nulls. They are never
rewritten: rewriting is cleaning, cleaning is Phase 6, and Phase 6 has an
approval gate for a reason.

**The vocabulary is not invented here.** It was, once, and the first run on a
six-row fixture reported a column as 83% missing because `unknown` was in the
list I had reasoned my way to. The defensible figure was 67%. Checking the
reference implementations instead gives a narrower list and a better model:

    pandas 3.0.2       19 strings, fixed default, overridable per read
    pyarrow 25.0.1     17 strings -- pandas' minus '<NA>' and 'None'
    Frictionless       missingValues defaults to [''], a DECLARED schema
                       property; [] disables the conversion entirely

`unknown`, `missing`, `none`, `-`, `--` and `?` are in none of them.
`MISSING_VALUES` below is the pandas/pyarrow intersection, upper-cased, with
the empty string removed because blanks are counted separately. Where those two
disagree -- `None` and `<NA>` -- the disagreement is the edge of consensus and
both are left out. `None` is also exactly the value that can be a real payment
type.

The failure modes are not symmetric, which is the whole argument. Over-
detection puts a wrong number in a headline, and headlines are what get
repeated. Under-detection leaves the value sitting in a frequency table where a
person reads it and rules on it. Tune toward the recoverable error.

Following Frictionless, the list is an argument rather than a constant: pass
`missing_values=[...]` to widen it for a source that writes `NR` or `#REF!`,
and `missing_values=[]` to switch detection off. Step 8 puts it on the contract
so the answer is declared once and read by Phase 6 and Phase 8 alike.

**Duplicate rows use `SELECT DISTINCT *`, and that is the THIRD null rule in
this codebase.** `count(DISTINCT c)` drops nulls (evidence finding 1).
`count(DISTINCT (a, b))` does not (finding 2). `DISTINCT *` also does not, and
two rows identical including their nulls collapse to one -- verified on 1.5.5:
five rows holding one exact NULL-bearing duplicate return four. Here that
behaviour is the one we want, because two identical rows ARE duplicates
whatever their nulls. It is used deliberately and said out loud, or someone
reads it as the same bug a third time.

**A narrower type is only claimed when the wider one round-trips.** DuckDB's
`TRY_CAST` converts rather than refusing where it can: `TRY_CAST('4.5' AS
BIGINT)` returns 5, and `TRY_CAST('2024-01-01 10:30:00' AS DATE)` returns
2024-01-01. Both succeed, both lose information, and both would have made this
module report a column as something it is not -- a decimal column reading as
integer, a timestamp column reading as date. So `BIGINT` and `DATE` carry a
guard: the value must survive the wider cast unchanged. `007`, `' 7 '` and
`1e3` still read as integers; `4.5` does not. A midnight timestamp still reads
as a date; 10:30 does not.

**An IQR of zero is not a licence to flag everything.** Tukey's fences are
Q1 - 1.5*IQR and Q3 + 1.5*IQR, and matplotlib's `boxplot.whiskers` default of
1.5 is where the constant comes from. On a column that is 95% one value the
quartiles coincide, the fences collapse to a point, and every remaining row is
technically outside them -- 5 of 100 in a probe where the five "outliers" were
1, 2 and 3. That is arithmetic nobody should act on, so a zero IQR suppresses
the count and says why instead.

Note on the package name: `profile` shadows a stdlib module. Every import here
is absolute, so nothing resolves to the stdlib profiler, but a bare
`import profile` anywhere in this tree would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from backend.engine.contract.evidence import (
    ColumnEvidence,
    _is_numeric,
    _is_text,
    _q,
    column_stats,
    suggest_role,
)

# The pandas/pyarrow intersection, upper-cased, without the empty string.
# Verified by running both rather than recalled. Matching is case-insensitive,
# which is the one deliberate widening: pandas ships 'NA', 'n/a' and 'nan' as
# separate entries, implying exact matching, so 'Null' slips through it. This
# widens case and never vocabulary.
MISSING_VALUES = (
    "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NAN",
    "1.#IND", "1.#QNAN", "N/A", "NA", "NAN", "NULL",
)

# Which token appeared costs one query per affected column. Capped and the
# remainder reported, the way evidence caps pair probes.
MAX_BREAKDOWN_COLUMNS = 20

# At or above this share of missing values a column is named in the summary
# rather than left for the reader to find in the table.
MOSTLY_MISSING = 0.5

# Types a text column is tested against, narrowest first. BIGINT and DATE are
# guarded (see _cast_ok): TRY_CAST will round '4.5' to 5 and drop the time from
# a timestamp, so an unguarded test claims a narrower type than the data has.
CAST_CANDIDATES = ("BIGINT", "DOUBLE", "DATE", "TIMESTAMP", "BOOLEAN")

# At or above this share, a text column that parses as something else is named
# in the summary. This is a DISPLAY threshold and not a claim about the data --
# the exact ratio is in the table for every text column either way.
#
# pandas, DuckDB's CSV sniffer and Frictionless all infer types all-or-nothing:
# one unparseable value and the column stays text, because they must choose a
# storage type and a lossy one is worse than a wide one. They are answering a
# different question. A profile reports, and the case worth reporting is
# precisely the one they stay silent about -- 99.8% numeric with three pieces
# of junk in it. Great Expectations' `mostly` parameter is the same idea for
# the same reason, though I have not verified its default here.
TYPE_MISMATCH_SHARE = 0.9

# Tukey's constant. matplotlib 3.10.8 ships boxplot.whiskers = 1.5, verified
# rather than recalled, and pandas and seaborn follow it.
OUTLIER_K = 1.5

# Values shown as examples of what would not cast. Three is enough to see the
# shape of the problem and short enough to sit in a summary line.
CAST_EXAMPLE_LIMIT = 3


def _lit(value: str) -> str:
    """A single-quoted SQL literal. Doubles any embedded quote.

    `missing_values` can arrive from a contract, which means from a person,
    which means it is not put into a query unescaped.
    """
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class TypeReading:
    """What a text column parses as, if anything, and how completely.

    `considered` excludes nulls, blanks and the missing tokens -- those are
    already reported as missing, and counting them again as cast failures
    would describe one problem twice and understate the ratio.
    """

    considered: int = 0
    ratios: dict[str, float] = field(default_factory=dict)
    best: str | None = None
    ratio: float = 0.0
    examples: list[str] = field(default_factory=list)
    #: Values with a zero before another digit -- what a number would drop (Cleanup Step 12).
    leading_zeros: int = 0
    #: Values that parse as a number only once currency signs, thousands separators and spaces go.
    currency_text: int = 0

    @property
    def is_currency_text(self) -> bool:
        """Numbers written for people: '₹10,846.00'. Named when the plain cast would not be."""
        plain = self.best is not None and self.ratio >= TYPE_MISMATCH_SHARE
        return (not plain and self.considered > 0
                and self.currency_text / self.considered >= TYPE_MISMATCH_SHARE)

    @property
    def is_total(self) -> bool:
        """Every value parses. This is the claim pandas or DuckDB would act on."""
        return self.best is not None and self.considered > 0 and self.ratio >= 1.0

    @property
    def is_worth_naming(self) -> bool:
        return (self.best is not None and self.ratio >= TYPE_MISMATCH_SHARE) or self.is_currency_text

    def _zeros(self) -> str:
        if self.leading_zeros and self.best in ("BIGINT", "DOUBLE"):
            return (f", but {self.leading_zeros:,} have leading zeros a number would drop -- "
                    f"a code, not a quantity")
        return ""

    def sentence(self) -> str:
        if self.is_currency_text:
            return (
                f"{self.currency_text / self.considered * 100:.1f}% of its {self.considered:,} "
                f"values read as numbers once the currency sign and thousands separators are "
                f"removed"
            )
        if self.best is None:
            return ""
        if self.is_total:
            return (
                f"every one of its {self.considered:,} values parses as "
                f"{self.best}{self._zeros()}"
            )
        failed = self.considered - round(self.ratio * self.considered)
        shown = ", ".join(repr(e) for e in self.examples)
        tail = f"; {shown} did not" if shown else ""
        return (
            f"{self.ratio * 100:.1f}% of its {self.considered:,} values parse "
            f"as {self.best}, {failed:,} did not{tail}{self._zeros()}"
        )


@dataclass(frozen=True)
class OutlierSummary:
    """Values outside Tukey's fences, or the reason there is no count."""

    lower_fence: float | None = None
    upper_fence: float | None = None
    below: int = 0
    above: int = 0
    suppressed: str | None = None

    method = f"Tukey IQR fences, k={OUTLIER_K}"

    @property
    def total(self) -> int:
        return self.below + self.above

    def sentence(self) -> str:
        if self.suppressed:
            return f"no outlier count: {self.suppressed}"
        if not self.total:
            return f"no values outside the fences ({self.method})"
        return (
            f"{self.total:,} value(s) outside "
            f"[{self.lower_fence:,.4g}, {self.upper_fence:,.4g}] "
            f"({self.below:,} below, {self.above:,} above; {self.method})"
        )


@dataclass(frozen=True)
class NumericSummary:
    """
    Where the middle is and how far it spreads.

    Every field is None-able, and that is not defensiveness: a column of all
    nulls has no mean and a single-row table has no sample standard deviation.
    Verified on 1.5.5 -- both return NULL rather than raising, and a zero
    written in their place would be a number nobody computed.
    """

    mean: float | None = None
    stddev: float | None = None
    q1: float | None = None
    median: float | None = None
    q3: float | None = None

    @property
    def iqr(self) -> float | None:
        """Q3 - Q1. Step 3's outlier fences are built from this."""
        if self.q1 is None or self.q3 is None:
            return None
        return self.q3 - self.q1


@dataclass(frozen=True)
class ColumnProfile:
    """One column: what evidence counted, plus what a reader asks next."""

    evidence: ColumnEvidence
    role: str
    role_reason: str
    numeric: NumericSummary | None = None
    blank_count: int = 0
    missing_token_count: int = 0
    missing_token_breakdown: dict[str, int] = field(default_factory=dict)
    type_reading: TypeReading | None = None
    outliers: OutlierSummary | None = None

    @property
    def name(self) -> str:
        return self.evidence.name

    @property
    def dtype(self) -> str:
        return self.evidence.dtype

    @property
    def hidden_missing(self) -> int:
        """Missing values no null count would find."""
        return self.blank_count + self.missing_token_count

    @property
    def missing_count(self) -> int:
        return self.evidence.null_count + self.hidden_missing

    @property
    def missing_ratio(self) -> float:
        rows = self.evidence.row_count
        return self.missing_count / rows if rows else 0.0

    def sentence(self) -> str:
        """
        This column's line: evidence's sentence, plus what it cannot say.

        Built ON `evidence.sentence()` rather than beside it. When that wording
        changes this changes with it, and the two never drift into describing
        one column two ways.
        """
        out = self.evidence.sentence()
        extra = []
        if self.type_reading is not None and self.type_reading.is_worth_naming:
            extra.append(f"It is stored as {self.dtype} but "
                         f"{self.type_reading.sentence()}.")
        if self.outliers is not None and self.outliers.total:
            extra.append(f"It has {self.outliers.sentence()}.")
        tail = (" " + " ".join(extra)) if extra else ""
        if not self.hidden_missing:
            return out + tail
        parts = []
        if self.blank_count:
            parts.append(f"{self.blank_count:,} blank or whitespace-only")
        if self.missing_token_count:
            named = ", ".join(
                f"{k} x{v:,}"
                for k, v in sorted(self.missing_token_breakdown.items())
            )
            parts.append(named or f"{self.missing_token_count:,} standard tokens")
        return (
            f"{out} A further {self.hidden_missing:,} value(s) read as missing "
            f"without being null: {'; '.join(parts)}.{tail}"
        )


@dataclass
class TableProfile:
    """One table, counted. Stores nothing and writes nothing."""

    dataset_name: str
    row_count: int
    columns: list[ColumnProfile]
    duplicate_rows: int | None = None
    missing_values: tuple[str, ...] = MISSING_VALUES
    notes: list[str] = field(default_factory=list)

    # The first twelve are what the envelope previews inline, and the order is
    # a decision: a first look is for the missing-data story, so the numeric
    # summary sits behind it in the file rather than in front of it on screen.
    HEADERS = [
        "column", "dtype", "role", "nulls", "null_pct", "blank",
        "reads_missing", "missing_pct", "distinct", "distinct_pct",
        "min", "max",
        "mean", "median", "stddev", "q1", "q3",
        "reads_as", "reads_as_pct", "parse_failures",
        "outliers_below", "outliers_above", "fence_low", "fence_high",
    ]

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def column(self, name: str) -> ColumnProfile:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def to_rows(self) -> list[list[object]]:
        rows: list[list[object]] = []
        for c in self.columns:
            e, n, t, o = c.evidence, c.numeric, c.type_reading, c.outliers
            rows.append([
                c.name, c.dtype, c.role,
                e.null_count, round(e.null_ratio * 100, 1),
                c.blank_count, c.missing_token_count,
                round(c.missing_ratio * 100, 1),
                e.distinct, round(e.distinct_ratio * 100, 1),
                e.min_value, e.max_value,
                None if n is None else n.mean,
                None if n is None else n.median,
                None if n is None else n.stddev,
                None if n is None else n.q1,
                None if n is None else n.q3,
                None if t is None else t.best,
                None if t is None or t.best is None else round(t.ratio * 100, 1),
                None if t is None or t.best is None
                else t.considered - round(t.ratio * t.considered),
                None if o is None or o.suppressed else o.below,
                None if o is None or o.suppressed else o.above,
                None if o is None or o.suppressed else o.lower_fence,
                None if o is None or o.suppressed else o.upper_fence,
            ])
        return rows

    def summary_lines(self) -> list[str]:
        """
        The findings, for the top of the envelope.

        What belongs here is what changes what someone does next. A column half
        missing changes an analysis; a column 0.2% missing does not, and
        putting both in the summary means neither gets read.
        """
        out = [f"{self.row_count:,} rows, {self.column_count} columns."]

        if self.duplicate_rows is None:
            out.append("Duplicate rows could not be counted -- see the notes.")
        elif self.duplicate_rows:
            pct = self.duplicate_rows / self.row_count * 100 if self.row_count else 0
            out.append(
                f"{self.duplicate_rows:,} row(s) are exact duplicates of another "
                f"row ({pct:.1f}%), identical including their nulls."
            )
        else:
            out.append("No exact duplicate rows.")

        hidden = [c for c in self.columns if c.hidden_missing]
        if hidden:
            named = ", ".join(f"{c.name} ({c.hidden_missing:,})" for c in hidden[:5])
            more = f", and {len(hidden) - 5} more" if len(hidden) > 5 else ""
            out.append(
                f"{len(hidden)} column(s) hold values that read as missing "
                f"without being null: {named}{more}. Nothing was rewritten."
            )

        mostly = [c for c in self.columns if c.missing_ratio >= MOSTLY_MISSING]
        if mostly:
            out.append(
                f"{len(mostly)} column(s) are at least "
                f"{int(MOSTLY_MISSING * 100)}% missing: "
                + ", ".join(
                    f"{c.name} ({c.missing_ratio * 100:.0f}%)" for c in mostly
                )
                + "."
            )

        money = [c for c in self.columns
                 if c.type_reading is not None and c.type_reading.is_currency_text]
        if money:
            out.append(
                f"{len(money)} text column(s) hold numbers written with a currency sign or "
                f"thousands separator: " + ", ".join(c.name for c in money)
                + ". Nothing was converted; a cleaning plan offers the conversion.")
        misread = [
            c for c in self.columns
            if c.type_reading is not None and c.type_reading.is_worth_naming
            and not c.type_reading.is_currency_text
        ]
        if misread:
            total = [c for c in misread if c.type_reading.is_total]
            partial = [c for c in misread if not c.type_reading.is_total]
            if total:
                out.append(
                    f"{len(total)} text column(s) hold nothing but values of "
                    f"another type: "
                    + ", ".join(f"{c.name} -> {c.type_reading.best}"
                                + (" (zero-padded: a code)" if c.type_reading.leading_zeros else "")
                                for c in total)
                    + ". Nothing was converted."
                )
            if partial:
                out.append(
                    f"{len(partial)} text column(s) are mostly another type "
                    f"with exceptions: "
                    + ", ".join(
                        f"{c.name} -> {c.type_reading.best} "
                        f"({c.type_reading.ratio * 100:.1f}%)"
                        for c in partial
                    )
                    + ". The exceptions are the interesting part."
                )

        flagged_outliers = [
            c for c in self.columns
            if c.outliers is not None and c.outliers.total and c.role == "measure"
        ]
        if flagged_outliers:
            out.append(
                f"{len(flagged_outliers)} measure(s) have values outside "
                f"{OutlierSummary.method}: "
                + ", ".join(
                    f"{c.name} ({c.outliers.total:,})" for c in flagged_outliers
                )
                + ". Outside a fence is not the same as wrong."
            )

        suppressed = [
            c for c in self.columns
            if c.outliers is not None and c.outliers.suppressed
        ]
        if suppressed:
            out.append(
                f"{len(suppressed)} numeric column(s) got no outlier count: "
                + ", ".join(
                    f"{c.name} ({c.outliers.suppressed})" for c in suppressed
                )
                + "."
            )

        constant = [c for c in self.columns if c.evidence.is_constant]
        if constant:
            out.append(
                f"{len(constant)} column(s) hold one value or none and cannot be "
                f"grouped by: " + ", ".join(c.name for c in constant) + "."
            )

        unique = [c for c in self.columns if c.evidence.is_unique]
        if unique:
            out.append(
                "Unique and null-free: " + ", ".join(c.name for c in unique)
                + ". Uniqueness here is a property of the rows that happen to be "
                "loaded, not a key."
            )
        return out

    def to_text(self) -> str:
        """A reading of the whole table, one line per column."""
        lines = [f"Profile of {self.dataset_name}", ""]
        lines += [f"  - {s}" for s in self.summary_lines()]
        lines += ["", "Columns:"]
        lines += [f"  - {c.sentence()}" for c in self.columns]
        if self.notes:
            lines += ["", "Notes:"] + [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


# Over finite values only. stddev_samp over an Infinity raises "STDDEV_SAMP is out of range",
# and profile_dataset answered a column holding one with an exception (P14-O6, B4). The sixth
# expression counts what was set aside, for the note. Floating-point columns only: a FILTER on
# every column of a 200-column table took the profile from 4 s to 24 s (step 7, measured).
NUMERIC_EXPRS = 6
_FLOATING = ("DOUBLE", "FLOAT", "REAL")


def _floating(dtype: str) -> bool:
    return dtype.split("(")[0].strip().upper() in _FLOATING


def _numeric_exprs(name: str, dtype: str = "DOUBLE") -> list[str]:
    col = _q(name)
    keep = f"FILTER (WHERE isfinite({col}))" if _floating(dtype) else ""
    return [
        f"(avg({col}) {keep})::DOUBLE",
        f"(stddev_samp({col}) {keep})::DOUBLE",
        f"(quantile_cont({col}, 0.25) {keep})::DOUBLE",
        f"(median({col}) {keep})::DOUBLE",
        f"(quantile_cont({col}, 0.75) {keep})::DOUBLE",
        f"count({col}) FILTER (WHERE NOT isfinite({col}))" if _floating(dtype) else "0",
    ]


def _text_exprs(name: str, listed: str) -> list[str]:
    col = _q(name)
    blank = f"count(*) FILTER (WHERE trim({col}) = '')"
    if not listed:
        # Frictionless semantics: an empty vocabulary disables the conversion
        # rather than meaning "use the default". The zero here is a real count.
        return [blank, "0"]
    return [blank, f"count(*) FILTER (WHERE upper(trim({col})) IN ({listed}))"]


def _cast_ok(col: str, sql_type: str) -> str:
    """
    Whether one value parses as `sql_type` WITHOUT losing anything.

    TRY_CAST converts rather than refusing where it can, which is the trap:
    TRY_CAST('4.5' AS BIGINT) is 5, and TRY_CAST('2024-01-01 10:30:00' AS DATE)
    is 2024-01-01. Both succeed and both drop information, so an unguarded test
    reports a decimal column as integer and a timestamp column as date.

    The guard for a narrower type is that the wider one round-trips it. `007`,
    `' 7 '` and `1e3` still read as integers; `4.5` does not. A midnight
    timestamp still reads as a date; 10:30 does not.
    """
    if sql_type == "BIGINT":
        return (
            f"TRY_CAST({col} AS BIGINT) IS NOT NULL AND "
            f"TRY_CAST({col} AS DOUBLE) = CAST(TRY_CAST({col} AS BIGINT) AS DOUBLE)"
        )
    if sql_type == "DATE":
        return (
            f"TRY_CAST({col} AS DATE) IS NOT NULL AND "
            f"TRY_CAST({col} AS TIMESTAMP) = "
            f"CAST(TRY_CAST({col} AS DATE) AS TIMESTAMP)"
        )
    return f"TRY_CAST({col} AS {sql_type}) IS NOT NULL"


def _considered(col: str, listed: str) -> str:
    """Values a cast test should be judged on: present, and not already
    reported as missing."""
    clause = f"{col} IS NOT NULL AND trim({col}) <> ''"
    if listed:
        clause += f" AND upper(trim({col})) NOT IN ({listed})"
    return clause


def _cast_exprs(name: str, listed: str) -> list[str]:
    col = _q(name)
    keep = _considered(col, listed)
    out = [f"count(*) FILTER (WHERE {keep})"]
    for sql_type in CAST_CANDIDATES:
        out.append(
            f"count(*) FILTER (WHERE {keep} AND {_cast_ok(col, sql_type)})"
        )
    # Two counts a cast cannot see (Cleanup Step 12): the zeros a number drops, and numbers
    # written with a currency sign or thousands separator.
    out.append(f"count(*) FILTER (WHERE {keep} AND regexp_matches({col}, '^[+-]?0[0-9]'))")
    out.append(
        f"count(*) FILTER (WHERE {keep} AND regexp_matches({col}, '[₹$€£¥,]') AND "
        f"TRY_CAST(regexp_replace({col}, '[₹$€£¥,\\s]', '', 'g') AS DOUBLE) IS NOT NULL)")
    return out


def _cast_counts(con, dataset_name: str, name: str, listed: str) -> list[int]:
    """`_cast_exprs` counted over the column's distinct values, each weighted by its rows.

    A cast is a function of the value, so the counts are the row counts exactly
    (tests/test_benchmark_fix_facts.py) -- and a column of four regions is cast four times,
    not once per row. Inside the table's one SELECT these casts cost 0.3-0.4 s per text column
    at 100,000 rows, which made profile_dataset 2.1 s there and 5.9 s at a million (P14-O15).
    """
    col = _q(name)
    weighted = [f"coalesce({e.replace('count(*)', 'sum(n)', 1)}, 0)"
                for e in _cast_exprs(name, listed)]
    row = con.execute(
        f"SELECT {', '.join(weighted)} FROM "
        f"(SELECT {col}, count(*) AS n FROM {_q(dataset_name)} GROUP BY {col})"
    ).fetchone()
    return [int(v) for v in row]


def _cast_examples(con, dataset_name: str, name: str, listed: str,
                   sql_type: str) -> list[str]:
    col = _q(name)
    rows = con.execute(
        f"SELECT DISTINCT {col} FROM {_q(dataset_name)} "
        f"WHERE {_considered(col, listed)} AND NOT ({_cast_ok(col, sql_type)}) "
        f"ORDER BY 1 LIMIT {CAST_EXAMPLE_LIMIT}"      # the same examples every run (D15)
    ).fetchall()
    return [r[0] for r in rows]


def _read_types(counts: list[int]) -> TypeReading:
    """
    Pick the narrowest type that fits best.

    `CAST_CANDIDATES` is ordered narrowest first and ties go to the earlier
    entry, which is why a column of 1s and 0s reads as BIGINT rather than
    BOOLEAN: both parse every value, and the integer reading assumes less.
    """
    considered, hits = counts[0], counts[1:]
    if not considered:
        return TypeReading(considered=0)
    ratios = {
        t: hit / considered for t, hit in zip(CAST_CANDIDATES, hits) if hit
    }
    if not ratios:
        return TypeReading(considered=considered)
    best_ratio = max(ratios.values())
    best = next(t for t in CAST_CANDIDATES if ratios.get(t) == best_ratio)
    return TypeReading(
        considered=considered, ratios=ratios, best=best, ratio=best_ratio
    )


def _outlier_counts(
    con, dataset_name: str, numeric: list, summaries: dict
) -> dict[str, OutlierSummary]:
    """
    Values outside Tukey's fences, in one pass for every numeric column.

    The fences come from quantiles already computed, so this costs one scan
    rather than one per column. A zero IQR is refused a count rather than
    given a meaningless one: on a column that is 95% a single value the
    quartiles coincide, the fences collapse to a point, and every other row is
    technically outside them.
    """
    out: dict[str, OutlierSummary] = {}
    live: list = []
    for c in numeric:
        n = summaries.get(c.name)
        if n is None or n.q1 is None or n.q3 is None:
            out[c.name] = OutlierSummary(
                suppressed="no quantiles -- the column holds no values"
            )
        elif not n.iqr:
            out[c.name] = OutlierSummary(
                suppressed=(
                    f"the quartiles are both {n.q1:,.4g}, so the fences "
                    f"collapse to a point and every other value would be "
                    f"flagged"
                )
            )
        else:
            live.append((c, n.q1 - OUTLIER_K * n.iqr, n.q3 + OUTLIER_K * n.iqr))

    if live:
        exprs = []
        for c, lo, hi in live:
            col = _q(c.name)
            fin = f" AND isfinite({col})" if _floating(c.dtype) else ""
            exprs += [
                f"count(*) FILTER (WHERE {col} < {lo!r}{fin})",
                f"count(*) FILTER (WHERE {col} > {hi!r}{fin})",
            ]
        row = con.execute(
            f"SELECT {', '.join(exprs)} FROM {_q(dataset_name)}"
        ).fetchone()
        for i, (c, lo, hi) in enumerate(live):
            out[c.name] = OutlierSummary(
                lower_fence=lo, upper_fence=hi,
                below=row[2 * i], above=row[2 * i + 1],
            )
    return out


def _duplicate_rows(
    con, dataset_name: str, row_count: int
) -> tuple[int | None, str | None]:
    """
    Rows that are exact duplicates of another, nulls included.

    The try/except is precautionary and I could not make it fire: on 1.5.5,
    LIST, MAP and UNION columns all survive `DISTINCT *`. It stays because a
    profile that crashes on one column is worth less than one that says which
    single count it could not produce, and because the set of types is not
    fixed forever. It has never been triggered by a real type -- the test that
    covers it makes the query raise deliberately.
    """
    try:
        distinct = con.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT * FROM {_q(dataset_name)})"
        ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        return None, (
            f"Exact-duplicate rows were not counted: {exc}. Every other number "
            f"here is unaffected."
        )
    return row_count - distinct, None


def _breakdown(con, dataset_name: str, name: str, listed: str) -> dict[str, int]:
    col = _q(name)
    rows = con.execute(
        # Named as written, not as the vocabulary spells it: 6,833 'n/a' were reported as
        # 'N/A x6,833', a value the column does not hold (recheck, 25/09/2026).
        f"SELECT trim({col}) AS token, count(*) "
        f"FROM {_q(dataset_name)} "
        f"WHERE upper(trim({col})) IN ({listed}) "
        f"GROUP BY 1 ORDER BY 2 DESC, 1"
    ).fetchall()
    return {token: n for token, n in rows}


def profile_table(
    con,
    dataset_name: str,
    *,
    missing_values: Sequence[str] = MISSING_VALUES,
    breakdown_cap: int = MAX_BREAKDOWN_COLUMNS,
    only: str | None = None,
) -> TableProfile:
    """
    Count everything about one loaded table.

    `only` restricts every per-column computation to one column, for profile_column. Each
    column's figures are computed independently of the others, so its numbers are the same
    ones the whole-table profile reports; the table-level duplicate count is not computed.
    Profiling one column of a 10M-row table cost as much as profiling all of it (Step 13).

    Reads. Writes nothing, stores nothing, proposes nothing, changes nothing.
    A dataset that is not loaded raises `ContractRefused` from `column_stats`,
    which is the refusal this codebase already produces for that.

    Two passes plus a capped third. `column_stats` is one SELECT for the counts
    it owns; this adds one SELECT for the aggregates it does not; the duplicate
    count is its own scan because `DISTINCT *` cannot ride along with
    aggregates. Which token appeared is one small query per affected column.
    """
    stats: list[ColumnEvidence] = column_stats(con, dataset_name, only=only)
    row_count = stats[0].row_count if stats else 0

    vocabulary = tuple(
        dict.fromkeys(v.strip().upper() for v in missing_values if v.strip())
    )
    listed = ", ".join(_lit(v) for v in vocabulary)

    numeric = [c for c in stats if _is_numeric(c.dtype)]
    text = [c for c in stats if _is_text(c.dtype)]

    exprs: list[str] = []
    for c in numeric:
        exprs += _numeric_exprs(c.name, c.dtype)
    for c in text:
        exprs += _text_exprs(c.name, listed)

    values: tuple = ()
    if exprs:
        values = con.execute(
            f"SELECT {', '.join(exprs)} FROM {_q(dataset_name)}"
        ).fetchone()

    summaries: dict[str, NumericSummary] = {}
    non_finite: dict[str, int] = {}
    for i, c in enumerate(numeric):
        mean, sd, q1, med, q3, bad = values[NUMERIC_EXPRS * i: NUMERIC_EXPRS * (i + 1)]
        summaries[c.name] = NumericSummary(
            mean=mean, stddev=sd, q1=q1, median=med, q3=q3
        )
        if bad:
            non_finite[c.name] = bad

    # Per text column: blank and token here; the type reading below, once per distinct value,
    # with Cleanup Step 12's two counts (leading zeros, currency text) last in _cast_exprs.
    offset = NUMERIC_EXPRS * len(numeric)
    blanks: dict[str, int] = {}
    tokens: dict[str, int] = {}
    readings: dict[str, TypeReading] = {}
    for i, c in enumerate(text):
        base = offset + 2 * i
        blanks[c.name], tokens[c.name] = values[base: base + 2]
        counts = _cast_counts(con, dataset_name, c.name, listed)
        reading = _read_types(counts[:-2])
        zeros, money = counts[-2:]
        readings[c.name] = TypeReading(
            considered=reading.considered, ratios=reading.ratios, best=reading.best,
            ratio=reading.ratio, leading_zeros=zeros, currency_text=money)

    notes: list[str] = [
        f"{name}: {n:,} value(s) are not a finite number (NaN or Infinity). Its mean, spread "
        f"and quartiles are over the finite values; propose_cleaning_plan offers to turn "
        f"these into nulls."
        for name, n in non_finite.items()
    ]
    flagged = [c.name for c in text if tokens.get(c.name)]
    breakdowns: dict[str, dict[str, int]] = {}
    for name in flagged[:breakdown_cap]:
        breakdowns[name] = _breakdown(con, dataset_name, name, listed)
    if len(flagged) > breakdown_cap:
        notes.append(
            f"{len(flagged)} column(s) hold values that read as missing; which "
            f"token appeared was looked up for the first {breakdown_cap}. The "
            f"counts themselves are complete for all of them."
        )

    named = [
        c.name for c in text
        if readings[c.name].is_worth_naming and not readings[c.name].is_total
        and not readings[c.name].is_currency_text
    ]
    for name in named[:breakdown_cap]:
        reading = readings[name]
        readings[name] = TypeReading(
            considered=reading.considered,
            ratios=reading.ratios,
            best=reading.best,
            ratio=reading.ratio,
            examples=_cast_examples(con, dataset_name, name, listed, reading.best),
            leading_zeros=reading.leading_zeros,
            currency_text=reading.currency_text,
        )

    outliers = _outlier_counts(con, dataset_name, numeric, summaries)

    columns: list[ColumnProfile] = []
    for c in stats:
        role, reason = suggest_role(c)
        columns.append(
            ColumnProfile(
                evidence=c,
                role=role,
                role_reason=reason,
                numeric=summaries.get(c.name),
                blank_count=blanks.get(c.name, 0),
                missing_token_count=tokens.get(c.name, 0),
                missing_token_breakdown=breakdowns.get(c.name, {}),
                type_reading=readings.get(c.name),
                outliers=outliers.get(c.name),
            )
        )

    dupes, dupe_note = (_duplicate_rows(con, dataset_name, row_count)
                        if only is None else (None, None))
    if dupe_note:
        notes.append(dupe_note)
    if flagged:
        notes.append(
            "The missing-value vocabulary is the pandas/pyarrow intersection "
            "and holds no judgement calls: 'unknown', 'none', '-' and '?' are "
            "in neither and are NOT counted here. If a source writes one of "
            "those it shows up in the column's frequency table instead, where "
            "a person decides. Nothing was rewritten either way."
        )

    if any(c.outliers is not None and c.outliers.total for c in columns):
        notes.append(
            f"An outlier here means outside {OutlierSummary.method} and "
            f"nothing more. A price that is genuinely ten times the others is "
            f"outside the fence and is not an error; a skewed distribution "
            f"puts a tail outside it by construction. Phase 9 compares IQR "
            f"against z-score and MAD, which disagree on purpose."
        )
    if any(
        c.type_reading is not None and c.type_reading.is_worth_naming
        for c in columns
    ):
        notes.append(
            "A column that parses as another type was NOT converted. "
            "TRY_CAST rounds and truncates where it can, so conversion is a "
            "decision with consequences and it belongs to Phase 6, behind its "
            "approval gate. What is reported here is only what parses."
        )

    return TableProfile(
        dataset_name=dataset_name,
        row_count=row_count,
        columns=columns,
        duplicate_rows=dupes,
        missing_values=vocabulary,
        notes=notes,
    )


__all__ = [
    "CAST_CANDIDATES",
    "CAST_EXAMPLE_LIMIT",
    "MAX_BREAKDOWN_COLUMNS",
    "MISSING_VALUES",
    "MOSTLY_MISSING",
    "OUTLIER_K",
    "TYPE_MISMATCH_SHARE",
    "ColumnProfile",
    "NumericSummary",
    "OutlierSummary",
    "TableProfile",
    "TypeReading",
    "profile_table",
]
