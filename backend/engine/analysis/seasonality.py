"""One declared measure folded onto the positions of a repeating cycle.

**The fold is over the calendar, not over the table.** The periods come from
`temporal.per_period_sql` exactly as they do for `trend`, and this wraps that
query whole rather than re-deriving it, so an absent period arrives here as a
row with a NULL value in it. That matters twice: `avg` over the joined calendar
leaves the NULL out of the mean rather than treating it as a zero, and
`count(*)` against `count(v)` is the difference between the periods a position
covers and the periods it actually rests on. A version that grouped the table
would have neither number.

**A position is the mean of its periods.** Every cycle weighs the same, because
the question is what a typical January looks like and not what every January
adds to. Pooling the rows instead would weight the year that happened to be
busy, which is the trend leaking into the season.

**The index is a ratio of means and nothing more.** A position's mean over the
mean of every period that holds rows, so 1.00 is an average period. No
decomposition, no fit: `growth_decomposition` is where change is split, and
`changepoint` is where a break is found.

The rulings this carries, measured on DuckDB 1.5.5 in Step 4d:

  P9-D22  avg() over DECIMAL(38,2) returns DOUBLE where sum() stays
          DECIMAL(38,2), so a position mean does not carry the measure's
          storage type the way a per-period value does.
  P9-D23  the position is read off the period's own label, so the calendar is
          wrapped rather than rebuilt and there is one place where a period's
          identity is decided.
  P9-D24  a year is refused a cycle, because this calendar has nothing above
          one for a year to repeat inside.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, ParamsInvalid, number
from .declared import AGG_SQL, agg_of, require_measure
from .registry import Output, register
from .temporal import (
    DEFAULT_GRAIN,
    calendar_for,
    edges,
    per_period_sql,
    require_date_column,
)

__all__ = ["seasonality"]

# Positions named before the list becomes a count, as the other two name them.
NAMED = 12

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# The cycle a grain repeats inside, how many positions that cycle has, the SQL
# that reads a period's position off its own label (P9-D23), and how the
# position is named. A week is keyed 1-53 rather than by a name because it has
# none, and 53 exists in some years and not others -- which is the imbalance
# the summary already reports rather than a separate rule.
CYCLES: dict[str, tuple[str, int, str, Any]] = {
    "day": ("week", 7, "isodow(CAST(t.label AS DATE))",
            lambda i: _DAYS[i - 1]),
    "week": ("year", 53, "week(CAST(t.label AS DATE))",
             lambda i: f"W{i:02d}"),
    "month": ("year", 12, "CAST(substr(t.label, 6, 2) AS INTEGER)",
              lambda i: _MONTHS[i - 1]),
    "quarter": ("year", 4, "CAST(right(t.label, 1) AS INTEGER)",
                lambda i: f"Q{i}"),
}


@register(
    "seasonality",
    tier=3,
    summary="One declared measure folded onto the positions of its cycle -- "
            "months onto the year, days onto the week -- with each position's "
            "mean taken over the periods that hold rows and the periods it "
            "lost to a gap counted beside it.",
)
def seasonality(con, gate, scope, measure: str, grain: str = DEFAULT_GRAIN,
                **params) -> Output:
    if params:
        raise TypeError(
            f"seasonality takes measure and grain; got "
            f"{', '.join(sorted(params))}."
        )

    contract = gate.contract
    m = require_measure(contract, measure)
    agg = agg_of(m)
    unit = getattr(m, "unit", None) or ""

    if agg in (None, "none"):
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, so it does not combine "
            f"across rows and a value per period would be invented before it "
            f"was ever averaged across cycles. Summing or averaging a "
            f"non-additive measure produces a number nothing downstream can "
            f"detect as wrong. Fold a measure that declares how it combines."
        )
    if agg not in AGG_SQL:
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, which this analysis "
            f"cannot compute. Known: {', '.join(sorted(AGG_SQL))}, none."
        )

    date_column = require_date_column(contract)
    cal = calendar_for(gate, scope, date_column, grain)
    key = cal.key
    if key not in CYCLES:
        raise ParamsInvalid(
            f"grain={key!r} has no cycle above it in this calendar, so there "
            f"is nothing for a {key} to repeat inside. Seasonality is "
            f"variation that comes back: a day inside a week, a month inside "
            f"a year. Ask for trend at this grain instead."
        )
    cycle, length, pos_sql, name_of = CYCLES[key]

    table = scope.source
    col = quote_identifier(date_column)
    value = AGG_SQL[agg].format(col=quote_identifier(measure))

    e = edges(con, cal, table, col, scope.where)
    headers = ["position",
               f"mean {measure} per {key}" + (f" ({unit})" if unit else ""),
               "index", f"{key}s observed", f"{key}s absent", "rows"]
    summary = [scope.method_note(), *gate.caveats]

    if e.lo is None or e.hi is None:
        summary.append(
            f"No row in scope has a {date_column}, so there are no periods to "
            f"fold. {e.undated:,} analysed row(s) are undated."
            if e.undated
            else "No rows are in scope, so there are no periods to fold."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="seasonality")

    periods = per_period_sql(
        cal, table, col, scope.where,
        inner=[f"{value} AS v", "count(*) AS n"],
        outer=["d.v", "coalesce(d.n, 0)"],
    )
    # count(v) is the periods holding rows and count(*) the periods the
    # position covers; the difference is what the gaps took. avg(v) skips the
    # NULLs, which is the absent period declining to be a zero.
    fetched = con.execute(
        f"WITH q AS (SELECT {pos_sql} AS pos, t.v AS v, t.n AS n "
        f"FROM ({periods}) t(label, v, n)), "
        f"g AS (SELECT avg(v) AS grand FROM q) "
        f"SELECT q.pos, avg(q.v), count(q.v), count(*), sum(q.n), "
        f"any_value(g.grand) FROM q, g GROUP BY q.pos ORDER BY q.pos"
    ).fetchall()

    held = sum(r[4] for r in fetched)
    if held + e.undated != scope.analysed:
        raise LostRows(
            f"seasonality lost rows: its {len(fetched)} position(s) hold "
            f"{held:,} row(s) and {e.undated:,} are undated, against "
            f"{scope.analysed:,} analysed."
        )

    grand = fetched[0][5] if fetched else None
    rows: list[list[Any]] = []
    for pos, mean, obs, slots, n, _ in fetched:
        index = ""
        if mean is not None and grand:
            index = f"{float(mean) / float(grand):.2f}"
        rows.append([name_of(pos), number(mean) if mean is not None else "",
                     index, number(obs), number(slots - obs), number(n)])

    covered = sum(r[3] for r in fetched)
    observed = [r[2] for r in fetched]
    starved = [name_of(r[0]) for r in fetched if r[3] - r[2]]
    holding = [r for r in fetched if r[2]]

    summary.append(
        f"{covered:,} {key}(s) from {cal.bounds_text}, folded onto the "
        f"{len(fetched)} position(s) of the {cycle} the calendar reaches."
        + (f" A {cycle} has {length}; the other "
           f"{length - len(fetched)} are outside this window entirely and are "
           f"not rows above." if len(fetched) < length else "")
    )
    summary.append(
        f"A position is the mean of its own {key}s, so every {cycle} weighs "
        f"the same however many rows it held. A {key} with no rows is left "
        f"out of that mean rather than entering it as a zero."
    )
    if starved:
        named = ", ".join(starved[:NAMED])
        rest = len(starved) - NAMED
        summary.append(
            f"{len(starved):,} position(s) lost at least one {key} to a gap: "
            f"{named}" + (f", and {rest:,} more." if rest > 0 else ".")
            + " The count each position rests on is beside it."
        )
    if observed and min(r[3] for r in fetched) != max(r[3] for r in fetched):
        summary.append(
            f"Positions cover between {min(r[3] for r in fetched):,} and "
            f"{max(r[3] for r in fetched):,} {key}(s), because the window is "
            f"not a whole number of {cycle}s. A comparison across positions is "
            f"then partly a comparison across {cycle}s, and a movement over "
            f"time can read here as a season."
        )
    if observed and max(observed) < 2:
        summary.append(
            f"No position rests on two {key}s, so nothing here separates a "
            f"season from a trend -- the column is the series re-labelled. "
            f"Two full {cycle}s is the least that distinguishes them."
        )
    if len(holding) == 1:
        summary.append(
            f"Only {name_of(holding[0][0])} holds rows, so its index is 1.00 "
            f"against itself and says nothing about a {cycle}."
        )
    elif grand:
        summary.append(
            f"The index is a position's mean over {number(grand)}, the mean of "
            f"every {key} that holds rows, so 1.00 is an average {key}. It is "
            f"a ratio of means and not a decomposition: growth_decomposition "
            f"is where a change is split into its parts."
        )
    else:
        summary.append(
            f"The mean of every {key} holding rows is {grand}, so an index "
            f"would be divided by it and the column is blank."
        )

    if cal.windowed and e.opens_mid_period:
        summary.append(
            f"The window opens on {cal.start}, inside a {key} it does not "
            f"cover whole, so that {key} can read low for that reason alone "
            f"and it is one of the {key}s behind its position's mean."
        )
    if e.undated:
        summary.append(
            f"{e.undated:,} analysed row(s) have no {date_column} and are in "
            f"no position above."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="seasonality")
