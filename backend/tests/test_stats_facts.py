"""Ground facts for Tier 6 (inferential), measured on the installed scipy and statsmodels.

Nothing here is recalled. Every number this file prints was produced by a run recorded in
docs/decisions.md under "Phase 10, Step 1". Assertions are structural where the behaviour is
certain and bounded where the value is version-dependent; the exact values are printed either
way, because a bound that passes is not a measurement.

No numpy. Measured 18/09/2026: numpy and pandas are transitive dependencies of statsmodels and
are not imported anywhere in src/ or tests/, and the analysis tier reads every value with
fetchall -- 58 calls across 22 modules, all of them tuples. Testing scipy on numpy arrays would
measure a calling convention this engine does not use. Every input below is a Python list, which
is what the engine will actually hand over.

Run with -s. Without it the measurements are swallowed and only the assertions remain.
"""

from __future__ import annotations

import math
import random
import statistics

import duckdb
import pytest
import scipy
import scipy.stats as st
import statsmodels
from statsmodels.stats.power import tt_ind_solve_power
from statsmodels.stats.proportion import proportion_confint
from statsmodels.stats.weightstats import DescrStatsW


def say(label: str, value: object) -> None:
    """Print one measurement. The label is what the decisions entry will quote."""
    print(f"    {label:<44} {value}")


def spread(low: float, high: float, n: int) -> list[float]:
    """n evenly spaced values from low to high inclusive. linspace without the dependency."""
    if n == 1:
        return [low]
    step = (high - low) / (n - 1)
    return [low + step * i for i in range(n)]


def welch_df(s1: float, n1: int, s2: float, n2: int) -> float:
    """Welch-Satterthwaite, written out so the engine can compute it from SQL-side stats."""
    a, b = s1 * s1 / n1, s2 * s2 / n2
    return (a + b) ** 2 / (a * a / (n1 - 1) + b * b / (n2 - 1))


def cohens_d(x: list[float], y: list[float]) -> float:
    """Pooled-SD standardised mean difference. Plain floats, so a zero denominator raises."""
    n1, n2 = len(x), len(y)
    v1, v2 = statistics.variance(x), statistics.variance(y)
    pooled = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2))
    return (statistics.fmean(x) - statistics.fmean(y)) / pooled


def hedges_j(n1: int, n2: int) -> float:
    """Small-sample correction factor applied to d to get g."""
    df = n1 + n2 - 2
    return 1.0 - 3.0 / (4.0 * df - 1.0)


def lognormals(seed: int, n: int, sigma: float) -> list[float]:
    """Deterministic mildly-skewed sample from the standard library."""
    rng = random.Random(seed)
    return [rng.lognormvariate(0.0, sigma) for _ in range(n)]


# --- which test, and is it named -------------------------------------------------------------

def test_versions_are_recorded():
    say("scipy", scipy.__version__)
    say("statsmodels", statsmodels.__version__)
    say("duckdb", duckdb.__version__)
    assert scipy.__version__ and statsmodels.__version__


def test_ttest_ind_default_is_pooled_not_welch():
    """The default is a Student t-test assuming equal variance. On unequal n and unequal
    spread it is a different test from Welch, and neither scipy nor a caller reading
    'ttest_ind' is told which one ran."""
    x = spread(11.0, 13.0, 10)
    y = spread(0.0, 20.0, 40)
    default = st.ttest_ind(x, y)
    student = st.ttest_ind(x, y, equal_var=True)
    welch = st.ttest_ind(x, y, equal_var=False)
    say("default p", default.pvalue)
    say("student p", student.pvalue)
    say("welch p", welch.pvalue)
    say("default t / welch t", f"{default.statistic} / {welch.statistic}")
    assert default.pvalue == student.pvalue
    assert default.pvalue != welch.pvalue


