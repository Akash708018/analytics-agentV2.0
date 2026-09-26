"""
Work out how to read a file, and say why.

Phase 2 already answers "what is in the top of this file": `preview_lines` for
CSV, `preview_rows` and `list_sheets` for Excel. This module answers the harder
question on top of them -- where the header actually is, how its rows should be
combined, and whether the file is a pivot table dump that will not analyse well
in the shape it arrives in.

Everything here produces a *proposal*, never a decision. Each proposal carries
the reasons that produced it, and those reasons end up in
`IngestSpec.assumptions` so the user reads them before confirming rather than
discovering them afterwards.

The confidence levels mean something specific:

    high    one contiguous block of text rows sits directly above rows with a
            different type profile, and every column is named.
    medium  the block needed evidence beyond the type change to resolve -- a
            merge covering the sparse upper row, say.
    low     something is genuinely ambiguous and the user has to say. A CSV
            with a sparse upper header row is always low, because no merge
            metadata exists to settle it (locked decision 10).

`low` is not a failure. It is the module refusing to guess, which is the
behaviour the Phase 3 Done-When asks for on multiheader.csv.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import re
from dataclasses import dataclass, field, replace

from backend.engine.ingest.headers import JOIN_MODES, assemble_names
from backend.engine.ingest.merges import merged_ranges, merges_on_rows
from backend.engine.ingest.spec import IngestSpec

# A row this sparse relative to the widest row is a title, not a header,
# unless merge evidence says otherwise.
SPARSE_RATIO = 0.5

# Fraction of columns that must look like a period before a file is called a
# pivot dump.
PIVOT_COLUMN_RATIO = 0.5

# A trailing row this sparse relative to the data width is a note or a totals
# line, not a record.
FOOTER_FILL_RATIO = 0.5

# How many rows at the end of a sheet are examined for a footer.
FOOTER_SCAN_ROWS = 20

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    "january|february|march|april|june|july|august|september|october|"
    "november|december"
)
_PERIOD_RE = re.compile(
    rf"^\s*(?:(?:{_MONTHS})[a-z]*[\s\-_/]*\d{{0,4}}"
    rf"|(?:19|20)\d{{2}}(?:[\s\-_/]*(?:q[1-4]|h[12]|\d{{1,2}}))?"
    rf"|q[1-4][\s\-_/]*(?:19|20)?\d{{0,4}}"
    rf"|fy\s?\d{{2,4}})\s*$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# cell classification
# --------------------------------------------------------------------------

def _is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _is_texty(v) -> bool:
    """
    True when a cell reads as a label rather than a measurement.

    A blank counts as texty: a header row with gaps under merges is still a
    header row. A string that parses as a number does not -- '1000' in a CSV
    is data even though csv gives it to us as str.
    """
    if _is_blank(v):
        return True
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float, _dt.date, _dt.datetime, _dt.time)):
        return False
    text = str(v).strip()
    try:
        float(text.replace(",", ""))
        return False
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            _dt.datetime.strptime(text, fmt)
            return False
        except ValueError:
            continue
    return True


def _fill(row) -> int:
    return sum(1 for v in row if not _is_blank(v))


def _as_year(v) -> int | None:
    s = str(v).strip()
    if isinstance(v, float) and v.is_integer():
        s = str(int(v))
    return int(s) if len(s) == 4 and s.isdigit() and 1900 <= int(s) <= 2100 else None


def _year_header(rows, i: int) -> bool:
    """A row of labels whose number cells are all distinct years -- 'region, 2021, 2022, 2023',
    a pivot's header -- above a row whose same cells are not years. Read as data, it made such a
    file "headerless" (P14-D53)."""
    row = rows[i]
    filled = [(j, v) for j, v in enumerate(row) if not _is_blank(v)]
    numbers = [(j, v) for j, v in filled if not _is_texty(v)]
    years = [_as_year(v) for _, v in numbers]
    if len(numbers) < 2 or None in years or len(set(years)) != len(years):
        return False
    if len(numbers) == len(filled) or i + 1 >= len(rows):
        return False
    below = rows[i + 1]
    return not all(j < len(below) and _as_year(below[j]) for j, _ in numbers)


def _headerish(rows, i: int) -> bool:
    return _all_texty(rows[i]) or _year_header(rows, i)


def _all_texty(row) -> bool:
    return _fill(row) > 0 and all(_is_texty(v) for v in row)


# --------------------------------------------------------------------------
# the guess
# --------------------------------------------------------------------------

@dataclass
class HeaderGuess:
    header_rows: list[int]
    data_start_row: int
    confidence: str
    reasons: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return self.confidence == "low" or bool(self.questions)


def guess_header(
    rows: list,
    *,
    source_type: str,
    merge_refs: list[str] | None = None,
) -> HeaderGuess:
    """
    Propose which rows form the header.

    `rows` is the preview: row 1 of the file at index 0. Only the preview is
    read, so this stays cheap on a 1.6 GB file.

    The rule, in order:

    1. Find the first row that contains a value which is not texty. That is
       the first data row.
    2. Walk upwards from it while rows are non-blank and entirely texty. That
       contiguous run is the header candidate. A blank row stops the walk,
       which is what separates a title block from the header beneath it.
    3. If the top row of that run is sparse compared to the widest row in it,
       decide whether it is a spanning header or a stray title. On Excel a
       merge covering it settles the question. On CSV nothing settles it, so
       say so.
    """
    if source_type not in ("csv", "excel"):
        raise ValueError(f"source_type must be 'csv' or 'excel', got {source_type!r}")
    if source_type == "csv" and merge_refs:
        raise ValueError("CSV files carry no merge metadata; merge_refs must be empty.")
    if not rows:
        raise ValueError("No preview rows; the file appears to be empty.")

    reasons: list[str] = []
    questions: list[str] = []

    first_data_idx = None
    for i, row in enumerate(rows):
        if not _is_blank_row(row) and not _headerish(rows, i):
            first_data_idx = i
            break

    if first_data_idx is None:
        # Every previewed row is text. One header row, everything else data.
        top = _first_non_blank_index(rows)
        if top is None:
            raise ValueError("Every previewed row is blank.")
        return HeaderGuess(
            header_rows=[top + 1],
            data_start_row=top + 2,
            confidence="low",
            reasons=[
                f"Every row in the preview is text, so no type change marks "
                f"where data begins. Assumed row {top + 1} is the header."
            ],
            questions=[
                "Is row {} the header, and is every row below it data?".format(top + 1)
            ],
        )

    if first_data_idx == 0:
        return HeaderGuess(
            header_rows=[],
            data_start_row=1,
            confidence="medium",
            reasons=["Row 1 already contains non-text values, so there is no header."],
            questions=["This file looks headerless. Supply column names?"],
        )

    start = first_data_idx
    while start > 0 and _headerish(rows, start - 1) and not _is_blank_row(rows[start - 1]):
        start -= 1

    block = list(range(start, first_data_idx))
    reasons.append(
        f"Row {first_data_idx + 1} is the first row containing non-text values, "
        f"so data starts there."
    )
    if start > 0 and _is_blank_row(rows[start - 1]):
        reasons.append(
            f"Row {start} is blank, which separates the header from whatever is "
            f"above it."
        )

    widest = max(_fill(rows[i]) for i in block)
    confidence = "high"

    while len(block) > 1:
        top = block[0]
        if _fill(rows[top]) >= widest * SPARSE_RATIO:
            break
        verdict, why = _sparse_row_verdict(
            rows[top], top + 1, source_type, merge_refs, widest
        )
        reasons.append(why)
        if verdict == "header":
            confidence = "medium" if confidence == "high" else confidence
            break
        if verdict == "title":
            block = block[1:]
            confidence = "medium" if confidence == "high" else confidence
            continue
        # ambiguous
        confidence = "low"
        questions.append(
            f"Is row {top + 1} part of the header, or a title? "
            f"Answer decides whether the column names carry its labels."
        )
        break

    if len(block) > 1 and confidence == "high":
        confidence = "medium"
        reasons.append(
            f"Rows {block[0] + 1}-{block[-1] + 1} are all text with no blank "
            f"between them, so they were read as one header block."
        )

    header_rows = [i + 1 for i in block]
    return HeaderGuess(
        header_rows=header_rows,
        data_start_row=first_data_idx + 1,
        confidence=confidence,
        reasons=reasons,
        questions=questions,
    )


def _is_blank_row(row) -> bool:
    return all(_is_blank(v) for v in row)


def _first_non_blank_index(rows) -> int | None:
    for i, row in enumerate(rows):
        if not _is_blank_row(row):
            return i
    return None


def _sparse_row_verdict(row, row_num_1idx, source_type, merge_refs, widest):
    """Is a thinly-filled top row a spanning header or a stray title?"""
    filled = _fill(row)
    if source_type == "excel":
        covering = merges_on_rows(merge_refs or [], [row_num_1idx])
        if covering:
            return (
                "header",
                f"Row {row_num_1idx} has only {filled} of {widest} cells filled, "
                f"but {len(covering)} merged range(s) sit on it "
                f"({', '.join(covering)}), so its labels span columns. Read as "
                f"part of the header.",
            )
        return (
            "title",
            f"Row {row_num_1idx} has only {filled} of {widest} cells filled and "
            f"no merged ranges, so it reads as a title rather than a header.",
        )
    return (
        "ambiguous",
        f"Row {row_num_1idx} has only {filled} of {widest} cells filled. In a "
        f"CSV there is no merge information to say whether those gaps are a "
        f"spanning label or empty columns, so nothing was assumed.",
    )


# --------------------------------------------------------------------------
# footer detection -- Excel only
# --------------------------------------------------------------------------

def count_trailing_junk(tail_rows: list, width: int) -> int:
    """
    How many rows at the end are notes or totals rather than records.

    Counts upwards from the last row while each one is blank or filled in
    fewer than half its columns. A record with a couple of empty fields is not
    junk; a one-cell "Source: internal ERP extract." line is.

    This is Excel-only by design. The row count of a sheet is known, so reading
    the last twenty rows is free. A CSV would have to be seeked to the end, and
    on a 1.6 GB file that is not free -- so there the question is asked instead
    of answered.
    """
    if width <= 0:
        return 0
    threshold = width * FOOTER_FILL_RATIO
    # Columns every ordinary row in the tail fills. A totals row leaves some of them empty (the
    # id, the date); a data row that happens to carry the label "Total" fills them all, and is
    # data (P14-D48).
    ordinary = [r for r in tail_rows if _fill(r) >= threshold and not is_totals_row(r)]
    always = ({i for i in range(width)
               if all(i < len(r) and not _is_blank(r[i]) for r in ordinary)}
              if ordinary else set())
    count = 0
    for row in reversed(tail_rows):
        labelled = is_totals_row(row) and (
            not always or any(i >= len(row) or _is_blank(row[i]) for i in always))
        if _fill(row) >= threshold and not labelled:
            break
        count += 1
    return count


# A row that labels itself a total, however many of its cells are filled. A totals row under a
# nine-column CSV filled in three cells was caught by the fill rule; one filled in five was not,
# and either doubles every sum it is loaded into (P14-O3, B1).
_TOTALS_LABEL = re.compile(r"^(grand\s+|sub-?)?totals?\b|^sum\b", re.IGNORECASE)


def is_totals_row(row) -> bool:
    """Whether the row's first filled cell is a totals label: Total, Grand total, Subtotal, Sum."""
    first = next((_c(v) for v in row if not _is_blank(v)), "")
    return bool(_TOTALS_LABEL.match(first))


