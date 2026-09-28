"""One declared measure, per period, over a calendar that includes its gaps.

**A trend request on a dataset with a missing quarter must produce a gap
warning, not a clean line** -- the build guide's own words, at the Phase 13
gold questions. That is not a check bolted on at the end here: the periods come
from `temporal.per_period_sql`, so an absent period is a row in the table with
no value in it, and the warning is a description of rows that are already
there. A version of this that grouped the table would have nothing to warn
about, because the absent period would not be in its output to notice.

**The direction is two endpoints, not a fit.** The first and last periods that
hold rows, and the difference between them, stated as such. No slope, no
moving average, no percentage unless both ends are non-zero. A line fitted
across the gap this analysis exists to report would be the gap warning's own
counter-example, and `growth_decomposition` is where the arithmetic of change
belongs.

**A split is columns, not rows.** With `dimension`, each member of a declared dimension is a
column beside the periods, then `(all)` -- the unsplit trend, computed from rows rather than
added from the cells, because a mean is not the mean of its members -- then `rows`. Added in
Cleanup Step 8: the bunty_babli run asked for order_value by month split by channel, and no
analysis took a period and a dimension together. A member with no rows in a period is blank,
never zero, for the reason an absent period is. Members are read from dated rows, because an
undated row lands in no period and the undated sentence already says where it went.

**The aggregate is the contract's.** `AGG_SQL[agg]` and nothing else. A measure
declared `agg='none'` is refused rather than averaged: a unit price summed or
averaged per month produces a number nothing downstream can detect as wrong,
which is the reason `Measure.agg` has no default.
"""

from __future__ import annotations

from typing import Any

from ..util.formatting import MAX_COLS
from ..util.sql_guard import quote_identifier
from .base import LostRows, label, number
from .declared import AGG_SQL, agg_of, require_dimension, require_measure
from .registry import Output, register
from .temporal import (
    DEFAULT_GRAIN,
    calendar_for,
    edges,
    longest_run,
    per_period_sql,
    require_date_column,
)

__all__ = ["trend"]

# Absent periods named before the list becomes a count, as calendar_coverage
# names them. A reader needs enough to go and look, not a second table.
NAMED_MISSING = 12
ALL = "(all)"
# The period, (all) and rows columns, then the members, inside the table limit.
MAX_MEMBERS = MAX_COLS - 3


@register(
    "trend",
    tier=3,
    summary="One declared measure per period, over a calendar that includes "
            "the periods holding no rows, with the gaps named and the "
            "direction read from the first and last periods that do; with "
            "dimension, one column per member of a declared dimension.",
)
def trend(con, gate, scope, measure: str, grain: str = DEFAULT_GRAIN,
          dimension: str | None = None, **params) -> Output:
    if params:
        raise TypeError(
            f"trend takes measure, grain and dimension; got "
            f"{', '.join(sorted(params))}."
        )

    contract = gate.contract
    m = require_measure(contract, measure)
    agg = agg_of(m)
    unit = getattr(m, "unit", None) or ""

    if agg in (None, "none"):
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, so it does not combine "
            f"across rows and a value per period would be invented rather than "
            f"computed. Summing or averaging a non-additive measure produces a "
            f"number nothing downstream can detect as wrong. Trend a measure "
            f"that declares how it combines, or ask for frequency instead."
        )
    if agg not in AGG_SQL:
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, which this analysis "
            f"cannot compute. Known: {', '.join(sorted(AGG_SQL))}, none."
        )

    if dimension is not None:
        require_dimension(contract, dimension)

    date_column = require_date_column(contract)
    cal = calendar_for(gate, scope, date_column, grain)
    key = cal.key

    table = scope.source
    col = quote_identifier(date_column)
    value = AGG_SQL[agg].format(col=quote_identifier(measure))

    e = edges(con, cal, table, col, scope.where)
    headers = ["period", f"{measure} ({agg})" + (f" {unit}" if unit else ""),
               "rows"]
    summary = [scope.method_note(), *gate.caveats]

    if e.lo is None or e.hi is None:
        summary.append(
            f"No row in scope has a {date_column}, so there are no periods to "
            f"trend over. {e.undated:,} analysed row(s) are undated."
            if e.undated
            else "No rows are in scope, so there are no periods to trend over."
        )
        return Output(headers=headers, rows=[], summary=summary, label="trend")

    if dimension is not None:
        return _split(con, gate, scope, cal, e, table, col, value, measure, agg,
                      unit, date_column, dimension)

    fetched = con.execute(per_period_sql(
        cal, table, col, scope.where,
        inner=[f"{value} AS v", "count(*) AS n"],
        outer=["d.v", "coalesce(d.n, 0)"],
    )).fetchall()

    held = sum(r[2] for r in fetched)
    if held + e.undated != scope.analysed:
        raise LostRows(
            f"trend lost rows: its {len(fetched)} {key}(s) hold {held:,} "
            f"row(s) and {e.undated:,} are undated, against "
            f"{scope.analysed:,} analysed."
        )

    # An absent period gets a blank, never a zero. coalesce(count, 0) is right
    # and coalesce(sum, 0) is a claim that the period happened and came to
    # nothing, which is the one thing the data does not say.
    rows: list[list[Any]] = [
        [r[0], number(r[1]) if r[1] is not None else "", number(r[2])]
        for r in fetched
    ]
    missing = [r[0] for r in fetched if not r[2]]
    present = [r for r in fetched if r[2]]

    summary.append(
        f"{len(fetched):,} {key}(s) between {fetched[0][0]} and "
        f"{fetched[-1][0]}, generated from {cal.bounds_text} and truncated to "
        f"the {key}."
    )

    if missing:
        named = ", ".join(missing[:NAMED_MISSING])
        rest = len(missing) - NAMED_MISSING
        summary.append(
            f"{len(missing):,} {key}(s) hold no rows and are blank above, not "
            f"zero: {named}" + (f", and {rest:,} more." if rest > 0 else ".")
        )
        summary.append(
            f"The longest unbroken gap is "
            f"{longest_run([not r[2] for r in fetched]):,} {key}(s). A line "
            f"drawn through these points crosses those periods without "
            f"stopping, and nothing here says whether the business paused or "
            f"the feed did."
        )

    if len(present) >= 2:
        first, last = present[0], present[-1]
        summary.append(
            f"From {number(first[1])} in {first[0]} to {number(last[1])} in "
            f"{last[0]}. That is two endpoints and the periods between them, "
            f"not a fitted line -- with {len(missing):,} {key}(s) absent, a fit "
            f"would be interpolating over them."
            if missing else
            f"From {number(first[1])} in {first[0]} to {number(last[1])} in "
            f"{last[0]}. That is two endpoints, not a fitted line."
        )
    elif len(present) == 1:
        summary.append(
            f"Only {present[0][0]} holds rows, so there is no direction to "
            f"read."
        )

    if cal.windowed and e.opens_mid_period:
        summary.append(
            f"The window opens on {cal.start}, inside the {key} beginning "
            f"{fetched[0][0]}, so that {key} covers part of its period and can "
            f"read low for that reason alone."
        )
    if e.undated:
        summary.append(
            f"{e.undated:,} analysed row(s) have no {date_column} and are in "
            f"no {key} above."
        )
    return Output(headers=headers, rows=rows, summary=summary, label="trend")


