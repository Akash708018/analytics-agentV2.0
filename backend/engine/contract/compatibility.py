"""
Is the agreement still true?

A contract is confirmed against a table on a Tuesday. The server outlives the
chat (F13), so on Thursday `sales_2024` may hold different data, a different
file, or a different shape entirely, under the same name. This module answers
what changed and what that means, and it is the only place in the codebase
allowed to decide that an answer is "refuse".

Two questions, deliberately separate.

**Did the structure move?** `classify_drift` compares the binding recorded on
the contract against the table as it is now, and sorts the difference into one
of four classes. The vocabulary is borrowed from schema-registry compatibility
modes, and one of the four names does not fit perfectly -- see below.

**Is the contract still true of the data?** `verify_key` runs the claim rather
than trusting it. A fingerprint says the columns are the same; it says nothing
about whether the primary key still identifies a row. That is the difference
between a stored hash and an executable expectation, and it is the reason the
mature tools (Great Expectations checkpoints, dbt tests) keep the second and
use the first only as a cache key.

The four classes:

    IDENTICAL    same columns, same types, same order, same row count.
                 Nothing to say.
    ADDITIVE     the structure moved and nothing the contract NAMES was
                 harmed. Proceed, mention it.
    NEUTRAL      structure identical, row count different. Proceed, and put a
                 caveat on every result: the definitions were agreed against
                 different data.
    DESTRUCTIVE  a column the contract names is gone, or has changed type.
                 Refuse. This is not a policy choice -- the SQL cannot run,
                 and failing at the gate beats failing three layers into an
                 analysis.

**ADDITIVE is a stretch and the name is kept anyway.** It covers a column
being added, but it also covers an unnamed column being dropped or the columns
being reordered, neither of which is additive in any literal sense. Both are
non-breaking for a contract that refers to columns by name, so they belong in
the same bucket, and inventing a fifth class to be pedantic would mean four
call sites branching on something nobody cares about. The class means "the
structure moved, nothing the contract needs was harmed".

Note that a reorder is non-breaking HERE and breaking one layer down: an
`IngestSpec` carries `names` positionally, so swapping two columns silently
mislabels data. Same event, two correct and opposite answers, because the two
layers refer to columns differently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from backend.engine.contract import ContractRefused
from backend.engine.contract.dataset_contract import Binding, DatasetContract
from backend.engine.contract.refusals import Reason, Refusal


def _q(name: str) -> str:
    """
    Double-quoted SQL identifier.

    The third copy of this in the codebase (csv_loader, evidence, here). Left
    duplicated on purpose: a shared sql_util imported by the ingest layer, the
    evidence layer and the gate would couple three things that otherwise share
    nothing, to save six lines.
    """
    return '"' + name.replace('"', '""') + '"'


class Drift(str, Enum):
    IDENTICAL = "IDENTICAL"
    ADDITIVE = "ADDITIVE"
    NEUTRAL = "NEUTRAL"
    DESTRUCTIVE = "DESTRUCTIVE"


@dataclass
class DriftVerdict:
    """What moved between a contract's binding and the table now."""

    drift: Drift
    dataset_name: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    retyped: list[tuple[str, str, str]] = field(default_factory=list)
    reordered: bool = False
    row_count_was: int = 0
    row_count_now: int = 0
    breaking: list[str] = field(default_factory=list)

    @property
    def blocks(self) -> bool:
        return self.drift is Drift.DESTRUCTIVE

    @property
    def rows_changed(self) -> int:
        return self.row_count_now - self.row_count_was

    def _rows_phrase(self) -> str:
        """
        The row-count half of a caveat, or '' when nothing moved.

        Kept separate because a structural change and a volume change can
        happen at once. The first version of this returned only the
        structural sentence in that case, which quietly dropped the fact that
        the table had also gained thirty rows -- found by walking a table
        through all four classes rather than by a test, because every test
        changed one thing at a time.
        """
        if self.row_count_was == self.row_count_now:
            return ""
        direction = "gained" if self.rows_changed > 0 else "lost"
        return (
            f"It has also {direction} {abs(self.rows_changed):,} rows since "
            f"then ({self.row_count_was:,} -> {self.row_count_now:,})."
        )

    def caveat(self) -> str:
        """
        The sentence attached to every result computed under this verdict.

        Empty when there is nothing to say. Anything else has to be readable
        at the bottom of a table of numbers, so it stays short and leads with
        the fact rather than with the classification.
        """
        if self.drift is Drift.IDENTICAL:
            return ""
        if self.drift is Drift.NEUTRAL:
            direction = "gained" if self.rows_changed > 0 else "lost"
            return (
                f"'{self.dataset_name}' has {direction} "
                f"{abs(self.rows_changed):,} rows since this contract was "
                f"confirmed against {self.row_count_was:,}. The definitions "
                f"still apply; the numbers were agreed against different data."
            )
        if self.drift is Drift.ADDITIVE:
            bits = []
            if self.added:
                bits.append(f"new column(s) {', '.join(self.added)}")
            if self.removed:
                bits.append(f"dropped column(s) {', '.join(self.removed)}")
            if self.reordered:
                bits.append("columns in a different order")
            change = "; ".join(bits) or "a structural change"
            rows = self._rows_phrase()
            return (
                f"'{self.dataset_name}' has changed since this contract was "
                f"confirmed -- {change}. Nothing the contract names was "
                f"affected, so it still applies as written."
                + (f" {rows}" if rows else "")
            )
        return (
            f"'{self.dataset_name}' no longer has {', '.join(self.breaking)}, "
            f"which this contract names."
        )

    def refusal(self) -> Refusal | None:
        """The refusal, or None when the verdict does not block."""
        if not self.blocks:
            return None
        detail = []
        if self.removed:
            detail.append(f"gone: {', '.join(self.removed)}")
        for name, was, now in self.retyped:
            detail.append(f"{name} was {was}, is now {now}")
        return Refusal(
            reason=Reason.CONTRACT_STALE,
            what=(
                f"the contract for '{self.dataset_name}' names column(s) the "
                f"table no longer has: {', '.join(self.breaking)}."
            ),
            why=(
                "the table was reloaded or replaced under the same name. Every "
                "measure, key and dimension in a contract is a column "
                "reference, so this one cannot be executed -- not as a "
                "degraded result, at all."
            ),
            detail="; ".join([
                *detail,
                "drafting one against the table as it is now is what to confirm; the "
                "superseded contract stays in the log",
            ]),
            state=(
                f"contract confirmed against {self.row_count_was:,} rows; "
                f"table now has {self.row_count_now:,}"
            ),
            next_call=f'propose_dataset_contract(dataset_name="{self.dataset_name}")',
        )

    def to_text(self) -> str:
        lines = [f"{self.dataset_name}: {self.drift.value}"]
        if self.added:
            lines.append(f"  added     {', '.join(self.added)}")
        if self.removed:
            lines.append(f"  removed   {', '.join(self.removed)}")
        for name, was, now in self.retyped:
            lines.append(f"  retyped   {name}: {was} -> {now}")
        if self.reordered:
            lines.append("  reordered column order differs")
        if self.row_count_was != self.row_count_now:
            lines.append(
                f"  rows      {self.row_count_was:,} -> {self.row_count_now:,}"
            )
        if self.breaking:
            lines.append(f"  BREAKING  {', '.join(self.breaking)}")
        caveat = self.caveat()
        if caveat:
            lines += ["", caveat]
        return "\n".join(lines)


