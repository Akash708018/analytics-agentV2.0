"""
What a loaded table can be made to say about itself.

The Dataset Contract (build guide Section 7) has two halves. One half is
checkable against the data: is this column unique, does it contain nulls, how
many distinct values, what is the earliest date. The other half is not
checkable at all: what one row means, whether an amount is net of tax, which
rows were excluded on purpose. This module does the first half and nothing
else. It proposes no contract, stores nothing, and decides nothing.

The separation is the point. Section 7 exists so that the agent uses your
definitions rather than inventing them, and the fastest way to lose that is to
let a plausible-looking guess arrive without the reader being told what it
rests on. So every fact here travels with the sentence that states it:

    order_id is unique across 200,000 of 200,000 rows, with no nulls.

Four findings from running this against DuckDB 1.5.5 shape the code, and each
one is a way of being wrong that reads as being right:

1. `count(DISTINCT c)` DROPS NULLS. A column holding 1, 2, NULL, NULL has
   count(DISTINCT c) = 2 and count(c) = 2, so a test of "distinct equals
   non-null" calls it unique. The correct single-column test compares against
   count(*), which nulls cannot satisfy.

2. `count(DISTINCT (a, b))` does NOT drop nulls -- a row whose columns are all
   NULL still counts, and two identical NULL-bearing rows collapse to one. So
   a composite CAN pass a uniqueness test while one of its columns is entirely
   NULL (verified: two rows, (1, NULL) and (2, NULL), distinct = 2 = row
   count). Uniqueness and null-freeness are separate checks, and a candidate
   that is unique-with-nulls is reported as exactly that rather than as a key.

3. Every pair containing an already-unique column is unique. Probing all 91
   pairs of a 14-column table returned 22 "unique pairs", 13 of which were just
   order_id with something else stapled on. A key with a redundant column is
   not a key, so columns that are unique on their own are excluded from pair
   probing entirely.

4. A near-continuous number is unique alongside almost anything. On
   clean_sales.csv -- 500 rows, unit_price holding 498 distinct values --
   eleven key candidates came back and ten of them were a price or a revenue
   paired with something irrelevant. Columns whose role reads as measure or
   free text are left out of pair search, because a key is made of columns
   that name a row rather than measure one.

And one thing this module cannot fix, only report: on 200,000 synthetic rows,
(customer_id, order_date), (order_date, unit_price) and (unit_price, memo) all
came back unique. They are unique by arithmetic accident of the generator, and
no amount of SQL distinguishes that from a real composite key. Uniqueness is
evidence. It is not a primary key until a person says so.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from backend.engine.contract import ContractRefused
from backend.engine.contract.refusals import Reason, Refusal

# Pair probing costs one query each. Ranked and capped rather than unbounded,
# because C(k, 2) on a 60-column table is 1,770 scans.
MAX_PAIR_PROBES = 200

# A VARCHAR column with more distinct values than this share of its rows reads
# as free text rather than as a category.
FREE_TEXT_RATIO = 0.5

# Above this many distinct values a low-ratio text column is still a dimension,
# but a wide one -- worth naming in the note.
WIDE_DIMENSION_DISTINCT = 50

# Numeric columns at or below this many distinct values are more likely a
# rating, a flag or a bucket than something to sum. Olist reviews are the
# reason: scores 1-5, J-shaped, and summing them is meaningless.
SMALL_NUMERIC_DISTINCT = 12

_NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC",
)
_TEMPORAL_TYPES = ("DATE", "TIMESTAMP", "TIME", "DATETIME")
_TEXT_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR", "UUID")

# Names that suggest an identifier. Matched on word boundaries so 'grid' and
# 'video' do not read as 'id'.
_IDENT_NAME_RE = re.compile(
    r"(^|_)(id|ids|key|code|codes|uuid|guid|no|num|number|sku|ref)($|_)", re.I
)

# Names that suggest a date even when the column arrived as text.
_DATE_NAME_RE = re.compile(
    r"(^|_)(date|dt|day|month|year|time|timestamp|created|updated|ordered|"
    r"delivered|shipped|approved|purchased)($|_)", re.I
)


def _q(name: str) -> str:
    """Double-quoted SQL identifier. Doubles any embedded quote."""
    return '"' + name.replace('"', '""') + '"'


def _base_type(dtype: str) -> str:
    """DECIMAL(18,2) -> DECIMAL, VARCHAR -> VARCHAR."""
    return dtype.split("(")[0].strip().upper()


def _is_whole(dtype: str) -> bool:
    return _base_type(dtype) in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
                                 "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT")


def _is_numeric(dtype: str) -> bool:
    return _base_type(dtype) in _NUMERIC_TYPES


def _is_temporal(dtype: str) -> bool:
    return _base_type(dtype) in _TEMPORAL_TYPES


def _is_text(dtype: str) -> bool:
    return _base_type(dtype) in _TEXT_TYPES


@dataclass
class ColumnEvidence:
    """One column, counted rather than described."""

    name: str
    dtype: str
    position: int
    row_count: int
    non_null: int
    distinct: int
    min_value: str | None = None
    max_value: str | None = None

    @property
    def null_count(self) -> int:
        return self.row_count - self.non_null

    @property
    def is_unique(self) -> bool:
        """Unique AND null-free. See finding 1 in the module docstring."""
        return self.row_count > 0 and self.distinct == self.row_count

    @property
    def distinct_ratio(self) -> float:
        return self.distinct / self.row_count if self.row_count else 0.0

    @property
    def null_ratio(self) -> float:
        return self.null_count / self.row_count if self.row_count else 0.0

    @property
    def is_constant(self) -> bool:
        return self.distinct <= 1

    def sentence(self) -> str:
        """The one line that states this column's shape."""
        bits = [
            f"{self.name} ({self.dtype}): {self.distinct:,} distinct value(s) "
            f"across {self.row_count:,} rows"
        ]
        if self.null_count:
            bits.append(
                f"{self.null_count:,} null ({self.null_ratio * 100:.1f}%)"
            )
        else:
            bits.append("no nulls")
        if self.min_value is not None and self.distinct > 1:
            bits.append(f"range {self.min_value} to {self.max_value}")
        return ", ".join(bits) + "."


