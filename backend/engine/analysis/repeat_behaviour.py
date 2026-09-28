"""How many people came back, how often, and how long they took.

The reframe for a dataset where retention is not the question. A cohort grid over Olist is 26
columns of near-zero: 2,997 repeat events spread across 676 cells, which is a sparse table shaped
like a retention curve. This says the same thing in four rows and does not pretend to a shape the
data has not got.

**It keys on the person, and which column that is is the caller's declaration, checked.** Step 1
Part D measured `customer_id` as 1:1 with orders on Olist -- 99,441 of each -- while
`customer_unique_id` gives 96,096 people and a 3.119% repeat rate. A key distinct per fact row
describes events, and an analysis of repeat behaviour keyed on it reports that nobody ever
returns. That is refused by name here rather than returned as a finding.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, label, number
from .declared import require_dimension
from .registry import Output, register

NO_ENTITY = "(no entity)"
NO_DATE = "(no date)"
BUCKETS = ((1, "once"), (2, "twice"), (3, "three times"), (5, "four or five times"))


@register(
    "repeat_behaviour",
    tier=7,
    summary="How many people appear once and how many come back, how often, and how long they "
            "take to return. The reframe for data where a retention grid would be mostly "
            "empty. Refuses a key that is distinct per row, which describes events, not people.",
)
def repeat_behaviour(con, gate, scope, entity: str, event: str | None = None,
                     **params) -> Output:
    if params:
        raise TypeError(
            f"repeat_behaviour takes entity and event; got {', '.join(sorted(params))}."
        )
    require_dimension(gate.contract, entity)
    if event is not None:
        _require_declared(gate.contract, event)
    date_column = getattr(gate.contract, "date_column", None)
    if not date_column:
        raise ValueError(
            "repeat_behaviour needs a date column on the contract: coming back is a statement "
            "about time, and without one every event is simultaneous."
        )

    summary = [scope.method_note(), *gate.caveats]
    if not scope.analysed:
        return Output(headers=["how often", "people"], rows=[], label="repeat_behaviour",
                      summary=summary + ["No rows are in scope, so nobody has returned yet."])

    table = scope.source
    key = quote_identifier(entity)
    dt = quote_identifier(date_column)
    usable = f"{scope.where} AND {key} IS NOT NULL AND {dt} IS NOT NULL"
    # With an event key (Cleanup Step 14, RF-O6), a person's events are the distinct values of
    # that column -- an order -- and not the rows: the retail run counted a two-line order as a
    # return. Rows with no event are counted apart, as rows with no person are.
    ev = quote_identifier(event) if event else None
    counted = f"count(DISTINCT {ev})" if ev else "count(*)"

    events, distinct_keys = con.execute(
        f"SELECT count(*), count(DISTINCT {key}) FROM {table} WHERE {usable}"
    ).fetchall()[0]
    if events and distinct_keys == events:
        raise ValueError(
            f"repeat_behaviour cannot use {entity!r}: it holds {distinct_keys:,} distinct "
            f"value(s) across {events:,} row(s), one per row. A key that never repeats "
            f"describes events rather than people, and this analysis would report that nobody "
            f"ever returns -- which would be a fact about the column, not about anybody's "
            f"behaviour. Name the column that identifies a person across their events."
        )


    spans = con.execute(
        f"SELECT count(*) FROM (SELECT {key} FROM {table} WHERE {usable} GROUP BY 1 "
        f"HAVING count(DISTINCT {dt}) > 1)").fetchone()[0]
    if events and not spans:
        # Cleanup Step 16, 2.1: an order id repeats across its lines but every line carries the
        # order's one timestamp, so it passes the one-per-row check and describes events anyway --
        # the re-run reported 59,948 of 150,000 orders "came back".
        raise ValueError(
            f"repeat_behaviour cannot use {entity!r}: none of its {distinct_keys:,} value(s) "
            f"appears at more than one moment -- every row of a value shares one {date_column}, "
            f"so it names an "
            f"event (an order of several lines), not someone who came back. Name the column that "
            f"identifies a person across their events."
        )

    no_entity, no_date = con.execute(
        f"SELECT count(*) FILTER (WHERE {key} IS NULL), "
        f"       count(*) FILTER (WHERE {key} IS NOT NULL AND {dt} IS NULL) "
        f"FROM {table} WHERE {scope.where}"
    ).fetchall()[0]
    no_event = 0
    if ev:
        no_event = con.execute(
            f"SELECT count(*) FROM {table} WHERE {usable} AND {ev} IS NULL").fetchone()[0]
    if events + no_entity + no_date != scope.analysed:
        raise LostRows(
            f"repeat_behaviour lost rows: {events:,} usable, {no_entity:,} with no {entity}, "
            f"{no_date:,} with no {date_column}, against {scope.analysed:,} in scope."
        )

    rows_usable = events
    if ev:
        usable = f"{usable} AND {ev} IS NOT NULL"
        events = con.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT {key}, {ev} FROM {table} WHERE {usable})"
        ).fetchone()[0]
    counts = con.execute(
        f"SELECT n, count(*) FROM (SELECT {key} AS k, {counted} AS n FROM {table} "
        f"WHERE {usable} GROUP BY 1) GROUP BY 1 ORDER BY 1"
    ).fetchall()
    people = sum(c for _, c in counts)
    repeaters = sum(c for n, c in counts if n > 1)
    if people != distinct_keys:
        raise LostRows(
            f"repeat_behaviour lost people: the distribution holds {people:,} against "
            f"{distinct_keys:,} distinct {entity} value(s)."
        )

    headers = ["how often", "people", "share of people", "events"]
    rows: list[list[Any]] = []
    placed = 0
    for upper, word in BUCKETS:
        lower = 1 if word == "once" else _previous(upper)
        in_bucket = [(n, c) for n, c in counts if lower <= n <= upper]
        if not in_bucket:
            continue
        subtotal = sum(c for _, c in in_bucket)
        placed += subtotal
        rows.append([word, number(subtotal), f"{subtotal / people:.1%}",
                     number(sum(n * c for n, c in in_bucket))])
    tail = [(n, c) for n, c in counts if n > BUCKETS[-1][0]]
    if tail:
        subtotal = sum(c for _, c in tail)
        placed += subtotal
        rows.append([f"more than {BUCKETS[-1][0]} times", number(subtotal),
                     f"{subtotal / people:.1%}", number(sum(n * c for n, c in tail))])
    if placed != people:
        raise LostRows(
            f"repeat_behaviour lost people: {placed:,} in buckets against {people:,} counted."
        )
    for name, count in ((NO_ENTITY, no_entity), (NO_DATE, no_date),
                        (f"(no {event})", no_event)):
        if count:
            rows.append([name, None, None, number(count)])

    rate = repeaters / people if people else 0.0
    what = (f"{events:,} distinct {event} event(s) in {rows_usable - no_event:,} row(s)" if ev
            else f"{events:,} event(s)")
    summary.append(
        f"{people:,} distinct {entity} value(s) across {what}. {repeaters:,} came "
        f"back at least once — a repeat rate of {rate:.3%}."
    )
    summary.append(
        f"The busiest {entity} accounts for {max(n for n, _ in counts):,} event(s); the median "
        f"is {_median_of(counts):,}."
    )
    gap = _first_gap(con, table, key, dt, usable, ev)
    if gap is not None:
        summary.append(
            f"Among those who returned, the median gap between a first event and a second is "
            f"{gap:,} day(s). That is the interval a retention grid would be resolving, and it "
            f"is why the period a grid is cut into matters more than the grid does."
        )
    between = _consecutive_gap(con, table, key, dt, usable, ev)
    if between is not None:
        summary.append(
            f"The median gap between consecutive {event or 'row'} events is {between:,} day(s), "
            f"across every return, not only the first.")
    summary.append(
        f"{no_entity:,} row(s) have no {entity} and {no_date:,} have no {date_column}; both are "
        f"shown above so the event counts add back to the {scope.analysed:,} in scope. People "
        f"are counted per distinct {entity}, so the people column does not add to a row count "
        f"and is not meant to."
    )
    return Output(headers=headers, rows=rows, summary=summary, label="repeat_behaviour")


def _previous(upper: int) -> int:
    for i, (bound, _) in enumerate(BUCKETS):
        if bound == upper:
            return BUCKETS[i - 1][0] + 1 if i else 1
    return upper


def _median_of(counts: list[tuple[int, int]]) -> int:
    """Median events per person, from the (events, people) distribution."""
    total = sum(c for _, c in counts)
    seen = 0
    for n, c in counts:
        seen += c
        if seen >= (total + 1) // 2:
            return n
    return counts[-1][0] if counts else 0


def _require_declared(contract, column: str) -> None:
    """An event key is read, so the contract must name it -- as a dimension or a measure."""
    named = set(getattr(contract, "dimensions", []) or []) | {
        m.name for m in getattr(contract, "measures", []) or []}
    if column not in named:
        raise ValueError(
            f"{column!r} is not declared in the contract of {contract.dataset_name}, so it cannot "
            f"be the event key. Declare it (as a dimension) and confirm the contract.")


def _events(table: str, key: str, dt: str, usable: str, ev: str | None) -> str:
    """One row per event: a row itself, or with an event key its earliest row."""
    if ev:
        return (f"SELECT {key} AS k, min({dt}) AS d FROM {table} WHERE {usable} "
                f"GROUP BY {key}, {ev}")
    return f"SELECT {key} AS k, {dt} AS d FROM {table} WHERE {usable}"


def _consecutive_gap(con, table: str, key: str, dt: str, usable: str,
                     ev: str | None) -> int | None:
    """Median days between each event and the person's previous one (Cleanup Step 14)."""
    row = con.execute(
        f"WITH e AS ({_events(table, key, dt, usable, ev)}), "
        f"g AS (SELECT date_diff('day', lag(d) OVER (PARTITION BY k ORDER BY d), d) AS gap "
        f"FROM e) SELECT median(gap) FROM g WHERE gap IS NOT NULL"
    ).fetchone()
    return None if row[0] is None else int(row[0])


def _first_gap(con, table: str, key: str, dt: str, usable: str,
               ev: str | None = None) -> int | None:
    """Median days between a person's first event and their second."""
    row = con.execute(
        f"WITH ordered AS ("
        f"  SELECT k, d, row_number() OVER (PARTITION BY k ORDER BY d) AS seq "
        f"  FROM ({_events(table, key, dt, usable, ev)})) "
        f"SELECT median(date_diff('day', a.d, b.d)) FROM ordered a JOIN ordered b "
        f"ON a.k = b.k AND a.seq = 1 AND b.seq = 2"
    ).fetchall()[0]
    return None if row[0] is None else int(row[0])
