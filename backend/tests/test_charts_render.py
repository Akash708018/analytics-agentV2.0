"""The render layer: what it draws, what it refuses, and what it says about a file nobody can see.

The values an analysis hands over are display strings -- `base.number()` has already run -- so
half of these tests are about the parse back. The other half are about Rule 4: a chart that
returns a path and nothing else is the one failure this layer exists to prevent.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import matplotlib.pyplot as plt  # noqa: E402

from backend.engine import workspace  # noqa: E402
from backend.engine.analysis.registry import Output  # noqa: E402
from backend.engine.charts.render import (  # noqa: E402
    KINDS,
    Chart,
    ChartRefused,
    as_number,
    charts_dir,
    is_numeric_column,
    render,
    series_from_output,
)

WORKSPACE = "charts_test"
T1 = datetime(2026, 9, 21, 12, 0, 0)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def ws():
    workspace.reset(WORKSPACE)
    try:
        yield WORKSPACE
    finally:
        workspace.reset(WORKSPACE)


def out(rows=None, headers=None, summary=None) -> Output:
    """An Output shaped the way every analysis builds one: strings and Nones."""
    return Output(
        headers=headers or ["group", "one", "two"],
        rows=rows if rows is not None else [
            ["south", "4.0", "1.0"],
            ["north", "3.0", "2.5"],
            ["east", "2.0", "3.0"],
            ["west", "1,234.56", "4.0"],
        ],
        summary=summary or ["14 of 14 row(s) analysed."],
        label="fake",
    )


# --- the parse back ------------------------------------------------------------------------

def test_a_thousands_separator_survives_the_round_trip():
    """base.number() writes '1,234.56'; an axis needs 1234.56."""
    assert as_number("1,234.56") == 1234.56
    assert as_number("11.5") == 11.5
    assert as_number("6") == 6.0


def test_a_negative_that_rounded_away_parses():
    """Measured on a real Output: a skew of about -1e-9 renders as the string '-0'."""
    assert as_number("-0") == 0.0


def test_an_empty_cell_is_a_gap_not_a_zero():
    """P8-D9 keeps None as None. A zero here would draw a point that is not in the data."""
    assert as_number(None) is None
    assert as_number("") is None
    assert as_number("   ") is None


def test_a_non_finite_value_is_refused_and_says_why():
    """P8-D1 lets nan and inf reach a cell as text so a reader sees them. An axis cannot."""
    for text in ("nan", "inf", "-inf"):
        with pytest.raises(ChartRefused, match="finite"):
            as_number(text)
    with pytest.raises(ChartRefused, match="finite"):
        as_number(float("nan"))


def test_a_group_label_is_refused_as_a_measure():
    with pytest.raises(ChartRefused, match="not a number"):
        as_number("south")


def test_a_boolean_is_not_a_magnitude():
    with pytest.raises(ChartRefused, match="true/false"):
        as_number(True)


def test_a_column_is_numeric_only_when_every_filled_cell_parses():
    assert is_numeric_column(["1", "2.5", None]) is True
    assert is_numeric_column(["1", "south"]) is False
    assert is_numeric_column([None, None]) is False


# --- what gets extracted -------------------------------------------------------------------

def test_the_first_column_is_the_x_and_the_numeric_rest_are_the_measures():
    e = series_from_output(out())
    assert e.x == ["south", "north", "east", "west"]
    assert [s.name for s in e.series] == ["one", "two"]
    assert e.series[0].values == [4.0, 3.0, 2.0, 1234.56]


def test_naming_y_takes_only_that_measure():
    e = series_from_output(out(), y=["two"])
    assert [s.name for s in e.series] == ["two"]


def test_a_column_that_is_not_there_is_refused_with_the_list():
    with pytest.raises(ChartRefused, match="Columns: group, one, two"):
        series_from_output(out(), y=["three"])


def test_a_result_with_no_numeric_column_is_refused():
    o = out(headers=["group", "note"], rows=[["a", "x"], ["b", "y"]])
    with pytest.raises(ChartRefused, match="no column"):
        series_from_output(o)


def test_an_empty_result_is_refused_rather_than_drawn_blank():
    with pytest.raises(ChartRefused, match="no rows"):
        series_from_output(out(rows=[]))


def test_empty_points_are_counted_and_named():
    o = out(rows=[["a", "1.0", "2.0"], ["b", None, "3.0"]])
    e = series_from_output(o)
    assert len(e.dropped) == 1
    assert "1 of 2 point(s) in one are empty" in e.dropped[0]


# --- drawing -------------------------------------------------------------------------------

def test_every_kind_the_guide_names_writes_a_png(ws):
    """Eight kinds, guide line 943, each from an Output rather than from prepared numbers."""
    single = {"line", "bar", "histogram", "waterfall"}
    for kind in KINDS:
        chart = render(ws, kind=kind, output=out(), label="k",
                       y=["one"] if kind in single else None, now=T1)
        assert chart.path.exists(), f"{kind} wrote nothing"
        assert chart.path.read_bytes()[:8] == PNG_MAGIC, f"{kind} is not a PNG"
        assert chart.path.suffix == ".png"


def test_two_renders_of_the_same_data_are_byte_identical(ws):
    """P11-D7 measured matplotlib writing no timestamp, which is what makes this assertable."""
    a = render(ws, kind="bar", output=out(), label="same", y=["one"], now=T1)
    b = render(ws, kind="bar", output=out(), label="same", y=["one"], now=T1)
    assert a.path != b.path, "the collision counter did not fire"
    assert a.path.read_bytes() == b.path.read_bytes()


def test_a_chart_lands_in_the_workspace_charts_directory(ws):
    chart = render(ws, kind="line", output=out(), label="where", y=["one"], now=T1)
    assert chart.path.parent == charts_dir(ws)
    assert chart.path.parent.name == "charts"


def test_an_unknown_kind_is_refused_with_the_list():
    with pytest.raises(ChartRefused, match="Available: line, bar"):
        render(WORKSPACE, kind="piechart", output=out(), label="x")


def test_a_label_that_is_not_a_filename_is_refused():
    with pytest.raises(ChartRefused, match="becomes a filename"):
        render(WORKSPACE, kind="line", output=out(), label="two words", y=["one"])


def test_a_single_measure_kind_refuses_two_measures_and_says_to_name_one():
    with pytest.raises(ChartRefused, match="Name one with y"):
        render(WORKSPACE, kind="line", output=out(), label="x")


def test_scatter_needs_two_measures():
    with pytest.raises(ChartRefused, match="Two are needed"):
        render(WORKSPACE, kind="scatter", output=out(), label="x", y=["one"])


def test_a_row_with_an_empty_measure_is_dropped_and_reported(ws):
    o = out(rows=[["a", "1.0", "2.0"], ["b", None, "3.0"], ["c", "3.0", "4.0"]])
    chart = render(ws, kind="bar", output=o, label="holes", y=["one"], now=T1)
    assert chart.drawn_count == 2
    assert chart.point_count == 3
    assert any("are empty and are not drawn" in n for n in chart.notes)


def test_a_measure_that_is_entirely_empty_is_refused_rather_than_drawn(ws):
    o = out(rows=[["a", None, "2.0"], ["b", None, "3.0"]])
    with pytest.raises(ChartRefused, match="nothing to place"):
        render(ws, kind="bar", output=o, label="empty", y=["one"], now=T1)


def test_rendering_leaves_no_figure_open(ws):
    """P11-D6: pyplot holds every figure it makes and this server does not exit between calls."""
    plt.close("all")
    for _ in range(3):
        render(ws, kind="bar", output=out(), label="leak", y=["one"], now=T1)
    assert plt.get_fignums() == []


def test_a_refused_render_also_leaves_no_figure_open(ws):
    """The finally, not the happy path. A refusal after the figure opens is the leak that hides."""
    plt.close("all")
    o = out(rows=[["a", None, "2.0"], ["b", None, "3.0"]])
    with pytest.raises(ChartRefused):
        render(ws, kind="bar", output=o, label="leak", y=["one"], now=T1)
    assert plt.get_fignums() == []


# --- Rule 4 --------------------------------------------------------------------------------

def test_the_text_carries_the_path_the_counts_and_the_numbers(ws):
    chart = render(ws, kind="bar", output=out(), label="rule4", y=["one"],
                   dataset_name="olist", now=T1)
    text = chart.to_text()
    assert str(chart.path) in text
    assert "You cannot see this image" in text
    assert "4 of 4 point(s) drawn" in text
    assert "group across the x axis" in text
    assert "for olist" in text
    # one = [4.0, 3.0, 2.0, 1234.56], so the lowest is 2 at east -- not 1, which is what
    # this line asserted first and is why the series is spelled out here.
    assert "lowest 2 at east" in text
    assert "highest 1,235 at west" in text
    assert "first 4, last 1,235" in text
    assert "14 of 14 row(s) analysed." in text


def test_there_is_no_accessor_that_returns_a_bare_path():
    """util.results.Result makes the same promise: the one that exists is the one that gets used."""
    names = [n for n in dir(Chart) if not n.startswith("_")]
    assert "to_text" in names
    assert not any(n in names for n in ("path_text", "as_path", "filename", "location"))


# --- the (all) roll-up (C98) -------------------------------------------------------------------

def test_the_rollup_row_is_not_drawn_beside_the_groups_it_totals():
    """C98, found by the first live agent run: group_compare's (all) row was drawn as a sixth bar,
    1.378e+06 beside groups of ~3e+05, and named as the chart's highest value."""
    rows = [["south", "378,416.78"], ["north", "320,593.28"], ["(all)", "1,377,896.84"]]
    ex = series_from_output(out(rows=rows, headers=["region", "total"]))
    assert ex.x == ["south", "north"]
    assert ex.series[0].values == [378416.78, 320593.28]
    assert any("(all)" in note and "not drawn" in note for note in ex.dropped)


