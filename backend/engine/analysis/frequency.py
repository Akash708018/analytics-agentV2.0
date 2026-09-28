"""How often each value of a dimension appears, and which are the biggest.

Two analyses, one file, because they are the same problem twice: put rows into
groups, sort the groups, cut the list. Everything that is hard about them is
hard about both.

**NULL is a group.** Step 1 measured that GROUP BY keeps it as its own group
and that PIVOT silently drops it, and separately that `''` and NULL are two
distinct values. So the group is labelled `(null)` rather than left blank -- a
blank cell reads as the empty string, which is a different answer.

**A cut list is not a top.** `LIMIT n` over ties answers a different question
than the one asked: Step 1 measured 100,000 rows tied at the top of a 300,000
row table, of which `LIMIT 5` returned five, the same five on five runs, and a
tiebreak on the key changed the answer completely. Both orderings are correct
and neither is "the top 5". So the order is made deterministic with an explicit
tiebreak, AND the number of groups tied at the cut is reported. The second half
is the one that matters: determinism makes the answer repeatable, saying so
makes it honest.

**A share needs a denominator that is a total.** P8-D1 and P8-D2: `x/0` is
`inf` in DuckDB 1.5.5, not an error, and a share of a mixed-sign total is not a
proportion -- four rows summing to zero produced `inf, inf, -inf, -inf` and a
cumulative share ending in `nan`. So the denominator is computed first and the
share column is dropped, with a sentence, when it cannot mean anything.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import ParamsInvalid, label, number, share_basis
from .declared import AGG_SQL, agg_of, require_dimension, require_measure
from .registry import Output, register
from .stats import ranked_totals

# How many groups a frequency table shows before it stops being a table anyone
# reads. results.py previews 20 rows and pages the rest, so this is not a data
# limit -- the file has everything -- it is what the cut is set to by default.
DEFAULT_LIMIT = 20


def _tie_note(rows: list[list[Any]], value_index: int, shown: int, total_groups: int) -> str | None:
    """How many groups sit at the same value as the last one shown.

    The sentence exists because "top 5" is a claim about the data and LIMIT is
    a claim about the output, and they are only the same claim when nothing
    ties at the cut.
    """
    if shown >= total_groups or not rows:
        return None
    cut_value = rows[shown - 1][value_index]
    tied = sum(1 for r in rows if r[value_index] == cut_value)
    if tied < 2:
        return None
    return (
        f"{tied} group(s) share the value at the cut, so which of them appear "
        f"here is decided by the tiebreak on the group name, not by the data."
    )


@register(
    "frequency",
    tier=1,
    summary="How many rows carry each value of a declared dimension, with the "
            "share of the analysed rows and NULL counted as its own group.",
)
def frequency(con, gate, scope, column: str, limit: int = DEFAULT_LIMIT, **params) -> Output:
    """One row per value of `column`, most frequent first."""
    if params:
        raise TypeError(
            f"frequency takes column and limit; got {', '.join(sorted(params))}."
        )
    if limit < 1:
        raise ParamsInvalid(f"limit must be at least 1; got {limit}.")

    contract = gate.contract
    require_dimension(contract, column)
    col = quote_identifier(column)
    table = scope.source

    grouped = con.execute(
        f"SELECT {col}, count(*) FROM {table} WHERE {scope.where} "
        f"GROUP BY 1 ORDER BY 2 DESC, 1 ASC"
    ).fetchall()

    total = scope.analysed
    headers = ["value", "rows", "share"]
    summary = [scope.method_note()] + list(gate.caveats)

    if total == 0:
        return Output(headers=headers, rows=[], label="frequency", summary=summary + [
            "No rows are in scope, so there is nothing to count. The counts "
            "above say which rows the contract removed."
        ])

    shown = min(limit, len(grouped))
    rows = [
        [label(v), number(n), f"{n / total * 100:.1f}%"]
        for v, n in grouped[:shown]
    ]

    summary.append(
        f"{len(grouped):,} distinct value(s) of {column}; "
        f"{'all' if shown == len(grouped) else f'the {shown} most frequent'} "
        f"shown. Share is of the {total:,} analysed row(s)."
    )
    tie = _tie_note([[v, n] for v, n in grouped], 1, shown, len(grouped))
    if tie:
        summary.append(tie)
    nulls = next((n for v, n in grouped if v is None), 0)
    if nulls:
        summary.append(
            f"{nulls:,} analysed row(s) have no {column} and are the (null) "
            f"group -- not dropped, and not the empty string, which would be a "
            f"different value."
        )
    return Output(headers=headers, rows=rows, summary=summary, label="frequency")


@register(
    "top_n",
    tier=1,
    summary="The largest groups of a declared dimension by a declared measure, "
            "with the number tied at the cut stated; with period, within one named "
            "period of the date column.",
    narrows=True,
)
def top_n(con, gate, scope, dimension: str, measure: str,
          n: int = 10, **params) -> Output:
    """The `n` biggest values of `dimension`, ranked by `measure`."""
    if params:
        raise TypeError(
            f"top_n takes dimension, measure, n, period and grain; got "
            f"{', '.join(sorted(params))}."
        )
    if n < 1:
        raise ParamsInvalid(f"n must be at least 1; got {n}.")

    contract = gate.contract
    require_dimension(contract, dimension)
    m = require_measure(contract, measure)
    agg = agg_of(m)
    if agg is None:
        raise ValueError(
            f"measure {measure!r} has no declared aggregate, so there is no "
            f"way to rank by it. confirm_dataset_contract with agg set is what "
            f"fixes that."
        )
    if agg == "none":
        raise ValueError(
            f"measure {measure!r} is declared non-additive, so ranking groups "
            f"by its total would produce an order built from a number the "
            f"contract says is not meaningful."
        )
    if agg not in AGG_SQL:
        raise ValueError(f"cannot rank by agg={agg!r}.")

    dim = quote_identifier(dimension)
    total_sql = AGG_SQL[agg].format(col=quote_identifier(measure))
    table = scope.source

    # P8-D4: the tiebreak is on the group name, so the same data gives the same
    # answer twice. It does not make the answer the only correct one, which is
    # what the tie note is for.
    grouped = ranked_totals(con, scope, dimension, total_sql)

    summary = [scope.method_note()] + list(gate.caveats)
    headers = ["value", f"{measure} ({agg})", "rows", "share"]

    if not grouped:
        return Output(headers=headers, rows=[], label="top_n", summary=summary + [
            "No rows are in scope, so there is nothing to rank."
        ])

    basis = share_basis(agg, [g[1] for g in grouped])

    shown = min(n, len(grouped))
    rows = []
    for value, total, count in grouped[:shown]:
        rows.append([label(value), number(total), number(count),
                     basis.share(total)])

    summary.append(
        f"{len(grouped):,} group(s) of {dimension}; the {shown} largest by "
        f"{agg} of {measure} shown."
    )
    tie = _tie_note([[g[0], g[1]] for g in grouped], 1, shown, len(grouped))
    if tie:
        summary.append(tie)

    if not basis.ok:
        summary.append(f"No share column: {basis.reason}")
    else:
        # The denominator is stated because it is not always the analysed rows:
        # AGG_SQL spells count as count(col), which skips a null measure, so a
        # count ranking divides by fewer rows than the method note reports.
        summary.append(
            f"Share is of {number(basis.denominator)}, the {agg} of {measure} "
            f"across all {len(grouped):,} group(s)."
        )
        if shown < len(grouped):
            # Stated, so no reader adds the column up themselves -- the live assistant of
            # Cleanup Step 10 did, and reported 56% for five shares summing to 53.1%.
            held = sum(g[1] for g in grouped[:shown] if g[1] is not None)
            summary.append(f"The {shown} shown hold {basis.share(held)} of it together.")
        if agg == "count" and basis.denominator != scope.analysed:
            summary.append(
                f"count({measure}) skips rows where {measure} is null, so that "
                f"denominator is {number(basis.denominator)} and not the "
                f"{scope.analysed:,} analysed row(s)."
            )
    return Output(headers=headers, rows=rows, summary=summary, label="top_n")