@dataclass
class KeyCandidate:
    """A column or pair that identifies a row uniquely IN THIS DATA."""

    columns: tuple[str, ...]
    distinct: int
    row_count: int
    null_bearing: list[str] = field(default_factory=list)
    caution: str = ""

    @property
    def is_unique(self) -> bool:
        return self.row_count > 0 and self.distinct == self.row_count

    @property
    def usable(self) -> bool:
        """Unique and null-free. A key with a null in it is not a key."""
        return self.is_unique and not self.null_bearing

    def label(self) -> str:
        return " + ".join(self.columns)

    def sentence(self) -> str:
        head = (
            f"{self.label()} is unique across {self.distinct:,} of "
            f"{self.row_count:,} rows"
        )
        if self.null_bearing:
            head += (
                f", but {', '.join(self.null_bearing)} contains nulls, so it "
                f"cannot be a primary key as it stands"
            )
        else:
            head += ", with no nulls"
        return head + "." + (f" {self.caution}" if self.caution else "")


@dataclass
class DatasetEvidence:
    """Everything countable about one loaded table."""

    dataset_name: str
    row_count: int
    columns: list[ColumnEvidence]
    key_candidates: list[KeyCandidate] = field(default_factory=list)
    pairs_probed: int = 0
    pairs_skipped: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def column(self, name: str) -> ColumnEvidence:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def usable_keys(self) -> list[KeyCandidate]:
        return [k for k in self.key_candidates if k.usable]

    def date_columns(self) -> list[ColumnEvidence]:
        """Real date/timestamp columns, earliest-widest first."""
        return sorted(
            (c for c in self.columns if _is_temporal(c.dtype)),
            key=lambda c: -c.distinct,
        )

    def suspected_text_dates(self) -> list[ColumnEvidence]:
        """Text columns whose NAME says date. Loaded as text, so not usable."""
        return [
            c for c in self.columns
            if _is_text(c.dtype) and _DATE_NAME_RE.search(c.name)
        ]

    def to_text(self) -> str:
        """Pipe-delimited, per guide 8.1 Rule 2."""
        out = [
            f"{self.dataset_name}: {self.row_count:,} rows, "
            f"{self.column_count} columns",
            "",
            "| column | type | suggested role | distinct | nulls |",
            "| --- | --- | --- | --- | --- |",
        ]
        for c in self.columns:
            role, _why = suggest_role(c)
            out.append(
                f"| {c.name} | {c.dtype} | {role} | {c.distinct:,} | "
                f"{c.null_count:,} |"
            )

        out.append("")
        if self.key_candidates:
            out.append("Key candidates, best first:")
            out += [f"  - {k.sentence()}" for k in self.key_candidates]
        else:
            out.append(
                "No column or pair identifies a row uniquely. Either the grain "
                "is coarser than one row, or the key is three or more columns "
                "-- which is not searched for, because the number of "
                "combinations grows faster than the evidence is worth."
            )

        dates = self.date_columns()
        if dates:
            out.append("")
            out.append("Date columns:")
            for c in dates:
                out.append(
                    f"  - {c.name} spans {c.min_value} to {c.max_value} "
                    f"({c.distinct:,} distinct values)"
                )
        if self.notes:
            out.append("")
            out.append("Notes:")
            out += [f"  - {n}" for n in self.notes]
        return "\n".join(out)


