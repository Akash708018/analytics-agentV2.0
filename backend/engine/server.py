"""MCP server: every tool the agent exposes to Claude Desktop.

Phase 2, Step 8; extended in Phase 3, Step 6 and Phase 4, Step 7. Full
re-delivery.

The Phase 1 and Phase 2 tools are unchanged in behaviour. Phase 3 adds two:

  propose_ingest_spec   read a file's top and tail, work out where the header
                        is, and hand back a draft spec with every assumption
                        written down and nothing loaded.
  confirm_ingest_spec   take that spec back -- edited or not -- and run it.

Phase 4 adds four more, and the same shape a second time:

  propose_dataset_contract  work out what the data can show about a loaded
                            dataset, ask about everything it cannot, and store
                            nothing.
  confirm_dataset_contract  take the agreed contract back and store it,
                            append-only.
  get_workflow_state        every dataset, when it arrived, and what to call
                            next.
  run_analysis              the gate. Locked decision 12 lives here: no
                            analysis without a confirmed contract.

Phase 5 adds three, and they are ungated: profiling is how a person
finds out what a contract should say, so requiring one first inverts
the workflow.

Phase 8 adds one, and it is gated for the same reason run_analysis is:

  compute_analysis      run one of the twenty-one analyses over a dataset,
                        under the contract in force, and write what it
                        found where it can be read back.

  profile_dataset       count everything about a loaded table and write
                        the counts where they can be read back.
  profile_column        one column in detail: values, distribution,
                        lengths, calendar coverage.
  read_result_file      read a page of a result a tool wrote earlier.

Those two are the conversation. The primitive load_csv / load_excel stay, so a
caller who already knows the shape of a file can skip the exchange entirely.

There is no server-side draft store. The proposal comes back as JSON and goes
out as JSON, which means the spec that gets confirmed is the spec that runs,
and any field can be edited in between. A draft held in server memory would
drift from what the user was shown the moment they asked to change something.

Tool annotations (guide 8.1, and the mcp-builder guidance): each tool declares
whether it only reads, whether it can destroy data, and whether calling it twice
is the same as calling it once. Claude Desktop uses these to decide what needs
confirming, so reset_workspace is marked destructive and every describe_/list_
tool is marked read-only. propose_ingest_spec is read-only: it proposes and
loads nothing.

Every tool returns a string. Refusals carry BLOCKED / WHY / NEXT STEP per
Section 8.2, so a failure tells the user what to do rather than what went wrong
internally.

No logic lives in this file. Every tool calls one function and returns what it
gets back.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastmcp import FastMCP

from .config import (
    DEFAULT_WORKSPACE_ID,
    MAX_EXCEL_MB,
    SERVER_NAME,
    SERVER_VERSION,
    WARN_CSV_MB,
    describe_size_gates,
)
from . import workspace
from .util import db
from .util.formatting import MAX_ROWS, format_kv
from .ingest import csv_loader, draft, excel, postgres, sizegate, merges
from .ingest.csv_loader import LoadRefused
from .contract import tools as contract_tools
from .analysis import tools as analysis_tools
from .profile import tools as profile_tools
from .clean import tools as clean_tools
from .report import tools as report_tools
from .validate import tools as validate_tools

mcp = FastMCP(SERVER_NAME)

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
READ_EXTERNAL = {"readOnlyHint": True, "openWorldHint": True}
WRITES = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}


def _unusable_path(path: str) -> str | None:
    """A refusal for a path the operating system cannot even look up, else None.

    A NUL byte raised ValueError and a 5,000-character name raised OSError out of check_file,
    preview_file and read_result_file (P14-D72): a tool answers with text, never an exception.
    A path that merely does not exist is not this function's business -- each tool says so.
    """
    try:
        if "\x00" in path:
            raise ValueError("it contains a NUL byte")
        os.stat(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        shown = path.replace("\x00", "\\0")
        return (f"BLOCKED: that is not a usable path ({shown[:80]!r}"
                f"{'...' if len(shown) > 80 else ''}).\n"
                f"WHY: {getattr(exc, 'strerror', None) or exc}.\n"
                f"NEXT STEP: pass the full path of an existing .csv or .xlsx file.")
    return None


def _refusal(exc: Exception) -> str:
    """Refusals are the user's to read, not a traceback."""
    return str(exc)


# ---------------------------------------------------------------------------
# Phase 1 tools, unchanged
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def ping() -> str:
    """Confirm the analytics agent is running and show its configuration."""
    return format_kv([
        ("server", SERVER_NAME),
        ("version", SERVER_VERSION),
        ("status", "alive"),
        ("workspace", DEFAULT_WORKSPACE_ID),
        ("workspace path", workspace.workspace_dir()),
        ("max excel MB", MAX_EXCEL_MB),
        ("warn csv MB", WARN_CSV_MB),
    ])


