"""
The layer the profiling MCP tools call. Strings in, strings out.

Everything below this returns objects and raises exceptions. Everything above it
is FastMCP, where a raised exception becomes a traceback and a traceback is not
an instruction -- the agent reads it, learns nothing actionable, and retries the
same call. So this is where `ContractRefused` stops being an exception and
becomes text with a reason code and a next call in it.

That is the Phase 4 `contract/tools.py` shape, for the Phase 4 reason, and the
tests assert on the STRINGS because the strings are the whole interface.

Three tools, and one rule each that is not obvious:

**`profile_dataset` always writes a file** -- Step 4's decision, and it is the
reason this takes a `workspace_id` at all. The profile itself needs only a
connection; the envelope needs somewhere to put the table.

**`profile_column` never writes one.** Bounded output, and its one truncation
ends in a call rather than a path. It also computes the table profile once and
hands it down, so the column's numbers are the same numbers `profile_dataset`
reported rather than a second opinion computed with slightly different SQL.

**`read_result_file` is the reason a path may be handed out at all.** It refuses
anything resolving outside the workspace's results directory, which matters more
here than anywhere else in this package: the path arrives from a model.
"""

from __future__ import annotations

from typing import Sequence

from backend.engine.contract import ContractRefused
from backend.engine.profile.column_profile import (
    TOP_VALUES,
    profile_column as _profile_column,
    render_column,
)
from backend.engine.profile import runs
from backend.engine.profile.render import render_written_profile, write_profile
from backend.engine.profile.table_profile import (
    MISSING_VALUES,
    profile_table,
)
from backend.engine.util import results


def profile_dataset(
    con,
    workspace_id: str,
    dataset_name: str,
    *,
    missing_values: Sequence[str] | None = None,
) -> str:
    """
    Count everything about one loaded table, write the table of counts, and
    return the findings with the path and the call that reads the rest.

    `missing_values` overrides the published vocabulary for this run only --
    Frictionless' model, where the list is a property of the data rather than
    of the tool. Passing an empty list switches detection off; passing nothing
    uses the pandas/pyarrow intersection.
    """
    try:
        profile = profile_table(
            con,
            dataset_name,
            missing_values=(
                MISSING_VALUES if missing_values is None else missing_values
            ),
        )
    except ContractRefused as exc:
        return str(exc)

    result = write_profile(workspace_id, profile)
    # Recorded AFTER the write, with the path that was actually produced. A
    # record naming a file that does not exist is worse than no record: the
    # workflow state would offer a read_result_file call that refuses.
    runs.record(
        con,
        dataset_name=profile.dataset_name,
        row_count=profile.row_count,
        column_count=profile.column_count,
        result_path=str(result.path),
        duplicate_rows=profile.duplicate_rows,
        columns_missing=sum(1 for c in profile.columns if c.missing_count),
    )
    return render_written_profile(profile, result)


def profile_column(
    con,
    workspace_id: str,
    dataset_name: str,
    column: str,
    *,
    top_n: int = TOP_VALUES,
    missing_values: Sequence[str] | None = None,
) -> str:
    """
    One column in detail: what its values are, how they are distributed, how
    long they are, and which days it covers.

    Writes nothing. The table profile is computed once here and handed down so
    that the nulls and missing counts shown for this column are identical to
    the ones `profile_dataset` reports for it.

    `workspace_id` is accepted and unused on purpose. Every profiling tool
    taking the same first arguments is worth more than saving one parameter,
    because a tool signature that varies by tool is a thing the agent has to
    remember rather than pattern-match.
    """
    try:
        table = profile_table(
            con,
            dataset_name,
            missing_values=(
                MISSING_VALUES if missing_values is None else missing_values
            ),
            only=column,
        )
        detail = _profile_column(
            con, dataset_name, column, top_n=top_n, table=table
        )
    except ContractRefused as exc:
        return str(exc)
    return render_column(detail)


def read_result(
    workspace_id: str,
    path: str,
    start: int = 1,
    limit: int = results.PAGE_ROWS,
    start_col: int = 1,
    col_limit: int = results.PREVIEW_COLS,
) -> str:
    """
    Read one page of a result file written by an earlier tool call.

    Rows are numbered from 1 and the header is not row 0. Columns are
    numbered from 1 too: a wide result shows twelve at a time and says how
    many are left, which is the only way the rest are reachable. A path
    resolving outside this workspace's results directory is refused whether
    or not it exists.
    """
    return results.read_result_file(
        workspace_id, path, start=start, limit=limit,
        start_col=start_col, col_limit=col_limit,
    )


__all__ = ["profile_column", "profile_dataset", "read_result"]