def suggest_role(col: ColumnEvidence) -> tuple[str, str]:
    """
    A role for one column, and the sentence that justifies it.

    Roles are guide 6.1: identifier, dimension, measure, date, flag, free_text,
    ignore. `dtype` says how a value is stored; `role` says what it is for
    (6.2). Nothing here is authoritative -- an integer column of 1-5 could be a
    rating to average or a bucket to group by, and only a person knows which.
    The suggestion exists so the reader has something to correct.
    """
    if col.row_count and col.non_null == 0:
        return "ignore", f"{col.name} is null in every row."

    if _is_temporal(col.dtype):
        return "date", (
            f"{col.name} is stored as {col.dtype}, spanning {col.min_value} to "
            f"{col.max_value}."
        )

    if _base_type(col.dtype) == "BOOLEAN":
        return "flag", f"{col.name} is BOOLEAN."

    # With one row, every value is unique and constant at once and says nothing about what the
    # column is: the distinctness rules below would call every column "ignore" (P14-O12, B14).
    counted = col.row_count > 1

    if counted and col.is_unique and not _is_numeric(col.dtype):
        return "identifier", (
            f"{col.name} is unique across all {col.row_count:,} rows, so it "
            f"names a row rather than describing one -- unless it is free "
            f"text that happens never to repeat, which counting cannot tell "
            f"apart."
        )

    if _IDENT_NAME_RE.search(col.name):
        if col.is_unique:
            return "identifier", (
                f"{col.name} is unique across all {col.row_count:,} rows and "
                f"is named like an identifier."
            )
        return "identifier", (
            f"{col.name} is named like an identifier and repeats "
            f"({col.distinct:,} distinct across {col.row_count:,} rows), which "
            f"is what a foreign key looks like. Summing it would be "
            f"meaningless whatever its type."
        )

    if counted and col.is_constant:
        return "ignore", (
            f"{col.name} holds a single value in every row, so it cannot "
            f"group or measure anything."
        )

    if _is_numeric(col.dtype):
        # Whole numbers only: a rating or a bucket is 1-5, not 0.0025 or 3.14. A DOUBLE with a
        # handful of values is a measurement that repeats (P14-O12, B14).
        if counted and _is_whole(col.dtype) and col.distinct <= SMALL_NUMERIC_DISTINCT:
            return "dimension", (
                f"{col.name} is numeric but has only {col.distinct:,} distinct "
                f"values, which is a rating or a bucket more often than "
                f"something to add up. Say if it should be a measure, and with "
                f"what aggregation."
            )
        return "measure", (
            f"{col.name} is numeric with {col.distinct:,} distinct values "
            f"spanning {col.min_value} to {col.max_value}."
        )

    if _is_text(col.dtype):
        if counted and col.distinct_ratio >= FREE_TEXT_RATIO:
            return "free_text", (
                f"{col.name} is text with {col.distinct:,} distinct values in "
                f"{col.row_count:,} rows -- too many to group by."
            )
        note = (
            f"{col.name} is text with {col.distinct:,} distinct values, so it "
            f"groups."
        )
        if col.distinct > WIDE_DIMENSION_DISTINCT:
            note += " That is a lot of categories for a chart axis."
        return "dimension", note

    return "dimension", f"{col.name} is {col.dtype}."


