"""
That a profile happened, when, and against how much data.

Section 8.2's example state string reads `loaded (51,290 rows), profiled, not
cleaned, no contract`. Nothing recorded the middle word until this module, and
without it `get_workflow_state` cannot tell a table nobody has looked at from
one profiled twice this morning.

**Why a row rather than a file on disk.** The result files are already there and
their names carry timestamps, so the stage could be inferred by globbing the
results directory. That would be cheaper and it would answer a worse question.
Inferring gives "a profile exists"; a record gives *profiled 40 minutes ago at
500 rows, and the table now has 530* -- which is the F13 leak, the one Phase 4
built `get_workflow_state` to surface, arriving through a door Phase 4 did not
cover. A directory listing cannot say that because it does not know what the
table looked like at the time.

**Append-only, and no versioning.** `contract/store.py` keeps SCD2 spans because
a contract SUPERSEDES its predecessor -- there is exactly one current agreement
and the old one has to stay readable. A profile supersedes nothing. It is an
observation, three of them in an hour are three facts rather than three drafts,
and the newest is interesting only because it is newest. So: one row per run,
`latest()` orders by time, and nothing is ever marked current or closed.

**Naming.** `_agent_profiles`, matching `_agent_datasets` and
`_agent_contracts`. The name is not decoration: a bookkeeping table that reads
as a dataset gets listed as one, which is precisely what happened to
`_agent_contracts` once. `is_bookkeeping()` exists so a filter can ask rather
than hard-code a third name it will forget to update on the fourth.
"""

from __future__ import annotations

from backend.engine.util import db

from dataclasses import dataclass
from datetime import datetime

PROFILE_TABLE = "_agent_profiles"

# Every table this codebase keeps for its own use starts here. A filter that
# tests the prefix keeps working when a fifth arrives; a filter that lists
# three names does not.
BOOKKEEPING_PREFIX = "_agent_"


def is_bookkeeping(table_name: str) -> bool:
    """True for a table the agent keeps for itself rather than for the user."""
    return table_name.startswith(BOOKKEEPING_PREFIX)


@dataclass(frozen=True)
class ProfileRun:
    """One profiling run, as it was recorded."""

    dataset_name: str
    run_at: datetime
    row_count: int
    column_count: int
    result_path: str
    duplicate_rows: int | None = None
    columns_missing: int = 0

    def age_phrase(self, now: datetime | None = None) -> str:
        """
        How long ago, in words, with the absolute time alongside.

        Both, deliberately. "12 minutes ago" is what a person reads and it is
        meaningless in a transcript read tomorrow; the timestamp is precise and
        nobody computes an interval from it in their head.
        """
        stamp = self.run_at.strftime("%Y-%m-%d %H:%M")
        delta = (now or datetime.now()) - self.run_at
        seconds = max(0, int(delta.total_seconds()))
        if seconds < 90:
            return f"profiled just now ({stamp})"
        minutes = seconds // 60
        if minutes < 90:
            return f"profiled {minutes} minutes ago ({stamp})"
        hours = minutes // 60
        if hours < 36:
            return f"profiled {hours} hours ago ({stamp})"
        return f"profiled {hours // 24} days ago ({stamp})"

    def drift_phrase(self, current_rows: int) -> str | None:
        """
        What changed under the profile, if anything.

        The whole reason the row count is stored. A profile of 500 rows read
        beside a table of 530 is not wrong, it is OLD, and those need different
        words -- an agent told "stale" re-profiles, an agent told "wrong"
        apologises.
        """
        if current_rows == self.row_count:
            return None
        if current_rows > self.row_count:
            gained = current_rows - self.row_count
            return (
                f"the table has gained {gained:,} row(s) since "
                f"({self.row_count:,} then, {current_rows:,} now), so these "
                f"counts describe less data than is loaded"
            )
        lost = self.row_count - current_rows
        return (
            f"the table has lost {lost:,} row(s) since ({self.row_count:,} "
            f"then, {current_rows:,} now), so this profile describes rows that "
            f"are no longer there"
        )


