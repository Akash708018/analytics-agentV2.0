"""
Turn raw header rows into final column names.

This module is the second half of the messy-header problem. `merges.py` finds
where the merged ranges are; this decides what the columns are called.

Three rules it exists to enforce:

1. Excel header rows are filled bounded by real merge ranges, never forward
   until the next non-empty cell (FMR F14).
2. CSV header rows are never filled at all. A CSV carries no merge metadata, so
   a blank in a header row could be a merge artifact or a genuinely empty
   column and nothing in the file distinguishes them. The blanks are reported
   as ambiguous and the caller asks (build guide 6.3, locked decision 10).
3. Nothing is silent. Blanks that had to be named, duplicates that had to be
   suffixed, and merges that were filled are all reported back so the draft
   spec can say what it did before anyone confirms it.

The output feeds `IngestSpec.columns[].source_name` and, through
`to_target_name`, `target_name`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.engine.ingest.merges import fill_bounded, merges_on_rows

# How the parts of a multi-row header are combined. Build guide 6.1.
JOIN_MODES = ("space", "underscore", "bottom_only", "top_only")

# Applied when a column has no name in any header row.
BLANK_NAME_TEMPLATE = "column_{n}"

# Applied to the 2nd and later occurrence of a repeated name.
DUPLICATE_SUFFIX_TEMPLATE = "{name}_{n}"


@dataclass
class HeaderResult:
    """Everything the caller needs to explain the names it is about to use."""

    names: list[str]
    filled_rows: list[list]
    blank_columns: list[int] = field(default_factory=list)
    renamed_duplicates: list[tuple[str, str]] = field(default_factory=list)
    filled_merges: list[str] = field(default_factory=list)
    authorised_fill: bool = False
    ambiguous_blanks: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        return len(self.names)


def _fill_forward(row) -> list:
    """Carry each value rightwards until the next non-blank one."""
    out, last = [], None
    for v in row:
        if not _is_empty(v):
            last = v
        out.append(last)
    return out


def _is_empty(v) -> bool:
    return _clean(v) == ""


def _clean(value) -> str:
    """A header cell as text. None, NaN-ish and whitespace all become ''."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "nan") else text


def _pad(rows: list[list], width: int) -> list[list]:
    return [list(r) + [None] * (width - len(r)) for r in rows]


def _join_parts(parts: list[str], join: str) -> str:
    if join == "space":
        return " ".join(p for p in parts if p)
    if join == "underscore":
        return "_".join(p for p in parts if p)
    if join == "bottom_only":
        return parts[-1] if parts else ""
    if join == "top_only":
        return parts[0] if parts else ""
    raise ValueError(f"header_join must be one of {JOIN_MODES}, got {join!r}")


