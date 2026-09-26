"""DuckDB connection management, scoped to a workspace.

Phase 2, Step 2. See build guide Section 4.1 (why DuckDB), 6.7 (workspace
identity), F2 (file lock) and F13 (state leaking across chats).

Two jobs:

  1. Hand out a DuckDB connection for a given workspace, one file per
     workspace, so two server processes never fight over one lock.
  2. Maintain the `_agent_datasets` table -- the record of what is loaded,
     when it was loaded and where it came from.

Why _agent_datasets exists in Phase 2 rather than Phase 4: Claude Desktop keeps
this process alive across chats, so a dataset loaded two hours ago in an
unrelated conversation is still sitting in the catalog. get_workflow_state
(Phase 4) has to be able to show every dataset with its load time, and the only
place that timestamp can honestly be captured is at load time. Recording it
later would be inventing it.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb

from ..config import DEFAULT_WORKSPACE_ID, validate_workspace_id
from .. import workspace

# Internal bookkeeping table. The leading underscore marks it as not-user-data;
# describe_dataset and every listing tool must filter it out.
METADATA_TABLE = "_agent_datasets"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {METADATA_TABLE} (
    dataset_name   VARCHAR PRIMARY KEY,
    source_type    VARCHAR NOT NULL,
    source_detail  VARCHAR,
    row_count      BIGINT,
    column_count   INTEGER,
    loaded_at      TIMESTAMP NOT NULL,
    notes          VARCHAR
)
"""


# postgres.load_table writes notes="where=<sql or ->, limit=<n or ->, source_rows=<n>". Greedy
# on the where clause, which is free SQL; source_rows is absent from records written before C95.
_NARROWING_RE = re.compile(r"^where=(.*), limit=(-|\d+)(?:, source_rows=(\d+))?$", re.S)


@dataclass(frozen=True)
class DatasetRecord:
    dataset_name: str
    source_type: str
    source_detail: str
    row_count: int
    column_count: int
    loaded_at: _dt.datetime
    notes: str = ""

    def narrowing(self) -> str | None:
        """One sentence when this load holds part of its source, None when it holds all of it.

        Only the Postgres loader narrows -- where and limit -- and it records both, with the
        source's row count, in `notes`. They were recorded and read by nothing, so a table cut
        to fit the copy limit was analysed and reported as the whole table (C95). The where
        clause is free SQL and may hold a comma, so the pattern anchors on the fields after it.
        A record written before source_rows existed still discloses, and says what it lacks.
        """
        if self.source_type != "postgres":
            return None
        m = _NARROWING_RE.match(self.notes or "")
        if m is None:
            return None
        where, limit, source_rows = m.group(1), m.group(2), m.group(3)
        where = None if where == "-" else where
        limit = None if limit == "-" else int(limit)
        total = int(source_rows) if source_rows else None
        if where is None and (limit is None or (total is not None and limit >= total)):
            return None
        filters = ", ".join(f for f in (where and f"where={where}", limit and f"limit={limit}")
                            if f)
        if total is not None:
            held = f"{self.row_count:,} of {total:,} rows"
        else:
            held = f"{self.row_count:,} rows (the source table's size was not recorded)"
        text = (f"Loaded as a subset: {held} of {self.source_detail}, copied with {filters}. "
                f"Every figure describes those rows, not the whole table.")
        if limit is not None:
            text += (" A limit keeps whichever rows the database returned first -- not a "
                     "random sample -- so shares and distinct counts can be far from the "
                     "table's.")
        return text

    def age_phrase(self, now: _dt.datetime | None = None) -> str:
        """Plain-language age, so a stale dataset reads as stale.

        'loaded 3 hours ago' makes a leftover from an unrelated chat obvious in
        a way that a bare timestamp does not.
        """
        now = now or _dt.datetime.now()
        seconds = max(0, int((now - self.loaded_at).total_seconds()))
        if seconds < 90:
            return "loaded just now"
        minutes = seconds // 60
        if minutes < 90:
            return f"loaded {minutes} minutes ago"
        hours = minutes // 60
        if hours < 36:
            return f"loaded {hours} hours ago"
        return f"loaded {hours // 24} days ago"


