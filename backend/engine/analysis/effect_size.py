"""How big the difference is, separately from whether it is detectable.

A p-value falls as rows are added whether or not anything changed, so on a large enough table
almost everything is significant. This answers the other question, and it is the one a reader
usually meant: is the gap worth acting on.

  two groups of a measure        Hedges' g, the difference in standard deviations
  more than two                  eta squared, the share of variation the grouping explains
  two dimensions                 Cramer's V, the association rescaled to [0, 1]

P10-D17: g rather than d always. The small-sample correction is 0.9577 at ten per group and
0.9962 at a hundred, so it matters exactly where group breakdowns land and costs nothing where
they do not.
"""

from __future__ import annotations

import math
from typing import Any

from ..util.sql_guard import quote_identifier
from .base import NO_MEMBER, LostRows, label, number, relation_types
from .declared import column_types, is_numeric, require_dimension, require_measure
from .inferential import FINITE, chi_square, eta_squared, group_stats, hedges_g
from .registry import Output, register
from .stats import MAX_GROUPS

NO_VALUE = "no_value"        # a key, not a label
NO_GROUP = "no_group"        # see _shown() below
NOT_FINITE = "not_finite"

# Cohen's conventions, named as conventions rather than applied as thresholds. They are a
# vocabulary for talking about a number, not a rule about which numbers matter -- what counts as
# a large effect is a question about the subject, not about the arithmetic.
G_BANDS = ((0.2, "small"), (0.5, "medium"), (0.8, "large"))
ETA_BANDS = ((0.01, "small"), (0.06, "medium"), (0.14, "large"))
V_BANDS = ((0.1, "small"), (0.3, "medium"), (0.5, "large"))


def _shown(key: str, dimension: str, measure: str | None = None) -> str:
    """The row label for an excluded bucket, named for the column it is missing.

    P9-O8: these constants are dictionary keys as well as labels, so the two are separated
    here rather than by changing the constants. P9-O8 asked which of two spellings for a null
    group wins; the Tier 4 answer -- name the column -- wins, because "(no region)" says which
    column was empty and "(no group)" does not.

    Built one branch at a time rather than as a dict of all three. The share branch of
    confidence_interval has a dimension and no measure, and a dict literal would evaluate
    f"(no {measure})" there whether or not that key was the one asked for.
    """
    if key == NO_GROUP:
        return NO_MEMBER.format(dimension=dimension)
    if key == NO_VALUE:
        return f"(no {measure})"
    if key == NOT_FINITE:
        return f"({measure} not a number)"
    raise KeyError(key)


def _shown_pair(dimension: str, second: str) -> str:
    """The row label when a row is missing either of two dimensions rather than a measure.

    The association branches cross two dimensions and have no measure in scope, so _shown's
    measure spelling would name a column that is not what is missing.
    """
    return f"(no {dimension} or {second})"

@register(
    "effect_size",
    tier=6,
    summary="How large a difference is, in units that do not grow with the row count: Hedges' g "
            "between two groups, eta squared across more, Cramer's V between two dimensions. "
            "Named, banded by Cohen's conventions, and stated as a convention rather than a "
            "verdict.",
    selects=True,
)
def effect_size(con, gate, scope, dimension: str, measure: str | None = None,
                second_dimension: str | None = None, **params) -> Output:
    if params:
        raise TypeError(
            f"effect_size takes dimension, and either measure or second_dimension; "
            f"got {', '.join(sorted(params))}."
        )
    if (measure is None) == (second_dimension is None):
        raise ValueError(
            "effect_size sizes the difference in one measure across the groups of a dimension, "
            "or the association between two dimensions. Pass exactly one of measure, "
            "second_dimension."
        )

    require_dimension(gate.contract, dimension)
    summary = [scope.method_note(), *gate.caveats]
    if not scope.analysed:
        return Output(headers=["group", "n"], rows=[], label="effect_size",
                      summary=summary + ["No rows are in scope, so there is nothing to size."])

    if second_dimension is not None:
        return _association(con, gate, scope, dimension, second_dimension, summary)
    return _difference(con, gate, scope, dimension, measure, summary)


def band(value: float, bands) -> str:
    """Cohen's label for a magnitude, named as his rather than asserted as true."""
    size = abs(value)
    name = "negligible"
    for threshold, word in bands:
        if size >= threshold:
            name = word
    return name


