"""The stress matrix's fourteen bugs (Phase 14 Step 7, P14-O3 to O12), one test each or more.

The files are the matrix's own generated cases (scripts/stress_matrix.py), so what failed there is
exactly what is tested here; each expectation is the generator's plain-Python ground truth.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "scripts"))

import stress_matrix as sm  # noqa: E402

from backend.engine import server, workspace  # noqa: E402
from backend.engine.util import db  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

CASES = {c.name: c for c in sm.cases()}


@pytest.fixture()
def be():
    return RealBackend()


@pytest.fixture()
def ws(be):
    wid = be.new_workspace_id()
    yield wid
    workspace.reset(wid)
    workspace.workspace_dir(wid).rmdir()


def _upload(be, ws, name):
    c = CASES[name]
    return be.save_upload(ws, c.filename, c.data).path


def _load(be, ws, name, **draft_args):
    d = be.draft_ingest(ws, _upload(be, ws, name), **draft_args)
    assert d.refusal is None, d.refusal and d.refusal.text
    assert not d.unresolved, d.unresolved
    r = be.confirm_ingest(ws, d.spec)
    assert r.ok, r.refusal and r.refusal.text
    return d, r


def _q(ws, sql):
    con = db.connect(ws)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _steps(be, ws, table):
    return {(s.kind, s.column): s for s in be.propose_cleaning(ws, table).steps}


# --- B1: a CSV total row -------------------------------------------------------------------------

def test_b1_a_csv_total_row_is_found_and_dropped(be, ws):
    d, r = _load(be, ws, "csv_total_row_at_bottom")
    assert d.footer_skip_rows == 1
    assert any("Total" in a for a in d.assumptions), d.assumptions
    t = CASES["csv_total_row_at_bottom"].truth
    n, units = _q(ws, "SELECT count(*), sum(units) FROM with_total")[0]
    assert (n, units) == (t.rows, t.sums["units"])


def test_b1_a_csv_without_a_footer_keeps_every_row(be, ws):
    d, _ = _load(be, ws, "baseline_csv")
    assert d.footer_skip_rows == 0
    assert _q(ws, "SELECT count(*) FROM baseline")[0][0] == 500


# --- B2: leading zeros ---------------------------------------------------------------------------

def test_b2_leading_zero_ids_are_not_offered_as_numbers(be, ws):
    """B2 first offered the conversion as lossy and never suggested (P14-O4). Main's Cleanup Step
    12 (RF-O5) went further -- a zero-padded code is not offered as a number at all -- and the
    merge of 25/09/2026 adopted it: the zeros cannot be lost by approving the wrong step."""
    _load(be, ws, "leading_zero_ids")
    steps = _steps(be, ws, "zips")
    assert ("CONVERT_TYPE", "zip") not in steps, steps.get(("CONVERT_TYPE", "zip"))


# --- B3: Excel formulas --------------------------------------------------------------------------

def test_b3_formula_columns_load_excels_cached_values(be, ws):
    _load(be, ws, "xlsx_formulas_cached")
    t = CASES["xlsx_formulas_cached"].truth
    typ, total = _q(ws, "SELECT any_value(typeof(revenue)), round(sum(revenue), 2) "
                          "FROM formulas_cached")[0]
    assert typ in ("DOUBLE", "DECIMAL(18,2)") and total == t.sums["revenue"]


def test_b3_formulas_with_no_saved_value_are_named_in_the_draft(be, ws):
    d = be.draft_ingest(ws, _upload(be, ws, "xlsx_formulas_no_cache"))
    assert any("formula" in a and "revenue" in a for a in d.assumptions), d.assumptions
    assert be.confirm_ingest(ws, d.spec).ok
    assert _q(ws, "SELECT count(*) FROM formulas WHERE revenue LIKE '=%'")[0][0] == 0


# --- B4: Infinity and NaN ------------------------------------------------------------------------

@pytest.fixture()
def infinite(be, ws):
    _load(be, ws, "scientific_nan_inf")
    draft = be.draft_contract(ws, "sci", grain="one row = one reading", primary_key=["id"],
                              measures=["reading"], dimensions=[],
                              aggregations={"reading": "sum"},
                              measure_definitions={"reading": "a reading"})
    assert be.confirm_contract(ws, draft).ok
    return ws


@pytest.mark.parametrize("kind", ["summary_stats", "outlier_detection", "distribution",
                                  "confidence_interval"])
def test_b4_non_finite_values_are_set_aside_and_said(infinite, kind):
    kw = {} if kind == "summary_stats" else {"measure": "reading"}
    out = server.compute_analysis(dataset_name="sci", analysis_type=kind,
                                  workspace_id=infinite, **kw)
    assert not out.startswith("BLOCKED"), out[:400]
    assert "finite" in out, out[:1200]


def test_b4_profile_answers_on_non_finite_values(infinite):
    out = server.profile_dataset(dataset_name="sci", workspace_id=infinite)
    assert "reading" in out and "not a finite number" in out, out[:1500]


def test_b4_cleaning_offers_to_null_non_finite_values(be, infinite):
    step = _steps(be, infinite, "sci").get(("NULL_NON_FINITE", "reading"))
    assert step is not None and step.rows_affected == 75 and step.lossy
    assert be.apply_cleaning(infinite, "sci", [step.action_id]).ok
    assert _q(infinite, "SELECT count(*) FROM sci WHERE NOT isfinite(reading)")[0][0] == 0


# --- B5: an empty first sheet --------------------------------------------------------------------

def test_b5_an_empty_first_sheet_is_passed_over_and_said(be, ws):
    d = be.draft_ingest(ws, _upload(be, ws, "xlsx_empty_first_sheet"))
    assert d.refusal is None and d.grid.sheet == "Data", d.message
    assert any("Cover" in a for a in d.assumptions), d.assumptions


def test_b5_naming_an_empty_sheet_is_a_refusal_not_an_exception(be, ws):
    d = be.draft_ingest(ws, _upload(be, ws, "xlsx_empty_first_sheet"), sheet="Cover")
    assert d.refusal is not None and "Data" in d.refusal.text


# --- B6: blank Excel rows ------------------------------------------------------------------------

def test_b6_blank_rows_inside_excel_data_are_skipped_and_counted(be, ws):
    _, r = _load(be, ws, "xlsx_blank_rows_mid")
    assert _q(ws, "SELECT count(*) FROM blanks")[0][0] == 160
    assert "3 blank row" in r.message, r.message


# --- B7: Latin-1 ---------------------------------------------------------------------------------

def test_b7_a_latin1_csv_loads_with_its_text_intact(be, ws):
    _, r = _load(be, ws, "latin1_encoding")
    regions = {v for (v,) in _q(ws, "SELECT DISTINCT region FROM latin1")}
    assert {"Sünd", "Nörd"} <= regions
    # Its letters are the same bytes in Windows-1252, which is sniffed first (P14-D47).
    assert "not UTF-8" in r.message


def test_b7_a_load_failure_never_shows_the_servers_path(be, ws):
    path = be.save_upload(ws, "bad.csv", b"a,b\n1,2\n").path
    d = be.draft_ingest(ws, path)
    spec = dict(d.spec, delimiter="|", columns=d.spec["columns"])  # a wrong delimiter
    r = be.confirm_ingest(ws, spec)
    if not r.ok:
        assert str(workspace.WORKSPACE_ROOT) not in r.refusal.text


# --- B8, B9, B12: numbers and dates written as text ----------------------------------------------

def test_b8_decimal_commas_get_a_conversion(be, ws):
    _load(be, ws, "semicolon_decimal_comma")
    step = _steps(be, ws, "eu_sales").get(("CONVERT_TYPE", "revenue"))
    assert step is not None and not step.lossy and step.suggested, step
    assert be.apply_cleaning(ws, "eu_sales", [step.action_id]).ok
    t = CASES["semicolon_decimal_comma"].truth
    assert round(float(_q(ws, "SELECT sum(revenue) FROM eu_sales")[0][0]), 2) == t.sums["revenue"]


def test_b9_currency_and_thousands_separators_get_a_conversion(be, ws):
    _load(be, ws, "currency_percent_thousands")
    steps = _steps(be, ws, "currency")
    rev = steps.get(("CONVERT_TYPE", "revenue"))
    assert rev is not None and not rev.lossy and rev.suggested
    pct = steps.get(("CONVERT_TYPE", "discount"))
    assert pct is not None and not pct.lossy and "%" in pct.intent
    assert be.apply_cleaning(ws, "currency", [rev.action_id]).ok
    t = CASES["currency_percent_thousands"].truth
    assert round(float(_q(ws, "SELECT sum(revenue) FROM currency")[0][0]), 2) == t.sums["revenue"]


def test_b12_mixed_date_formats_get_a_conversion(be, ws):
    _load(be, ws, "dates_mixed_formats")
    step = _steps(be, ws, "mixdates").get(("CONVERT_TYPE", "order_date"))
    assert step is not None and not step.lossy, step
    assert be.apply_cleaning(ws, "mixdates", [step.action_id]).ok
    lo, hi = _q(ws, "SELECT min(order_date)::DATE::VARCHAR, max(order_date)::DATE::VARCHAR "
                    "FROM mixdates")[0]
    assert (lo, hi) == CASES["dates_mixed_formats"].truth.date_range


# --- B10, B11: header-only and repeated headers --------------------------------------------------

def test_b10_a_header_only_csv_says_it_has_no_data_rows(be, ws):
    d = be.draft_ingest(ws, _upload(be, ws, "header_only"), header_rows=[1])
    r = be.confirm_ingest(ws, d.spec)
    assert not r.ok and "no data rows" in r.refusal.text, r.refusal.text


def test_b11_a_repeated_header_is_found_and_dropping_it_is_free(be, ws):
    _load(be, ws, "repeated_header_mid_file")
    step = _steps(be, ws, "pasted").get(("DROP_HEADER_ROWS", None))
    assert step is not None and step.rows_affected == 1 and step.suggested and not step.lossy
    assert be.apply_cleaning(ws, "pasted", [step.action_id]).ok
    conv = _steps(be, ws, "pasted").get(("CONVERT_TYPE", "units"))
    assert conv is not None and not conv.lossy and conv.suggested


# --- B13, B14: refusal wording, role suggestions -------------------------------------------------

def test_b13_a_missing_argument_is_named_in_the_engines_words(be, ws):
    _load(be, ws, "baseline_csv")
    draft = be.draft_contract(ws, "baseline", grain="one row = one order", primary_key=["order_id"],
                              date_column="order_date", measures=["units"], dimensions=["region"],
                              aggregations={"units": "sum"}, measure_definitions={"units": "u"},
                              analysis_window_start="2023-01-01", analysis_window_end="2024-12-31")
    assert be.confirm_contract(ws, draft).ok
    out = server.compute_analysis(dataset_name="baseline", analysis_type="top_n", workspace_id=ws)
    assert "positional argument" not in out and "top_n()" not in out
    assert 'measure="units"' in out and 'dimension="region"' in out, out


def test_b14_a_fractional_column_with_few_values_is_a_measure(be, ws):
    _load(be, ws, "scientific_nan_inf")
    roles = {c.name: c.suggested_role for c in be.draft_contract(ws, "sci").columns}
    assert roles["reading"] == "measure"


def test_b14_one_row_is_not_every_column_constant(be, ws):
    d, _ = _load(be, ws, "one_row")
    roles = {c.name: c.suggested_role for c in be.draft_contract(ws, d.dataset_name).columns}
    assert roles["revenue"] == "measure" and roles["region"] == "dimension", roles


# --- Step 8, part A: what remained after Step 7 --------------------------------------------------

def _csv(be, ws, name, text, encoding="utf-8"):
    return be.save_upload(ws, name, text.encode(encoding)).path


def test_r1_a_windows_1252_csv_keeps_its_euro_signs_and_curly_quotes(be, ws):
    path = _csv(be, ws, "cp.csv", "item,price\n€ voucher,5\n“Quoted” name – dash,7\n", "cp1252")
    d = be.draft_ingest(ws, path)
    r = be.confirm_ingest(ws, d.spec)
    assert r.ok, r.refusal and r.refusal.text
    items = {v for (v,) in _q(ws, "SELECT item FROM cp")}
    assert items == {"€ voucher", "“Quoted” name – dash"}
    assert "Windows-1252" in r.message


def test_r1_a_utf16_csv_loads(be, ws):
    path = _csv(be, ws, "u16.csv", "item\tqty\nSüd\t3\n北部\t4\n", "utf-16")
    d = be.draft_ingest(ws, path)
    r = be.confirm_ingest(ws, d.spec)
    assert r.ok, r.refusal and r.refusal.text
    assert _q(ws, "SELECT sum(qty) FROM u16")[0][0] == 7
    assert {v for (v,) in _q(ws, "SELECT item FROM u16")} == {"Süd", "北部"}


def test_r2_a_full_last_row_labelled_total_is_data_not_a_footer(be, ws):
    rows = "".join(f"{r},2024-01-0{i + 1},{i + 1}\n" for i, r in enumerate(["North", "South", "Total"]))
    path = _csv(be, ws, "cats.csv", "region,day,units\n" + rows)
    d = be.draft_ingest(ws, path)
    assert d.footer_skip_rows == 0
    assert be.confirm_ingest(ws, d.spec).ok
    assert _q(ws, "SELECT count(*) FROM cats")[0][0] == 3


def test_r3_an_excel_error_names_the_sheet_row_after_blank_rows(tmp_path):
    import duckdb
    from backend.engine.ingest.csv_loader import LoadRefused
    from backend.engine.ingest.excel import load_excel
    path = tmp_path / "rows.xlsx"
    path.write_bytes(sm.to_xlsx([("S", [["id", "n"], ["a", 1], ["b", 2], [None, None],
                                        [None, None], ["c", "oops"]])]))
    con = duckdb.connect()
    with pytest.raises(LoadRefused) as exc:
        load_excel(con, path, "rows", inference_rows=2)
    assert "row 6 of rows.xlsx" in str(exc.value), str(exc.value)


def test_r4_a_column_that_reads_both_ways_is_offered_both_readings_and_neither_suggested(be, ws):
    vals = ["1,234", "2,500", "3,750", "1,000", "12,345"] * 10
    path = _csv(be, ws, "amb.csv", "id,amount\n" + "".join(f"{i},\"{v}\"\n" for i, v in enumerate(vals)))
    d = be.draft_ingest(ws, path)
    assert be.confirm_ingest(ws, d.spec).ok
    steps = [s for s in be.propose_cleaning(ws, "amb").steps if s.column == "amount"]
    assert len(steps) == 2 and not any(s.suggested for s in steps), steps
    both = be.apply_cleaning(ws, "amb", [s.action_id for s in steps])
    assert not both.ok and both.refusal.reason == "ACTIONS_CONFLICT"
    thousands = next(s for s in steps if "thousands" in s.intent)
    assert be.apply_cleaning(ws, "amb", [thousands.action_id]).ok
    assert _q(ws, "SELECT sum(amount) FROM amb")[0][0] == sum(
        int(v.replace(",", "")) for v in vals)


def test_r5_the_tail_probe_starting_inside_a_quoted_field_still_finds_the_footer(tmp_path):
    from backend.engine.ingest import csv_loader, draft
    body = "id,note,amount\n" + "".join(
        f'{i},"line one of {i}\n' + "x" * 300 + f'\nline three",{i}\n' for i in range(400))
    path = tmp_path / "multi.csv"
    path.write_text(body + "Total,,79800\n", encoding="utf-8")
    assert path.stat().st_size > csv_loader.PROBE_BYTES
    d = draft.draft_for_path(path)
    assert d.spec.footer_skip_rows == 1, d.spec.assumptions


def test_r6_a_float_columns_range_is_over_its_finite_values(infinite):
    out = server.profile_dataset(dataset_name="sci", workspace_id=infinite)
    line = next(ln for ln in out.splitlines() if "reading (DOUBLE)" in ln)
    assert "range -7 to 100000" in line.replace(".0", ""), line
    assert "nan" not in line and "inf" not in line


# --- Step 8, round 2 of the matrix: new anomalies ------------------------------------------------

R2 = {c.name: c for c in sm.cases_round2()}


def _load2(be, ws, name, **draft_args):
    c = R2[name]
    d = be.draft_ingest(ws, be.save_upload(ws, c.filename, c.data).path, **draft_args)
    assert d.refusal is None and not d.unresolved, (d.refusal and d.refusal.text, d.unresolved)
    r = be.confirm_ingest(ws, d.spec)
    assert r.ok, r.refusal and r.refusal.text
    return d, r


def test_r2_timestamps_with_an_offset_load_as_utc_and_describe_answers(be, ws):
    d, r = _load2(be, ws, "timestamps_with_offsets")
    assert _q(ws, "SELECT any_value(typeof(order_date)) FROM tz")[0][0] == "TIMESTAMP"
    assert "UTC" in r.message
    out = server.describe_dataset(dataset_name="tz", workspace_id=ws)
    assert not out.startswith("BLOCKED") and "order_date" in out


def test_r2_rows_short_of_fields_are_padded_not_refused(be, ws):
    _, r = _load2(be, ws, "ragged_rows")
    t = R2["ragged_rows"].truth
    assert _q(ws, "SELECT count(*), sum(units) FROM ragged")[0] == (t.rows, t.sums["units"])
    assert "fewer fields" in r.message


def test_r2_a_trailing_delimiter_adds_no_column(be, ws):
    _, r = _load2(be, ws, "trailing_delimiter")
    assert len(_q(ws, "DESCRIBE \"trailing\"")) == 9
    assert "ends with a delimiter" in r.message


def test_r2_accounting_negatives_convert_to_negative_numbers(be, ws):
    _load2(be, ws, "accounting_negatives")
    step = _steps(be, ws, "accounting").get(("CONVERT_TYPE", "revenue"))
    assert step is not None and not step.lossy and step.suggested
    assert be.apply_cleaning(ws, "accounting", [step.action_id]).ok
    want = R2["accounting_negatives"].truth.sums["revenue"]
    assert round(float(_q(ws, "SELECT sum(revenue) FROM accounting")[0][0]), 2) == want


def test_r2_excel_error_cells_load_empty_and_are_counted(be, ws):
    _, r = _load2(be, ws, "xlsx_error_cells")
    want = R2["xlsx_error_cells"].truth.sums["revenue"]
    typ, total = _q(ws, "SELECT any_value(typeof(revenue)), sum(revenue) FROM errors")[0]
    assert typ == "DOUBLE" and round(total, 2) == want
    assert "Excel error" in r.message and "#DIV/0!" in r.message


def test_r2_a_vertical_merge_fills_every_row_it_covers(be, ws):
    _, r = _load2(be, ws, "xlsx_vertically_merged_cells")
    assert _q(ws, "SELECT count(*) FROM vmerge WHERE region IS NULL")[0][0] == 0
    assert "merged ranges" in r.message


def test_r2_comment_lines_are_comments_and_mixed_line_endings_load(be, ws):
    d, r = _load2(be, ws, "comment_lines_above_header")
    assert d.header_rows == [3] and any("comments" in a for a in d.assumptions)
    assert _q(ws, "SELECT count(*) FROM commented")[0][0] == 140


def test_r2_mixed_line_endings_load(be, ws):
    _, r = _load2(be, ws, "mixed_line_endings")
    assert _q(ws, "SELECT count(*) FROM mixed_eol")[0][0] == 150
    assert "CRLF" in r.message


def test_r2_a_header_of_years_is_a_header(be, ws):
    d, _ = _load2(be, ws, "years_as_columns")
    assert d.header_rows == [1]
    assert _q(ws, "SELECT count(*) FROM \"pivot\"")[0][0] == 4


def test_r2_integers_beyond_bigint_keep_every_digit(be, ws):
    _, r = _load2(be, ws, "integers_beyond_int64")
    typ, total = _q(ws, "SELECT any_value(typeof(amount)), sum(amount) FROM huge")[0]
    assert typ == "HUGEINT" and total == R2["integers_beyond_int64"].truth.sums["amount"]
    assert "HUGEINT" in r.message


# --- Step 8, round 3 ---------------------------------------------------------------------------

R3 = {c.name: c for c in sm.cases_round3()}


def _load3(be, ws, name):
    c = R3[name]
    d = be.draft_ingest(ws, be.save_upload(ws, c.filename, c.data).path)
    assert d.refusal is None and not d.unresolved, (d.refusal and d.refusal.text, d.unresolved)
    r = be.confirm_ingest(ws, d.spec)
    assert r.ok, r.refusal and r.refusal.text
    return d, r


def test_r3_two_digit_years_are_not_read_year_first(be, ws):
    """DuckDB read 31/12/24 as 2031-12-24 and the column loaded as a valid DATE, all wrong."""
    _, r = _load3(be, ws, "two_digit_years")
    assert _q(ws, "SELECT any_value(typeof(order_date)) FROM yy")[0][0] == "VARCHAR"
    assert "two-digit year" in r.message
    step = _steps(be, ws, "yy").get(("CONVERT_TYPE", "order_date"))
    assert step is not None and step.suggested and "day first" in step.intent
    assert be.apply_cleaning(ws, "yy", [step.action_id]).ok
    lo, hi = _q(ws, "SELECT min(order_date)::VARCHAR, max(order_date)::VARCHAR FROM yy")[0]
    assert (lo, hi) == R3["two_digit_years"].truth.date_range


def test_r3_a_header_cell_holding_a_line_break_loads(be, ws):
    _load3(be, ws, "header_cells_with_newlines")
    assert _q(ws, "SELECT count(*) FROM nlheader")[0][0] == 120


def test_r3_an_accented_file_name_is_made_safe_not_refused(be, ws):
    up = be.save_upload(ws, "Ventes 2024 (été).csv", b"a,b\n1,2\n")
    assert up.verdict == "OK" and up.path.endswith("Ventes 2024 _ete.csv")


def test_r3_sap_trailing_minus_reads_as_negative(be, ws):
    _load3(be, ws, "trailing_minus_numbers")
    step = _steps(be, ws, "sap").get(("CONVERT_TYPE", "revenue"))
    assert step is not None and not step.lossy and step.suggested
    assert be.apply_cleaning(ws, "sap", [step.action_id]).ok
    want = R3["trailing_minus_numbers"].truth.sums["revenue"]
    assert round(float(_q(ws, "SELECT sum(revenue) FROM sap")[0][0]), 2) == want


def test_r3_mysql_zero_dates_are_absent_not_lost(be, ws):
    _load3(be, ws, "mysql_zero_dates")
    step = _steps(be, ws, "zerodates").get(("CONVERT_TYPE", "order_date"))
    assert step is not None and not step.lossy and step.suggested
    assert be.apply_cleaning(ws, "zerodates", [step.action_id]).ok
    assert _q(ws, "SELECT count(*) FROM zerodates WHERE order_date IS NULL")[0][0] == 10
