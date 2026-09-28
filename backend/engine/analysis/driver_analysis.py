"""Which declared dimensions account for a measure's variation, and by how much.

**A raw share of variance ranks the id column first.** Measured on the fixture:
`id` accounts for 1.000 of the variation in `spend` -- nine groups over nine
rows, every row its own group, all of it explained and none of it learned. Any
ranking by raw share puts the primary key at the top and the useful dimension
below it, which is worse than no ranking at all because it looks like a result.

**So the share is ranked against what chance would give.** A grouping into g
groups over n rows explains, on random data, about (g-1)/(n-1) of the variance
by arithmetic alone. That baseline is computed per dimension and subtracted,
and the table is ordered by the excess. Measured: `id` scores 1.000 against a
baseline of 1.000 for an excess of exactly 0.000, and `region` scores 0.995
against 0.375 for an excess of 0.620. `id` explains everything and adds
nothing, which is what an excess of zero means.

**A negative excess is reported as a negative number.** A dimension can explain
less than its own shape would explain by chance -- measured, `size` scores
0.073 against a baseline of 0.125. Clamping that to zero would hide the most
useful thing the row says, which is that grouping by it tells you nothing.

**Nothing is called a driver.** The tool is named `driver_analysis` because the
build guide names it that; the output says a dimension *accounts for* variation
and never that it *drives* it. Every column here is association. A dimension
that accounts for most of the variance in revenue may be caused by revenue,
may share a cause with it, or may be a relabelling of it.

The rulings this carries, measured on DuckDB 1.5.5 in Step 5c:

  P9-D45  the share of variance is reported beside the share chance would
          give, (g-1)/(n-1), and the ranking is on the difference.
  P9-D46  a measure that does not vary has a total sum of squares of exactly
          0.0, so every share is undefined rather than zero.
  P9-D47  a NULL in a dimension is a group, as it is in growth_decomposition.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import NO_MEMBER, LostRows, ParamsInvalid, number, relation_types
from .declared import require_measure
from .registry import Output, register

__all__ = ["driver_analysis"]

# A dimension at or above this share of rows-as-groups is reported with its
# numbers but named as arithmetic rather than as a finding. At 1.0 every row is
# its own group; well below that the excess already collapses on its own, so
# this is a labelling threshold and not a filter -- nothing is dropped.
GRANULAR = 0.5


@register(
    "driver_analysis",
    tier=4,
    summary="Every declared dimension ranked by how much of one measure's "
            "variation it accounts for, against the share a grouping of the "
            "same shape would account for by chance, with nothing called a "
            "driver.",
)
def driver_analysis(con, gate, scope, measure: str, **params) -> Output:
    if params:
        raise TypeError(
            f"driver_analysis takes measure; got {', '.join(sorted(params))}."
        )

    contract = gate.contract
    # agg is not consulted, for the reason correlation does not consult it
    # (P9-D41): the sums of squares are over raw values and combine nothing
    # the contract has a view on.
    require_measure(contract, measure)
    dimensions = list(getattr(contract, "dimensions", []) or [])
    if scope.unit_text:
        # A per-unit measure (Cleanup Step 15): only dimensions constant within its unit are in the
        # relation; the rest are named in the scope's own sentence.
        present = set(relation_types(con, scope))
        dimensions = [d for d in dimensions if d in present]
    if not dimensions:
        raise ParamsInvalid(
            f"{contract.dataset_name} declares no dimensions, so there is "
            f"nothing to group {measure} by. confirm_dataset_contract with "
            f"dimensions set is what fixes that."
        )

    table = scope.source
    y = quote_identifier(measure)
    headers = ["dimension", "groups", "rows", "share of variance",
               "chance share", "excess", "one-row groups"]
    summary = [scope.method_note(), *gate.caveats]

    total = con.execute(
        f"WITH p AS (SELECT {y} AS y FROM {table} WHERE {scope.where} "
        f"AND {y} IS NOT NULL), t AS (SELECT avg(y) AS gm FROM p) "
        f"SELECT (SELECT count(*) FROM p), "
        f"(SELECT sum((p.y - t.gm) * (p.y - t.gm)) FROM p, t)"
    ).fetchall()[0]
    rows_with_value, sst = total
    dropped = scope.analysed - rows_with_value
    if rows_with_value + dropped != scope.analysed:
        raise LostRows(
            f"driver_analysis lost rows: {rows_with_value:,} with a {measure} "
            f"and {dropped:,} without, against {scope.analysed:,} analysed."
        )

    summary.append(
        f"{rows_with_value:,} of {scope.analysed:,} analysed row(s) hold a "
        f"{measure}; {dropped:,} do not and are in no group below."
    )

    if not sst or rows_with_value < 2:
        summary.append(
            f"{measure} does not vary across the rows that have one, so its "
            f"total sum of squares is exactly 0.0 and every share would be a "
            f"division by it. There is no variation for any dimension to "
            f"account for -- which is a finding about {measure}, not a failure "
            f"to compute one."
            if rows_with_value else
            f"No row in scope holds a {measure}, so there is nothing to "
            f"account for."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="driver_analysis")

    measured: list[tuple] = []
    for dimension in dimensions:
        dim = quote_identifier(dimension)
        no_member = NO_MEMBER.format(dimension=dimension)
        groups, ssb, singles = con.execute(
            f"WITH p AS (SELECT coalesce(CAST({dim} AS VARCHAR), ?) AS g, "
            f"{y} AS y FROM {table} WHERE {scope.where} AND {y} IS NOT NULL), "
            f"t AS (SELECT avg(y) AS gm FROM p), "
            f"k AS (SELECT g, avg(y) AS m, count(*) AS n FROM p GROUP BY g) "
            f"SELECT (SELECT count(*) FROM k), "
            f"(SELECT sum(k.n * (k.m - t.gm) * (k.m - t.gm)) FROM k, t), "
            f"(SELECT count(*) FROM k WHERE n = 1)",
            [no_member],
        ).fetchall()[0]
        share = float(ssb) / float(sst)
        # What a grouping of this shape explains on data with no structure in
        # it: g-1 degrees of freedom out of n-1. Subtracting it is the whole
        # difference between a ranking and a restatement of cardinality.
        chance = (groups - 1) / (rows_with_value - 1)
        measured.append((dimension, groups, share, chance, share - chance,
                         singles))

    measured.sort(key=lambda r: -r[4])
    rows: list[list[Any]] = [
        [dimension, number(groups), number(rows_with_value),
         f"{share:.3f}", f"{chance:.3f}", f"{excess:+.3f}", number(singles)]
        for dimension, groups, share, chance, excess, singles in measured
    ]

    best = measured[0]
    summary.append(
        f"{best[0]} accounts for the most: {best[2]:.3f} of the variation in "
        f"{measure} against {best[3]:.3f} from its shape alone, an excess of "
        f"{best[4]:+.3f} across {best[1]:,} group(s). Accounts for, not "
        f"drives -- every number here is association. A dimension that "
        f"accounts for most of the variation in {measure} may be caused by it, "
        f"may share a cause with it, or may be another name for it."
    )
    summary.append(
        f"The chance column is what a grouping into that many groups accounts "
        f"for on data with no structure at all, (groups - 1) over "
        f"({rows_with_value:,} - 1). Without it the ranking is a ranking of "
        f"how many distinct values each dimension has, and the primary key "
        f"wins every time."
    )

    granular = [r for r in measured if r[1] >= GRANULAR * rows_with_value]
    if granular:
        summary.append(
            f"{', '.join(r[0] for r in granular)} put(s) at least half the "
            f"rows in a group of their own, so a high share there is "
            f"arithmetic rather than a finding: enough groups explain "
            f"everything whatever the data says. The excess column is where "
            f"that shows -- it collapses toward zero as the groups approach "
            f"the rows."
        )
    below = [r for r in measured if r[4] < 0]
    if below:
        summary.append(
            f"{', '.join(r[0] for r in below)} account(s) for less than a "
            f"grouping of the same shape would by chance, reported as a "
            f"negative excess rather than clamped to zero. Grouping {measure} "
            f"by them tells you less than nothing about it."
        )
    singles = [r for r in measured if r[5]]
    if singles:
        summary.append(
            f"{', '.join(f'{r[0]} ({r[5]:,})' for r in singles)}: group(s) "
            f"holding one row. A one-row group has no variation of its own and "
            f"sits exactly on its own mean, so it adds to the share without "
            f"telling anybody anything."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="driver_analysis")
