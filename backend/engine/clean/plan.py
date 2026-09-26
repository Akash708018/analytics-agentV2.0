"""What was proposed, when, and against which table.

Phase 6, Step 3. The layer `apply_cleaning_plan(approved_action_ids=[...])`
resolves against, and the reason that signature is safe.

**Why a plan is stored at all.** The tool takes action ids AND NOTHING ELSE, so
`C003` has to mean something after the proposal has scrolled off. Storing the
plan is what makes an id a reference rather than a coincidence.

**And storing it is what creates the staleness problem.** A plan describes a
table as it was when the plan was made. Between proposing and approving, the
table can be reloaded -- `load_excel(replace=True)` is the default and
overwrites in silence -- and then `C003` still resolves, still renders, and now
converts a column that is not the column it was written for. That is F13, third
appearance in this project: state that outlives the thing it describes. Phase 4
answered it for contracts with a fingerprint, Phase 5's live run found the same
hole in the profile run record, and this module carries the fingerprint from the
start rather than discovering it later.

**Per-plan action ids, not global ones.** `C001` is meant to be read off a
proposal and typed back, which rules out a uuid. It also means the id is
ambiguous across plans, so resolution always goes through the LATEST plan for a
dataset and refuses on staleness rather than silently picking one.

**One flat table, denormalised on purpose.** `_agent_cleaning_plans` carries the
plan-level columns on every action row. A plan is always read whole and never
updated, so a join buys normalisation nobody spends. `profile/runs.py` made the
same call for the same reason.

**What is NOT here.** Computing the fingerprint. `contract/binding.py` owns
that and this module has not read it, so `record()` takes a fingerprint as a
parameter rather than deriving one. Also no SQL: `clean/sql.py` renders it and
`CleaningAction` only carries the string, because a model that can build SQL is
a model that can build DIFFERENT SQL from the one that gets shown.
"""

from __future__ import annotations

from backend.engine.util import db

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..profile.runs import BOOKKEEPING_PREFIX

# Built from the shared prefix rather than spelled out, which is what
# runs.py's note about "a filter that lists three names" was asking for.
PLAN_TABLE = f"{BOOKKEEPING_PREFIX}cleaning_plans"


class ActionKind(str, Enum):
    """What a cleaning action does.

    String values so a stored plan stays readable and a renamed member breaks
    loudly, the same reasoning `Reason` records.

    This list covers what the fixtures can prove. Step 5's detection adds
    members as rules arrive, and an action kind with no fixture behind it is a
    rule nobody has watched run.
    """

    CONVERT_TYPE = "CONVERT_TYPE"
    NORMALISE_MISSING = "NORMALISE_MISSING"
    TRIM_WHITESPACE = "TRIM_WHITESPACE"
    NORMALISE_CASE = "NORMALISE_CASE"
    DROP_DUPLICATE_ROWS = "DROP_DUPLICATE_ROWS"
    EXCLUDE_COLUMN = "EXCLUDE_COLUMN"
    # Phase 14 Step 7: found by the stress matrix (P14-O6, P14-O11).
    NULL_NON_FINITE = "NULL_NON_FINITE"
    DROP_HEADER_ROWS = "DROP_HEADER_ROWS"