def _difference(con, gate, scope, dimension: str, measure: str, summary: list[str]) -> Output:
    m = require_measure(gate.contract, measure)
    unit = getattr(m, "unit", None) or ""
    if not is_numeric(relation_types(con, scope).get(measure, "")):
        raise ValueError(f"effect_size sizes a numeric measure; {measure!r} is not one.")

    groups = group_stats(con, scope, dimension, measure)
    if len(groups) > MAX_GROUPS:
        raise ValueError(
            f"effect_size of {measure} by {dimension} would be {len(groups)} group(s) against a "
            f"cap of {MAX_GROUPS}. top_n on {dimension} says which of its groups matter."
        )

    excluded = _excluded(con, scope, dimension, measure)
    sized = sum(g.n for g in groups)
    if sized + sum(excluded.values()) != scope.analysed:
        raise LostRows(
            f"effect_size lost rows: {sized:,} sized, {excluded[NO_GROUP]:,} with no "
            f"{dimension}, {excluded[NO_VALUE]:,} with no {measure}, "
            f"{excluded[NOT_FINITE]:,} not a finite number, against {scope.analysed:,} in scope."
        )

    headers = ["group", "n", f"mean{f' ({unit})' if unit else ''}", "stddev"]
    rows: list[list[Any]] = [
        [label(g.name), number(g.n), number(g.mean), number(g.sd)] for g in groups
    ]
    for key, count in excluded.items():
        if count:
            rows.append([_shown(key, dimension, measure), number(count), None, None])

    if len(groups) < 2:
        summary.append(
            f"No effect size: {dimension} has {len(groups)} group(s) among the analysed rows, "
            f"and a difference needs two."
        )
        return Output(headers=headers, rows=rows, summary=summary, label="effect_size")

    if any(g.variance is None for g in groups):
        names = ", ".join(repr(label(g.name)) for g in groups if g.variance is None)
        summary.append(
            f"No effect size: {names} holds one row, so it has no spread and the difference has "
            f"no scale to be measured in. hypothesis_test will still compare them by rank."
        )
        return Output(headers=headers, rows=rows, summary=summary, label="effect_size")

    try:
        if len(groups) == 2:
            hedges_g(groups[0], groups[1])
        else:
            eta_squared(groups)
    except ZeroDivisionError as exc:
        summary.append(f"No effect size: {exc}. The group means are in the table above.")
        return Output(headers=headers, rows=rows, summary=summary, label="effect_size")

    if len(groups) == 2:
        g = hedges_g(groups[0], groups[1])
        raw = groups[0].mean - groups[1].mean
        # The groups arrive in SQL's order, which is alphabetical, not by size. Naming them in
        # that order would print "a exceeds b" whenever b is in fact larger -- a right number
        # inside a wrong sentence, which reads as authoritative and is not.
        higher, lower = ((groups[0], groups[1]) if raw > 0 else (groups[1], groups[0]))
        summary.append(
            f"Hedges' g {number(g)} — {band(g, G_BANDS)} by Cohen's convention. "
            f"{label(higher.name)} exceeds {label(lower.name)} by {number(abs(raw))}"
            f"{f' {unit}' if unit else ''}, which is {number(abs(g))} standard deviation(s)."
        )
        summary.append(
            "g rather than d: P10-D17 measured the small-sample correction at 0.9578 for ten "
            "rows per group and 0.9962 for a hundred, so it matters where group breakdowns "
            "actually land."
        )
    else:
        e = eta_squared(groups)
        summary.append(
            f"Eta squared {number(e)} — {band(e, ETA_BANDS)} by Cohen's convention. "
            f"{e:.1%} of the variation in {measure} lies between the groups of {dimension} "
            f"rather than within them."
        )

    summary.append(
        "Cohen's bands are a vocabulary, not a verdict: they were proposed for psychology and "
        "what counts as large is a question about the subject, not about the arithmetic."
    )
    summary.append(
        "An effect size does not fall as rows are added, which is the whole point of reading it "
        "beside a p-value rather than instead of one."
    )
    summary.append(_adds_back(excluded, scope, dimension, measure))
    return Output(headers=headers, rows=rows, summary=summary, label="effect_size")


