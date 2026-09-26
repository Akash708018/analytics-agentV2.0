"""
Unit tests for ingest/preview.py.

The three fixture shapes used throughout mirror the real files:

    MESSY   a title, then blanks, then the header, then data. Shaped like
            tests/fixtures/messy_headers.xlsx, whose real header is row 5 --
            the exact row is deliberately not encoded here, because the point
            of the guesser is that it reads it off the file.
    SALES   spanning labels in row 1 under merges, leaf names in row 2,
            data from row 3 (tests/fixtures/merged_multiheader.xlsx)
    SALES as CSV  the same rows with no merge metadata, which must not
            resolve (tests/fixtures/multiheader.csv)
"""

from __future__ import annotations

import inspect

import pytest

from backend.engine.ingest.preview import (
    HeaderGuess,
    detect_pivot_dump,
    draft_spec,
    guess_header,
    parse_csv_preview,
    propose_join,
)

MESSY = [
    ("Quarterly Report", None, None, None),
    (None, None, None, None),
    (None, None, None, None),
    ("order_id", "region", "units", "revenue"),
    ("A1", "North", 10, 100.5),
    ("A2", "South", 20, 200.0),
]

SALES = [
    ("Identifiers", None, "Dimensions", None, None, "Measures", None, None),
    ("order_id", "order_date", "region", "product", "channel", "units",
     "unit_price", "revenue"),
    ("A1", "2024-01-01", "North", "widget", "web", 10, 9.5, 95.0),
]

SALES_MERGES = ["A1:B1", "C1:E1", "F1:H1"]

PLAIN = [
    ("order_id", "region", "units"),
    ("A1", "North", 10),
    ("A2", "South", 20),
]


# --------------------------------------------------------------------------
# guess_header
# --------------------------------------------------------------------------

def test_plain_single_row_header_is_high_confidence():
    g = guess_header(PLAIN, source_type="csv")
    assert g.header_rows == [1]
    assert g.data_start_row == 2
    assert g.confidence == "high"
    assert not g.needs_confirmation


def test_title_and_blanks_above_the_header_are_excluded():
    """The title and the blanks above the header are dropped, not joined in."""
    g = guess_header(MESSY, source_type="excel", merge_refs=[])
    assert g.header_rows == [4]
    assert g.data_start_row == 5
    assert g.confidence == "high"
    assert any("blank" in r for r in g.reasons)


def test_a_merge_promotes_a_sparse_row_into_the_header():
    g = guess_header(SALES, source_type="excel", merge_refs=SALES_MERGES)
    assert g.header_rows == [1, 2]
    assert g.data_start_row == 3
    assert g.confidence == "medium"
    assert any("merged range" in r for r in g.reasons)


def test_a_sparse_row_with_no_merge_is_a_title():
    rows = [
        ("Quarterly Report", None, None),
        ("order_id", "region", "units"),
        ("A1", "North", 10),
    ]
    g = guess_header(rows, source_type="excel", merge_refs=[])
    assert g.header_rows == [2]
    assert g.data_start_row == 3
    assert any("title" in r for r in g.reasons)


def test_the_same_sparse_row_in_a_csv_is_not_resolved():
    """
    Locked decision 10 at the level of the guess. No merge metadata exists, so
    the module says so instead of picking.
    """
    g = guess_header([list(r) for r in SALES], source_type="csv")
    assert g.confidence == "low"
    assert g.needs_confirmation
    assert g.questions
    assert any("no merge information" in r for r in g.reasons)


def test_csv_with_merge_refs_is_refused():
    with pytest.raises(ValueError) as exc:
        guess_header(PLAIN, source_type="csv", merge_refs=["A1:B1"])
    assert "no merge metadata" in str(exc.value)


def test_a_headerless_file_is_reported_not_invented():
    rows = [("A1", "North", 10), ("A2", "South", 20)]
    g = guess_header(rows, source_type="csv")
    assert g.header_rows == []
    assert g.data_start_row == 1
    assert g.needs_confirmation


def test_an_all_text_file_is_low_confidence():
    """No type change means nothing marks where data begins."""
    rows = [("a", "b"), ("x", "y"), ("p", "q")]
    g = guess_header(rows, source_type="csv")
    assert g.confidence == "low"
    assert g.needs_confirmation