def test_welch_df_is_fractional_and_our_formula_matches():
    """The engine reports df beside the test name, computed from group-level statistics, so our
    formula must agree with the one scipy used."""
    x = spread(11.0, 13.0, 10)
    y = spread(0.0, 20.0, 40)
    ours = welch_df(statistics.stdev(x), 10, statistics.stdev(y), 40)
    say("welch df (ours)", ours)
    assert abs(ours - round(ours)) > 1e-6
    theirs = getattr(st.ttest_ind(x, y, equal_var=False), "df", None)
    say("welch df (scipy)", theirs)
    if theirs is not None:
        assert abs(float(theirs) - ours) < 1e-9


def test_from_stats_reproduces_the_array_form():
    """ttest_ind_from_stats takes n, mean and sample sd -- exactly what count, avg and
    stddev_samp produce in SQL. If it agrees with the sequence form then hypothesis_test never
    materialises a column, which is the convention every other analysis module follows."""
    x = spread(11.0, 13.0, 10)
    y = spread(0.0, 20.0, 40)
    sequences = st.ttest_ind(x, y, equal_var=False)
    stats = st.ttest_ind_from_stats(
        statistics.fmean(x), statistics.stdev(x), 10,
        statistics.fmean(y), statistics.stdev(y), 40,
        equal_var=False,
    )
    say("sequence p", sequences.pvalue)
    say("from_stats p", stats.pvalue)
    say("absolute difference", abs(sequences.pvalue - stats.pvalue))
    assert abs(sequences.statistic - stats.statistic) < 1e-12
    assert abs(sequences.pvalue - stats.pvalue) < 1e-12


def test_paired_and_independent_disagree_on_the_same_data():
    """Same two columns, two tests, two answers. Choosing wrong is a silent wrong answer and
    not an error, which is why the engine decides pairing from the contract's grain and says
    so in the method note rather than inferring it from the shapes."""
    x = [float(i) for i in range(1, 11)]
    y = [v + 1.0 + (0.1 if i % 2 == 0 else -0.1) for i, v in enumerate(x)]
    paired = st.ttest_rel(x, y)
    independent = st.ttest_ind(x, y)
    say("paired p", paired.pvalue)
    say("independent p", independent.pvalue)
    assert paired.pvalue < independent.pvalue
    with pytest.raises(ValueError):
        st.ttest_rel(x, y[:-1])


# --- nulls ------------------------------------------------------------------------------------

def test_one_nan_propagates_silently():
    """nan_policy defaults to propagate. One nan in a hundred values returns nan for the
    statistic and nan for the p-value, with no warning and no refusal."""
    x = spread(0.0, 1.0, 99) + [float("nan")]
    y = spread(2.0, 3.0, 100)
    res = st.ttest_ind(x, y)
    say("statistic with one nan", res.statistic)
    say("p-value with one nan", res.pvalue)
    assert math.isnan(res.statistic)
    assert math.isnan(res.pvalue)


def test_nan_policy_omit_changes_n():
    """omit produces an answer, but the n it used is not the n the caller passed and scipy
    does not report the difference."""
    x = spread(0.0, 1.0, 99) + [float("nan")]
    y = spread(2.0, 3.0, 100)
    res = st.ttest_ind(x, y, nan_policy="omit")
    used = sum(1 for v in x if not math.isnan(v))
    say("p-value under omit", res.pvalue)
    say("values passed / values used", f"{len(x)} / {used}")
    assert not math.isnan(res.pvalue)


def test_nulls_cross_as_none_not_nan():
    """The engine reads with fetchall, so a DuckDB NULL arrives as Python None, not nan. What
    None does downstream decides whether the null rule is 'drop in SQL and report the count' or
    merely 'preferable'."""
    con = duckdb.connect()
    sql = "SELECT * FROM (VALUES (CAST(1.0 AS DOUBLE)), (NULL), (CAST(3.0 AS DOUBLE))) AS t(x)"
    raw = [row[0] for row in con.execute(sql).fetchall()]
    con.close()
    say("fetchall values", raw)
    say("middle value is None", raw[1] is None)
    assert raw[1] is None

    with pytest.raises(TypeError):
        [float(v) for v in raw]

    try:
        say("ttest_1samp on the raw list", st.ttest_1samp(raw, 0.0).pvalue)
    except (TypeError, ValueError) as exc:
        say("ttest_1samp raised", f"{type(exc).__name__}: {exc}")