def connect(workspace_id: str = DEFAULT_WORKSPACE_ID) -> duckdb.DuckDBPyConnection:
    """Open the DuckDB file for this workspace, creating it if needed.

    One file per workspace (locked decision 21). DuckDB takes an exclusive lock
    per file, so two processes on one file raise
    'IO Error: Could not set lock on file' -- F2. Track A has a single
    workspace and never hits this; Track B hits it with the second user, who
    would also see the first user's tables. Scoping the file now means Phase 14
    changes nothing here.

    The caller closes the connection. Do not cache it at module level: a
    long-lived handle holds the lock and defeats the isolation.
    """
    validate_workspace_id(workspace_id)
    path = workspace.duckdb_path(workspace_id)
    con = duckdb.connect(str(path))
    create_if_missing(con, _SCHEMA)
    return con


def connect_read_only(workspace_id: str = DEFAULT_WORKSPACE_ID) -> duckdb.DuckDBPyConnection:
    """A handle on this workspace that the engine will not let write.

    Moved here from `clean/tools.py` in Phase 7, Step 7, which is the moment
    that file's own docstring named: "connect_read_only lives here for now
    rather than in util/db.py, where it would sit beside a function it
    deliberately does not call. Moving it is a one-line change if that reads
    better later." A second package needed it, so later arrived.

    It cannot be `connect()` with a flag, and that is the reason it is a
    separate function rather than a parameter: `connect()` runs
    `CREATE TABLE IF NOT EXISTS _agent_datasets` on every open, and a
    read-only ATTACH refuses CREATE by statement type, IF NOT EXISTS included.
    Phase 6, Step 1 measured that.

    One handle per file per process still holds. Open this OR `connect()`,
    never both at once.
    """
    validate_workspace_id(workspace_id)
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{workspace.duckdb_path(workspace_id)}' AS ws (READ_ONLY)")
    con.execute("USE ws")
    return con


def register_dataset(
    con: duckdb.DuckDBPyConnection,
    dataset_name: str,
    source_type: str,
    source_detail: str,
    row_count: int,
    column_count: int,
    notes: str = "",
) -> DatasetRecord:
    """Record a load in _agent_datasets. Every loader calls this on success.

    Re-loading the same dataset_name replaces the row rather than appending, so
    the timestamp always reflects the data actually present. This is distinct
    from the cleaning ledger (Phase 6), which is append-only because it records
    history rather than current state.
    """
    now = _dt.datetime.now()
    con.execute(f"DELETE FROM {METADATA_TABLE} WHERE dataset_name = ?", [dataset_name])
    con.execute(
        f"INSERT INTO {METADATA_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
        [dataset_name, source_type, source_detail, row_count,
         column_count, now, notes],
    )
    return DatasetRecord(
        dataset_name=dataset_name,
        source_type=source_type,
        source_detail=source_detail,
        row_count=row_count,
        column_count=column_count,
        loaded_at=now,
        notes=notes,
    )


def list_datasets(con: duckdb.DuckDBPyConnection) -> list[DatasetRecord]:
    """Every registered dataset, newest first."""
    rows = con.execute(
        f"""SELECT dataset_name, source_type, source_detail, row_count,
                   column_count, loaded_at, notes
            FROM {METADATA_TABLE} ORDER BY loaded_at DESC"""
    ).fetchall()
    return [
        DatasetRecord(
            dataset_name=r[0], source_type=r[1], source_detail=r[2] or "",
            row_count=r[3] or 0, column_count=r[4] or 0,
            loaded_at=r[5], notes=r[6] or "",
        )
        for r in rows
    ]


def get_dataset(
    con: duckdb.DuckDBPyConnection, dataset_name: str
) -> DatasetRecord | None:
    """One dataset's record, or None if it was never loaded."""
    for rec in list_datasets(con):
        if rec.dataset_name == dataset_name:
            return rec
    return None