def binding_for(
    con, dataset_name: str, loaded_at: datetime | None = None
) -> Binding:
    """
    The table as it stands now, in the shape a contract binds to.

    `loaded_at` is passed in rather than read from the dataset registry, so
    this stays usable on a table the loaders did not create -- a temp table in
    a test, a view. The registry lookup belongs to whoever is storing the
    contract, not to the thing describing a table.
    """
    rows = con.execute(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_schema='main' AND table_name=?
           ORDER BY ordinal_position""",
        [dataset_name],
    ).fetchall()
    if not rows:
        raise ContractRefused(
            Refusal(
                reason=Reason.DATASET_NOT_LOADED,
                what=f"there is no table called '{dataset_name}' in this workspace.",
                why=(
                    "a contract binds to a loaded table, and nothing here reads "
                    "from disk."
                ),
                detail="list_datasets() shows what is loaded.",
                next_call="list_datasets()",
            ).to_text()
        )
    count = con.execute(f"SELECT count(*) FROM {_q(dataset_name)}").fetchone()[0]
    return Binding.from_pairs([(n, t) for n, t in rows], count, loaded_at)


def classify_drift(
    contract: DatasetContract, observed: Binding
) -> DriftVerdict:
    """
    Sort the difference between a contract's binding and the table now.

    Breaking is judged against the columns the CONTRACT NAMES, not against the
    table in general. A warehouse that drops a column nobody put in a contract
    has not invalidated the contract, and refusing there would train whoever
    reads the refusal to ignore it -- which is the real cost of a gate that
    fires when it does not have to.
    """
    if contract.bound_to is None:
        raise ContractRefused(
            Refusal(
                reason=Reason.CONTRACT_INVALID,
                what=(
                    f"the contract for '{contract.dataset_name}' has no "
                    f"binding, so there is nothing to compare the table with."
                ),
                why=(
                    "a binding is written when a contract is confirmed. This "
                    "one was drafted or read back without one."
                ),
                detail="Draft one and confirm it.",
                next_call=(
                    f'propose_dataset_contract(dataset_name='
                    f'"{contract.dataset_name}")'
                ),
            ).to_text()
        )

    was = contract.bound_to
    was_types = was.dtypes
    now_types = observed.dtypes

    added = [c for c in observed.column_names if c not in was_types]
    removed = [c for c in was.column_names if c not in now_types]
    retyped = [
        (c, was_types[c], now_types[c])
        for c in was.column_names
        if c in now_types and was_types[c].upper() != now_types[c].upper()
    ]
    surviving_was = [c for c in was.column_names if c in now_types]
    surviving_now = [c for c in observed.column_names if c in was_types]
    reordered = surviving_was != surviving_now

    used = set(contract.columns_used())
    breaking = sorted(
        (set(removed) | {c for c, _w, _n in retyped}) & used
    )

    if breaking:
        drift = Drift.DESTRUCTIVE
    elif added or removed or retyped or reordered:
        drift = Drift.ADDITIVE
    elif was.row_count != observed.row_count:
        drift = Drift.NEUTRAL
    else:
        drift = Drift.IDENTICAL

    return DriftVerdict(
        drift=drift,
        dataset_name=contract.dataset_name,
        added=added,
        removed=removed,
        retyped=retyped,
        reordered=reordered,
        row_count_was=was.row_count,
        row_count_now=observed.row_count,
        breaking=breaking,
    )


@dataclass
class KeyVerdict:
    """Whether a stated key actually identifies a row, right now."""

    dataset_name: str
    columns: list[str]
    row_count: int
    distinct: int
    null_counts: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    # Rows that repeat another row in every column. Counted only when the key repeats: when they
    # are the whole repeat, dropping them (propose_cleaning_plan's C001) is the fix, and the
    # refusal names that call instead of a template (Step 13 benchmark).
    exact_duplicates: int = 0

    @property
    def label(self) -> str:
        return " + ".join(self.columns)

    @property
    def keyed_rows(self) -> int:
        """Rows the distinct count could possibly have counted.

        P7-D6. `count(DISTINCT (a, b))` counts a tuple containing NULL;
        `count(DISTINCT x)` drops it. So `distinct` and `row_count` describe
        the same population for a composite key and different populations for
        a single-column one, and subtracting the second from the first
        reported every null key as a duplicate as well as a null. The verdict
        was unaffected -- `holds` refuses a null-bearing key either way -- but
        the DETAIL line of a KEY_NOT_UNIQUE refusal named a repeat that did
        not exist, and that line is what an agent reads before deciding what
        to do next.
        """
        if len(self.columns) == 1:
            return self.row_count - self.null_counts.get(self.columns[0], 0)
        return self.row_count

    @property
    def is_unique(self) -> bool:
        return bool(self.columns) and self.keyed_rows > 0 and self.distinct == self.keyed_rows

    @property
    def null_bearing(self) -> list[str]:
        return sorted(c for c, n in self.null_counts.items() if n)

    @property
    def holds(self) -> bool:
        return self.is_unique and not self.null_bearing and not self.missing

    @property
    def duplicate_rows(self) -> int:
        return max(0, self.keyed_rows - self.distinct)

    def sentence(self) -> str:
        if self.missing:
            return (
                f"{', '.join(self.missing)} are not columns of "
                f"{self.dataset_name}."
            )
        if self.holds:
            return (
                f"{self.label} is unique across {self.distinct:,} of "
                f"{self.row_count:,} rows, with no nulls."
            )
        if not self.row_count:
            return (
                f"{self.dataset_name} has no rows, so {self.label} identifies "
                f"nothing."
            )
        parts = []
        if self.duplicate_rows:
            parts.append(
                f"{self.distinct:,} distinct value(s) across "
                f"{self.keyed_rows:,} keyed row(s), so {self.duplicate_rows:,} "
                f"row(s) repeat a key that is meant to be unique"
            )
        for c in self.null_bearing:
            parts.append(f"{c} is null in {self.null_counts[c]:,} row(s)")
        return f"{self.label} does not identify a row: " + "; ".join(parts) + "."

    def refusal(self) -> Refusal | None:
        if self.holds:
            return None
        reason = (
            Reason.CONTRACT_STALE if self.missing else Reason.KEY_NOT_UNIQUE
        )
        return Refusal(
            reason=reason,
            what=(
                f"the primary key stated for '{self.dataset_name}' "
                f"({self.label}) does not identify a row."
            ),
            why=(
                "the grain of every later number depends on this. If the key "
                "repeats, a join or a sum over it double-counts, and nothing "
                "downstream can tell that it did."
            ),
            detail=(
                f"{self.sentence()} "
                f'validate_dataset(dataset_name="{self.dataset_name}") reports '
                f"this and everything else that disagrees with the contract, "
                f"rather than stopping at the key."
            ),
            next_call=self._next_call(),
        )

    def _next_call(self) -> str:
        if (
            self.duplicate_rows
            and not self.null_bearing
            and self.exact_duplicates >= self.duplicate_rows
        ):
            return (
                f'propose_cleaning_plan(dataset_name="{self.dataset_name}") -- '
                f"{self.exact_duplicates:,} row(s) repeat another row in every column, "
                f"which is every repeat of {self.label}; dropping them makes the key hold"
            )
        return (
            f'propose_dataset_contract(dataset_name="{self.dataset_name}", '
            f"primary_key=[...]) with a key that holds, or state the grain "
            f"that matches the key you gave"
        )


def verify_key(con, dataset_name: str, columns: list[str]) -> KeyVerdict:
    """
    Check a key a person STATED, rather than searching for one.

    This is the other half of `evidence.find_key_candidates`, and the half
    that matters more. Search is bounded by heuristics that are wrong at the
    edges: `order_item_id` holds 21 distinct values, past the twelve at which
    a numeric column stops being offered as a dimension, and it survived pair
    search on olist only because its name ends in `_id`. Rename that column
    `line` in another warehouse and search finds nothing.

    Nothing about verification is heuristic. You name the columns, this runs
    the count, and the answer is arithmetic. It follows the Phase 3 rule that
    an answer comes back the way the question went out -- through a parameter,
    not by editing JSON -- and it is why the search being imperfect is
    acceptable rather than blocking.

    A composite is counted with count(DISTINCT (a, b)), which does NOT drop
    nulls the way the single-column form does, so null counts are gathered
    separately. Unique and usable are different questions.
    """
    if not columns:
        raise ValueError("verify_key needs at least one column.")

    known = {
        r[0]
        for r in con.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='main' AND table_name=?""",
            [dataset_name],
        ).fetchall()
    }
    if not known:
        raise ContractRefused(
            Refusal(
                reason=Reason.DATASET_NOT_LOADED,
                what=f"there is no table called '{dataset_name}' in this workspace.",
                why="a key can only be verified against a loaded table.",
                detail="list_datasets() shows what is loaded.",
                next_call="list_datasets()",
            ).to_text()
        )

    missing = [c for c in columns if c not in known]
    if missing:
        return KeyVerdict(
            dataset_name=dataset_name,
            columns=list(columns),
            row_count=0,
            distinct=0,
            missing=missing,
        )

    quoted = ", ".join(_q(c) for c in columns)
    distinct_expr = (
        f"count(DISTINCT {_q(columns[0])})"
        if len(columns) == 1
        else f"count(DISTINCT ({quoted}))"
    )
    null_exprs = ", ".join(
        f"count(*) FILTER (WHERE {_q(c)} IS NULL)" for c in columns
    )
    row = con.execute(
        f"SELECT count(*), {distinct_expr}, {null_exprs} "
        f"FROM {_q(dataset_name)}"
    ).fetchone()
    verdict = KeyVerdict(
        dataset_name=dataset_name,
        columns=list(columns),
        row_count=row[0],
        distinct=row[1],
        null_counts={c: row[2 + i] for i, c in enumerate(columns)},
    )
    if verdict.duplicate_rows:
        verdict.exact_duplicates = row[0] - con.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT * FROM {_q(dataset_name)})"
        ).fetchone()[0]
    return verdict


