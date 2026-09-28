"""CSV and delimited-text loading into DuckDB.

Phase 2, Step 5; extended in Phase 3, Step 5. See build guide Section 4.2 and
4.2.1 (the verified read path), locked decision 4 (previews never load the whole
file), F5 (everything typed VARCHAR), F9 (coercion failures counted, never
silent).

The interface is deliberately primitive: paths, integers and lists of strings,
not an IngestSpec. IngestSpec.to_loader_kwargs() maps onto these same arguments,
so nothing here is rewritten -- and Phase 2 stays testable without pulling the
conversation layer forward.

The 4.2.1 rule, verified on DuckDB 1.5.5:

    Naive:   read_csv(path, header=true)
             -> a second header row is read as data, every column VARCHAR
    Correct: read_csv(path, skip=N, header=false, names=[...])
             -> units BIGINT, unit_price DOUBLE, order_date DATE

Two behaviours found by testing, both guarded against below:

  1. Supplying MORE names than the file has columns raises a sniffing error.
  2. Supplying FEWER names does NOT error. DuckDB pads the tail with
     'column7'. Data is not shifted, but you get a silently misnamed column.
     load_csv counts the columns first and refuses on any mismatch.

Three more, found while adding footer skipping and coercion counts:

  3. A read_csv scan preserves file order, so `LIMIT n` keeps the FIRST n rows.
     Verified on a 20,000-row file at threads=8 with preserve_insertion_order
     at its default: the kept ids were exactly 1..20000, in order. That is what
     makes footer_skip_rows implementable, since read_csv has no skipfooter.
  4. DuckDB's type inference WIDENS rather than fails. A column of integers
     with one 'oops' in it comes back VARCHAR, not BIGINT-with-an-error. So a
     coercion failure count is only meaningful once the target types are
     pinned, which is what on_error='null' does.
  5. store_rejects=true reports per-column counts, but it drops the whole ROW
     and writes into persistent reject tables that accumulate across loads.
     TRY_CAST is used instead: it nulls the offending CELL, keeps the row, and
     needs no side tables. Verified on a 53-row file -- 53 rows kept,
     {'units': 1, 'price': 2} counted.
"""

from __future__ import annotations

import codecs
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from ..config import CSV_PREVIEW_LINES, DEFAULT_NA_VALUES
from ..util import db
from . import sizegate

# A dataset name becomes a SQL identifier. Validate rather than quote-and-hope.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")

# What to do with a value that will not fit its column.
ON_ERROR_STOP = "stop"
ON_ERROR_NULL = "null"
ON_ERROR_MODES = (ON_ERROR_STOP, ON_ERROR_NULL)


@dataclass
class LoadResult:
    dataset_name: str
    row_count: int
    column_count: int
    columns: list[tuple[str, str]] = field(default_factory=list)
    gate_verdict: str = "OK"
    gate_message: str = ""
    coercion_failures: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # what the load did that a person should know
    #: Per column, per token: values read as NULL because they matched a missing-value token.
    #: Blank fields are not listed -- every reader makes them null (Cleanup Step 12, RF-O4).
    null_tokens: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def coercion_total(self) -> int:
        return sum(self.coercion_failures.values())

    def summary(self) -> str:
        lines = [
            f"Loaded {self.dataset_name}: {self.row_count:,} rows, "
            f"{self.column_count} columns.",
        ]
        if self.gate_message:
            lines.append(f"NOTE: {self.gate_message}")
        if self.null_tokens:
            total = sum(n for per in self.null_tokens.values() for n in per.values())
            said = "; ".join(
                f"{col} (" + ", ".join(f"{tok!r} {n:,}" for tok, n in sorted(
                    per.items(), key=lambda kv: (-kv[1], kv[0]))) + ")"
                for col, per in self.null_tokens.items())
            lines.append(
                f"NOTE: {total:,} value(s) matched a missing-value token and were read as NULL: "
                f"{said}. A token is data until someone says it means absent -- reload with "
                f"na_values=[] to keep them as text, or na_values=[...] to choose which.")
        if self.coercion_failures:
            worst = ", ".join(
                f"{c} ({n:,})"
                for c, n in sorted(
                    self.coercion_failures.items(), key=lambda kv: -kv[1]
                )
            )
            lines.append(
                f"NOTE: {self.coercion_total:,} value(s) did not fit their "
                f"column and were stored as NULL: {worst}. The rows were kept. "
                f"Reload with on_error='stop' to see the first one, or force "
                f"those columns to text."
            )
        lines += [f"NOTE: {n}" for n in self.notes]
        lines.append("")
        lines.append("| column | type |")
        lines.append("| --- | --- |")
        for name, dtype in self.columns:
            lines.append(f"| {name} | {dtype} |")
        return "\n".join(lines)