# --- categorical --------------------------------------------------------------------------------

def test_chi2_2x2_applies_yates_by_default():
    """correction defaults to True and only affects 2x2. Two p-values from one table, and the
    function name does not distinguish them."""
    table = [[12, 18], [22, 8]]
    corrected = st.chi2_contingency(table)
    raw = st.chi2_contingency(table, correction=False)
    say("p with Yates", corrected[1])
    say("p without Yates", raw[1])
    assert corrected[1] != raw[1]
    assert corrected[1] > raw[1]


def test_chi2_does_not_check_expected_counts():
    """Every expected count below five and the function returns a p-value without comment.
    The assumption check is ours, from the expected table it hands back."""
    chi2, p, dof, expected = st.chi2_contingency([[1, 4], [4, 1]])
    smallest = min(v for row in expected for v in row)
    say("smallest expected count", smallest)
    say("p returned anyway", p)
    assert smallest < 5.0
    assert 0.0 <= p <= 1.0


def test_chi2_refuses_a_zero_margin():
    """An empty row or column is refused rather than returned as nan, so the engine translates
    a ValueError here instead of screening for it first."""
    with pytest.raises(ValueError):
        st.chi2_contingency([[10, 0], [5, 0]])


# --- rank tests ----------------------------------------------------------------------------------

def test_mannwhitney_exact_and_asymptotic_differ():
    """method='auto' picks exact for small untied samples and the normal approximation
    otherwise. The two give different p-values, so the method is part of the test's name."""
    x = [1.0, 3.0, 5.0, 7.0, 9.0]
    y = [2.0, 4.0, 6.0, 8.0, 12.0]
    exact = st.mannwhitneyu(x, y, method="exact")
    asymptotic = st.mannwhitneyu(x, y, method="asymptotic")
    say("exact p", exact.pvalue)
    say("asymptotic p", asymptotic.pvalue)
    assert exact.pvalue != asymptotic.pvalue


def test_mannwhitney_is_not_a_test_of_medians():
    """Identical medians, and the test rejects. Any interpretation line reading 'the median
    differs' is wrong; it tests whether one draw tends to exceed the other."""
    x = [1.0] * 20 + [9.0] * 80
    y = [8.0] * 50 + [10.0] * 50
    res = st.mannwhitneyu(x, y)
    say("median x / median y", f"{statistics.median(x)} / {statistics.median(y)}")
    say("mannwhitney p", res.pvalue)
    assert statistics.median(x) == statistics.median(y)
    assert res.pvalue < 0.05


# --- reporting ------------------------------------------------------------------------------------

def test_p_values_underflow_to_zero():
    """Separation large enough and the p-value is exactly 0.0 in float. A cell reading 'p = 0'
    claims certainty no test can deliver, so the renderer prints a floor instead."""
    x = spread(-1.0, 1.0, 1000)
    y = spread(9.0, 11.0, 1000)
    res = st.ttest_ind(x, y, equal_var=False)
    say("t", res.statistic)
    say("p", res.pvalue)
    say("p == 0.0 exactly", res.pvalue == 0.0)
    assert res.pvalue == 0.0


# --- assumptions ------------------------------------------------------------------------------

def test_shapiro_rejects_mild_skew_at_scale():
    """A normality test is a test of n as much as of shape. The same mild skew passes at 50
    values and is rejected at 2000, so a pass/fail verdict from shapiro is not an assumption
    check that survives contact with a real table."""
    small = lognormals(20260918, 50, 0.25)
    large = lognormals(20260918, 2000, 0.25)
    p_small = st.shapiro(small).pvalue
    p_large = st.shapiro(large).pvalue
    say("shapiro p at n=50", p_small)
    say("shapiro p at n=2000", p_large)
    say("skew at n=2000", st.skew(large))
    assert p_large < 0.05


