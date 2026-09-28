"""The shape of one declared measure: equal-width bins, shares, quantiles.

**Not every column.** profile_column already describes any column without a
contract. This answers the narrower question about a column the contract
declares, the same division summary_stats draws.

**A distribution totals nothing,** so an unset or non-additive `agg` does not
stop it: P8-D22's refusals exist to stop a WRONG TOTAL, and there is no total
here. It says so in a sentence rather than behaving differently from
summary_stats without a word.

**Edges are computed in Python and bound as parameters.** Step 7's ground facts
measured floor((x - min) / width) filing 0.3, 0.6 and 0.7 one bin low over
0.0..1.0 in tenths, and putting the maximum in a bin that does not exist.
"""

from __future__ import annotations

from typing import Any

from ..util.formatting import MAX_ROWS
from ..util.sql_guard import quote_identifier
from .base import LostRows, ParamsInvalid, number, relation_types
from .declared import agg_of, column_types, is_integer, is_numeric, require_measure
from .registry import Output, register

_HEADERS = ["from", "to", "rows", "share", "cumulative"]

DEFAULT_BINS = 10
MAX_BINS = MAX_ROWS


@register(
    "distribution",
    tier=1,
    summary="The shape of one declared measure over the contract's rows: "
            "equal-width bins with counts and shares, and its quantiles.",
)
def distribution(con, gate, scope, measure: str, bins: int = DEFAULT_BINS, **params) -> Output:
    if params:
        raise TypeError(
            f"distribution takes measure and bins; got {', '.join(sorted(params))}."
        )
    if isinstance(bins, bool) or not isinstance(bins, int) or not 2 <= bins <= MAX_BINS:
        raise ParamsInvalid(
            f"bins must be a whole number from 2 to {MAX_BINS}; got {bins!r}."
        )

    contract = gate.contract
    m = require_measure(contract, measure)
    agg = agg_of(m)

    dtype = relation_types(con, scope).get(measure, "")
    if not is_numeric(dtype):
        raise ValueError(
            f"{measure!r} has type {dtype}, not numeric, so there is nothing to "
            f"bin. profile_column describes any column without a contract."
        )

    table = scope.source
    col = quote_identifier(measure)
    v = col if is_integer(dtype) else f"CAST({col} AS DOUBLE)"

    stats = con.execute(
        f"SELECT count({col}), "
        f"count(*) FILTER (WHERE isfinite({v})), "
        f"min({v}) FILTER (WHERE isfinite({v})), "
        f"max({v}) FILTER (WHERE isfinite({v})), "
        f"quantile_cont(CAST({col} AS DOUBLE), [0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0]) "
        f"FILTER (WHERE isfinite({v})) "
        f"FROM {table} WHERE {scope.where}"
    ).fetchall()[0]
    present, finite, lo, hi, quantiles = stats
    nulls = scope.analysed - present
    non_finite = present - finite

    summary = [scope.method_note()] + list(gate.caveats)

    def _tail(out: list[str]) -> list[str]:
        if non_finite:
            out.append(
                f"{non_finite:,} value(s) of {measure} are inf or nan and are "
                f"not binned or in the quantiles -- one nan makes max() nan and "
                f"every edge built from it meaningless."
            )
        if nulls:
            out.append(
                f"{nulls:,} analysed row(s) have no {measure} and are not binned."
            )
        if agg is None:
            out.append(
                f"{measure} has no declared aggregate; distribution totals "
                f"nothing, so that does not limit it."
            )
        elif agg == "none":
            out.append(
                f"{measure} is declared non-additive; distribution totals "
                f"nothing, so that does not limit it."
            )
        return out

    if not finite:
        summary.append(
            f"No finite value of {measure} is in scope, so there is nothing to bin."
        )
        return Output(headers=_HEADERS, rows=[], summary=_tail(summary),
                      label="distribution")

    integer = is_integer(dtype)
    if integer:
        span = hi - lo + 1
        width = -(-span // bins)
        k = -(-span // width)
        edges = [lo + i * width for i in range(k)] + [hi]
    else:
        k = bins
        span = width = None
        edges = [lo] + [lo + (hi - lo) * i / bins for i in range(1, bins)] + [hi]

    # P8-O13: a bare ? against a UHUGEINT column makes DuckDB cast the COLUMN to
    # BIGINT, which fails on any value above 2**63-1. The edge carries the
    # column's own type instead, so the column is never narrowed.
    edge = f"CAST(? AS {dtype})" if integer else "?"
    cells = [
        f"count(*) FILTER (WHERE isfinite({v}) AND {v} >= {edge} AND "
        f"{v} {'<=' if i == k - 1 else '<'} {edge})"
        for i in range(k)
    ]
    bound: list[Any] = []
    for i in range(k):
        bound += [edges[i], edges[i + 1]]
    counts = list(
        con.execute(
            f"SELECT {', '.join(cells)} FROM {table} WHERE {scope.where}", bound
        ).fetchall()[0]
    )

    held = sum(counts)
    if held != finite:
        raise LostRows(
            f"distribution lost rows: its {k} bin(s) hold {held} value(s) "
            f"against {finite} finite value(s) of {measure} in scope."
        )

    rows: list[list[Any]] = []
    running = 0
    for i, count in enumerate(counts):
        running += count
        rows.append([
            number(edges[i]),
            number(edges[i + 1]),
            number(count),
            f"{count / finite * 100:.1f}%",
            f"{running / finite * 100:.1f}%",
        ])

    if lo == hi:
        summary.append(
            f"Every finite value of {measure} is {number(lo)}, so there is one bin."
        )
    else:
        summary.append(
            f"{k} bin(s) of {measure} between {number(lo)} and {number(hi)}; each "
            f"includes its lower edge and excludes its upper, except the last, "
            f"which includes both."
        )
        if integer and k != bins:
            summary.append(
                f"{k} bin(s), not the {bins} requested: {measure} is an integer "
                f"column spanning {span} value(s), so each bin is {width} "
                f"value(s) wide rather than a fraction that would leave some "
                f"bins empty by construction."
            )
        if integer and k * width != span:
            # The equal-width description held for every bin but the last (Step 13 benchmark).
            summary.append(
                f"The last bin is narrower: {span - (k - 1) * width} value(s) wide against "
                f"{width} for the others, because {span} value(s) do not divide into "
                f"{width}-wide bins."
            )

    q = [number(x) for x in quantiles]
    summary.append(
        f"Quantiles of {measure} over {finite:,} finite value(s): min {q[0]}, "
        f"p5 {q[1]}, p25 {q[2]}, median {q[3]}, p75 {q[4]}, p95 {q[5]}, max {q[6]}."
    )
    summary.append(
        f"Share is of the {finite:,} binned value(s), not of the "
        f"{scope.analysed:,} analysed row(s)."
    )
    return Output(headers=_HEADERS, rows=rows, summary=_tail(summary),
                  label="distribution")
