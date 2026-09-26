"""How wide the uncertainty is around a number this scope produced.

`group_compare` gives the mean. This gives the range the mean is consistent with, which is the
difference between "12.4" and "12.4, and the rows cannot tell 11 from 14 apart".

Two shapes, both settled by measurement rather than convention:

  a measure, by group or overall   the t interval (P10-D16)
  a group's share of the rows      the Wilson interval (P10-D15)

P10-D16: the t and normal widths differ by 15% at n=10 and by 0.1% at n=1000, so the t form costs
nothing where it does not matter and is the honest one where it does. P10-D15: the Wald interval
returns (0.0, 0.0) at zero successes in fifty -- an interval that asserts certainty from absence.
Wilson does not.
"""

from __future__ import annotations

import math
from typing import Any

from scipy.stats import t as t_dist
from statsmodels.stats.proportion import proportion_confint

from ..util.sql_guard import quote_identifier
from .base import NO_MEMBER, LostRows, label, number, relation_types
from .declared import column_types, is_numeric, require_dimension, require_measure
from .inferential import FINITE, group_stats
from .registry import Output, register

NO_VALUE = "no_value"        # a key, not a label
NO_GROUP = "no_group"        # see _shown() below
NOT_FINITE = "not_finite"


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

@register(
    "confidence_interval",
    tier=6,
    summary="The range a mean or a share is consistent with, given how many rows produced it. "
            "The t interval for a measure, Wilson for a share, both named, with the width "
            "stated so a reader can see what the scope could and could not resolve.",
    selects=True,
)
def confidence_interval(con, gate, scope, dimension: str | None = None,
                        measure: str | None = None, confidence: float = 0.95,
                        **params) -> Output:
    if params:
        raise TypeError(
            f"confidence_interval takes dimension, measure and confidence; "
            f"got {', '.join(sorted(params))}."
        )
    if not 0.5 <= confidence < 1.0:
        raise ValueError(
            f"confidence={confidence} is outside [0.5, 1.0). 0.95 is the convention; below 0.5 "
            f"the interval is narrower than a coin toss and above 1.0 it is everything."
        )
    if dimension is None and measure is None:
        raise ValueError(
            "confidence_interval needs a measure to put an interval around, a dimension to put "
            "one around each group's share, or both."
        )

    summary = [scope.method_note(), *gate.caveats]
    if not scope.analysed:
        return Output(headers=["group", "n"], rows=[], label="confidence_interval",
                      summary=summary + ["No rows are in scope, so there is nothing to bound."])

    if measure is None:
        return _share_intervals(con, gate, scope, dimension, confidence, summary)
    return _mean_intervals(con, gate, scope, dimension, measure, confidence, summary)


def _mean_intervals(con, gate, scope, dimension: str | None, measure: str,
                    confidence: float, summary: list[str]) -> Output:
    """The t interval around a mean, per group or over the whole scope."""
    m = require_measure(gate.contract, measure)
    unit = getattr(m, "unit", None) or ""
    if not is_numeric(relation_types(con, scope).get(measure, "")):
        raise ValueError(f"confidence_interval bounds a numeric measure; {measure!r} is not one.")

    if dimension is None:
        groups = _whole_scope(con, scope, measure)
    else:
        require_dimension(gate.contract, dimension)
        groups = group_stats(con, scope, dimension, measure)

    excluded = _excluded(con, scope, dimension, measure)
    tested = sum(g.n for g in groups)
    if tested + sum(excluded.values()) != scope.analysed:
        raise LostRows(
            f"confidence_interval lost rows: {tested:,} bounded, {excluded[NO_GROUP]:,} with no "
            f"{dimension}, {excluded[NO_VALUE]:,} with no {measure}, "
            f"{excluded[NOT_FINITE]:,} not a finite number, against {scope.analysed:,} in scope."
        )

    pct = f"{confidence:.0%}"
    headers = ["group", "n", f"mean{f' ({unit})' if unit else ''}",
               f"low ({pct})", f"high ({pct})", "half-width"]
    rows: list[list[Any]] = []
    unbounded = 0
    for g in groups:
        if g.variance is None or g.n < 2:
            unbounded += 1
            rows.append([label(g.name), number(g.n), number(g.mean), None, None, None])
            continue
        half = t_dist.ppf(0.5 + confidence / 2.0, g.n - 1) * math.sqrt(g.variance / g.n)
        rows.append([label(g.name), number(g.n), number(g.mean),
                     number(g.mean - half), number(g.mean + half), number(half)])
    for key, count in excluded.items():
        if count:
            rows.append([_shown(key, dimension, measure), number(count),
                         None, None, None, None])

    summary.append(
        f"Interval: Student's t on n - 1 degrees of freedom, {pct}. Not the normal interval — "
        f"P10-D16 measured the two differing by 15% at ten rows and by 0.1% at a thousand, so "
        f"the t form costs nothing where the difference is invisible."
    )
    if unbounded:
        summary.append(
            f"{unbounded} group(s) have fewer than two rows and so have no spread and no "
            f"interval. The mean is still shown: one row does produce a mean, it just produces "
            f"no evidence about what the next row would be."
        )
    summary.append(
        f"A {pct} interval means the procedure that built it captures the true mean {pct} of the "
        f"time across repeated samples. It is not a {pct} probability that this particular "
        f"interval contains it."
    )
    summary.append(_adds_back(excluded, scope, dimension, measure))
    return Output(headers=headers, rows=rows, summary=summary, label="confidence_interval")