def test_numeric_strings_count_as_data_not_labels():
    """A CSV gives every cell as str; '10' is still data."""
    rows = [["order_id", "units"], ["A1", "10"], ["A2", "20"]]
    g = guess_header(rows, source_type="csv")
    assert g.header_rows == [1]
    assert g.data_start_row == 2


def test_empty_preview_is_refused():
    with pytest.raises(ValueError):
        guess_header([], source_type="csv")


def test_all_blank_preview_is_refused():
    with pytest.raises(ValueError):
        guess_header([(None, None), (None, None)], source_type="csv")


def test_unknown_source_type_is_refused():
    with pytest.raises(ValueError):
        guess_header(PLAIN, source_type="parquet")


# --------------------------------------------------------------------------
# propose_join
# --------------------------------------------------------------------------

def test_unique_bottom_row_proposes_bottom_only():
    mode, why = propose_join([list(SALES[0]), list(SALES[1])])
    assert mode == "bottom_only"
    assert "groupings" in why


def test_repeated_bottom_row_proposes_a_joining_mode():
    rows = [
        ["Q3 2024", None, "Q4 2024", None],
        ["Units", "Amt", "Units", "Amt"],
    ]
    mode, why = propose_join(rows)
    assert mode == "space"
    assert "'Units'" in why and "'Amt'" in why


def test_a_gap_in_the_bottom_row_proposes_a_joining_mode():
    """A blank in the bottom row means it cannot name every column alone."""
    rows = [["Q3", "Q3", "Q4"], ["Units", None, "Amt"]]
    mode, why = propose_join(rows)
    assert mode == "space"
    assert "does not name every column" in why


def test_single_header_row_join_is_moot():
    mode, why = propose_join([["a", "b"]])
    assert mode == "space"
    assert "no effect" in why


# --------------------------------------------------------------------------
# pivot dump -- build guide 6.4
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "names",
    [
        ["region", "Jan", "Feb", "Mar", "Apr"],
        ["region", "Jan 2024", "Feb 2024", "Mar 2024"],
        ["product", "2021", "2022", "2023", "2024"],
        ["product", "Q1 2024", "Q2 2024", "Q3 2024", "Q4 2024"],
        ["product", "FY22", "FY23", "FY24"],
    ],
)
def test_period_columns_are_flagged(names):
    v = detect_pivot_dump(names)
    assert v.is_pivot_dump
    assert "unpivot" in v.message


@pytest.mark.parametrize(
    "names",
    [
        ["order_id", "region", "units", "revenue"],
        ["order_id", "order_date", "units"],
        ["region", "Jan"],
        [],
    ],
)
def test_ordinary_columns_are_not_flagged(names):
    assert not detect_pivot_dump(names).is_pivot_dump


def test_a_date_column_name_alone_is_not_a_pivot():
    """One period column among many is a variable, not a pivot."""
    assert not detect_pivot_dump(
        ["order_id", "region", "product", "channel", "2024"]
    ).is_pivot_dump


# --------------------------------------------------------------------------
# parse_csv_preview
# --------------------------------------------------------------------------

def test_comma_delimiter_is_sniffed():
    rows = parse_csv_preview(["a,b,c\n", "1,2,3\n"])
    assert rows == [["a", "b", "c"], ["1", "2", "3"]]


def test_semicolon_delimiter_is_sniffed():
    rows = parse_csv_preview(["a;b;c\n", "1;2;3\n"])
    assert rows == [["a", "b", "c"], ["1", "2", "3"]]


def test_an_explicit_delimiter_wins():
    rows = parse_csv_preview(["a|b\n", "1|2\n"], delimiter="|")
    assert rows == [["a", "b"], ["1", "2"]]


def test_quoted_commas_survive():
    rows = parse_csv_preview(['a,b\n', '"North, East",2\n'])
    assert rows[1] == ["North, East", "2"]


# --------------------------------------------------------------------------
# draft_spec
# --------------------------------------------------------------------------

