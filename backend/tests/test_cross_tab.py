"""One dimension against another, with NULL as a group and honest margins.

Phase 8, Step 7c. Run from the repo root:

    uv run pytest tests/test_cross_tab.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import cross_tab as _ct  # noqa: E402,F401
from backend.engine.analysis.base import LostRows, Scope, scope_for  # noqa: E402
from backend.engine.analysis.cross_tab import (  # noqa: E402
    MAX_COLUMN_GROUPS,
    MAX_ROW_GROUPS,
    TOTAL,
)
from backend.engine.analysis.registry import run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "what was charged"
    unit: str | None = None


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
    measures: list = field(default_factory=lambda: [FakeMeasure("amount")])
    dimensions: list = field(default_factory=lambda: ["region", "channel"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


YEAR = FakeWindow(date(2024, 1, 1), date(2024, 12, 31))

# Ten rows, one of them in 2025. Row 8 is in scope and its amount is NULL, so a
# sum cell exists with no value in it. Every expected value below came from
# plain SQL against this table.
FIXTURE = """
CREATE TABLE sales AS SELECT * FROM (VALUES
 (1, 'North', 'Retail', TIMESTAMP '2024-02-01', 10.00::DECIMAL(18,2)),
 (2, 'North', 'Online', TIMESTAMP '2024-03-01', 20.00::DECIMAL(18,2)),
 (3, 'North', NULL,     TIMESTAMP '2024-04-01',  5.00::DECIMAL(18,2)),
 (4, 'South', 'B''s',   TIMESTAMP '2024-05-01',  7.00::DECIMAL(18,2)),
 (5, 'South', 'Online', TIMESTAMP '2024-06-01', 30.00::DECIMAL(18,2)),
 (6, NULL,    'Retail', TIMESTAMP '2024-07-01',  1.00::DECIMAL(18,2)),
 (7, NULL,    '',       TIMESTAMP '2024-08-01',  4.00::DECIMAL(18,2)),
 (8, 'South', 'Retail', TIMESTAMP '2024-09-01', NULL),
 (9, 'East',  'Online', TIMESTAMP '2025-03-01', 99.00::DECIMAL(18,2)),
 (10,'North', 'Online', TIMESTAMP '2024-10-01', 40.00::DECIMAL(18,2))
) v(id, region, channel, ts, amount)
"""


@pytest.fixture
def con():
    with duckdb.connect(":memory:") as connection:
        connection.execute(FIXTURE)
        yield connection


def gate(**kwargs):
    kwargs.setdefault("analysis_window", YEAR)
    caveats = kwargs.pop("caveats", ["the loader reported 2 bad line(s)"])
    return FakeGate(FakeContract(**kwargs), caveats=caveats)


def out(con, rows="region", columns="channel", measure=None, **kwargs):
    g = gate(**kwargs)
    return run(con, g, scope_for(con, g), "cross_tab",
               rows=rows, columns=columns, measure=measure)


def cell(output, row_label, header):
    line = next(r for r in output.rows if r[0] == row_label)
    return line[output.headers.index(header)]


def test_every_combination_is_counted_in_a_deterministic_order(con):
    o = out(con)
    assert o.headers == ["region", "", "B's", "Online", "Retail", "(null)", TOTAL]
    assert o.rows == [
        ["North", "0", "0", "2", "1", "1", "4"],
        ["South", "0", "1", "1", "1", "0", "3"],
        ["(null)", "1", "0", "0", "1", "0", "2"],
        [TOTAL, "1", "1", "3", "3", "1", "9"],
    ]


def test_null_is_a_row_and_a_column_with_a_name(con):
    o = out(con)
    assert "(null)" in [r[0] for r in o.rows]
    assert "(null)" in o.headers
    text = " ".join(o.summary)
    assert "have no region and are the (null) row" in text
    assert "have no channel and are the (null) column" in text


def test_the_empty_string_is_its_own_column(con):
    o = out(con)
    assert "" in o.headers
    assert cell(o, "(null)", "") == "1"
    assert any("empty string" in s for s in o.summary)


def test_a_value_with_a_quote_is_bound_not_escaped(con):
    o = out(con)
    assert "B's" in o.headers
    assert cell(o, "South", "B's") == "1"


def test_the_counts_add_back_to_the_scope(con):
    o = out(con)
    assert cell(o, TOTAL, TOTAL) == "9"
    body = [r for r in o.rows if r[0] != TOTAL]
    assert sum(int(r[-1]) for r in body) == 9


def test_pivot_would_have_lost_the_null_column(con):
    where = ("ts >= DATE '2024-01-01' AND ts < DATE '2024-12-31' + INTERVAL 1 DAY")
    pivoted = con.execute(
        f"SELECT * FROM (PIVOT (SELECT region, channel, amount FROM sales "
        f"WHERE {where}) ON channel USING sum(amount) GROUP BY region)"
    ).fetchall()
    cells = sum(c for row in pivoted for c in row[1:] if c is not None)
    scoped = con.execute(f"SELECT sum(amount) FROM sales WHERE {where}").fetchall()[0][0]
    assert float(cells) == 112.00
    assert float(scoped) == 117.00


def test_a_count_cell_with_no_rows_is_zero(con):
    assert cell(out(con), "North", "") == "0"


def test_a_sum_cell_with_no_value_is_blank_and_says_so(con):
    o = out(con, measure="amount")
    assert cell(o, "North", "") is None
    assert cell(o, "South", "Retail") is None
    assert any("A blank cell is not zero" in s for s in o.summary)


def test_sum_margins_are_totals_of_the_rows(con):
    o = out(con, measure="amount")
    assert cell(o, "North", TOTAL) == "75.00"
    assert cell(o, TOTAL, "Online") == "90.00"
    assert cell(o, TOTAL, TOTAL) == "117.00"


def test_an_average_margin_is_over_the_rows_not_the_cells(con):
    o = out(con, measure="amount", measures=[FakeMeasure("amount", agg="mean")])
    assert cell(o, "North", TOTAL) == "18.75"
    assert cell(o, TOTAL, "Retail") == "5.5"
    assert cell(o, TOTAL, TOTAL) == "14.625"
    assert any("not a combination of the cells" in s for s in o.summary)


def test_the_scope_decides_which_rows_are_crossed(con):
    o = out(con)
    assert not any("East" in str(c) for row in o.rows for c in row)
    assert o.summary[0] == "9 of 10 row(s) analysed. 1 outside 2024-01-01 to 2024-12-31."


def test_the_null_group_counts_toward_the_column_cap(con):
    con.execute(
        f"CREATE TABLE wide AS SELECT i AS id, 'r' AS region, "
        f"CASE WHEN i = 0 THEN NULL ELSE 'c' || i END AS channel "
        f"FROM range({MAX_COLUMN_GROUPS}) t(i)"
    )
    g = FakeGate(FakeContract(dataset_name="wide", date_column=None, primary_key=[]))
    o = run(con, g, scope_for(con, g), "cross_tab", rows="region", columns="channel")
    assert len(o.headers) == MAX_COLUMN_GROUPS + 2

    con.execute("INSERT INTO wide VALUES (999, 'r', 'c999')")
    with pytest.raises(ValueError) as exc:
        run(con, g, scope_for(con, g), "cross_tab", rows="region", columns="channel")
    assert "NULL counted as a group" in str(exc.value)


def test_too_many_row_groups_are_refused_with_both_counts(con):
    con.execute(
        f"CREATE TABLE tall AS SELECT i AS id, 'r' || i AS region, "
        f"'c' || (i % 2) AS channel FROM range({MAX_ROW_GROUPS} + 1) t(i)"
    )
    g = FakeGate(FakeContract(dataset_name="tall", date_column=None, primary_key=[]))
    with pytest.raises(ValueError) as exc:
        run(con, g, scope_for(con, g), "cross_tab", rows="region", columns="channel")
    assert f"{MAX_ROW_GROUPS + 1} row group(s)" in str(exc.value)
    assert "top_n" in str(exc.value)


def test_a_dimension_against_itself_is_refused(con):
    with pytest.raises(ValueError) as exc:
        out(con, columns="region")
    assert "against itself" in str(exc.value)
    assert "frequency" in str(exc.value)


def test_an_undeclared_dimension_is_refused_with_the_list(con):
    with pytest.raises(ValueError) as exc:
        out(con, columns="id")
    assert "Declared:" in str(exc.value)


def test_an_excluded_dimension_is_refused(con):
    with pytest.raises(ValueError) as exc:
        out(con, excluded_columns=["channel"])
    assert "never to read it" in str(exc.value)


def test_a_non_additive_measure_is_refused(con):
    with pytest.raises(ValueError) as exc:
        out(con, measure="amount", measures=[FakeMeasure("amount", agg="none")])
    assert "non-additive" in str(exc.value)
    assert "counts rows instead" in str(exc.value)


def test_a_measure_with_no_aggregate_is_refused(con):
    with pytest.raises(ValueError) as exc:
        out(con, measure="amount", measures=[FakeMeasure("amount", agg=None)])
    assert "no declared aggregate" in str(exc.value)


def test_an_undeclared_measure_is_refused(con):
    with pytest.raises(ValueError) as exc:
        out(con, measure="id")
    assert "not a declared measure" in str(exc.value)


def test_labels_that_collide_are_refused(con):
    con.execute(
        "CREATE TABLE clash AS SELECT * FROM (VALUES "
        "(1, 'r', '(null)'), (2, 'r', NULL)) v(id, region, channel)"
    )
    g = FakeGate(FakeContract(dataset_name="clash", date_column=None, primary_key=[]))
    with pytest.raises(ValueError) as exc:
        run(con, g, scope_for(con, g), "cross_tab", rows="region", columns="channel")
    assert "cannot label" in str(exc.value)


def test_a_scope_the_cells_disagree_with_raises_lost_rows(con):
    lying = Scope(dataset_name="sales", where="id <= 5", rows=10, excluded=0,
                  outside_window=0, no_date=0, analysed=10)
    with pytest.raises(LostRows) as exc:
        run(con, gate(), lying, "cross_tab", rows="region", columns="channel")
    assert "lost rows" in str(exc.value)


def test_an_empty_scope_crosses_nothing_and_says_why(con):
    o = out(con, analysis_window=FakeWindow(date(2030, 1, 1), date(2030, 12, 31)))
    assert o.rows == []
    assert o.headers == ["region", TOTAL]
    assert any("nothing to cross" in s for s in o.summary)


def test_the_method_note_and_caveats_travel(con):
    o = out(con)
    assert o.summary[0].startswith("9 of 10 row(s) analysed")
    assert "the loader reported 2 bad line(s)" in o.summary


def test_parameters_it_does_not_take_are_refused(con):
    g = gate()
    with pytest.raises(TypeError) as exc:
        run(con, g, scope_for(con, g), "cross_tab", rows="region",
            columns="channel", limit=5)
    assert "takes rows, columns and measure" in str(exc.value)