class LoadRefused(Exception):
    """Raised when a load must not proceed. The message is user-facing and
    always carries a NEXT STEP -- see guide 8.2."""


def validate_dataset_name(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise LoadRefused(
            f"BLOCKED: {name!r} is not a usable table name.\n"
            f"WHY: it becomes a SQL identifier, so it must start with a letter "
            f"and contain only letters, digits and underscores.\n"
            f"NEXT STEP: try something like 'sales_2024'."
        )
    return name


def validate_on_error(mode: str) -> str:
    if mode not in ON_ERROR_MODES:
        raise LoadRefused(
            f"BLOCKED: on_error={mode!r} is not a mode.\n"
            f"WHY: 'stop' refuses the load at the first value that does not "
            f"fit its column, naming the row. 'null' stores that cell as NULL, "
            f"keeps the row, and counts the failures per column.\n"
            f"NEXT STEP: pass one of {ON_ERROR_MODES}."
        )
    return mode


def _sql_path(path: Path) -> str:
    """Single-quoted SQL literal for a path. Doubles any embedded quote."""
    return "'" + str(path).replace("'", "''") + "'"


def _q(name: str) -> str:
    """Double-quoted SQL identifier. Doubles any embedded quote."""
    return '"' + name.replace('"', '""') + '"'


# Bytes read from each end of a file to settle its encoding and, at the end, how it finishes.
# A seek, so the cost is the same on a 10 KB file and on a 2.5 GB one (P14-O3, P14-O9).
PROBE_BYTES = 64 * 1024

UTF8, UTF16, CP1252, LATIN1 = "utf-8", "utf-16", "cp1252", "latin-1"


def _decodes_as_utf8(chunk: bytes, *, from_middle: bool) -> bool:
    """Whether a chunk is UTF-8, allowing a character cut at either edge of the chunk."""
    if from_middle:
        i = 0
        while i < min(3, len(chunk)) and 0x80 <= chunk[i] <= 0xBF:  # continuation bytes
            i += 1
        chunk = chunk[i:]
    try:
        codecs.getincrementaldecoder("utf-8")().decode(chunk, final=False)
        return True
    except UnicodeDecodeError:
        return False


def _decodes_as(chunk: bytes, encoding: str) -> bool:
    try:
        codecs.getincrementaldecoder(encoding)().decode(chunk, final=False)
        return True
    except UnicodeDecodeError:
        return False


def sniff_encoding(path: Path | str) -> str:
    """utf-8, utf-16, cp1252 or latin-1, from a byte-order mark and the first and last PROBE_BYTES.

    UTF-16 is recognised by its byte-order mark (Excel's "Unicode text" export writes one).
    Otherwise UTF-8 if both probes decode as UTF-8; else Windows-1252, the encoding Windows
    exports use, if they decode as that (it leaves five bytes undefined); else Latin-1, which
    every byte decodes as. Latin-1 alone was wrong for the euro sign and curly quotes, and DuckDB
    refused such a file outright ("File is not latin-1 encoded", measured, P14-D47).
    """
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(PROBE_BYTES)
        tail = b""
        if size > PROBE_BYTES:
            f.seek(max(PROBE_BYTES, size - PROBE_BYTES))
            tail = f.read()
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return UTF16
    if _decodes_as_utf8(head, from_middle=False) and (
            not tail or _decodes_as_utf8(tail, from_middle=True)):
        return UTF8
    if _decodes_as(head, CP1252) and _decodes_as(tail, CP1252):
        return CP1252
    return LATIN1


def mixed_line_endings(path: Path | str) -> bool:
    """Whether the first PROBE_BYTES end lines both as CRLF and as a bare LF.

    DuckDB's strict parser refuses such a file ("state machine reached an invalid state", or a
    failed sniff -- measured on both, P14-D54); two exports pasted together, or comment lines
    added by hand, produce one.
    """
    with Path(path).open("rb") as f:
        head = f.read(PROBE_BYTES)
    crlf = head.count(b"\r\n")
    return bool(crlf) and head.count(b"\n") > crlf


def to_utf8(path: Path, encoding: str) -> tuple[Path, str]:
    """A temporary UTF-8 copy with every line ending LF, and the encoding it was read as.

    DuckDB's reader takes UTF-8, UTF-16 and Latin-1 only (measured: 'windows-1252' is "not
    supported"), so every other file is transcoded here, streamed a megabyte at a time. A cp1252
    guess that meets an undefined byte further in is redone as Latin-1, which cannot fail.
    The caller deletes the copy.
    """
    fd, tmp = tempfile.mkstemp(suffix=".csv", prefix="agent_utf8_")
    os.close(fd)
    target = Path(tmp)
    for enc in ([encoding, LATIN1] if encoding == CP1252 else [encoding]):
        try:
            decoder = codecs.getincrementaldecoder(enc)()
            carry = ""  # a CR at the end of one chunk may be the first half of a CRLF
            with path.open("rb") as src, target.open("w", encoding=UTF8, newline="") as out:
                while chunk := src.read(1 << 20):
                    text = carry + decoder.decode(chunk)
                    carry = "\r" if text.endswith("\r") else ""
                    text = text[:-1] if carry else text
                    out.write(text.replace("\r\n", "\n").replace("\r", "\n"))
                out.write((carry + decoder.decode(b"", final=True))
                          .replace("\r\n", "\n").replace("\r", "\n"))
            return target, enc
        except UnicodeDecodeError:
            continue
    target.unlink(missing_ok=True)
    raise LoadRefused(f"BLOCKED: {path.name} could not be decoded as {encoding}.\n"
                      f"NEXT STEP: save the file as UTF-8 and upload it again.")


def tail_lines(path: Path | str, n: int) -> list[str]:
    """The lines of the file's last PROBE_BYTES, from a record boundary -- read with a seek,
    never the whole file. The caller parses them and keeps the last `n` non-blank RECORDS: a
    quoted field may span lines, so lines are not records. Blank records are not rows (DuckDB
    skips blank lines, measured) and must not be counted as a footer.
    """
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        start = max(0, size - PROBE_BYTES)
        f.seek(start)
        raw = f.read()
    encoding = sniff_encoding(path)
    if encoding == UTF16:
        # Mid-file there is no byte-order mark: take the order from the file's first two bytes,
        # and start on a character boundary, which in UTF-16 is an even offset.
        with path.open("rb") as f:
            bom = f.read(2)
        if start % 2:
            raw = raw[1:]
        encoding = "utf-16-le" if bom == b"\xff\xfe" else "utf-16-be"
        if start == 0:
            raw = raw[2:]
    text = raw.decode(encoding, errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        # The probe may begin inside a quoted field that spans lines. The file's end is outside
        # every quote, so a line boundary is outside one exactly when an even number of quote
        # characters follows it: start at the first such boundary (P14-D51).
        after = text.count('"')
        for i, line in enumerate(lines):
            after -= line.count('"')
            if i >= 0 and after % 2 == 0:
                lines = lines[i + 1:]
                break
        else:
            lines = []
    return lines


def has_data_rows(path: Path | str, header_rows: int) -> bool:
    """Whether any non-blank line follows the header. Stops at the first one found."""
    path = Path(path)
    with path.open("r", encoding=sniff_encoding(path), errors="replace", newline="") as f:
        for i, line in enumerate(f):
            if i >= header_rows and line.strip():
                return True
    return False


def preview_lines(path: Path | str, n: int = CSV_PREVIEW_LINES) -> list[str]:
    """First n lines as raw text. Never reads the whole file.

    Locked decision 4. This is what the header conversation in Phase 3 reads,
    and why a 1.5 GB file previews instantly: it stops after n lines rather
    than scanning to the end.
    """
    path = Path(path)
    out: list[str] = []
    # Decoded as the load will decode it, so a Latin-1 file previews as 'Sünd', not 'S?nd'.
    with path.open("r", encoding=sniff_encoding(path), errors="replace", newline="") as f:
        for i, line in enumerate(f):
            if i >= n:
                break
            out.append(line.rstrip("\r\n"))
    return out


def detect_column_count(
    con: duckdb.DuckDBPyConnection, path: Path, skip: int = 0, encoding: str = UTF8,
    padding: bool = False, header: bool = False,
) -> int:
    """Column count without reading any rows.

    LIMIT 0 makes DuckDB sniff the structure and return no data, so this costs
    a few kilobytes regardless of file size.
    """
    enc = f", encoding='{encoding}'" if encoding != UTF8 else ""
    enc += ", null_padding=true" if padding else ""
    where = "header=true" if header else f"skip={skip}, header=false"
    rel = con.execute(
        f"SELECT * FROM read_csv({_sql_path(path)}, {where}, "
        f"sample_size=1024{enc}) LIMIT 0"
    )
    return len(rel.description)


def _sniff_types(
    con: duckdb.DuckDBPyConnection, read_expr: str
) -> list[tuple[str, str]]:
    """(name, DuckDB type) per column, sniffed without loading any rows."""
    rel = con.execute(f"SELECT * FROM {read_expr} LIMIT 0")
    return [(d[0], str(d[1])) for d in rel.description]


def load_csv(
    con: duckdb.DuckDBPyConnection,
    path: Path | str,
    dataset_name: str,
    header_rows: int = 1,
    names: list[str] | None = None,
    na_values: list[str] | None = None,
    delimiter: str | None = None,
    footer_skip_rows: int = 0,
    dtypes: dict[str, str] | None = None,
    on_error: str = ON_ERROR_STOP,
    sample_size: int = 20_480,
    replace: bool = True,
) -> LoadResult:
    """Load a delimited file into a DuckDB table and register it.

    header_rows       How many rows at the top are headers, not data. 1 is the
                      ordinary case. 2 or more requires `names`, because DuckDB
                      cannot infer a column name spread over several rows.
    names             Explicit column names. Required when header_rows != 1.
                      Must match the file's column count exactly.
    na_values         Tokens to read as NULL. Defaults to
                      config.DEFAULT_NA_VALUES. An empty list means no token
                      is null. Empty FIELDS are already NULL in DuckDB either
                      way, so this only ever affects a literal token such as
                      'N/A' or '-'.
    delimiter         Field separator. Sniffed when omitted.
    footer_skip_rows  Data rows at the END of the file to drop -- a totals row,
                      a 'generated on' line. Costs one extra count(*) pass,
                      because read_csv has no skipfooter and the row total has
                      to be known before the tail can be cut.
    dtypes            Pin specific columns to a type, e.g.
                      {'order_id': 'VARCHAR'}. Columns not named here are
                      inferred as usual.
    on_error          'stop' (default) leaves DuckDB to refuse a value that
                      will not convert. 'null' stores that cell as NULL, keeps
                      the row, and counts the failures per column into
                      LoadResult.coercion_failures -- F9.
    sample_size       Rows scanned to infer types. DuckDB's default is 20480.
                      Pass -1 to scan everything -- correct but slow on a large
                      file, and worth it when a column turns to text partway
                      down.
    """
    path = Path(path)
    validate_dataset_name(dataset_name)
    validate_on_error(on_error)

    gate = sizegate.check_file(path, "csv")
    if not gate.allowed:
        raise LoadRefused(gate.message)

    if header_rows < 0:
        raise LoadRefused(
            f"BLOCKED: header_rows cannot be negative (got {header_rows}).\n"
            f"NEXT STEP: use 0 for a file with no header, 1 for the usual case."
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
            f"WHY: with more than one header row DuckDB cannot work out the "
            f"names itself, and reading them as data types every column "
            f"VARCHAR.\n"
            f"NEXT STEP: pass names=[...], one per column."
        )

    # A header with nothing under it. DuckDB sniffs an empty remainder as ONE column, so the
    # names check below used to refuse it as "9 names but 1 column" (P14-O11, B10).
    if not has_data_rows(path, header_rows):
        raise LoadRefused(
            f"BLOCKED: {path.name} has a header and no data rows.\n"
            f"WHY: there is nothing to load -- every line after the first "
            f"{header_rows} is blank or missing.\n"
            f"NEXT STEP: check the export finished, and upload the file with its rows."
        )

    encoding = sniff_encoding(path)
    notes: list[str] = []
    source, copies = path, []
    if encoding != UTF8:
        source, encoding = to_utf8(path, encoding)
        copies.append(source)
        notes.append(_encoding_note(path, encoding))
    elif mixed_line_endings(path):
        source, _ = to_utf8(path, UTF8)
        copies.append(source)
        notes.append(f"{path.name} ends some lines with CRLF and others with LF; it was read "
                     f"with every line ending made the same.")
    try:
        return _load_csv(con, path, source, copies, notes, dataset_name, header_rows, names,
                         na_values, delimiter, footer_skip_rows, dtypes, on_error, sample_size,
                         replace, gate)
    finally:
        for c in copies:
            c.unlink(missing_ok=True)


def _load_csv(con, path, source, copies, notes, dataset_name, header_rows, names, na_values,
              delimiter, footer_skip_rows, dtypes, on_error, sample_size, replace, gate):
    """load_csv's body, reading `source` (the file, or its UTF-8 copy) and naming `path`."""

    # Guard the silent-padding trap: fewer names than columns does not error
    # in DuckDB, it just appends 'column7'.
    if names:
        try:
            one = header_rows == 1
            actual = detect_column_count(con, source, skip=header_rows, header=one)
            # Rows with fewer fields than the header make the sniff read one column. Padded,
            # they match, and the short rows' missing trailing values load empty (P14-D56).
            if actual != len(names) and detect_column_count(
                    con, source, skip=header_rows, padding=True, header=one) == len(names):
                actual, padded = len(names), True
            else:
                padded = False
        except duckdb.Error as exc:
            raise LoadRefused(_duck_refusal(path, exc, source)) from exc
        if len(names) != actual:
            raise LoadRefused(
                f"BLOCKED: {len(names)} column names given but "
                f"{path.name} has {actual} columns after skipping "
                f"{header_rows} header row(s).\n"
                f"WHY: DuckDB does not reject a short list -- it silently names "
                f"the leftover column 'column{actual - 1}', which is easy to "
                f"miss.\n"
                f"NEXT STEP: supply exactly {actual} names, or check "
                f"header_rows is right."
            )

    nulls = list(na_values) if na_values is not None else list(DEFAULT_NA_VALUES)

    # An empty list is a legitimate "nothing is a null token", but DuckDB
    # rejects nullstr=[] outright: "requires a non-empty list of possible null
    # strings". Omitting the option is what an empty list has to mean.
    opts = [f"sample_size={sample_size}"]
    if names and padded:
        opts.append("null_padding=true")
        notes.append("Some rows have fewer fields than the header; their missing trailing "
                     "values load as empty.")
    if delimiter:
        opts.append(f"delim='{delimiter}'")
    if names and header_rows == 1:
        # One header RECORD, read as a record: skip=1 skips one LINE, and a header cell holding
        # a line break ("Units\nSold") left its second half to be read as data (P14-D63).
        opts.append("header=true")
        opts.append(f"names={names!r}")
    elif names:
        opts.append(f"skip={header_rows}")
        opts.append("header=false")
        opts.append(f"names={names!r}")
    else:
        opts.append("header=true" if header_rows == 1 else "header=false")
        if header_rows > 1:
            opts.append(f"skip={header_rows}")

    # The same read with every value as text and no token nulled: what the tokens were, counted
    # after the load (RF-O4). Built before nullstr joins the options, so it cannot drift from them.
    text_read = f'read_csv({_sql_path(source)}, {", ".join(opts + ["all_varchar=true"])})'
    if nulls:
        opts.append(f"nullstr={nulls!r}")
    read_expr = f'read_csv({_sql_path(source)}, {", ".join(opts)})'

    coercion: dict[str, int] = {}
    verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"

    try:
        try:
            coercion = _load(con, read_expr, dataset_name, dtypes, footer_skip_rows, verb,
                             on_error, path)
        except duckdb.Error as exc:
            # UTF-8 held at the probed ends and broke in the middle: transcode the whole file.
            if source != path or "unicode" not in str(exc).lower():
                raise
            copy, used = to_utf8(path, CP1252)
            copies.append(copy)
            notes.append(_encoding_note(path, used))
            read_expr = read_expr.replace(_sql_path(path), _sql_path(copy), 1)
            text_read = text_read.replace(_sql_path(path), _sql_path(copy), 1)
            source = copy
            coercion = _load(con, read_expr, dataset_name, dtypes, footer_skip_rows, verb,
                             on_error, path)
        ambiguous = _two_digit_year_dates(con, dataset_name, read_expr)
        huge = _huge_integer_columns(con, dataset_name, read_expr)
        if ambiguous or huge:
            types = ", ".join([f"'{c}': 'VARCHAR'" for c in ambiguous]
                              + [f"'{c}': 'HUGEINT'" for c in huge])
            read_expr = read_expr[:-1] + f", types={{{types}}})"
            coercion = _load(con, read_expr, dataset_name, dtypes, footer_skip_rows,
                             "CREATE OR REPLACE TABLE", on_error, path)
        if ambiguous:
            notes.append(f"{', '.join(ambiguous)} write{'s' if len(ambiguous) == 1 else ''} "
                         f"dates with a two-digit year (31/12/24), which the reader took as "
                         f"YEAR first (2031-12-24). Kept as text; propose_cleaning_plan offers "
                         f"the day-first or month-first reading the values support.")
        if huge:
            notes.append(f"{', '.join(huge)} hold{'s' if len(huge) == 1 else ''} whole numbers "
                         f"beyond 2^53, which a floating-point column cannot keep exactly; read as "
                         f"HUGEINT so every digit is kept.")
    except duckdb.Error as exc:
        raise LoadRefused(_duck_refusal(path, exc, source)) from exc

    dropped = _drop_trailing_empty(con, dataset_name)
    if dropped:
        notes.append(f"Every line ends with a delimiter, so the file has {len(dropped)} empty "
                     f"column(s) at the end with no name ({', '.join(dropped)}); dropped.")
    notes += db.utc_note(db.utc_timestamps(con, dataset_name))

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

    null_tokens = _count_null_tokens(
        con, text_read, [c for c, _t in columns], [t for t in nulls if t != ""],
        footer_skip_rows, path)

    db.register_dataset(
        con,
        dataset_name=dataset_name,
        source_type="csv",
        source_detail=str(path),
        row_count=rows,
        column_count=cols,
        notes=(
            f"header_rows={header_rows}, footer_skip_rows={footer_skip_rows}, "
            f"on_error={on_error}"
        ),
    )

    return LoadResult(
        dataset_name=dataset_name,
        row_count=rows,
        column_count=cols,
        columns=columns,
        gate_verdict=gate.verdict.value,
        gate_message=gate.message,
        coercion_failures=coercion,
        notes=list(dict.fromkeys(notes)),
        null_tokens=null_tokens,
    )


_ENCODING_NAMES = {UTF16: "UTF-16", CP1252: "Windows-1252 (Western European)",
                   LATIN1: "Latin-1 (Western European)"}


_AUTO_NAME = re.compile(r"^column_?\d+$")
_EXACT_DOUBLE = 2 ** 53


def _two_digit_year_dates(con, table: str, read_expr: str) -> list[str]:
    """DATE or TIMESTAMP columns whose text has a two-digit year.

    DuckDB's sniffer reads '31/12/24' as %y/%m/%d, 2031-12-24 -- a valid date, loaded in silence,
    every one wrong (measured, P14-D60). A two-digit year comes last in the day-first and
    month-first conventions that write one, so such a column is kept as text for cleaning to read.
    """
    dated = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' "
        "AND table_name = ? AND data_type IN ('DATE', 'TIMESTAMP')", [table]).fetchall()]
    if not dated:
        return []
    text = read_expr[:-1] + ", all_varchar=true)"
    hits = con.execute("SELECT " + ", ".join(
        f"count(*) FILTER (WHERE regexp_full_match(trim({_q(c)}), "
        f"'\\d{{1,2}}[/.-]\\d{{1,2}}[/.-]\\d{{2}}([ T].*)?'))" for c in dated)
        + f" FROM {text}").fetchone()
    return [c for c, n in zip(dated, hits) if n]