def _association(con, gate, scope, dimension: str, second: str, summary: list[str]) -> Output:
    require_dimension(gate.contract, second)
    if dimension == second:
        raise ValueError(f"effect_size cannot size {dimension!r} against itself.")

    table = scope.source
    a, b = quote_identifier(dimension), quote_identifier(second)
    cells = con.execute(
        f"SELECT {a}, {b}, count(*) FROM {table} WHERE {scope.where} "
        f"AND {a} IS NOT NULL AND {b} IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    missing = con.execute(
        f"SELECT count(*) FROM {table} WHERE {scope.where} "
        f"AND ({a} IS NULL OR {b} IS NULL)"
    ).fetchall()[0][0]

    left = sorted({r[0] for r in cells})
    right = sorted({r[1] for r in cells})
    lookup = {(r[0], r[1]): r[2] for r in cells}
    grid = [[lookup.get((x, y), 0) for y in right] for x in left]
    counted = sum(sum(row) for row in grid)
    if counted + missing != scope.analysed:
        raise LostRows(
            f"effect_size lost rows: the table holds {counted:,} and {missing:,} have no "
            f"{dimension} or no {second}, against {scope.analysed:,} in scope."
        )

    headers = [dimension] + [label(y) for y in right]
    rows: list[list[Any]] = [[label(x)] + [number(v) for v in row]
                             for x, row in zip(left, grid)]
    if missing:
        rows.append([_shown_pair(dimension, second)] + [None] * len(right))

    if len(left) < 2 or len(right) < 2:
        summary.append(
            f"No effect size: the table is {len(left)}x{len(right)}, and an association needs "
            f"at least two values of each. P10-D32: asking anyway returns a confident-looking "
            f"number built on zero degrees of freedom."
        )
        return Output(headers=headers, rows=rows, summary=summary, label="effect_size")

    result, smallest = chi_square(grid)
    v = math.sqrt(result.statistic / (counted * (min(len(left), len(right)) - 1)))
    summary.append(
        f"Cramer's V {number(v)} — {band(v, V_BANDS)} by Cohen's convention. Chi-square "
        f"{number(result.statistic)} on {result.df} df over {counted:,} row(s), rescaled to "
        f"[0, 1] so it does not grow with the table."
    )
    summary.append(result.note)
    summary.append(
        "V says how strongly the two dimensions move together, not which way and not why. "
        "cross_tab shows the shape; hypothesis_test says whether it is distinguishable from "
        "none."
    )
    if smallest < 5.0:
        summary.append(
            f"Smallest expected count {number(smallest)}, below 5: the chi-square V rests on is "
            f"unreliable at these counts, so read the number as indicative."
        )
    if missing:
        summary.append(
            f"{missing:,} analysed row(s) have no {dimension} or no {second}, shown as "
            f"{_shown_pair(dimension, second)} so the counts add back to "
            f"{scope.analysed:,}."
        )
    return Output(headers=headers, rows=rows, summary=summary, label="effect_size")


def _excluded(con, scope, dimension: str, measure: str) -> dict[str, int]:
    table = scope.source
    dim, col = quote_identifier(dimension), quote_identifier(measure)
    no_group, no_value, not_finite = con.execute(
        f"SELECT count(*) FILTER (WHERE {dim} IS NULL), "
        f"       count(*) FILTER (WHERE {dim} IS NOT NULL AND {col} IS NULL), "
        f"       count(*) FILTER (WHERE {dim} IS NOT NULL AND {col} IS NOT NULL "
        f"                        AND NOT ({FINITE.format(col=col)})) "
        f"FROM {table} WHERE {scope.where}"
    ).fetchall()[0]
    return {NO_GROUP: no_group, NO_VALUE: no_value, NOT_FINITE: not_finite}


def _adds_back(excluded: dict[str, int], scope, dimension: str, measure: str) -> str:
    parts = [f"{excluded[NO_GROUP]:,} with no {dimension}",
             f"{excluded[NO_VALUE]:,} with no {measure}"]
    if excluded[NOT_FINITE]:
        parts.append(f"{excluded[NOT_FINITE]:,} whose {measure} is not a finite number")
    return (f"Excluded and shown: {', '.join(parts)}. The counts add back to the "
            f"{scope.analysed:,} row(s) in scope.")
