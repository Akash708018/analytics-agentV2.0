"""The shape of one declared measure: bins, shares, quantiles.

Phase 8, Step 7b.1. Run from the repo root:

    uv run pytest tests/test_distribution.py -q
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import distribution as _dist  # noqa: E402,F401
from backend.engine.analysis import summary_stats as _summ  # noqa: E402,F401
from backend.engine.analysis.base import scope_for  # noqa: E402
from backend.engine.analysis.distribution import MAX_BINS  # noqa: E402
from backend.engine.analysis.registry import run  # noqa: E402


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
    dataset_name: str = "dist"
    date_column: str | None = "ts"
    analysis_window: FakeWindow | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [FakeMeasure("amount")])
    dimensions: list = field(default_factory=list)
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


# x: 0.0 .. 1.0 in tenths, then inf, nan, NULL. rating: 1..5. amount: 1.25 ..
# 17.50. flat: 7 everywhere. Every expected value below came from plain SQL
# against this table, not from an implementation of distribution.
FIXTURE = """
CREATE TABLE dist AS
SELECT i AS id,
       DATE '2024-01-01' + i::INTEGER AS ts,
       CASE WHEN i <= 11 THEN ((i - 1) / 10.0)::DOUBLE
            WHEN i = 12 THEN 'inf'::DOUBLE
            WHEN i = 13 THEN 'nan'::DOUBLE
            ELSE NULL END AS x,
       ((i % 5) + 1)::INTEGER AS rating,
       (i * 1.25)::DECIMAL(18,2) AS amount,
       7::INTEGER AS flat
