"""Who came back, arranged by when they first arrived.

A cohort grid asks whether people who started in March behave like people who started in June.
It is the right question when enough people return to make the rows mean something, and the wrong
shape entirely when they do not -- which is why this checks the repeat rate before it draws
anything, and says so rather than rendering 676 cells of zero.

**The cells are people, not percentages.** A percentage of three people is noise wearing a rate's
clothes. The cohort's size sits beside its label so a reader can see what each row rests on.

**No cohort is suppressed and no cohort is flagged.** Any cutoff would be a number nobody
measured -- the objection that ruled out a threshold-driven test selector in Step 3. The size
column is the honest version of the same information.

**The key is checked before anything else.** Step 1 Part D measured Olist's customer_id at 99,441
distinct across 99,441 orders: keyed on it, every cohort has exactly one member and retention is
0.000% forever. A clean grid of zeros reads like a finding and is a fact about the column.
"""

from __future__ import annotations

from typing import Any

from ..util.formatting import MAX_COLS
from ..util.sql_guard import quote_identifier
from .base import LostRows, label, number
from .declared import require_dimension
from .registry import Output, register

PERIODS = ("month", "week", "quarter")
# The guide's threshold, not one invented here: below this a retention grid is mostly empty and
# repeat_behaviour says the same thing in four rows.
LOW_REPEAT = 0.05
# Headers are the cohort label, its size, then one column per offset.
MAX_OFFSETS = MAX_COLS - 2


