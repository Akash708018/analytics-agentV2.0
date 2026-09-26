"""Two measures breaking at the same time, and how often that happens anyway.

The last analysis in Phase 9, and the one the Done-When names. The build guide
calls it the signature move: the Mandi Tamil Nadu detection, ported and
generalised. What it does is narrow -- find where each of two series changes
level, and say whether those two places are the same place -- and almost all
of the work is in not overstating the answer.

**Coincidence is not correlation, and the chance rate is reported beside every
verdict.** Two series that break in the same month look like a finding. With
nine admissible split points and a tolerance of one period either side, two
*unrelated* series land within tolerance of each other about a third of the
time by arithmetic alone. That number is computed and printed next to the
verdict, in the same spirit as `driver_analysis` printing what a grouping of
the same shape explains by chance: a coincidence is only evidence to the extent
it is unlikely, and the reader cannot judge that without the denominator.

**The tolerance is one period and it is not a parameter.** A knob here would be
tuned until two series coincided -- run it at one, then two, then three, and
something always lines up. Fixing it makes the chance rate above a fixed
property of the calendar rather than something the caller chose after seeing
the data.

**Both of Tier 5's rulings carry over.** A split with a gap beside it is
excluded, not caveated (P9-D56), and here a gap in *either* series blocks that
split in *both* -- the two are compared period by period and a period missing
from one is missing from the comparison. And a break is judged against
within-segment spread rather than against its runner-up (P9-D58), reusing
`changepoint`'s own statistic rather than a second copy of it.

**Two breaks at the same period is one observation.** The analysis says the two
best splits coincide, how unlikely that is, and nothing else. It does not say
one series moved the other, and it does not say a third thing moved both --
which is the explanation it cannot rule out and the one most often true.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, ParamsInvalid, number
from .changepoint import MIN_SEGMENT, SEPARATION, within
from .declared import AGG_SQL, agg_of, require_measure
from .registry import Output, register
from .temporal import (
    DEFAULT_GRAIN,
    calendar_for,
    edges,
    per_period_sql,
    require_date_column,
)

__all__ = ["correlated_shift"]

# Periods either side within which two breaks count as the same break. A
# constant rather than a parameter: a tolerance the caller can raise is a
# tolerance that gets raised until something coincides.
TOLERANCE = 1


def _best(series: list[tuple]) -> tuple | None:
    """The largest admissible level shift in one series, or None.

    Admissible on `changepoint`'s terms: MIN_SEGMENT periods holding rows on
    each side, and no absent period flanking the split (P9-D56).
    """
    found = None
    for i in range(len(series) - 1):
        before = [r for r in series[:i + 1] if r[2]]
        after = [r for r in series[i + 1:] if r[2]]
        if len(before) < MIN_SEGMENT or len(after) < MIN_SEGMENT:
            continue
        if not series[i][2] or not series[i + 1][2]:
            continue
        mb = sum(float(r[1]) for r in before) / len(before)
        ma = sum(float(r[1]) for r in after) / len(after)
        if found is None or abs(ma - mb) > abs(found[1]):
            found = (series[i][0], ma - mb, i)
    return found


def _admissible(series: list[tuple]) -> int:
    """How many split points survive the same two rules. The denominator."""
    count = 0
    for i in range(len(series) - 1):
        if len([r for r in series[:i + 1] if r[2]]) < MIN_SEGMENT:
            continue
        if len([r for r in series[i + 1:] if r[2]]) < MIN_SEGMENT:
            continue
        if not series[i][2] or not series[i + 1][2]:
            continue
        count += 1
    return count


@register(
    "correlated_shift",
    tier=5,
    summary="Whether two declared measures change level at the same point in "
            "the calendar, with the rate at which unrelated series coincide "
            "by chance reported beside the verdict.",
)
def correlated_shift(con, gate, scope, measure: str, against: str,
                     grain: str = DEFAULT_GRAIN, **params) -> Output:
    if params:
        raise TypeError(
            f"correlated_shift takes measure, against and grain; got "
            f"{', '.join(sorted(params))}."
        )

    contract = gate.contract
    pair = []
    for name in (measure, against):
        m = require_measure(contract, name)
        agg = agg_of(m)
        if agg in (None, "none") or agg not in AGG_SQL:
            raise ValueError(
                f"measure {name!r} declares agg={agg!r}, so it has no value "
                f"per period and no level to shift. Known: "
                f"{', '.join(sorted(AGG_SQL))}."
            )
        pair.append((name, AGG_SQL[agg].format(col=quote_identifier(name))))
    if measure == against:
        raise ParamsInvalid(
            f"measure and against are both {measure!r}. A series breaks where "
            f"it breaks, so the two would coincide by construction."
        )

    date_column = require_date_column(contract)
    cal = calendar_for(gate, scope, date_column, grain)
    key = cal.key
    table = scope.source
    col = quote_identifier(date_column)
    x, y = quote_identifier(measure), quote_identifier(against)

    e = edges(con, cal, table, col, scope.where)
    headers = ["series", "breaks after", "difference", "separation",
               f"admissible {key}s"]
    summary = [scope.method_note(), *gate.caveats]

    if e.lo is None or e.hi is None:
        summary.append(
            f"No row in scope has a {date_column}, so there are no series to "
            f"compare."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="correlated_shift")

    # A period counts only where both measures have a value. A period present
    # in one series and absent from the other is absent from the comparison,
    # which is P9-D56 applied to two series instead of one.
    both = f"{x} IS NOT NULL AND {y} IS NOT NULL"
    fetched = con.execute(per_period_sql(
        cal, table, col, scope.where,
        inner=[f"{pair[0][1]} FILTER (WHERE {both}) AS a",
               f"{pair[1][1]} FILTER (WHERE {both}) AS b",
               f"count(*) FILTER (WHERE {both}) AS n",
               "count(*) AS total"],
        outer=["d.a", "d.b", "coalesce(d.n, 0)", "coalesce(d.total, 0)"],
    )).fetchall()

    held = sum(r[4] for r in fetched)
    if held + e.undated != scope.analysed:
        raise LostRows(
            f"correlated_shift lost rows: its {len(fetched)} {key}(s) hold "
            f"{held:,} row(s) and {e.undated:,} are undated, against "
            f"{scope.analysed:,} analysed."
        )

    left = [(r[0], r[1], r[3]) for r in fetched]
    right = [(r[0], r[2], r[3]) for r in fetched]
    paired = sum(r[3] for r in fetched)
    absent = [r[0] for r in fetched if not r[3]]

    summary.append(
        f"{len(fetched):,} {key}(s) between {fetched[0][0]} and "
        f"{fetched[-1][0]}, of which {len(fetched) - len(absent):,} hold "
        f"both {measure} and {against} together across {paired:,} row(s). A "
        f"{key} holding one of the two and not the other is in neither series."
    )

    best = {name: _best(s) for name, s in ((measure, left), (against, right))}
    candidates = _admissible(left)
    rows: list[list[Any]] = []
    for name, series in ((measure, left), (against, right)):
        found = best[name]
        if found is None:
            rows.append([name, "", "", "", number(candidates)])
            continue
        spread = within(series, found[0])
        rows.append([
            name, found[0],
            f"{found[1]:+,.4f}".rstrip("0").rstrip("."),
            "no within-segment variation" if spread is None
            else f"{abs(found[1]) / spread:,.1f}x",
            number(candidates),
        ])

    missing = [n for n, f in best.items() if f is None]
    if missing:
        summary.append(
            f"{' and '.join(missing)} "
            f"{'has' if len(missing) == 1 else 'have'} no admissible split: a "
            f"split needs "
            f"{MIN_SEGMENT} {key}(s) holding rows on each side and no absent "
            f"{key} beside it. With one series unbroken there is nothing to "
            f"compare, and no verdict is offered."
        )
        return Output(headers=headers, rows=rows, summary=summary,
                      label="correlated_shift")

    a, b = best[measure], best[against]
    apart = abs(a[2] - b[2])
    window = 2 * TOLERANCE + 1
    chance = min(1.0, window / candidates) if candidates else 1.0

    if apart <= TOLERANCE:
        summary.append(
            f"Both break in the same place: {measure} after {a[0]} and "
            f"{against} after {b[0]}"
            + (f", the same {key}." if apart == 0
               else f", {apart:,} {key} apart and within the tolerance of "
                    f"{TOLERANCE}.")
        )
        summary.append(
            f"Two unrelated series land this close about {chance * 100:.0f}% "
            f"of the time: there are {candidates:,} admissible split(s) and a "
            f"window of {window:,}. That is the number this coincidence has to "
            f"beat to be worth anything, and it is printed here because a "
            f"coincidence is only evidence to the extent it is unlikely."
        )
    else:
        summary.append(
            f"They do not break in the same place: {measure} after {a[0]} and "
            f"{against} after {b[0]}, {apart:,} {key}(s) apart against a "
            f"tolerance of {TOLERANCE}. Unrelated series coincide about "
            f"{chance * 100:.0f}% of the time here, so not coinciding is the "
            f"ordinary outcome and says little on its own."
        )

    weak = [name for name, found in best.items()
            if (lambda s: s is not None and abs(found[1]) < SEPARATION * s)(
                within(left if name == measure else right, found[0]))]
    if weak:
        summary.append(
            f"{' and '.join(weak)}: the break is smaller than "
            f"{SEPARATION:g} times the variation inside its own segments, so "
            f"it is the largest split in that series rather than a level "
            f"shift in it. Two such breaks coinciding is two pieces of "
            f"arithmetic agreeing."
        )
    if absent:
        summary.append(
            f"{len(absent):,} {key}(s) hold no pair and are named here because "
            f"they narrow the comparison: {', '.join(absent[:12])}"
            f"{', and more' if len(absent) > 12 else ''}. Splits beside them "
            f"are excluded from both series, which is why the admissible count "
            f"is {candidates:,} and not {len(fetched) - 1:,}."
        )
    summary.append(
        f"Nothing here says {measure} moved {against}. Two series breaking "
        f"together is one observation of a coincidence, and the explanation "
        f"this analysis cannot rule out -- a third thing moving both -- is the "
        f"one most often true."
    )
    if e.undated:
        summary.append(
            f"{e.undated:,} analysed row(s) have no {date_column} and are in "
            f"no {key} above."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="correlated_shift")