FROM range(1, 15) t(i)
"""

MEASURES = [
    FakeMeasure("x", agg="mean", unit=None),
    FakeMeasure("rating", agg="mean", unit=None),
    FakeMeasure("amount"),
    FakeMeasure("flat", unit=None),
    FakeMeasure("ts", agg="min", unit=None),
]


@pytest.fixture
def con():
    with duckdb.connect(":memory:") as connection:
        connection.execute(FIXTURE)
        yield connection


def gate(**kwargs):
    kwargs.setdefault("measures", [FakeMeasure(m.name, m.agg, m.definition, m.unit)
                                   for m in MEASURES])
    caveats = kwargs.pop("caveats", ["the loader reported 2 bad line(s)"])
    return FakeGate(FakeContract(**kwargs), caveats=caveats)


def out(con, measure, bins=10, **kwargs):
    g = gate(**kwargs)
    return run(con, g, scope_for(con, g), "distribution", measure=measure, bins=bins)


def column(output, index):
    return [row[index] for row in output.rows]


def test_a_value_on_an_edge_lands_in_the_bin_it_starts(con):
    o = out(con, "x")
    assert column(o, 2) == ["1"] * 9 + ["2"]
    assert column(o, 0) == ["0", "0.1", "0.2", "0.3", "0.4",
                            "0.5", "0.6", "0.7", "0.8", "0.9"]


def test_the_maximum_is_inside_the_last_bin(con):
    last = out(con, "x").rows[-1]
    assert last[1] == "1"
    assert last[2] == "2"


def test_the_bins_add_up_to_the_finite_values(con):
    assert sum(int(c) for c in column(out(con, "x"), 2)) == 11


def test_shares_and_the_running_share(con):
    o = out(con, "x")
    assert column(o, 3)[0] == "9.1%"
    assert column(o, 3)[-1] == "18.2%"
    assert column(o, 4)[4] == "45.5%"
    assert column(o, 4)[-1] == "100.0%"


def test_the_bin_rule_is_stated(con):
    text = " ".join(out(con, "x").summary)
    assert "10 bin(s) of x between 0 and 1" in text
    assert "includes its lower edge" in text


def test_the_quantiles_are_stated(con):
    assert (
        "Quantiles of x over 11 finite value(s): min 0, p5 0.05, p25 0.25, "
        "median 0.5, p75 0.75, p95 0.95, max 1."
    ) in out(con, "x").summary


def test_non_finite_values_are_counted_and_not_binned(con):
    assert any("2 value(s) of x are inf or nan" in s for s in out(con, "x").summary)


def test_nulls_are_counted_and_not_binned(con):
    text = " ".join(out(con, "x").summary)
    assert "1 analysed row(s) have no x" in text
    assert "not of the 14 analysed row(s)" in text


def test_the_median_agrees_with_summary_stats(con):
    g = gate(measures=[FakeMeasure("amount")])
    scope = scope_for(con, g)
    text = " ".join(run(con, g, scope, "distribution", measure="amount").summary)
    assert "median 9.375" in text
    assert "9.37," not in text
    stats = run(con, g, scope, "summary_stats")
    row = dict(zip(stats.headers, stats.rows[0]))
    assert row["median"] == "9.375"


def test_an_integer_measure_gets_whole_number_bins(con):
    o = out(con, "rating")
    assert len(o.rows) == 5
    assert column(o, 0) == ["1", "2", "3", "4", "5"]
    assert column(o, 1) == ["2", "3", "4", "5", "5"]
    assert column(o, 2) == ["2", "3", "3", "3", "3"]
    assert any("5 bin(s), not the 10 requested" in s for s in o.summary)


def test_integer_bins_can_be_wider_than_one(con):
    o = out(con, "rating", bins=2)
    assert column(o, 0) == ["1", "4"]
    assert column(o, 1) == ["4", "5"]
    assert column(o, 2) == ["8", "6"]


def test_a_measure_that_never_varies_is_one_bin_not_an_error(con):
    o = out(con, "flat")
    assert o.rows == [["7", "7", "14", "100.0%", "100.0%"]]
    assert "Every finite value of flat is 7, so there is one bin." in o.summary


def test_an_unsigned_128_bit_measure_is_binned_at_its_own_width(con):
    con.execute("CREATE TABLE wide AS SELECT * FROM (VALUES "
                "(0::UHUGEINT), (7::UHUGEINT), "
                "(340282366920938463463374607431768211455::UHUGEINT)) v(big)")
    g = FakeGate(FakeContract(dataset_name="wide", date_column=None,
                              primary_key=[], measures=[FakeMeasure("big")]))
    o = run(con, g, scope_for(con, g), "distribution", measure="big", bins=2)
    assert column(o, 2) == ["2", "1"]
    assert column(o, 1)[-1] == "340,282,366,920,938,463,463,374,607,431,768,211,455"


def test_the_scope_decides_which_rows_are_binned(con):
    o = out(con, "x", analysis_window=FakeWindow(date(2024, 1, 2), date(2024, 1, 6)))
    assert sum(int(c) for c in column(o, 2)) == 5
    assert o.summary[0].startswith("5 of 14 row(s) analysed")


def test_a_measure_with_no_declared_aggregate_still_has_a_distribution(con):
    o = out(con, "x", measures=[FakeMeasure("x", agg=None, unit=None)])
    assert len(o.rows) == 10
    assert any("totals nothing" in s for s in o.summary)


def test_a_non_additive_measure_still_has_a_distribution(con):
    o = out(con, "x", measures=[FakeMeasure("x", agg="none", unit=None)])
    assert len(o.rows) == 10
    assert any("non-additive" in s and "totals nothing" in s for s in o.summary)


def test_a_date_measure_is_refused_with_somewhere_to_go(con):
    with pytest.raises(ValueError) as exc:
        out(con, "ts")
    assert "has type DATE" in str(exc.value)
    assert "not numeric" in str(exc.value)
    assert "profile_column" in str(exc.value)


def test_an_undeclared_measure_is_refused_with_the_list(con):
    with pytest.raises(ValueError) as exc:
        out(con, "id")
    assert "Declared:" in str(exc.value)
    assert "x" in str(exc.value)


def test_an_excluded_measure_is_refused(con):
    with pytest.raises(ValueError) as exc:
        out(con, "x", excluded_columns=["x"])
    assert "never to read it" in str(exc.value)


def test_bins_outside_the_range_are_refused(con):
    for bad in (1, MAX_BINS + 1, True):
        with pytest.raises(ValueError):
            out(con, "x", bins=bad)


def test_an_empty_scope_bins_nothing_and_says_why(con):
    o = out(con, "x", analysis_window=FakeWindow(date(2030, 1, 1), date(2030, 12, 31)))
    assert o.rows == []
    assert any("nothing to bin" in s for s in o.summary)


def test_the_method_note_and_caveats_travel(con):
    o = out(con, "x")
    assert o.summary[0] == "14 of 14 row(s) analysed."
    assert "the loader reported 2 bad line(s)" in o.summary


def test_parameters_it_does_not_take_are_refused(con):
    g = gate()
    with pytest.raises(TypeError) as exc:
        run(con, g, scope_for(con, g), "distribution", measure="x", limit=5)
    assert "takes measure and bins" in str(exc.value)
