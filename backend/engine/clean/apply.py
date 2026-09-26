"""Run the approved actions, all of them or none.

Phase 6, Step 6. The writing half, and the two decisions this phase deferred to
the layer that actually names and writes tables.

**P6-D1 — clean forward under the same name, snapshot behind the prefix.**
Locked decision 17 asks for versioned tables and locked decision 13 partitions
all state by `dataset_name`. Both are satisfied by writing the clean result to
`sales` and keeping the previous contents as `_agent_history_sales_v1`:

  - one row in `_agent_datasets`, one entry in every listing, because
    `db.user_tables` filters `table_name NOT LIKE '\\_%'` in SQL and
    `state._loadable_tables` filters the leading underscore again;
  - the contract and the profile stay bound to `sales`, which is the name they
    were written against;
  - both tables are on disk, so "here is the table before and the table after"
    is evidence rather than a ledger line describing one.

The cost, recorded rather than glossed: those history tables are invisible to
every tool that goes through `user_tables`, and `run_sql` does not exist until
Phase 8. A before-table nobody can open is not evidence, so `history_tables()`
and `read_history()` below are part of this module rather than an afterthought.

**P6-D11 — a conversion claims its column.** Detection offers
`CONVERT_TYPE on units` and `NORMALISE_MISSING on units` because each rule fires
on its own terms, and approving both fails: the second meets a BIGINT column and
DuckDB says `No function matches the given name and argument types
'trim(BIGINT)'`. So `conflicts()` refuses the combination.

**It refuses by offering an order, not by telling you to drop one.** The first
version said "approve one", and Step 7's live run showed why that is worse
advice than it looks: approving only the conversion still disposes of the seven
declared tokens, but the ledger records them as casualties of a cast rather than
as a normalisation somebody named. Two calls -- normalise, re-propose, convert
-- end with the same table and a record of which step did what. An agent worked
that out unprompted and the refusal now says it.

The alternative to refusing at all -- re-rendering each action against the table
as it stands -- stays out, because the SQL shown would stop being the SQL run.

**All or nothing.** DuckDB's DDL is transactional, measured: a
`CREATE OR REPLACE TABLE` inside a transaction rolls back cleanly, and a table
created inside one disappears. So a failing action leaves the dataset exactly as
it was rather than half-cleaned. `conflicts()` is still the better guard --
being told which id to drop beats a rollback that says nothing -- but the
rollback is what makes the failure survivable when the guard misses.

**The ledger is written inside the same transaction.** A ledger written after
the commit can still fail after it, leaving a clean nobody recorded; written
before a rollback it records a clean that never happened. A row in the same
transaction cannot disagree with the tables it describes. That is why
`clean/ledger.py` is a table and not the JSONL file the build guide asks for,
and why `apply` takes a `plan_id` -- a ledger entry that cannot name the plan
its action came from is a change with no provenance.

**`_agent_datasets` is deliberately not rewritten.** `register_dataset` REPLACES
the row and resets `loaded_at`, which would make a cleaned dataset look freshly
loaded and erase where it came from. Row counts in that table go stale for a
cleaned dataset; `state.dataset_states` reads shape from `db.table_shape` live,
so the listing stays correct. Recorded because it is a choice, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass

from datetime import datetime

from ..profile.runs import BOOKKEEPING_PREFIX
from . import ledger
from .plan import ActionKind, CleaningAction
from .sql import ident

HISTORY_PREFIX = f"{BOOKKEEPING_PREFIX}history_"


def history_table(dataset_name: str, version: int) -> str:
    return f"{HISTORY_PREFIX}{dataset_name}_v{version}"


def history_tables(con, dataset_name: str) -> list[str]:
    """Every snapshot kept for one dataset, oldest first.

    Public because nothing else can see them: `db.user_tables` excludes
    anything starting with an underscore, in SQL.
    """
    like = f"{HISTORY_PREFIX}{dataset_name}_v%"
    return [
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name LIKE ? "
            "ORDER BY length(table_name), table_name",
            [like],
        ).fetchall()
    ]


def next_version(con, dataset_name: str) -> int:
    existing = history_tables(con, dataset_name)
    return len(existing) + 1


def read_history(con, table: str, limit: int = 20) -> list[tuple]:
    """Open a snapshot. The reader that makes the snapshot evidence.

    Deliberately narrow: it takes a table name that `history_tables` returned
    and reads rows. Anything richer belongs to Phase 8's `run_sql`, which does
    not exist yet and, when it does, will very likely reuse `db.user_tables`
    for its own guard -- and exclude these tables by accident unless somebody
    remembers this note.
    """
    if not table.startswith(HISTORY_PREFIX):
        raise ValueError(f"{table!r} is not a cleaning snapshot")
    return con.execute(f"SELECT * FROM {ident(table)} LIMIT {int(limit)}").fetchall()


def conflicts(actions: list[CleaningAction]) -> list[str]:
    """Reasons this set of actions cannot be applied together.

    One rule, and it is P6-D11: a `CONVERT_TYPE` claims its column. Every other
    action on that column was rendered against text and will meet whatever the
    conversion produced.

    Two text actions on one column are fine -- trimming then case-folding is
    two text functions on a VARCHAR, in that order, and both survive.
    """
    converted = {
        a.column for a in actions
        if a.kind is ActionKind.CONVERT_TYPE and a.column
    }
    out = []
    # Two readings of one column (a thousands comma and a decimal comma, P14-D50): one of them.
    seen: dict[str, str] = {}
    for a in actions:
        if a.kind is ActionKind.CONVERT_TYPE and a.column:
            if a.column in seen:
                out.append(
                    f"{seen[a.column]} and {a.action_id} are two readings of {a.column}. "
                    f"Approve the one that matches the file's convention, not both.")
            else:
                seen[a.column] = a.action_id
    for a in actions:
        if a.kind is ActionKind.CONVERT_TYPE or not a.column:
            continue
        if a.column in converted:
            owner = next(
                x for x in actions
                if x.kind is ActionKind.CONVERT_TYPE and x.column == a.column
            )
            out.append(
                f"{a.action_id} and {owner.action_id} both change "
                f"{a.column}. {owner.action_id} reads it as a different type, "
                f"and {a.action_id} was written against text, so it would fail "
                f"once {owner.action_id} has run. Approve {a.action_id} on its "
                f"own first, then propose again and approve the conversion. "
                f"Both orders end with the same table; only that one records "
                f"which step disposed of the values, instead of leaving them "
                f"as casualties of a cast."
            )
    return out


def first_step(actions: list[CleaningAction]) -> str:
    """The id the conflict message tells you to approve first.

    `conflicts()` says "approve C004 on its own first, then propose again". A
    refusal whose NEXT STEP then names C003 contradicts its own WHY two lines
    above, which is how the first version behaved: it used the first approved
    id in list order and that happened to be the conversion.

    So the recommended id is computed the same way the message is: the action
    that is NOT the type change on the contested column.
    """
    converted = {
        a.column for a in actions
        if a.kind is ActionKind.CONVERT_TYPE and a.column
    }
    for a in actions:
        if a.kind is not ActionKind.CONVERT_TYPE and a.column in converted:
            return a.action_id
    return actions[0].action_id


@dataclass(frozen=True)
class AppliedAction:
    action_id: str
    kind: ActionKind
    column: str | None
    rows_before: int
    rows_after: int
    statement: str

    @property
    def rows_removed(self) -> int:
        return self.rows_before - self.rows_after


@dataclass(frozen=True)
class ApplyResult:
    dataset_name: str
    history_table: str
    rows_before: int
    rows_after: int
    applied: tuple[AppliedAction, ...]

    def line(self) -> str:
        return (
            f"{len(self.applied)} action(s) applied to {self.dataset_name}. "
            f"{self.rows_before:,} row(s) before, {self.rows_after:,} after. "
            f"The previous contents are kept as {self.history_table}."
        )


def _rows(con, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {ident(table)}").fetchone()[0]


def apply(
    con,
    *,
    dataset_name: str,
    actions: list[CleaningAction],
    plan_id: str = "(none)",
    now: datetime | None = None,
) -> ApplyResult:
    """Snapshot, then run every approved action in order, in one transaction.

    Raises rather than returning a refusal object: the tool layer owns the
    wording of refusals and this module owns whether they happen.
    """
    if not actions:
        raise ValueError("nothing was approved")

    problems = conflicts(actions)
    if problems:
        raise ValueError(" ".join(problems))

    # Every statement must write to the dataset it was proposed for. A stored
    # plan is data, and data that names its own write target should be checked
    # against the target the caller asked for rather than trusted.
    expected = f"CREATE OR REPLACE TABLE {ident(dataset_name)} AS"
    for a in actions:
        if not a.sql.startswith(expected):
            raise ValueError(
                f"{a.action_id} does not write to {dataset_name}. Its statement "
                f"begins {a.sql.splitlines()[0]!r}. A plan proposed against one "
                f"table cannot be applied to another."
            )

    version = next_version(con, dataset_name)
    snapshot = history_table(dataset_name, version)
    before = _rows(con, dataset_name)
    applied_at = now or datetime.now()

    # Outside the transaction on purpose: CREATE TABLE IF NOT EXISTS inside one
    # would roll back with everything else, and the ledger's own existence is
    # not part of what an apply is allowed to undo.
    ledger.ensure_table(con)

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            f"CREATE TABLE {ident(snapshot)} AS SELECT * FROM {ident(dataset_name)}"
        )
        applied = []
        for a in actions:
            rows_before = _rows(con, dataset_name)
            con.execute(a.sql)
            rows_after = _rows(con, dataset_name)
            ledger.record_action(
                con,
                applied_at=applied_at,
                dataset_name=dataset_name,
                plan_id=plan_id,
                action_id=a.action_id,
                kind=a.kind.value,
                column=a.column,
                rows_before=rows_before,
                rows_after=rows_after,
                history_table=snapshot,
                statement=a.sql,
            )
            applied.append(
                AppliedAction(
                    action_id=a.action_id,
                    kind=a.kind,
                    column=a.column,
                    rows_before=rows_before,
                    rows_after=rows_after,
                    statement=a.sql,
                )
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return ApplyResult(
        dataset_name=dataset_name,
        history_table=snapshot,
        rows_before=before,
        rows_after=_rows(con, dataset_name),
        applied=tuple(applied),
    )


__all__ = [
    "HISTORY_PREFIX",
    "AppliedAction",
    "ApplyResult",
    "apply",
    "conflicts",
    "history_table",
    "history_tables",
    "next_version",
    "read_history",
]