def assemble_names(
    header_rows_values: list[list],
    *,
    header_rows_1idx: list[int],
    source_type: str,
    join: str = "space",
    merge_refs: list[str] | None = None,
    authorised_fill: bool = False,
) -> HeaderResult:
    """
    Build one name per column from one or more header rows.

    header_rows_values -- the raw rows, outermost list in the same order as
        header_rows_1idx. Ragged rows are padded to the widest row.
    header_rows_1idx -- the 1-indexed sheet row numbers those rows came from.
        Needed to decide which merge ranges apply to which row.
    source_type -- 'excel' or 'csv'. CSV is never filled; passing merge_refs
        with source_type='csv' is a programming error, not a fallback.
    join -- one of JOIN_MODES.
    merge_refs -- every merged range on the sheet. Only those intersecting the
        header rows are used.
    authorised_fill -- CSV only. A person has stated that an upper header row
        holds spanning group labels, so its values are carried rightwards until
        the next label. Locked decision 10 bans filling a CSV header on the
        FILE's say-so, because the file cannot distinguish a spanning label
        from an empty column. It does not ban filling on a human's, which is
        the entire purpose of having asked. Reaching this requires a caller to
        state that the question was answered, and the fill is recorded in the
        notes naming who authorised it.
    """
    if join not in JOIN_MODES:
        raise ValueError(f"header_join must be one of {JOIN_MODES}, got {join!r}")
    if not header_rows_values:
        raise ValueError("header_rows_values is empty; a header needs at least one row")
    if len(header_rows_values) != len(header_rows_1idx):
        raise ValueError(
            f"{len(header_rows_values)} header rows supplied but "
            f"{len(header_rows_1idx)} row numbers; they must correspond"
        )
    if source_type not in ("excel", "csv"):
        raise ValueError(f"source_type must be 'excel' or 'csv', got {source_type!r}")
    if source_type == "excel" and authorised_fill:
        raise ValueError(
            "authorised_fill is for CSV headers. An Excel header is filled "
            "from its merge ranges, which the file itself provides."
        )
    if source_type == "csv" and merge_refs:
        raise ValueError(
            "merge_refs supplied for a CSV. CSV files carry no merge metadata, "
            "so any refs here are invented. CSV headers are never auto-filled."
        )

    width = max(len(r) for r in header_rows_values)
    rows = _pad(header_rows_values, width)

    result = HeaderResult(names=[], filled_rows=[])

    if source_type == "excel" and merge_refs:
        applicable = merges_on_rows(merge_refs, header_rows_1idx)
        result.filled_merges = applicable
        filled = [
            fill_bounded(row, applicable, row_num)
            for row, row_num in zip(rows, header_rows_1idx)
        ]
        # fill_bounded pads out to a merge that runs past the row; re-level.
        width = max(width, max(len(r) for r in filled))
        filled = _pad(filled, width)
    elif source_type == "csv" and authorised_fill and len(rows) > 1:
        # Unbounded by necessity: a CSV has no range to stop at. Legitimate
        # only because a person said these are group labels.
        filled = [_fill_forward(r) for r in rows[:-1]] + [list(rows[-1])]
        result.authorised_fill = True
    else:
        filled = rows

    result.filled_rows = filled

    # CSV blanks above the bottom row are ambiguous and must be surfaced --
    # unless a person has already resolved them.
    if source_type == "csv" and len(filled) > 1 and not authorised_fill:
        for col in range(width):
            upper = [_clean(filled[r][col]) for r in range(len(filled) - 1)]
            if not any(upper):
                result.ambiguous_blanks.append(col)

    raw_names = []
    for col in range(width):
        parts = [_clean(row[col]) for row in filled]
        raw_names.append(_join_parts(parts, join))

    named = []
    for col, name in enumerate(raw_names):
        if name:
            named.append(name)
        else:
            named.append(BLANK_NAME_TEMPLATE.format(n=col + 1))
            result.blank_columns.append(col)

    result.names = _dedupe(named, result)
    # Blank counts in the note describe the row AS READ, before filling --
    # that is the number that tells you why the fill was needed at all.
    result.notes = _build_notes(result, header_rows_1idx, source_type, rows, join)
    return result


def _dedupe(names: list[str], result: HeaderResult) -> list[str]:
    """
    Give repeated names a numeric suffix. First occurrence keeps the name.

    Suffixing continues past a collision -- if 'units' and 'units_2' both
    already exist, the second 'units' becomes 'units_3', not a second
    'units_2'.
    """
    seen: dict[str, int] = {}
    taken = set()
    out = []
    for name in names:
        if name not in seen:
            seen[name] = 1
            taken.add(name)
            out.append(name)
            continue
        n = seen[name] + 1
        candidate = DUPLICATE_SUFFIX_TEMPLATE.format(name=name, n=n)
        while candidate in taken:
            n += 1
            candidate = DUPLICATE_SUFFIX_TEMPLATE.format(name=name, n=n)
        seen[name] = n
        taken.add(candidate)
        out.append(candidate)
        result.renamed_duplicates.append((name, candidate))
    return out