def test_an_rollup_that_is_the_only_row_is_the_answer_and_is_drawn():
    """confidence_interval with no dimension returns one row, (all): it is the result."""
    ex = series_from_output(out(rows=[["(all)", "12.5"]], headers=["group", "mean"]))
    assert ex.x == ["(all)"] and ex.dropped == []


# --- defaults the caller already chose (Cleanup Step 8) --------------------------------------

TREND = out(headers=["period", "order_value (sum)", "rows"],
            rows=[["2025-01", "163,606.39", "52"], ["2025-02", "110,565.31", "50"]])
SPLIT = out(headers=["period", "Online", "Store", "(all)", "rows"],
            rows=[["2025-01", "90.00", "73.61", "163.61", "52"],
                  ["2025-02", "60.00", "50.57", "110.57", "50"]])


def test_the_measure_the_caller_named_is_the_one_drawn():
    """bunty_babli: render_chart(trend, bar, measure="order_value") refused, offering
    'order_value (sum)' and 'rows'. P11-D13 forbids picking a measure nobody named; this one
    was named."""
    got = series_from_output(TREND, measure="order_value")
    assert [s.name for s in got.series] == ["order_value (sum)"]
    assert any("rows" in n for n in got.dropped)


def test_with_no_measure_named_nothing_is_chosen():
    assert [s.name for s in series_from_output(TREND).series] == ["order_value (sum)", "rows"]


