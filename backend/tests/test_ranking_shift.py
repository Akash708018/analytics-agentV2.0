"""How a ranking changed between two periods.

Phase 8, Step 8d. Run from the repo root:

    uv run pytest tests/test_ranking_shift.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import ranking_shift as _  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "what was charged"
    unit: str | None = "GBP"


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
    dimensions: list = field(default_factory=lambda: ["region"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# north leads January and falls to third in March; south rises; west arrives;
# east leaves. Row 7 sits in February, in neither period and worth more than
# any other row -- if it leaked into either one, north would rank first there.
SHIFT = """SELECT * FROM (VALUES
 (1,'north', DATE '2024-01-10', 500.00::DECIMAL(18,2)),
 (2,'south', DATE '2024-01-20', 300.00::DECIMAL(18,2)),
 (3,'east',  DATE '2024-01-25', 100.00::DECIMAL(18,2)),
 (4,'north', DATE '2024-03-10', 100.00::DECIMAL(18,2)),
 (5,'south', DATE '2024-03-15', 400.00::DECIMAL(18,2)),
 (6,'west',  DATE '2024-03-20', 250.00::DECIMAL(18,2)),
 (7,'north', DATE '2024-02-14', 999.00::DECIMAL(18,2))
) v(id, region, ts, amount)"""

JAN = ("2024-01-01", "2024-01-31")
MAR = ("2024-03-01", "2024-03-31")


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE sales AS {SHIFT}")
    yield c
    c.close()


def gate(**kw) -> FakeGate:
    caveats = kw.pop("caveats", [])
    return FakeGate(contract=FakeContract(**kw), caveats=caveats)


def out_for(con, g, **params):
    params.setdefault("dimension", "region")
    params.setdefault("measure", "amount")
    params.setdefault("before_start", JAN[0])
    params.setdefault("before_end", JAN[1])
    params.setdefault("after_start", MAR[0])
    params.setdefault("after_end", MAR[1])
    return run(con, g, scope_for(con, g), "ranking_shift", **params)


def cell(out, group, column):
    row = next(r for r in out.rows if r[0] == group)
    return row[out.headers.index(column)]


def test_it_is_registered_in_tier_two():
    entry = next(c for c in catalogue() if c[0] == "ranking_shift")
    assert entry[1] == 2


def test_the_after_ranking_orders_the_table(con):
    out = out_for(con, gate())
    assert [r[0] for r in out.rows] == ["south", "west", "north", "east"]


def test_a_group_that_rose_and_one_that_fell(con):
    out = out_for(con, gate())
    assert cell(out, "south", "change") == "+1"
    assert cell(out, "north", "change") == "-2"


def test_a_group_in_one_period_only_is_named_not_scored(con):
    out = out_for(con, gate())
    assert cell(out, "west", "change") == "new"
    assert cell(out, "east", "change") == "gone"
    assert cell(out, "west", "rank before") == ""
    assert cell(out, "east", "rank after") == ""
    assert any("no rank to subtract from" in s for s in out.summary)


def test_rows_in_neither_period_are_counted_and_excluded(con):
    """Row 7 is worth 999.00 in February. If it reached either period north
    would rank first there."""
    out = out_for(con, gate())
    assert cell(out, "north", "amount before") == "500.00"
    assert cell(out, "north", "amount after") == "100.00"
    assert any("1 analysed row(s) fall in neither period" in s
               for s in out.summary)


def test_the_period_bounds_are_inclusive_at_both_ends(con):
    """P7-D2: written `<= end` the bound casts to midnight and the last day is
    lost. Row 3 is 2024-01-25 and row 6 is 2024-03-20."""
    out = out_for(con, gate(), before_end="2024-01-25", after_start="2024-03-20")
    assert cell(out, "east", "amount before") == "100.00"
    assert cell(out, "west", "amount after") == "250.00"


def test_a_contract_with_no_date_column_is_refused(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(date_column=None))
    assert "no date_column" in str(e.value)
    assert "group_compare" in str(e.value)


def test_a_date_that_is_not_a_date_is_refused_by_name(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), before_start="last January")
    assert "before period" in str(e.value)
    assert "YYYY-MM-DD" in str(e.value)


def test_a_period_that_ends_before_it_starts_is_refused(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), after_start="2024-03-31", after_end="2024-03-01")
    assert "after period ends" in str(e.value)


def test_the_scope_still_decides_which_rows_a_period_can_see(con):
    """The period narrows within the scope; it does not replace it."""
    g = gate(analysis_window=FakeWindow(date(2024, 2, 1), date(2024, 12, 31)))
    out = out_for(con, g)
    assert not any(r[0] == "east" for r in out.rows)
    assert out.summary[0].startswith("4 of 7 row(s) analysed")


def test_a_non_additive_measure_is_refused(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(measures=[FakeMeasure("amount", agg="none")]))
    assert "non-additive" in str(e.value)


def test_a_measure_with_no_aggregate_is_refused(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(measures=[FakeMeasure("amount", agg=None)]))
    assert "no declared aggregate" in str(e.value)


def test_an_undeclared_dimension_is_refused_with_the_list(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), dimension="id")
    assert "region" in str(e.value)


def test_an_excluded_measure_is_refused(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(excluded_columns=["amount"]))
    assert "never to read it" in str(e.value)


def test_a_tie_makes_the_change_an_artifact_and_says_so(con):
    con.execute("UPDATE sales SET amount = 100.00 WHERE ts < DATE '2024-02-01'")
    out = out_for(con, gate())
    assert any("come from the tiebreak on the group name" in s
               for s in out.summary)


def test_an_empty_scope_ranks_nothing_and_says_why(con):
    g = gate(analysis_window=FakeWindow(date(2030, 1, 1), date(2030, 12, 31)))
    out = out_for(con, g)
    assert out.rows == []
    assert any("nothing to rank" in s for s in out.summary)


def test_parameters_it_does_not_take_are_refused(con):
    with pytest.raises(TypeError):
        out_for(con, gate(), n=5)


def test_the_method_note_and_caveats_travel(con):
    out = out_for(con, gate(caveats=["gained 30 rows since v1"]))
    assert out.summary[0].startswith("7 of 7 row(s) analysed")
    assert "gained 30 rows since v1" in out.summary
