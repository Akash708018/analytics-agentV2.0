"""Excel loading: openpyxl read-only streaming into DuckDB.

Phase 2, Step 6; extended in Phase 3, Step 5. See build guide Section 4.2
(read_only=True, 50,000-row batches), locked decision 4, failure modes F3, F5
and F9.

Why this is harder than CSV. DuckDB reads a CSV and infers each column's type
itself. openpyxl hands back Python objects, so the inference has to be written
here -- and a wrong guess produces a silently mistyped column, which is F5.

Verified against openpyxl on real files:

  * A date written to Excel comes back as datetime.datetime, never
    datetime.date. Excel has no pure date type.
  * bool is a subclass of int in Python, so isinstance(True, int) is True.
    Booleans must be tested BEFORE integers or every one becomes BIGINT.
  * A column of entirely empty cells yields no type information at all and
    falls back to VARCHAR.
  * merged_cells.ranges does NOT exist on a read-only worksheet.

That last point used to be served by a `merged_ranges` function here, which
opened the workbook in NORMAL mode and carried a docstring warning nobody could
enforce. It is gone. `ingest.merges.merged_ranges` streams the sheet XML out of
the zip instead: constant memory, correct under tab reordering, and tested.
Callers that used to pass `sheet=None` and get the active sheet should use
`merges.active_sheet_name(path)`, which reads the same `activeTab` openpyxl
does.

Memory: iter_rows on a read-only worksheet is a generator. Rows are staged to a
temporary CSV and loaded with one read_csv. Peak memory is the inference sample
plus the footer buffer, not the file.
"""

from __future__ import annotations

import collections as _collections
import csv as _csv
import datetime as _dt
import os as _os
import tempfile as _tempfile
from pathlib import Path

import duckdb

from ..config import EXCEL_BATCH_ROWS, EXCEL_PREVIEW_ROWS  # noqa: F401
from ..util import db
from . import sizegate
from .csv_loader import (
    ON_ERROR_NULL,
    ON_ERROR_STOP,
    LoadRefused,
    LoadResult,
    validate_dataset_name,
    validate_on_error,
)

# Python type -> DuckDB type. Order matters at the call site: bool first.
_TYPE_RANK = {
    "BOOLEAN": 0,
    "BIGINT": 1,
    "DOUBLE": 2,
    "TIMESTAMP": 3,
    "VARCHAR": 4,
}


def _duck_type(value: object) -> str | None:
    """DuckDB type for one Python value, or None when it says nothing."""
    if value is None:
        return None
    if isinstance(value, bool):        # MUST precede int
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE"
    if isinstance(value, (_dt.datetime, _dt.date)):
        return "TIMESTAMP"
    return "VARCHAR"


def _merge_types(a: str | None, b: str | None) -> str | None:
    """Widen two observed types to one that holds both.

    BIGINT + DOUBLE -> DOUBLE. Anything genuinely incompatible -> VARCHAR,
    because keeping the value as text loses nothing, while forcing a type
    would lose the row.
    """
    if a is None:
        return b
    if b is None:
        return a
    if a == b:
        return a
    if {a, b} == {"BIGINT", "DOUBLE"}:
        return "DOUBLE"
    return "VARCHAR"


