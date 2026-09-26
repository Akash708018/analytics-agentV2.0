"""That a validation ran, when, against which contract, and what it found.

Phase 7, Step 8.

**Why a row rather than nothing at all.** `get_workflow_state` can say a dataset
is loaded, profiled and cleaned. It cannot say whether anybody has checked it
against its contract, and "nobody has validated this" is exactly the sentence a
reader of that report needs before trusting a number computed from it. Nothing
else in the workspace can answer it: validation writes no file, so there is not
even a directory listing to infer from.

**Why the counts and not a path.** `profile/runs.py` stores `result_path`
because a profile is expensive and its output is a CSV somebody may page
through. A validation report is prose, regenerated in one call, and writing it
to disk would create the F7 shape on purpose -- a path in a transcript that
nobody opens. So the record stores what the state line needs to say something
useful -- how many checks ran, failed and could not run -- and the report itself
is re-derived by calling the tool.

**Why the row count is stored.** The same reason `profile/runs.py` stores it:
`validated 40 minutes ago at 186 rows, and the table now holds 190` is the F13
leak arriving through a door this phase opened. A validation is a statement
about a table at a moment, and the moment has to be recorded beside it or the
statement quietly becomes a claim about data nobody checked.

**Append-only, no versioning.** A validation supersedes nothing. Three runs in
an hour are three facts, and the newest is interesting only because it is
newest. `contract/store.py` keeps SCD2 spans because a contract replaces its
predecessor; this does not.
"""

from __future__ import annotations

from backend.engine.util import db

from dataclasses import dataclass
from datetime import datetime

from ..profile.runs import BOOKKEEPING_PREFIX

VALIDATION_TABLE = f"{BOOKKEEPING_PREFIX}validations"


@dataclass(frozen=True)
class ValidationRun:
    """One run, as it was recorded."""

    dataset_name: str
    run_at: datetime
    contract_version: int
    row_count: int
    checks_total: int
    checks_failed: int
    checks_not_run: int

    @property
    def checks_passed(self) -> int:
        return self.checks_total - self.checks_failed - self.checks_not_run

    def age_phrase(self, now: datetime | None = None) -> str:
        """How long ago, in words, with the timestamp alongside.

        Both, for the reason `profile/runs.ProfileRun.age_phrase` gives: the
        interval is what a person reads and means nothing in a transcript read
        tomorrow, and the timestamp is precise and nobody computes an interval
        from it in their head.
        """
        stamp = self.run_at.strftime("%Y-%m-%d %H:%M")
        seconds = max(0, int(((now or datetime.now()) - self.run_at).total_seconds()))
        if seconds < 90:
            return f"validated just now ({stamp})"
        minutes = seconds // 60
        if minutes < 90:
            return f"validated {minutes} minutes ago ({stamp})"
        hours = minutes // 60
        if hours < 36:
            return f"validated {hours} hours ago ({stamp})"
        return f"validated {hours // 24} days ago ({stamp})"

    def verdict_phrase(self) -> str:
        """What it found, in the vocabulary P7-D10 settled.

        Counts, never one word. A run where nothing failed and four checks
        could not run is not a pass, and the state line is read faster than
        the report is.
        """
        parts = []
        if self.checks_failed:
            parts.append(f"{self.checks_failed} of {self.checks_total} failed")
        if self.checks_passed:
            parts.append(
                f"{self.checks_passed} passed" if parts
                else f"{self.checks_passed} of {self.checks_total} passed"
            )
        if self.checks_not_run:
            parts.append(f"{self.checks_not_run} could not run")
        return ", ".join(parts) if parts else "nothing was checked"

    def drift_phrase(self, current_rows: int) -> str | None:
        """What changed under the validation, if anything.

        Old, not wrong -- the distinction `profile/runs.py` draws in the same
        words, because an agent told "stale" re-runs and an agent told "wrong"
        apologises.
        """
        if current_rows == self.row_count:
            return None
        if current_rows > self.row_count:
            return (
                f"the table has gained {current_rows - self.row_count:,} row(s) "
                f"since ({self.row_count:,} then, {current_rows:,} now), so "
                f"nothing has checked the rows that arrived after it"
            )
        return (
            f"the table has lost {self.row_count - current_rows:,} row(s) since "
            f"({self.row_count:,} then, {current_rows:,} now), so this describes "
            f"rows that are no longer there"
        )