@dataclass(frozen=True)
class CleaningAction:
    """One proposed change, with the numbers that justify it.

    `values_lost` is the field this phase turns on. Step 2 measured two
    conversions identical in shape and different in kind: `units` holds 'n/a',
    which is a declared missing token, so converting it to NULL makes an
    absence explicit and loses nothing. `unit_price` holds 'not priced', which
    is not declared -- it says a price was withheld rather than merely absent,
    and converting it destroys the only record of the difference.

    So `values_lost` counts what an action destroys that was not already
    declared absent, and it is not allowed to be non-zero without a sample.
    Checked here rather than remembered, because a rule enforced by a docstring
    is a rule until somebody is in a hurry.

    `loss_unit` names WHAT is destroyed, because "becomes NULL" is only true
    for a type conversion. Case folding nulls nothing and still loses
    something: 'North' and 'north' merge and the record that the source wrote
    them differently is gone, so its unit is "distinct value". Dropping exact
    duplicates destroys multiplicity, so its unit is "row". P6-D10, found by
    writing clean/sql.py and fixed here rather than left as wording that is
    right a quarter of the time.
    """

    action_id: str
    kind: ActionKind
    intent: str
    sql: str
    column: str | None = None
    rows_affected: int = 0
    values_lost: int = 0
    loss_unit: str = "value"
    sample: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.values_lost < 0 or self.rows_affected < 0:
            raise ValueError("counts cannot be negative")
        if self.values_lost and not self.sample:
            raise ValueError(
                f"{self.action_id} would discard {self.values_lost} undeclared "
                f"value(s) and carries no sample. An action that cannot show "
                f"what it destroys is not a proposal."
            )
        if not self.sql.strip():
            raise ValueError(f"{self.action_id} carries no SQL")

    @property
    def is_lossy(self) -> bool:
        return self.values_lost > 0

    def line(self) -> str:
        """One line for the proposal, loss stated where there is any."""
        where = f" on {self.column}" if self.column else ""
        head = f"{self.action_id}  {self.kind.value}{where}: {self.intent}"
        if not self.is_lossy:
            return f"{head} ({self.rows_affected:,} row(s))"
        shown = ", ".join(repr(s) for s in self.sample[:3]) or "no sample"
        return (
            f"{head} ({self.rows_affected:,} row(s)). "
            f"{self.values_lost:,} {self.loss_unit}(s) will be discarded and "
            f"are not declared missing anywhere: {shown}. That is information, "
            f"not absence -- approve this only if you mean to lose it."
        )


@dataclass(frozen=True)
class CleaningPlan:
    """Everything proposed for one dataset in one pass, and what it was
    proposed against."""

    plan_id: str
    dataset_name: str
    proposed_at: datetime
    row_count: int
    fingerprint: str
    actions: tuple[CleaningAction, ...] = field(default_factory=tuple)

    def action(self, action_id: str) -> CleaningAction | None:
        for a in self.actions:
            if a.action_id == action_id:
                return a
        return None

    def resolve(
        self, action_ids: list[str]
    ) -> tuple[list[CleaningAction], list[str]]:
        """Split requested ids into the ones this plan has and the ones it does
        not. Both halves are returned: a caller that only gets the hits cannot
        tell the user which id it ignored."""
        found, unknown = [], []
        for wanted in action_ids:
            hit = self.action(wanted)
            (found if hit else unknown).append(hit or wanted)
        return found, unknown

    def staleness(
        self, current_rows: int, current_fingerprint: str
    ) -> str | None:
        """Why this plan should not be applied, or None.

        The fingerprint is checked FIRST and reads harder than a row count,
        because they are different events. Rows changing means the plan
        describes less or more of the same table. The fingerprint changing
        means it describes a DIFFERENT table wearing the same name -- which is
        what a silent reload produces, and no row count catches it when the
        replacement happens to be the same size.
        """
        if current_fingerprint != self.fingerprint:
            return (
                f"the table's structure has changed since this plan was made "
                f"({self.fingerprint} then, {current_fingerprint} now). These "
                f"actions were written for different columns. Re-run "
                f"propose_cleaning_plan(dataset_name=\"{self.dataset_name}\")"
            )
        if current_rows != self.row_count:
            direction = "gained" if current_rows > self.row_count else "lost"
            delta = abs(current_rows - self.row_count)
            return (
                f"the table has {direction} {delta:,} row(s) since this plan "
                f"was made ({self.row_count:,} then, {current_rows:,} now), so "
                f"the counts below are old. Re-run "
                f"propose_cleaning_plan(dataset_name=\"{self.dataset_name}\")"
            )
        return None

    def age_phrase(self, now: datetime | None = None) -> str:
        stamp = self.proposed_at.strftime("%Y-%m-%d %H:%M")
        seconds = max(0, int(((now or datetime.now()) - self.proposed_at)
                             .total_seconds()))
        if seconds < 90:
            return f"proposed just now ({stamp})"
        minutes = seconds // 60
        if minutes < 90:
            return f"proposed {minutes} minutes ago ({stamp})"
        hours = minutes // 60
        if hours < 36:
            return f"proposed {hours} hours ago ({stamp})"
        return f"proposed {hours // 24} days ago ({stamp})"


def next_action_id(taken: int) -> str:
    """C001, C002, ... Short enough to read off a screen and type back."""
    return f"C{taken + 1:03d}"


