"""The size gate. Runs before every load, without exception.

Phase 2, Step 4. See build guide locked decision 5 ("a size gate runs before
every load; refusing with guidance beats being OOM-killed"), failure mode F3,
and Section 8.2 (instructional refusals).

Three checks, in order of cost:

  1. File size against the type's thresholds   -- one stat() call
  2. Free disk headroom                        -- one statvfs call
  3. Row count, Excel only                     -- reads the sheet dimension

Each returns one of three verdicts:

  OK      load proceeds silently
  WARN    load proceeds, the caller tells the user it will be slow
  REFUSE  load does not start; the message says why and what to do instead

A refusal is not an apology. It states the number, the limit, and the next
action. Section 8.2: an error the user cannot act on is a bug.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..config import (
    SIZE_GATES,
    IS_SMALL_MACHINE,
    TOTAL_RAM_GIB,
    human_bytes,
)


class Verdict(str, Enum):
    OK = "OK"
    WARN = "WARN"
    REFUSE = "REFUSE"


@dataclass(frozen=True)
class GateResult:
    verdict: Verdict
    message: str
    size_bytes: int = 0
    row_estimate: int | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is not Verdict.REFUSE

    def __bool__(self) -> bool:
        return self.allowed


# Extension -> logical source type. Anything unlisted is treated as CSV-like,
# because a stream-parsed text file is the generous case and a wrong guess
# there fails safe.
_EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}
_CSV_SUFFIXES = {".csv", ".tsv", ".txt", ".psv"}


def source_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _EXCEL_SUFFIXES:
        return "excel"
    if suffix in _CSV_SUFFIXES:
        return "csv"
    if suffix == ".xls":
        return "xls"
    return "csv"


def _machine_note() -> str:
    if IS_SMALL_MACHINE:
        return (f"Limits are halved on this machine ({TOTAL_RAM_GIB} GB RAM) "
                f"to avoid running out of memory.")
    return f"Limits are set for {TOTAL_RAM_GIB} GB of RAM."


def check_disk_headroom(path: Path, size_bytes: int) -> GateResult | None:
    """Refuse if the volume lacks room for the load plus working space.

    DuckDB spills intermediates to disk rather than failing, which is why CSV
    limits can be generous -- but spilling needs somewhere to spill to. Returns
    None when there is enough room.
    """
    g = SIZE_GATES
    try:
        free = shutil.disk_usage(path.parent).free
    except OSError:
        return None  # cannot tell; do not block on a diagnostic failing

    needed = max(int(size_bytes * g.disk_headroom_multiplier),
                 g.disk_min_free_bytes)
    if free >= needed:
        return None

    return GateResult(
        verdict=Verdict.REFUSE,
        size_bytes=size_bytes,
        message=(
            f"BLOCKED: not enough free disk space to load this file safely.\n"
            f"File is {human_bytes(size_bytes)}; loading needs about "
            f"{human_bytes(needed)} free, but only {human_bytes(free)} is "
            f"available.\n"
            f"WHY: DuckDB writes intermediate results to disk during a load. "
            f"Without headroom the load fails partway and leaves a corrupt "
            f"table.\n"
            f"NEXT STEP: free up space, or load a subset of the file instead."
        ),
    )


def estimate_excel_rows(path: Path) -> int | None:
    """Best-effort row count from the sheet dimension.

    openpyxl in read_only mode exposes max_row from the worksheet's declared
    dimension. That declaration is written by the producing application and is
    sometimes absent or wrong, so this is an estimate and never the sole basis
    for a refusal. Returns None when it cannot be determined.
    """
    try:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True)
        try:
            ws = wb.active
            rows = ws.max_row
            return int(rows) if rows else None
        finally:
            wb.close()
    except Exception:
        return None


def check_file(path: Path | str, source_type: str | None = None) -> GateResult:
    """Run the gate for one file. Call this before opening anything.

    The caller does not need to know which checks apply to which type; that is
    decided here from the extension.
    """
    path = Path(path)
    g = SIZE_GATES

    if not path.exists():
        return GateResult(
            verdict=Verdict.REFUSE,
            message=(
                f"BLOCKED: no file at {path}.\n"
                f"NEXT STEP: check the path. If it contains spaces, quote it."
            ),
        )
    if path.is_dir():
        return GateResult(
            verdict=Verdict.REFUSE,
            message=(
                f"BLOCKED: {path} is a folder, not a file.\n"
                f"NEXT STEP: give the path to a single .csv or .xlsx file."
            ),
        )

    size = path.stat().st_size
    kind = source_type or source_type_for(path)

    if size == 0:
        return GateResult(
            verdict=Verdict.REFUSE,
            size_bytes=0,
            message=(
                f"BLOCKED: {path.name} is empty (0 bytes).\n"
                f"NEXT STEP: check the file downloaded or exported completely."
            ),
        )

    # .xls is a different format entirely; openpyxl cannot read it.
    if kind == "xls":
        return GateResult(
            verdict=Verdict.REFUSE,
            size_bytes=size,
            message=(
                f"BLOCKED: {path.name} is the legacy .xls format, which this "
                f"agent cannot read.\n"
                f"NEXT STEP: open it in Excel or Numbers and save as .xlsx, "
                f"then load that."
            ),
        )

    if kind == "excel":
        if size > g.excel_refuse_bytes:
            return GateResult(
                verdict=Verdict.REFUSE,
                size_bytes=size,
                message=(
                    f"BLOCKED: {path.name} is {human_bytes(size)}, over the "
                    f"{human_bytes(g.excel_refuse_bytes)} limit for Excel "
                    f"files.\n"
                    f"WHY: Excel is parsed row by row as XML and cannot spill "
                    f"to disk, so a file this size would take a very long time "
                    f"or exhaust memory. {_machine_note()}\n"
                    f"NEXT STEP: open it in Excel and save the sheet as CSV, "
                    f"then load that. CSV of the same data is allowed up to "
                    f"{human_bytes(g.csv_refuse_bytes)}."
                ),
            )

        disk = check_disk_headroom(path, size)
        if disk is not None:
            return disk

        rows = estimate_excel_rows(path)
        if rows is not None and rows > g.excel_refuse_rows:
            return GateResult(
                verdict=Verdict.REFUSE,
                size_bytes=size,
                row_estimate=rows,
                message=(
                    f"BLOCKED: {path.name} declares about {rows:,} rows, over "
                    f"the {g.excel_refuse_rows:,} row limit for Excel.\n"
                    f"WHY: openpyxl reads roughly 50,000 rows per second, so "
                    f"this would take several minutes with no way to report "
                    f"progress. {_machine_note()}\n"
                    f"NEXT STEP: save the sheet as CSV and load that instead."
                ),
            )

        if size > g.excel_warn_bytes or (
            rows is not None and rows > g.excel_warn_rows
        ):
            detail = f"{human_bytes(size)}"
            if rows is not None:
                detail += f", about {rows:,} rows"
            return GateResult(
                verdict=Verdict.WARN,
                size_bytes=size,
                row_estimate=rows,
                message=(
                    f"{path.name} is large for an Excel file ({detail}). "
                    f"Loading will work but may take a minute or two. "
                    f"Saving it as CSV would be considerably faster."
                ),
            )

        return GateResult(Verdict.OK, "", size, rows)

    # CSV and everything text-like.
    if size > g.csv_refuse_bytes:
        return GateResult(
            verdict=Verdict.REFUSE,
            size_bytes=size,
            message=(
                f"BLOCKED: {path.name} is {human_bytes(size)}, over the "
                f"{human_bytes(g.csv_refuse_bytes)} limit.\n"
                f"WHY: {_machine_note()}\n"
                f"NEXT STEP: split the file, or load it into Postgres and "
                f"connect to that instead -- a database source has no size "
                f"limit because nothing is copied."
            ),
        )

    disk = check_disk_headroom(path, size)
    if disk is not None:
        return disk

    if size > g.csv_warn_bytes:
        return GateResult(
            verdict=Verdict.WARN,
            size_bytes=size,
            message=(
                f"{path.name} is {human_bytes(size)}, over the "
                f"{human_bytes(g.csv_warn_bytes)} warning threshold (files over "
                f"{human_bytes(g.csv_refuse_bytes)} are refused). Loading will work and "
                f"the preview will be fast, but profiling and analysis will "
                f"take noticeably longer than on a small file."
            ),
        )

    return GateResult(Verdict.OK, "", size)


def describe(path: Path | str) -> str:
    """One-line human summary of the gate outcome. For tool output."""
    result = check_file(path)
    if result.verdict is Verdict.OK:
        return f"{Path(path).name}: {human_bytes(result.size_bytes)}, within limits."
    return result.message
