"""compute_analysis: the gate, the scope, the envelope, and four refusals.

Phase 8, Step 9a. Run from the repo root:

    uv run pytest tests/test_analysis_tools.py -q

These tests assert on STRINGS, because the strings are the whole interface --
the Phase 4 shape, for the Phase 4 reason. A reason code is asserted with
reason_of() rather than by matching prose, so improving a sentence does not
fail a test.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import state, workspace  # noqa: E402
from backend.engine.contract import ContractRefused  # noqa: E402
from backend.engine.contract.refusals import Refusal  # noqa: E402
from backend.engine.analysis import registry, tools  # noqa: E402
from backend.engine.analysis.base import LostRows  # noqa: E402
from backend.engine.analysis.registry import Output  # noqa: E402
from backend.engine.contract.refusals import Reason, reason_of  # noqa: E402
from backend.engine.util import results  # noqa: E402

WORKSPACE = "analysis_tools_test"

# What the patched require_contract will return, by dataset name. Reset by the
# fixture; a test that needs a different contract replaces its entry.
GATES: dict = {}


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
    grain: str = "one row = one sale"
    date_column: str | None = "ts"
    analysis_window: object = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [FakeMeasure("amount")])
    dimensions: list = field(default_factory=lambda: ["status"])
    primary_key: list = field(default_factory=lambda: ["id"])
    caveats: list = field(default_factory=lambda: ["v1 agreed at 5 rows"])


SALES = """SELECT * FROM (VALUES
 (1,'shipped',   TIMESTAMP '2024-06-01', 10.50::DECIMAL(18,2)),
 (2,'cancelled', TIMESTAMP '2024-06-02', 99.00::DECIMAL(18,2)),
 (3,NULL,        TIMESTAMP '2024-06-03', 20.25::DECIMAL(18,2)),
 (4,'shipped',   TIMESTAMP '2024-12-31', NULL),
 (5,'shipped',   TIMESTAMP '2025-01-01', 88.00::DECIMAL(18,2))
) v(id, status, ts, amount)"""


@dataclass
class FakeDrift:
    """The gate calls drift.caveat(); nothing here drifts."""

    text: str = ""

    def caveat(self) -> str:
        return self.text


def _gate(**kw) -> state.Gate:
    kw.setdefault("analysis_window", FakeWindow(date(2024, 1, 1), date(2024, 12, 31)))
    kw.setdefault("known_exclusions", [FakeExclusion(rule="status = 'cancelled'")])
    return state.Gate(contract=FakeContract(**kw), version=1, drift=FakeDrift())


@pytest.fixture()
def con(monkeypatch):
    """A loaded table and a gate that passes for 'sales' and nothing else.

    `require_contract` is patched in the tool module's namespace, the way
    test_validate_tools.py patches contract_store.current: these tests are
    about how analysis/tools.py USES a gate, and a fixture contract that has
    to satisfy the real store's validators is a different test.

    Step 10 is where this shortcut has to go -- P8-O10 is exactly "stand-in
    contracts looser than the real models".
    """
    workspace.reset(WORKSPACE)
    c = duckdb.connect()
    c.execute(f"CREATE TABLE sales AS {SALES}")

    GATES.clear()
    GATES["sales"] = _gate()

    def _require(con_, dataset_name):
        gate = GATES.get(dataset_name)
        if gate is None:
            raise ContractRefused(Refusal(
                reason=Reason.NO_CONTRACT,
                what=f"'{dataset_name}' has no confirmed Dataset Contract.",
                why="no analysis runs without an agreed grain.",
                state="loaded, no contract",
                next_call=f'propose_dataset_contract(dataset_name="{dataset_name}")',
            ))
        return gate

    monkeypatch.setattr(tools, "require_contract", _require)
    yield c
    c.close()


def run(con, analysis_type="summary_stats", dataset_name="sales", **params):
    return tools.compute_analysis(
        con, WORKSPACE, dataset_name, analysis_type, **params)


# --- the catalogue


def test_importing_the_package_registers_every_analysis():
    """A partial catalogue is a wrong answer that reads like a right one."""
    names = {name for name, _, _ in registry.catalogue()}
    assert {"summary_stats", "frequency", "top_n", "group_compare",
            "pareto", "concentration", "ranking_shift"} <= names


# --- the result


def test_a_result_carries_the_contract_header_and_the_envelope(con):
    text = run(con)
    assert reason_of(text) is None
    assert text.startswith("Under contract v1 for sales: one row = one sale.")
    assert "rows x 11 columns, written to" in text
    assert "read_result_file(path=" in text or "1 rows x" in text


def test_the_method_note_leads_what_this_shows(con):
    text = run(con)
    assert "3 of 5 row(s) analysed" in text


def test_the_gates_caveats_travel_into_the_result(con):
    text = run(con)
    assert "v1 agreed at 5 rows" in text


def test_the_file_it_names_can_actually_be_read_back(con):
    """The round trip, asserted rather than assumed: a path that does not open
    is F7 with extra steps."""
    text = run(con)
    path = next(line.strip() for line in text.splitlines()
                if line.strip().endswith(".csv"))
    page = results.read_result_file(WORKSPACE, path)
    assert reason_of(page) is None
    assert "amount" in page


def test_each_analysis_writes_under_its_own_label(con):
    run(con)
    run(con, "frequency", column="status")
    names = [p.name for p in results.list_results(WORKSPACE)]
    assert any(n.startswith("summary_stats_") for n in names)
    assert any(n.startswith("frequency_") for n in names)


# --- the run record


def test_a_successful_analysis_records_the_call_that_produced_it(con):
    """P12-O1. Before this, top_n_<stamp>.csv said an analysis called top_n ran at a time and
    nothing said what it was called with."""
    from backend.engine.analysis import runs

    # This fixture's columns are id, status, ts and amount. The first version of this test
    # asked for dimension="region", which clean_sales has and this does not, so the call was
    # refused and nothing was recorded -- which is the behaviour working, and was read as a
    # failure of the record for about a minute.
    run(con, "top_n", dimension="status", measure="amount", n=3)
    recorded = runs.history(con, "sales")
    assert len(recorded) == 1
    assert recorded[0].call() == (
        'compute_analysis(dataset_name="sales", analysis_type="top_n", '
        'dimension="status", measure="amount", n=3)'
    )
    assert recorded[0].result_path and recorded[0].result_path.endswith(".csv")
    assert recorded[0].method_note()


def test_a_chart_records_its_kind_and_its_png(con):
    from backend.engine.analysis import runs

    draw(con, "frequency", "bar", column="status")
    recorded = runs.charts(con, "sales")
    assert len(recorded) == 1
    assert recorded[0].chart_kind == "bar"
    assert recorded[0].chart_path.endswith(".png")
    assert 'chart="bar"' in recorded[0].call()


def test_a_refused_call_records_nothing(con):
    """The appendix lists what ran, not what was attempted. A refusal is a sentence the caller
    already has; it is not a step in reproducing the work."""
    from backend.engine.analysis import runs

    assert reason_of(run(con, "summry_stats")) is Reason.ANALYSIS_NOT_FOUND
    assert reason_of(run(con, "summary_stats", n=5)) is Reason.ANALYSIS_PARAMS_INVALID
    assert reason_of(run(con, dataset_name="other")) is Reason.NO_CONTRACT
    assert runs.count(con) == 0


# --- charts


def draw(con, analysis_type="frequency", chart="bar", dataset_name="sales", **params):
    return tools.render_chart(
        con, WORKSPACE, dataset_name, analysis_type, chart, **params)


def test_a_chart_carries_the_contract_header_and_the_envelope(con):
    """Rule 4, and the gate. A chart is a claim about data, so it goes through what a table
    goes through and says which contract it was drawn under."""
    text = draw(con, "frequency", "bar", column="status", y="rows")
    assert text.startswith("Under contract v1 for sales:")
    assert "Chart written:" in text
    assert "You cannot see this image" in text


def test_the_png_it_names_is_really_there_and_is_a_png(con):
    text = draw(con, "frequency", "bar", column="status", y="rows")
    named = [ln.split("Chart written: ", 1)[1].strip()
             for ln in text.splitlines() if ln.startswith("Chart written: ")]
    assert len(named) == 1, text
    path = Path(named[0])
    assert path.exists()
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert path.parent.name == "charts"


def test_a_chart_reports_the_numbers_a_reader_cannot_see(con):
    text = draw(con, "frequency", "bar", column="status", y="rows")
    assert "point(s) drawn" in text
    assert "lowest" in text and "highest" in text
    assert "rows" in text


def test_no_contract_refuses_a_chart_in_the_same_words_as_a_table(con):
    text = draw(con, dataset_name="other", column="status", y="rows")
    assert reason_of(text) is Reason.NO_CONTRACT
    assert 'propose_dataset_contract(dataset_name="other")' in text


def test_an_unknown_analysis_is_refused_before_anything_is_drawn(con):
    text = draw(con, "summry_stats", "bar")
    assert reason_of(text) is Reason.ANALYSIS_NOT_FOUND


def test_an_unknown_chart_kind_is_refused_and_names_the_ones_that_exist(con):
    text = draw(con, "frequency", "piechart", column="status")
    assert reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE
    assert "line, bar" in text


def test_a_chart_refusal_says_the_analysis_was_fine(con):
    """The distinction worth spending a sentence on: the numbers ran, the shape does not fit.
    An agent told only 'not possible' would doubt the data."""
    text = draw(con, "summary_stats", "line")
    assert reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE
    assert "The numbers are not in question" in text
    assert "Nothing was written." in text


def test_a_refusal_that_y_would_fix_names_the_chart_call_not_the_table(con):
    """C93. The recovery used to be compute_analysis, which succeeds and returns a table to a
    caller who asked for a chart. It is now render_chart with y named, and it draws."""
    text = draw(con, "summary_stats", "line")
    step = next(ln for ln in text.splitlines() if ln.startswith("NEXT STEP: call "))
    call = step[len("NEXT STEP: call "):]
    assert call.startswith('render_chart(dataset_name="sales", analysis_type="summary_stats"')
    assert 'chart="line"' in call and ", y=" in call
    assert "works as y instead" in text
    kwargs = dict(kv.split("=", 1) for kv in call[len("render_chart("):-1].split(", "))
    again = draw(con, "summary_stats", "line", y=kwargs["y"].strip('"'))
    assert reason_of(again) is None, again.splitlines()[0]


def test_the_suggested_y_prefers_the_analysis_measure_over_the_first_offered():
    assert tools._suggested_y(("rows", "revenue (sum)"), "revenue") == "revenue (sum)"
    assert tools._suggested_y(("n", "mean"), None) == "n"
    assert tools._suggested_y(("to", "rows"), "revenue") == "to"


def test_a_refusal_no_y_can_fix_still_names_the_table(con):
    """Too few measures for a two-measure kind has no y that fixes it, so the table stays."""
    text = draw(con, "frequency", "grouped_bar", column="status")
    assert reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE
    assert 'NEXT STEP: call compute_analysis(dataset_name="sales"' in text
    assert "works as y instead" not in text


def test_naming_y_resolves_the_one_measure_a_line_needs(con):
    """The refusal above names the fix; this is the fix working."""
    text = draw(con, "summary_stats", "line", y="mean")
    assert reason_of(text) is None
    assert "Chart written:" in text


def test_a_kind_that_wants_two_measures_gets_them_without_y(con):
    for kind in ("grouped_bar", "scatter", "box"):
        text = draw(con, "summary_stats", kind)
        assert reason_of(text) is None, f"{kind}: {text.splitlines()[0]}"


def test_a_heatmap_of_different_quantities_is_refused(con):
    """Superseded at Phase 14 Step 12 (P14-O20): the test above drew summary_stats as a heatmap
    too, which shades n, nulls, total and mean on one colour scale. A heatmap is drawn for
    cross_tab and cohort_retention only (tests/test_benchmark_fixes.py draws both)."""
    text = draw(con, "summary_stats", "heatmap")
    assert reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE
    assert "one colour scale" in text


def test_frequency_offers_one_measure_because_share_is_a_percentage(con):
    """Measured while writing these tests, having assumed otherwise. frequency's columns are
    value, rows and share, and share is rendered as a percentage -- a string that is not a
    magnitude, so is_numeric_column rejects it and only rows is plottable. A grouped bar of
    frequency is therefore refused rather than drawn, which is right: two of its three columns
    are the same count in different clothes."""
    text = draw(con, "frequency", "grouped_bar", column="status")
    assert reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE
    assert "draws two or more measures" in text

    ok = draw(con, "frequency", "bar", column="status")
    assert reason_of(ok) is None, ok.splitlines()[0]
    assert "measure rows" in ok


# --- the refusals


def test_no_contract_passes_the_gates_refusal_straight_through(con):
    text = run(con, dataset_name="other")
    assert reason_of(text) is Reason.NO_CONTRACT
    assert 'propose_dataset_contract(dataset_name="other")' in text


def test_an_unknown_analysis_returns_the_whole_valid_list(con):
    text = run(con, "summry_stats")
    assert reason_of(text) is Reason.ANALYSIS_NOT_FOUND
    for name in ("summary_stats", "frequency", "top_n", "group_compare",
                 "pareto", "concentration", "ranking_shift"):
        assert name in text


def test_arguments_an_analysis_cannot_take_are_params_invalid(con):
    text = run(con, "summary_stats", n=5)
    assert reason_of(text) is Reason.ANALYSIS_PARAMS_INVALID
    assert "compute_analysis(" in text


def test_an_unparseable_period_is_params_invalid_not_not_possible(con):
    text = run(con, "ranking_shift", dimension="status", measure="amount",
               before_start="last January", before_end="2024-01-31",
               after_start="2024-03-01", after_end="2024-03-31")
    assert reason_of(text) is Reason.ANALYSIS_PARAMS_INVALID


def test_an_undeclared_column_is_not_possible_and_names_the_contract(con):
    text = run(con, "frequency", column="id")
    assert reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE
    assert 'propose_dataset_contract(dataset_name="sales")' in text


def test_a_measure_with_no_aggregate_is_not_possible(con):
    GATES["sales"] = _gate(measures=[FakeMeasure("amount", agg=None)])
    text = run(con, "top_n", dimension="status", measure="amount")
    assert reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE


# --- the refusal that is not the caller's fault


def test_a_result_without_its_method_note_is_unsound_and_writes_nothing(con):
    """All nine put scope.method_note() first in summary and nothing enforced
    it. A result that does not say what it was computed over is refused."""
    registry.REGISTRY["_silent"] = registry.Analysis(
        name="_silent", tier=1, summary="test double",
        run=lambda con, gate, scope, **p: Output(
            headers=["x"], rows=[[1]], summary=["not the method note"],
            label="silent"),
    )
    try:
        text = run(con, "_silent")
        assert reason_of(text) is Reason.ANALYSIS_RESULT_UNSOUND
        assert "Nothing was written" in text
        assert not any(p.name.startswith("silent_")
                       for p in results.list_results(WORKSPACE))
    finally:
        del registry.REGISTRY["_silent"]


def test_lost_rows_is_unsound_rather_than_a_traceback(con):
    def _loses(con, gate, scope, **params):
        raise LostRows("its cells count 2 row(s) against 3 in scope.")

    registry.REGISTRY["_lossy"] = registry.Analysis(
        name="_lossy", tier=1, summary="test double", run=_loses)
    try:
        text = run(con, "_lossy")
        assert reason_of(text) is Reason.ANALYSIS_RESULT_UNSOUND
        assert 'validate_dataset(dataset_name="sales")' in text
    finally:
        del registry.REGISTRY["_lossy"]


def test_every_refusal_names_a_call_the_agent_can_make(con):
    for kwargs in ({"analysis_type": "summry_stats"},
                   {"analysis_type": "summary_stats", "n": 5},
                   {"analysis_type": "frequency", "column": "id"}):
        text = run(con, **kwargs)
        line = next(l for l in text.splitlines() if l.startswith("NEXT STEP:"))
        assert "(" in line and ")" in line


def test_a_trend_bar_draws_the_measure_it_was_given(con):
    """Cleanup Step 8, from the bunty_babli run: this refused and spent the agent's last step."""
    text = draw(con, "trend", "bar", measure="amount")
    assert reason_of(text) is None, text.splitlines()[:3]
    assert "measure amount (sum)" in text