def test_shapiro_warns_above_5000():
    """scipy itself says the p-value stops being accurate. Above the threshold the engine
    reports skew, kurtosis and n rather than a verdict."""
    rng = random.Random(20260918)
    big = [rng.gauss(0.0, 1.0) for _ in range(6000)]
    with pytest.warns(UserWarning):
        res = st.shapiro(big)
    say("shapiro p at n=6000", res.pvalue)


# --- intervals --------------------------------------------------------------------------------

def test_wald_interval_collapses_at_zero_successes():
    """proportion_confint defaults to the Wald interval, which is [0, 0] at zero successes:
    an interval asserting the rate is certainly zero, from fifty observations."""
    wald = proportion_confint(0, 50, method="normal")
    wilson = proportion_confint(0, 50, method="wilson")
    say("wald at 0/50", wald)
    say("wilson at 0/50", wilson)
    assert wald[0] == 0.0 and wald[1] == 0.0
    assert wilson[1] > 0.0


def test_t_and_z_intervals_diverge_at_small_n():
    """The normal interval is too narrow at the sizes a group breakdown produces, and
    indistinguishable at the sizes it does not."""
    rng = random.Random(20260918)
    for n in (10, 1000):
        sample = [rng.gauss(0.0, 1.0) for _ in range(n)]
        d = DescrStatsW(sample)
        lo_t, hi_t = d.tconfint_mean()
        lo_z, hi_z = d.zconfint_mean()
        ratio = (hi_t - lo_t) / (hi_z - lo_z)
        say(f"t-width / z-width at n={n}", ratio)
        assert ratio > 1.0


# --- effect size ------------------------------------------------------------------------------

def test_hedges_correction_at_small_n():
    """d is biased upward at small n. The correction is a few percent where group comparisons
    actually land and negligible where they do not, which is why it is applied always."""
    say("J at n=10 per group", hedges_j(10, 10))
    say("J at n=100 per group", hedges_j(100, 100))
    assert abs(hedges_j(10, 10) - 0.957746) < 1e-5
    assert hedges_j(100, 100) > 0.99


def test_cohens_d_is_undefined_at_zero_variance():
    """Two constant groups with different constants: the difference is real and the
    standardised difference does not exist. Refused, not returned as inf."""
    with pytest.raises(ZeroDivisionError):
        cohens_d([5.0] * 10, [7.0] * 10)


# --- adequacy ---------------------------------------------------------------------------------

def test_power_solve_requires_exactly_one_unknown():
    """One None or it raises. The engine solves for nobs1 and rounds up, because a fractional
    row count is not a recommendation."""
    n = tt_ind_solve_power(effect_size=0.5, alpha=0.05, power=0.8, nobs1=None)
    say("n per group for d=0.5, power=0.8", n)
    say("rounded up", math.ceil(n))
    assert 60.0 < n < 70.0
    with pytest.raises(ValueError):
        tt_ind_solve_power(effect_size=0.5, alpha=0.05, power=None, nobs1=None)


def test_post_hoc_power_is_a_restatement_of_p():
    """At fixed n, power computed from the observed effect moves monotonically against the
    p-value. It carries no information the p-value did not, which is why sample_adequacy
    reports the effect detectable at the observed n instead."""
    ps, powers = [], []
    for d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        p = st.ttest_ind_from_stats(d, 1.0, 30, 0.0, 1.0, 30, equal_var=True).pvalue
        power = tt_ind_solve_power(effect_size=d, nobs1=30, alpha=0.05, power=None)
        ps.append(p)
        powers.append(power)
        say(f"observed d={d}", f"p={p:.6f}  post-hoc power={power:.6f}")
    assert all(ps[i] > ps[i + 1] for i in range(len(ps) - 1))
    assert all(powers[i] < powers[i + 1] for i in range(len(powers) - 1))

# --- the bridge to this engine ------------------------------------------------------------