def test_draft_spec_on_the_sales_shape():
    spec, guess, pivot = draft_spec(
        SALES,
        path="tests/fixtures/merged_multiheader.xlsx",
        source_type="excel",
        dataset_name="mm",
        sheet="Sales",
        merge_refs=SALES_MERGES,
    )
    assert spec is not None
    assert spec.header_rows == [1, 2]
    assert spec.data_start_row == 3
    assert spec.loader_header_rows == 2
    assert spec.header_join == "bottom_only"
    assert spec.target_names == [
        "order_id", "order_date", "region", "product", "channel",
        "units", "unit_price", "revenue",
    ]
    assert not pivot.is_pivot_dump
    assert guess.confidence == "medium"


def test_draft_spec_assumptions_carry_the_reasoning():
    spec, _guess, _pivot = draft_spec(
        SALES,
        path="x.xlsx",
        source_type="excel",
        dataset_name="mm",
        sheet="Sales",
        merge_refs=SALES_MERGES,
    )
    joined = " ".join(spec.assumptions)
    assert "first row containing non-text values" in joined
    assert "merged range" in joined
    assert "bottom_only" in joined
    # and it must not claim the merged labels reached the names
    assert "filled but not used" in joined


def test_draft_spec_on_the_messy_shape():
    spec, guess, _pivot = draft_spec(
        MESSY,
        path="tests/fixtures/messy_headers.xlsx",
        source_type="excel",
        dataset_name="messy",
        sheet="Report",
        merge_refs=[],
    )
    assert spec is not None
    assert spec.header_rows == [4]
    assert spec.data_start_row == 5
    assert spec.loader_header_rows == 4
    assert guess.confidence == "high"


def test_a_low_confidence_guess_yields_a_provisional_unloadable_spec():
    """
    The Done-When clause. It used to be served by returning nothing, which was
    safe and left the caller no way to answer its own question. Now a spec
    comes back with the guessed field named in `unresolved`, and it cannot be
    loaded until a person settles it.
    """
    spec, guess, _pivot = draft_spec(
        [list(r) for r in SALES],
        path="tests/fixtures/multiheader.csv",
        source_type="csv",
        dataset_name="multi",
    )
    assert spec is not None
    assert spec.unresolved == ["header_rows"]
    assert not spec.is_confirmable
    assert spec.questions
    assert guess.needs_confirmation


def test_an_answer_clears_the_unresolved_field():
    spec, _guess, _pivot = draft_spec(
        [list(r) for r in SALES],
        path="tests/fixtures/multiheader.csv",
        source_type="csv",
        dataset_name="multi",
        header_rows=[1, 2],
        authorised_fill=True,
    )
    assert spec.unresolved == []
    assert spec.is_confirmable
    assert spec.authorised_fill
    assert spec.target_names[0] == "identifiers_order_id"


def test_answering_that_row_one_was_a_title_gives_bare_names():
    spec, _guess, _pivot = draft_spec(
        [list(r) for r in SALES],
        path="tests/fixtures/multiheader.csv",
        source_type="csv",
        dataset_name="multi",
        header_rows=[2],
    )
    assert spec.is_confirmable
    assert spec.target_names[0] == "order_id"
    assert spec.loader_header_rows == 2


def test_draft_spec_flags_a_pivot_dump_in_the_assumptions():
    rows = [
        ("region", "Jan 2024", "Feb 2024", "Mar 2024"),
        ("North", 1, 2, 3),
    ]
    spec, _guess, pivot = draft_spec(
        rows, path="p.csv", source_type="csv", dataset_name="p"
    )
    assert pivot.is_pivot_dump
    assert any("pivot table export" in a for a in spec.assumptions)


# --------------------------------------------------------------------------
# live -- against the Phase 2 preview functions
# --------------------------------------------------------------------------

def test_phase2_preview_signatures_are_what_this_module_expects():
    """
    preview.py is driven from these. If Phase 2 renames a parameter, this
    fails here rather than at the first real file.
    """
    from backend.engine.ingest.csv_loader import preview_lines
    from backend.engine.ingest.excel import list_sheets, preview_rows

    assert list(inspect.signature(preview_lines).parameters) == ["path", "n"]
    assert list(inspect.signature(preview_rows).parameters) == ["path", "sheet", "n"]
    assert list(inspect.signature(list_sheets).parameters) == ["path"]
