"""Which periods the data covers, and which ones are not there at all.

**Absence cannot be selected from the data.** A GROUP BY over the date column
returns the periods that have rows; a period with no rows has no row of its own
to return, so it is not in the output as a zero -- it is not in the output.
Measured in Step 1 (P9-D1): four months and a NULL group where five months were
in the span. So the periods come off a generated calendar and the table is LEFT
JOINed onto it, and every later Tier 3 analysis reads its periods the same way.

**The calendar is generated from truncated bounds.** P9-D2: a monthly series
started at an observed minimum of 2017-01-31 walks Feb 28, Mar 28, Apr 28 --
February's clamp sticks -- and none of those is a value date_trunc will ever
produce, so every period would read as missing. The bounds are truncated to the
grain first, in SQL, and `generate_series` is used rather than `range` because
it includes the upper bound (P9-D3); `range` drops the last period, which on
Olist is October 2018.

**A window and a span are not the same claim.** P9-D9: `analysis_window` is a
decision somebody made and the observed span is a fact about the data. Both can
bound a calendar and the summary says which one did. Phase 4 already ruled that
the window is reported and never proposed, so this does not invent one when the
contract has none.

**An undated row is not a missing period.** P9-D5: its period is in the window
and the row is in scope. When the contract has no window, `scope_for` leaves
those rows in `analysed` -- there is no window to be outside of -- so they are
counted here and named, and the reconciliation below expects them.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, number, relation_types
from .declared import column_types
from .registry import Output, register
from .temporal import (
    DEFAULT_GRAIN,
    calendar_for,
    edges,
    longest_run,
    per_period_sql,
    require_date_column,
)

__all__ = ["calendar_coverage"]

# How many absent periods are named before the list becomes a count. A reader
# needs the names to go and look; a reader given 300 names has been given a
# second table inside a sentence. The remainder is stated, never dropped.
NAMED_MISSING = 12


@register(
    "calendar_coverage",
    tier=3,
    summary="Which periods of the contract's date column hold rows and which "
            "are absent entirely, at a grain you choose, with the longest "
            "unbroken gap named.",
)
def calendar_coverage(con, gate, scope, grain: str = DEFAULT_GRAIN, **params) -> Output:
    if params:
        raise TypeError(
            f"calendar_coverage takes grain; got {', '.join(sorted(params))}."
        )
    contract = gate.contract
    date_column = require_date_column(contract)
    cal = calendar_for(gate, scope, date_column, grain)
    key = cal.key

    table = scope.source
    col = quote_identifier(date_column)
    # The column's type is the contract's check, not this one's (P9-D10):
    # dataset_contract refuses a text date at confirm time. It is read here
    # only to decide whether the session zone is worth stating (P9-D7).
    dtype = relation_types(con, scope).get(date_column, "")
    tz_aware = "TIME ZONE" in dtype.upper()

    window = getattr(contract, "analysis_window", None)
    bounds_text = cal.bounds_text

    (lo, hi, seen_lo, seen_hi, undated,
     starts_late, ends_early, opens_mid_period) = edges(
        con, cal, table, col, scope.where)

    headers = ["period", "rows"]
    summary = [scope.method_note(), *gate.caveats]

    if lo is None or hi is None:
        summary.append(
            f"No row in scope has a {date_column}, so there is no calendar to "
            f"cover. {undated:,} analysed row(s) are undated."
            if undated
            else "No rows are in scope, so there is no calendar to cover."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="calendar_coverage")

    rows_sql = per_period_sql(
        cal, table, col, scope.where,
        inner=["count(*) AS n"],
        outer=["coalesce(d.n, 0)"],
    )
    fetched = con.execute(rows_sql).fetchall()

    held = sum(r[1] for r in fetched)
    if held + undated != scope.analysed:
        raise LostRows(
            f"calendar_coverage lost rows: its {len(fetched)} {key}(s) hold "
            f"{held:,} row(s) and {undated:,} are undated, against "
            f"{scope.analysed:,} analysed."
        )

    rows: list[list[Any]] = [[r[0], number(r[1])] for r in fetched]
    present = sum(1 for r in fetched if r[1])
    missing = [r[0] for r in fetched if not r[1]]
    longest = longest_run([not r[1] for r in fetched])

    summary.append(
        f"{len(fetched):,} {key}(s) between {fetched[0][0]} and "
        f"{fetched[-1][0]}, generated from {bounds_text} and truncated to the "
        f"{key} -- not read off the rows, which is why an absent {key} appears "
        f"here at all."
    )
    if missing:
        named = ", ".join(missing[:NAMED_MISSING])
        rest = len(missing) - NAMED_MISSING
        summary.append(
            f"{present:,} {key}(s) hold rows and {len(missing):,} hold none: "
            f"{named}"
            + (f", and {rest:,} more." if rest > 0 else ".")
        )
        summary.append(
            f"The longest unbroken gap is {longest:,} {key}(s). A gap is not a "
            f"zero -- nothing says whether the business stopped or the feed "
            f"did, and a trend drawn across these periods interpolates over "
            f"them silently."
        )
    else:
        summary.append(
            f"Every {key} in the range holds at least one row, so a trend over "
            f"this column does not cross an absent period."
        )

    if window is not None:
        if starts_late or ends_early:
            summary.append(
                f"The window and the data do not coincide: {date_column} runs "
                f"{seen_lo} to {seen_hi}, and rows outside the window are out "
                f"of scope rather than absent from it."
            )
        if opens_mid_period:
            summary.append(
                f"The window opens on {window.start.isoformat()}, inside the "
                f"{key} beginning {fetched[0][0]}, so that {key} holds part of "
                f"its period and can read low for that reason alone -- a ramp "
                f"at the start of a chart that is not a movement in the data."
            )
    else:
        summary.append(
            f"The range runs {seen_lo} to {seen_hi}, so the first and last "
            f"{key} hold only part of their period's time. On a chart that is "
            f"a ramp at one end and a cliff at the other, and neither is a "
            f"movement in the data."
        )
    if undated:
        summary.append(
            f"{undated:,} analysed row(s) have no {date_column} and are in no "
            f"{key} above. They are not a missing period: their rows are in "
            f"scope and their period is unknown."
        )
    if tz_aware:
        zone = con.execute("SELECT current_setting('TimeZone')").fetchall()[0][0]
        summary.append(
            f"{date_column} is {dtype}, so its periods were cut at midnight in "
            f"{zone}. The same instant falls in a different {key} under a "
            f"different session zone, so a period here is a period in that "
            f"zone and not in any other."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="calendar_coverage")