def ensure_table(con) -> None:
    """Create the log if it is missing, lazily.

    Not in `db.connect()`, for the reason `contract/store.py` and
    `profile/runs.py` both record: a workspace that has never validated
    anything should not carry an empty table, and every read path here calls
    this first anyway.
    """
    db.create_if_missing(
        con,
        f"""
        CREATE TABLE IF NOT EXISTS {VALIDATION_TABLE} (
            dataset_name     VARCHAR NOT NULL,
            run_at           TIMESTAMP NOT NULL,
            contract_version INTEGER NOT NULL,
            row_count        BIGINT NOT NULL,
            checks_total     INTEGER NOT NULL,
            checks_failed    INTEGER NOT NULL,
            checks_not_run   INTEGER NOT NULL
        )
        """
    )


def record(
    con,
    *,
    dataset_name: str,
    contract_version: int,
    row_count: int,
    checks_total: int,
    checks_failed: int,
    checks_not_run: int,
    now: datetime | None = None,
) -> ValidationRun:
    """Append one run. Never updates, never deletes."""
    ensure_table(con)
    run = ValidationRun(
        dataset_name=dataset_name,
        run_at=now or datetime.now(),
        contract_version=contract_version,
        row_count=row_count,
        checks_total=checks_total,
        checks_failed=checks_failed,
        checks_not_run=checks_not_run,
    )
    con.execute(
        f"INSERT INTO {VALIDATION_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            run.dataset_name, run.run_at, run.contract_version, run.row_count,
            run.checks_total, run.checks_failed, run.checks_not_run,
        ],
    )
    return run


def _row_to_run(row) -> ValidationRun:
    return ValidationRun(
        dataset_name=row[0], run_at=row[1], contract_version=row[2],
        row_count=row[3], checks_total=row[4], checks_failed=row[5],
        checks_not_run=row[6],
    )


def latest(con, dataset_name: str) -> ValidationRun | None:
    """The most recent run for one dataset, or None if it was never validated."""
    ensure_table(con)
    row = con.execute(
        f"SELECT * FROM {VALIDATION_TABLE} WHERE dataset_name = ? "
        f"ORDER BY run_at DESC LIMIT 1",
        [dataset_name],
    ).fetchone()
    return _row_to_run(row) if row else None


def history(con, dataset_name: str) -> list[ValidationRun]:
    """Every run for one dataset, newest first."""
    ensure_table(con)
    rows = con.execute(
        f"SELECT * FROM {VALIDATION_TABLE} WHERE dataset_name = ? "
        f"ORDER BY run_at DESC",
        [dataset_name],
    ).fetchall()
    return [_row_to_run(r) for r in rows]


def state_notes(
    con,
    dataset_name: str,
    current_rows: int,
    current_version: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Lines for get_workflow_state, or [] if this dataset was never validated.

    `current_version` is the version in force NOW, or None when the caller does
    not know. None skips the comparison rather than assuming a match.

    Deliberately the same shape as `profile/runs.state_notes` and
    `clean/ledger.state_notes`: a list the caller splices in, empty when there
    is nothing to say. The third module to arrive this way, which is the point
    -- `describe_workflow_state` has not needed a new branch since Phase 5.

    NOT added to `DatasetState.stage`, for the reason Phase 6 Step 10c gives
    about cleaning: that column is about whether ANALYSIS CAN RUN, and
    validation is a different axis again. A dataset can be validated and have
    no contract in force by the time you read it, or carry a contract nobody
    has ever checked it against.
    """
    run = latest(con, dataset_name)
    if run is None:
        return []
    lines = [
        f"{run.age_phrase(now)} against contract v{run.contract_version}: "
        f"{run.verdict_phrase()}."
    ]

    # Two ways a validation goes out of date, and Step 8 shipped one of them.
    # The rows can move under it, and the AGREEMENT can move over it: validate
    # at v2, confirm v3, and without this the section reads "validated against
    # contract v2" above "Contract v3" with nothing saying the check predates
    # the agreement it is quoted against. Collected into one sentence with one
    # instruction, because two "re-run this" lines for one stale run is noise.
    reasons = []
    if current_version is not None and current_version != run.contract_version:
        reasons.append(
            f"the contract has moved to v{current_version} since "
            f"(v{run.contract_version} then), so this describes an agreement "
            f"nobody is working to now"
        )
    drift = run.drift_phrase(current_rows)
    if drift:
        reasons.append(drift)

    if reasons:
        lines.append(
            f"That validation is out of date: {'; '.join(reasons)}. "
            f'Re-run validate_dataset(dataset_name="{dataset_name}").'
        )
    else:
        lines.append(
            f'Full report: validate_dataset(dataset_name="{dataset_name}")'
        )
    return lines

__all__ = [
    "VALIDATION_TABLE",
    "ValidationRun",
    "ensure_table",
    "history",
    "latest",
    "record",
    "state_notes",
]
