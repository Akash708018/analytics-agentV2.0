"""
Where a result goes when it is too big to say, and how it is said anyway.

Phase 5 is the first layer that produces output nobody can fit in a tool
result. A 4,000-row breakdown is a legitimate answer and it is also 4,000 rows.
The obvious move -- write the file, return the path -- is failure mode F7: the
agent receives a path, never opens it, and reports confidently on data it did
not see. Nothing in the transcript looks wrong. That is what makes it the worst
of the failure modes rather than the most annoying.

So a path never travels alone. Every write returns an ENVELOPE: the path, the
shape, a summary, the first rows, and the literal call that fetches the rest.
The rule is locked decision 20, and it is enforced here rather than in each
caller, because "remember to include a summary" is not a rule, it is a hope.

Three things this module does that are less obvious.

**It truncates before formatting, never after.** `util/formatting.format_table`
caps at 50 rows and 50 columns. If a 60-column preview were handed to it, the
cap would decide what the model sees and the model would not be told that a cap
happened. So the preview is sliced HERE, to 20 rows and 12 columns, and the
envelope states both counts out loud. format_table never reaches its own limits,
which also means its behaviour at them stops mattering.

**A path from a model is not trusted.** `read_result_file` takes a path the
model wrote down. It is resolved and checked against the workspace's results
directory, and anything outside is refused with a reason code. This is one
`Path.resolve()` and one comparison, and without it an MCP tool reads arbitrary
files on request.

**Nothing is overwritten.** Names carry a timestamp and a counter. A profile
from an hour ago is evidence about what the table looked like an hour ago, and
the moment two runs share a name, the second silently destroys the thing the
first was for.

`workspace_id` is a required argument everywhere, never defaulted at import.
That is locked decision 14, and this module is the first place a wrong default
would put files somewhere `reset_workspace` does not clear.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from backend.engine import workspace
from backend.engine.contract.refusals import Reason, Refusal
from backend.engine.util.formatting import format_table

# The subdirectory of the workspace that results live in. Inside the workspace
# on purpose: reset_workspace clears the workspace directory, so results are
# cleared with it and a leftover from an earlier chat cannot outlive the reset
# that was meant to remove it (F13).
RESULTS_DIRNAME = "results"

# What the envelope shows inline. Twenty rows is enough to see the shape of an
# answer and short enough that it cannot be mistaken for the answer. Twelve
# columns is the point past which a pipe table stops being readable in a chat
# window at all.
PREVIEW_ROWS = 20
PREVIEW_COLS = 12

# What one read_result_file call returns. Matches formatting.MAX_ROWS so a page
# is exactly what the formatter was built to render.
PAGE_ROWS = 50

# Labels become filenames, so they are constrained the way column names are.
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")

_STAMP_FORMAT = "%Y%m%d-%H%M%S"


def claim(directory: Path, stem: str, suffix: str) -> Path:
    """A new file's path, created empty so no other writer can take it.

    `exists()` then `open("w")` let two threads of one workspace pick the same free name inside
    one second, and the second write replaced the first: a reply named a file that held another
    dataset's rows (Step 14, H_concurrency; D16). Exclusive creation is atomic, so the loser of
    a race moves on to the next counter.
    """
    directory.mkdir(parents=True, exist_ok=True)
    counter = 1
    while True:
        path = directory / (f"{stem}{suffix}" if counter == 1 else f"{stem}_{counter}{suffix}")
        try:
            with path.open("x"):
                return path
        except FileExistsError:
            counter += 1


def results_dir(workspace_id: str) -> Path:
    """
    The results directory for one workspace, created if it is not there.

    Derived from workspace.workspace_dir rather than declared separately, so
    that whatever reset() removes, this goes with it.
    """
    path = workspace.workspace_dir(workspace_id) / RESULTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clean(value: Any) -> str:
    """One cell as CSV sees it. None is empty, everything else is its str()."""
    return "" if value is None else str(value)


@dataclass(frozen=True)
class Result:
    """
    A result on disk, and everything needed to say it without opening it.

    `to_text()` is what a tool returns. The path alone is never returned by
    anything -- there is deliberately no accessor that produces just the path
    as a string, because the one that exists is the one that gets used.
    """

    path: Path
    headers: list[str]
    row_count: int
    created_at: datetime
    label: str
    dataset_name: str | None = None
    summary: list[str] = field(default_factory=list)
    preview_rows: list[list[str]] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        return len(self.headers)

    def paging_hint(self, start: int = PREVIEW_ROWS + 1) -> str:
        """The literal call that fetches the next page. Not a description."""
        return (
            f'read_result_file(path="{self.path}", start={start}, '
            f"limit={PAGE_ROWS})"
        )

    def to_text(self) -> str:
        shown = min(len(self.preview_rows), PREVIEW_ROWS)
        head = self.headers[:PREVIEW_COLS]

        lines = [
            f"{self.row_count:,} rows x {self.column_count} columns, written to",
            f"  {self.path}",
        ]
        if self.dataset_name:
            lines.append(f"from dataset   {self.dataset_name}")
        lines.append(f"created        {self.created_at:%Y-%m-%d %H:%M:%S}")

        if self.summary:
            lines += ["", "What this shows:"]
            lines += [f"  - {s}" for s in self.summary]

        if shown:
            lines += ["", f"First {shown} of {self.row_count:,} rows:", ""]
            lines.append(
                format_table([r[:PREVIEW_COLS] for r in self.preview_rows[:shown]], head)
            )
        if self.column_count > PREVIEW_COLS:
            lines.append(
                f"Showing {len(head)} of {self.column_count} columns. The file "
                f"has all of them: {', '.join(self.headers)}."
            )

        if self.row_count > shown:
            lines += [
                "",
                f"{self.row_count - shown:,} more rows are in the file and are "
                f"NOT shown above. Read them before describing them:",
                f"  {self.paging_hint(shown + 1)}",
            ]
        return "\n".join(lines)


def write_result(
    workspace_id: str,
    *,
    label: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    summary: Sequence[str] = (),
    dataset_name: str | None = None,
    now: datetime | None = None,
) -> Result:
    """
    Write a table to the workspace results directory and describe it.

    Returns a `Result`, never a path. Callers render it with `to_text()`; the
    envelope is not optional and there is no way to get the path out without
    having built the thing that says what is in it.
    """
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"result label {label!r} must start with a letter and contain only "
            f"letters, digits and underscores -- it becomes a filename."
        )
    if not headers:
        raise ValueError("a result needs at least one column header.")

    stamp = (now or datetime.now()).strftime(_STAMP_FORMAT)
    directory = results_dir(workspace_id)

    # Two runs inside one second must not share a name. The counter is checked
    # against the filesystem rather than held in memory, because a second
    # process writing to the same workspace is exactly the F13 situation -- and
    # the name is claimed by creating the file, because a check is not a claim.
    path = claim(directory, f"{label}_{stamp}", ".csv")

    head = [str(h) for h in headers]
    written = 0
    preview: list[list[str]] = []
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(head)
        for row in rows:
            cells = [_clean(v) for v in row]
            writer.writerow(cells)
            written += 1
            if len(preview) < PREVIEW_ROWS:
                preview.append(cells)

    return Result(
        path=path,
        headers=head,
        row_count=written,
        created_at=now or datetime.now(),
        label=label,
        dataset_name=dataset_name,
        summary=list(summary),
        preview_rows=preview,
    )


def list_results(workspace_id: str) -> list[Path]:
    """Every result file in this workspace, newest first."""
    directory = results_dir(workspace_id)
    return sorted(directory.glob("*.csv"), key=lambda p: p.name, reverse=True)


def _known(workspace_id: str) -> str:
    found = list_results(workspace_id)
    if not found:
        return "(no result files in this workspace)"
    return ", ".join(p.name for p in found[:10])


def _read_one_of(workspace_id: str) -> str:
    """The call to make instead: runnable as written when one file is there (Step 13 benchmark
    counted the `<one of: x>` template a placeholder when x was the only file)."""
    found = list_results(workspace_id)
    if len(found) == 1:
        return f'read_result_file(path="{found[0]}")'
    return (
        f'read_result_file(path="{results_dir(workspace_id)}/<one of: '
        f'{_known(workspace_id)}>")'
    )


def _refuse_outside(workspace_id: str, given: str) -> str:
    return Refusal(
        reason=Reason.RESULT_OUT_OF_SCOPE,
        what=f"{given!r} is not a result file in this workspace.",
        why=(
            "results are read from the workspace's own results directory and "
            "nowhere else. A path that resolves outside it is refused whether "
            "or not it exists, because a tool that reads any path on request "
            "is a tool that can be asked to read anything."
        ),
        state=f"results directory: {results_dir(workspace_id)}",
        next_call=_read_one_of(workspace_id),
    ).to_text()


def read_result_file(
    workspace_id: str,
    path: str,
    start: int = 1,
    limit: int = PAGE_ROWS,
    start_col: int = 1,
    col_limit: int = PREVIEW_COLS,
) -> str:
    """
    Read one page of a result file. Rows are numbered from 1, header excluded.

    The counterpart to `write_result`, and the reason a path may be handed out
    at all. Every path any tool in this phase returns must open here -- that
    round trip is asserted, because a writer and a reader in the same module
    still drift when the workspace root reaches them by different routes.
    """
    directory = results_dir(workspace_id).resolve()
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return _refuse_outside(workspace_id, path)

    if target.parent != directory:
        return _refuse_outside(workspace_id, path)

    if not target.exists():
        return Refusal(
            reason=Reason.RESULT_NOT_FOUND,
            what=f"there is no result file called {target.name!r}.",
            why=(
                "it was either never written, or the workspace was reset since "
                "-- reset_workspace clears results along with everything else."
            ),
            state=f"in {directory}: {_known(workspace_id)}",
            next_call=f'read_result_file(path="{directory}/<a name listed above>")',
        ).to_text()

    if start_col < 1:
        return Refusal(
            reason=Reason.RESULT_OUT_OF_SCOPE,
            what=f"start_col={start_col} is not a column number.",
            why=(
                "columns are numbered from 1, like rows. Reading from 1 "
                "instead would return a page that does not begin where it "
                "was asked to."
            ),
            next_call=f'read_result_file(path="{target}", start_col=1)',
        ).to_text()

    if start < 1:
        return Refusal(
            reason=Reason.RESULT_OUT_OF_SCOPE,
            what=f"start={start} is not a row number.",
            why=(
                "rows are numbered from 1, and the header is not row 0. "
                "Reading from 1 instead would return a page that does not "
                "begin where it was asked to, which is worse than refusing."
            ),
            next_call=f'read_result_file(path="{target}", start=1)',
        ).to_text()

    asked = limit
    limit = max(1, min(limit, PAGE_ROWS))

    with target.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            headers = next(reader)
        except StopIteration:
            return f"{target.name} is empty -- it has no header row."
        page: list[list[str]] = []
        total = 0
        for i, row in enumerate(reader, start=1):
            total = i
            if start <= i < start + limit:
                page.append(row)

    if not page:
        return (
            f"{target.name} has {total:,} rows and row {start:,} is past the "
            f"end. Nothing was returned. The last page starts at "
            f"{max(1, total - limit + 1):,}:\n"
            f'  read_result_file(path="{target}", '
            f"start={max(1, total - limit + 1)}, limit={limit})"
        )

    col_limit = max(1, min(col_limit, PREVIEW_COLS))
    first_col = start_col - 1
    shown_headers = headers[first_col:first_col + col_limit]
    if not shown_headers:
        return (
            f"{target.name} has {len(headers)} columns and column "
            f"{start_col:,} is past the end. Nothing was returned. The "
            f"last column page starts at "
            f"{max(1, len(headers) - col_limit + 1):,}:\n"
            f'  read_result_file(path="{target}", '
            f"start_col={max(1, len(headers) - col_limit + 1)})"
        )
    last_col = first_col + len(shown_headers)

    last = start + len(page) - 1
    out = [
        f"{target.name}: rows {start:,} to {last:,} of {total:,}, "
        f"columns {start_col:,} to {last_col:,} of {len(headers):,}.",
    ]
    if asked > PAGE_ROWS:
        # The cap was applied without a word: limit=200 returned 50 rows and the reader could not
        # tell a cap from a short file (Step 13 benchmark, D13).
        out.append(f"limit={asked:,} was asked; a page holds at most {PAGE_ROWS} rows, so "
                   f"{len(page):,} are shown"
                   + ("; the next page is the call below." if last < total else "."))
    out += [
        "",
        format_table([r[first_col:last_col] for r in page], shown_headers),
    ]
    if last_col < len(headers):
        # P8-O15: the columns past the window were written to the file and
        # were returnable by nothing before this. Measured on a 50-column
        # result: twelve cells came back, the label column among them, and
        # thirty-eight columns existed only on disk.
        out += [
            "",
            f"{len(headers) - last_col:,} more columns are in the file and "
            f"are NOT shown above:",
            f'  read_result_file(path="{target}", start={start}, '
            f"limit={limit}, start_col={last_col + 1})",
        ]
    if last < total:
        out += [
            "",
            f"{total - last:,} rows after this page:",
            f'  read_result_file(path="{target}", start={last + 1}, limit={limit})',
        ]
    else:
        out += ["", "That is the end of the file."]
    return "\n".join(out)


__all__ = [
    "PAGE_ROWS",
    "PREVIEW_COLS",
    "PREVIEW_ROWS",
    "RESULTS_DIRNAME",
    "Result",
    "list_results",
    "read_result_file",
    "results_dir",
    "write_result",
]