def exact_copies(con, dataset_name: str) -> int:
    """How many rows of a table are exact copies of another row -- the rows a DISTINCT would drop.

    The one double-counting question left when a contract states no key: with nothing to verify,
    four rows copied whole went through the gate of the bunty_babli run as 604 of 604 analysed.

    Screened by a hash of every column, then counted exactly only when the screen finds
    something. Measured on 5M rows x 8 columns (Cleanup Step 8): SELECT DISTINCT * 0.213s, the
    hash screen 0.119s -- the range Phase 7 accepted for verify_key (0.077-0.179s). A collision
    can only make the screen report copies that are not there, never hide one, and the exact
    count settles it. DISTINCT and hash() both treat two NULLs as equal
    (tests/test_duplicate_facts.py), so rows that match through a NULL are copies.
    """
    columns = [
        r[0] for r in con.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position""",
            [dataset_name],
        ).fetchall()
    ]
    if not columns:
        return 0
    table = _q(dataset_name)
    hashed = ", ".join(_q(c) for c in columns)
    screened = con.execute(
        f"SELECT count(*) - count(DISTINCT hash({hashed})) FROM {table}"
    ).fetchone()[0]
    if not screened:
        return 0
    return con.execute(
        f"SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM {table})) FROM {table}"
    ).fetchone()[0]


__all__ = [
    "Drift",
    "DriftVerdict",
    "KeyVerdict",
    "binding_for",
    "classify_drift",
    "exact_copies",
    "verify_key",
]