def describe_footer(n: int, tail_rows: list) -> str:
    """The assumption line for a detected footer."""
    shown = []
    for row in tail_rows[-n:]:
        first = next((_c(v) for v in row if not _is_blank(v)), "")
        shown.append(f"'{first[:40]}'" if first else "(blank)")
    return (
        f"The last {n} row{'s' if n != 1 else ''} of the file "
        f"({', '.join(shown)}) read as totals or notes rather than records -- "
        f"labelled as a total, or filled in fewer than half their columns -- so "
        f"footer_skip_rows is set to {n}. Say so if any of them is real data."
    )


def _c(v) -> str:
    return str(v).strip()


# --------------------------------------------------------------------------
# join proposal
# --------------------------------------------------------------------------

def propose_join(header_rows_values: list) -> tuple[str, str]:
    """
    Propose a header_join, with the reason.

    If the bottom row already names every column uniquely, the rows above it
    are groupings and add length without adding information -- `bottom_only`.
    If collapsing to the bottom row would create duplicates, those upper rows
    are load-bearing and must be joined in.
    """
    if len(header_rows_values) <= 1:
        return "space", "Single header row, so the join mode has no effect."

    bottom = [str(v).strip() for v in header_rows_values[-1] if not _is_blank(v)]
    full_width = len(header_rows_values[-1])

    if len(bottom) == full_width and len(set(bottom)) == full_width:
        return (
            "bottom_only",
            f"The bottom header row already names all {full_width} columns "
            f"uniquely, so the rows above it are groupings. Proposed "
            f"bottom_only, which keeps the names short.",
        )

    dupes = sorted({v for v in bottom if bottom.count(v) > 1})
    detail = (
        f"collapsing to it would repeat {', '.join(repr(d) for d in dupes)}"
        if dupes
        else "it does not name every column"
    )
    return (
        "space",
        f"The bottom header row cannot stand alone -- {detail}. The upper "
        f"row(s) carry the distinction, so they are joined in.",
    )


