"""One measure per group of one dimension, against an (all) row.

Phase 8, Step 8b. Run from the repo root:

    uv run pytest tests/test_group_compare.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import group_compare as _  # noqa: E402,F401
from backend.engine.analysis.base import LostRows, scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


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
    dimensions: list = field(default_factory=lambda: ["status"])
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


def gate(**kw) -> FakeGate:
    caveats = kw.pop("caveats", [])
    kw.setdefault("analysis_window", YEAR)
    kw.setdefault("known_exclusions", [DROP_CANCELLED])
    kw.setdefault("measures", [FakeMeasure("amount")])
    return FakeGate(contract=FakeContract(**kw), caveats=caveats)


def out_for(con, g, **params):
    params.setdefault("dimension", "status")
    params.setdefault("measure", g.contract.measures[0].name)
    return run(con, g, scope_for(con, g), "group_compare", **params)


def cell(out, group, column):
    row = next(r for r in out.rows if r[0] == group)
    return row[out.headers.index(column)]


def test_it_is_registered_in_tier_two():
    entry = next(c for c in catalogue() if c[0] == "group_compare")
    assert entry[1] == 2


def test_one_row_per_group_ordered_by_name_with_all_last(con):
    out = out_for(con, gate())
    assert [r[0] for r in out.rows] == ["shipped", "(null)", "(all)"]


def test_the_groups_hold_the_scopes_rows_not_the_tables(con):
    out = out_for(con, gate())
    assert cell(out, "shipped", "n") == "1"
    assert cell(out, "shipped", "nulls") == "1"
    assert cell(out, "(all)", "n") == "2"
    assert out.summary[0].startswith("3 of 5 row(s) analysed")


def test_the_all_row_is_over_the_rows_not_the_group_cells(con):
    """P8-D43: the mean of the group means is 15.375 here only by accident of
    two groups; the stddev is the case that cannot be recombined at all."""
    out = out_for(con, gate())
    assert cell(out, "(all)", "mean") == "15.375"
    assert cell(out, "(all)", "stddev") == "6.8943"
    assert cell(out, "shipped", "stddev") is None
    assert any("not from the group rows" in s for s in out.summary)


def test_null_is_a_group_with_a_name(con):
    out = out_for(con, gate())
    assert cell(out, "(null)", "total (GBP)") == "20.25"
    assert any("not the empty string" in s for s in out.summary)


def test_a_decimal_keeps_the_scale_its_column_declared(con):
    out = out_for(con, gate())
    assert cell(out, "shipped", "min") == "10.50"


def test_the_share_column_appears_for_an_additive_aggregate(con):
    out = out_for(con, gate())
    assert cell(out, "shipped", "share") == "34.1%"
    assert cell(out, "(null)", "share") == "65.9%"
    assert cell(out, "(all)", "share") == "100.0%"


def test_a_mean_measure_has_a_total_column_and_no_share(con):
    g = gate(measures=[FakeMeasure("unit_price", agg="mean")])
    out = out_for(con, g)
    assert "share" not in out.headers
    assert cell(out, "(all)", "total (GBP)") == "2.6667"
    assert any("mean does not add up across groups" in s for s in out.summary)


def test_the_total_column_is_never_named_after_the_aggregate(con):
    """A column headed 'mean' beside the mean statistic is two columns with one
    name, which cross_tab's label check refuses."""
    g = gate(measures=[FakeMeasure("unit_price", agg="mean", unit=None)])
    out = out_for(con, g)
    assert out.headers.count("mean") == 1
    assert "total" in out.headers
    assert len(out.headers) == len(set(out.headers))


def test_a_non_additive_measure_gets_no_total_column(con):
    g = gate(measures=[FakeMeasure("unit_price", agg="none")])
    out = out_for(con, g)
    assert not any(h.startswith("total") for h in out.headers)
    assert cell(out, "shipped", "min") == "2.5"
    assert any("non-additive" in s for s in out.summary)


def test_a_measure_with_no_aggregate_gets_no_total_column(con):
    g = gate(measures=[FakeMeasure("unit_price", agg=None)])
    out = out_for(con, g)
    assert not any(h.startswith("total") for h in out.headers)
    assert any("no declared aggregate" in s for s in out.summary)


def test_an_undeclared_dimension_is_refused_with_the_list(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), dimension="rating")
    assert "status" in str(e.value)


def test_an_excluded_dimension_is_refused(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(excluded_columns=["status"]))
    assert "never to read it" in str(e.value)


def test_an_undeclared_measure_is_refused_with_the_list(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), measure="rating")
    assert "amount" in str(e.value)


def test_an_unknown_aggregate_is_refused_by_name(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(measures=[FakeMeasure("amount", agg="geomean")]))
    assert "geomean" in str(e.value)


def test_too_many_groups_are_refused_with_the_count(con):
    con.execute("CREATE TABLE wide AS SELECT range AS id, "
                "('g' || range) AS status, TIMESTAMP '2024-06-01' AS ts, "
                "range::DECIMAL(18,2) AS amount FROM range(60)")
    with pytest.raises(ValueError) as e:
        out_for(con, gate(dataset_name="wide", known_exclusions=[]))
    assert "60 group(s)" in str(e.value)
    assert "top_n" in str(e.value)


def test_an_empty_scope_compares_nothing_and_says_why(con):
    g = gate(analysis_window=FakeWindow(date(2030, 1, 1), date(2030, 12, 31)))
    out = out_for(con, g)
    assert out.rows == []
    assert any("nothing to compare" in s for s in out.summary)


def test_parameters_it_does_not_take_are_refused(con):
    with pytest.raises(TypeError):
        out_for(con, gate(), n=5)


def test_the_method_note_and_caveats_travel(con):
    out = out_for(con, gate(caveats=["gained 30 rows since v1"]))
    assert out.summary[0].startswith("3 of 5 row(s) analysed")
    assert "gained 30 rows since v1" in out.summary


def test_lost_rows_is_available_to_this_analysis():
    assert issubclass(LostRows, ValueError)
