"""One declared measure in one period, against the same measure in another.

**Both periods are named by the caller.** No "latest against previous" default,
because the latest period is the one most likely to be partial and a default
would pick it silently -- a month three days old against a whole month is a
fall that did not happen. Phase 4's rule that a window is reported and never
proposed, one tier down: the comparison is a decision, and the caller makes it.

**The periods are looked up in the generated calendar, not in the table.** P9-D12
again. A label the calendar does not contain is refused and names the range; a
label the calendar contains but the table has no rows for is reported as
holding no rows, and no change is computed from it. Those are different
answers, and only a generated calendar can tell them apart -- a GROUP BY has
one response to both.

**A change is not computed off an absent period.** A period with no rows did
not fall to zero, so there is no difference to take and no percentage to
report. The two values stand in the table and the summary says why the third
number is missing.

**Two periods are not the same length.** February is 28 days and March is 31,
and a sum over one against a sum over the other is 28 days of data against 31
before anything in the business moved. The difference in length is stated
whenever the aggregate adds -- Step 8a's ruling about which aggregates add
across groups, applied to two periods as two groups.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, ParamsInvalid, number
from .declared import ADDITIVE_AGGS, AGG_SQL, agg_of, require_measure
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

__all__ = ["period_compare"]

# Step 8a ruled which aggregates add across groups: sum and count do, and
# count_distinct, mean, median, min and max do not. Two periods are two groups,
# so the ruling holds here unchanged.
# If declared.py already exports that set, this should import it rather than
# restate it -- P9-O6.
ADDITIVE = frozenset(ADDITIVE_AGGS)  # P9-O6: declared.py owns this

# Labels shown when a period nobody has is refused, before the list becomes a
# count. Long enough to recognise the vocabulary, short enough not to be a
# second table inside a sentence.
NAMED = 12


@register(
    "period_compare",
    tier=3,
    summary="One declared measure in two named periods, with the difference "
            "between them, the rows and the days behind each, and no change "
            "computed from a period that holds no rows.",
)
def period_compare(con, gate, scope, measure: str, period: str, baseline: str,
                   grain: str = DEFAULT_GRAIN, **params) -> Output:
    if params:
        raise TypeError(
            f"period_compare takes measure, period, baseline and grain; got "
            f"{', '.join(sorted(params))}."
        )

    contract = gate.contract
    m = require_measure(contract, measure)
    agg = agg_of(m)
    unit = getattr(m, "unit", None) or ""

    if agg in (None, "none"):
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, so it does not combine "
            f"across rows and neither period would have a value to compare. "
            f"Summing or averaging a non-additive measure produces a number "
            f"nothing downstream can detect as wrong. Compare a measure that "
            f"declares how it combines."
        )
    if agg not in AGG_SQL:
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, which this analysis "
            f"cannot compute. Known: {', '.join(sorted(AGG_SQL))}, none."
        )
    if period == baseline:
        raise ParamsInvalid(
            f"period and baseline are both {period!r}, so the comparison is a "
            f"period against itself and every difference is zero by "
            f"construction. Name two periods."
        )

    date_column = require_date_column(contract)
    cal = calendar_for(gate, scope, date_column, grain)
    key = cal.key

    table = scope.source
    col = quote_identifier(date_column)
    value = AGG_SQL[agg].format(col=quote_identifier(measure))

    e = edges(con, cal, table, col, scope.where)
    headers = ["period", f"{measure} ({agg})" + (f" {unit}" if unit else ""),
               "rows", "days"]
    summary = [scope.method_note(), *gate.caveats]

    if e.lo is None or e.hi is None:
        summary.append(
            f"No row in scope has a {date_column}, so there are no periods to "
            f"compare. {e.undated:,} analysed row(s) are undated."
            if e.undated
            else "No rows are in scope, so there are no periods to compare."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="period_compare")

    # The days column is selected against s.period, which per_period_sql leaves
    # in scope for exactly this: the length of a period is a property of the
    # calendar and not of the rows that landed in it.
    fetched = con.execute(per_period_sql(
        cal, table, col, scope.where,
        inner=[f"{value} AS v", "count(*) AS n"],
        outer=["d.v", "coalesce(d.n, 0)",
               f"date_diff('day', s.period, s.period + {cal.step})"],
    )).fetchall()

    held = sum(r[2] for r in fetched)
    if held + e.undated != scope.analysed:
        raise LostRows(
            f"period_compare lost rows: its {len(fetched)} {key}(s) hold "
            f"{held:,} row(s) and {e.undated:,} are undated, against "
            f"{scope.analysed:,} analysed."
        )

    by_label = {r[0]: r for r in fetched}
    unknown = [lab for lab in (baseline, period) if lab not in by_label]
    if unknown:
        vocabulary = ", ".join(r[0] for r in fetched[:NAMED])
        more = len(fetched) - NAMED
        raise ParamsInvalid(
            f"{', '.join(repr(u) for u in unknown)} is not a {key} in this "
            f"calendar. It runs {fetched[0][0]} to {fetched[-1][0]}, "
            f"{len(fetched):,} {key}(s), labelled: {vocabulary}"
            + (f", and {more:,} more." if more > 0 else ".")
        )

    pair = [r for r in fetched if r[0] in (baseline, period)]
    rows: list[list[Any]] = [
        [r[0], number(r[1]) if r[1] is not None else "", number(r[2]),
         number(r[3])]
        for r in pair
    ]

    base_row, per_row = by_label[baseline], by_label[period]
    base_v, per_v = base_row[1], per_row[1]

    summary.append(
        f"Both {key}s were taken from the calendar generated from "
        f"{cal.bounds_text}, which is why a {key} holding no rows is a row "
        f"here rather than a lookup that found nothing."
    )

    empty = [lab for lab, val in ((baseline, base_v), (period, per_v))
             if val is None]
    if empty:
        summary.append(
            f"{' and '.join(empty)} hold(s) no rows, so there is no difference "
            f"to take. A {key} with no rows did not fall to zero -- nothing "
            f"here says whether the business paused or the feed did, and a "
            f"change computed against it would be a number about neither."
        )
    else:
        delta = per_v - base_v
        direction = ("unchanged" if delta == 0
                     else "up" if delta > 0 else "down")
        line = (
            f"{measure} ({agg}) was {number(base_v)} in {baseline} and "
            f"{number(per_v)} in {period}: {direction}"
            + ("." if delta == 0 else f" by {number(abs(delta))}.")
        )
        if delta != 0 and base_v > 0:
            line += (f" That is {float(delta) / float(base_v) * 100:+.1f}% of "
                     f"the baseline.")
        elif delta != 0:
            line += (f" No percentage: a change measured against a baseline of "
                     f"{number(base_v)} is not a ratio anybody can read.")
        summary.append(line)

        for lab in (baseline, period):
            said = months_sentence(lab, empty_months(con, cal, table, col, scope.where, lab),
                                   agg in ADDITIVE)
            if said:
                summary.append(said)
        if agg in ADDITIVE and base_row[3] != per_row[3]:
            summary.append(
                f"{baseline} covers {base_row[3]:,} calendar days and {period} covers "
                f"{per_row[3]:,}, so this {agg} sets {base_row[3]:,} calendar days "
                f"against {per_row[3]:,}. The lengths alone differ by "
                f"{(per_row[3] - base_row[3]) / base_row[3] * 100:+.1f}%, "
                f"before anything in the business moved."
            )

    first, last = fetched[0][0], fetched[-1][0]
    partial = []
    if first in (baseline, period) and (not cal.windowed or e.opens_mid_period):
        partial.append(f"{first} is the first {key} of the calendar and holds "
                       f"only the part of its time from {e.seen_lo} onward")
    if last in (baseline, period) and (not cal.windowed or e.ends_early):
        partial.append(f"{last} is the last {key} of the calendar and holds "
                       f"only the part of its time up to {e.seen_hi}")
    if partial:
        summary.append(
            f"{'; '.join(partial)}. A part of a {key} set against a whole one "
            f"reads low for that reason alone."
        )

    if e.undated:
        summary.append(
            f"{e.undated:,} analysed row(s) have no {date_column} and are in "
            f"neither {key}."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="period_compare")
