"""
Unit tests for ingest/spec.py.

Most of these run against stand-in loaders declared in this file, so the suite
stays fast and independent. The exceptions are the tests marked "live" at the
bottom, which import the real `load_csv` and `load_excel` and check the bridge
against their actual signatures. Those are the ones that catch a Phase 2 change
breaking Phase 3.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from backend.engine.ingest.headers import assemble_names
from backend.engine.ingest.spec import (
    ColumnSpec,
    IngestSpec,
    SpecNotSupported,
)


# --------------------------------------------------------------------------
# stand-ins mirroring the Phase 2 signatures
# --------------------------------------------------------------------------

def stub_load_csv(
    con, path, dataset_name, header_rows=1, names=None, na_values=None,
    delimiter=None, footer_skip_rows=0, dtypes=None, on_error="stop",
    sample_size=20_480, replace=True,
):
    ...


def stub_load_excel(
    con, path, dataset_name, sheet=None, header_rows=1, names=None,
    na_values=None, footer_skip_rows=0, dtypes=None, on_error="stop",
    all_text=False, inference_rows=5_000, replace=True,
):
    ...


def narrow_loader(con, path, dataset_name, header_rows=1, names=None):
    """
    A loader that accepts only the bare minimum.

    Step 5 gave the real loaders every field the spec can carry, so nothing on
    a real spec is refused any more. The refusal mechanism still has to work
    for the next field somebody adds to IngestSpec, so it is pinned against
    this instead of against a real loader that has caught up.
    """
    ...


def cols(*names):
    return [ColumnSpec(source_name=n, target_name=n) for n in names]


THREE = ("order_id", "region", "units")


def csv_spec(**over):
    base = dict(
        path="tests/fixtures/multiheader.csv",
        source_type="csv",
        dataset_name="multi",
        header_rows=[1, 2],
        data_start_row=3,
        columns=cols(*THREE),
    )
    base.update(over)
    return IngestSpec(**base)


def excel_spec(**over):
    base = dict(
        path="tests/fixtures/merged_multiheader.xlsx",
        source_type="excel",
        dataset_name="mm",
        sheet="Sales",
        header_rows=[1, 2],
        data_start_row=3,
        columns=cols(*THREE),
    )
    base.update(over)
    return IngestSpec(**base)


# --------------------------------------------------------------------------
# the off-by-one -- FMR F12
# --------------------------------------------------------------------------

def test_loader_header_rows_is_a_count_not_a_length():
    """
    messy_headers.xlsx: title in row 1, junk, header in row 4, data in row 5.
    One header row, but the loader must skip four. Deriving the skip from
    len(header_rows) would be wrong by three and would load the title as data.
    """
    s = IngestSpec(
        path="tests/fixtures/messy_headers.xlsx",
        source_type="excel",
        dataset_name="messy",
        sheet="Report",
        header_rows=[4],
        data_start_row=5,
        columns=cols(*THREE),
    )
    assert len(s.header_rows) == 1
    assert s.loader_header_rows == 4
    assert s.to_loader_kwargs()["header_rows"] == 4


def test_loader_header_rows_for_an_adjacent_two_row_header():
    assert excel_spec().loader_header_rows == 2


def test_data_start_row_must_be_past_the_header():
    with pytest.raises(ValidationError) as exc:
        excel_spec(header_rows=[1, 2], data_start_row=2)
    assert "would include the header" in str(exc.value)


# --------------------------------------------------------------------------
# the bridge
# --------------------------------------------------------------------------

def test_csv_kwargs_carry_names_delimiter_and_na_values():
    s = csv_spec(na_values=["N/A", "-"], delimiter=";")
    assert s.to_loader_kwargs(stub_load_csv) == {
        "dataset_name": "multi",
        "header_rows": 2,
        "names": ["order_id", "region", "units"],
        "delimiter": ";",
        "na_values": ["N/A", "-"],
    }


def test_excel_kwargs_carry_the_sheet():
    k = excel_spec().to_loader_kwargs(stub_load_excel)
    assert k["sheet"] == "Sales"
    assert "delimiter" not in k


def test_kwargs_use_target_names_not_source_names():
    s = excel_spec(
        columns=[
            ColumnSpec(source_name="Q3 2024 Amt", target_name="q3_2024_amt"),
            ColumnSpec(source_name="Order ID", target_name="order_id"),
        ]
    )
    assert s.to_loader_kwargs()["names"] == ["q3_2024_amt", "order_id"]


def test_kwargs_omit_con_and_path():
    k = csv_spec().to_loader_kwargs(stub_load_csv)
    assert "con" not in k and "path" not in k


def test_unsupported_field_is_refused_by_name():
    with pytest.raises(SpecNotSupported) as exc:
        csv_spec(na_values=["-"]).to_loader_kwargs(narrow_loader)
    msg = str(exc.value)
    assert "na_values" in msg
    assert "NEXT STEP" in msg


def test_every_refused_field_is_named_not_just_the_first():
    with pytest.raises(SpecNotSupported) as exc:
        csv_spec(na_values=["-"], footer_skip_rows=2).to_loader_kwargs(narrow_loader)
    msg = str(exc.value)
    assert "na_values" in msg and "footer_skip_rows" in msg


def test_the_real_loaders_now_accept_na_values_and_footer_skip():
    """Step 3 left both refused. Step 5 is what makes this pass."""
    from backend.engine.ingest.csv_loader import load_csv
    from backend.engine.ingest.excel import load_excel

    csv_spec(na_values=["-"], footer_skip_rows=1).to_loader_kwargs(load_csv)
    excel_spec(na_values=["-"], footer_skip_rows=1).to_loader_kwargs(load_excel)


def test_no_loader_means_no_signature_check():
    """The dict is still buildable without a loader to check against."""
    k = csv_spec(footer_skip_rows=2).to_loader_kwargs()
    assert k["footer_skip_rows"] == 2


def test_zero_footer_skip_is_not_emitted():
    assert "footer_skip_rows" not in csv_spec().to_loader_kwargs()


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def test_header_rows_must_be_contiguous():
    with pytest.raises(ValidationError) as exc:
        excel_spec(header_rows=[1, 3], data_start_row=4)
    assert "contiguous" in str(exc.value)


def test_header_rows_must_be_ordered():
    with pytest.raises(ValidationError):
        excel_spec(header_rows=[2, 1], data_start_row=3)


def test_header_rows_are_one_indexed():
    with pytest.raises(ValidationError) as exc:
        excel_spec(header_rows=[0, 1], data_start_row=2)
    assert "1-indexed" in str(exc.value)


def test_empty_header_rows_is_refused():
    with pytest.raises(ValidationError):
        excel_spec(header_rows=[], data_start_row=1)


def test_dataset_name_must_be_an_identifier():
    with pytest.raises(ValidationError) as exc:
        csv_spec(dataset_name="2024 sales")
    assert "must start with a letter" in str(exc.value)


def test_target_name_must_be_an_identifier():
    with pytest.raises(ValidationError) as exc:
        csv_spec(columns=[ColumnSpec(source_name="Q3 Amt", target_name="Q3 Amt")])
    assert "to_target_name" in str(exc.value)


def test_duplicate_target_names_are_refused():
    with pytest.raises(ValidationError) as exc:
        csv_spec(
            columns=[
                ColumnSpec(source_name="Units", target_name="units"),
                ColumnSpec(source_name="units", target_name="units"),
            ]
        )
    assert "unique" in str(exc.value)


def test_sheet_is_refused_on_a_csv():
    with pytest.raises(ValidationError) as exc:
        csv_spec(sheet="Sheet1")
    assert "not CSV files" in str(exc.value)


def test_delimiter_is_refused_on_a_workbook():
    with pytest.raises(ValidationError) as exc:
        excel_spec(delimiter=",")
    assert "not worksheets" in str(exc.value)


def test_negative_footer_skip_is_refused():
    with pytest.raises(ValidationError):
        csv_spec(footer_skip_rows=-1)


def test_at_least_one_column_is_required():
    with pytest.raises(ValidationError):
        csv_spec(columns=[])


def test_unknown_join_mode_is_refused():
    with pytest.raises(ValidationError):
        excel_spec(header_join="pipe")


# --------------------------------------------------------------------------
# from_header_result
# --------------------------------------------------------------------------

def test_from_header_result_normalises_and_carries_the_notes():
    rows = [["Q3 2024", None, "Q4 2024", None], ["Units", "Amt", "Units", "Amt"]]
    hr = assemble_names(
        rows,
        header_rows_1idx=[1, 2],
        source_type="excel",
        merge_refs=["A1:B1", "C1:D1"],
    )
    s = IngestSpec.from_header_result(
        hr,
        path="x.xlsx",
        source_type="excel",
        dataset_name="q",
        header_rows=[1, 2],
        data_start_row=3,
        sheet="S",
    )
    assert s.source_names == [
        "Q3 2024 Units",
        "Q3 2024 Amt",
        "Q4 2024 Units",
        "Q4 2024 Amt",
    ]
    assert s.target_names == [
        "q3_2024_units",
        "q3_2024_amt",
        "q4_2024_units",
        "q4_2024_amt",
    ]
    assert any("merged range" in a for a in s.assumptions)


def test_from_header_result_suffixes_targets_that_collide_after_normalising():
    hr = assemble_names([["Units", "units"]], header_rows_1idx=[1], source_type="csv")
    s = IngestSpec.from_header_result(
        hr,
        path="x.csv",
        source_type="csv",
        dataset_name="u",
        header_rows=[1],
        data_start_row=2,
    )
    assert s.target_names == ["units", "units_2"]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def test_to_text_shows_both_row_numbers_and_the_skip_count():
    text = excel_spec().to_text()
    assert "header rows    [1, 2]" in text
    assert "row 3" in text and "skips 2" in text
    assert "3 columns" in text


def test_to_text_lists_assumptions():
    text = csv_spec(assumptions=["Row 1 was a title, not a header."]).to_text()
    assert "Assumptions:" in text
    assert "was a title" in text


# --------------------------------------------------------------------------
# live -- against the real Phase 2 loaders
# --------------------------------------------------------------------------

def test_stubs_still_match_the_real_signatures():
    """
    The stand-ins above are only useful while they mirror the real loaders.
    If Phase 2 changes, this fails and the stubs get updated deliberately.
    """
    from backend.engine.ingest.csv_loader import load_csv
    from backend.engine.ingest.excel import load_excel

    for real, stub in ((load_csv, stub_load_csv), (load_excel, stub_load_excel)):
        assert set(inspect.signature(real).parameters) == set(
            inspect.signature(stub).parameters
        ), f"{real.__name__} signature has changed; update the stub in this file"


def test_a_plain_spec_is_accepted_by_the_real_csv_loader():
    from backend.engine.ingest.csv_loader import load_csv

    csv_spec().to_loader_kwargs(load_csv)


def test_a_plain_spec_is_accepted_by_the_real_excel_loader():
    from backend.engine.ingest.excel import load_excel

    excel_spec().to_loader_kwargs(load_excel)