def test_duckdb_stddev_is_sample_not_population():
    """ttest_ind_from_stats wants the sample sd (ddof=1). analysis/stats.py already selects
    stddev(col) for its seven cells; if DuckDB's stddev were the population form, every
    from-stats test would be quietly wrong by a factor of sqrt((n-1)/n) -- 3% at n=20."""
    con = duckdb.connect()
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    con.execute("CREATE TABLE t (x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    sd, sd_samp, sd_pop = con.execute(
        "SELECT stddev(x), stddev_samp(x), stddev_pop(x) FROM t"
    ).fetchall()[0]
    con.close()
    say("duckdb stddev", sd)
    say("duckdb stddev_samp", sd_samp)
    say("duckdb stddev_pop", sd_pop)
    say("statistics.stdev", statistics.stdev(values))
    assert sd == sd_samp
    assert abs(sd - statistics.stdev(values)) < 1e-12
    assert sd != sd_pop


def test_sql_stats_reach_scipy_unchanged():
    """The whole parametric path in one test. DuckDB computes count, avg and stddev per group,
    six numbers cross into Python as a tuple, and the from-stats form has to agree with the
    sequence form on the same values. Every other analysis module reads this way; if this
    disagrees, hypothesis_test cannot follow the convention and Step 2 starts over."""
    con = duckdb.connect()
    left = spread(11.0, 13.0, 10)
    right = spread(0.0, 20.0, 40)
    con.execute("CREATE TABLE t (g VARCHAR, x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?)",
                    [("a", v) for v in left] + [("b", v) for v in right])
    rows = con.execute(
        "SELECT g, count(x), avg(x), stddev(x) FROM t GROUP BY 1 ORDER BY 1"
    ).fetchall()
    con.close()
    (_, n_a, mean_a, sd_a), (_, n_b, mean_b, sd_b) = rows
    say("from SQL: group a", f"n={n_a} mean={mean_a} sd={sd_a}")
    say("from SQL: group b", f"n={n_b} mean={mean_b} sd={sd_b}")

    from_sql = st.ttest_ind_from_stats(mean_a, sd_a, n_a, mean_b, sd_b, n_b, equal_var=False)
    from_lists = st.ttest_ind(left, right, equal_var=False)
    say("p from six SQL numbers", from_sql.pvalue)
    say("p from both sequences", from_lists.pvalue)
    say("absolute difference", abs(from_sql.pvalue - from_lists.pvalue))
    assert abs(from_sql.statistic - from_lists.statistic) < 1e-9
    assert abs(from_sql.pvalue - from_lists.pvalue) < 1e-9


def test_nan_reaches_a_cell_as_text():
    """base.number is what turns a computed value into a cell, and P8-D1 already anticipated
    inf and nan arriving in one. Measured here on the shape Tier 6 would produce: a p-value
    from nan_policy='propagate'. LostRows cannot catch it -- the row count is correct and the
    number is not -- so the null rule has to stop it upstream, in SQL."""
    from backend.engine.analysis.base import number

    say("number(nan)", repr(number(float("nan"))))
    say("number(inf)", repr(number(float("inf"))))
    say("number(0.05)", repr(number(0.05)))
    assert number(float("nan")) == "nan"
    assert number(float("inf")) == "inf"


def test_number_renders_a_float_to_four_places_and_a_decimal_to_its_own_scale():
    """P9-O5: number() has been handed a float since P9-D22 and no test in any Tier 3 file
    asserted how it renders one, so the decimal places in a mean column were unasserted.

    The Decimal contrast is the whole point of the function. A DECIMAL(18,2) column's 10.50 is
    two places because somebody declared two; a float's seventeen digits are an artifact of the
    type. stats.py owns no mean renderer, so base.number() is the home and there was nothing for
    seasonality to adopt. Measured 21/09/2026.
    """
    from decimal import Decimal

    from backend.engine.analysis.base import number

    for value, expected in (
        (10.0, "10"),
        (0.05, "0.05"),
        (2.5, "2.5"),
        (-0.125, "-0.125"),
        (1234567.89, "1,234,567.89"),
        (23.583333333333332, "23.5833"),
    ):
        say(f"number({value!r})", repr(number(value)))
        assert number(value) == expected, f"number({value!r}) rendered {number(value)!r}"

    # A float small enough to round away still has to fill a cell: rounding to four places
    # gives 0.0000, the rstrips leave "", and base.py's `text or "0"` makes it "0". So a
    # reader sees zero where the value was not zero. Pinned because it is invisible.
    say("number(1e-07)", repr(number(1e-07)))
    assert number(1e-07) == "0"

    say("number(Decimal('10.50'))", repr(number(Decimal("10.50"))))
    assert number(Decimal("10.50")) == "10.50", "a declared scale is not the float's to round"
    assert number(7) == "7"


