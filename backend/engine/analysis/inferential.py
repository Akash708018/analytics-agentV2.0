"""The inferential arithmetic, and the one place its SQL is spelled.

`stats.py` holds the seven expressions a *summary* reports. These are different numbers for a
different purpose -- count, mean, sample variance, skewness and kurtosis per group, computed so a
test can be run from them -- but they carry the same risk C9 measured, so they get the same
treatment: one home, and every Tier 6 analysis interpolates from here rather than spelling its
own.

**Nothing in this module receives a column.** Measured in Phase 10 Step 1 and Step 2: the
parametric path reproduces scipy's sequence form to 1.39e-16 from six numbers (P10-D3), and the
rank path reproduces `mannwhitneyu` exactly from midrank sums and tie-group sizes (P10-D23). All
22 analysis modules before this one read aggregates with `fetchall` and none materialises a
column; Tier 6 keeps that. scipy is used here for distribution tails only -- `t.sf`, `f.sf`,
`norm.sf` -- each of which takes scalars.

**Why the midrank is spelled `rank() + (tie - 1) / 2.0`.** P10-D25: `rank()` alone returns the
minimum rank inside a tie group, which gives a smaller rank sum, a smaller U and a p-value that is
merely plausible. Nothing raises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import chi2 as chi2_dist
from scipy.stats import f as f_dist
from scipy.stats import norm, t as t_dist

from statsmodels.stats.power import tt_ind_solve_power

from ..util.sql_guard import quote_identifier

__all__ = [
    "GroupStats", "TestResult", "group_stats", "welch", "one_way_anova",
    "mann_whitney", "kruskal_wallis", "chi_square", "hedges_g", "eta_squared", "p_text",
    "detectable_effect", "rows_for_effect",
]

# Below this the float underflows and a cell would read "0". P10-D13: measured at t = -386.7,
# scipy returns exactly 0.0, and a cell claiming p = 0 claims a certainty no test delivers.
P_FLOOR = 1e-300

# P10-O2: a DOUBLE column can hold NaN and infinity. Defined in stats.py since Phase 14 Step 7,
# where the descriptive analyses need it too (P14-O6); imported here under its old name.
from .stats import FINITE  # noqa: E402


@dataclass(frozen=True)
class GroupStats:
    """One group's inputs to a test. `variance` is None when the group holds one row."""

    name: object
    n: int
    mean: float | None
    variance: float | None
    skewness: float | None
    kurtosis: float | None

    @property
    def sd(self) -> float | None:
        return None if self.variance is None else math.sqrt(self.variance)


@dataclass(frozen=True)
class TestResult:
    """What a test came to, in the words the summary will use."""

    name: str
    statistic: float
    p: float
    df: str
    note: str


def p_text(p: float) -> str:
    """A p-value as a cell. P10-D13: never the string "0"."""
    if p <= 0.0 or p < P_FLOOR:
        return f"< {P_FLOOR:g}"
    return f"{p:.6g}"


def group_stats(con, scope, dimension: str, measure: str) -> list[GroupStats]:
    """Count, mean, sample variance, skewness and kurtosis per group, nulls excluded.

    P10-D6: rows with no value in the measure are dropped here, in SQL, and the caller reports
    how many -- a test that silently used fewer rows than its scope is what `LostRows` exists to
    refuse. Rows with no *dimension* are excluded too and counted separately by the caller: a
    (null) group is not a group somebody chose to compare.

    P10-D27: `var_samp` is the sample form. A one-row group returns NULL for it rather than 0
    (P10-D30), and that NULL is left as None rather than coalesced, because only the caller knows
    whether its branch can proceed without it.
    """
    table = scope.source
    dim = quote_identifier(dimension)
    col = quote_identifier(measure)
    rows = con.execute(
        f"SELECT {dim}, count({col}), avg({col}), var_samp({col}), "
        f"skewness({col}), kurtosis({col}) "
        f"FROM {table} WHERE {scope.where} AND {dim} IS NOT NULL "
        f"AND {col} IS NOT NULL AND {FINITE.format(col=col)} "
        f"GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return [GroupStats(name=r[0], n=r[1], mean=r[2], variance=r[3],
                       skewness=r[4], kurtosis=r[5]) for r in rows]


def welch(a: GroupStats, b: GroupStats) -> TestResult:
    """Welch's unequal-variance t-test from six numbers.

    P10-D1: scipy's `ttest_ind` defaults to the pooled Student form, and on one measured shape
    the two disagree across the conventional threshold -- 0.301 against 0.046 on the same data.
    The engine runs Welch and names it, because "t-test" does not identify which ran.

    P10-D2: df is fractional and is reported.
    """
    var_a, var_b = a.variance, b.variance
    if var_a is None or var_b is None:
        raise ValueError("welch needs a variance for both groups")
    sa, sb = var_a / a.n, var_b / b.n
    denominator = math.sqrt(sa + sb)
    if denominator == 0.0:
        raise ZeroDivisionError("both groups have zero variance")
    t = (a.mean - b.mean) / denominator
    df = (sa + sb) ** 2 / (sa * sa / (a.n - 1) + sb * sb / (b.n - 1))
    p = 2.0 * float(t_dist.sf(abs(t), df))
    return TestResult(
        name="Welch's unequal-variance t-test (two-sided)",
        # "2e+06" read as notation, not as a count of degrees of freedom (Step 13 benchmark);
        # below 1,000 the four significant figures stay (1.471, not 1.5: the regression's
        # control caught the one-decimal form losing them)
        statistic=t, p=p, df=f"{df:,.1f}" if df >= 1000 else f"{df:.4g}",
        note="Welch rather than Student: the groups are not assumed to share a variance.",
    )


