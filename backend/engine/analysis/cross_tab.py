"""One declared dimension against another: counts, or a measure's aggregate.

**NULL is a row and a column.** P8-D5: GROUP BY keeps it, PIVOT drops it. Step
7's ground facts measured PIVOT summing to 112.00 of 117.00 on this step's
fixture, and naming the empty-string column after its own generated SQL.

**A cell is chosen by a bound value.** `= ?` never matches a NULL row;
`IS NOT DISTINCT FROM ?` matches it, matches `''`, and matches a value holding
a quote without escaping, because a data value never enters the SQL text.

**Margins are computed from rows, in a second query.** The average of North's
cells is 15; the average of North's rows is 18.75. GROUPING SETS returns two
rows both called NULL -- the NULL group and the grand total -- so the totals
come from the same expressions with no GROUP BY.
"""

from __future__ import annotations

from typing import Any

from ..util.formatting import MAX_COLS, MAX_ROWS
from ..util.sql_guard import quote_identifier
from .base import LostRows, ParamsInvalid, label, number
from .declared import AGG_SQL, agg_of, require_dimension, require_measure
from .registry import Output, register
from .stats import MAX_GROUPS

TOTAL = "(total)"
MAX_ROW_GROUPS = MAX_GROUPS          # the (total) row; stats.py is its home
MAX_COLUMN_GROUPS = MAX_COLS - 2     # the label column and the (total) column


@register(
    "cross_tab",
    tier=1,
    summary="Rows of one declared dimension against the values of another: "
            "counts, or a declared measure under its aggregate, with NULL as "
            "its own row and column and margins computed from the rows.",
)
def cross_tab(con, gate, scope, rows: str, columns: str,
              measure: str | None = None, **params) -> Output:
    if params:
        raise TypeError(
            f"cross_tab takes rows, columns and measure; got "
            f"{', '.join(sorted(params))}."
        )
    if rows == columns:
        raise ParamsInvalid(
            f"cross_tab of {rows} against itself is its frequency: "
            f'frequency(column="{rows}") answers that.'
        )

    contract = gate.contract
    require_dimension(contract, rows)
    require_dimension(contract, columns)

    agg = unit = None
    if measure is not None:
        m = require_measure(contract, measure)
        agg = agg_of(m)
        unit = getattr(m, "unit", None)
        if agg is None:
            raise ValueError(
                f"measure {measure!r} has no declared aggregate, so there is "
                f"no way to fill a cell with it. confirm_dataset_contract with "
                f"agg set is what fixes that; cross_tab without a measure "
                f"counts rows instead."
            )
        if agg == "none":
            raise ValueError(
                f"measure {measure!r} is declared non-additive, so each cell "
                f"would hold a total the contract says is not meaningful. "
                f"cross_tab without a measure counts rows instead."
            )
        if agg not in AGG_SQL:
            raise ValueError(f"cannot fill a cell with agg={agg!r}.")

    table = scope.source
    r = quote_identifier(rows)
    c = quote_identifier(columns)

    if not scope.analysed:
        return Output(
            headers=[rows, TOTAL],
            rows=[],
            summary=[scope.method_note(), *gate.caveats,
                     "No rows are in scope, so there is nothing to cross."],
            label="cross_tab",
        )

    n_rows, n_cols = con.execute(
        f"SELECT (SELECT count(*) FROM (SELECT DISTINCT {r} FROM {table} "
        f"WHERE {scope.where})), "
        f"(SELECT count(*) FROM (SELECT DISTINCT {c} FROM {table} "
        f"WHERE {scope.where}))"
    ).fetchall()[0]
    if n_rows > MAX_ROW_GROUPS or n_cols > MAX_COLUMN_GROUPS:
        raise ValueError(
            f"cross_tab of {rows} by {columns} would be {n_rows} row group(s) "
            f"by {n_cols} column group(s), NULL counted as a group. It is "
            f"capped at {MAX_ROW_GROUPS} row group(s) and {MAX_COLUMN_GROUPS} "
            f"column group(s), so that with its {TOTAL} row and column it fits "
            f"the {MAX_ROWS} x {MAX_COLS} table limit. frequency or top_n on "
            f"the larger dimension says which of its values matter; a coarser "
            f"declared dimension is the other way."
        )

    values = [v[0] for v in con.execute(
        f"SELECT DISTINCT {c} FROM {table} WHERE {scope.where} "
        f"ORDER BY 1 NULLS LAST"
    ).fetchall()]

    if measure is None:
        margin = "count(*)"
        cell = "count(*)"
    else:
        margin = AGG_SQL[agg].format(col=quote_identifier(measure))
        cell = margin
    cells = [f"{cell} FILTER (WHERE {c} IS NOT DISTINCT FROM ?)" for _ in values]

    body = con.execute(
        f"SELECT {r}, {', '.join(cells)}, {margin}, count(*) FROM {table} "
        f"WHERE {scope.where} GROUP BY 1 ORDER BY 1 NULLS LAST",
        list(values),
    ).fetchall()
    totals = con.execute(
        f"SELECT {', '.join(cells)}, {margin}, count(*), "
        f"count(*) FILTER (WHERE {c} IS NULL) FROM {table} WHERE {scope.where}",
        list(values),
    ).fetchall()[0]

    counted = sum(row[-1] for row in body)
    if not counted == totals[-2] == scope.analysed:
        raise LostRows(
            f"cross_tab lost rows: its cells count {counted:,} row(s) against "
            f"{scope.analysed:,} in scope -- the failure P8-D5 measured PIVOT "
            f"making."
        )

    headers = [rows] + [label(v) for v in values] + [TOTAL]
    first = [label(row[0]) for row in body] + [TOTAL]
    for names in (headers, first):
        seen, dups = set(), set()
        for name in names:
            (dups if name in seen else seen).add(name)
        if dups:
            raise ValueError(
                f"cross_tab cannot label its table: {sorted(dups)} would "
                f"appear twice. A value spelled '(null)' or '(total)' collides "
                f"with the labels this table uses for NULL and for the margin."
            )

    out: list[list[Any]] = []
    for row in body:
        out.append([label(row[0])] + [number(v) for v in row[1:-1]])
    out.append([TOTAL] + [number(v) for v in totals[:-2]])

    summary = [scope.method_note(), *gate.caveats]
    if measure is None:
        summary.append(
            f"Cells count analysed rows; rows are {rows}, columns are {columns}."
        )
    else:
        summary.append(
            f"Cells are {agg} of {measure}{f' ({unit})' if unit else ''}; rows "
            f"are {rows}, columns are {columns}. The {TOTAL} row and column are "
            f"{agg} over the underlying rows, not a combination of the cells."
        )
        if any(cell is None for line in out for cell in line[1:]):
            summary.append(
                f"A blank cell is not zero: no analysed row had a value of "
                f"{measure} for that combination."
            )
    null_rows = next((row[-1] for row in body if row[0] is None), 0)
    if null_rows:
        summary.append(
            f"{null_rows:,} analysed row(s) have no {rows} and are the (null) "
            f"row -- not dropped, and not the empty string, which would be a "
            f"different value."
        )
    if totals[-1]:
        summary.append(
            f"{totals[-1]:,} analysed row(s) have no {columns} and are the "
            f"(null) column -- not dropped, and not the empty string, which "
            f"would be a different value."
        )
    if "" in values:
        summary.append(
            "One column is the empty string '' -- a value somebody wrote, not "
            "a missing one; (null) is the missing one."
        )
    return Output(headers=headers, rows=out, summary=summary, label="cross_tab")