def _huge_integer_columns(con, table: str, read_expr: str) -> list[str]:
    """DOUBLE columns reaching 2^53 whose text is whole numbers: DuckDB's sniffer offers no
    HUGEINT ("not accepted as a valid input", measured), so 10**19 + 7 loaded as 1e19 and a sum
    was off by 34,650 (P14-D57). One aggregate per load; the text is read only on a hit."""
    doubles = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' "
        "AND table_name = ? AND data_type = 'DOUBLE'", [table]).fetchall()]
    if not doubles:
        return []
    big = con.execute("SELECT " + ", ".join(
        f"coalesce(max(abs({_q(c)})) >= {_EXACT_DOUBLE}, false)" for c in doubles)
        + f" FROM {_q(table)}").fetchone()
    hits = [c for c, b in zip(doubles, big) if b]
    if not hits:
        return []
    text = read_expr[:-1] + ", all_varchar=true)"
    whole = con.execute("SELECT " + ", ".join(
        f"count(*) FILTER (WHERE {_q(c)} IS NOT NULL "
        f"AND NOT regexp_full_match(trim({_q(c)}), '[-+]?[0-9]+'))" for c in hits)
        + f" FROM {text}").fetchone()
    return [c for c, n in zip(hits, whole) if n == 0]


def _drop_trailing_empty(con, table: str) -> list[str]:
    """Drop columns at the END that had no header name and hold no value: what a delimiter at
    the end of every line makes (P14-D56). A named empty column is kept -- it may be the one
    that matters and the feed is broken -- as is an unnamed one with any value in it."""
    cols = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' "
        "AND table_name = ? ORDER BY ordinal_position", [table]).fetchall()]
    dropped = []
    while len(cols) > 1 and _AUTO_NAME.match(cols[-1]):
        filled = con.execute(f"SELECT count({_q(cols[-1])}) FROM {_q(table)}").fetchone()[0]
        if filled:
            break
        con.execute(f"ALTER TABLE {_q(table)} DROP COLUMN {_q(cols[-1])}")
        dropped.append(cols.pop())
    return list(reversed(dropped))


