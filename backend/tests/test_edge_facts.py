"""What happens at the edges the selector will actually hit. P10-O4 and its neighbours.

P10-D6 requires dropping nulls in SQL, and dropping nulls is how a dimension value ends up with
one surviving row. Welch divides by (n - 1) per group; the pooled form by (n1 + n2 - 2).
group_compare's own summary already says a one-row group has no stddev -- "a blank spread is not
zero" -- so the question is what a blank reaching each of the four branches produces: a refusal,
a nan, or a number.

Nothing here is asserted about scipy's behaviour that was not printed first. Run with -s.
"""

from __future__ import annotations

import math

import duckdb
import pytest
import scipy.stats as st


def say(label: str, value: object) -> None:
    print(f"    {label:<44} {value}")


def one_and_many():
    con = duckdb.connect()
    con.execute("CREATE TABLE t (g VARCHAR, x DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?)",
                    [("a", 5.0)] + [("b", v) for v in (1.0, 2.0, 3.0, 4.0, 5.0)])
    return con


# --- what SQL hands over ---------------------------------------------------------------

def test_a_one_row_group_has_no_spread():
    """DuckDB returns NULL, not 0 and not an error. So the six numbers the parametric path
    needs (P10-D3) arrive with one of them missing, as Python None."""
    con = one_and_many()
    rows = con.execute(
        "SELECT g, count(x), avg(x), stddev(x), var_samp(x) FROM t GROUP BY 1 ORDER BY 1"
    ).fetchall()
    con.close()
    for name, n, mean, sd, var in rows:
        say(f"group {name}", f"n={n} mean={mean} stddev={sd} var_samp={var}")
    assert rows[0][3] is None
    assert rows[0][4] is None
    assert rows[1][3] is not None


def test_a_missing_spread_reaches_the_parametric_path_as_none():
    """P10-D5 measured that None is loud rather than silent. Measured here on the call the
    selector would actually make, so the refusal can name the group instead of the exception."""
    with pytest.raises(TypeError):
        st.ttest_ind_from_stats(5.0, None, 1, 3.0, 1.5811388300841898, 5, equal_var=False)


def test_what_scipy_does_with_a_one_element_sequence():
    """If the engine ever passed sequences instead of statistics, this is what it would get.
    Printed rather than predicted."""
    try:
        res = st.ttest_ind([5.0], [1.0, 2.0, 3.0, 4.0, 5.0], equal_var=False)
        say("ttest_ind p with n1=1", res.pvalue)
        say("ttest_ind t with n1=1", res.statistic)
        say("df", getattr(res, "df", None))
    except Exception as exc:  # noqa: BLE001 - the point is which one
        say("ttest_ind raised", f"{type(exc).__name__}: {exc}")


# --- the other three branches ------------------------------------------------------------

def test_what_anova_does_with_a_one_element_group():
    """The closed form (P10-D26) divides the within-group sum of squares by (N - k) and takes
    (n - 1) * var per group, so a group of one contributes nothing and costs a degree of
    freedom. Whether f_oneway agrees is the question."""
    try:
        res = st.f_oneway([5.0], [1.0, 2.0, 3.0], [4.0, 6.0, 8.0])
        say("f_oneway F", res.statistic)
        say("f_oneway p", res.pvalue)
    except Exception as exc:  # noqa: BLE001
        say("f_oneway raised", f"{type(exc).__name__}: {exc}")


def test_chi2_on_a_single_row_table():
    """A dimension with one surviving value makes a 1 x k table: zero degrees of freedom and
    no comparison to make. Measured rather than assumed to raise."""
    try:
        chi2, p, dof, expected = st.chi2_contingency([[10, 20, 30]])
        say("1 x 3 chi2 / dof", f"{chi2} / {dof}")
        say("1 x 3 p", p)
    except Exception as exc:  # noqa: BLE001
        say("1 x 3 raised", f"{type(exc).__name__}: {exc}")


def test_the_rank_test_survives_a_group_of_one():
    """The asymmetry that matters for the selection rule: U needs no variance, so the branch
    the parametric path cannot take is the one the rank path takes without complaint."""
    res = st.mannwhitneyu([5.0], [1.0, 2.0, 3.0, 4.0, 6.0], method="asymptotic")
    say("mannwhitneyu U with n1=1", res.statistic)
    say("mannwhitneyu p with n1=1", res.pvalue)
    assert not math.isnan(res.pvalue)