def ensure_table(con) -> None:
    """Create the plan log if it is missing.

    Lazily, for the reason contract/store.py and profile/runs.py both record.
    Note that this cannot run on the read-only connection the proposal path
    uses -- a read-only ATTACH refuses CREATE by statement type, IF NOT EXISTS
    and all. So the writable connection creates it before the proposal is
    stored, never during the reading half.
    """
    db.create_if_missing(
        con,
        f"""
        CREATE TABLE IF NOT EXISTS {PLAN_TABLE} (
            plan_id        VARCHAR NOT NULL,
            dataset_name   VARCHAR NOT NULL,
            proposed_at    TIMESTAMP NOT NULL,
            row_count      BIGINT NOT NULL,
            fingerprint    VARCHAR NOT NULL,
            action_id      VARCHAR NOT NULL,
            kind           VARCHAR NOT NULL,
            column_name    VARCHAR,
            intent         VARCHAR NOT NULL,
            loss_unit      VARCHAR NOT NULL,
            sql_text       VARCHAR NOT NULL,
            rows_affected  BIGINT NOT NULL,
            values_lost    BIGINT NOT NULL,
            sample         VARCHAR
        )
        """
    )


_SAMPLE_SEP = "\x1f"  # unit separator; will not occur in a data value


def record(
    con,
    *,
    dataset_name: str,
    row_count: int,
    fingerprint: str,
    actions: list[CleaningAction],
    now: datetime | None = None,
) -> CleaningPlan:
    """Append one plan. Never updates, never deletes.

    A plan supersedes nothing -- proposing twice in an hour is two observations
    of a table, the same argument runs.py makes for profiles. `latest()` picks
    the newest and the older ones stay readable.
    """
    ensure_table(con)
    plan = CleaningPlan(
        plan_id=uuid.uuid4().hex[:12],
        dataset_name=dataset_name,
        proposed_at=now or datetime.now(),
        row_count=row_count,
        fingerprint=fingerprint,
        actions=tuple(actions),
    )
    for a in plan.actions:
        con.execute(
            f"INSERT INTO {PLAN_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                plan.plan_id, plan.dataset_name, plan.proposed_at,
                plan.row_count, plan.fingerprint, a.action_id, a.kind.value,
                a.column, a.intent, a.loss_unit, a.sql, a.rows_affected,
                a.values_lost,
                _SAMPLE_SEP.join(a.sample) if a.sample else None,
            ],
        )
    return plan


def _rows_to_plan(rows) -> CleaningPlan:
    head = rows[0]
    return CleaningPlan(
        plan_id=head[0],
        dataset_name=head[1],
        proposed_at=head[2],
        row_count=head[3],
        fingerprint=head[4],
        actions=tuple(
            CleaningAction(
                action_id=r[5],
                kind=ActionKind(r[6]),
                column=r[7],
                intent=r[8],
                loss_unit=r[9],
                sql=r[10],
                rows_affected=r[11],
                values_lost=r[12],
                sample=tuple(r[13].split(_SAMPLE_SEP)) if r[13] else (),
            )
            for r in rows
        ),
    )


def latest(con, dataset_name: str) -> CleaningPlan | None:
    """The most recent plan for one dataset, or None if none was proposed."""
    ensure_table(con)
    rows = con.execute(
        f"SELECT * FROM {PLAN_TABLE} WHERE plan_id = ("
        f"  SELECT plan_id FROM {PLAN_TABLE} WHERE dataset_name = ? "
        f"  ORDER BY proposed_at DESC LIMIT 1) ORDER BY action_id",
        [dataset_name],
    ).fetchall()
    return _rows_to_plan(rows) if rows else None


def history(con, dataset_name: str) -> list[CleaningPlan]:
    """Every plan for one dataset, newest first."""
    ensure_table(con)
    rows = con.execute(
        f"SELECT * FROM {PLAN_TABLE} WHERE dataset_name = ? "
        f"ORDER BY proposed_at DESC, action_id",
        [dataset_name],
    ).fetchall()
    by_plan: dict[str, list] = {}
    for r in rows:
        by_plan.setdefault(r[0], []).append(r)
    return [_rows_to_plan(v) for v in by_plan.values()]


__all__ = [
    "PLAN_TABLE",
    "ActionKind",
    "CleaningAction",
    "CleaningPlan",
    "ensure_table",
    "history",
    "latest",
    "next_action_id",
    "record",
]