# --- rank tests without leaving SQL -------------------------------------------------------

def test_duckdb_ranks_reproduce_the_mannwhitney_statistic():
    """U is arithmetic on ranks and DuckDB has rank(), so the question is whether the
    arithmetic agrees. Average ranks are needed, not minimum ranks: rank() returns the
    minimum of a tie group, and the average that U is defined on is rank() + (tie - 1)/2.

    If this holds, P10-O1 closes toward SQL -- mannwhitneyu never sees a column, the same way
    ttest_ind_from_stats never sees one (P10-D3)."""
    left = [1.0, 3.0, 5.0, 7.0, 9.0, 9.0, 11.0, 2.0, 4.0, 6.0]
    right = [2.0, 4.0, 6.0, 8.0, 9.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0]

    con = duckdb.connect()
    con.execute("CREATE TABLE t (g VARCHAR, x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?)",
                    [("a", v) for v in left] + [("b", v) for v in right])
    rows = con.execute(
        """
        WITH ranked AS (
          SELECT g,
                 rank() OVER (ORDER BY x) AS r_min,
                 count(*) OVER (PARTITION BY x) AS tie
          FROM t)
        SELECT g, sum(r_min + (tie - 1) / 2.0), count(*) FROM ranked GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    con.close()

    (_, rank_sum_a, n_a), (_, rank_sum_b, n_b) = rows
    u_a = float(rank_sum_a) - n_a * (n_a + 1) / 2.0
    u_b = float(rank_sum_b) - n_b * (n_b + 1) / 2.0
    theirs = st.mannwhitneyu(left, right, method="asymptotic")

    say("rank sum a / n", f"{rank_sum_a} / {n_a}")
    say("U from SQL ranks (a)", u_a)
    say("U from scipy (a)", theirs.statistic)
    say("U_a + U_b", u_a + u_b)
    say("n_a * n_b", n_a * n_b)
    assert abs(u_a - float(theirs.statistic)) < 1e-9
    assert abs((u_a + u_b) - n_a * n_b) < 1e-9


def test_the_tie_corrected_p_follows_from_two_more_sql_numbers():
    """The asymptotic p needs the tie-group sizes and nothing else from the rows. Whether our
    normal approximation lands on scipy's depends on its continuity convention, which is
    measured here rather than assumed: U is exact arithmetic and is asserted, p is printed."""
    left = [1.0, 3.0, 5.0, 7.0, 9.0, 9.0, 11.0, 2.0, 4.0, 6.0]
    right = [2.0, 4.0, 6.0, 8.0, 9.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0]

    con = duckdb.connect()
    con.execute("CREATE TABLE t (g VARCHAR, x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?)",
                    [("a", v) for v in left] + [("b", v) for v in right])
    rank_sum_a, n_a = con.execute(
        """
        WITH ranked AS (
          SELECT g,
                 rank() OVER (ORDER BY x) AS r_min,
                 count(*) OVER (PARTITION BY x) AS tie
          FROM t)
        SELECT sum(r_min + (tie - 1) / 2.0), count(*) FROM ranked WHERE g = 'a'
        """
    ).fetchall()[0]
    tie_term, total = con.execute(
        "SELECT coalesce(sum(c * c * c - c), 0), sum(c) FROM "
        "(SELECT count(*) AS c FROM t GROUP BY x)"
    ).fetchall()[0]
    con.close()

    n_b = total - n_a
    u_a = float(rank_sum_a) - n_a * (n_a + 1) / 2.0
    mu = n_a * n_b / 2.0
    variance = (n_a * n_b / 12.0) * ((total + 1) - float(tie_term) / (total * (total - 1)))
    sigma = math.sqrt(variance)
    z = (abs(u_a - mu) - 0.5) / sigma
    ours = 2.0 * st.norm.sf(z)
    theirs = st.mannwhitneyu(left, right, method="asymptotic")

    say("tie term sum(t^3 - t)", tie_term)
    say("sigma with tie correction", sigma)
    say("z with continuity", z)
    say("p from SQL numbers", ours)
    say("p from scipy", theirs.pvalue)
    say("absolute difference", abs(ours - theirs.pvalue))
    assert abs(ours - theirs.pvalue) < 1e-12


def test_the_tie_correction_carries_a_heavily_tied_sample():
    """The same SQL against 200 values across four distinct numbers: the tie term is 769800 and
    the correction is doing nearly all the work, not nudging a result the raw ranks already had.
    This is the sample P10-D12 used to show Mann-Whitney is not a test of medians, so its
    p-value is known from a different route."""
    left = [1.0] * 20 + [9.0] * 80
    right = [8.0] * 50 + [10.0] * 50

    con = duckdb.connect()
    con.execute("CREATE TABLE t (g VARCHAR, x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?)",
                    [("a", v) for v in left] + [("b", v) for v in right])
    rank_sum_a, n_a = con.execute(
        """
        WITH ranked AS (
          SELECT g,
                 rank() OVER (ORDER BY x) AS r_min,
                 count(*) OVER (PARTITION BY x) AS tie
          FROM t)
        SELECT sum(r_min + (tie - 1) / 2.0), count(*) FROM ranked WHERE g = 'a'
        """
    ).fetchall()[0]
    tie_term, total = con.execute(
        "SELECT coalesce(sum(c * c * c - c), 0), sum(c) FROM "
        "(SELECT count(*) AS c FROM t GROUP BY x)"
    ).fetchall()[0]
    con.close()

    n_b = total - n_a
    u_a = float(rank_sum_a) - n_a * (n_a + 1) / 2.0
    mu = n_a * n_b / 2.0
    variance = (n_a * n_b / 12.0) * ((total + 1) - float(tie_term) / (total * (total - 1)))
    z = (abs(u_a - mu) - 0.5) / math.sqrt(variance)
    ours = 2.0 * float(st.norm.sf(z))
    theirs = st.mannwhitneyu(left, right, method="asymptotic")

    say("tie term sum(t^3 - t)", tie_term)
    say("U from SQL ranks", u_a)
    say("p from SQL numbers", ours)
    say("p from scipy", theirs.pvalue)
    say("p from P10-D12", 0.010202431360127625)
    assert abs(u_a - float(theirs.statistic)) < 1e-9
    assert abs(ours - theirs.pvalue) < 1e-12
    assert abs(ours - 0.010202431360127625) < 1e-12


def test_rank_is_not_the_midrank():
    """The correction in the SQL above is not decoration. rank() returns the minimum of a tie
    group, and using it directly gives a smaller rank sum, a smaller U and a p-value that is
    merely plausible -- wrong in a direction nothing flags."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t (x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in [1.0, 2.0, 2.0, 2.0, 5.0]])
    rows = con.execute(
        """
        SELECT x, min(r_min) AS rank_fn, min(r_min + (tie - 1) / 2.0) AS midrank FROM (
          SELECT x, rank() OVER (ORDER BY x) AS r_min,
                 count(*) OVER (PARTITION BY x) AS tie FROM t
        ) GROUP BY x ORDER BY x
        """
    ).fetchall()
    con.close()
    say("x, rank(), midrank", rows)
    assert [r[1] for r in rows] == [1, 2, 5]
    assert [r[2] for r in rows] == [1.0, 3.0, 5.0]
