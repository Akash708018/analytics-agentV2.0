"""One declared measure, summarised per group of one declared dimension.

`summary_stats` answers "what does this measure come to" over the whole scope.
This answers the same question per group, plus the (all) row the groups are
read against. The statistics are `stats.py`'s, not a second spelling of them.

**The (all) row is computed from rows, not from the cells.** P8-D43, measured
on cross_tab: North's mean over its rows is 18.75 and the mean of its cells is
15. So the (all) row is the same expressions with no GROUP BY, in a second
query, and never a combination of the group rows.

**A comparison is read by group; a ranking is read by value.** The order is the
group name ascending, NULLS LAST -- deterministic without needing a tiebreak,
because a group name appears once. `top_n` is the ranking, and P8-D26's tie
note belongs there.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, TooManyGroups, label, number, share_basis, relation_types
from .declared import AGG_SQL, agg_of, column_types, is_numeric
from .declared import require_dimension, require_measure
from .registry import Output, register
from .stats import (MAX_GROUPS, STAT_HEADERS, can_be_non_finite, non_finite_count,
                    non_finite_note, stat_cells, stat_exprs)

ALL = "(all)"


@register(
    "group_compare",
    tier=2,
    summary="One declared measure summarised per group of a declared "
            "dimension, with an (all) row computed from the rows and a share "
            "column when the declared aggregate adds across groups.",
    selects=True,
)
def group_compare(con, gate, scope, dimension: str, measure: str, **params) -> Output:
    if params:
        raise TypeError(
            f"group_compare takes dimension and measure; got "
            f"{', '.join(sorted(params))}."
        )

    contract = gate.contract
    require_dimension(contract, dimension)
    m = require_measure(contract, measure)
    agg = agg_of(m)
    unit = getattr(m, "unit", None) or ""

    if agg is not None and agg != "none" and agg not in AGG_SQL:
        raise ValueError(
            f"measure {measure!r} declares agg={agg!r}, which this analysis "
            f"cannot compute. Known: {', '.join(sorted(AGG_SQL))}, none."
        )

    table = scope.source
    dim = quote_identifier(dimension)
    col = quote_identifier(measure)
    dtype = relation_types(con, scope).get(measure, "")
    numeric = is_numeric(dtype)
    floating = can_be_non_finite(dtype)

    totalled = agg is not None and agg != "none"
    headers = [dimension] + STAT_HEADERS[:2]
    if totalled:
        # Named "total", as summary_stats names it, and never after the
        # aggregate: a column headed "mean" beside the mean statistic is two
        # columns with one name, which cross_tab's label check would refuse.
        headers.append(f"total{f' ({unit})' if unit else ''}")
    headers += STAT_HEADERS[2:]

    summary = [scope.method_note(), *gate.caveats]

    if not scope.analysed:
        return Output(headers=headers, rows=[], label="group_compare",
                      summary=summary + [
                          "No rows are in scope, so there is nothing to "
                          "compare."])

    n_groups = con.execute(
        f"SELECT count(*) FROM (SELECT DISTINCT {dim} FROM {table} "
        f"WHERE {scope.where})"
    ).fetchall()[0][0]
    if n_groups > MAX_GROUPS:
        raise TooManyGroups(
            f"group_compare of {measure} by {dimension} would be {n_groups} "
            f"group(s), NULL counted as a group, against a cap of "
            f"{MAX_GROUPS} so that the {ALL} row fits the table limit. "
            f"top_n on {dimension} says which of its groups matter.", dimension, measure
        )

    exprs = stat_exprs(col, numeric, floating)
    total_sql = AGG_SQL[agg].format(col=col) if totalled else "NULL"

    body = con.execute(
        f"SELECT {dim}, {', '.join(exprs)}, {total_sql} FROM {table} "
        f"WHERE {scope.where} GROUP BY 1 ORDER BY 1 NULLS LAST"
    ).fetchall()
    overall = con.execute(
        f"SELECT {', '.join(exprs)}, {total_sql} FROM {table} "
        f"WHERE {scope.where}"
    ).fetchall()[0]

    counted = sum(row[1] for row in body)
    if counted != scope.analysed or overall[0] != scope.analysed:
        raise LostRows(
            f"group_compare lost rows: its groups hold {counted:,} row(s) and "
            f"the {ALL} row {overall[0]:,}, against {scope.analysed:,} in scope."
        )

    basis = share_basis(agg, [row[-1] for row in body]) if totalled else None

    out: list[list[Any]] = []
    for row in body:
        cells = stat_cells(row[1:8])
        line = [label(row[0]), cells[0], cells[1]]
        if totalled:
            line.append(number(row[-1]))
        line += cells[2:]
        if basis is not None and basis.ok:
            line.append(basis.share(row[-1]))
        out.append(line)

    cells = stat_cells(overall[:7])
    line = [ALL, cells[0], cells[1]]
    if totalled:
        line.append(number(overall[-1]))
    line += cells[2:]
    if basis is not None and basis.ok:
        line.append("100.0%")
    out.append(line)

    if basis is not None and basis.ok:
        headers.append("share")
        summary.append(
            f"The total column is {agg} of {measure}. Share is of "
            f"{number(basis.denominator)}, that total across all "
            f"{len(body):,} group(s)."
        )
    elif basis is not None:
        summary.append(
            f"The total column is {agg} of {measure}. No share column: "
            f"{basis.reason}"
        )

    if not totalled:
        summary.append(
            "No total column: "
            + (f"{measure} is declared non-additive, so a per-group total would "
               f"be a number the contract says is not meaningful."
               if agg == "none" else
               f"{measure} has no declared aggregate. "
               f"confirm_dataset_contract with agg set is what fixes that.")
        )

    if floating:
        bad = con.execute(f"SELECT {non_finite_count(col)} FROM {table} "
                          f"WHERE {scope.where}").fetchall()[0][0]
        if bad:
            summary.append(non_finite_note(bad, measure))
    summary.append(
        f"The {ALL} row is computed over the analysed rows, not from the group "
        f"rows above it: a mean of group means is not the mean."
    )
    nulls = next((row[1] for row in body if row[0] is None), 0)
    if nulls:
        summary.append(
            f"{nulls:,} analysed row(s) have no {dimension} and are the (null) "
            f"group -- not dropped, and not the empty string, which would be a "
            f"different value."
        )
    if any(line[STAT_HEADERS.index('stddev') + (2 if totalled else 1)] is None
           for line in out):
        summary.append(
            "A blank spread is not zero: stddev is the sample standard "
            "deviation and one row has none."
        )
    return Output(headers=headers, rows=out, summary=summary,
                  label="group_compare")
