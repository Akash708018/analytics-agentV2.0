"""Dimensions ranked by what they explain beyond their own shape.

Phase 9, Step 5c. Run from the repo root:

    uv run pytest tests/test_driver_analysis.py -q

The fixture declares `id` as a dimension deliberately. It accounts for every
last bit of the variation in `spend` and is worth nothing, which is the whole
reason the chance baseline exists: by raw share it ranks first, by excess it
ranks last.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import driver_analysis as _  # noqa: E402,F401
from backend.engine.analysis.base import ParamsInvalid, scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "what a customer spent"
    unit: str | None = None


@dataclass
class FakeContract:
    dataset_name: str = "sales"
    date_column: str | None = "ts"
    analysis_window: object | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [
        FakeMeasure("spend"),
        FakeMeasure("flat", definition="a measure that never varies"),
    ])
    dimensions: list = field(default_factory=lambda: ["region", "size", "id"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# region separates spend almost completely, size barely at all, and id gives
# every row its own group. One region is NULL, which is a group. One row has no
# spend and is in no group.
DRIVEN = """SELECT * FROM (VALUES
  (1, 'north', 'small', 10.0, 7.0), (2, 'north', 'small', 12.0, 7.0),
  (3, 'north', 'large', 11.0, 7.0), (4, 'south', 'small', 50.0, 7.0),
  (5, 'south', 'large', 52.0, 7.0), (6, 'south', 'large', 48.0, 7.0),
  (7, 'west',  'small', 30.0, 7.0), (8, 'west',  'large', 31.0, 7.0),
  (9, NULL,    'large', 29.0, 7.0), (10, 'west', 'small', NULL, 7.0)
) v(id, region, size, spend, flat)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE sales AS {DRIVEN}")
    yield c
    c.close()


def driven(con, **params):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "spend")
    return run(con, gate, scope, "driver_analysis", **params)


def at(out, dimension):
    return next(row for row in out.rows if row[0] == dimension)


def test_the_primary_key_explains_everything_and_adds_nothing(con):
    out = driven(con)
    assert at(out, "id")[3] == "1.000", (
        "Nine groups over nine rows explains all of it. That is arithmetic."
    )
    assert at(out, "id")[5] == "+0.000", (
        "Explaining exactly what its own shape explains. Not last in the "
        "table -- size is below chance and ranks under it -- but worth nothing."
    )
    assert out.rows[0][0] != "id", [r[0] for r in out.rows]


def test_the_ranking_is_on_excess_and_not_on_raw_share(con):
    assert [r[0] for r in driven(con).rows] == ["region", "id", "size"], (
        "By raw share this is id, region, size. By excess it is not."
    )


def test_the_chance_share_is_reported_beside_the_raw_share(con):
    region = at(driven(con), "region")
    assert (region[3], region[4], region[5]) == ("0.995", "0.375", "+0.620")


def test_a_dimension_below_chance_is_reported_negative(con):
    size = at(driven(con), "size")
    assert size[5].startswith("-"), size
    assert "less than nothing" in " ".join(driven(con).summary)


def test_a_null_is_a_group(con):
    assert at(driven(con), "region")[1] == "4", (
        "north, south, west and the NULL. Three groups would mean the NULL "
        "rows had been dropped."
    )


def test_a_row_without_the_measure_is_in_no_group(con):
    out = driven(con)
    assert at(out, "region")[2] == "9"
    assert "1 do not and are in no group below" in " ".join(out.summary)


def test_one_row_groups_are_counted(con):
    out = driven(con)
    assert at(out, "id")[6] == "9"
    assert "holding one row" in " ".join(out.summary)


def test_a_dimension_as_granular_as_the_rows_is_named_as_arithmetic(con):
    text = " ".join(driven(con).summary)
    assert "arithmetic rather than a finding" in text


def test_nothing_is_said_to_drive_anything(con):
    text = " ".join(driven(con).summary)
    assert "Accounts for, not drives" in text
    assert "may be another name for it" in text


def test_a_measure_that_does_not_vary_has_no_shares_at_all(con):
    out = driven(con, measure="flat")
    assert out.rows == []
    assert "total sum of squares is exactly 0.0" in " ".join(out.summary)
    assert "not a failure" in " ".join(out.summary)


def test_a_measure_nobody_declared_is_refused(con):
    with pytest.raises(Exception):
        driven(con, measure="profit")


def test_a_contract_with_no_dimensions_is_refused(con):
    contract = FakeContract()
    contract.dimensions = []
    gate = FakeGate(contract)
    scope = scope_for(con, gate)
    with pytest.raises(ParamsInvalid) as excinfo:
        run(con, gate, scope, "driver_analysis", measure="spend")
    assert "declares no dimensions" in str(excinfo.value)


def test_the_method_note_is_the_first_summary_line(con):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "driver_analysis", measure="spend")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "driver_analysis")
    assert entry[1] == 4 and "chance" in entry[2]