def ensure_table(con) -> None:
    """
    Create the log if it is missing.

    Lazily, not in `db.connect()`, for the reason `contract/store.py` records:
    a workspace that has never profiled anything should not carry an empty
    table, and every read path here calls this first anyway.
    """
    db.create_if_missing(
        con,
        f"""
        CREATE TABLE IF NOT EXISTS {PROFILE_TABLE} (
            dataset_name   VARCHAR NOT NULL,
            run_at         TIMESTAMP NOT NULL,
            row_count      BIGINT NOT NULL,
            column_count   INTEGER NOT NULL,
            result_path    VARCHAR NOT NULL,
            duplicate_rows BIGINT,
            columns_missing INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def record(
    con,
    *,
    dataset_name: str,
    row_count: int,
    column_count: int,
    result_path: str,
    duplicate_rows: int | None = None,
    columns_missing: int = 0,
    now: datetime | None = None,
) -> ProfileRun:
    """Append one run. Never updates, never deletes."""
    ensure_table(con)
    run = ProfileRun(
        dataset_name=dataset_name,
        run_at=now or datetime.now(),
        row_count=row_count,
        column_count=column_count,
        result_path=str(result_path),
        duplicate_rows=duplicate_rows,
        columns_missing=columns_missing,
    )
    con.execute(
        f"INSERT INTO {PROFILE_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            run.dataset_name, run.run_at, run.row_count, run.column_count,
            run.result_path, run.duplicate_rows, run.columns_missing,
        ],
    )
    return run


def _row_to_run(row) -> ProfileRun:
    return ProfileRun(
        dataset_name=row[0], run_at=row[1], row_count=row[2],
        column_count=row[3], result_path=row[4], duplicate_rows=row[5],
        columns_missing=row[6],
    )


def latest(con, dataset_name: str) -> ProfileRun | None:
    """The most recent run for one dataset, or None if it was never profiled."""
    ensure_table(con)
    row = con.execute(
        f"SELECT * FROM {PROFILE_TABLE} WHERE dataset_name = ? "
        f"ORDER BY run_at DESC LIMIT 1",
        [dataset_name],
    ).fetchone()
    return _row_to_run(row) if row else None


def history(con, dataset_name: str) -> list[ProfileRun]:
    """Every run for one dataset, newest first."""
    ensure_table(con)
    rows = con.execute(
        f"SELECT * FROM {PROFILE_TABLE} WHERE dataset_name = ? "
        f"ORDER BY run_at DESC",
        [dataset_name],
    ).fetchall()
    return [_row_to_run(r) for r in rows]


def all_latest(con) -> dict[str, ProfileRun]:
    """The most recent run per dataset, keyed by name."""
    ensure_table(con)
    rows = con.execute(
        f"SELECT * FROM {PROFILE_TABLE} p WHERE run_at = ("
        f"  SELECT max(run_at) FROM {PROFILE_TABLE} q "
        f"  WHERE q.dataset_name = p.dataset_name)"
    ).fetchall()
    return {r[0]: _row_to_run(r) for r in rows}


def state_notes(
    con, dataset_name: str, current_rows: int, now: datetime | None = None
) -> list[str]:
    """
    What `get_workflow_state` should say about profiling for one dataset.

    Returns lines, not a paragraph, so the caller decides how they sit among
    everything else it reports. An empty list means the dataset has never been
    profiled -- which is itself worth saying, and the caller says it, because
    only the caller knows whether that matters yet.
    """
    run = latest(con, dataset_name)
    if run is None:
        return []
    lines = [f"{run.age_phrase(now)}, {run.row_count:,} rows"]
    drift = run.drift_phrase(current_rows)
    if drift:
        lines.append(
            f"That profile is out of date: {drift}. "
            f'Re-run profile_dataset(dataset_name="{dataset_name}").'
        )
    else:
        lines.append(f"Full counts: read_result_file(path=\"{run.result_path}\")")
    return lines


__all__ = [
    "BOOKKEEPING_PREFIX",
    "PROFILE_TABLE",
    "ProfileRun",
    "all_latest",
    "ensure_table",
    "history",
    "is_bookkeeping",
    "latest",
    "record",
    "state_notes",
]