def _build_notes(
    result: HeaderResult,
    header_rows_1idx: list[int],
    source_type: str,
    raw_rows: list[list],
    join: str,
) -> list[str]:
    notes = []
    span = (
        str(header_rows_1idx[0])
        if len(header_rows_1idx) == 1
        else f"{min(header_rows_1idx)}-{max(header_rows_1idx)}"
    )
    width = result.column_count

    if result.filled_merges:
        top = raw_rows[0]
        blank_in_top = sum(1 for v in top if not _clean(v))
        plural = "s" if len(result.filled_merges) != 1 else ""
        found = (
            f"Row{'s' if len(header_rows_1idx) > 1 else ''} {span} contain "
            f"{len(result.filled_merges)} merged range{plural} "
            f"({', '.join(result.filled_merges)})."
        )
        # Whether the fill reached the output depends on the join mode. Under
        # bottom_only every row above the last is discarded, so saying the
        # merges were "used to build the names" would be false -- and the
        # assumptions list is only worth reading if it can be trusted without
        # checking.
        if join == "bottom_only" and len(header_rows_1idx) > 1:
            notes.append(
                f"{found} They were filled but not used: header_join is "
                f"bottom_only, so the names come from row "
                f"{header_rows_1idx[-1]} alone and the merged labels do not "
                f"appear in them."
            )
        elif join == "top_only" and len(header_rows_1idx) > 1:
            notes.append(
                f"{found} Filled them to build column names from row "
                f"{header_rows_1idx[0]} alone. Row {header_rows_1idx[0]} as "
                f"read showed {blank_in_top} of {width} columns blank, so "
                f"every name here came from a merge. Check them before "
                f"confirming."
            )
        else:
            notes.append(
                f"{found} Filled them to build column names. Row "
                f"{header_rows_1idx[0]} alone showed {blank_in_top} of "
                f"{width} columns blank. Check the assembled names before "
                f"confirming."
            )

    if result.authorised_fill:
        notes.append(
            f"Row(s) {span} were forward-filled because you said the upper "
            f"row holds group labels spanning columns. A CSV cannot state "
            f"that itself -- this fill exists only because it was asked about "
            f"and answered. Each label was carried rightwards to the column "
            f"before the next label."
        )

    if result.ambiguous_blanks:
        cols = ", ".join(str(c + 1) for c in result.ambiguous_blanks)
        notes.append(
            f"This is a CSV, which carries no merge information. Column(s) {cols} "
            f"are blank in the upper header row(s). That blank could be a merged "
            f"cell that lost its value, or a genuinely empty column. Nothing in "
            f"the file says which, so nothing was filled. Confirm or correct "
            f"these names before loading."
        )

    if result.blank_columns:
        cols = ", ".join(str(c + 1) for c in result.blank_columns)
        notes.append(
            f"Column(s) {cols} had no name in any header row and were named "
            f"{BLANK_NAME_TEMPLATE.format(n='N')} by position."
        )

    if result.renamed_duplicates:
        pairs = ", ".join(f"{old} -> {new}" for old, new in result.renamed_duplicates)
        notes.append(f"Duplicate names were suffixed: {pairs}.")

    return notes


_NON_WORD = re.compile(r"[^0-9a-zA-Z]+")
_REPEATED = re.compile(r"_+")


def to_target_name(source_name: str) -> str:
    """
    Normalise a source name to a SQL-safe snake_case identifier.

    'Q3 2024 Amt' -> 'q3_2024_amt'
    '% of total'  -> 'pct_of_total'
    '2024'        -> 'col_2024'   (identifiers cannot start with a digit)
    ''            -> 'unnamed'
    """
    text = source_name.strip()
    text = text.replace("%", " pct ").replace("#", " num ").replace("&", " and ")
    text = _NON_WORD.sub("_", text)
    text = _REPEATED.sub("_", text).strip("_").lower()
    if not text:
        return "unnamed"
    if text[0].isdigit():
        text = f"col_{text}"
    return text


def to_target_names(source_names: list[str]) -> list[str]:
    """Normalise a whole header, deduping names that collide after normalising."""
    result = HeaderResult(names=[], filled_rows=[])
    return _dedupe([to_target_name(n) for n in source_names], result)
