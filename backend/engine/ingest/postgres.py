"""Postgres as a data source, via DuckDB's postgres extension.

Phase 2, Step 7. See build guide locked decision 22 (Postgres is ALWAYS
READ_ONLY), Section 4.2, and Section 8.2 (instructional refusals).

Two ways to use an attached database:

  1. Query it in place -- <alias>.public.orders. Nothing is copied. No size limit,
     because DuckDB pushes the work down to Postgres. INSPECTION ONLY: preview,
     describe and row counts reach an attached table; analysis does not, because
     compute_analysis goes through require_contract and state._loadable_tables
     reads db.user_tables, which lists what this workspace owns. An attached
     catalog is not a loaded dataset however cleanly it attaches. That is P9-O4.
  2. Copy a table into the workspace -- load_table(). Subject to a row gate,
     because that data does land in the DuckDB file.

Tools take an ALIAS from the registry, never a raw DSN, so a connection string
cannot reach a chat transcript. See config.load_source_registry.

Verified against a live PostgreSQL server:

  * ATTACH '<dsn>' AS pg (TYPE postgres, READ_ONLY) works as the guide states.
  * READ_ONLY is genuinely enforced. CREATE, INSERT and DELETE against the
    attached database all raise InvalidInputException. It is not advisory.
  * Postgres NUMERIC(10,2) arrives as DuckDB DECIMAL(10,2); TEXT as VARCHAR.
  * Non-default schemas are visible: pg.staging.whatever resolves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import duckdb

from ..config import (
    SIZE_GATES,
    load_source_registry,
    redact_dsn,
)
from ..util import db
from .csv_loader import LoadRefused, LoadResult, validate_dataset_name

# System schemas are never interesting and clutter every listing.
_HIDDEN_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

# Copying a table into the workspace is bounded by the same reasoning as a
# file load: it becomes rows in the DuckDB file. Reuses the Excel row gates
# rather than inventing new numbers, since both are row-oriented copies.
ROW_WARN = SIZE_GATES.excel_warn_rows
ROW_REFUSE = SIZE_GATES.excel_refuse_rows


@dataclass(frozen=True)
class PgTable:
    schema: str
    name: str
    estimated_rows: int | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


def list_sources() -> str:
    """Configured aliases, with DSNs redacted. Never returns a password."""
    reg = load_source_registry()
    lines = ["| alias | connection | description |", "| --- | --- | --- |"]
    for alias, src in sorted(reg.sources.items()):
        lines.append(f"| {alias} | {src.redacted()} | {src.description or '-'} |")
    out = "\n".join(lines)
    if reg.load_error:
        out += f"\n\nNOTE: {reg.load_error}"
    out += f"\n\nAdd more in {reg.path}."
    return out


def _resolve_dsn(alias: str) -> str:
    reg = load_source_registry()
    try:
        return reg.get(alias).dsn
    except KeyError as exc:
        raise LoadRefused(str(exc.args[0])) from exc


#: A downloaded postgres_scanner.duckdb_extension file, for a machine that cannot reach
#: extensions.duckdb.org (P14-O25). Installed from the file, then cached like any other.
EXTENSION_FILE_ENV = "ANALYTICS_DUCKDB_POSTGRES_EXTENSION"


def _load_extension(con) -> None:
    """LOAD first: a cached extension needs no network. INSTALL only when that fails -- from
    the file EXTENSION_FILE_ENV names if set, from DuckDB's repository otherwise."""
    try:
        con.execute("LOAD postgres")
        return
    except duckdb.Error:
        pass
    local = os.environ.get(EXTENSION_FILE_ENV)
    try:
        if local:
            con.execute("INSTALL '" + local.replace("'", "''") + "'")
        else:
            con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
    except duckdb.Error as exc:
        platform = con.execute("PRAGMA platform").fetchone()[0]
        version = "v" + duckdb.__version__
        raise LoadRefused(
            f"BLOCKED: could not load DuckDB's postgres extension.\n"
            f"DuckDB said: {exc}\n"
            f"WHY: the extension is downloaded on first use and cached in "
            f"~/.duckdb/extensions/{version}/{platform}/; this machine has neither the "
            f"cached copy nor a route to the download.\n"
            f"NEXT STEP: allow access to extensions.duckdb.org once, or download "
            f"http://extensions.duckdb.org/{version}/{platform}/postgres_scanner.duckdb_extension.gz"
            f" on another machine, unzip it, and set {EXTENSION_FILE_ENV} to the file's path."
        ) from exc


