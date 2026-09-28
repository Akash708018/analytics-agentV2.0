"""A change in an average, split into what moved and what was reweighted.

**This is the analysis `growth_decomposition` refuses.** P9-D29 gates that one
to additive aggregates, because per-group changes in an average do not sum to
the change in the average. That ruling stands and this does not break it: a
mean is not decomposed into per-group changes in the mean here. It is
decomposed into three different things, and they do sum.

A per-row mean is a weighted sum: M = sum over groups of w(g) * m(g), with w
the group's share of rows and m its own mean. So a change in M has exactly
three sources per group, and their identities are arithmetic rather than
interpretation:

    rate         w0 * (m1 - m0)          the group's own mean moved
    mix          m0 * (w1 - w0)          the group's share of rows moved
    interaction  (w1 - w0) * (m1 - m0)   both moved, at once

**The interaction is reported and never folded away.** Most implementations
have two columns because they fold the cross term into one of the other two,
which is what choosing `w0` or `w1` as the weighting base silently does. The
answer then depends on a choice nobody was told about, and the two choices
disagree by exactly the interaction. Reporting it is cheaper than defending
either.

**Measured, on the fixture this ships with**: every group's mean rises -- 100 to
110 and 10 to 12 -- while the overall mean falls from 82.0 to 31.6. Rate
totals +8.4 and mix totals -54.0. A reader given only the headline sees a
collapse; a reader given only the group means sees improvement everywhere.
Both are true and the mix column is the reason.

**A group present in only one period gets no rate and no mix.** Its share in
the other period is zero and its mean there does not exist, so `w0 * (m1 - m0)`
has no value to take. Imputing the overall mean for the missing side would
manufacture a rate effect out of an arrival. The whole of such a group's
contribution is reported as a contribution and named as an entry or an exit --
the same stance as P9-D30 without the part that does not transfer.

  P9-D49  the decomposition is in floating point, so the parts are checked
          against the whole to a tolerance rather than exactly. Measured
          residual on the fixture: 7.11e-15.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import NO_MEMBER, RECONCILE_TOLERANCE, LostRows, ParamsInvalid, number
from .declared import require_dimension, require_measure
from .registry import Output, register
from .temporal import (
    DEFAULT_GRAIN,
    calendar_for,
    edges,
    per_period_sql,
    require_date_column,
)

__all__ = ["mix_shift"]

NAMED = 12

# growth_decomposition reconciles in Decimal and demands equality. A weighted
# mean is DOUBLE all the way down, so equality is the wrong test: measured, the
# three terms sum to the change with a residual of 7.11e-15 on ten rows.
# Scaled to the size of the change, because an absolute epsilon is wrong at
# both ends of the range.
TOLERANCE = RECONCILE_TOLERANCE  # base.py owns it; growth_decomposition reads it too


@register(
    "mix_shift",
    tier=4,
    summary="A change in one measure's per-row average between two periods, "
            "split across a dimension into the part where group averages "
            "moved, the part where group shares moved, and the interaction "
            "between them, which is reported rather than folded away.",
)
def mix_shift(con, gate, scope, measure: str, dimension: str, period: str,
              baseline: str, grain: str = DEFAULT_GRAIN, **params) -> Output:
    if params:
        raise TypeError(
            f"mix_shift takes measure, dimension, period, baseline and grain; "
            f"got {', '.join(sorted(params))}."
        )

    contract = gate.contract
    # agg is not consulted (P9-D41). The quantity decomposed is the per-row
    # average of the measure, which is avg(column) whatever the contract says
    # about combining it -- and a measure declaring agg='none' is the classic
    # case here, since "average unit price fell" is the question this answers.
    require_measure(contract, measure)
    require_dimension(contract, dimension)
    if period == baseline:
        raise ParamsInvalid(
            f"period and baseline are both {period!r}, so every effect is zero "
            f"by construction. Name two periods."
        )

    date_column = require_date_column(contract)
    cal = calendar_for(gate, scope, date_column, grain)
    key = cal.key
    table = scope.source
    col = quote_identifier(date_column)
    dim = quote_identifier(dimension)
    val = quote_identifier(measure)
    no_member = NO_MEMBER.format(dimension=dimension)

    e = edges(con, cal, table, col, scope.where)
    headers = [dimension, f"mean in {baseline}", f"mean in {period}",
               f"share in {baseline}", f"share in {period}",
               "rate", "mix", "interaction", "contribution"]
    summary = [scope.method_note(), *gate.caveats]

    if e.lo is None or e.hi is None:
        summary.append(
            f"No row in scope has a {date_column}, so there are no periods to "
            f"compare. {e.undated:,} analysed row(s) are undated."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="mix_shift")

    calendar = con.execute(per_period_sql(
        cal, table, col, scope.where,
        inner=[f"avg({val}) AS v", "count(*) AS n"],
        outer=["d.v", "coalesce(d.n, 0)"],
    )).fetchall()
    by_label = {r[0]: r for r in calendar}
    unknown = [lab for lab in (baseline, period) if lab not in by_label]
    if unknown:
        raise ParamsInvalid(
            f"{', '.join(repr(u) for u in unknown)} is not a {key} in this "
            f"calendar. It runs {calendar[0][0]} to {calendar[-1][0]}, "
            f"{len(calendar):,} {key}(s): "
            f"{', '.join(r[0] for r in calendar[:NAMED])}."
        )

    base_row, per_row = by_label[baseline], by_label[period]
    if base_row[1] is None or per_row[1] is None:
        empty = [lab for lab, r in ((baseline, base_row), (period, per_row))
                 if r[1] is None]
        summary.append(
            f"{' and '.join(empty)} hold(s) no rows, so there is no average "
            f"there and no change to split. A {key} with no rows did not "
            f"average zero."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="mix_shift")

    label = cal.label(f"date_trunc('{cal.key}', {col})")
    grouped = con.execute(
        f"SELECT {label} AS p, "
        f"coalesce(CAST({dim} AS VARCHAR), ?) AS member, "
        f"avg({val}) AS m, count(*) AS n "
        f"FROM {table} WHERE {scope.where} AND {col} IS NOT NULL "
        f"AND {val} IS NOT NULL AND {label} IN (?, ?) GROUP BY 1, 2",
        [no_member, baseline, period],
    ).fetchall()

    members: dict[str, dict[str, tuple[float, int]]] = {}
    for p, member, m, n in grouped:
        members.setdefault(member, {})[p] = (float(m), n)

    n0 = sum(v[baseline][1] for v in members.values() if baseline in v)
    n1 = sum(v[period][1] for v in members.values() if period in v)
    m0 = sum(v[baseline][0] * v[baseline][1] for v in members.values()
             if baseline in v) / n0
    m1 = sum(v[period][0] * v[period][1] for v in members.values()
             if period in v) / n1
    change = m1 - m0

    computed: list[tuple] = []
    for member, seen in members.items():
        b, p = seen.get(baseline), seen.get(period)
        w0 = b[1] / n0 if b else 0.0
        w1 = p[1] / n1 if p else 0.0
        if b and p:
            rate = w0 * (p[0] - b[0])
            mix = b[0] * (w1 - w0)
            inter = (w1 - w0) * (p[0] - b[0])
            parts = (rate, mix, inter)
        else:
            # No rate without two means to subtract, and no mix without a mean
            # to reweight. Imputing one would invent an effect out of arrival.
            parts = (None, None, None)
        contribution = (w1 * p[0] if p else 0.0) - (w0 * b[0] if b else 0.0)
        computed.append((member, b, p, w0, w1, parts, contribution))

    residual = abs(sum(r[6] for r in computed) - change)
    if residual > TOLERANCE * max(1.0, abs(change)):
        raise LostRows(
            f"mix_shift does not reconcile: {len(computed)} member(s) of "
            f"{dimension} contribute {sum(r[6] for r in computed):+.6f} "
            f"against a change of {change:+.6f}, a residual of {residual:.3e} "
            f"beyond the tolerance for floating point."
        )

    computed.sort(key=lambda r: (-abs(r[6]), r[0]))
    rows: list[list[Any]] = []
    for member, b, p, w0, w1, parts, contribution in computed:
        rate, mix, inter = parts
        rows.append([
            member,
            number(b[0]) if b else "", number(p[0]) if p else "",
            f"{w0:.3f}" if b else "", f"{w1:.3f}" if p else "",
            f"{rate:+.3f}" if rate is not None else "",
            f"{mix:+.3f}" if mix is not None else "",
            f"{inter:+.3f}" if inter is not None else "",
            f"{contribution:+.3f}",
        ])

    both = [r for r in computed if r[1] and r[2]]
    rate_total = sum(r[5][0] for r in both)
    mix_total = sum(r[5][1] for r in both)
    inter_total = sum(r[5][2] for r in both)

    summary.append(
        f"Mean {measure} per row was {number(m0)} across {n0:,} row(s) in "
        f"{baseline} and {number(m1)} across {n1:,} in {period}: a change of "
        f"{change:+.3f}."
    )
    summary.append(
        f"Of that, {rate_total:+.3f} is rate -- group averages moving -- and "
        f"{mix_total:+.3f} is mix, the groups' shares of the rows moving. The "
        f"interaction, {inter_total:+.3f}, is both at once and is reported "
        f"rather than folded into either: folding it is what choosing "
        f"{baseline} or {period} as the weighting base does silently, and the "
        f"two choices disagree by exactly that number."
    )
    if rate_total and (rate_total > 0) != (change > 0):
        summary.append(
            f"Rate and the total point opposite ways. Every group that was "
            f"present in both {key}s can have moved one way while {measure} "
            f"moved the other, because what changed is which groups the rows "
            f"are in. A reader shown only the group averages would conclude "
            f"the reverse of a reader shown only the headline, and both would "
            f"be reading true numbers."
        )

    entrants = [r[0] for r in computed if not r[1]]
    leavers = [r[0] for r in computed if not r[2]]
    for names, held in ((entrants, period), (leavers, baseline)):
        if names:
            summary.append(
                f"{len(names):,} member(s) appear only in {held}: "
                f"{', '.join(sorted(names)[:NAMED])}. They have no rate and no "
                f"mix above -- there is no second mean to subtract and no "
                f"share to reweight -- so their whole effect sits in the "
                f"contribution column."
            )
    if e.undated:
        summary.append(
            f"{e.undated:,} analysed row(s) have no {date_column} and are in "
            f"neither {key}."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="mix_shift")