# --------------------------------------------------------------------------
# pivot dump detection -- build guide 6.4
# --------------------------------------------------------------------------

@dataclass
class PivotVerdict:
    is_pivot_dump: bool
    period_columns: list[str] = field(default_factory=list)
    message: str = ""


def detect_pivot_dump(names: list[str]) -> PivotVerdict:
    """
    Flag a file whose columns are periods rather than variables.

    `region, Jan, Feb, Mar, Apr` is a report, not a dataset. It loads fine and
    then resists every question worth asking, because the thing you want to
    group by is spread across column names.
    """
    if not names:
        return PivotVerdict(False)

    periods = [n for n in names if _PERIOD_RE.match(str(n))]
    if len(periods) < 3 or len(periods) / len(names) < PIVOT_COLUMN_RATIO:
        return PivotVerdict(False, periods)

    shown = ", ".join(periods[:4]) + (" ..." if len(periods) > 4 else "")
    return PivotVerdict(
        True,
        periods,
        f"{len(periods)} of {len(names)} columns are time periods ({shown}). "
        f"This looks like a pivot table export rather than a table of records. "
        f"It will load, but grouping by period means unpivoting those columns "
        f"into one period column and one value column first. Say if you want "
        f"it loaded as-is.",
    )


# --------------------------------------------------------------------------
# draft spec
# --------------------------------------------------------------------------

