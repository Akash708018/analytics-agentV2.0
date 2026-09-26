"""How a ranking of groups changed between two periods.

**The two periods are arguments, not contract state.** `AnalysisWindow` is one
inclusive start and end, and `scope_for` reads it off the contract, so a
contract cannot describe two periods. The alternative -- letting this analysis
build a second Scope -- would put the method note printed above the table out
of step with the rows beneath it, which is the failure base.py exists to
prevent. So each period narrows WITHIN the scope: `scope.where AND (period)`.

**A period is not the scope.** Two periods need not cover the analysed rows,
and usually will not: rows before the first, between the two, or after the
second are in scope and in neither period. That count is reported rather than
left for a reader to derive from three numbers that do not add up.

**Arriving and leaving are not rank changes.** A group present in one period
and absent from the other has no rank there, and subtracting from a blank
produces a number nobody can check. Those groups are labelled rather than
scored, and counted in the summary -- a ranking that silently dropped them
would read as stability.

**A rank is a position, and positions tie.** P8-D26: the order is
deterministic by the group-name tiebreak, which makes the answer repeatable
without making it the only correct one. Where groups tie on a total, the note
says so, because the rank change of a tied group is an artifact of the
tiebreak.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..util.sql_guard import quote_identifier
from .base import ParamsInvalid, TooManyGroups, label, number, window_clause
from .declared import AGG_SQL, agg_of, require_dimension, require_measure
from .registry import Output, register
from .stats import MAX_GROUPS, ranked_totals

NEW = "new"
GONE = "gone"


def _period(name: str, start: str, end: str) -> tuple[date, date]:
    """Two ISO dates, parsed and ordered, or a refusal naming which end."""
    try:
        first = date.fromisoformat(str(start))
        last = date.fromisoformat(str(end))
    except (TypeError, ValueError) as exc:
        raise ParamsInvalid(
            f"the {name} period needs two ISO dates (YYYY-MM-DD); got "
            f"{start!r} and {end!r} -- {exc}"
        ) from exc
    if last < first:
        raise ParamsInvalid(
            f"the {name} period ends {last} which is before it starts {first}."
        )
    return first, last


@register(
    "ranking_shift",
    tier=2,
    summary="How the ranking of a declared dimension by a declared measure "
            "changed between two periods, with groups that arrived or left "
            "named rather than scored.",
)
def ranking_shift(con, gate, scope, dimension: str, measure: str,
                  before_start: str, before_end: str,
                  after_start: str, after_end: str, **params) -> Output:
    if params:
        raise TypeError(
            f"ranking_shift takes dimension, measure and the four period "
            f"dates; got {', '.join(sorted(params))}."
        )

    contract = gate.contract
    date_column = getattr(contract, "date_column", None)
    if not date_column:
        raise ValueError(
            f"{contract.dataset_name} has no date_column, so there is no "
            f"column to cut two periods out of. confirm_dataset_contract with "
            f"date_column set is what fixes that; group_compare compares "
            f"groups without a period."
        )

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

    before = _period("before", before_start, before_end)
    after = _period("after", after_start, after_end)

    headers = [dimension, f"{measure} before", "rank before",
               f"{measure} after", "rank after", "change"]
    summary = [scope.method_note(), *gate.caveats]

    if not scope.analysed:
        return Output(headers=headers, rows=[], label="ranking_shift",
                      summary=summary + [
                          "No rows are in scope, so there is nothing to rank."])

    total_sql = AGG_SQL[agg].format(col=quote_identifier(measure))
    clauses = {
        "before": window_clause(date_column, *before),
        "after": window_clause(date_column, *after),
    }
    ranked = {
        which: ranked_totals(con, scope, dimension, total_sql, extra=clause)
        for which, clause in clauses.items()
    }

    groups = {label(g[0]) for side in ranked.values() for g in side}
    if len(groups) > MAX_GROUPS:
        raise TooManyGroups(
            f"{dimension} has {len(groups)} group(s) across the two periods, "
            f"NULL counted as a group, against a cap of {MAX_GROUPS}. top_n on "
            f"{dimension} says which of its groups matter.", dimension, measure
        )

    positions: dict[str, dict[str, Any]] = {}
    for which, side in ranked.items():
        for rank, (value, total, _rows) in enumerate(side, start=1):
            positions.setdefault(label(value), {})[which] = (rank, total)

    rows: list[list[Any]] = []
    arrived = departed = 0
    def _order(group: str):
        """Groups the after period ranks, in that order; then the ones it does
        not, by name. A departed group has no after rank to sort on."""
        return (positions[group].get("after", (10 ** 9,))[0], group)

    for name in sorted(groups, key=_order):
        b = positions[name].get("before")
        a = positions[name].get("after")
        if b and a:
            moved = b[0] - a[0]
            change = "0" if moved == 0 else f"{moved:+d}"
        elif a:
            change, arrived = NEW, arrived + 1
        else:
            change, departed = GONE, departed + 1
        rows.append([
            name,
            number(b[1]) if b else "", number(b[0]) if b else "",
            number(a[1]) if a else "", number(a[0]) if a else "",
            change,
        ])

    counts = con.execute(
        f"SELECT count(*) FILTER (WHERE {clauses['before']}), "
        f"count(*) FILTER (WHERE {clauses['after']}), "
        f"count(*) FILTER (WHERE NOT ({clauses['before']}) "
        f"AND NOT ({clauses['after']})) "
        f"FROM {scope.source} "
        f"WHERE {scope.where}"
    ).fetchall()[0]

    summary.append(
        f"Ranked by {agg} of {measure}. Before is "
        f"{before[0].isoformat()} to {before[1].isoformat()} and holds "
        f"{counts[0]:,} analysed row(s); after is {after[0].isoformat()} to "
        f"{after[1].isoformat()} and holds {counts[1]:,}."
    )
    if counts[2]:
        summary.append(
            f"{counts[2]:,} analysed row(s) fall in neither period and are in "
            f"no column here -- the two periods do not have to cover the scope, "
            f"and these rows are why the two counts above do not add to the "
            f"{scope.analysed:,} analysed."
        )
    if arrived or departed:
        summary.append(
            f"{arrived} group(s) appear only in the after period and "
            f"{departed} only in the before period. They carry {NEW} and "
            f"{GONE} rather than a rank change: there is no rank to subtract "
            f"from, and a blank treated as zero would read as a move."
        )
    for which, side in ranked.items():
        totals = [g[1] for g in side]
        tied = len(totals) - len(set(totals))
        if tied:
            summary.append(
                f"{tied + 1} group(s) tie on {agg} of {measure} in the {which} "
                f"period, so their positions -- and every change computed from "
                f"them -- come from the tiebreak on the group name, not from "
                f"the data."
            )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="ranking_shift")