def one_way_anova(groups: list[GroupStats]) -> TestResult:
    """F and p from (n, mean, variance) per group.

    P10-D26: scipy ships no `f_oneway_from_stats`, so this arithmetic is the engine's own and was
    measured against `f_oneway` to 0.0 exactly. P10-D33: a one-row group contributes nothing to
    the within-group sum of squares and costs a degree of freedom, which is what `f_oneway` does,
    so its NULL variance is coalesced to zero *here* and only here.
    """
    total = sum(g.n for g in groups)
    k = len(groups)
    grand = sum(g.n * g.mean for g in groups) / total
    between = sum(g.n * (g.mean - grand) ** 2 for g in groups)
    within = sum((g.n - 1) * (g.variance or 0.0) for g in groups)
    df_b, df_w = k - 1, total - k
    if within == 0.0:
        raise ZeroDivisionError("every group has zero variance")
    f = (between / df_b) / (within / df_w)
    return TestResult(
        name="one-way ANOVA (F test)",
        statistic=f, p=float(f_dist.sf(f, df_b, df_w)), df=f"{df_b}, {df_w}",
        note=f"{k} groups compared at once, rather than {k * (k - 1) // 2} pairwise tests.",
    )


def mann_whitney(con, scope, dimension: str, measure: str,
                 first: object, second: object) -> TestResult:
    """U and its tie-corrected p-value, computed entirely in SQL.

    P10-D23 and P10-D24, measured against `mannwhitneyu` to every printed digit on an untied pair
    and on a pair whose tie term is 769,800: U from midrank sums, the normal approximation with
    the tie correction, and the continuity correction applied.

    Needs no variance, which is why this is the branch a one-row group can still take (P10-D30).
    """
    table = scope.source
    dim = quote_identifier(dimension)
    col = quote_identifier(measure)
    where = (f"{scope.where} AND {dim} IS NOT NULL AND {col} IS NOT NULL "
             f"AND {FINITE.format(col=col)}")
    rank_sum, n1 = con.execute(
        f"WITH ranked AS ("
        f"  SELECT {dim} AS g, rank() OVER (ORDER BY {col}) AS r_min, "
        f"         count(*) OVER (PARTITION BY {col}) AS tie "
        f"  FROM {table} WHERE {where}) "
        f"SELECT sum(r_min + (tie - 1) / 2.0), count(*) FROM ranked WHERE g = ?",
        [first],
    ).fetchall()[0]
    tie_term, total = con.execute(
        f"SELECT coalesce(sum(c::HUGEINT * c * c - c), 0), sum(c) FROM ("
        f"  SELECT count(*) AS c FROM {table} WHERE {where} GROUP BY {col})"
    ).fetchall()[0]

    n2 = total - n1
    u = float(rank_sum) - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    variance = (n1 * n2 / 12.0) * ((total + 1) - float(tie_term) / (total * (total - 1)))
    if variance <= 0.0:
        raise ZeroDivisionError("every value is tied, so the ranks carry no information")
    z = (abs(u - mu) - 0.5) / math.sqrt(variance)
    return TestResult(
        name="Mann-Whitney U (two-sided, normal approximation, tie-corrected)",
        statistic=u, p=2.0 * float(norm.sf(z)), df="n/a",
        note=(f"Tests whether a draw from {first!r} tends to exceed one from {second!r}. "
              f"P10-D12: it is not a test of medians -- two groups with the same median can "
              f"separate here."),
    )


def chi_square(table: list[list[int]]) -> tuple[TestResult, float]:
    """Chi-square of independence, with Yates stated and the smallest expected count returned.

    P10-D9: `chi2_contingency` applies Yates' continuity correction by default on a 2x2, which
    moved one measured p-value from 0.00918 to 0.01904 -- across 0.01. The correction is named
    rather than left implicit.

    P10-D10: scipy returns expected frequencies and refuses nothing for being small, so the
    caller screens. P10-D32: it also returns chi2 0.0, dof 0 and p 1.0 for a single-row table,
    which is why the caller checks the shape before calling this at all.
    """
    from scipy.stats import chi2_contingency

    two_by_two = len(table) == 2 and len(table[0]) == 2
    chi2, p, dof, expected = chi2_contingency(table, correction=two_by_two)
    smallest = min(v for row in expected for v in row)
    note = ("Yates' continuity correction is applied, as it is on any 2x2."
            if two_by_two else
            f"No continuity correction: it applies only to a 2x2, and this is "
            f"{len(table)}x{len(table[0])}.")
    return TestResult(name="chi-square test of independence", statistic=chi2, p=p,
                      df=str(dof), note=note), smallest


