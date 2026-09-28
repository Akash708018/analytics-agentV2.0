"""Is the difference this table shows bigger than the noise it was drawn from.

`group_compare` says what a measure comes to per group. This says whether the groups differ by
more than sampling would produce on its own -- and, because that question has four shapes and
four answers, it names which test it ran every time.

**The shapes, and why they are the shapes.** Measured across Phase 10 Steps 1 to 3:

  one measure by a dimension, two groups     Welch's t, or Mann-Whitney on request
  one measure by a dimension, three or more  one-way ANOVA
  two dimensions                             chi-square of independence

**The caller chooses parametric or rank; the engine only overrides when the choice is not
available.** P10-D14 rules out the obvious alternative: a normality test is a test of n as much
as of shape -- the same mild skew passes shapiro at n=50 and is rejected at n=2000 -- so gating
on a normality p-value would send every large group to the rank branch and call it a finding. A
threshold on n or skew would be a number nobody in this repository measured. So `method` defaults
to `auto`, which means the parametric test unless a group has one row and therefore no variance
(P10-D30), and the summary always reports skew, kurtosis and n so a reader can judge the choice
that was made.

**Every excluded row is a row in the table.** P10-D6: fourteen modules raise `LostRows` when
their output does not account for `scope.analysed`, and a test that drops nulls has fewer rows
than its scope by construction. So the rows with no value and the rows with no group are counted
and shown, and they add back.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import NO_MEMBER, LostRows, TooManyGroups, label, number, relation_types
from .declared import column_types, is_numeric, require_dimension, require_measure
from .inferential import (FINITE, GroupStats, chi_square, eta_squared, group_stats,
                          hedges_g, kruskal_wallis,
                          mann_whitney, one_way_anova, p_text, welch)
from .registry import Output, register
from .stats import MAX_GROUPS

NO_VALUE = "no_value"        # a key, not a label
NO_GROUP = "no_group"        # see _shown() below
# P10-O2: NaN and infinity are not nulls and are not values. They get their own
# row because a reader who sees them counted separately can go and fix them.
NOT_FINITE = "not_finite"
METHODS = ("auto", "parametric", "rank")

# Below this an expected count makes the chi-square approximation unreliable. Pearson's rule,
# stated rather than enforced: P10-D10 measured that scipy returns a p-value for a table whose
# every expected count is 2.5 and says nothing about it, so the sentence is the engine's.
MIN_EXPECTED = 5.0


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
    "hypothesis_test",
    tier=6,
    summary="Whether groups of a declared dimension differ by more than sampling noise, "
            "naming the test it ran, the assumption it checked, and what the result means "
            "in words. Welch's t for two groups, one-way ANOVA for more, chi-square for two "
            "dimensions, Mann-Whitney on request or when a group has no variance.",
    selects=True,
)
def hypothesis_test(con, gate, scope, dimension: str, measure: str | None = None,
                    second_dimension: str | None = None, method: str = "auto",
                    **params) -> Output:
    if params:
        raise TypeError(
            f"hypothesis_test takes dimension, and either measure or second_dimension, "
            f"plus method; got {', '.join(sorted(params))}."
        )
    if (measure is None) == (second_dimension is None):
        raise ValueError(
            "hypothesis_test compares one measure across the groups of a dimension, or one "
            "dimension against another. Pass exactly one of measure, second_dimension."
        )
    if method not in METHODS:
        raise ValueError(
            f"method={method!r} is not one of {', '.join(METHODS)}."
        )

    contract = gate.contract
    require_dimension(contract, dimension)
    summary = [scope.method_note(), *gate.caveats]

    if not scope.analysed:
        return Output(headers=["group", "n"], rows=[], label="hypothesis_test",
                      summary=summary + ["No rows are in scope, so there is nothing to test."])

    if second_dimension is not None:
        return _independence(con, gate, scope, dimension, second_dimension, method, summary)
    return _difference(con, gate, scope, dimension, measure, method, summary)


def _difference(con, gate, scope, dimension: str, measure: str, method: str,
                summary: list[str]) -> Output:
    """One measure across the groups of one dimension."""
    m = require_measure(gate.contract, measure)
    unit = getattr(m, "unit", None) or ""
    if not is_numeric(relation_types(con, scope).get(measure, "")):
        raise ValueError(
            f"hypothesis_test compares a numeric measure; {measure!r} is not one. "
            f"cross_tab or a second dimension is how two categorical columns are compared."
        )

    groups = group_stats(con, scope, dimension, measure)
    if len(groups) > MAX_GROUPS:
        raise TooManyGroups(
            f"hypothesis_test of {measure} by {dimension} would be {len(groups)} group(s) "
            f"against a cap of {MAX_GROUPS}. top_n on {dimension} says which of its groups "
            f"matter.", dimension, measure
        )

    excluded = _excluded(con, scope, dimension, measure)
    tested = sum(g.n for g in groups)
    if (tested + excluded[NO_GROUP] + excluded[NO_VALUE]
            + excluded[NOT_FINITE] != scope.analysed):
        raise LostRows(
            f"hypothesis_test lost rows: {tested:,} tested, {excluded[NO_GROUP]:,} with no "
            f"{dimension}, {excluded[NO_VALUE]:,} with no {measure}, "
            f"{excluded[NOT_FINITE]:,} whose {measure} is not a finite number, against "
            f"{scope.analysed:,} in scope."
        )

    headers = ["group", "n", f"mean{f' ({unit})' if unit else ''}", "stddev", "skew"]
    rows: list[list[Any]] = [
        [label(g.name), number(g.n), number(g.mean), number(g.sd), number(g.skewness)]
        for g in groups
    ]
    for key, count in ((NO_GROUP, excluded[NO_GROUP]), (NO_VALUE, excluded[NO_VALUE]),
                       (NOT_FINITE, excluded[NOT_FINITE])):
        if count:
            rows.append([_shown(key, dimension, measure), number(count), None, None, None])

    if len(groups) < 2:
        summary.append(
            f"No test: {dimension} has {len(groups)} group(s) among the analysed rows, and a "
            f"comparison needs two. The counts above are still true."
        )
        return Output(headers=headers, rows=rows, summary=summary, label="hypothesis_test")

    forced = [g for g in groups if g.variance is None]
    try:
        result, effect, effect_name = _run_difference(con, scope, dimension, measure,
                                                      groups, method, forced)
    except ZeroDivisionError as exc:
        # inferential raises these on purpose, each naming why a statistic does not exist
        # (both groups constant, every value tied). They escaped as exceptions (Step 13: D2).
        summary.append(
            f"No test: {exc}. The group means are in the table above; a difference between "
            f"values that do not vary is not a question sampling can answer."
        )
        return Output(headers=headers, rows=rows, summary=summary, label="hypothesis_test")

    summary.append(
        f"Test: {result.name}. Statistic {number(result.statistic)}, df {result.df}, "
        f"p {p_text(result.p)}."
    )
    summary.append(result.note)
    if effect is not None:
        summary.append(
            f"Effect size: {effect_name} {number(effect)}. A p-value says the difference is "
            f"unlikely to be noise; this says how big it is."
        )
    if forced and method == "auto":
        names = ", ".join(repr(label(g.name)) for g in forced)
        summary.append(
            f"Method chosen, not requested: {names} holds one row and therefore has no spread, "
            f"so the parametric test has no denominator. The rank test needs no variance. Pass "
            f"method='parametric' to see the refusal instead."
        )
    summary.append(_assumption_line(groups))
    summary.append(_interpretation(result, dimension, measure))
    summary.append(
        f"{excluded[NO_VALUE]:,} analysed row(s) have no {measure} and are excluded from the "
        f"test, {excluded[NO_GROUP]:,} have no {dimension}. Both are rows in the table above, "
        f"so the counts add back to the {scope.analysed:,} in scope."
    )
    return Output(headers=headers, rows=rows, summary=summary, label="hypothesis_test")


def _run_difference(con, scope, dimension: str, measure: str, groups: list[GroupStats],
                    method: str, forced: list[GroupStats]):
    """Pick the branch, run it, and return the result with its effect size."""
    two = len(groups) == 2
    wants_rank = method == "rank" or (method == "auto" and forced)

    if wants_rank:
        if two:
            result = mann_whitney(con, scope, dimension, measure,
                                  groups[0].name, groups[1].name)
            return result, None, None
        # P10-O6 closed: H from the same midrank sums, measured against scipy.stats.kruskal.
        return kruskal_wallis(con, scope, dimension, measure), eta_squared(groups), "eta squared"

    if forced:
        names = ", ".join(repr(label(g.name)) for g in forced)
        raise ValueError(
            f"hypothesis_test cannot run a parametric test: {names} holds one row, so it has "
            f"no variance and the test has no denominator. method='rank' compares the same two "
            f"groups without needing one."
        )

    if two:
        return welch(groups[0], groups[1]), hedges_g(groups[0], groups[1]), "Hedges' g"
    return one_way_anova(groups), eta_squared(groups), "eta squared"


def _independence(con, gate, scope, dimension: str, second: str, method: str,
                  summary: list[str]) -> Output:
    """Two dimensions against each other."""
    require_dimension(gate.contract, second)
    if method == "rank":
        raise ValueError(
            "method='rank' compares a measure across groups. Two dimensions are compared by "
            "chi-square, which is already distribution-free."
        )
    if dimension == second:
        raise ValueError(
            f"hypothesis_test cannot test {dimension!r} against itself; the table would be "
            f"diagonal and the answer meaningless."
        )

    table_name = scope.source
    a, b = quote_identifier(dimension), quote_identifier(second)
    cells = con.execute(
        f"SELECT {a}, {b}, count(*) FROM {table_name} WHERE {scope.where} "
        f"AND {a} IS NOT NULL AND {b} IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    missing = con.execute(
        f"SELECT count(*) FROM {table_name} WHERE {scope.where} "
        f"AND ({a} IS NULL OR {b} IS NULL)"
    ).fetchall()[0][0]

    left = sorted({r[0] for r in cells})
    right = sorted({r[1] for r in cells})
    lookup = {(r[0], r[1]): r[2] for r in cells}
    table = [[lookup.get((x, y), 0) for y in right] for x in left]

    counted = sum(sum(row) for row in table)
    if counted + missing != scope.analysed:
        raise LostRows(
            f"hypothesis_test lost rows: the table holds {counted:,} and {missing:,} have no "
            f"{dimension} or no {second}, against {scope.analysed:,} in scope."
        )

    headers = [dimension] + [label(y) for y in right]
    rows: list[list[Any]] = [[label(x)] + [number(v) for v in row]
                             for x, row in zip(left, table)]
    if missing:
        rows.append([_shown_pair(dimension, second)] + [None] * len(right))

    if len(left) < 2 or len(right) < 2:
        # P10-D32: chi2_contingency returns chi2 0.0, dof 0 and p 1.0 here -- no error, no nan,
        # and a reader sees "no association" where the truth is that there is nothing to
        # compare. scipy will not refuse it, so this does.
        summary.append(
            f"No test: the table is {len(left)}x{len(right)}, and independence needs at least "
            f"two values of each. Asking anyway returns p = 1.0 on zero degrees of freedom, "
            f"which reads like a finding and is not one."
        )
        return Output(headers=headers, rows=rows, summary=summary, label="hypothesis_test")

    result, smallest = chi_square(table)
    summary.append(
        f"Test: {result.name}. Statistic {number(result.statistic)}, df {result.df}, "
        f"p {p_text(result.p)}."
    )
    summary.append(result.note)
    summary.append(
        f"Smallest expected count {number(smallest)}"
        + (f", below {MIN_EXPECTED:g}: the chi-square approximation is unreliable here and the "
           f"p-value should be read as indicative. Combining sparse categories is what fixes "
           f"it." if smallest < MIN_EXPECTED else
           f", above {MIN_EXPECTED:g}, so the approximation holds.")
    )
    summary.append(
        f"{'A' if result.p < 0.05 else 'No'} detectable association between {dimension} and "
        f"{second} at the conventional 5% threshold"
        + ("." if result.p < 0.05 else
           " -- which is not evidence they are independent, only that this many rows cannot "
           "tell.")
    )
    if missing:
        summary.append(
            f"{missing:,} analysed row(s) have no {dimension} or no {second} and are excluded "
            f"from the table, shown as {_shown_pair(dimension, second)} so the counts "
            f"add back to {scope.analysed:,}."
        )
    return Output(headers=headers, rows=rows, summary=summary, label="hypothesis_test")


def _excluded(con, scope, dimension: str, measure: str) -> dict[str, int]:
    """Rows in scope that no group holds, split by which column is missing."""
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


def _assumption_line(groups: list[GroupStats]) -> str:
    """P10-D14: skew, kurtosis and n -- never a normality verdict.

    Measured: the same lognormal shape gives shapiro 0.445 at n=50 and 1.89e-20 at n=2000. A
    pass/fail on that statistic is a statement about how many rows there are.
    """
    smallest = min(g.n for g in groups)
    skews = [abs(g.skewness) for g in groups if g.skewness is not None]
    worst = max(skews) if skews else None
    line = (f"Assumption reported, not judged: smallest group {smallest:,} row(s)"
            + (f", largest absolute skew {number(worst)}." if worst is not None else "."))
    if worst is not None and worst > 1.0 and smallest < 30:
        line += (" A skew above 1 in a group this small is where the t-test's assumption is "
                 "weakest; method='rank' makes no distributional assumption.")
    elif smallest >= 30:
        line += (" At this many rows per group the test on means is robust to skew, so the "
                 "skew column is context rather than a warning.")
    return line


def _interpretation(result, dimension: str, measure: str) -> str:
    """The plain-English sentence the Done-When asks for."""
    if result.p < 0.05:
        return (f"At the conventional 5% threshold, {measure} differs between the groups of "
                f"{dimension} by more than sampling alone would produce. That is a statement "
                f"about these rows, not a cause.")
    return (f"At the conventional 5% threshold, this many rows cannot distinguish {measure} "
            f"between the groups of {dimension}. That is not evidence they are the same -- "
            f"sample_adequacy says how large a difference this scope could have detected.")
