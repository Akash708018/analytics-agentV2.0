"""A change between two named periods, split across a declared dimension.

Three analyses deferred their arithmetic here and this is where the debt comes
due. `trend` reports two endpoints and refuses a fit, saying the arithmetic of
change belongs to `growth_decomposition`. `seasonality` reports a ratio of
means and refuses a decomposition, naming the same place. `period_compare`
takes the difference between two periods and stops. What is left for this
module is the only question those three left open: *which parts of the
dimension moved, and by how much of the whole*.

**Only an aggregate that adds can be decomposed.** A sum splits across members
because the members' sums add back to it; a mean does not, and neither does a
median, a minimum, a maximum or a distinct count. A "decomposition" of a change
in an average into per-member changes in that average produces numbers that do
not add to the thing they claim to explain, and nothing downstream can detect
that. So P9-D27's additive set decides who may be decomposed at all, not merely
who hears a caveat about it.

**The parts are checked against the whole rather than assumed to match it.**
The two period totals are computed independently of the per-member values, and
the members' contributions must sum to the difference between them exactly. A
decomposition whose parts do not add to its whole is not a decomposition, so
the mismatch raises rather than being reported.

**A member missing from one period is a zero here, and that is not a
contradiction of P9-D1.** An absent *period* has no value because the calendar
says that time existed and the data does not cover it. An absent *member* in a
period that does hold rows is a different claim: the time is covered, and the
member did not appear in it. That is a real zero and it has to be one, or the
contributions do not sum. Such members are named as entrants and leavers rather
than left to look like ordinary movements.

**The total not moving is a result, not an absence of one.** When two periods
come to the same number, the members underneath have usually not stayed still,
and a headline of zero is exactly what hides that. Gross movement is reported
beside net movement in every case, and a share above 100% or below zero is
reported without apology: it means a member moved further than the total did,
which is what offsetting looks like from the inside.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import NO_MEMBER, RECONCILE_TOLERANCE, LostRows, ParamsInvalid, number
from .declared import ADDITIVE_AGGS, AGG_SQL, agg_of, require_dimension, require_measure
from .registry import Output, register
from .temporal import (
    DEFAULT_GRAIN,
    calendar_for,
    edges,
    empty_months,
    months_sentence,
    per_period_sql,
    require_date_column,
)

__all__ = ["growth_decomposition"]

# P9-D27's set, and the reason it is a refusal here rather than a caveat: these
# two add across groups, so their changes add across members. The other five do
# not. If declared.py or stats.py already exports this, import it -- P9-O6.
ADDITIVE = frozenset(ADDITIVE_AGGS)  # P9-O6: declared.py owns this

# Labels shown when a period nobody has is refused, as period_compare shows
# them, and members named in a summary sentence before the list becomes a count.
NAMED = 12


@register(
    "growth_decomposition",
    tier=3,
    summary="The change in one declared measure between two named periods, "
            "split across the members of a declared dimension, with the "
            "contributions checked to sum to the whole change and entrants "
            "and leavers named.",
)
def growth_decomposition(con, gate, scope, measure: str, dimension: str,
                         period: str, baseline: str,
                         grain: str = DEFAULT_GRAIN, **params) -> Output:
    if params:
        raise TypeError(
            f"growth_decomposition takes measure, dimension, period, baseline "
            f"and grain; got {', '.join(sorted(params))}."
        )

    contract = gate.contract
    m = require_measure(contract, measure)
    agg = agg_of(m)
    unit = getattr(m, "unit", None) or ""

    if agg not in ADDITIVE:
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, which does not add "
            f"across groups, so a change in it does not split across the "
            f"members of a dimension. Per-member changes in an average do not "
            f"sum to the change in the average, and a table of them would look "
            f"exactly like one that did. Decompose a measure declaring "
            f"{' or '.join(sorted(ADDITIVE))}, or use period_compare, which "
            f"compares {agg!r} between two periods without splitting it."
        )

    require_dimension(contract, dimension)
    if period == baseline:
        raise ParamsInvalid(
            f"period and baseline are both {period!r}, so every contribution "
            f"is zero by construction. Name two periods."
        )

    date_column = require_date_column(contract)
    cal = calendar_for(gate, scope, date_column, grain)
    key = cal.key

    table = scope.source
    col = quote_identifier(date_column)
    dim = quote_identifier(dimension)
    value = AGG_SQL[agg].format(col=quote_identifier(measure))
    no_member = NO_MEMBER.format(dimension=dimension)

    e = edges(con, cal, table, col, scope.where)
    headers = [dimension,
               f"{measure} in {baseline}" + (f" ({unit})" if unit else ""),
               f"{measure} in {period}" + (f" ({unit})" if unit else ""),
               "change", "share of change"]
    summary = [scope.method_note(), *gate.caveats]

    if e.lo is None or e.hi is None:
        summary.append(
            f"No row in scope has a {date_column}, so there are no periods to "
            f"decompose between. {e.undated:,} analysed row(s) are undated."
            if e.undated
            else "No rows are in scope, so there is no change to decompose."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="growth_decomposition")

    # The calendar first, for the same two reasons period_compare needs it: a
    # label nobody has is refused against the vocabulary that exists, and a
    # period's length is a property of the calendar (P9-D27).
    calendar = con.execute(per_period_sql(
        cal, table, col, scope.where,
        inner=[f"{value} AS v", "count(*) AS n"],
        outer=["d.v", "coalesce(d.n, 0)",
               f"date_diff('day', s.period, s.period + {cal.step})"],
    )).fetchall()
    by_label = {r[0]: r for r in calendar}

    unknown = [lab for lab in (baseline, period) if lab not in by_label]
    if unknown:
        vocabulary = ", ".join(r[0] for r in calendar[:NAMED])
        more = len(calendar) - NAMED
        raise ParamsInvalid(
            f"{', '.join(repr(u) for u in unknown)} is not a {key} in this "
            f"calendar. It runs {calendar[0][0]} to {calendar[-1][0]}, "
            f"{len(calendar):,} {key}(s), labelled: {vocabulary}"
            + (f", and {more:,} more." if more > 0 else ".")
        )

    base_row, per_row = by_label[baseline], by_label[period]
    summary.append(
        f"Both {key}s were taken from the calendar generated from "
        f"{cal.bounds_text}, so a {key} holding no rows is known to hold none "
        f"rather than assumed to."
    )

    if base_row[1] is None or per_row[1] is None:
        empty = [lab for lab, r in ((baseline, base_row), (period, per_row))
                 if r[1] is None]
        summary.append(
            f"{' and '.join(empty)} hold(s) no rows, so there is no change to "
            f"decompose. A {key} with no rows did not fall to zero, and "
            f"splitting a change that does not exist across members would "
            f"give every member a contribution to nothing."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="growth_decomposition")

    # baseline, period and the no-member label are bound rather than
    # interpolated. All three are safe by the time they get here -- the two
    # labels only reach this line after matching a label DuckDB itself
    # generated, and the third is built from a declared dimension -- but that
    # is safety by execution order, and an edit that moves the membership check
    # would remove it silently. quote_identifier covers identifiers; this is
    # the first analysis in the tier with a value to pass.
    label = cal.label(f"date_trunc('{cal.key}', {col})")
    grouped = con.execute(
        f"SELECT {label} AS p, "
        f"coalesce(CAST({dim} AS VARCHAR), ?) AS member, "
        f"{value} AS v, count(*) AS n "
        f"FROM {table} WHERE {scope.where} AND {col} IS NOT NULL "
        f"AND {label} IN (?, ?) "
        f"GROUP BY 1, 2",
        [no_member, baseline, period],
    ).fetchall()

    members: dict[str, list[Any]] = {}
    for p, member, v, n in grouped:
        slot = members.setdefault(member, [None, None, 0, 0])
        if p == baseline:
            slot[0], slot[2] = v, n
        else:
            slot[1], slot[3] = v, n

    base_total, per_total = base_row[1], per_row[1]
    change = per_total - base_total
    zero = base_total - base_total  # typed zero: DECIMAL or int, never a float

    contributions = {
        member: (slot[1] if slot[1] is not None else zero)
        - (slot[0] if slot[0] is not None else zero)
        for member, slot in members.items()
    }
    # Compared against a tolerance rather than exactly, and the difference is not cosmetic:
    # exact equality holds for a DECIMAL or integer measure and fails for every DOUBLE one, so
    # this refused any float measure outright. It also printed both sides through number(),
    # which rounds to four places, so the refusal showed two identical numbers and called them
    # unequal. mix_shift already compared this way; this is the same ruling, now in one place.
    total = sum(contributions.values(), zero)
    residual = abs(float(total) - float(change))
    if residual > RECONCILE_TOLERANCE * max(1.0, abs(float(change))):
        raise LostRows(
            f"growth_decomposition does not reconcile: the "
            f"{len(contributions)} member(s) of {dimension} contribute "
            f"{float(total):+.6f} against a change of {float(change):+.6f} "
            f"between {baseline} and {period}, a residual of {residual:.3e} "
            f"beyond the tolerance for floating point. Parts that do not add "
            f"to the whole are not a decomposition of it."
        )

    order = sorted(contributions, key=lambda k: (-abs(contributions[k]), k))
    rows: list[list[Any]] = []
    for member in order:
        slot = members[member]
        contribution = contributions[member]
        share = ("" if change == zero
                 else f"{float(contribution) / float(change) * 100:+.1f}%")
        rows.append([
            member,
            number(slot[0]) if slot[0] is not None else "",
            number(slot[1]) if slot[1] is not None else "",
            number(contribution), share,
        ])

    direction = ("unchanged" if change == zero
                 else "up" if change > zero else "down")
    line = (
        f"{measure} ({agg}) was {number(base_total)} in {baseline} and "
        f"{number(per_total)} in {period}: {direction}"
        + ("." if change == zero else f" by {number(abs(change))}.")
    )
    if change != zero and base_total > zero:
        line += (f" That is {float(change) / float(base_total) * 100:+.1f}% of "
                 f"the baseline.")
    summary.append(line)

    gross = sum((abs(c) for c in contributions.values()), zero)
    rose = sum(1 for c in contributions.values() if c > zero)
    fell = sum(1 for c in contributions.values() if c < zero)
    summary.append(
        f"{len(contributions):,} member(s) of {dimension} account for that "
        f"change exactly -- their contributions sum to {number(change)}, "
        f"checked against the two period totals rather than assumed. "
        f"{rose:,} rose, {fell:,} fell, and gross movement is "
        f"{number(gross)} against a net {number(change)}."
    )
    if change == zero:
        summary.append(
            f"The total did not move, so there are no shares to take -- a "
            f"share of a change of zero is not a number. The contributions "
            f"stand: {number(gross)} of movement cancelled out, which a "
            f"headline of nought is exactly what hides."
        )
    elif rose and fell:
        # Directions, not magnitudes (Cleanup Step 16, 2.2): members moved against each other
        # exactly when some rose and some fell. `gross > abs(change)` compared two float sums whose
        # last bits depend on the order DuckDB's threads add them in, and the retail re-run said
        # this on one run and not the next with all five categories rising.
        summary.append(
            "Gross movement exceeds the net change, so members moved against "
            "each other and a share above 100% or below zero is a member that "
            "moved further than the total did."
        )

    entrants = [k for k, s in members.items() if s[0] is None]
    leavers = [k for k, s in members.items() if s[1] is None]
    for names, gone, held in ((entrants, baseline, period),
                              (leavers, period, baseline)):
        if names:
            shown = ", ".join(sorted(names)[:NAMED])
            rest = len(names) - NAMED
            summary.append(
                f"{len(names):,} member(s) appear in {held} and not in {gone}: "
                f"{shown}" + (f", and {rest:,} more." if rest > 0 else ".")
                + f" Their {gone} value is blank above and counted as zero in "
                f"the arithmetic, which is what a member that did not appear "
                f"in a {key} the data covers actually is."
            )

    for lab in (baseline, period):
        said = months_sentence(lab, empty_months(con, cal, table, col, scope.where, lab),
                               agg in ADDITIVE)
        if said:
            summary.append(said)
    if base_row[3] != per_row[3]:
        summary.append(
            f"{baseline} covers {base_row[3]:,} calendar days and {period} covers "
            f"{per_row[3]:,}, so this {agg} sets {base_row[3]:,} calendar days "
            f"against {per_row[3]:,}. Every contribution above carries that "
            f"same {(per_row[3] - base_row[3]) / base_row[3] * 100:+.1f}% of "
            f"length with it."
        )
    if no_member in members:
        summary.append(
            f"{no_member} is a member here: those rows are in both periods' "
            f"totals and their {dimension} is unknown, which is a fact about "
            f"the rows rather than a gap in the calendar."
        )
    if e.undated:
        summary.append(
            f"{e.undated:,} analysed row(s) have no {date_column} and are in "
            f"neither {key}."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="growth_decomposition")