def _encoding_note(path: Path, encoding: str) -> str:
    return (f"{path.name} is not UTF-8, so it was read as {_ENCODING_NAMES[encoding]}. If "
            f"letters look wrong, the file uses another encoding: save it as UTF-8 and upload "
            f"it again.")


def _duck_refusal(path: Path, exc: Exception, source: Path | None = None) -> str:
    """DuckDB's reason, without the server's paths or the SQL it was running.

    Its message names the file's absolute path and echoes the query ('LINE 1: SELECT * FROM
    rea...'); both reached a web visitor through the Latin-1 refusal (P14-O9, B7). The lines
    before the first blank one, and the 'Possible Solution' lines, are what explain it.
    """
    kept = []
    for line in str(exc).splitlines():
        stripped = line.strip()
        if stripped.startswith(("LINE ", "file =")) or stripped.startswith("^"):
            break
        if stripped:
            kept.append(stripped)
    reason = " ".join(kept)
    for p in {path, path.resolve(), *((source, Path(source).resolve()) if source else ())}:
        reason = reason.replace(str(p), path.name)
    return (
        f"BLOCKED: DuckDB could not read {path.name}.\n"
        f"DuckDB said: {reason}\n"
        f"NEXT STEP: check the delimiter and the number of header rows. "
        f"Call preview_lines() to see the first lines as they actually "
        f"are. If a single bad value is the problem, on_error='null' "
        f"stores it as NULL and counts it instead of stopping."
    )