def test_a_period_reaches_the_analysis_through_the_tool_layer(con):
    """Cleanup Step 9, 4.1: narrowed inside top_n, every result was refused as
    ANALYSIS_RESULT_UNSOUND -- its method note described a scope _produce never built. The unit
    tests went through registry.run and could not see it."""
    text = run(con, "top_n", dimension="status", measure="amount", period="2024-06")
    assert reason_of(text) is None, text.splitlines()[:3]
    assert "outside the month 2024-06" in text
    assert 'period="2024-06"' in text or "2024-06" in text


# --- a group cap names the call its WHY names (Cleanup Step 11, CL10-O1) ----------------------

WIDE = ("SELECT i AS id, 'shop' || lpad(i::VARCHAR, 2, '0') AS shop, "
        "TIMESTAMP '2024-06-01' + INTERVAL (i % 28) DAY AS ts, (i + 1)::DECIMAL(18,2) AS amount "
        "FROM range(60) t(i)")


@pytest.fixture()
def wide(con):
    con.execute(f"CREATE TABLE wide AS {WIDE}")
    GATES["wide"] = _gate(dataset_name="wide", dimensions=["shop"], known_exclusions=[])
    return con


def _next_call(text):
    return next(ln for ln in text.splitlines()
                if ln.startswith("NEXT STEP: call "))[len("NEXT STEP: call "):]


def test_a_group_cap_names_top_n_not_a_new_contract(wide):
    """Live, 15:03: WHY said 'top_n on order_id says which of its groups matter' and NEXT STEP
    said propose_dataset_contract, and the assistant spent its last round on the refusal."""
    # concentration until Cleanup Step 14 removed its cap; group_compare still has one.
    text = run(wide, "group_compare", dataset_name="wide", dimension="shop", measure="amount")
    assert reason_of(text) is Reason.ANALYSIS_NOT_POSSIBLE
    assert _next_call(text) == ('compute_analysis(dataset_name="wide", analysis_type="top_n", '
                                'dimension="shop", measure="amount")')


def test_the_named_top_n_succeeds_verbatim(wide):
    text = run(wide, "group_compare", dataset_name="wide", dimension="shop", measure="amount")
    assert 'analysis_type="top_n"' in _next_call(text)
    again = run(wide, "top_n", dataset_name="wide", dimension="shop", measure="amount")
    assert reason_of(again) is None, again.splitlines()[:3]