def test_a_measure_named_is_matched_whole_not_as_a_substring():
    got = series_from_output(out(headers=["period", "units_returned (sum)", "rows"],
                                 rows=[["a", "1", "2"]]), measure="units")
    assert [s.name for s in got.series] == ["units_returned (sum)"], (
        "units is not units_returned; with no column carrying it, rows is still left out "
        "because the caller asked about a measure and rows is not one."
    )


def test_a_rollup_column_is_not_drawn_beside_the_members_it_totals():
    """C98 for columns: (all) beside Online and Store is their sum and dwarfs them."""
    got = series_from_output(SPLIT, measure="order_value")
    assert [s.name for s in got.series] == ["Online", "Store"]
    assert any("(all)" in n for n in got.dropped)


def test_a_total_column_is_left_out_even_with_no_measure_named():
    got = series_from_output(out(headers=["region", "Online", "Store", "(total)"],
                                 rows=[["north", "1", "2", "3"]]))
    assert [s.name for s in got.series] == ["Online", "Store"]


def test_a_rollup_column_that_is_the_only_series_is_drawn():
    got = series_from_output(out(headers=["region", "(total)"], rows=[["north", "3"]]))
    assert [s.name for s in got.series] == ["(total)"]


def test_the_named_measure_draws_a_bar_through_render(ws):
    chart = render(ws, kind="bar", output=TREND, label="trend", measure="order_value", now=T1)
    assert chart.series_names == ["order_value (sum)"]


# --- every x keeps its slot (Cleanup Step 9) --------------------------------------------------
#
# The 12:17 grouped bar had no July: _draw kept only x positions where every series had a
# value, so an absent period vanished and June sat beside August.

GAPPED = out(headers=["period", "a", "b"],
             rows=[["06", "1", "2"], ["07", None, None], ["08", "3", None]])


def _ticks(kind, output, y=None):
    from backend.engine.charts import render as r
    fig, ax = plt.subplots()
    try:
        drawn = r._draw(ax, kind, r.series_from_output(output, y=y))
        return [t.get_text() for t in ax.get_xticklabels()], drawn
    finally:
        plt.close(fig)


@pytest.mark.parametrize("kind", ["bar", "line"])
def test_an_empty_period_keeps_its_slot_on_the_axis(kind):
    ticks, drawn = _ticks(kind, GAPPED, y=["a"])
    assert ticks == ["06", "07", "08"] and drawn == 2


def test_a_grouped_bar_keeps_the_empty_slot_and_draws_the_values_it_has():
    ticks, drawn = _ticks("grouped_bar", GAPPED)
    assert ticks == ["06", "07", "08"]
    assert drawn == 3, "06 has two values and 08 one; 07 none"


def test_the_empty_slot_is_named(ws):
    chart = render(ws, kind="grouped_bar", output=GAPPED, label="gapped", now=T1)
    assert any("07" in n and "empty slot" in n for n in chart.notes)