def _load(con, read_expr, dataset_name, dtypes, footer_skip_rows, verb, on_error, path):
    """One attempt at the load. Returns the coercion counts (empty unless on_error='null')."""
    if on_error == ON_ERROR_NULL:
        return _load_with_coercion_counts(
            con, read_expr, dataset_name, dtypes, footer_skip_rows, verb
        )
    select = "SELECT * FROM " + read_expr
    if dtypes:
        sniffed = _sniff_types(con, read_expr)
        select = (
            "SELECT "
            + ", ".join(
                f"CAST({_q(c)} AS {dtypes[c]}) AS {_q(c)}"
                if c in dtypes
                else _q(c)
                for c, _t in sniffed
            )
            + " FROM "
            + read_expr
        )
    if footer_skip_rows:
        select = _apply_footer_skip(con, select, read_expr, footer_skip_rows, path)
    con.execute(f"{verb} {_q(dataset_name)} AS {select}")
    return {}


def _count_null_tokens(con, text_read: str, columns: list[str], tokens: list[str],
                       footer_skip_rows: int, path: Path) -> dict[str, dict[str, int]]:
    """How many values each missing-value token turned into NULL, per column.

    One more pass over the file, read as text with no token nulled. The load itself cannot say:
    after it, a nulled 'NA' and an empty field are the same NULL. The retail run lost 8,069 'NA'
    and 5,353 '-' from rating this way with nothing in the reply (Cleanup Step 12, RF-O4).
    """
    if not tokens or not columns:
        return {}
    select = f"SELECT * FROM {text_read}"
    if footer_skip_rows:
        select = _apply_footer_skip(con, select, text_read, footer_skip_rows, path)
    pairs = [(c, t) for c in columns for t in tokens]
    exprs = ", ".join(
        f"count(*) FILTER (WHERE {_q(c)} = '{t.replace(chr(39), chr(39) * 2)}')" for c, t in pairs)
    row = con.execute(f"SELECT {exprs} FROM ({select})").fetchone()
    out: dict[str, dict[str, int]] = {}
    for (c, t), n in zip(pairs, row):
        if n:
            out.setdefault(c, {})[t] = n
    return out