def hedges_g(a: GroupStats, b: GroupStats) -> float:
    """The standardised difference, bias-corrected.

    P10-D17: J is 0.9578 at ten per group and 0.9962 at a hundred, so the correction matters at
    the sizes a group breakdown actually produces and costs nothing at the sizes it does not.
    Undefined at zero pooled variance and raised rather than returned as inf.
    """
    pooled = math.sqrt(((a.n - 1) * a.variance + (b.n - 1) * b.variance) / (a.n + b.n - 2))
    if pooled == 0.0:
        raise ZeroDivisionError("both groups are constant, so the difference has no scale")
    d = (a.mean - b.mean) / pooled
    return d * (1.0 - 3.0 / (4.0 * (a.n + b.n - 2) - 1.0))


def eta_squared(groups: list[GroupStats]) -> float:
    """Share of the measure's variation that lies between groups rather than within them."""
    total = sum(g.n for g in groups)
    grand = sum(g.n * g.mean for g in groups) / total
    between = sum(g.n * (g.mean - grand) ** 2 for g in groups)
    within = sum((g.n - 1) * (g.variance or 0.0) for g in groups)
    return between / (between + within) if (between + within) else 0.0


def kruskal_wallis(con, scope, dimension: str, measure: str) -> TestResult:
    """H and its p-value for more than two groups, from the same midrank sums as `mann_whitney`.

    P10-O6 closed. The rank branch stopped at two groups because Kruskal-Wallis was not built and
    approximating it would have been worse than refusing. The arithmetic is one line past what
    P10-D23 already computes: H = 12/(N(N+1)) * sum(R_i^2 / n_i) - 3(N+1), divided by the tie
    correction 1 - sum(t^3 - t)/(N^3 - N), read against chi-square on k - 1.

    Needs no variance, which is what makes it the rank branch's answer to a one-row group.
    """
    table = scope.source
    dim = quote_identifier(dimension)
    col = quote_identifier(measure)
    where = (f"{scope.where} AND {dim} IS NOT NULL AND {col} IS NOT NULL "
             f"AND {FINITE.format(col=col)}")
    rows = con.execute(
        f"WITH ranked AS ("
        f"  SELECT {dim} AS g, rank() OVER (ORDER BY {col}) AS r_min, "
        f"         count(*) OVER (PARTITION BY {col}) AS tie "
        f"  FROM {table} WHERE {where}) "
        f"SELECT g, sum(r_min + (tie - 1) / 2.0), count(*) FROM ranked GROUP BY 1 ORDER BY 1"
    ).fetchall()
    tie_term, total = con.execute(
        f"SELECT coalesce(sum(c::HUGEINT * c * c - c), 0), sum(c) FROM ("
        f"  SELECT count(*) AS c FROM {table} WHERE {where} GROUP BY {col})"
    ).fetchall()[0]

    k, n = len(rows), int(total)
    if n < 2:
        raise ZeroDivisionError("a rank test needs at least two values")
    h = (12.0 / (n * (n + 1))) * sum(float(r) ** 2 / c for _, r, c in rows) - 3.0 * (n + 1)
    correction = 1.0 - float(tie_term) / (n ** 3 - n)
    if correction <= 0.0:
        raise ZeroDivisionError("every value is tied, so the ranks carry no information")
    h /= correction
    return TestResult(
        name="Kruskal-Wallis H (tie-corrected)",
        statistic=h, p=float(chi2_dist.sf(h, k - 1)), df=str(k - 1),
        note=(f"The rank test for {k} groups. Like Mann-Whitney it compares whole "
              f"distributions rather than means, and needs no variance."),
    )


def detectable_effect(n1: int, n2: int, alpha: float = 0.05, power: float = 0.8) -> float:
    """The smallest standardised difference these group sizes could have detected.

    P10-D18: the useful direction. Post-hoc power computed from the observed effect was measured
    monotone against the p-value, so it restates what the reader already has; this asks what the
    rows were capable of resolving, which the p-value does not say.

    `tt_ind_solve_power` requires exactly one unknown (P10-D21) and it is the effect size here.
    """
    if n1 < 2 or n2 < 2:
        raise ValueError("a detectable effect needs at least two rows in each group")
    return float(tt_ind_solve_power(effect_size=None, nobs1=n1, alpha=alpha, power=power,
                                    ratio=n2 / n1, alternative="two-sided"))


def rows_for_effect(d: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Rows per group needed to detect a standardised difference of d, rounded up.

    P10-D18 again: 63.765610588911635 for d=0.5 at 80% power, reported as 64, because a
    fractional row count is not a recommendation.
    """
    if d <= 0.0:
        raise ValueError("an effect size to detect must be positive")
    return math.ceil(tt_ind_solve_power(effect_size=d, nobs1=None, alpha=alpha, power=power,
                                        ratio=1.0, alternative="two-sided"))
