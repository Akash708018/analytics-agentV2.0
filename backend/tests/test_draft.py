"""
Tests for ingest/draft.py -- the path-to-proposal layer, and the JSON round
trip that carries a spec out to the user and back.

These run against the real fixtures in tests/fixtures/, because the point of
this layer is deciding what a real file means. Run make_fixtures.py first if
they are missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.engine import workspace
from backend.engine.ingest import draft, preview
from backend.engine.ingest.csv_loader import LoadRefused, load_csv
from backend.engine.ingest.excel import load_excel
from backend.engine.ingest.spec import IngestSpec
from backend.engine.util import db

FIXTURES = Path("tests/fixtures")
MESSY = str(FIXTURES / "messy_headers.xlsx")
MERGED = str(FIXTURES / "merged_multiheader.xlsx")
MULTI = str(FIXTURES / "multiheader.csv")
GAPS = str(FIXTURES / "gaps_and_dupes.csv")
CLEAN = str(FIXTURES / "clean_sales.csv")

WORKSPACE = "step6_test"


@pytest.fixture(autouse=True)
def _fixtures_exist():
    if not Path(MESSY).exists():
        pytest.skip("run: uv run python tests/fixtures/make_fixtures.py")


@pytest.fixture
def con():
    workspace.reset(WORKSPACE)
    c = db.connect(WORKSPACE)
    try:
        yield c
    finally:
        c.close()
        workspace.reset(WORKSPACE)


# --------------------------------------------------------------------------
# footer detection -- Excel only
# --------------------------------------------------------------------------

def test_trailing_notes_are_counted_as_footer():
    tail = [
        ("ORD-00200", "2024-07-12", None, "Cog", "Online", 9, 44.74, 402.66),
        (None, None, None, None, None, None, None, None),
        ("Notes: figures exclude cancelled orders.", None, None, None,
         None, None, None, None),
        ("Source: internal ERP extract.", None, None, None,
         None, None, None, None),
    ]
    assert preview.count_trailing_junk(tail, width=8) == 3


def test_a_record_with_a_few_empty_fields_is_not_footer():
    """Row 200 of messy_headers has a blank region. It is still a record."""
    tail = [("ORD-00200", "2024-07-12", None, "Cog", "Online", 9, 44.74, 402.66)]
    assert preview.count_trailing_junk(tail, width=8) == 0


def test_a_clean_tail_has_no_footer():
    tail = [(i, "x", 1, 2, 3, 4, 5, 6) for i in range(5)]
    assert preview.count_trailing_junk(tail, width=8) == 0


def test_footer_counting_stops_at_the_first_real_row():
    """Junk above a record does not count; only the trailing run does."""
    tail = [
        ("note", None, None, None),
        (1, 2, 3, 4),
        (None, None, None, None),
    ]
    assert preview.count_trailing_junk(tail, width=4) == 1


def test_zero_width_is_not_a_division_error():
    assert preview.count_trailing_junk([(None,)], width=0) == 0


def test_messy_headers_draft_sets_footer_skip_to_three():
    d = draft.draft_for_path(MESSY)
    assert d.spec is not None
    assert d.spec.footer_skip_rows == 3
    assert any("footer_skip_rows is set to 3" in a for a in d.spec.assumptions)


def test_a_csv_draft_reads_its_end_and_finds_no_footer_where_there_is_none():
    """
    Superseded by Phase 14 Step 7 (P14-O3). This test used to pin "a CSV draft never
    guesses at a footer", on the premise that seeking to the end of a 1.6 GB file is not worth
    it. A seek costs the same at any size, and not looking let a totals row double every sum.
    The end is read now; a file that ends in data keeps every row and claims no footer.
    """
    d = draft.draft_for_path(GAPS)
    assert d.spec is not None
    assert d.spec.footer_skip_rows == 0
    assert not any("footer" in a or "not examined" in a for a in d.spec.assumptions)


# --------------------------------------------------------------------------
# draft_for_path
# --------------------------------------------------------------------------

def test_messy_headers_resolves_header_and_skip():
    d = draft.draft_for_path(MESSY)
    assert d.sheet == "Report"
    assert d.spec.header_rows == [5]
    assert d.spec.data_start_row == 6
    assert d.spec.loader_header_rows == 5
    assert d.guess.confidence == "high"


def test_merged_multiheader_resolves_to_bottom_only():
    d = draft.draft_for_path(MERGED)
    assert d.sheet == "Sales"
    assert d.spec.header_rows == [1, 2]
    assert d.spec.header_join == "bottom_only"
    assert d.spec.target_names[0] == "order_id"
    assert not any(n.startswith("identifiers") for n in d.spec.target_names)


def test_multiheader_csv_is_provisional_and_cannot_be_loaded():
    """The Phase 3 Done-When clause, at the layer the tool calls."""
    d = draft.draft_for_path(MULTI)
    assert d.needs_answer
    assert d.spec.unresolved == ["header_rows"]
    assert not d.spec.is_confirmable
    assert d.spec.questions


def test_answering_multiheader_csv_makes_it_confirmable():
    d = draft.draft_for_path(MULTI, header_rows=[1, 2], authorised_fill=True)
    assert not d.needs_answer
    assert d.spec.is_confirmable
    assert d.spec.target_names == [
        "identifiers_order_id", "identifiers_order_date", "dimensions_region",
        "dimensions_product", "dimensions_channel", "measures_units",
        "measures_unit_price", "measures_revenue",
    ]


def test_the_blocking_message_names_the_field_and_the_question():
    d = draft.draft_for_path(MULTI)
    msg = d.spec.blocking_message()
    assert "header_rows" in msg
    assert "NEXT STEP" in msg
    assert "Do not simply delete" in msg


def test_gaps_and_dupes_names_are_resolved_and_reported():
    d = draft.draft_for_path(GAPS)
    assert d.spec.target_names == [
        "order_id", "units", "column_3", "units_2", "column_5", "revenue",
    ]
    joined = " ".join(d.spec.assumptions)
    assert "named column_N by position" in joined
    assert "units -> units_2" in joined


def test_dataset_name_defaults_to_a_safe_identifier():
    assert draft.draft_for_path(MESSY).spec.dataset_name == "messy_headers"
    assert draft.draft_for_path(GAPS).spec.dataset_name == "gaps_and_dupes"


def test_an_explicit_dataset_name_wins():
    assert draft.draft_for_path(MESSY, dataset_name="q3").spec.dataset_name == "q3"


def test_a_missing_file_is_refused_with_a_next_step():
    with pytest.raises(LoadRefused) as exc:
        draft.draft_for_path("tests/fixtures/nope.xlsx")
    assert "NEXT STEP" in str(exc.value)


def test_an_unknown_sheet_lists_the_real_ones():
    with pytest.raises(LoadRefused) as exc:
        draft.draft_for_path(MESSY, sheet="Sheet1")
    assert "Report" in str(exc.value)


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def test_render_carries_the_spec_the_reasoning_and_the_json():
    text = draft.render(draft.draft_for_path(MESSY))
    assert "loader skips 5" in text
    assert "Assumptions:" in text
    assert "```json" in text
    assert "Nothing is loaded yet" in text


def test_render_lists_the_assumptions_once_in_prose():
    """
    They were printed twice on the first run, which read as two lists. The
    copy inside the JSON block does not count -- that is the payload, not
    something the user reads twice.
    """
    text = draft.render(draft.draft_for_path(MESSY))
    prose = text.split("```json")[0]
    assert prose.count("Row 4 is blank") == 1
    assert text.count("Row 4 is blank") == 2  # prose + payload


def test_render_of_an_ambiguous_file_says_it_cannot_be_loaded():
    text = draft.render(draft.draft_for_path(MULTI))
    assert "CANNOT BE LOADED" in text
    assert "PROVISIONAL" in text
    assert "Put these to the user:" in text
    assert "propose_ingest_spec again" in text


def test_an_answered_file_does_not_still_print_open_questions():
    """
    Found in the Step 8 live run. render read guess.questions -- what the FILE
    could not settle, which never changes -- instead of spec.questions, what
    is still outstanding after a person has spoken. It printed "Open
    questions" at a user who had just answered them.
    """
    answered = draft.draft_for_path(MULTI, header_rows=[1, 2], authorised_fill=True)
    text = draft.render(answered)

    assert answered.spec.questions == []
    assert "Open questions" not in text
    assert "CANNOT BE LOADED" not in text

    unanswered = draft.render(draft.draft_for_path(MULTI))
    assert "Put these to the user:" in unanswered


def test_an_answer_raises_confidence_off_low():
    """
    Also found live. Confidence describes how well the header is known, not
    how well the file stated it. Reporting 'low' after the one ambiguity has
    been settled reports a doubt nobody holds.
    """
    assert draft.draft_for_path(MULTI).guess.confidence == "low"
    answered = draft.draft_for_path(MULTI, header_rows=[1, 2], authorised_fill=True)
    assert answered.guess.confidence == "high"


def test_answering_does_not_inflate_an_already_good_guess():
    """A file that was never ambiguous keeps whatever it had."""
    before = draft.draft_for_path(MERGED).guess.confidence
    after = draft.draft_for_path(MERGED, header_rows=[1, 2]).guess.confidence
    assert before == "medium"
    assert after == "medium"


def test_render_names_the_other_sheets_when_there_are_several():
    text = draft.render(draft.draft_for_path(MESSY))
    assert "Report" in text


# --------------------------------------------------------------------------
# the JSON round trip
# --------------------------------------------------------------------------

def test_a_rendered_spec_parses_back_to_the_same_spec():
    d = draft.draft_for_path(MESSY)
    text = draft.render(d)
    body = text.split("```json")[1].split("```")[0]
    assert draft.spec_from_json(body) == d.spec


def test_a_fenced_block_parses():
    d = draft.draft_for_path(MESSY)
    fenced = "```json\n" + d.spec.model_dump_json() + "\n```"
    assert draft.spec_from_json(fenced) == d.spec


def test_an_edited_spec_is_honoured_as_written():
    """The whole point of handing the JSON out: the user can change it."""
    d = draft.draft_for_path(MESSY)
    payload = json.loads(d.spec.model_dump_json())
    payload["footer_skip_rows"] = 0
    payload["dataset_name"] = "edited"

    spec = draft.spec_from_json(json.dumps(payload))
    assert spec.footer_skip_rows == 0
    assert spec.dataset_name == "edited"


def test_nonsense_json_is_refused_with_a_next_step():
    with pytest.raises(LoadRefused) as exc:
        draft.spec_from_json("{not json")
    assert "NEXT STEP" in str(exc.value)


def test_an_invalid_edit_is_refused_rather_than_loaded():
    """data_start_row moved above the header. Pydantic catches it here."""
    d = draft.draft_for_path(MESSY)
    payload = json.loads(d.spec.model_dump_json())
    payload["data_start_row"] = 2
    with pytest.raises(LoadRefused) as exc:
        draft.spec_from_json(json.dumps(payload))
    assert "NEXT STEP" in str(exc.value)


# --------------------------------------------------------------------------
# end to end: propose, then load what was proposed
# --------------------------------------------------------------------------

def test_the_proposed_spec_loads_messy_headers_correctly(con):
    d = draft.draft_for_path(MESSY)
    spec = draft.spec_from_json(d.spec.model_dump_json())
    result = load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))

    assert result.row_count == 200
    assert con.execute(
        "SELECT count(*) FROM messy_headers WHERE order_id LIKE 'Notes%'"
    ).fetchone()[0] == 0
    assert dict(result.columns)["units"] == "BIGINT"


def test_the_proposed_spec_loads_the_merged_workbook(con):
    d = draft.draft_for_path(MERGED)
    spec = draft.spec_from_json(d.spec.model_dump_json())
    result = load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))

    assert result.row_count == 150
    assert result.column_count == 8
    assert "identifiers_order_id" not in dict(result.columns)


def test_the_proposed_spec_loads_gaps_and_dupes(con):
    d = draft.draft_for_path(GAPS)
    spec = draft.spec_from_json(d.spec.model_dump_json())
    result = load_csv(con, spec.path, **spec.to_loader_kwargs(load_csv))

    assert result.row_count == 200
    names = [c for c, _t in result.columns]
    assert names == [
        "order_id", "units", "column_3", "units_2", "column_5", "revenue",
    ]


def test_the_csv_sentinel_is_only_caught_when_na_values_asks(con):
    """
    gaps_and_dupes.csv is the only CSV fixture carrying a literal 'N/A'. Every
    other one writes an empty field, which DuckDB nulls whatever na_values
    says -- so before this, no CSV test could tell whether na_values did
    anything at all.
    """
    d = draft.draft_for_path(GAPS)
    spec = draft.spec_from_json(d.spec.model_dump_json())

    spec.na_values = []
    spec.dataset_name = "kept"
    load_csv(con, spec.path, **spec.to_loader_kwargs(load_csv))
    kept = con.execute(
        "SELECT count(*) FILTER (WHERE column_3 IS NULL) FROM kept"
    ).fetchone()[0]

    spec.na_values = ["N/A"]
    spec.dataset_name = "nulled"
    load_csv(con, spec.path, **spec.to_loader_kwargs(load_csv))
    nulled = con.execute(
        "SELECT count(*) FILTER (WHERE column_3 IS NULL) FROM nulled"
    ).fetchone()[0]

    assert kept == 0
    assert nulled > 0


def test_a_clean_file_still_drafts_and_loads(con):
    """The easy case must not have become harder."""
    d = draft.draft_for_path(CLEAN)
    assert d.spec.header_rows == [1]
    assert d.spec.loader_header_rows == 1
    spec = draft.spec_from_json(d.spec.model_dump_json())
    result = load_csv(con, spec.path, **spec.to_loader_kwargs(load_csv))
    assert result.row_count == 500
    assert result.column_count == 8


def test_editing_the_footer_away_changes_what_loads(con):
    """
    Proof the edit is honoured rather than politely ignored: turning the
    footer skip off pulls the three notes rows in as data.
    """
    d = draft.draft_for_path(MESSY)
    payload = json.loads(d.spec.model_dump_json())
    payload["footer_skip_rows"] = 0
    payload["dataset_name"] = "with_notes"
    payload["columns"][0]["dtype"] = None

    spec = draft.spec_from_json(json.dumps(payload))
    result = load_excel(con, spec.path, **spec.to_loader_kwargs(load_excel))

    # 202, not 203: of the three footer rows one is blank, and a blank row is a gap in the
    # sheet rather than a record since Phase 14 Step 7 (P14-O8). The two notes rows load.
    assert result.row_count == 202
    assert any("1 blank row" in n for n in result.notes)
    assert con.execute(
        "SELECT count(*) FROM with_notes WHERE order_id LIKE 'Notes%'"
    ).fetchone()[0] == 1
