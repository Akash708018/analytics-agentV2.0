"""Where a measure changes level, and where a gap makes that unanswerable.

**This is the analysis Step 4c owed a different answer.** That step ruled a gap
is a caveat and never fatal -- the reader decides whether a half-empty window
is worth reading -- and said in as many words that `changepoint` may want
otherwise. It does. A level shift detected across an absent period is a
description of the absence: the series steps because it stops, and no arithmetic
on the periods either side can tell that apart from the business stepping. So a
candidate split flanked by a period holding no rows is not caveated here, it is
**excluded**, and if every candidate is excluded the analysis reports no
changepoint at all and says the gaps are why.

**Every series has a best split, which is why the runner-up is reported.** Take
any twelve numbers and one of the eleven splits will separate them better than
the other ten. Reporting that one as "the changepoint" manufactures a finding
out of arithmetic. So every admissible split is reported with its own
difference, ordered, and the summary names the gap between the best and the
second best: a series with one real break has a clear leader, and a series
with none has eleven candidates that look alike.

**No significance is claimed.** There is no p-value here and no test. `scipy`
arrives in Phase 10 and a test would need assumptions about the distribution
that nothing in the contract states. What is reported is the size of each
split's difference and how many periods stand behind each side.

**A split needs room on both sides.** One period against eleven is not a level
shift, it is the last period. Both sides must hold at least MIN_SEGMENT periods
with rows, and the splits that fail that are not in the table.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, number
from .declared import AGG_SQL, agg_of, require_measure
from .registry import Output, register
from .temporal import (
    DEFAULT_GRAIN,
    calendar_for,
    edges,
    longest_run,
    per_period_sql,
    require_date_column,
)

# P9-O12: correlated_shift reads three of these. They are exported rather than reached for
# privately, because the alternative is a second copy of the same statistic in the other
# module, and P9-O6 records what two copies of one ruling do. A name another module depends
# on is part of this module's surface whether or not it is spelled that way.
__all__ = ["changepoint", "within", "MIN_SEGMENT", "SEPARATION"]

# Periods each side of a split before it is a level shift rather than an
# endpoint. Three is the fewest that can have a level at all.
MIN_SEGMENT = 3

# How many within-segment standard deviations the best split's difference must
# span before it is described as standing out. Comparing it to the runner-up
# does not work: adjacent splits share all but one period, so on a perfect step
# function the top two differ by little and the break is still perfect.
# Measured on the fixture: a clean step of 40 has no within-segment variation
# at all, and a noisy series with no break separates by 0.47.
SEPARATION = 2.0


def within(series, split_label: str) -> float | None:
    """Pooled standard deviation inside the two segments a split creates.

    None when neither segment varies at all, which is not zero spread to divide
    by but an absence of anything to compare the difference against.
    """
    index = next(i for i, r in enumerate(series) if r[0] == split_label)
    halves = ([float(r[1]) for r in series[:index + 1] if r[2]],
              [float(r[1]) for r in series[index + 1:] if r[2]])
    residual = 0.0
    for half in halves:
        mean = sum(half) / len(half)
        residual += sum((v - mean) ** 2 for v in half)
    dof = sum(len(h) for h in halves) - 2
    if dof <= 0 or not residual:
        return None
    return (residual / dof) ** 0.5


@register(
    "changepoint",
    tier=5,
    summary="Where one declared measure changes level across a calendar, with "
            "every admissible split reported rather than only the best, and "
            "splits a gap could explain excluded rather than caveated.",
)
def changepoint(con, gate, scope, measure: str, grain: str = DEFAULT_GRAIN,
                **params) -> Output:
    if params:
        raise TypeError(
            f"changepoint takes measure and grain; got "
            f"{', '.join(sorted(params))}."
        )

    contract = gate.contract
    m = require_measure(contract, measure)
    agg = agg_of(m)
    if agg in (None, "none") or agg not in AGG_SQL:
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, so it has no value per "
            f"period and there is no level for a level shift to be in. "
            f"Known: {', '.join(sorted(AGG_SQL))}."
        )

    date_column = require_date_column(contract)
    cal = calendar_for(gate, scope, date_column, grain)
    key = cal.key
    table = scope.source
    col = quote_identifier(date_column)
    value = AGG_SQL[agg].format(col=quote_identifier(measure))

    e = edges(con, cal, table, col, scope.where)
    headers = ["split after", f"{key}s before", f"{key}s after",
               "mean before", "mean after", "difference"]
    summary = [scope.method_note(), *gate.caveats]

    if e.lo is None or e.hi is None:
        summary.append(
            f"No row in scope has a {date_column}, so there is no series to "
            f"look for a level shift in."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="changepoint")

    series = con.execute(per_period_sql(
        cal, table, col, scope.where,
        inner=[f"{value} AS v", "count(*) AS n"],
        outer=["d.v", "coalesce(d.n, 0)"],
    )).fetchall()

    held = sum(r[2] for r in series)
    if held + e.undated != scope.analysed:
        raise LostRows(
            f"changepoint lost rows: its {len(series)} {key}(s) hold {held:,} "
            f"row(s) and {e.undated:,} are undated, against "
            f"{scope.analysed:,} analysed."
        )

    absent = [r[0] for r in series if not r[2]]
    summary.append(
        f"{len(series):,} {key}(s) between {series[0][0]} and "
        f"{series[-1][0]}, generated from {cal.bounds_text}, of which "
        f"{len(series) - len(absent):,} hold rows."
    )

    # Every split, then the ones a gap could account for taken back out. The
    # order matters: a reader is told how many candidates existed before the
    # exclusion, not only how many survived it.
    candidates: list[tuple] = []
    blocked: list[str] = []
    for i in range(len(series) - 1):
        before = [r for r in series[:i + 1] if r[2]]
        after = [r for r in series[i + 1:] if r[2]]
        if len(before) < MIN_SEGMENT or len(after) < MIN_SEGMENT:
            continue
        if not series[i][2] or not series[i + 1][2]:
            blocked.append(series[i][0])
            continue
        mb = sum(float(r[1]) for r in before) / len(before)
        ma = sum(float(r[1]) for r in after) / len(after)
        candidates.append((series[i][0], len(before), len(after), mb, ma,
                           ma - mb))

    if absent:
        summary.append(
            f"{len(absent):,} {key}(s) hold no rows -- {', '.join(absent[:12])}"
            f"{' and more' if len(absent) > 12 else ''} -- and the longest "
            f"unbroken run of them is {longest_run([not r[2] for r in series]):,}. "
            f"A level shift detected across an absent {key} is a description of "
            f"the absence: the series steps because it stopped, and nothing in "
            f"these periods distinguishes that from the business stepping."
        )
    if blocked:
        summary.append(
            f"{len(blocked):,} split(s) are excluded rather than caveated "
            f"because a {key} holding no rows sits on one side of them: "
            f"{', '.join(blocked[:12])}"
            f"{', and more' if len(blocked) > 12 else ''}. Step 4c ruled that "
            f"a gap is a caveat and never fatal, and named this analysis as "
            f"the one that may need otherwise. It does -- here the gap is not "
            f"a caveat on the answer, it is the reason there is no answer at "
            f"that split."
        )

    if not candidates:
        summary.append(
            f"No admissible split remains, so no changepoint is reported. A "
            f"split needs {MIN_SEGMENT} {key}(s) holding rows on each side and "
            f"no absent {key} beside it."
            + (f" {len(blocked):,} candidate(s) were excluded by gaps alone."
               if blocked else "")
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="changepoint")

    ranked = sorted(candidates, key=lambda r: -abs(r[5]))
    rows: list[list[Any]] = [
        [label, number(nb), number(na), f"{mb:,.4f}".rstrip("0").rstrip("."),
         f"{ma:,.4f}".rstrip("0").rstrip("."),
         f"{diff:+,.4f}".rstrip("0").rstrip(".")]
        for label, nb, na, mb, ma, diff in ranked
    ]

    best = ranked[0]
    summary.append(
        f"The largest difference in level falls after {best[0]}: "
        f"{best[3]:,.4f} across {best[1]:,} {key}(s) before and {best[4]:,.4f} "
        f"across {best[2]:,} after, a change of {best[5]:+,.4f}."
    )

    # Against the variation inside the two segments, not against the runner-up.
    # Adjacent splits share all but one period, so on a perfect step the second
    # best is always close behind and a runner-up test calls a clean break
    # unclear. Measured: a step of 40 with no noise has the top two at +40 and
    # +34.29, which no margin rule separates from a series with no break at all.
    spread = within(series, best[0])
    if spread is None:
        summary.append(
            "Neither side varies within itself at all, so the difference is "
            "the whole of what the series does. Every series has a best split "
            "-- any twelve numbers divide better one way than another -- and "
            "what makes this one a break rather than arithmetic is that "
            "nothing else in either segment moves."
        )
    elif abs(best[5]) >= SEPARATION * spread:
        summary.append(
            f"The difference spans {abs(best[5]) / spread:,.1f} times the "
            f"variation inside the two segments ({spread:,.4f}), so it is "
            f"larger than the series' own movement rather than the largest "
            f"piece of it. Every series has a best split; this one is not "
            f"merely the best available."
        )
    else:
        summary.append(
            f"The difference spans only {abs(best[5]) / spread:,.1f} times the "
            f"variation inside the two segments ({spread:,.4f}), so the best "
            f"split is the best of a set that all look alike rather than a "
            f"break. Every series has a best split, and the whole table is "
            f"above so that this is visible instead of hidden behind one "
            f"answer."
        )
    summary.append(
        f"No significance is claimed for any of this. There is no test here "
        f"and no p-value: a test needs assumptions about how {measure} is "
        f"distributed that nothing in the contract states. What is reported is "
        f"the size of each difference and how many {key}(s) stand behind each "
        f"side of it."
    )
    if e.undated:
        summary.append(
            f"{e.undated:,} analysed row(s) have no {date_column} and are in "
            f"no {key} above."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="changepoint")