def sniff_delimiter(lines: list[str]) -> str:
    """The delimiter of these lines, "," when nothing can be sniffed."""
    text = "".join(l if l.endswith("\n") else l + "\n" for l in lines)
    try:
        return csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv_preview(lines: list[str], delimiter: str | None = None) -> list[list]:
    """Turn preview_lines output into rows. Sniffs the delimiter if not given."""
    text = "".join(l if l.endswith("\n") else l + "\n" for l in lines)
    if delimiter is None:
        delimiter = sniff_delimiter(lines)
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def draft_spec(
    rows: list,
    *,
    path: str,
    source_type: str,
    dataset_name: str,
    sheet: str | None = None,
    merge_refs: list[str] | None = None,
    delimiter: str | None = None,
    tail_rows: list | None = None,
    header_rows: list[int] | None = None,
    header_join: str | None = None,
    authorised_fill: bool = False,
) -> tuple[IngestSpec | None, HeaderGuess, PivotVerdict]:
    """
    Everything above, assembled.

    Returns (spec, guess, pivot).

    When the file cannot settle where the header is, a spec is still returned
    -- with a default filled in, the questions attached, and `unresolved`
    naming the guessed field. It is deliberately un-loadable in that state:
    `IngestSpec.is_confirmable` is False and `confirm_ingest_spec` refuses.

    That is a change from returning nothing. Returning nothing was safe and
    left the caller with no way to answer its own question except by
    hand-building a load, which is what happened in the Step 7 live run. A
    provisional spec keeps one mechanism -- edit the JSON, hand it back --
    while `unresolved` keeps the guarantee in the code rather than in a
    docstring.

    `header_rows`, `header_join` and `authorised_fill` are the answers coming
    back. Supplying header_rows overrides the guess and clears `unresolved`,
    because at that point a person has settled it.
    """
    guess = guess_header(rows, source_type=source_type, merge_refs=merge_refs)

    answered = header_rows is not None
    chosen_rows = header_rows if answered else guess.header_rows
    if not chosen_rows:
        return None, guess, PivotVerdict(False)

    unresolved: list[str] = []
    if guess.confidence == "low" and not answered:
        unresolved.append("header_rows")

    header_values = [list(rows[r - 1]) for r in chosen_rows]
    join, join_reason = propose_join(header_values)
    if authorised_fill and source_type == "csv" and header_join is None:
        # propose_join looks at the RAW rows, where the bottom row is unique,
        # so it proposes bottom_only -- which throws away the very labels the
        # authorisation was given to keep. Asking to use the group labels and
        # then discarding them would be absurd.
        join, join_reason = (
            "space",
            "The upper row was authorised as group labels, so it is joined "
            "into the names rather than discarded. bottom_only would have "
            "thrown away the labels you just confirmed.",
        )
    if header_join is not None:
        join = header_join
        join_reason = f"header_join was set to {join!r} rather than proposed."

    result = assemble_names(
        header_values,
        header_rows_1idx=chosen_rows,
        source_type=source_type,
        join=join,
        merge_refs=merge_refs if source_type == "excel" else None,
        authorised_fill=authorised_fill and source_type == "csv",
    )

    pivot = detect_pivot_dump(result.names)

    assumptions = list(guess.reasons) + [join_reason] + list(result.notes)
    if answered:
        assumptions.append(
            f"header_rows was given as {chosen_rows} rather than worked out "
            f"from the file, so the ambiguity above is settled."
        )

    # The end of a CSV is read too, now: its last lines are a seek away, not a scan, so the
    # size of the file never mattered (P14-O3). `tail_rows` is None only for a caller that
    # did not read the end, and then the draft says so.
    footer = 0
    if tail_rows:
        footer = count_trailing_junk(tail_rows, len(result.names))
        # Never the whole file: a tail that is all "junk" is data that happens to be sparse.
        if footer and footer >= len(tail_rows) - len(chosen_rows):
            footer = 0
        if footer:
            assumptions.append(describe_footer(footer, tail_rows))
    elif source_type == "csv":
        assumptions.append(
            "The end of this file was not examined. If it ends with totals "
            "or notes, say how many rows and they will be dropped."
        )

    if pivot.is_pivot_dump:
        assumptions.append(pivot.message)

    if answered:
        # Confidence describes how well the header is known, not how well the
        # FILE stated it. Leaving it at 'low' after a person has settled the
        # one ambiguity reports a doubt nobody holds any more.
        guess = replace(
            guess,
            confidence="high" if guess.confidence == "low" else guess.confidence,
            questions=[],
            reasons=list(guess.reasons),
        )

    spec = IngestSpec.from_header_result(
        result,
        path=path,
        source_type=source_type,
        dataset_name=dataset_name,
        sheet=sheet,
        header_rows=chosen_rows,
        data_start_row=max(guess.data_start_row, chosen_rows[-1] + 1),
        header_join=join,
        footer_skip_rows=footer,
        delimiter=delimiter if source_type == "csv" else None,
        authorised_fill=bool(authorised_fill and source_type == "csv"),
        questions=list(guess.questions) if unresolved else [],
        unresolved=unresolved,
    )
    spec.assumptions = assumptions
    return spec, guess, pivot


__all__ = [
    "HeaderGuess",
    "PivotVerdict",
    "count_trailing_junk",
    "describe_footer",
    "detect_pivot_dump",
    "draft_spec",
    "guess_header",
    "parse_csv_preview",
    "propose_join",
    "JOIN_MODES",
]
