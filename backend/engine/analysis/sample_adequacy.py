"""What this scope could have detected, which is not the same as what it found.

A non-significant result has two readings and the p-value does not distinguish them: the groups
are alike, or there were not enough rows to tell. `hypothesis_test` says a difference was not
detectable. This says how large a difference would have had to be.

**It does not report observed power, and that is a decision rather than an omission.** P10-D18
measured post-hoc power against the p-value at n=30 per group across eight effect sizes: as d ran
0.1 to 0.8 the p-value fell 0.699953 to 0.002999 and power rose 0.066783 to 0.861423, monotone
throughout. Power computed from the effect you observed is the p-value rearranged. Printing both
would look like two findings and be one.

Two groups only. The k>2 analogue needs `FTestAnovaPower`, unmeasured here, so it is refused by
name rather than approximated.
"""

from __future__ import annotations

import math
from typing import Any

from .base import NO_MEMBER, LostRows, TooManyGroups, label, number, relation_types
from .declared import column_types, is_numeric, require_dimension, require_measure
from .inferential import detectable_effect, group_stats, rows_for_effect
from .registry import Output, register
from .stats import MAX_GROUPS

NO_VALUE = "no_value"        # a key, not a label
NO_GROUP = "no_group"        # see _shown() below
NOT_FINITE = "not_finite"

# Cohen's conventions, used here as a scale to quote rows against rather than as thresholds.
TARGETS = ((0.2, "small"), (0.5, "medium"), (0.8, "large"))


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
    "sample_adequacy",
    tier=6,
    summary="How large a difference the rows in scope could have detected, in standard "
            "deviations and in the measure's own units, with the rows that would be needed "
            "for smaller ones. Never observed power, which restates the p-value.",
    selects=True,
)
def sample_adequacy(con, gate, scope, dimension: str, measure: str,
                    power: float = 0.8, alpha: float = 0.05, **params) -> Output:
    if params:
        raise TypeError(
            f"sample_adequacy takes dimension, measure, power and alpha; "
            f"got {', '.join(sorted(params))}."
        )
    if not 0.5 <= power < 1.0:
        raise ValueError(
            f"power={power} is outside [0.5, 1.0). 0.8 is the convention; below 0.5 the test is "
            f"likelier to miss a real difference than to find it."
        )
    if not 0.0 < alpha < 0.5:
        raise ValueError(f"alpha={alpha} is outside (0.0, 0.5). 0.05 is the convention.")

    require_dimension(gate.contract, dimension)
    m = require_measure(gate.contract, measure)
    unit = getattr(m, "unit", None) or ""
    if not is_numeric(relation_types(con, scope).get(measure, "")):
        raise ValueError(f"sample_adequacy needs a numeric measure; {measure!r} is not one.")

    summary = [scope.method_note(), *gate.caveats]
    if not scope.analysed:
        return Output(headers=["group", "n"], rows=[], label="sample_adequacy",
                      summary=summary + ["No rows are in scope, so there is nothing to assess."])

    groups = group_stats(con, scope, dimension, measure)
    if len(groups) > MAX_GROUPS:
        raise TooManyGroups(
            f"sample_adequacy of {measure} by {dimension} would be {len(groups)} group(s) "
            f"against a cap of {MAX_GROUPS}. top_n on {dimension} says which of its groups "
            f"matter.", dimension, measure
        )

    excluded = _excluded(con, scope, dimension, measure)
    assessed = sum(g.n for g in groups)
    if assessed + sum(excluded.values()) != scope.analysed:
        raise LostRows(
            f"sample_adequacy lost rows: {assessed:,} assessed, {excluded[NO_GROUP]:,} with no "
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

    if len(groups) != 2:
        summary.append(
            f"No assessment: {dimension} has {len(groups)} group(s) among the analysed rows. "
            f"This compares two. The version for more than two rests on FTestAnovaPower, which "
            f"nothing in this repository has measured, and an unmeasured number here would be a "
            f"claim about what your data could resolve."
        )
        return Output(headers=headers, rows=rows, summary=summary, label="sample_adequacy")

    a, b = groups
    if a.variance is None or b.variance is None:
        names = ", ".join(repr(label(g.name)) for g in groups if g.variance is None)
        summary.append(
            f"No assessment: {names} holds one row, so there is no spread to express a "
            f"detectable difference in. hypothesis_test will still compare the two by rank."
        )
        return Output(headers=headers, rows=rows, summary=summary, label="sample_adequacy")

    d = detectable_effect(a.n, b.n, alpha=alpha, power=power)
    pooled = math.sqrt(((a.n - 1) * a.variance + (b.n - 1) * b.variance) / (a.n + b.n - 2))
    in_units = d * pooled
    observed = abs(a.mean - b.mean)

    summary.append(
        f"At {a.n:,} and {b.n:,} row(s), {power:.0%} power and alpha {alpha:g}, the smallest "
        f"difference this scope could reliably detect is {number(d)} standard deviation(s) — "
        f"{number(in_units)}{f' {unit}' if unit else ''} of {measure}."
    )
    summary.append(
        f"The difference actually between the groups is {number(observed)}"
        f"{f' {unit}' if unit else ''}, which is "
        f"{'larger' if observed >= in_units else 'smaller'} than that floor."
        + ("" if observed >= in_units else
           " So a non-significant result here is a statement about the row count, not about "
           "the groups.")
    )
    needed = ", ".join(
        f"{rows_for_effect(target, alpha=alpha, power=power):,} for a {word} effect (d={target})"
        for target, word in TARGETS
    )
    summary.append(f"Rows per group needed at the same power: {needed}.")
    summary.append(
        "Observed power is not reported. P10-D18 measured it moving monotonically against the "
        "p-value across eight effect sizes at a fixed n, so power computed from the effect you "
        "found is the p-value rearranged — two numbers that look like two findings and are one."
    )
    summary.append(
        f"Excluded and shown: {excluded[NO_GROUP]:,} with no {dimension}, "
        f"{excluded[NO_VALUE]:,} with no {measure}"
        + (f", {excluded[NOT_FINITE]:,} not a finite number" if excluded[NOT_FINITE] else "")
        + f". The counts add back to the {scope.analysed:,} row(s) in scope."
    )
    return Output(headers=headers, rows=rows, summary=summary, label="sample_adequacy")


def _excluded(con, scope, dimension: str, measure: str) -> dict[str, int]:
    from ..util.sql_guard import quote_identifier
    from .inferential import FINITE

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