def _share_intervals(con, gate, scope, dimension: str, confidence: float,
                     summary: list[str]) -> Output:
    """The Wilson interval around each group's share of the scope's rows."""
    require_dimension(gate.contract, dimension)
    table = scope.source
    dim = quote_identifier(dimension)
    rows_sql = con.execute(
        f"SELECT {dim}, count(*) FROM {table} WHERE {scope.where} AND {dim} IS NOT NULL "
        f"GROUP BY 1 ORDER BY 1"
    ).fetchall()
    missing = con.execute(
        f"SELECT count(*) FROM {table} WHERE {scope.where} AND {dim} IS NULL"
    ).fetchall()[0][0]

    counted = sum(c for _, c in rows_sql)
    if counted + missing != scope.analysed:
        raise LostRows(
            f"confidence_interval lost rows: {counted:,} in groups and {missing:,} with no "
            f"{dimension}, against {scope.analysed:,} in scope."
        )

    pct = f"{confidence:.0%}"
    headers = ["group", "n", "share", f"low ({pct})", f"high ({pct})"]
    rows: list[list[Any]] = []
    for name, count in rows_sql:
        low, high = proportion_confint(count, scope.analysed, alpha=1.0 - confidence,
                                       method="wilson")
        rows.append([label(name), number(count), f"{count / scope.analysed:.1%}",
                     f"{low:.1%}", f"{high:.1%}"])
    if missing:
        rows.append([_shown(NO_GROUP, dimension), number(missing), None, None, None])

    summary.append(
        f"Interval: Wilson, {pct}, on each group's share of the {scope.analysed:,} analysed "
        f"rows. Not Wald — P10-D15 measured Wald returning (0.0, 0.0) at zero successes in "
        f"fifty, an interval asserting a rate is certainly zero on the strength of not having "
        f"seen it. Wilson stays inside [0, 1] and does not collapse."
    )
    summary.append(
        "The intervals are each group against the whole, so they do not sum to 100%. Overlapping "
        "intervals are not a test that two groups are the same; hypothesis_test is."
    )
    if missing:
        summary.append(
            f"{missing:,} analysed row(s) have no {dimension} and are shown as "
            f"{_shown(NO_GROUP, dimension)} so the counts add back to {scope.analysed:,}."
        )
    return Output(headers=headers, rows=rows, summary=summary, label="confidence_interval")


def _whole_scope(con, scope, measure: str):
    """One pseudo-group covering every usable row, for an interval with no breakdown."""
    from .inferential import GroupStats

    table = scope.source
    col = quote_identifier(measure)
    n, mean, var, skew, kurt = con.execute(
        f"SELECT count({col}), avg({col}), var_samp({col}), skewness({col}), kurtosis({col}) "
        f"FROM {table} WHERE {scope.where} AND {col} IS NOT NULL "
        f"AND {FINITE.format(col=col)}"
    ).fetchall()[0]
    return [GroupStats(name="(all)", n=n, mean=mean, variance=var,
                       skewness=skew, kurtosis=kurt)]


def _excluded(con, scope, dimension: str | None, measure: str) -> dict[str, int]:
    table = scope.source
    col = quote_identifier(measure)
    dim = quote_identifier(dimension) if dimension else None
    no_group_expr = f"{dim} IS NULL" if dim else "FALSE"
    present = f"{dim} IS NOT NULL AND " if dim else ""
    no_group, no_value, not_finite = con.execute(
        f"SELECT count(*) FILTER (WHERE {no_group_expr}), "
        f"       count(*) FILTER (WHERE {present}{col} IS NULL), "
        f"       count(*) FILTER (WHERE {present}{col} IS NOT NULL "
        f"                        AND NOT ({FINITE.format(col=col)})) "
        f"FROM {table} WHERE {scope.where}"
    ).fetchall()[0]
    return {NO_GROUP: no_group, NO_VALUE: no_value, NOT_FINITE: not_finite}


def _adds_back(excluded: dict[str, int], scope, dimension: str | None, measure: str) -> str:
    parts = [f"{excluded[NO_VALUE]:,} with no {measure}"]
    if dimension:
        parts.append(f"{excluded[NO_GROUP]:,} with no {dimension}")
    if excluded[NOT_FINITE]:
        parts.append(f"{excluded[NOT_FINITE]:,} whose {measure} is not a finite number")
    return (f"Excluded and shown: {', '.join(parts)}. The counts add back to the "
            f"{scope.analysed:,} row(s) in scope.")
