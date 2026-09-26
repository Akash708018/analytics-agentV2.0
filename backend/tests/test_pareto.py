"""How much of a total sits in how few groups.

Phase 8, Step 8c. Run from the repo root:

    uv run pytest tests/test_pareto.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import pareto as _  # noqa: E402,F401
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
    date_column: str | None = None
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


# Built so the textbook answer is exact: north and south are 80% of 1,000.00
# on the nose, east and west tie at 80.00 so a 90% threshold crosses inside a
# tie, and the NULL group is neither first nor last.
SKEW = """SELECT * FROM (VALUES
 (1,'north',  600.00::DECIMAL(18,2)),
 (2,'south',  200.00::DECIMAL(18,2)),
 (3,'east',    80.00::DECIMAL(18,2)),
 (4,'west',    80.00::DECIMAL(18,2)),
 (5,NULL,      30.00::DECIMAL(18,2)),
 (6,'centre',  10.00::DECIMAL(18,2))
) v(id, region, amount)"""


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute(f"CREATE TABLE sales AS {SKEW}")
    yield c
    c.close()


def gate(**kw) -> FakeGate:
    caveats = kw.pop("caveats", [])
    return FakeGate(contract=FakeContract(**kw), caveats=caveats)


def out_for(con, g, kind, **params):
    params.setdefault("dimension", "region")
    params.setdefault("measure", "amount")
    return run(con, g, scope_for(con, g), kind, **params)


def test_both_are_registered_in_tier_two():
    tiers = {name: tier for name, tier, _ in catalogue()}
    assert tiers["pareto"] == 2
    assert tiers["concentration"] == 2


def test_groups_run_biggest_first_with_null_in_its_place(con):
    out = out_for(con, gate(), "pareto")
    assert [r[1] for r in out.rows] == [
        "north", "south", "east", "west", "(null)", "centre"]


def test_the_running_share_ends_at_one_hundred_percent(con):
    """Accumulated from the totals: adding the rounded shares would not have
    to land on 100.0%."""
    out = out_for(con, gate(), "pareto")
    assert out.rows[-1][4] == "100.0%"
    assert any("rather than from the rounded shares" in s for s in out.summary)


def test_two_of_six_groups_carry_eighty_percent(con):
    out = out_for(con, gate(), "pareto")
    assert out.rows[1][4] == "80.0%"
    assert any("2 of 6 group(s) reach 80%" in s for s in out.summary)


def test_a_threshold_crossing_inside_a_tie_says_so(con):
    out = out_for(con, gate(), "pareto", threshold=90)
    assert any("4 of 6 group(s) reach 90%" in s for s in out.summary)
    assert any("share the total at the 90% crossing" in s for s in out.summary)


def test_a_threshold_outside_the_range_is_refused(con):
    for bad in (0, -1, 101):
        with pytest.raises(ValueError) as e:
            out_for(con, gate(), "pareto", threshold=bad)
        assert "percentage" in str(e.value)


def test_a_zero_total_is_refused_rather_than_charted(con):
    """top_n drops its share column and keeps its ranking. A Pareto curve
    without shares is not a reduced answer, it is no answer."""
    con.execute("UPDATE sales SET amount = 0")
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), "pareto")
    assert "inf rather than an error" in str(e.value)


def test_a_negative_group_total_is_refused(con):
    con.execute("UPDATE sales SET amount = -900.00 WHERE region = 'centre'")
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), "concentration")
    assert "not a proportion" in str(e.value)


def test_a_non_additive_aggregate_is_refused(con):
    for agg, wanted in (("none", "non-additive"), ("mean", "does not add up")):
        with pytest.raises(ValueError) as e:
            out_for(con, gate(measures=[FakeMeasure("amount", agg=agg)]), "pareto")
        assert wanted in str(e.value)


def test_a_measure_with_no_aggregate_is_refused(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(measures=[FakeMeasure("amount", agg=None)]), "pareto")
    assert "no declared aggregate" in str(e.value)


def test_an_undeclared_dimension_is_refused_with_the_list(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(), "pareto", dimension="id")
    assert "region" in str(e.value)


def test_an_excluded_dimension_is_refused(con):
    with pytest.raises(ValueError) as e:
        out_for(con, gate(excluded_columns=["region"]), "concentration")
    assert "never to read it" in str(e.value)


def test_more_groups_than_a_table_holds_are_still_ranked(con):
    """This test pinned a refusal above 49 groups until Cleanup Step 14 (RF-O8): pareto's answer
    is a count to a threshold, and the retail run's 500 SKUs were refused for a table it does not
    need. The ranking goes to the result file, which pages."""
    con.execute("CREATE TABLE wide AS SELECT range AS id, "
                "('g' || range) AS region, range::DECIMAL(18,2) + 1 AS amount "
                "FROM range(60)")
    out = out_for(con, gate(dataset_name="wide"), "pareto")
    assert len(out.rows) == 60
    assert "across all 60 group(s)" in " ".join(out.summary)


def test_an_empty_scope_charts_nothing_and_says_why(con):
    con.execute("DELETE FROM sales")
    out = out_for(con, gate(), "pareto")
    assert out.rows == []
    assert any("nothing to rank" in s for s in out.summary)


def test_parameters_they_do_not_take_are_refused(con):
    for kind in ("pareto", "concentration"):
        with pytest.raises(TypeError):
            out_for(con, gate(), kind, n=5)


def test_concentration_reports_the_cuts_below_the_group_count(con):
    out = out_for(con, gate(), "concentration")
    assert [r[0] for r in out.rows] == ["top 1", "top 3", "top 5", "all 6"]
    assert [r[1] for r in out.rows] == ["60.0%", "88.0%", "99.0%", "100.0%"]


def test_concentration_on_few_groups_reports_only_what_exists(con):
    """Three groups, not two: NULL NOT IN (...) is NULL, never true, so the
    delete leaves the NULL region behind. The cuts above the group count are
    the ones that do not appear."""
    con.execute("DELETE FROM sales WHERE region NOT IN ('north', 'south')")
    assert con.execute("SELECT count(DISTINCT region) FROM sales "
                       "WHERE region IS NULL").fetchall()[0][0] == 0
    out = out_for(con, gate(), "concentration")
    assert [r[0] for r in out.rows] == ["top 1", "all 3"]


def test_the_index_is_a_number_and_not_a_verdict(con):
    out = out_for(con, gate(), "concentration")
    text = " ".join(out.summary)
    assert "HHI 4,138" in text
    assert "1,667" in text, "the floor for six equal groups"
    for verdict in ("highly concentrated", "unconcentrated", "moderately"):
        assert verdict not in text.lower()


def test_the_effective_number_of_groups_is_reported(con):
    out = out_for(con, gate(), "concentration")
    assert any("Effective number of groups 2.4, against 6" in s
               for s in out.summary)


def test_the_method_note_and_caveats_travel(con):
    out = out_for(con, gate(caveats=["gained 30 rows since v1"]), "pareto")
    assert out.summary[0].startswith("6 of 6 row(s) analysed")
    assert "gained 30 rows since v1" in out.summary