@register(
    "cohort_retention",
    tier=7,
    summary="How many people from each starting period came back in each later one, as a grid "
            "of people rather than percentages, with the cohort size beside its label. Warns "
            "and points at repeat_behaviour when too few people return for the shape to mean "
            "anything, and refuses a key that is distinct per row.",
)
def cohort_retention(con, gate, scope, entity: str, period: str = "month",
                     **params) -> Output:
    if params:
        raise TypeError(
            f"cohort_retention takes entity and period; got {', '.join(sorted(params))}."
        )
    if period not in PERIODS:
        raise ValueError(f"period={period!r} is not one of {', '.join(PERIODS)}.")
    require_dimension(gate.contract, entity)
    date_column = getattr(gate.contract, "date_column", None)
    if not date_column:
        raise ValueError(
            "cohort_retention needs a date column on the contract: a cohort is a group of "
            "people who started at the same time, and without one nobody started."
        )

    summary = [scope.method_note(), *gate.caveats]
    if not scope.analysed:
        return Output(headers=["cohort", "size"], rows=[], label="cohort_retention",
                      summary=summary + ["No rows are in scope, so no cohort has formed."])

    table = scope.source
    key = quote_identifier(entity)
    dt = quote_identifier(date_column)
    usable = f"{scope.where} AND {key} IS NOT NULL AND {dt} IS NOT NULL"

    events, people = con.execute(
        f"SELECT count(*), count(DISTINCT {key}) FROM {table} WHERE {usable}"
    ).fetchall()[0]
    if events and people == events:
        raise ValueError(
            f"cohort_retention cannot use {entity!r}: it holds {people:,} distinct value(s) "
            f"across {events:,} row(s), one per row. Every cohort would have exactly one "
            f"member and retention would be zero in every cell — a fact about the column, not "
            f"about anybody coming back. Name the column that identifies a person across their "
            f"events."
        )


    spans = con.execute(
        f"SELECT count(*) FROM (SELECT {key} FROM {table} WHERE {usable} GROUP BY 1 "
        f"HAVING count(DISTINCT {dt}) > 1)").fetchone()[0]
    if events and not spans:
        # Cleanup Step 16, 2.1: an order id repeats across its lines but every line carries the
        # order's one timestamp, so it passes the one-per-row check and describes events anyway --
        # the re-run reported 59,948 of 150,000 orders "came back".
        raise ValueError(
            f"cohort_retention cannot use {entity!r}: none of its {people:,} value(s) appears at more "
            f"than one moment -- every row of a value shares one {date_column}, so it names an "
            f"event (an order of several lines), not someone who came back. Name the column that "
            f"identifies a person across their events."
        )

    no_entity, no_date = con.execute(
        f"SELECT count(*) FILTER (WHERE {key} IS NULL), "
        f"       count(*) FILTER (WHERE {key} IS NOT NULL AND {dt} IS NULL) "
        f"FROM {table} WHERE {scope.where}"
    ).fetchall()[0]
    if events + no_entity + no_date != scope.analysed:
        raise LostRows(
            f"cohort_retention lost rows: {events:,} usable, {no_entity:,} with no {entity}, "
            f"{no_date:,} with no {date_column}, against {scope.analysed:,} in scope."
        )

    grid = con.execute(
        f"WITH firsts AS ("
        f"  SELECT {key} AS k, min(date_trunc('{period}', {dt}))::DATE AS cohort "
        f"  FROM {table} WHERE {usable} GROUP BY 1), "
        f"activity AS ("
        f"  SELECT DISTINCT {key} AS k, date_trunc('{period}', {dt})::DATE AS p "
        f"  FROM {table} WHERE {usable}) "
        f"SELECT f.cohort, date_diff('{period}', f.cohort, a.p) AS offset, "
        f"       count(DISTINCT a.k) AS people "
        f"FROM firsts f JOIN activity a USING (k) GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()

    sizes = {c: n for c, o, n in grid if o == 0}
    if sum(sizes.values()) != people:
        raise LostRows(
            f"cohort_retention lost people: cohorts hold {sum(sizes.values()):,} at their own "
            f"start against {people:,} distinct {entity} value(s). P10-D52: a grid cannot add "
            f"back to rows, but every person belongs to exactly one cohort."
        )

    # Coming back is a second moment, not a second row: two lines of one order share a timestamp.
    repeaters = spans
    rate = repeaters / people if people else 0.0

    widest = max((o for _, o, _ in grid), default=0)
    shown = min(widest, MAX_OFFSETS)
    cohorts = sorted(sizes)
    cells = {(c, o): n for c, o, n in grid}

    headers = ["cohort", "size"] + [f"+{i}" for i in range(shown + 1)]
    rows: list[list[Any]] = [
        [label(c), number(sizes[c])] + [
            number(cells[(c, i)]) if (c, i) in cells else None for i in range(shown + 1)
        ]
        for c in cohorts
    ]
    for name, count in (("(no entity)", no_entity), ("(no date)", no_date)):
        if count:
            rows.append([name, None] + [None] * (shown + 1))

    summary.append(
        f"{len(cohorts):,} {period}ly cohort(s) covering {people:,} distinct {entity} value(s) "
        f"across {events:,} event(s), from {label(cohorts[0])} to {label(cohorts[-1])}."
    )
    summary.append(
        f"Cells are people, not percentages: the count in a cohort's +{1} column is how many of "
        f"its members were active one {period} later. The size column is what each row rests on, "
        f"and it is there instead of a minimum-size rule, because any cutoff would be a number "
        f"nobody measured."
    )
    if rate < LOW_REPEAT:
        summary.append(
            f"Repeat rate {rate:.3%}, below the {LOW_REPEAT:.0%} at which this shape stops "
            f"saying anything: {repeaters:,} of {people:,} came back at all, so almost every "
            f"cell beyond +0 is empty. repeat_behaviour on {entity!r} reports the same fact in "
            f"four rows without a grid implying a curve that is not there."
        )
    else:
        summary.append(
            f"Repeat rate {rate:.3%}: {repeaters:,} of {people:,} came back at least once, "
            f"which is enough for the rows to be worth comparing."
        )
    if widest > shown:
        summary.append(
            f"The span runs to +{widest} but the table stops at +{shown}, which is the "
            f"{MAX_COLS}-column cap less the cohort label and its size. The later columns exist "
            f"and are not shown."
        )
    summary.append(
        f"{no_entity:,} row(s) have no {entity} and {no_date:,} have no {date_column}; both are "
        f"listed so the event counts add back to the {scope.analysed:,} in scope. A person "
        f"occupies several rows, so the grid itself counts people and does not reconcile to a "
        f"row count."
    )
    return Output(headers=headers, rows=rows, summary=summary, label="cohort_retention")