def attach(
    con: duckdb.DuckDBPyConnection,
    alias: str,
    db_alias: str | None = None,
) -> str:
    """ATTACH a configured Postgres source, read-only. Returns the db alias.

    The attachment is named after the source, so `olist` attaches as `olist`
    and is queried as olist.public.orders. Two sources can therefore be
    attached at once.

    That naming is not cosmetic. An earlier version attached everything as
    "pg" and skipped the work when "pg" already existed -- so attaching a
    SECOND source silently reused the FIRST, and queries ran against the wrong
    database with no error at all. Keying the attachment to the source name
    makes that impossible.

    Idempotent: re-attaching the same source is a no-op, because a tool may be
    called twice in one chat.

    READ_ONLY is not optional and is not a parameter. Locked decision 22.
    """
    dsn = _resolve_dsn(alias)
    db_alias = db_alias or alias
    validate_dataset_name(db_alias)

    attached = {r[0] for r in con.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    if db_alias in attached:
        return db_alias

    _load_extension(con)

    try:
        con.execute(
            f"ATTACH '{dsn}' AS {db_alias} (TYPE postgres, READ_ONLY)"
        )
    except duckdb.Error as exc:
        raise LoadRefused(
            f"BLOCKED: could not connect to source '{alias}'.\n"
            f"Connection: {redact_dsn(dsn)}\n"
            f"DuckDB said: {exc}\n"
            f"NEXT STEP: check the server is running (`pg_isready`), that the "
            f"database name is right (`psql -l`), and that the alias in "
            f"sources.yaml matches."
        ) from exc

    return db_alias


def detach(con: duckdb.DuckDBPyConnection, db_alias: str) -> bool:
    """Release an attachment. True if one was released."""
    attached = {r[0] for r in con.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    if db_alias not in attached:
        return False
    con.execute(f"DETACH {db_alias}")
    return True


def list_tables(
    con: duckdb.DuckDBPyConnection,
    alias: str,
    db_alias: str | None = None,
    schema: str | None = None,
) -> list[PgTable]:
    """Tables and views in the attached database, system schemas excluded."""
    db_alias = attach(con, alias, db_alias)
    hidden = ", ".join(f"'{s}'" for s in _HIDDEN_SCHEMAS)
    where = f"table_schema NOT IN ({hidden})"
    params: list = []
    if schema:
        where += " AND table_schema = ?"
        params.append(schema)
    rows = con.execute(
        f"""SELECT table_schema, table_name FROM {db_alias}.information_schema.tables
            WHERE {where} ORDER BY table_schema, table_name""",
        params,
    ).fetchall()
    return [PgTable(schema=r[0], name=r[1]) for r in rows]


def describe_source(
    con: duckdb.DuckDBPyConnection, alias: str, db_alias: str | None = None
) -> str:
    """Markdown listing of what is in a source. Does not copy anything."""
    # Resolve the attachment name FIRST. list_tables() resolves its own copy
    # internally, and that does not propagate back here -- an earlier version
    # left db_alias as None, built "SELECT count(*) FROM None.public.orders",
    # and the except below turned every count into "?" while still listing the
    # tables correctly. A broad except that hides a bug is worse than no
    # except, so this one now reports what went wrong.
    db_alias = attach(con, alias, db_alias)
    tables = list_tables(con, alias, db_alias)
    if not tables:
        return f"Source '{alias}' has no user tables."
    lines = [f"Source '{alias}' - {len(tables)} table(s)", "",
             "| schema | table | rows |", "| --- | --- | --- |"]
    errors: list[str] = []
    for t in tables:
        try:
            n = con.execute(
                f'SELECT count(*) FROM {db_alias}."{t.schema}"."{t.name}"'
            ).fetchone()[0]
            n_str = f"{n:,}"
        except duckdb.Error as exc:
            n_str = "?"
            if len(errors) < 3:
                errors.append(f"{t.qualified}: {exc}")
        lines.append(f"| {t.schema} | {t.name} | {n_str} |")
    if errors:
        lines.append("")
        lines.append("NOTE: some row counts could not be read:")
        lines.extend(f"  {e}" for e in errors)
    return "\n".join(lines)


def preview_table(
    con: duckdb.DuckDBPyConnection,
    alias: str,
    table: str,
    schema: str = "public",
    n: int = 20,
    db_alias: str | None = None,
) -> list[tuple]:
    """First n rows, queried in place. Nothing is copied into the workspace."""
    db_alias = attach(con, alias, db_alias)
    return con.execute(
        f'SELECT * FROM {db_alias}."{schema}"."{table}" LIMIT {int(n)}'
    ).fetchall()


def row_count(
    con: duckdb.DuckDBPyConnection,
    alias: str,
    table: str,
    schema: str = "public",
    db_alias: str | None = None,
) -> int:
    db_alias = attach(con, alias, db_alias)
    try:
        return con.execute(
            f'SELECT count(*) FROM {db_alias}."{schema}"."{table}"'
        ).fetchone()[0]
    except duckdb.Error as exc:
        available = list_tables(con, alias, db_alias)
        names = ", ".join(t.qualified for t in available[:20]) or "(none)"
        raise LoadRefused(
            f"BLOCKED: no table {schema}.{table} in source '{alias}'.\n"
            f"Tables available: {names}\n"
            f"NEXT STEP: check the schema. Tables outside 'public' need it "
            f"given explicitly."
        ) from exc


def load_table(
    con: duckdb.DuckDBPyConnection,
    alias: str,
    table: str,
    dataset_name: str | None = None,
    schema: str = "public",
    where: str | None = None,
    limit: int | None = None,
    db_alias: str | None = None,
    replace: bool = True,
) -> LoadResult:
    """Copy a Postgres table into the workspace and register it.

    Query in place where inspection is all you need -- attach() then
    `SELECT ... FROM pg.public.orders` copies nothing and has no size limit.
    Copy when the data will be profiled, cleaned, joined or ANALYSED: every
    analysis goes through require_contract, which only sees loaded datasets.

    where   Optional SQL predicate, applied on the Postgres side so only
            matching rows cross the wire.
    limit   Optional row cap. Applied after `where`.
    """
    dataset_name = dataset_name or table
    validate_dataset_name(dataset_name)
    db_alias = attach(con, alias, db_alias)

    n = row_count(con, alias, table, schema, db_alias)
    effective = min(n, limit) if limit else n

    if effective > ROW_REFUSE and not limit:
        raise LoadRefused(
            f"BLOCKED: {schema}.{table} has {n:,} rows, over the "
            f"{ROW_REFUSE:,} row limit for copying into the workspace.\n"
            f"WHY: a copy of that size would fill memory and the workspace "
            f"file.\n"
            f"NEXT STEP: narrow it with where=... or limit=... . The table is "
            f"already attached as {db_alias}.{schema}.{table} and can be "
            f"previewed and described in place, but an attached table cannot be "
            f"analysed -- require_contract only sees loaded datasets (P9-O4)."
        )

    source = f'{db_alias}."{schema}"."{table}"'
    sql = f'SELECT * FROM {source}'
    if where:
        sql += f" WHERE {where}"
    if limit:
        sql += f" LIMIT {int(limit)}"

    verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"
    try:
        con.execute(f'{verb} "{dataset_name}" AS {sql}')
    except duckdb.Error as exc:
        raise LoadRefused(
            f"BLOCKED: could not copy {schema}.{table} from '{alias}'.\n"
            f"DuckDB said: {exc}\n"
            f"NEXT STEP: if you passed where=..., check it is valid Postgres "
            f"SQL against this table."
        ) from exc

    utc = db.utc_timestamps(con, dataset_name)
    rows, cols = db.table_shape(con, dataset_name)
    columns = [
        (r[0], r[1])
        for r in con.execute(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema='main' AND table_name=?
               ORDER BY ordinal_position""",
            [dataset_name],
        ).fetchall()
    ]

    gate_message = ""
    if rows > ROW_WARN:
        gate_message = (
            f"{rows:,} rows copied into the workspace. Queries will be fast, "
            f"but the workspace file is now sizeable; reset_workspace clears it."
        )

    db.register_dataset(
        con,
        dataset_name=dataset_name,
        source_type="postgres",
        source_detail=f"{alias}:{schema}.{table}",
        row_count=rows,
        column_count=cols,
        # source_rows lets DatasetRecord.narrowing() say "1,000 of 1,000,163" (C95).
        notes=f"where={where or '-'}, limit={limit or '-'}, source_rows={n}",
    )

    return LoadResult(
        dataset_name=dataset_name,
        row_count=rows,
        column_count=cols,
        columns=columns,
        gate_verdict="WARN" if gate_message else "OK",
        gate_message=gate_message,
        notes=db.utc_note(utc),
    )