def list_sheets(path: Path | str) -> list[str]:
    """Sheet names, without reading any data."""
    from openpyxl import load_workbook

    wb = load_workbook(Path(path), read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def preview_rows(
    path: Path | str,
    sheet: str | None = None,
    n: int = EXCEL_PREVIEW_ROWS,
) -> list[tuple]:
    """First n rows as raw tuples. Never reads the whole sheet.

    Locked decision 4. This is what the header conversation in Phase 3 reads to
    work out where the real headers are.
    """
    from openpyxl import load_workbook

    # data_only: the value Excel saved for a formula, not the formula's text (P14-O5, B3).
    wb = load_workbook(Path(path), read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.active
        out = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= n:
                break
            out.append(row)
        return out
    finally:
        wb.close()


def _apply_na(value: object, na_tokens: frozenset[str]) -> object:
    """Turn a configured null token into None. Only strings are candidates.

    A numeric 0 or a real date is never a null token, whatever the token list
    says, so the comparison is deliberately restricted to str.
    """
    if isinstance(value, str) and value.strip() in na_tokens:
        return None
    return value


def _infer_column_types(
    rows: list[tuple], n_cols: int, all_text: bool
) -> list[str]:
    if all_text:
        return ["VARCHAR"] * n_cols
    types: list[str | None] = [None] * n_cols
    for row in rows:
        for i in range(min(n_cols, len(row))):
            types[i] = _merge_types(types[i], _duck_type(row[i]))
    # A column that was entirely empty told us nothing. VARCHAR holds anything.
    return [t or "VARCHAR" for t in types]


def _coerce(value: object, duck_type: str):
    """Prepare one value for insertion, or raise ValueError.

    Never silently drops a value. A value that will not fit its column raises;
    load_excel turns that into an instructional refusal naming the cell under
    on_error='stop', or a counted NULL under on_error='null'.
    """
    if value is None:
        return None
    if duck_type == "VARCHAR":
        return value if isinstance(value, str) else str(value)
    if duck_type == "BOOLEAN":
        if isinstance(value, bool):
            return value
        raise ValueError(f"{value!r} is not a boolean")
    if duck_type == "BIGINT":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{value!r} is not an integer")
        return value
    if duck_type == "DOUBLE":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{value!r} is not a number")
        return float(value)
    if duck_type in ("TIMESTAMP", "DATE"):
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value
        if isinstance(value, str):
            # Only reachable when dtypes pinned the column: inference never
            # produces TIMESTAMP from a str. Parsed here rather than handed to
            # DuckDB so a bad value is counted like any other coercion failure.
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
                try:
                    return _dt.datetime.strptime(value.strip(), fmt)
                except ValueError:
                    continue
        raise ValueError(f"{value!r} is not a date")
    return value


def load_excel(
    con: duckdb.DuckDBPyConnection,
    path: Path | str,
    dataset_name: str,
    sheet: str | None = None,
    header_rows: int = 1,
    names: list[str] | None = None,
    na_values: list[str] | None = None,
    footer_skip_rows: int = 0,
    dtypes: dict[str, str] | None = None,
    on_error: str = ON_ERROR_STOP,
    all_text: bool = False,
    inference_rows: int = 5_000,
    replace: bool = True,
) -> LoadResult:
    """Stream a worksheet into a DuckDB table and register it.

    sheet             Sheet name. Defaults to the active sheet.
    header_rows       Rows at the top that are headers, not data.
    names             Explicit column names. Required when header_rows != 1.
    na_values         Tokens to read as NULL, e.g. ['N/A', '-']. Applied to
                      string cells only, before type inference -- so a column
                      of numbers with a few 'N/A' cells is BIGINT with NULLs
                      rather than VARCHAR. Defaults to no tokens; an empty
                      cell is already None from openpyxl.
    footer_skip_rows  Data rows at the END of the sheet to drop -- a totals
                      row, a 'generated on' line. Held in a small buffer as
                      the sheet streams, so it costs footer_skip_rows of
                      memory and no second pass.
    dtypes            Pin specific columns to a DuckDB type, e.g.
                      {'order_date': 'TIMESTAMP'}. Keyed on the FINAL column
                      name -- the one in `names` if names were given. Columns
                      not named here are inferred as usual. This is the only
                      way to say 'that text column is really a date', because
                      inference maps a str to VARCHAR and never guesses at its
                      contents.
    on_error          'stop' (default) refuses the load at the first value
                      that does not fit its column, naming the row and the
                      column. 'null' stores that cell as NULL, keeps the row,
                      and counts the failures per column into
                      LoadResult.coercion_failures -- F9.
    all_text          Load every column as VARCHAR. The blunt escape hatch
                      when a column has mixed types partway down; on_error
                      ='null' is usually the better answer.
    inference_rows    Rows examined to decide column types. Raising it costs
                      memory; lowering it risks a wrong guess.
    """
    from openpyxl import load_workbook

    path = Path(path)
    validate_dataset_name(dataset_name)
    validate_on_error(on_error)

    gate = sizegate.check_file(path, "excel")
    if not gate.allowed:
        raise LoadRefused(gate.message)

    if header_rows < 0:
        raise LoadRefused(
            f"BLOCKED: header_rows cannot be negative (got {header_rows}).\n"
            f"NEXT STEP: use 0 for no header, 1 for the usual case."
        )
    if footer_skip_rows < 0:
        raise LoadRefused(
            f"BLOCKED: footer_skip_rows cannot be negative (got "
            f"{footer_skip_rows}).\n"
            f"NEXT STEP: use 0 to keep every data row."
        )
    if header_rows != 1 and not names:
        raise LoadRefused(
            f"BLOCKED: header_rows={header_rows} but no column names were "
            f"given.\n"
            f"WHY: with more than one header row the names cannot be read "
            f"automatically.\n"
            f"NEXT STEP: call preview_rows() to see the top of the sheet, then "
            f"pass names=[...]."
        )

    na_tokens = frozenset(t.strip() for t in (na_values or []))

    coercion: dict[str, int] = {}

    # data_only: a formula cell loads the value Excel saved for it. Without it openpyxl returns
    # '=G2*H2', and every formula column loaded as text (P14-O5, B3).
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - BadZipFile, InvalidFileException, KeyError
        # The draft path refuses these (P14-D70); load_excel called directly raised openpyxl's
        # InvalidFileException out of the MCP tool, the benchmark's one crash (P14-O13).
        raise LoadRefused(
            f"BLOCKED: {path.name} is not a readable .xlsx workbook.\n"
            f"WHY: {type(exc).__name__}: {exc}. It may be another kind of file renamed, cut "
            f"short, or protected with a password.\n"
            # propose_ingest_spec on the same file meets the same unreadable workbook (Step 13
            # benchmark): the next step is a new file, not the same call through another tool.
            f"NEXT STEP: open it in Excel or the program that made it and save it again as "
            f".xlsx, or export it as .csv, then call propose_ingest_spec(path=...) on the new "
            f"file."
        ) from exc
    try:
        if sheet is not None and sheet not in wb.sheetnames:
            raise LoadRefused(
                f"BLOCKED: {path.name} has no sheet called {sheet!r}.\n"
                f"Sheets present: {', '.join(wb.sheetnames)}\n"
                f"NEXT STEP: pass one of those names."
            )
        ws = wb[sheet] if sheet else wb.active
        stream = ws.iter_rows(values_only=True)
        # What the sheet SHOWS: a merged cell's value in every cell of the merge, and an error
        # cell (#DIV/0!, #N/A) as no value (P14-D58). Both counted into the load's notes.
        filled, errors = [0], {}
        stream = _as_displayed(stream, _merge_boxes(path, ws.title), filled, errors)

        header: list[str] = []
        for _ in range(header_rows):
            try:
                header = list(next(stream))
            except StopIteration:
                raise LoadRefused(
                    f"BLOCKED: {path.name} has fewer than {header_rows} rows.\n"
                    f"NEXT STEP: check the sheet is the one you meant."
                ) from None

        # Blank rows inside the data are skipped, not loaded as rows of NULLs (P14-O8, B6). After
        # the footer is trimmed, because footer_skip_rows counts the blank rows in a footer.
        blanks = [0]
        kept: list[int] = []  # the sheet-relative position of every row that is emitted
        data = _apply_na_rows(_skip_blank(_trim_footer(stream, footer_skip_rows), blanks, kept),
                              na_tokens)

        # Buffer enough rows to infer types before creating the table.
        sample: list[tuple] = []
        for row in data:
            sample.append(row)
            if len(sample) >= inference_rows:
                break

        if not sample:
            raise LoadRefused(
                f"BLOCKED: {path.name} has headers but no data rows"
                + (
                    f" after dropping {footer_skip_rows} footer row(s)"
                    if footer_skip_rows
                    else ""
                )
                + ".\n"
                f"NEXT STEP: check the sheet, or set header_rows=0 if the "
                f"first row is really data."
            )

        n_cols = max(len(r) for r in sample)
        if names:
            if len(names) != n_cols:
                raise LoadRefused(
                    f"BLOCKED: {len(names)} column names given but the sheet "
                    f"has {n_cols} columns.\n"
                    f"NEXT STEP: supply exactly {n_cols} names, or check "
                    f"header_rows."
                )
            columns = list(names)
        else:
            columns = [
                str(h).strip() if h is not None else f"column{i}"
                for i, h in enumerate(header[:n_cols])
            ]
            columns += [f"column{i}" for i in range(len(columns), n_cols)]

        seen: dict[str, int] = {}
        final: list[str] = []
        for c in columns:
            c = c or "column"
            if c in seen:
                seen[c] += 1
                c = f"{c}_{seen[c]}"
            else:
                seen[c] = 0
            final.append(c)
        columns = final

        types = _infer_column_types(sample, n_cols, all_text)

        if dtypes:
            unknown = sorted(set(dtypes) - set(columns))
            if unknown:
                raise LoadRefused(
                    f"BLOCKED: dtypes names column(s) that are not in this "
                    f"sheet: {', '.join(unknown)}.\n"
                    f"Columns present: {', '.join(columns)}\n"
                    f"NEXT STEP: dtypes is keyed on the final column name, "
                    f"which is the name from `names` when you supply one."
                )
            types = [dtypes.get(c, t) for c, t in zip(columns, types)]

        cols_ddl = ", ".join(f'"{c}" {t}' for c, t in zip(columns, types))
        verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"
        con.execute(f'{verb} "{dataset_name}" ({cols_ddl})')

        # Rows are staged to a temporary CSV and loaded with one read_csv,
        # rather than bound as statement parameters.
        #
        # Measured on 120,000 rows: executemany 7,000 rows/s; multi-row INSERT
        # 98,000 rows/s on one machine but only 3,100 on another, with no
        # difference between in-memory and on-disk DuckDB -- so the cost is
        # parameter binding, not the disk. Staging through a CSV hands the work
        # to DuckDB's vectorised reader instead: 1,460,000 rows/s, and it does
        # not vary the same way.
        #
        # Verified to survive the round trip: BOOLEAN, TIMESTAMP, NULL, and
        # strings containing quotes, commas and newlines.
        fd, tmp_path = _tempfile.mkstemp(suffix=".csv", prefix="agent_xl_")
        _os.close(fd)
        total = 0
        emitted = 0

        try:
            with open(tmp_path, "w", newline="", encoding="utf-8") as tmp:
                writer = _csv.writer(tmp)
                for row in _chain(sample, data):
                    # The sheet's own row number, blank rows above included (P14-D49): counting
                    # emitted rows named the wrong row once any blank one had been skipped.
                    row_no = header_rows + kept[emitted] + 1
                    emitted += 1
                    padded = list(row[:n_cols]) + [None] * (n_cols - len(row))
                    out = []
                    for i, (value, dtype) in enumerate(zip(padded, types)):
                        try:
                            out.append(_coerce(value, dtype))
                        except ValueError as exc:
                            if on_error == ON_ERROR_NULL:
                                out.append(None)
                                coercion[columns[i]] = coercion.get(columns[i], 0) + 1
                                continue
                            raise LoadRefused(
                                f"BLOCKED: row {row_no} of {path.name} has a "
                                f"value that does not fit its column.\n"
                                f"Column '{columns[i]}' was read as "
                                f"{types[i]}, but this row holds "
                                f"{value!r} ({exc}).\n"
                                f"WHY: types were inferred from the first "
                                f"{len(sample):,} rows, and this row "
                                f"disagrees. Nothing has been dropped -- the "
                                f"load stopped instead.\n"
                                f"NEXT STEP: reload with on_error='null' to "
                                f"store that cell as NULL and count how many "
                                f"there are, with all_text=True to keep every "
                                f"column as text, or raise inference_rows "
                                f"above {row_no}."
                            ) from exc
                    writer.writerow(out)
                    total += 1

            col_spec = ", ".join(
                f"'{c}': '{t}'" for c, t in zip(columns, types)
            )
            con.execute(
                f'INSERT INTO "{dataset_name}" SELECT * FROM read_csv('
                f"'{tmp_path}', header=false, columns={{{col_spec}}})"
            )
        finally:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass
    finally:
        wb.close()

    utc = db.utc_timestamps(con, dataset_name)
    rows, cols = db.table_shape(con, dataset_name)
    result_columns = [
        (r[0], r[1])
        for r in con.execute(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema='main' AND table_name=?
               ORDER BY ordinal_position""",
            [dataset_name],
        ).fetchall()
    ]

    db.register_dataset(
        con,
        dataset_name=dataset_name,
        source_type="excel",
        source_detail=f"{path} [{ws.title}]",
        row_count=rows,
        column_count=cols,
        notes=(
            f"header_rows={header_rows}, footer_skip_rows={footer_skip_rows}, "
            f"on_error={on_error}, all_text={all_text}"
        ),
    )

    return LoadResult(
        dataset_name=dataset_name,
        row_count=rows,
        column_count=cols,
        columns=result_columns,
        gate_verdict=gate.verdict.value,
        gate_message=gate.message,
        coercion_failures=coercion,
        notes=([f"{blanks[0]:,} blank row(s) inside the data were skipped: a row with no "
                f"value in any column is a gap in the sheet, not a record."] if blanks[0] else [])
        + ([f"{filled[0]:,} cell(s) inside merged ranges took their merge's value, as the "
            f"sheet displays them."] if filled[0] else [])
        + ([f"{sum(errors.values()):,} cell(s) held an Excel error ("
            + ", ".join(f"{k} x{v}" for k, v in sorted(errors.items()))
            + ") and load as empty: an error is a formula that failed, not a value."]
           if errors else [])
        + db.utc_note(utc),
    )


EXCEL_ERRORS = frozenset({"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A",
                          "#GETTING_DATA", "#SPILL!", "#CALC!"})


def _merge_boxes(path, sheet: str) -> list[tuple[int, int, int, int]]:
    """(min_row, min_col, max_row, max_col), 1-based, of every merge spanning more than one row."""
    from openpyxl.utils.cell import range_boundaries

    from . import merges
    boxes = []
    for ref in merges.merged_ranges(str(path), sheet):
        c1, r1, c2, r2 = range_boundaries(ref)
        if r2 > r1:
            boxes.append((r1, c1, r2, c2))
    return boxes


def _as_displayed(stream, boxes, filled: list[int], errors: dict[str, int]):
    """Rows as the sheet displays them: a vertical merge's value repeated down it, and error
    cells as None. `stream` is the sheet from row 1, so the row number is the position + 1."""
    anchors: dict[tuple[int, int], object] = {}
    for r, row in enumerate(stream, start=1):
        row = list(row)
        for i, v in enumerate(row):
            if isinstance(v, str) and v.strip() in EXCEL_ERRORS:
                errors[v.strip()] = errors.get(v.strip(), 0) + 1
                row[i] = None
        for r1, c1, r2, c2 in boxes:
            if r1 <= r <= r2:
                for c in range(c1, c2 + 1):
                    if r == r1:
                        anchors[(r1, c)] = row[c - 1] if c - 1 < len(row) else None
                    elif c - 1 < len(row) and row[c - 1] is None and \
                            anchors.get((r1, c)) is not None:
                        row[c - 1] = anchors[(r1, c)]
                        filled[0] += 1
        yield tuple(row)


def _skip_blank(stream, counter: list[int], kept: list[int] | None = None):
    """Every row that holds at least one value; counter[0] counts the ones that held none, and
    `kept` receives the position of each row yielded, so an error can name its sheet row."""
    for position, row in enumerate(stream):
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in row):
            counter[0] += 1
            continue
        if kept is not None:
            kept.append(position)
        yield row


def _apply_na_rows(stream, na_tokens: frozenset[str]):
    """Replace configured null tokens before anything else sees the row."""
    if not na_tokens:
        yield from stream
        return
    for row in stream:
        yield tuple(_apply_na(v, na_tokens) for v in row)


def _trim_footer(stream, footer_skip_rows: int):
    """
    Yield every row except the last `footer_skip_rows`.

    A deque of that size runs one step behind the stream, so the trailing rows
    are never emitted and the sheet is still read once. Peak memory is
    footer_skip_rows rows, not the sheet.
    """
    if footer_skip_rows <= 0:
        yield from stream
        return
    buffer = _collections.deque(maxlen=footer_skip_rows)
    for row in stream:
        if len(buffer) == footer_skip_rows:
            yield buffer[0]
        buffer.append(row)


def _safe_fails(value: object, duck_type: str) -> bool:
    try:
        _coerce(value, duck_type)
        return False
    except ValueError:
        return True


def _chain(first: list, rest):
    """Replay the buffered sample, then continue streaming. Avoids holding the
    whole sheet just because the first rows were needed for inference."""
    yield from first
    yield from rest