def _split(con, gate, scope, cal, e, table, col, value, measure, agg, unit,
           date_column, dimension) -> Output:
    """trend with one column per member of `dimension`. See the module docstring."""
    key = cal.key
    d = quote_identifier(dimension)
    members = [r[0] for r in con.execute(
        f"SELECT DISTINCT {d} FROM {table} WHERE {scope.where} AND {col} IS NOT NULL "
        f"ORDER BY 1 NULLS LAST"
    ).fetchall()]
    if len(members) > MAX_MEMBERS:
        raise ValueError(
            f"trend of {measure} split by {dimension} would have {len(members)} member "
            f"column(s), NULL counted as one. It is capped at {MAX_MEMBERS}, so that with "
            f"period, {ALL} and rows it fits the {MAX_COLS}-column table limit. top_n on "
            f"{dimension} says which members matter; a coarser declared dimension is the "
            f"other way."
        )
    names = [label(m) for m in members]
    clash = sorted({n for n in names if names.count(n) > 1 or n in ("period", ALL, "rows")})
    if clash:
        raise ValueError(
            f"trend cannot label its columns: {clash} would collide with each other or with "
            f"the period, {ALL} and rows columns."
        )

    inner = [f"{value} FILTER (WHERE {d} IS NOT DISTINCT FROM ?) AS v{i}"
             for i in range(len(members))]
    inner += [f"{value} AS v", "count(*) AS n"]
    outer = [f"d.v{i}" for i in range(len(members))] + ["d.v", "coalesce(d.n, 0)"]
    fetched = con.execute(
        per_period_sql(cal, table, col, scope.where, inner=inner, outer=outer),
        list(members),
    ).fetchall()

    held = sum(r[-1] for r in fetched)
    if held + e.undated != scope.analysed:
        raise LostRows(
            f"trend lost rows: its {len(fetched)} {key}(s) hold {held:,} row(s) and "
            f"{e.undated:,} are undated, against {scope.analysed:,} analysed."
        )

    rows: list[list[Any]] = [
        [r[0]] + [number(v) if v is not None else "" for v in r[1:-1]] + [number(r[-1])]
        for r in fetched
    ]
    headers = ["period", *names, ALL, "rows"]
    summary = [scope.method_note(), *gate.caveats]
    summary.append(
        f"{len(fetched):,} {key}(s) between {fetched[0][0]} and {fetched[-1][0]}, generated "
        f"from {cal.bounds_text} and truncated to the {key}. Each member of {dimension} is a "
        f"column of {agg} of {measure}{f' ({unit})' if unit else ''}; {ALL} is {agg} over "
        f"every row in the {key}, not a combination of the cells."
    )
    summary.append(
        f"A blank cell is not zero: no analysed row of that member, with a value of "
        f"{measure}, falls in that {key}."
    )
    missing = [r[0] for r in fetched if not r[-1]]
    if missing:
        named = ", ".join(missing[:NAMED_MISSING])
        rest = len(missing) - NAMED_MISSING
        summary.append(
            f"{len(missing):,} {key}(s) hold no rows for any member and are blank across the "
            f"row: {named}" + (f", and {rest:,} more." if rest > 0 else ".")
        )
    for i, name in enumerate(names):
        present = [(r[0], r[1 + i]) for r in fetched if r[1 + i] is not None]
        if len(present) >= 2:
            summary.append(
                f"{name}: from {number(present[0][1])} in {present[0][0]} to "
                f"{number(present[-1][1])} in {present[-1][0]}, two endpoints and not a fit."
            )
        elif present:
            summary.append(f"{name}: only {present[0][0]} holds a value.")
    if cal.windowed and e.opens_mid_period:
        summary.append(
            f"The window opens on {cal.start}, inside the {key} beginning {fetched[0][0]}, so "
            f"that {key} covers part of its period and can read low for that reason alone."
        )
    if e.undated:
        summary.append(
            f"{e.undated:,} analysed row(s) have no {date_column} and are in no {key} above."
        )
    return Output(headers=headers, rows=rows, summary=summary, label="trend")
