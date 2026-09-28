"""
Merged-cell discovery and bounded forward-fill for .xlsx headers.

Why this module exists
----------------------
openpyxl's read-only mode is the only memory-safe way to stream a large
workbook, but its ReadOnlyWorksheet has no ``merged_cells`` attribute --
accessing it raises AttributeError (FMR F11). Merge information is therefore
read straight out of the sheet XML inside the .xlsx zip, which costs no
workbook load and constant memory.

The second job here is the fill itself. A spanning header like "Q3 2024"
merged across C:D stores its value only in C4; D4 is empty. Filling that
value rightwards without stopping at the end of the merge range silently
mislabels unrelated columns (FMR F14). Every fill in this codebase is
bounded by a real merge range.

Nothing in this module is Excel-only in spirit but it is Excel-only in fact:
CSV files carry no merge metadata, so CSV headers are never auto-filled.
"""

from __future__ import annotations

import posixpath
import zipfile
from xml.etree import ElementTree as ET

from openpyxl.utils import range_boundaries

# Namespaces used inside an .xlsx package.
NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class MergeParseError(Exception):
    """Raised when the workbook cannot be read as an OOXML package."""


def _open_package(path: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise MergeParseError(
            f"{path} is not a readable .xlsx/.xlsm package. "
            "Legacy .xls files are not OOXML zips and are not supported; "
            "re-save the file as .xlsx and retry."
        ) from exc
    except FileNotFoundError as exc:
        raise MergeParseError(f"File not found: {path}") from exc


def sheet_names(path: str) -> list[str]:
    """Tab names in the order Excel displays them."""
    with _open_package(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = wb.find(f"{NS_MAIN}sheets")
        if sheets is None:
            raise MergeParseError(f"{path} has no <sheets> block in xl/workbook.xml")
        return [s.get("name") for s in sheets]


def active_sheet_name(path: str) -> str:
    """
    The tab Excel opens on -- what openpyxl calls ``wb.active``.

    Needed because the Phase 2 ``excel.merged_ranges`` defaulted ``sheet=None``
    to the active sheet, and this module requires an explicit name. The active
    tab is NOT the first tab: ``activeTab`` in ``xl/workbook.xml`` is an index
    into <sheets> order and is wherever the file was last saved from. It counts
    hidden sheets rather than skipping them.

    Verified against openpyxl's ``wb.active`` on five workbooks: default,
    second tab active, third tab active, tabs reordered, and one sheet hidden.
    """
    with _open_package(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets_el = wb.find(f"{NS_MAIN}sheets")
        if sheets_el is None or len(sheets_el) == 0:
            raise MergeParseError(f"{path} has no sheets")
        names = [s.get("name") for s in sheets_el]

        idx = 0
        views = wb.find(f"{NS_MAIN}bookViews")
        if views is not None:
            for view in views:
                raw = view.get("activeTab")
                if raw is not None:
                    try:
                        idx = int(raw)
                    except ValueError:
                        idx = 0
                    break

        return names[idx] if 0 <= idx < len(names) else names[0]


def _sheet_part(z: zipfile.ZipFile, sheet_name: str) -> str:
    """
    Resolve a tab name to its part name inside the zip.

    Tab order and file numbering are independent. Reordering or deleting tabs
    in Excel changes <sheets> order while sheetN.xml filenames stay put, so
    position in <sheets> must never be used to build a filename. The only
    correct path is r:id -> xl/_rels/workbook.xml.rels -> Target.
    """
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = wb.find(f"{NS_MAIN}sheets")
    if sheets is None:
        raise MergeParseError("No <sheets> block in xl/workbook.xml")

    rid = None
    available = []
    for s in sheets:
        available.append(s.get("name"))
        if s.get("name") == sheet_name:
            rid = s.get(f"{NS_REL}id")
    if rid is None:
        raise KeyError(
            f"Sheet {sheet_name!r} not found. Sheets in this workbook: {available}"
        )

    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    for rel in rels:
        if rel.get("Id") == rid:
            target = rel.get("Target")
            # Targets appear both absolute ("/xl/worksheets/sheet1.xml") and
            # relative to xl/ ("worksheets/sheet1.xml"). Both are legal.
            if target.startswith("/"):
                return target.lstrip("/")
            return posixpath.normpath(posixpath.join("xl", target))

    raise MergeParseError(f"Relationship {rid!r} for sheet {sheet_name!r} is missing")


def merged_ranges(path: str, sheet_name: str) -> list[str]:
    """
    Every merged range on one sheet, as A1-style refs sorted top-left first.

    Streams the sheet XML rather than loading the workbook. <mergeCells> is
    written after </sheetData> in the OOXML schema, so the whole row block
    must be walked past -- there is no early exit. Each <row> is cleared as
    it goes by; clearing only at the end of <sheetData> lets the entire row
    tree accumulate in memory first, which defeats the point.
    """
    refs: list[str] = []
    with _open_package(path) as z:
        part = _sheet_part(z, sheet_name)
        with z.open(part) as f:
            for _, el in ET.iterparse(f, events=("end",)):
                if el.tag == f"{NS_MAIN}mergeCell":
                    ref = el.get("ref")
                    if ref:
                        refs.append(ref)
                elif el.tag == f"{NS_MAIN}row":
                    el.clear()
    return sorted(refs, key=_ref_sort_key)


def _ref_sort_key(ref: str) -> tuple[int, int]:
    c1, r1, _c2, _r2 = range_boundaries(ref)
    return (r1, c1)


def merges_on_rows(merge_refs: list[str], rows_1idx: list[int]) -> list[str]:
    """Subset of merge_refs that touch any of the given 1-indexed rows."""
    wanted = set(rows_1idx)
    hits = []
    for ref in merge_refs:
        _c1, r1, _c2, r2 = range_boundaries(ref)
        if wanted & set(range(r1, r2 + 1)):
            hits.append(ref)
    return sorted(hits, key=_ref_sort_key)


def fill_bounded(row_values, merge_refs: list[str], row_num_1idx: int) -> list:
    """
    Copy each merge anchor's value across its own range and no further.

    row_values is 0-indexed; merge refs and row_num_1idx are 1-indexed to
    match what Excel shows. That conversion happens here and nowhere else.

    Rows shorter than a merge range are padded with None rather than raising:
    openpyxl pads to the sheet's declared dimension, which a hand-edited or
    tool-generated file can under-report.
    """
    out = list(row_values)
    for ref in merge_refs:
        c1, r1, c2, r2 = range_boundaries(ref)
        if not (r1 <= row_num_1idx <= r2):
            continue
        if len(out) < c2:
            out.extend([None] * (c2 - len(out)))
        anchor = out[c1 - 1]
        for c in range(c1, c2 + 1):
            out[c - 1] = anchor
    return out
