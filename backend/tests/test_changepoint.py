"""A level shift, and the gap that makes one unanswerable.

Phase 9, Step 6b. Run from the repo root:

    uv run pytest tests/test_changepoint.py -q

Step 4c ruled that a gap is a caveat and never fatal, and named this analysis
as the one that may need otherwise. It does, and the fixtures here are built to
show both halves of that: a clean break with no gaps, and the same break with
the two months around it holding nothing at all.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.analysis import changepoint as _  # noqa: E402,F401
from backend.engine.analysis.base import ParamsInvalid, scope_for  # noqa: E402
from backend.engine.analysis.registry import catalogue, run  # noqa: E402


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "a reading"
    unit: str | None = None


@dataclass
class FakeContract:
    dataset_name: str = "meter"
    date_column: str | None = "ts"
    analysis_window: object | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [
        FakeMeasure("level"),
        FakeMeasure("unit_price", agg="none", definition="price of one item"),
    ])
    dimensions: list = field(default_factory=lambda: ["site"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


def months(values: dict[int, float]) -> str:
    rows = ", ".join(
        f"(TIMESTAMP '2017-{m:02d}-05', {v}, 1.0)" for m, v in values.items())
    return f"SELECT * FROM (VALUES {rows}) v(ts, level, unit_price)"


# Ten for six months, fifty for six. No noise anywhere, so the two segments
# have no variation of their own.
CLEAN = {m: (10.0 if m <= 6 else 50.0) for m in range(1, 13)}

# The same break with a month of noise on every reading, so the segments vary.
NOISY = {m: (10.0 if m <= 6 else 50.0) + (m % 3) for m in range(1, 13)}

# No break at all: three values cycling for a year.
FLATISH = {m: 20.0 + (m % 3) * 2 for m in range(1, 13)}

# The clean break with June and July missing, so the step straddles a gap.
STRADDLED = {m: v for m, v in CLEAN.items() if m not in (6, 7)}

# Three months, a two-month hole, three months: every admissible split has an
# absent month beside it.
HOLED = {1: 10.0, 2: 10.0, 3: 10.0, 6: 50.0, 7: 50.0, 8: 50.0}


def con_for(values: dict[int, float]):
    c = duckdb.connect(":memory:")
    c.execute(f"CREATE TABLE meter AS {months(values)}")
    return c


def broken(con, **params):
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    params.setdefault("measure", "level")
    return run(con, gate, scope, "changepoint", **params)


def test_the_break_is_found_where_it_is():
    out = broken(con_for(CLEAN))
    assert out.rows[0] == ["2017-06", "6", "6", "10", "50", "+40"]


def test_every_admissible_split_is_reported_not_only_the_best():
    out = broken(con_for(CLEAN))
    assert len(out.rows) == 7, (
        f"{len(out.rows)}. Twelve months, three needed on each side: seven "
        "splits. Reporting one of them as the changepoint hides the other six."
    )
    assert [r[0] for r in out.rows][:3] == ["2017-06", "2017-05", "2017-07"]


def test_a_break_with_no_noise_says_the_segments_do_not_vary():
    text = " ".join(broken(con_for(CLEAN)).summary)
    assert "Neither side varies within itself at all" in text


def test_a_break_is_measured_against_variation_inside_the_segments():
    """Not against the runner-up: adjacent splits share all but one period."""
    text = " ".join(broken(con_for(NOISY)).summary)
    assert "spans 44.7 times the variation inside the two segments" in text
    assert "not merely the best available" in text


def test_a_series_with_no_break_says_its_best_split_is_arithmetic():
    text = " ".join(broken(con_for(FLATISH)).summary)
    assert "spans only 0.4 times the variation" in text
    assert "best of a set that all look alike rather than a break" in text


def test_a_split_beside_an_absent_period_is_excluded_not_caveated():
    out = broken(con_for(STRADDLED))
    text = " ".join(out.summary)
    assert "3 split(s) are excluded rather than caveated" in text
    assert "2017-05, 2017-06, 2017-07" in text
    assert [r[0] for r in out.rows] == [
        "2017-04", "2017-08", "2017-09", "2017-03"]


def test_the_exclusion_names_the_ruling_it_departs_from():
    text = " ".join(broken(con_for(STRADDLED)).summary)
    assert "Step 4c ruled that a gap is a caveat and never fatal" in text
    assert "it is the reason there is no answer at that split" in text


def test_a_gap_that_blocks_every_split_reports_no_changepoint():
    out = broken(con_for(HOLED))
    assert out.rows == []
    assert "No admissible split remains" in " ".join(out.summary)


def test_the_absent_periods_are_named_with_their_longest_run():
    text = " ".join(broken(con_for(STRADDLED)).summary)
    assert "2 month(s) hold no rows -- 2017-06, 2017-07" in text
    assert "longest unbroken run of them is 2" in text


def test_no_significance_is_claimed():
    text = " ".join(broken(con_for(CLEAN)).summary)
    assert "No significance is claimed" in text
    assert "no p-value" in text


def test_a_measure_with_no_value_per_period_is_refused():
    with pytest.raises(ValueError) as excinfo:
        broken(con_for(CLEAN), measure="unit_price")
    assert "no level for a level shift to be in" in str(excinfo.value)


def test_a_grain_nobody_offers_is_refused():
    with pytest.raises(ParamsInvalid):
        broken(con_for(CLEAN), grain="fortnight")


def test_the_method_note_is_the_first_summary_line():
    con = con_for(CLEAN)
    gate = FakeGate(FakeContract())
    scope = scope_for(con, gate)
    out = run(con, gate, scope, "changepoint", measure="level")
    assert out.summary[0] == scope.method_note()


def test_the_catalogue_reports_name_tier_and_a_sentence():
    entry = next(c for c in catalogue() if c[0] == "changepoint")
    assert entry[1] == 5 and "excluded" in entry[2]