@mcp.tool(annotations=DESTRUCTIVE)
def reset_workspace(confirm: bool = False, workspace_id: str | None = None) -> str:
    """Delete everything loaded in the workspace. Cannot be undone.

    Requires confirm=True. Ask the user before calling with confirm=True.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    if not confirm:
        return (
            "BLOCKED: reset_workspace deletes all loaded data and cannot be "
            "undone.\n\n"
            "NEXT STEP: ask the user to confirm, then call "
            "reset_workspace(confirm=True)."
        )
    info = workspace.reset(wid)
    # A workspace other than the default exports its contracts to docs/contracts/<id>/, and
    # those outlived the data they describe (P14-O26). The default workspace's exports are the
    # version-controlled docs/contracts/<dataset>.yaml and stay.
    exports = 0
    if wid != DEFAULT_WORKSPACE_ID:
        root = contract_tools.workspace_export_root(wid)
        if root.is_dir():
            exports = sum(1 for p in root.rglob("*") if p.is_file())
            shutil.rmtree(root)
    note = f" Removed its {exports} exported contract file(s) too." if exports else ""
    if not info["existed"]:
        return f"Workspace '{wid}' did not exist. Nothing to remove.{note}"
    return (
        f"Workspace '{wid}' reset. Removed {info['removed_files']} file(s), "
        f"{info['bytes'] / 1024 / 1024:.1f} MB.{note}"
    )


# ---------------------------------------------------------------------------
# Inspecting before loading
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def check_file(path: str) -> str:
    """Check a file's size against the load limits WITHOUT reading it.

    Call this first when a file might be large. Reports OK, a warning, or a
    refusal explaining what to do instead.
    """
    if (unusable := _unusable_path(path)):
        return unusable
    result = sizegate.check_file(path)
    if result.verdict.value == "OK":
        return f"{Path(path).name}: within limits. Safe to load."
    return f"[{result.verdict.value}] {result.message}"


@mcp.tool(annotations=READ_ONLY)
def preview_file(path: str, sheet: str | None = None, lines: int = 15) -> str:
    """Show the first lines of a CSV or the first rows of an Excel sheet.

    Reads only the top of the file, never the whole thing, so this is safe on a
    file of any size. Use it to see the raw shape of a file.

    For a messy file, prefer propose_ingest_spec: it reads the same rows and
    works out what they mean.
    """
    if (unusable := _unusable_path(path)):
        return unusable
    p = Path(path)
    if not p.exists():
        return f"BLOCKED: no file at {path}.\nNEXT STEP: check the path."

    try:
        if sizegate.source_type_for(p) == "excel":
            sheets = excel.list_sheets(p)
            rows = excel.preview_rows(p, sheet, n=lines)
            head = f"{p.name} - sheets: {', '.join(sheets)}\n\n"
            body = "\n".join(f"row {i}: {r}" for i, r in enumerate(rows, 1))
            merged = merges.merged_ranges(p, sheet or merges.active_sheet_name(p))
            if merged:
                body += f"\n\nMerged cells: {', '.join(merged)}"
            return head + body
        out = csv_loader.preview_lines(p, n=lines)
        return f"{p.name} - first {len(out)} lines\n\n" + "\n".join(
            f"{i}: {line}" for i, line in enumerate(out, 1)
        )
    except Exception as exc:
        return f"BLOCKED: could not preview {p.name}.\nDetail: {exc}"


# ---------------------------------------------------------------------------
# The ingest conversation
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def propose_ingest_spec(
    path: str,
    dataset_name: str | None = None,
    sheet: str | None = None,
    header_rows: list[int] | None = None,
    header_join: str | None = None,
    authorised_fill: bool = False,
) -> str:
    """Work out how to read a messy file, and propose it. Loads nothing.

    Use this for any file that is not a plain one-header-row table: stacked
    headers, merged cells, a title above the data, notes below it.

    Returns a readable spec, the reasoning behind every choice, and the same
    spec as JSON. Show the user the reasoning. If they want a change, edit the
    JSON and pass it to confirm_ingest_spec -- the spec you send is the spec
    that runs.

    When the file cannot settle where the header is, the spec comes back marked
    PROVISIONAL with questions attached, and confirm_ingest_spec will refuse
    it. Put those questions to the user. Do not guess an answer on their
    behalf: a CSV with a sparse first header row cannot be resolved from the
    file alone, which is exactly why it is being asked about.

    Once they answer, call this tool AGAIN with their answer rather than
    building a load by hand:

      header_rows      the rows that form the header, e.g. [1, 2] or [2]
      header_join      space | underscore | bottom_only | top_only
      authorised_fill  true when the user says an upper CSV row holds group
                       labels spanning columns. Only a person can say this;
                       the file cannot.

    The spec that comes back has nothing outstanding and can be confirmed.
    """
    if (unusable := _unusable_path(path)):
        return unusable
    try:
        d = draft.draft_for_path(
            path, dataset_name=dataset_name, sheet=sheet,
            header_rows=header_rows, header_join=header_join,
            authorised_fill=authorised_fill,
        )
        return draft.render(d)
    except LoadRefused as exc:
        return _refusal(exc)
    except Exception as exc:
        return (
            f"BLOCKED: could not read {Path(path).name} well enough to propose "
            f"a spec.\n"
            f"Detail: {exc}\n"
            f"NEXT STEP: call preview_file to see the raw rows."
        )


@mcp.tool(annotations=WRITES)
def confirm_ingest_spec(spec_json: str, workspace_id: str | None = None) -> str:
    """Load a file exactly as a confirmed ingest spec describes.

    spec_json  The JSON from propose_ingest_spec, with any edits the user
               asked for. Every field is honoured as written.

    Only call this once the user has seen the spec and agreed to it. The point
    of the two-step is that the shape of the load is visible before the data
    moves, so loading a spec the user has not read defeats it.

    A spec marked PROVISIONAL is refused. Answer its questions and call
    propose_ingest_spec again rather than deleting the unresolved field.

    Coercion failures are reported per column. A value that will not fit its
    column is stored as NULL and counted, never dropped in silence.
    """
    try:
        spec = draft.spec_from_json(spec_json)
    except LoadRefused as exc:
        return _refusal(exc)

    if not spec.is_confirmable:
        return spec.blocking_message()

    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        loader = excel.load_excel if spec.source_type == "excel" else csv_loader.load_csv
        result = loader(con, spec.path, **spec.to_loader_kwargs(loader))
        return result.summary()
    except LoadRefused as exc:
        return _refusal(exc)
    except Exception as exc:
        return (
            f"BLOCKED: the spec was valid but the load failed.\n"
            f"Detail: {exc}\n"
            f"NEXT STEP: call propose_ingest_spec again -- the file may not be "
            f"the one the spec was written for."
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Loading directly
# ---------------------------------------------------------------------------


@mcp.tool(annotations=WRITES)
def load_csv(
    path: str,
    dataset_name: str,
    header_rows: int = 1,
    names: list[str] | None = None,
    delimiter: str | None = None,
    na_values: list[str] | None = None,
    footer_skip_rows: int = 0,
    dtypes: dict[str, str] | None = None,
    on_error: str = "stop",
    workspace_id: str | None = None,
) -> str:
    """Load a CSV or other delimited file into the workspace.

    For a messy file, propose_ingest_spec works these arguments out and shows
    them before anything is loaded. Use this directly when the shape is known.

    header_rows       Rows at the top that are headers. Use 2+ for stacked
                      headers, and then names must be supplied.
    names             Explicit column names, one per column. Required when
                      header_rows is not 1.
    na_values         Tokens to read as NULL, e.g. ['N/A', '-'].
    footer_skip_rows  Data rows at the END to drop -- a totals line, a
                      'generated on' note.
    dtypes            Pin a column's type, e.g. {'order_id': 'VARCHAR'}.
    on_error          'stop' refuses at the first value that does not fit its
                      column. 'null' stores that cell as NULL, keeps the row,
                      and reports the count per column.
    """
    if (unusable := _unusable_path(path)):
        return unusable
    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        r = csv_loader.load_csv(
            con, path, dataset_name,
            header_rows=header_rows, names=names, delimiter=delimiter,
            na_values=na_values, footer_skip_rows=footer_skip_rows,
            dtypes=dtypes, on_error=on_error,
        )
        return r.summary()
    except LoadRefused as exc:
        return _refusal(exc)
    finally:
        con.close()


@mcp.tool(annotations=WRITES)
def load_excel(
    path: str,
    dataset_name: str,
    sheet: str | None = None,
    header_rows: int = 1,
    names: list[str] | None = None,
    na_values: list[str] | None = None,
    footer_skip_rows: int = 0,
    dtypes: dict[str, str] | None = None,
    on_error: str = "stop",
    all_text: bool = False,
    workspace_id: str | None = None,
) -> str:
    """Load one worksheet from an Excel file into the workspace.

    For a messy workbook, propose_ingest_spec works these arguments out and
    shows them before anything is loaded.

    na_values         Tokens to read as NULL. Applied before types are
                      inferred, so a number column containing 'N/A' stays a
                      number column.
    footer_skip_rows  Data rows at the END to drop.
    dtypes            Pin a column's type, e.g. {'order_date': 'DATE'}. Dates
                      written into a sheet as text stay text otherwise --
                      inference reads a string as a string and never looks
                      inside it. A pinned DATE or TIMESTAMP column parses an
                      ISO string such as '2024-01-22', and an empty cell stays
                      NULL rather than failing, so pinning a date column costs
                      nothing even with on_error='stop'. Prefer DATE for a
                      date-only column: TIMESTAMP invents a midnight component
                      that shows up in every later group-by.
    on_error          'stop' refuses at the first value that does not fit its
                      column. 'null' stores that cell as NULL and counts it.
    all_text          Load every column as text. The blunt version of
                      on_error='null'.
    """
    if (unusable := _unusable_path(path)):
        return unusable
    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        r = excel.load_excel(
            con, path, dataset_name,
            sheet=sheet, header_rows=header_rows, names=names,
            na_values=na_values, footer_skip_rows=footer_skip_rows,
            dtypes=dtypes, on_error=on_error, all_text=all_text,
        )
        return r.summary()
    except LoadRefused as exc:
        return _refusal(exc)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def list_sources() -> str:
    """List configured database sources. Connection strings are redacted."""
    return postgres.list_sources()


@mcp.tool(annotations=READ_EXTERNAL)
def describe_source(alias: str, workspace_id: str | None = None) -> str:
    """List the tables and views in a configured database, with row counts.

    Copies nothing. The connection is always read-only.
    """
    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        return postgres.describe_source(con, alias)
    except LoadRefused as exc:
        return _refusal(exc)
    finally:
        con.close()


@mcp.tool(annotations=WRITES)
def load_postgres_table(
    alias: str,
    table: str,
    dataset_name: str | None = None,
    schema: str = "public",
    where: str | None = None,
    limit: int | None = None,
    workspace_id: str | None = None,
) -> str:
    """Copy a table from a configured database into the workspace.

    Prefer query_source for exploring -- it copies nothing and has no size
    limit. Copy when the data will be cleaned or joined repeatedly.

    where  SQL predicate applied on the database side, so only matching rows
           are transferred.
    """
    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        r = postgres.load_table(
            con, alias, table, dataset_name,
            schema=schema, where=where, limit=limit,
        )
        return r.summary()
    except LoadRefused as exc:
        return _refusal(exc)
    finally:
        con.close()


@mcp.tool(annotations=READ_EXTERNAL)
def query_source(alias: str, sql: str, workspace_id: str | None = None) -> str:
    """Run a read-only SELECT against a configured database without copying it.

    Reference tables as <alias>.<schema>.<table>, for example
    olist.public.orders. Writes are rejected by the connection itself.
    """
    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        postgres.attach(con, alias)
        rows = con.execute(sql).fetchmany(MAX_ROWS)
        names = [d[0] for d in con.description]
        if not rows:
            return "Query returned no rows."
        lines = ["| " + " | ".join(names) + " |",
                 "| " + " | ".join("---" for _ in names) + " |"]
        lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in rows]
        if len(rows) == MAX_ROWS:
            lines.append(f"\n(first {MAX_ROWS} rows)")
        return "\n".join(lines)
    except LoadRefused as exc:
        return _refusal(exc)
    except Exception as exc:
        return (f"BLOCKED: query failed.\nDetail: {exc}\n"
                f"NEXT STEP: reference tables as {alias}.schema.table.")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Workspace state
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def list_datasets(workspace_id: str | None = None) -> str:
    """Show every dataset loaded in the workspace and when it was loaded.

    Load times matter: this server stays running between chats, so a dataset
    may be left over from an earlier conversation.
    """
    return db.workspace_summary(workspace_id or DEFAULT_WORKSPACE_ID)


@mcp.tool(annotations=READ_ONLY)
def describe_dataset(
    dataset_name: str,
    workspace_id: str | None = None,
    sample_rows: int = 5,
) -> str:
    """Describe a loaded dataset: shape, columns, types, nulls, and a sample.

    This is a structural description, not a full profile. Profiling with
    distributions and outliers comes later in the workflow.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    con = db.connect(wid)
    try:
        if dataset_name not in db.user_tables(con):
            available = ", ".join(db.user_tables(con)) or "(none loaded)"
            return (
                f"BLOCKED: no dataset called '{dataset_name}' in workspace "
                f"'{wid}'.\n"
                f"Loaded: {available}\n"
                f"NEXT STEP: call list_datasets to see what is available."
            )

        rows, ncols = db.table_shape(con, dataset_name)
        record = db.get_dataset(con, dataset_name)

        header = [f"# {dataset_name}", "",
                  f"{rows:,} rows, {ncols} columns"]
        if record:
            header.append(
                f"Source: {record.source_type} - {record.source_detail}"
            )
            header.append(record.age_phrase())
        else:
            header.append(
                "NOTE: no load record. This table was not created by a loader."
            )

        cols = con.execute(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema='main' AND table_name=?
               ORDER BY ordinal_position""",
            [dataset_name],
        ).fetchall()

        body = ["", "| column | type | nulls | distinct |",
                "| --- | --- | --- | --- |"]
        for name, dtype in cols:
            n_null, n_distinct = con.execute(
                f'SELECT count(*) FILTER (WHERE "{name}" IS NULL), '
                f'count(DISTINCT "{name}") FROM "{dataset_name}"'
            ).fetchone()
            pct = f" ({n_null / rows * 100:.1f}%)" if rows and n_null else ""
            body.append(
                f"| {name} | {dtype} | {n_null:,}{pct} | {n_distinct:,} |"
            )

        sample = con.execute(
            f'SELECT * FROM "{dataset_name}" LIMIT {int(sample_rows)}'
        ).fetchall()
        names = [c[0] for c in cols]
        body += ["", f"Sample ({len(sample)} rows)", "",
                 "| " + " | ".join(names) + " |",
                 "| " + " | ".join("---" for _ in names) + " |"]
        body += ["| " + " | ".join(str(v) for v in r) + " |" for r in sample]

        return "\n".join(header + body)
    finally:
        con.close()


@mcp.tool(annotations=READ_ONLY)
def show_limits() -> str:
    """Show the size limits that apply to loading on this machine."""
    return describe_size_gates()


# ---------------------------------------------------------------------------
# The Dataset Contract
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def propose_dataset_contract(
    dataset_name: str,
    grain: str | None = None,
    primary_key: list[str] | None = None,
    date_column: str | None = None,
    measures: list[str] | None = None,
    dimensions: list[str] | None = None,
    measure_definitions: dict[str, str] | None = None,
    aggregations: dict[str, str] | None = None,
    analysis_window_start: str | None = None,
    analysis_window_end: str | None = None,
    known_exclusions: list[dict] | None = None,
    caveats: list[str] | None = None,
    foreign_keys: list[dict] | None = None,
    domains: dict[str, list[str]] | None = None,
    expectations: list[dict] | None = None,
    measure_columns: dict[str, str] | None = None,
    measure_per: dict[str, list[str]] | None = None,
    ratios: dict[str, dict] | None = None,
    workspace_id: str | None = None,
) -> str:
    """Draft a Dataset Contract for a loaded dataset. Stores nothing.

    A contract is what the agent is allowed to assume about a dataset: what one
    row is, what each number means, which rows were left out on purpose. No
    analysis runs without one.

    The draft fills in only what the data can show -- the key, the roles, the
    span of the date column -- and comes back marked PROVISIONAL for everything
    it cannot. Show the whole thing to the user, including the reasoning.

    **Do not answer the questions on the user's behalf.** The grain is the
    sentence every later number depends on and nothing in the data states it;
    a definition like "net of tax, excludes cancelled" cannot be derived from a
    column of numbers. A guessed grain reads as knowledge and will be believed.

    Once the user answers, call this tool AGAIN with their words rather than
    editing the JSON -- the questions are about these fields, so an edited
    document disagrees with itself:

      grain="one row = one item on one order"
      measure_definitions={"price": "item price, excludes freight"}
      primary_key=["order_id", "order_item_id"]   verified, not trusted
      aggregations={"price": "mean"}              where sum is wrong
      analysis_window_start="2024-01-01"
      analysis_window_end="2024-09-30"            both ends or neither
      known_exclusions=[{"rule": "status = 'cancelled'",
                         "reason": "not real revenue"}]
      expectations=[{"rule": "units > 0",
                     "reason": "a line sells at least one item"}]
                                                  every row must satisfy it;
                                                  validate_dataset counts those
                                                  that do not
      measure_per={"order_shipping_fee": ["order_id"]}  a value repeated on every
                                                  line of an order is one value
                                                  per order; statistics over it
                                                  are taken over orders
      measure_columns={"returned_rate": "is_returned"}  a measure reading another
                                                  column (a true/false column
                                                  reads as 0/1, so mean = rate)
      ratios={"gross_margin_pct": {"numerator": ["line_revenue", "-line_cost"],
              "denominator": ["line_revenue"], "scale": 100}}  a ratio of sums,
                                                  never a mean of ratios

    The contract that comes back with nothing unresolved is the one to confirm.
    Deleting an entry from `unresolved` does not work: the blank it was
    declaring is invalid on its own, so the edit is refused rather than stored.
    """
    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        return contract_tools.propose(
            con, dataset_name,
            grain=grain, primary_key=primary_key, date_column=date_column,
            measures=measures, dimensions=dimensions,
            measure_definitions=measure_definitions, aggregations=aggregations,
            analysis_window_start=analysis_window_start,
            analysis_window_end=analysis_window_end,
            known_exclusions=known_exclusions, caveats=caveats,
            foreign_keys=foreign_keys, domains=domains, expectations=expectations,
            measure_columns=measure_columns, measure_per=measure_per, ratios=ratios,
        )
    finally:
        con.close()


@mcp.tool(annotations=WRITES)
def confirm_dataset_contract(
    contract_json: str, workspace_id: str | None = None
) -> str:
    """Store a Dataset Contract the user has agreed to.

    contract_json  The JSON from propose_dataset_contract. Every field is
                   honoured as written.

    Only call this once the user has read the contract and agreed to it. The
    whole point of the two-step is that the definitions every later number
    rests on are visible before anything is built on them.

    A contract still marked PROVISIONAL is refused. Answer its questions and
    call propose_dataset_contract again.

    Storing is append-only. A new version supersedes the previous one rather
    than replacing it, so what was agreed during any past window stays
    recoverable, and a copy is written to docs/contracts/ for version control --
    docs/contracts/<workspace_id>/ for any workspace but the default, so two
    workspaces with a dataset of the same name keep their own copy.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    con = db.connect(wid)
    try:
        return contract_tools.confirm(con, contract_json, workspace_id=wid)
    finally:
        con.close()


@mcp.tool(annotations=READ_ONLY)
def get_workflow_state(workspace_id: str | None = None) -> str:
    """Show every dataset in the workspace and what has to happen next.

    Call this when you are unsure what has already been done, when a tool
    refuses and you want the whole picture, or at the start of a conversation
    that continues earlier work.

    This server keeps running between chats, so a dataset loaded in another
    conversation is still here. Each one is listed with when it arrived, so a
    leftover from earlier is visible rather than mistaken for something fresh.

    For every dataset: rows, columns, whether a contract is in force and which
    version, whether the table has changed since that contract was agreed, and
    the exact call to make next.
    """
    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        return contract_tools.workflow_state(con)
    finally:
        con.close()


@mcp.tool(annotations=READ_ONLY)
def run_analysis(
    dataset_name: str,
    question: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Check what may be computed for a dataset, under its Dataset Contract.

    Every analysis passes through here first, and there is no path around it:
    without a confirmed contract there is no agreed grain, so nothing can say
    whether summing a column double-counts.

    If it refuses, it names the exact call that unblocks it -- usually
    propose_dataset_contract. Make that call rather than trying a different
    tool; nothing else will produce the number.

    This computes nothing. What it returns is the agreement in force, the
    caveats any result would have to carry, and the analyses that can be run
    against it. compute_analysis runs one of them. Do not report a computed
    answer from this tool -- it does not compute one.
    """
    con = db.connect(workspace_id or DEFAULT_WORKSPACE_ID)
    try:
        return contract_tools.analyse(con, dataset_name, question)
    finally:
        con.close()


@mcp.tool(annotations=READ_ONLY)
def compute_analysis(
    dataset_name: str,
    analysis_type: str,
    column: str | None = None,
    dimension: str | None = None,
    measure: str | None = None,
    against: str | None = None,
    rows: str | None = None,
    columns: str | None = None,
    limit: int | None = None,
    n: int | None = None,
    bins: int | None = None,
    threshold: float | None = None,
    before_start: str | None = None,
    before_end: str | None = None,
    after_start: str | None = None,
    after_end: str | None = None,
    period: str | None = None,
    baseline: str | None = None,
    grain: str | None = None,
    second_dimension: str | None = None,
    method: str | None = None,
    entity: str | None = None,
    event: str | None = None,
    groups: list[str] | None = None,
    where: str | None = None,
    confidence: float | None = None,
    alpha: float | None = None,
    power: float | None = None,
    workspace_id: str | None = None,
) -> str:
    """Run one named analysis over a dataset, under the contract in force.

    run_analysis lists what can be computed for a dataset and computes nothing.
    This computes one of them. An analysis_type nobody registered comes back
    with the full list of valid names, so a wrong guess costs one call.

    Which arguments apply depends on analysis_type. Passing one that does not
    apply WITH A VALUE is refused rather than ignored -- every analysis raises
    on an unexpected keyword. Passing it as None is not refused: tools.py
    strips None before dispatch, so bins=10 to calendar_coverage comes back as
    a refusal and bins=None is accepted silently. The behaviour is right and
    the shorter sentence oversold it (P9-O3):

      summary_stats   nothing -- every declared measure at once
      distribution    measure, bins
      frequency       column, limit
      cross_tab       rows, columns, and optionally measure
      top_n           dimension, measure, n; period and grain optional
      group_compare   dimension, measure; groups optional
      pareto          dimension, measure, threshold; period and grain optional
      concentration   dimension, measure; period and grain optional. No group
                      cap for these two: the answer is a count or a few cuts. With
                      period ("2025-11", or "2025-Q4" with grain="quarter"),
                      these three rank only that period's rows -- which
                      orders drive a month.
      ranking_shift   dimension, measure, and four ISO dates:
                      before_start, before_end, after_start, after_end
      trend           measure, and grain as below; dimension optional.
                      One measure per period, with the periods holding
                      no rows blank rather than zero and the gaps named.
                      With dimension, one column per member of that
                      declared dimension, then (all) and rows.
      seasonality     measure, and grain as below except year. One
                      measure folded onto the positions of its cycle,
                      each position's mean over the periods that hold
                      rows and the periods it lost counted beside it.
      correlated_shift  measure, against and grain. Whether two
                      measures change level at the same point in the
                      calendar, with the rate at which unrelated
                      series coincide by chance beside the verdict.
      changepoint     measure and grain. Where the measure changes
                      level across the calendar, with every
                      admissible split reported and the splits a gap
                      could explain excluded rather than caveated.
      outlier_detection  measure; dimension optional, for fences within
                      each group. Unusual values by three methods at
                      once -- Tukey's fence, the z-score and the
                      median absolute deviation -- with their bounds
                      and the masking that makes the z-score miss.
      mix_shift       measure, dimension, period, baseline and grain.
                      A change in the measure's per-row average split
                      into rate, mix and the interaction between
                      them, which is reported and not folded away.
      driver_analysis  measure. Every declared dimension ranked by
                      how much of that measure's variation it
                      accounts for, against what a grouping of the
                      same shape would account for by chance.
      bivariate       measure, against and bins. How against behaves
                      across the range of measure, binned by value
                      rather than by row, with the turns counted.
      correlation     measure and against, both declared measures.
                      Pearson and Spearman over the rows holding
                      both values, with the pair count and the gap
                      between the two coefficients named.
      growth_decomposition  measure, dimension, period, baseline and
                      grain as below. The change in one measure
                      between two periods, split across a dimension,
                      with the contributions summing to the whole.
      period_compare  measure, period, baseline, and grain as below.
                      One measure in two named periods, with the
                      difference between them and no change computed
                      from a period that holds no rows.
      calendar_coverage  grain: day, week, month, quarter or year.
                      Defaults to month. Which periods hold rows and
                      which hold none -- a period with no rows cannot
                      appear in a GROUP BY, so a trend drawn over this
                      column crosses absent periods without saying so.
      where="..."     on any analysis: only the rows this is true for, e.g.
                      where="lower(trim(delivery_status)) = 'delivered'". The
                      text after WHERE, on this table's columns; no subquery.
                      The rows it leaves out are counted in the result.
      groups=[...]    on group_compare, hypothesis_test, effect_size,
                      confidence_interval, sample_adequacy: keep only
                      those members of dimension, e.g. Store and Online.
      hypothesis_test  dimension, and either measure or second_dimension,
                      plus method (auto, parametric or rank). Whether the
                      groups differ by more than sampling alone would
                      produce, naming the test it ran, its variant and its
                      df. Welch for two groups, one-way ANOVA for more,
                      chi-square for two dimensions, and a rank test where
                      a group has one row and therefore no variance.
      confidence_interval  measure or dimension, and confidence. The range
                      a mean or a share is consistent with, given how many
                      rows produced it. Student's t around a mean, Wilson
                      around a share, with the width stated.
      effect_size     dimension, and either measure or second_dimension.
                      How large a difference is in units that do not grow
                      with the row count -- Hedges' g between two groups,
                      eta squared across more, Cramer's V between two
                      dimensions -- banded by Cohen's conventions and said
                      to be conventions.
      sample_adequacy  dimension, measure, power and alpha. The smallest
                      difference these row counts could reliably detect,
                      in standard deviations and in the measure's units,
                      beside the difference actually there. Never observed
                      power, which restates the p-value.
      cohort_retention  entity and period. How many people from each
                      starting period came back in each later one, as a
                      grid of people rather than percentages with the
                      cohort's size beside its label. Warns and points at
                      repeat_behaviour when too few return for the shape
                      to mean anything.
      repeat_behaviour  entity; event optional (e.g. order_id, so a person's
                      events are orders, not rows). How many people appear once and how many
                      come back, how often and how long they take. Both
                      refuse an entity that is distinct per row, which
                      describes events rather than people.

    Only columns the contract declares can be named. A column that exists in
    the table but is not a declared dimension or measure is refused with the
    declared list, because the contract is what says summing a column does not
    double-count.

    The full table is written to a file and the first rows come back inline
    with a note saying what was computed over and what was left out. Read the
    rest with read_result_file before describing it.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    con = db.connect(wid)
    try:
        return analysis_tools.compute_analysis(
            con, wid, dataset_name, analysis_type,
            column=column, dimension=dimension, measure=measure,
            against=against, rows=rows, columns=columns, limit=limit, n=n,
            bins=bins, threshold=threshold, before_start=before_start,
            before_end=before_end, after_start=after_start,
            after_end=after_end, period=period, baseline=baseline, grain=grain,
            second_dimension=second_dimension, method=method, entity=entity,
            event=event, groups=groups, where=where,
            confidence=confidence, alpha=alpha, power=power,
        )
    finally:
        con.close()


@mcp.tool(annotations=WRITES)
def propose_metric(
    dataset_name: str,
    name: str,
    left: str,
    op: str,
    definition: str,
    right: str | None = None,
    value: float | None = None,
    agg: str = "mean",
    workspace_id: str | None = None,
) -> str:
    """Propose a yes/no metric the contract lacks; a person approves it before it is used.

    For a question that needs a yes/no per row the table does not hold -- an SLA breach is
    left="recorded_delivery_minutes", op=">", right="promised_minutes" -- rather than refusing.
    op is one of > >= < <= = <>; right names a column, or value gives a number instead. agg
    "mean" makes it a rate, "sum" a count. The engine checks both sides are numbers (or both
    dates) and counts the rows it can judge. Nothing is computed with it until the person
    approves it (on the Ask screen, or decide_metric); then compute_analysis takes it as a
    measure, and every result says it is provisional and not in the contract.
    """
    from backend.engine.contract import provisional
    from backend.engine.contract import store

    wid = workspace_id or DEFAULT_WORKSPACE_ID
    con = db.connect(wid)
    try:
        stored = store.current(con, dataset_name)
        contract = stored.contract if stored else None
        try:
            p = provisional.propose(con, wid, dataset_name, name=name, left=left, op=op,
                                    right=right, value=value, agg=agg, definition=definition,
                                    contract=contract)
        except provisional.ProposalError as exc:
            return (f"BLOCKED: the metric {name} cannot be proposed.\nWHY: {exc}\n"
                    f"NEXT STEP: propose it again with columns of {dataset_name}.\n\n"
                    f"reason: ANALYSIS_NOT_POSSIBLE")
    finally:
        con.close()
    return (f"PROPOSED, waiting for the person's approval (id {p.id}):\n"
            f"{provisional.describe(p)}\n\n"
            f"Nothing has been computed with it. Tell the person what you proposed and why; "
            f"they approve or reject it with the buttons under your answer on the Ask screen. "
            f"Once approved, pass measure=\"{name}\" to compute_analysis.")


@mcp.tool(annotations=WRITES)
def decide_metric(proposal_id: str, approve: bool, workspace_id: str | None = None) -> str:
    """Approve or reject a proposed metric, as the person decided.

    Call it only when the person has said which; the web assistant is not given this tool.
    """
    from backend.engine.contract import provisional

    wid = workspace_id or DEFAULT_WORKSPACE_ID
    try:
        p = provisional.decide(wid, proposal_id, approve)
    except provisional.ProposalError as exc:
        return f"BLOCKED: {exc}\n\nreason: ANALYSIS_NOT_POSSIBLE"
    return (f"{p.name} is {p.status}." + (f" {p.label()}" if approve else ""))


@mcp.tool(annotations=READ_ONLY)
def render_chart(
    dataset_name: str,
    analysis_type: str,
    chart: str,
    column: str | None = None,
    dimension: str | None = None,
    measure: str | None = None,
    against: str | None = None,
    rows: str | None = None,
    columns: str | None = None,
    limit: int | None = None,
    n: int | None = None,
    bins: int | None = None,
    threshold: float | None = None,
    before_start: str | None = None,
    before_end: str | None = None,
    after_start: str | None = None,
    after_end: str | None = None,
    period: str | None = None,
    baseline: str | None = None,
    grain: str | None = None,
    second_dimension: str | None = None,
    method: str | None = None,
    entity: str | None = None,
    event: str | None = None,
    groups: list[str] | None = None,
    where: str | None = None,
    confidence: float | None = None,
    alpha: float | None = None,
    power: float | None = None,
    x: str | None = None,
    y: str | None = None,
    title: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Draw one analysis as a chart, under the contract in force.

    Call this with the same arguments you would give compute_analysis, plus
    chart. The analysis runs the same way and through the same gate; a dataset
    with no confirmed contract is refused here exactly as it is there.

    You cannot see the image this writes. Do not describe it from the filename
    or from what you expected -- the reply states what was plotted, how many
    points were drawn of how many the result held, and the lowest, highest,
    first and last value of every measure with the group each falls at. Report
    those numbers. If you need the whole table, call compute_analysis.

    chart is one of:

      line          one measure across the groups in the order they came.
                    For calendar_coverage, trend, seasonality, period_compare.
      bar           one measure per group. For frequency, top_n, group_compare,
                    ranking_shift.
      grouped_bar   two or more measures side by side per group. For cross_tab,
                    period_compare, and trend with a dimension.
      scatter       the first measure against the second, one point per row.
                    For correlation and bivariate.
      histogram     one measure binned by value. For distribution.
      box           the spread of each measure. For summary_stats and
                    outlier_detection.
      heatmap       every measure shaded across the groups. For cross_tab and
                    cohort_retention.
      waterfall     one measure stacked so each bar starts where the last
                    ended. For mix_shift and growth_decomposition, where the
                    parts sum to the whole.

    line, bar, histogram and waterfall draw one measure. If the result holds
    more than one, name the one you want with y rather than letting the tool
    pick -- it will refuse and list them instead of choosing. Two things you
    already chose are honoured without y: the measure you passed is drawn
    when one column is that measure (rows is then left beside the chart),
    and a (all) or (total) column is not drawn beside the columns it totals.

    x names the column along the bottom and defaults to the result's first,
    which is the group. title defaults to the analysis and the chart kind.

    Empty cells are not drawn and the reply says how many were left out. A
    value that is not a finite number is refused rather than quietly omitted,
    because a chart missing a point it does not mention is a claim about a
    distribution that was not measured.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    con = db.connect(wid)
    try:
        return analysis_tools.render_chart(
            con, wid, dataset_name, analysis_type, chart,
            x=x, y=y, title=title,
            column=column, dimension=dimension, measure=measure,
            against=against, rows=rows, columns=columns, limit=limit, n=n,
            bins=bins, threshold=threshold, before_start=before_start,
            before_end=before_end, after_start=after_start,
            after_end=after_end, period=period, baseline=baseline, grain=grain,
            second_dimension=second_dimension, method=method, entity=entity,
            event=event, groups=groups, where=where,
            confidence=confidence, alpha=alpha, power=power,
        )
    finally:
        con.close()


@mcp.tool(annotations=READ_ONLY)
def profile_dataset(
    dataset_name: str,
    missing_values: list[str] | None = None,
    workspace_id: str | None = None,
) -> str:
    """Count everything about a loaded table: nulls, duplicates, distributions.

    Reports what is there and changes nothing. No column is cleaned, converted
    or rewritten, and a column reported as parsing to another type has NOT been
    converted -- that is a decision with consequences and it belongs to Phase 6,
    behind its own approval step.

    Values that read as missing without being null are counted separately from
    nulls, using the published pandas/pyarrow vocabulary. Pass
    missing_values=["NR"] to widen it for a source that writes something else,
    or missing_values=[] to switch the check off.

    The full table of counts is written to a file and the first rows come back
    inline. Read the rest with read_result_file before describing it.
    """
    # One workspace id, used twice: db.connect opens the data and the results
    # directory hangs off the same workspace. Resolving it once is what keeps a
    # profile and its result file in the same place.
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    con = db.connect(wid)
    try:
        return profile_tools.profile_dataset(
            con, wid, dataset_name, missing_values=missing_values
        )
    finally:
        con.close()


@mcp.tool(annotations=READ_ONLY)
def profile_column(
    dataset_name: str,
    column: str,
    top_n: int = 10,
    workspace_id: str | None = None,
) -> str:
    """Look at one column closely: its values, distribution, lengths and dates.

    The drill-down after profile_dataset says something about a column worth
    following up. Writes nothing.

    Shows the most frequent values with their shares, so read that list for
    anything that means missing without being null -- 'unknown', 'none' and '-'
    are judgement calls this tool deliberately does not make for you. Pass a
    larger top_n to see further down the list.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    con = db.connect(wid)
    try:
        return profile_tools.profile_column(
            con, wid, dataset_name, column, top_n=top_n
        )
    finally:
        con.close()


@mcp.tool(annotations=READ_ONLY)
def read_result_file(
    path: str,
    start: int = 1,
    limit: int = 50,
    start_col: int = 1,
    col_limit: int = 12,
    workspace_id: str | None = None,
) -> str:
    """Read a page of a result file written by an earlier profile_dataset call.

    Use the path exactly as it was returned by that earlier call; a path from
    anywhere else is refused. Rows are numbered from 1 and the header line is
    not counted, so start=21 gives you the twenty-first row of data.

    This is how a large result is actually seen. Columns are paged as well as
    rows: twelve come back at a time, and a wide result says how many are
    left and gives the call that returns them. A cross_tab can be fifty
    columns wide, and without start_col the ones past the twelfth are in the
    file and reachable by nothing.
    """
    if (unusable := _unusable_path(path)):
        return unusable
    # No connection: a result file is on disk, not in DuckDB.
    return profile_tools.read_result(
        workspace_id or DEFAULT_WORKSPACE_ID, path, start, limit,
        start_col=start_col, col_limit=col_limit,
    )


def main() -> None:
    mcp.run()


@mcp.tool(annotations=READ_ONLY)
def propose_cleaning_plan(
    dataset_name: str,
    missing_values: list[str] | None = None,
    workspace_id: str | None = None,
) -> str:
    """Propose changes to a loaded table. Changes nothing.

    Reads the table on a connection that cannot write, lists every change it
    could make with the exact SQL and the exact counts, and stores the list so
    the ids mean something. Nothing here is applied.

    Every proposal that would discard a value says so, with a sample. A value
    that already means missing costs nothing to convert; anything else is
    information, and losing it is a decision you make by approving it.

    Approve with apply_cleaning_plan(approved_action_ids=[...]). Anything you
    do not name is not run.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    return clean_tools.propose_cleaning_plan(
        wid, dataset_name, missing_values=missing_values
    )


@mcp.tool
def apply_cleaning_plan(
    dataset_name: str,
    approved_action_ids: list[str],
    workspace_id: str | None = None,
) -> str:
    """Run exactly the approved changes on a loaded table, or none of them.

    Takes ids from the most recent propose_cleaning_plan. An id that is not in
    that plan refuses the whole call rather than running the rest. The ids are
    applied in the order you give them, not in the order they were proposed. If
    the table has changed since the plan was made, this refuses and asks for a
    new proposal -- the plan is old, not wrong.

    The table keeps its name and its previous contents are kept alongside it,
    so what was there before an approved change is still readable afterwards.

    All of the approved changes are applied or none of them are.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    return clean_tools.apply_cleaning_plan(
        wid, dataset_name, approved_action_ids
    )


@mcp.tool(annotations=READ_ONLY)
def get_cleaning_ledger(
    dataset_name: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """List every cleaning change that has actually run, newest first.

    One line per applied action, each naming the table that holds the data as
    it was before that action. Nothing appears here that was not approved by
    id. Omit dataset_name for every dataset in this workspace.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    return clean_tools.get_cleaning_ledger(wid, dataset_name)


@mcp.tool(annotations=READ_ONLY)
def validate_dataset(
    dataset_name: str,
    workspace_id: str | None = None,
) -> str:
    """Check a loaded table against the contract in force for it.

    Returns a pass/fail table: the key that identifies a row, whether every
    row is dated and falls inside the analysis window, and whether the table
    still holds the row count the contract was agreed at. Every check reports
    how many rows passed, how many failed, and how many it could not examine.

    A check with nothing to test against says NOT RUN rather than passing. A
    contract that names no primary key produces a report where nothing could
    be checked, and that is not a clean bill of health.

    Reads on a connection that cannot write. Nothing here changes the table.

    Call this when run_analysis refuses, when a number looks wrong, or before
    trusting a dataset somebody else loaded. It reports every disagreement it
    finds rather than stopping at the first.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    return validate_tools.validate_dataset(wid, dataset_name)


@mcp.tool(annotations=READ_ONLY)
def build_report(
    dataset_name: str,
    question: str,
    workspace_id: str | None = None,
) -> str:
    """Write the full report for a dataset: what was asked, what was done, and how to repeat it.

    Call this at the end of a piece of work, once the analyses the user wanted have run. It
    computes nothing -- every number in it was recorded by the tool that produced it -- so
    running it twice costs nothing and running it early produces a report whose later sections
    say that nothing has happened yet.

    question is what the user actually asked, in their words. Nothing in the workspace records
    why a dataset was loaded, so this is the one part of the report that has no record behind
    it. Do not invent one, and do not paraphrase the user into something tidier than they said.

    The report has nine sections and always has nine, whatever has been done: the question, the
    dataset and its grain, data quality, the cleaning ledger, validation results, findings with
    their charts, method notes, caveats and exclusions, and a reproduction appendix listing the
    exact calls that produced every number. A section with nothing behind it appears and says
    so, because a report missing its cleaning ledger reads as a dataset that needed no cleaning.

    A dataset with no confirmed contract is still reported rather than refused. The report then
    says no grain was agreed, which is the most important thing a reader could be told about the
    numbers in it.

    You cannot read the file this writes. The reply gives the table of contents, the key
    findings, and which sections had nothing to report -- describe the work from those, not from
    the filename.
    """
    wid = workspace_id or DEFAULT_WORKSPACE_ID
    con = db.connect(wid)
    try:
        return report_tools.build_report(con, wid, dataset_name, question)
    finally:
        con.close()


if __name__ == "__main__":
    main()