def _table_exists(con, dataset_name: str) -> bool:
    return bool(
        con.execute(
            """SELECT 1 FROM information_schema.tables
               WHERE table_schema='main' AND table_name=?""",
            [dataset_name],
        ).fetchone()
    )


def _user_tables(con) -> list[str]:
    """Tables a person loaded. Anything underscore-prefixed is bookkeeping."""
    return [
        r[0]
        for r in con.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='main' ORDER BY table_name"""
        ).fetchall()
        if not r[0].startswith("_")
    ]


def column_stats(con, dataset_name: str, only: str | None = None) -> list[ColumnEvidence]:
    """
    Every column's counts in ONE pass over the table.

    Four aggregates per column in a single SELECT rather than one query per
    column: on 200,000 rows and 14 columns that is 0.31s against 14 separate
    scans. min and max are cast to VARCHAR so a DATE, a DECIMAL and a string
    all come back the same way and nothing here has to know about types twice.
    """
    cols = con.execute(
        """SELECT column_name, data_type, ordinal_position
           FROM information_schema.columns
           WHERE table_schema='main' AND table_name=?
           ORDER BY ordinal_position""",
        [dataset_name],
    ).fetchall()
    # One column's counts for a caller that shows one column: count(DISTINCT) over every other
    # column was most of profile_column's cost on wide, high-cardinality tables (Step 13).
    if only is not None and any(name == only for name, _t, _p in cols):
        cols = [c for c in cols if c[0] == only]
    if not cols:
        raise ContractRefused(
            Refusal(
                reason=Reason.DATASET_NOT_LOADED,
                what=f"'{dataset_name}' has no columns.",
                why="there is nothing to count.",
                # describe_dataset on a name that is not loaded refuses the same way; the list of
                # what is loaded is the call that helps (P14-O18).
                next_call="list_datasets()",
            ).to_text()
        )

    exprs = ["count(*)"]
    for name, dtype, _pos in cols:
        # A float column's range is over its finite values: NaN sorts above everything, so the
        # raw range of a column holding one read "-inf to nan" (P14-D52). The profile's note says
        # how many values are not finite. Floating-point columns only; a filter per column is
        # what made wide profiles slow (C100).
        keep = (f" FILTER (WHERE isfinite({_q(name)}))"
                if _base_type(dtype) in ("DOUBLE", "FLOAT", "REAL") else "")
        exprs += [
            f"count({_q(name)})",
            f"count(DISTINCT {_q(name)})",
            f"(min({_q(name)}){keep})::VARCHAR",
            f"(max({_q(name)}){keep})::VARCHAR",
        ]
    row = con.execute(
        f"SELECT {', '.join(exprs)} FROM {_q(dataset_name)}"
    ).fetchone()

    n = row[0]
    out = []
    for i, (name, dtype, pos) in enumerate(cols):
        non_null, distinct, lo, hi = row[1 + 4 * i: 5 + 4 * i]
        out.append(
            ColumnEvidence(
                name=name,
                dtype=dtype,
                position=pos,
                row_count=n,
                non_null=non_null,
                distinct=distinct,
                min_value=lo,
                max_value=hi,
            )
        )
    return out


def _rank_pairs(pairs, stats):
    """
    Probe the pairs most likely to be a real key first.

    A composite key in practice is a parent identifier plus a small sequence
    within it -- order_id + line_number. So: pairs naming an identifier come
    first, then those whose smaller side is smallest, then source order. The
    ranking only decides what gets probed when the cap bites; it does not
    decide what is true.
    """
    def sort_key(p):
        a, b = p
        named = any(_IDENT_NAME_RE.search(c) for c in p)
        return (0 if named else 1, min(stats[a].distinct, stats[b].distinct))

    return sorted(pairs, key=sort_key)


def find_key_candidates(
    con,
    dataset_name: str,
    stats: list[ColumnEvidence],
    *,
    probe_pairs: bool = True,
    max_pair_probes: int = MAX_PAIR_PROBES,
) -> tuple[list[KeyCandidate], int, int, list[str]]:
    """
    Columns and pairs that identify a row uniquely in this data.

    Returns (candidates, probed, skipped, notes).

    Singles are free -- the counts are already in `stats`. Pairs cost one query
    each, so three things cut the list down before any of them runs:

      minimality  a column that is unique on its own is dropped from pair
                  search. Every pair containing it would be unique, and a key
                  with a redundant column is not a key. Without this, a
                  14-column table returned 22 unique pairs, 13 of them
                  order_id plus noise.
      arithmetic  a pair can only be unique if d_a * d_b >= row_count. On the
                  same table this cut 91 pairs to 25, and 1.85s to 0.89s.
      the cap     what survives is ranked and the first max_pair_probes are
                  probed. Whatever is skipped is COUNTED and reported, never
                  dropped quietly.
    """
    by_name = {c.name: c for c in stats}
    n = stats[0].row_count if stats else 0
    notes: list[str] = []
    # A key is made of things that NAME a row. Measures and free text are not
    # among them, and including them is not merely untidy: on clean_sales.csv,
    # where unit_price holds 498 distinct values in 500 rows, every pair
    # containing it came back unique. Ten of the eleven candidates reported
    # were that -- a near-continuous number stapled to something else.
    key_roles = ("identifier", "dimension", "date", "flag")
    roles = {c.name: suggest_role(c)[0] for c in stats}

    candidates = []
    for c in stats:
        if not c.is_unique:
            continue
        caution = ""
        if roles[c.name] not in key_roles:
            caution = (
                f"{c.name} reads as a {roles[c.name].replace('_', ' ')} rather "
                f"than an identifier, so this is a property of this data, not "
                f"a name for a row."
            )
        candidates.append(
            KeyCandidate(
                columns=(c.name,), distinct=c.distinct, row_count=n,
                caution=caution,
            )
        )

    if n == 0:
        return candidates, 0, 0, ["The table is empty, so nothing can be shown to be unique."]
    if not probe_pairs:
        return candidates, 0, 0, notes

    unique_singles = {k.columns[0] for k in candidates}

    not_key_material = [
        c.name for c in stats
        if roles[c.name] not in key_roles and not c.is_constant
    ]
    considered = [
        c.name for c in stats
        if not c.is_constant and c.name not in not_key_material
    ]
    usable = [c for c in considered if c not in unique_singles]
    dropped_for_minimality = [
        p for p in itertools.combinations(considered, 2)
        if set(p) & unique_singles
    ]
    pairs = [
        p for p in itertools.combinations(usable, 2)
        if by_name[p[0]].distinct * by_name[p[1]].distinct >= n
    ]
    ranked = _rank_pairs(pairs, by_name)
    probing, skipped = ranked[:max_pair_probes], ranked[max_pair_probes:]

    for a, b in probing:
        d = con.execute(
            f"SELECT count(DISTINCT ({_q(a)}, {_q(b)})) FROM {_q(dataset_name)}"
        ).fetchone()[0]
        if d != n:
            continue
        nulls = [c for c in (a, b) if by_name[c].null_count]
        candidates.append(
            KeyCandidate(
                columns=(a, b),
                distinct=d,
                row_count=n,
                null_bearing=nulls,
                caution=(
                    "Two columns can be unique together by coincidence; this "
                    "says the data has no repeat, not that the pair is the "
                    "key."
                ),
            )
        )

    if skipped:
        notes.append(
            f"{len(skipped)} further column pair(s) were not probed: the cap "
            f"is {max_pair_probes} and {len(ranked)} pairs survived pruning. "
            f"Raise max_pair_probes to search all of them."
        )
    if not_key_material:
        notes.append(
            f"{', '.join(not_key_material)} were not paired: their suggested "
            f"role is measure or free text, and a key is made of columns that "
            f"name a row rather than measure one. A near-continuous number is "
            f"unique alongside almost anything, which says nothing."
        )
    if dropped_for_minimality:
        notes.append(
            f"{len(dropped_for_minimality)} pair(s) containing "
            f"{', '.join(sorted(unique_singles))} were not probed. They would "
            f"all be unique, because that column already is -- a key with a "
            f"spare column in it is not a key."
        )
    return candidates, len(probing), len(skipped), notes


def gather(
    con,
    dataset_name: str,
    *,
    probe_pairs: bool = True,
    max_pair_probes: int = MAX_PAIR_PROBES,
) -> DatasetEvidence:
    """
    Everything countable about one loaded table.

    Reads. Writes nothing, stores nothing, proposes nothing. The output is the
    input to a contract proposal, and it is also readable on its own -- which
    is the point of keeping it separate: a person can check the evidence
    without being shown a conclusion first.
    """
    if not _table_exists(con, dataset_name):
        available = ", ".join(_user_tables(con)) or "(none loaded)"
        raise ContractRefused(
            Refusal(
                reason=Reason.DATASET_NOT_LOADED,
                what=f"no dataset called '{dataset_name}' in this workspace.",
                why=(
                    "evidence is gathered from a loaded table, not from a file. "
                    "Nothing is read from disk here."
                ),
                state=f"loaded: {available}",
                next_call=(
                    "list_datasets(), or propose_ingest_spec(path=\"...\") then "
                    "confirm_ingest_spec to load the file first"
                ),
            ).to_text()
        )

    stats = column_stats(con, dataset_name)
    keys, probed, skipped, notes = find_key_candidates(
        con, dataset_name, stats,
        probe_pairs=probe_pairs, max_pair_probes=max_pair_probes,
    )

    ev = DatasetEvidence(
        dataset_name=dataset_name,
        row_count=stats[0].row_count if stats else 0,
        columns=stats,
        key_candidates=keys,
        pairs_probed=probed,
        pairs_skipped=skipped,
        notes=list(notes),
    )

    for c in ev.suspected_text_dates():
        ev.notes.append(
            f"{c.name} is named like a date but is stored as {c.dtype}. It "
            f"will sort as text and cannot be grouped by month. Reload with "
            f"dtypes={{'{c.name}': 'DATE'}} if it should be a date."
        )
    if not ev.date_columns():
        ev.notes.append(
            "No DATE or TIMESTAMP column, so no analysis window can be "
            "proposed and nothing here can be trended over time."
        )
    return ev


__all__ = [
    "ColumnEvidence",
    "DatasetEvidence",
    "KeyCandidate",
    "column_stats",
    "find_key_candidates",
    "gather",
    "suggest_role",
    "MAX_PAIR_PROBES",
]