def _apply_footer_skip(
    con: duckdb.DuckDBPyConnection,
    select: str,
    read_expr: str,
    footer_skip_rows: int,
    path: Path,
) -> str:
    """Wrap a SELECT so the last `footer_skip_rows` data rows are dropped.

    read_csv has no skipfooter, so the total has to be counted first. A scan
    preserves file order (verified at threads=8 on 20,000 rows), so LIMIT keeps
    the first n rows rather than an arbitrary n.
    """
    total = con.execute(f"SELECT count(*) FROM {read_expr}").fetchone()[0]
    keep = total - footer_skip_rows
    if keep < 0:
        raise LoadRefused(
            f"BLOCKED: footer_skip_rows={footer_skip_rows} but {path.name} has "
            f"only {total} data row(s).\n"
            f"NEXT STEP: lower footer_skip_rows, or check header_rows is right."
        )
    return f"{select} LIMIT {keep}"


def _load_with_coercion_counts(
    con: duckdb.DuckDBPyConnection,
    read_expr: str,
    dataset_name: str,
    dtypes: dict[str, str] | None,
    footer_skip_rows: int,
    verb: str,
) -> dict[str, int]:
    """
    Load with cell-level coercion, counting what did not fit.

    DuckDB's own store_rejects reports per-column counts but drops the whole
    row and writes into persistent side tables. TRY_CAST is used instead: the
    offending cell becomes NULL, the row survives, and the count is one extra
    aggregate over the staged text.

    Target types come from `dtypes` where given and from DuckDB's own sniff
    otherwise. That matters because inference WIDENS -- a column of integers
    containing one 'oops' is sniffed as VARCHAR, and nothing can fail to
    convert to VARCHAR. Pinning the type is what makes a failure countable.
    """
    sniffed = _sniff_types(con, read_expr)
    targets = {c: (dtypes or {}).get(c, t) for c, t in sniffed}

    staging = f"_stage_{dataset_name}"
    text_expr = read_expr.replace("read_csv(", "read_csv(", 1)
    # all_varchar keeps every value as text so TRY_CAST has something to fail
    # against; without it DuckDB has already widened the column.
    text_expr = text_expr[:-1] + ", all_varchar=true)"

    # The footer is trimmed HERE, before anything is counted. Counting first
    # and trimming afterwards reports a coercion failure for a 'TOTAL' row
    # that is then thrown away -- verified: it counted id=1 on a file whose
    # only bad value was in the footer.
    stage_select = f"SELECT * FROM {text_expr}"
    if footer_skip_rows:
        total = con.execute(f"SELECT count(*) FROM {text_expr}").fetchone()[0]
        keep = total - footer_skip_rows
        if keep < 0:
            raise LoadRefused(
                f"BLOCKED: footer_skip_rows={footer_skip_rows} but the file "
                f"has only {total} data row(s).\n"
                f"NEXT STEP: lower footer_skip_rows."
            )
        stage_select = f"{stage_select} LIMIT {keep}"

    con.execute(f"CREATE OR REPLACE TEMP TABLE {_q(staging)} AS {stage_select}")
    try:
        countable = [
            (c, t) for c, t in targets.items() if t.upper() not in ("VARCHAR", "TEXT")
        ]
        coercion: dict[str, int] = {}
        if countable:
            exprs = ", ".join(
                f"count(*) FILTER (WHERE {_q(c)} IS NOT NULL "
                f"AND TRY_CAST({_q(c)} AS {t}) IS NULL) AS {_q(c)}"
                for c, t in countable
            )
            row = con.execute(f"SELECT {exprs} FROM {_q(staging)}").fetchone()
            coercion = {c: n for (c, _t), n in zip(countable, row) if n}

        select = "SELECT " + ", ".join(
            f"TRY_CAST({_q(c)} AS {t}) AS {_q(c)}" if t.upper() not in ("VARCHAR", "TEXT")
            else _q(c)
            for c, t in targets.items()
        ) + f" FROM {_q(staging)}"

        con.execute(f"{verb} {_q(dataset_name)} AS {select}")
        return coercion
    finally:
        con.execute(f"DROP TABLE IF EXISTS {_q(staging)}")
