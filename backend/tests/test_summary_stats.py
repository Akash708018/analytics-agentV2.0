"""The registry, and the contract's measures over the contract's rows.

Phase 8, Step 5. Run from the repo root:

    uv run pytest tests/test_summary_stats.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import summary_stats as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.registry import (  # noqa: E402
    UnknownAnalysis, catalogue, get, run,
)


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "what was charged"
    unit: str | None = "GBP"


@dataclass
class FakeExclusion:
    rule: str
    reason: str = "not real revenue"
    row_count: int | None = None


@dataclass
class FakeWindow:
    start: date
    end: date


@dataclass
class FakeContract:
    dataset_name: str = "sales"
    date_column: str | None = "ts"
    analysis_window: FakeWindow | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=list)
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


SALES = """SELECT * FROM (VALUES
 (1,'shipped',   TIMESTAMP '2024-06-01 10:00:00', 10.50::DECIMAL(18,2), 3, 2.5),
 (2,'cancelled', TIMESTAMP '2024-06-02 10:00:00', 99.00::DECIMAL(18,2), 5, 9.0),
 (3,NULL,        TIMESTAMP '2024-06-03 10:00:00', 20.25::DECIMAL(18,2), 5, 1.5),
 (4,'shipped',   TIMESTAMP '2024-12-31 23:59:59', NULL,                 1, 4.0),
 (5,'shipped',   TIMESTAMP '2025-01-01 00:00:00', 88.00::DECIMAL(18,2), 2, 3.0)
) v(id, status, ts, amount, rating, unit_price)"""

YEAR = FakeWindow(start=date(2024, 1, 1), end=date(2024, 12, 31))
DROP_CANCELLED = FakeExclusion(rule="status = 'cancelled'")


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE sales AS {SALES}")
    yield c
    c.close()


def output_for(con, gate):
    scope = scope_for(con, gate)
    return run(con, gate, scope, "summary_stats")


def gate(**kw) -> FakeGate:
    caveats = kw.pop("caveats", [])
    kw.setdefault("analysis_window", YEAR)
    kw.setdefault("known_exclusions", [DROP_CANCELLED])
    kw.setdefault("measures", [FakeMeasure("amount")])
    return FakeGate(contract=FakeContract(**kw), caveats=caveats)


def cell(out, measure, column):
    row = next(r for r in out.rows if r[0] == measure)
    return row[out.headers.index(column)]


# --- the registry


def test_an_unknown_type_comes_back_with_the_list():
    with pytest.raises(UnknownAnalysis) as e:
        get("summry_stats")
    assert "summary_stats" in str(e.value)


def test_the_catalogue_reports_name_tier_and_a_sentence():
    entry = next(c for c in catalogue() if c[0] == "summary_stats")
    assert entry[1] == 1
    assert "measure" in entry[2]


def test_an_analysis_refuses_parameters_it_does_not_take(con):
    g = gate()
    scope = scope_for(con, g)
    with pytest.raises(TypeError):
        run(con, g, scope, "summary_stats", n=5)


# --- the numbers


def test_the_total_is_over_the_scope_not_the_table(con):
    """Row 2 is cancelled and row 5 is outside the window; three rows are
    analysed and one of those three has no amount."""
    out = output_for(con, gate())
    assert cell(out, "amount", "total") == "30.75"
    assert cell(out, "amount", "n") == "2"
    assert out.summary[0].startswith("3 of 5 row(s) analysed")


def test_a_null_inside_the_scope_is_counted_not_dropped(con):
    """Row 4 is in the window and has no amount."""
    out = output_for(con, gate())
    assert cell(out, "amount", "nulls") == "1"


def test_the_declared_aggregate_is_used_rather_than_sum(con):
    out = output_for(con, gate(measures=[FakeMeasure("unit_price", agg="mean")]))
    assert cell(out, "unit_price", "agg") == "mean"
    assert cell(out, "unit_price", "total") == "2.6667"


def test_a_measure_with_no_declared_aggregate_is_not_totalled(con):
    """Measure.agg has no default on purpose. The guess that gets guessed is
    sum, and summing a unit price produces a number nobody can detect."""
    out = output_for(con, gate(measures=[FakeMeasure("unit_price", agg=None)]))
    assert cell(out, "unit_price", "total") == ""
    assert cell(out, "unit_price", "agg") == "(not declared)"
    assert any("no declared aggregate" in s for s in out.summary)


def test_a_non_additive_measure_gets_no_total_even_as_a_convenience(con):
    out = output_for(con, gate(measures=[FakeMeasure("unit_price", agg="none")]))
    assert cell(out, "unit_price", "total") == "not additive"
    assert cell(out, "unit_price", "min") == "1.5"


def test_a_decimal_keeps_the_scale_its_column_declared(con):
    """DuckDB returns DECIMAL as decimal.Decimal. 10.50 on a DECIMAL(18,2) is
    two places because somebody declared two -- that is not float noise and is
    not rounded away."""
    out = output_for(con, gate())
    assert cell(out, "amount", "min") == "10.50"


def test_the_mean_is_not_seventeen_digits_wide(con):
    """format_table renders cells with str(), so a float arrives exactly as
    wide as it is. avg of a money column is 2.6666666666666665 raw."""
    out = output_for(con, gate(measures=[FakeMeasure("unit_price", agg="mean")]))
    assert cell(out, "unit_price", "mean") == "2.6667"


def test_spread_on_one_row_is_blank_and_says_so(con):
    con.execute("CREATE TABLE one AS SELECT * FROM sales WHERE id = 1")
    out = output_for(con, gate(dataset_name="one"))
    assert cell(out, "amount", "stddev") is None
    assert any("blank spread is not zero" in s for s in out.summary)


def test_a_date_measure_reports_extremes_and_no_average(con):
    out = output_for(con, gate(measures=[FakeMeasure("ts", agg="min", unit=None)]))
    assert cell(out, "ts", "mean") is None
    assert "2024-06-01" in str(cell(out, "ts", "min"))


# --- what it refuses to summarise


def test_the_key_column_is_never_summarised(con):
    out = output_for(con, gate())
    assert not any(r[0] == "id" for r in out.rows)
    assert any("key column(s) id" in s for s in out.summary)


def test_an_undeclared_numeric_column_is_named_not_summarised(con):
    """P8-O1: the contract is the authority. rating is an integer nobody
    declared, and only a person knows if it is a rating or a bucket."""
    out = output_for(con, gate())
    assert not any(r[0] == "rating" for r in out.rows)
    assert any("undeclared numeric column(s)" in s and "rating" in s
               for s in out.summary)


def test_an_excluded_column_is_not_read(con):
    out = output_for(con, gate(
        measures=[FakeMeasure("amount"), FakeMeasure("unit_price", agg="mean")],
        excluded_columns=["unit_price"]))
    assert not any(r[0] == "unit_price" for r in out.rows)
    assert any("excluded_columns" in s for s in out.summary)


# --- what travels with the numbers


def test_the_method_note_leads_the_summary(con):
    out = output_for(con, gate())
    assert out.summary[0].startswith("3 of 5 row(s) analysed")


def test_the_gates_caveats_travel_with_the_result(con):
    out = output_for(con, gate(caveats=["the table gained 30 rows since v1"]))
    assert "the table gained 30 rows since v1" in out.summary


def test_an_unknown_aggregate_is_refused_by_name(con):
    with pytest.raises(ValueError) as e:
        output_for(con, gate(measures=[FakeMeasure("amount", agg="geomean")]))
    assert "geomean" in str(e.value)


def test_the_median_agrees_with_the_mean_rather_than_the_column_scale(con):
    """DuckDB computes median of a DECIMAL at the column's scale: the median of
    10.50 and 20.25 is 15.375 and comes back as Decimal('15.37'). Printed beside
    a mean of 15.375 that reads as a bug in one of them."""
    out = output_for(con, gate())
    assert cell(out, "amount", "median") == "15.375"
    assert cell(out, "amount", "mean") == "15.375"
    assert cell(out, "amount", "min") == "10.50", "values keep the column's scale"
