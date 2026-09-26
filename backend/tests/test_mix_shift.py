"""Every group's average rises and the overall average falls by fifty.

Phase 9, Step 5d. Run from the repo root:

    uv run pytest tests/test_mix_shift.py -q

The fixture is the case the analysis exists for. Premium goes from 100 to 110
and budget from 10 to 12, so both improve; the overall mean falls from 82.0 to
31.6, because premium's share of the rows goes from 0.8 to 0.2. Rate totals
+8.4, mix totals -54.0, and a reader given either number alone reaches the
opposite conclusion from a reader given the other.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import mix_shift as _  # noqa: E402,F401
from backend.engine.analysis.base import ParamsInvalid, scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "none"
    definition: str = "the price of one item"
    unit: str | None = None


@dataclass
class FakeContract:
    dataset_name: str = "orders"
    date_column: str | None = "ts"
    analysis_window: object | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [FakeMeasure("price")])
    dimensions: list = field(default_factory=lambda: ["tier"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# January: 8 premium at 100, 2 budget at 10 -> mean 82.0
# February: 2 premium at 110, 8 budget at 12 -> mean 31.6
# March holds a tier that exists nowhere else, for the entrant case, and May
# extends the calendar past an April that holds nothing at all.
SHIFTED = """SELECT * FROM (VALUES
  (TIMESTAMP '2017-01-01','premium',100.0),(TIMESTAMP '2017-01-02','premium',100.0),
  (TIMESTAMP '2017-01-03','premium',100.0),(TIMESTAMP '2017-01-04','premium',100.0),
  (TIMESTAMP '2017-01-05','premium',100.0),(TIMESTAMP '2017-01-06','premium',100.0),
  (TIMESTAMP '2017-01-07','premium',100.0),(TIMESTAMP '2017-01-08','premium',100.0),
  (TIMESTAMP '2017-01-09','budget',10.0),(TIMESTAMP '2017-01-10','budget',10.0),
  (TIMESTAMP '2017-02-01','premium',110.0),(TIMESTAMP '2017-02-02','premium',110.0),
  (TIMESTAMP '2017-02-03','budget',12.0),(TIMESTAMP '2017-02-04','budget',12.0),
  (TIMESTAMP '2017-02-05','budget',12.0),(TIMESTAMP '2017-02-06','budget',12.0),
  (TIMESTAMP '2017-02-07','budget',12.0),(TIMESTAMP '2017-02-08','budget',12.0),
  (TIMESTAMP '2017-02-09','budget',12.0),(TIMESTAMP '2017-02-10','budget',12.0),
  (TIMESTAMP '2017-03-01','trial',5.0),
  (TIMESTAMP '2017-05-01','trial',5.0)
) v(ts, tier, price)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE orders AS {SHIFTED}")
    yield c
    c.close()


def shifted(con, **params):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "price")
    params.setdefault("dimension", "tier")
    params.setdefault("baseline", "2017-01")
    params.setdefault("period", "2017-02")
    return run(con, gate, scope, "mix_shift", **params)


def at(out, member):
    return next(row for row in out.rows if row[0] == member)


def test_every_group_improves_while_the_total_falls(con):
    out = shifted(con)
    premium, budget = at(out, "premium"), at(out, "budget")
    assert (premium[1], premium[2]) == ("100", "110")
    assert (budget[1], budget[2]) == ("10", "12")
    assert "-50.400" in " ".join(out.summary), (
        "Both group means rise and the overall mean falls by 50.4. That is "
        "the entire reason this analysis is separate from period_compare."
    )


def test_rate_and_mix_are_separated(con):
    text = " ".join(shifted(con).summary)
    assert "+8.400 is rate" in text
    assert "-54.000 is mix" in text


def test_the_interaction_is_reported_and_not_folded(con):
    out = shifted(con)
    assert at(out, "premium")[7] == "-6.000"
    assert at(out, "budget")[7] == "+1.200"
    assert "rather than folded into either" in " ".join(out.summary)


def test_the_two_weighting_bases_differ_by_exactly_the_interaction(con):
    assert "disagree by exactly that number" in " ".join(shifted(con).summary)


def test_the_contributions_sum_to_the_change(con):
    out = shifted(con)
    total = sum(float(r[8]) for r in out.rows)
    assert abs(total - -50.4) < 1e-9, total


def test_rate_pointing_against_the_total_is_called_out(con):
    text = " ".join(shifted(con).summary)
    assert "Rate and the total point opposite ways" in text
    assert "both would be reading true numbers" in text


def test_the_shares_are_reported_for_both_periods(con):
    premium = at(shifted(con), "premium")
    assert (premium[3], premium[4]) == ("0.800", "0.200")


def test_a_group_in_only_one_period_gets_no_rate_and_no_mix(con):
    out = shifted(con, period="2017-03")
    trial = at(out, "trial")
    assert (trial[5], trial[6], trial[7]) == ("", "", ""), trial
    assert trial[8] != ""
    assert "no rate and no mix" in " ".join(out.summary)


def test_a_measure_that_cannot_be_summed_is_welcome_here(con):
    """agg='none' is the classic mix-shift case, not a reason to refuse."""
    assert shifted(con).rows, "A unit price is exactly what this decomposes."


def test_a_period_holding_no_rows_has_no_average_to_split(con):
    out = shifted(con, period="2017-04")
    assert out.rows == []
    assert "did not average zero" in " ".join(out.summary)


def test_a_period_the_calendar_does_not_have_is_refused(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        shifted(con, period="2019-11")
    assert "is not a month" in str(excinfo.value)


def test_a_period_against_itself_is_refused(con):
    with pytest.raises(ParamsInvalid) as excinfo:
        shifted(con, period="2017-01")
    assert "zero by construction" in str(excinfo.value)


def test_a_column_nobody_declared_as_a_dimension_is_refused(con):
    with pytest.raises(ValueError) as excinfo:
        shifted(con, dimension="ts")
    assert "not a declared dimension" in str(excinfo.value)


def test_the_method_note_is_the_first_summary_line(con):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "mix_shift", measure="price", dimension="tier",
              baseline="2017-01", period="2017-02")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence(con):
    entry = next(c for c in catalogue() if c[0] == "mix_shift")
    assert entry[1] == 4 and "interaction" in entry[2]