def unregister_dataset(con: duckdb.DuckDBPyConnection, dataset_name: str) -> bool:
    """Forget a dataset. Returns True if a row was removed."""
    existed = get_dataset(con, dataset_name) is not None
    con.execute(f"DELETE FROM {METADATA_TABLE} WHERE dataset_name = ?", [dataset_name])
    return existed


def create_if_missing(con, ddl: str) -> None:
    """Run a CREATE TABLE IF NOT EXISTS that another connection may be running at the same time.

    Two connections creating the same table at once conflict in DuckDB's catalog
    ("Catalog write-write conflict on create", a TransactionException) -- the loser is not told
    the table exists. Found by the cross-domain benchmark's concurrency phase (Step 13: D4):
    threads analysing different datasets of one workspace, each first to create a log table.
    The loser retries, and IF NOT EXISTS then finds it.
    """
    import time as _time
    for attempt in range(6):
        try:
            con.execute(ddl)
            return
        except duckdb.TransactionException:
            if attempt == 5:
                raise
            _time.sleep(0.01 * (attempt + 1))


def user_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Tables in the catalog excluding internal bookkeeping.

    A table can exist without a metadata row (created by hand in run_sql, or a
    load that failed after CREATE). Comparing this against list_datasets is how
    describe_dataset spots the difference instead of hiding it.
    """
    rows = con.execute(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema = 'main' AND table_name NOT LIKE '\\_%' ESCAPE '\\'
           ORDER BY table_name"""
    ).fetchall()
    return [r[0] for r in rows]


def utc_timestamps(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """Convert every TIMESTAMP WITH TIME ZONE column of `table` to TIMESTAMP, in UTC.

    DuckDB hands a TIMESTAMPTZ value to Python only through pytz, which this project does not
    depend on, so the first tool to fetch one -- describe_dataset -- raised ModuleNotFoundError
    (P14-D55). UTC keeps every instant exact; the returned names go into the load's notes.
    """
    cols = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' "
        "AND table_name = ? AND data_type = 'TIMESTAMP WITH TIME ZONE'", [table]).fetchall()]
    for c in cols:
        q = '"' + c.replace('"', '""') + '"'
        con.execute(f'ALTER TABLE "{table}" ALTER {q} TYPE TIMESTAMP USING timezone(\'UTC\', {q})')
    return cols


def utc_note(cols: list[str]) -> list[str]:
    return ([f"{', '.join(cols)} carried a time zone offset; stored in UTC, so an instant written "
             f"12:00+05:30 reads 06:30."] if cols else [])


def table_shape(con: duckdb.DuckDBPyConnection, table: str) -> tuple[int, int]:
    """(row_count, column_count) for a table. Raises if it does not exist."""
    rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    cols = con.execute(
        """SELECT count(*) FROM information_schema.columns
           WHERE table_schema = 'main' AND table_name = ?""",
        [table],
    ).fetchone()[0]
    return int(rows), int(cols)


def workspace_summary(workspace_id: str = DEFAULT_WORKSPACE_ID) -> str:
    """Human-readable state of one workspace. Opens and closes its own
    connection, so it is safe to call from a tool without holding the lock."""
    con = connect(workspace_id)
    try:
        records = list_datasets(con)
        tables = user_tables(con)
        registered = {r.dataset_name for r in records}
        orphans = [t for t in tables if t not in registered]

        if not records and not tables:
            return f"Workspace '{workspace_id}' is empty. Nothing loaded."

        lines = [f"Workspace '{workspace_id}' - {len(records)} dataset(s)", ""]
        lines.append("| dataset | source | rows | cols | loaded |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in records:
            lines.append(
                f"| {r.dataset_name} | {r.source_type} | {r.row_count:,} "
                f"| {r.column_count} | {r.age_phrase()} |"
            )
        if orphans:
            lines.append("")
            lines.append(
                "NOTE: tables present with no load record: "
                + ", ".join(orphans)
                + ". These were not created by a loader."
            )
        return "\n".join(lines)
    finally:
        con.close()