"""Charts, drawn from what an analysis returned and described so a reader who cannot see one
still learns what it holds.

Rule 4 (build guide line 563) is why `Chart` exists rather than a path: handing an LLM a file it
cannot open invites a confident report about data it never saw, and a PNG is the one artifact in
this engine that literally cannot be read back. So every render returns the path together with
what was plotted, how much of it, and the numbers a reader would have taken off the axes.

**The values arriving here are strings.** Every analysis builds its rows with `base.number()` and
`base.label()`, so an `Output` cell is a display string or None -- `'1,234.56'`, `'11.5'`, `'-0'`
-- never a float. Measured 21/09/2026. `as_number` is the inverse and is the only place that
parse happens; the eight drawing functions below see plain numbers and nothing else.

**Nulls are screened here rather than by matplotlib.** P11-D4 measured `ax.bar` on a series
holding a None raising `TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'` --
a message naming no column, no row and no chart, which a caller cannot act on. Every analysis in
this engine can return a None cell (P8-D9 keeps None as None), so the chart layer drops those
points itself and says how many it dropped, the way P10-D36 makes every Tier 6 and 7 analysis
report what it excludes.

**The backend is pinned before pyplot is imported.** P11-D2: pyplot chooses its backend at import
time, so `matplotlib.use` afterwards is too late to be a guarantee, and a GUI backend can hang
this server on macOS. Figures are closed in a `finally` because pyplot holds a reference to every
one it makes (P11-D6) and this process does not exit between renders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  -- must follow the backend choice

from .. import workspace  # noqa: E402
from ..util.results import claim  # noqa: E402

CHARTS_DIRNAME = "charts"

#: The eight the build guide names at line 943. Waterfall is there for `mix_shift`.
KINDS = ("line", "bar", "grouped_bar", "scatter", "histogram", "box", "heatmap", "waterfall")

#: Kinds that plot one series against the x labels, and so need exactly one measure.
_SINGLE = ("line", "bar", "histogram", "waterfall")
#: The label group_compare and confidence_interval give the row computed over all the others.
ROLLUP_LABEL = "(all)"
#: Columns that total the columns beside them: trend's split and cross_tab's margin.
ROLLUP_COLUMNS = ("(all)", "(total)")
#: The row count most results carry beside their measure. It is the measure only when no
#: measure was asked for -- frequency and calendar_coverage chart exactly this.
SUPPORT_COLUMN = "rows"

_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_NOT_FINITE = ("nan", "inf", "-inf", "+inf", "infinity", "-infinity")


class ChartRefused(ValueError):
    """A chart that cannot be drawn honestly, refused in this project's words.

    A ValueError so that the tool layer's existing `except ValueError` turns it into an
    ANALYSIS_NOT_POSSIBLE refusal if Step 3 wires it before giving charts their own branch.

    `choices` is set only when naming one of them as y would draw the chart -- the tool layer
    turns it into a render_chart call rather than a compute_analysis one (C93).
    """

    def __init__(self, message: str, choices: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.choices = tuple(choices)


def charts_dir(workspace_id: str) -> Path:
    """The workspace's charts directory, created on demand -- guide line 721 puts it beside
    `results/` rather than inside it, because a PNG is not a result file and `read_result_file`
    cannot page one."""
    path = workspace.workspace_dir(workspace_id) / CHARTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def as_number(cell: Any) -> float | None:
    """One cell as a number, or None if it is empty. The inverse of `base.number()`.

    Raises ChartRefused on anything that is neither -- a group label, or a non-finite value that
    P8-D1 lets through as the text 'nan' or 'inf'. A chart is a claim about magnitudes, and a
    point that is not a magnitude has no honest position on an axis.
    """
    if cell is None:
        return None
    if isinstance(cell, bool):
        raise ChartRefused(f"{cell!r} is a true/false value and has no position on an axis.")
    if isinstance(cell, (int, float)):
        value = float(cell)
        if value != value or value in (float("inf"), float("-inf")):
            raise ChartRefused(f"{cell!r} is not a finite number and cannot be plotted.")
        return value

    text = str(cell).strip()
    if not text:
        return None
    if text.lower() in _NOT_FINITE:
        raise ChartRefused(
            f"{text!r} is not a finite number and cannot be plotted. A cell reaches a chart as "
            f"text because base.number() rendered it; P8-D1 lets nan and inf through so a "
            f"reader sees them, and an axis cannot."
        )
    try:
        return float(text.replace(",", ""))
    except ValueError:
        raise ChartRefused(
            f"{text!r} is not a number. Only a measure column can be plotted; a dimension's "
            f"members belong on the other axis."
        ) from None


def is_numeric_column(cells: Sequence[Any]) -> bool:
    """True when every non-empty cell in the column parses, and at least one does.

    How a measure column is told from a dimension column without asking the contract: the
    contract is not reachable from here, and the Output has already lost which is which.
    """
    seen = False
    for cell in cells:
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            continue
        try:
            as_number(cell)
        except ChartRefused:
            return False
        seen = True
    return seen


@dataclass(frozen=True)
class Series:
    """One measure, named as its column was, with None where a cell was empty."""

    name: str
    values: list[float | None]

    @property
    def present(self) -> list[float]:
        return [v for v in self.values if v is not None]


@dataclass(frozen=True)
class Extract:
    """What `series_from_output` found: the x labels, the measures, and what it left out."""

    x: list[str]
    series: list[Series]
    dropped: list[str] = field(default_factory=list)


def series_from_output(output, x: str | None = None, y: Sequence[str] | None = None,
                       measure: str | None = None) -> Extract:
    """The plottable parts of an analysis result.

    `x` names the label column and defaults to the first, which is where every analysis in this
    engine puts its group. `y` names the measures and defaults to every remaining column that is
    numeric all the way down -- a column holding one label is not a measure with a bad cell, it
    is a different kind of column.

    With y unset, two narrowings the caller has already made (Cleanup Step 8). A roll-up column
    is left out beside the columns it totals, as C98 leaves out the roll-up row. And `measure`,
    the analysis's own argument: the one column that IS it (`revenue`, or `revenue (sum)`) is
    drawn; with none, `rows` is left out, because the caller asked about a measure and a row
    count is not one. P11-D13 stands -- nothing is picked that nobody named, and a result still
    offering several refuses as before.
    """
    headers = [str(h) for h in output.headers]
    if not headers:
        raise ChartRefused("this result has no columns, so there is nothing to plot.")
    if not output.rows:
        raise ChartRefused("this result has no rows, so there is nothing to plot.")

    x_name = x if x is not None else headers[0]
    if x_name not in headers:
        raise ChartRefused(
            f"{x_name!r} is not a column of this result. Columns: {', '.join(headers)}."
        )
    x_at = headers.index(x_name)
    rows = output.rows
    rollup_note: list[str] = []
    # A roll-up row totals the rows beside it; drawn as a peer it dwarfs them and becomes the
    # chart's "highest value". Found by the first live agent run: group_compare's (all) bar at
    # 1.378e+06 beside regions of ~3e+05 (C98). Left in when it is the only row -- then it is the
    # answer (confidence_interval with no dimension) -- and always kept in the table.
    kept = [r for r in rows if r[x_at] != ROLLUP_LABEL]
    if kept and len(kept) < len(rows):
        rows = kept
        rollup_note.append(
            f"The {ROLLUP_LABEL} row totals the groups shown and is not drawn beside them; the "
            f"table beside this chart still holds it.")
    labels = [("" if r[x_at] is None else str(r[x_at])) for r in rows]

    if y is not None:
        wanted = [str(name) for name in y]
        missing = [name for name in wanted if name not in headers]
        if missing:
            raise ChartRefused(
                f"{', '.join(missing)} is not a column of this result. "
                f"Columns: {', '.join(headers)}."
            )
    else:
        wanted = [
            h for i, h in enumerate(headers)
            if i != x_at and is_numeric_column([r[i] for r in rows])
        ]
        members = [h for h in wanted if h not in ROLLUP_COLUMNS]
        if len(members) < len(wanted) and [h for h in members if h != SUPPORT_COLUMN]:
            left = [h for h in wanted if h in ROLLUP_COLUMNS]
            wanted = members
            rollup_note.append(
                f"{', '.join(left)} totals the columns shown and is not drawn beside them; the "
                f"table beside this chart still holds it.")
        if measure:
            named = [h for h in wanted if h == measure or h.startswith(f"{measure} (")]
            others = named or [h for h in wanted if h != SUPPORT_COLUMN]
            if others and len(others) < len(wanted):
                left = [h for h in wanted if h not in others]
                wanted = others
                rollup_note.append(
                    f"Drawn: {', '.join(wanted)}, for measure {measure}; "
                    f"{', '.join(left)} is in the table beside this chart.")
    if not wanted:
        raise ChartRefused(
            f"no column of this result holds numbers to plot. Columns: {', '.join(headers)}."
        )

    series: list[Series] = []
    for name in wanted:
        at = headers.index(name)
        series.append(Series(name=name, values=[as_number(r[at]) for r in rows]))

    dropped: list[str] = list(rollup_note)
    for s in series:
        empty = len(s.values) - len(s.present)
        if empty:
            dropped.append(
                f"{empty:,} of {len(s.values):,} point(s) in {s.name} are empty and are not "
                f"drawn; the table beside this chart still holds them."
            )
    return Extract(x=labels, series=series, dropped=dropped)


SERIES_SHOWN = 12


@dataclass(frozen=True)
class Chart:
    """A chart on disk, and everything needed to say what it holds without seeing it.

    `to_text()` is what a tool returns. As with `util.results.Result`, there is deliberately no
    accessor that produces just the path: the one that exists is the one that gets used, and a
    bare path is what Rule 4 forbids.
    """

    path: Path
    kind: str
    label: str
    created_at: datetime
    x_name: str
    series_names: list[str]
    point_count: int
    drawn_count: int
    key_values: list[str] = field(default_factory=list)
    dataset_name: str | None = None
    summary: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        of = f" for {self.dataset_name}" if self.dataset_name else ""
        plural = "s" if len(self.series_names) != 1 else ""
        # A cohort heatmap has a series per offset: 49 of them wrote a line each and the reply
        # passed the 8,000 characters an agent reads (Step 13 benchmark). The first
        # SERIES_SHOWN are described and the rest counted; the analysis's table holds them all.
        more = len(self.series_names) - SERIES_SHOWN
        names = ", ".join(self.series_names[:SERIES_SHOWN]) + (
            f" and {more:,} more" if more > 0 else "")
        lines = [
            f"Chart written: {self.path}",
            "",
            f"A {self.kind} chart of {self.label}{of}. "
            f"{self.drawn_count:,} of {self.point_count:,} point(s) drawn, "
            f"{self.x_name} across the x axis, "
            f"measure{plural} {names}.",
            "",
            "You cannot see this image. What it shows:",
        ]
        lines += [f"  {v}" for v in self.key_values[:SERIES_SHOWN]]
        if len(self.key_values) > SERIES_SHOWN:
            lines.append(f"  ... {len(self.key_values) - SERIES_SHOWN:,} more series, not "
                         f"described here; compute_analysis with the same arguments returns "
                         f"the table that holds them")
        if self.notes:
            notes = self.notes[:SERIES_SHOWN] + (
                [f"... {len(self.notes) - SERIES_SHOWN:,} more note(s) of the same kind"]
                if len(self.notes) > SERIES_SHOWN else [])
            lines += [""] + [f"  {n}" for n in notes]
        if self.summary:
            lines += ["", "From the analysis it was drawn from:"] + [
                f"  {s}" for s in self.summary
            ]
        return "\n".join(lines)


def _key_values(extract: Extract) -> list[str]:
    """The numbers a reader would have taken off the axes, since they cannot."""
    out: list[str] = []
    for s in extract.series:
        present = s.present
        if not present:
            out.append(f"{s.name}: no values")
            continue
        low, high = min(present), max(present)
        at_low = extract.x[s.values.index(low)] if low in s.values else "?"
        at_high = extract.x[s.values.index(high)] if high in s.values else "?"
        out.append(
            f"{s.name}: lowest {low:,.4g} at {at_low or '(blank)'}, "
            f"highest {high:,.4g} at {at_high or '(blank)'}, "
            f"first {present[0]:,.4g}, last {present[-1]:,.4g}, "
            f"{len(present):,} point(s)"
        )
    return out


def _positions(n: int) -> list[float]:
    return [float(i) for i in range(n)]


def _draw(ax, kind: str, extract: Extract) -> int:
    """Draw one kind onto an axis and return how many points reached it.

    Every branch receives numbers with the Nones already removed, because P11-D4 measured what
    matplotlib says when one gets through and it names nothing a caller could act on.
    """
    series = extract.series
    if kind in _SINGLE and len(series) != 1:
        raise ChartRefused(
            f"a {kind} chart draws one measure and this result offers "
            f"{len(series)}: {', '.join(s.name for s in series)}. Name one with y.",
            choices=[s.name for s in series],
        )

    if kind == "histogram":
        values = series[0].present
        if not values:
            raise ChartRefused("every value in this measure is empty, so there is nothing to bin.")
        ax.hist(values, bins=min(10, max(1, len(values))))
        ax.set_xlabel(series[0].name)
        ax.set_ylabel("count")
        return len(values)

    if kind == "box":
        data = [s.present for s in series]
        if not any(data):
            raise ChartRefused("every value is empty, so there is nothing to summarise.")
        ax.boxplot([d for d in data if d], tick_labels=[s.name for s in series if s.present])
        return sum(len(d) for d in data)

    if kind == "heatmap":
        # Every cell stays at its own column: an empty one is left blank (NaN is not drawn).
        # Dropping the empties shifted the later values of a row under the wrong labels and cut
        # every row to the shortest -- a cohort grid to its youngest cohort (P14-O20).
        width = len(extract.x)
        if not width or not any(s.present for s in series):
            raise ChartRefused("every value is empty, so there is nothing to shade.")
        grid = [[float("nan") if v is None else v for v in s.values] for s in series]
        image = ax.imshow(grid, aspect="auto")
        ax.set_yticks(_positions(len(series)))
        ax.set_yticklabels([s.name for s in series])
        ticks, names = _tick_subset(_positions(width), list(extract.x[:width]))
        ax.set_xticks(ticks)
        ax.set_xticklabels(names, rotation=45, ha="right")
        ax.figure.colorbar(image, ax=ax)
        return sum(len(s.present) for s in series)

    if kind == "scatter":
        if len(series) < 2:
            raise ChartRefused(
                "a scatter chart plots one measure against another and this result offers "
                f"{len(series)}. Two are needed."
            )
        pairs = [(a, b) for a, b in zip(series[0].values, series[1].values)
                 if a is not None and b is not None]
        if not pairs:
            raise ChartRefused("no row holds both measures, so there is no point to place.")
        ax.scatter([p[0] for p in pairs], [p[1] for p in pairs])
        ax.set_xlabel(series[0].name)
        ax.set_ylabel(series[1].name)
        return len(pairs)

    # The remaining kinds share an x of category positions, because P11-D5 measured matplotlib
    # keeping the order it is given and ranked_totals hands groups back biggest first.
    #
    # Every label keeps its slot (Cleanup Step 9). Positions used to be only the x where every
    # series had a value, so an absent month vanished from the axis and a chart of a gapped
    # calendar showed June beside August -- the evenly spaced line the trend analysis exists to
    # warn against. Now a slot with no value is drawn empty. Waterfall alone keeps the filter: a
    # running total cannot step over a missing part and still mean what it says.
    if kind == "waterfall":
        kept = [(i, lab) for i, lab in enumerate(extract.x)
                if all(s.values[i] is not None for s in series)]
    else:
        kept = list(enumerate(extract.x))
    if not any(s.values[i] is not None for s in series for i, _ in kept):
        raise ChartRefused(
            "no row holds a value for the measures drawn, so there is nothing to place."
        )
    at = _positions(len(kept))
    labels = [lab for _, lab in kept]

    def present(s: Series) -> tuple[list[float], list[float]]:
        pairs = [(p, s.values[i]) for p, (i, _) in zip(at, kept) if s.values[i] is not None]
        return [a for a, _ in pairs], [v for _, v in pairs]

    drawn = 0
    if kind == "line":
        # P11-D4: a None in plot() is a gap, so the line stops at the empty slot rather than
        # bridging it.
        ax.plot(at, [series[0].values[i] for i, _ in kept], marker="o")
        ax.set_ylabel(series[0].name)
        drawn = len(series[0].present)
    elif kind == "bar":
        xs, ys = present(series[0])
        ax.bar(xs, ys)
        ax.set_ylabel(series[0].name)
        drawn = len(ys)
    elif kind == "waterfall":
        values = [series[0].values[i] for i, _ in kept]
        bottoms: list[float] = []
        running = 0.0
        for v in values:
            bottoms.append(running)
            running += v
        ax.bar(at, values, bottom=bottoms)
        ax.set_ylabel(f"{series[0].name} (cumulative)")
        drawn = len(kept)
    elif kind == "grouped_bar":
        if len(series) < 2:
            raise ChartRefused(
                "a grouped bar chart draws two or more measures side by side and this result "
                f"offers {len(series)}."
            )
        width = 0.8 / len(series)
        start = -0.4 + width / 2
        for j, s in enumerate(series):
            xs, ys = present(s)
            ax.bar([p + start + j * width for p in xs], ys, width=width, label=s.name)
            drawn += len(ys)
        ax.legend()
    else:  # pragma: no cover -- render() checks the name before this is reached
        raise ChartRefused(f"{kind!r} is not a chart this engine draws.")

    at, labels = _tick_subset(list(at), list(labels))
    ax.set_xticks(at)
    ax.set_xticklabels(labels, rotation=45 if any(len(s) > 4 for s in labels) else 0,
                       ha="right" if any(len(s) > 4 for s in labels) else "center")
    return drawn


MAX_TICKS = 24


def _tick_subset(at: list[float], labels: list[str]) -> tuple[list[float], list[str]]:
    """At most MAX_TICKS labelled ticks, evenly spaced, the first and last always among them.

    Keeping every empty period (Cleanup Step 9) gave a trend over 1800-2999 14,400 monthly slots,
    and a tick and label for each: 98.8 s to draw 14 points, almost all of it matplotlib laying
    out labels no one could read (stress matrix, extreme_dates, after the merge of 25/09/2026).
    The slots stay; only their labels thin out.
    """
    n = len(at)
    if n <= MAX_TICKS:
        return list(at), list(labels)
    keep = sorted({round(i * (n - 1) / (MAX_TICKS - 1)) for i in range(MAX_TICKS)})
    return [at[i] for i in keep], [labels[i] for i in keep]


def empty_slots(extract: Extract) -> list[str]:
    """Labels where no series drawn has a value: slots a line, bar or grouped bar keeps empty."""
    return [lab for i, lab in enumerate(extract.x)
            if all(s.values[i] is None for s in extract.series)]


def render(
    workspace_id: str,
    *,
    kind: str,
    output,
    label: str,
    dataset_name: str | None = None,
    x: str | None = None,
    y: Sequence[str] | None = None,
    title: str | None = None,
    now: datetime | None = None,
    measure: str | None = None,
) -> Chart:
    """Draw one analysis result and describe what was drawn.

    Returns a `Chart`, never a path. The figure is closed in a `finally` because pyplot holds a
    reference to every figure it makes (P11-D6) and this server does not exit between calls.
    """
    if kind not in KINDS:
        raise ChartRefused(
            f"there is no chart called {kind!r}. Available: {', '.join(KINDS)}."
        )
    if not _LABEL_RE.match(label):
        raise ChartRefused(
            f"chart label {label!r} must start with a letter and contain only letters, digits "
            f"and underscores -- it becomes a filename."
        )

    extract = series_from_output(output, x=x, y=y, measure=measure)
    x_name = x if x is not None else str(output.headers[0])

    stamp = (now or datetime.now()).strftime(_STAMP_FORMAT)
    directory = charts_dir(workspace_id)
    path = claim(directory, f"{label}_{stamp}", ".png")   # atomic, as results (D16)

    fig, ax = plt.subplots(figsize=(8.0, 4.5))
    try:
        drawn = _draw(ax, kind, extract)
        ax.set_title(title or f"{label}: {kind}")
        fig.tight_layout()
        fig.savefig(path)
    except BaseException:
        path.unlink(missing_ok=True)                  # the claimed name, never drawn on
        raise
    finally:
        plt.close(fig)

    return Chart(
        path=path,
        kind=kind,
        label=label,
        created_at=now or datetime.now(),
        x_name=x_name,
        series_names=[s.name for s in extract.series],
        point_count=len(extract.x) * len(extract.series),
        drawn_count=drawn,
        key_values=_key_values(extract),
        dataset_name=dataset_name,
        summary=list(getattr(output, "summary", []) or []),
        notes=extract.dropped + (
            [f"{', '.join(empty_slots(extract))} hold(s) no value in any series and "
             f"stay on the axis as an empty slot, not a zero."]
            if kind in ("line", "bar", "grouped_bar") and empty_slots(extract) else []),
    )


__all__ = [
    "CHARTS_DIRNAME", "KINDS", "ChartRefused", "Chart", "Series", "Extract",
    "charts_dir", "as_number", "is_numeric_column", "series_from_output", "render",
]
