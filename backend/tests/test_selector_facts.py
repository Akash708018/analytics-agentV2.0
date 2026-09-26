"""Ground facts for the hypothesis_test selector. Measured, not derived on paper.

Step 1 settled that the two-group parametric path survives the trip through SQL (P10-D3). This
file asks the same question of the three paths that remain, because the selector cannot be
written until each one is known to hold:

  k > 2 groups of a measure   one-way ANOVA from (n, mean, var) per group against f_oneway
  two dimensions             chi-square on a contingency table built by SQL
  a rank test                U from SQL midrank sums against mannwhitneyu (this is P10-O1)

If the third holds, Tier 6 never materialises a column and the convention 22 modules keep extends
to rank tests. If it does not, P10-O1 becomes a row budget and an explicit refusal.

Python lists and DuckDB only -- no numpy (P10-D22). Run with -s.
"""

from __future__ import annotations

import math
import statistics

import duckdb
import scipy.stats as st

TIED = ([1.0] * 20 + [9.0] * 80, [8.0] * 50 + [10.0] * 50)
UNTIED = ([1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0], [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.5])


def say(label: str, value: object) -> None:
    print(f"    {label:<44} {value}")


def loaded(groups: dict[str, list[float]]):
    """A connection holding one two-column table, the shape every analysis reads."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t (g VARCHAR, x DOUBLE)")
    con.executemany(
        "INSERT INTO t VALUES (?, ?)",
        [(name, v) for name, values in groups.items() for v in values],
    )
    return con


def anova_from_stats(stats: list[tuple[int, float, float]]) -> tuple[float, float, int, int]:
    """F and p from (n, mean, sample variance) per group. No raw values anywhere."""
    total = sum(n for n, _, _ in stats)
    k = len(stats)
    grand = sum(n * mean for n, mean, _ in stats) / total
    between = sum(n * (mean - grand) ** 2 for n, mean, _ in stats)
    within = sum((n - 1) * var for n, _, var in stats)
    df_b, df_w = k - 1, total - k
    f = (between / df_b) / (within / df_w)
    return f, float(st.f.sf(f, df_b, df_w)), df_b, df_w


def u_from_rank_sum(r1: float, n1: int) -> float:
    """U for the first group, from its midrank sum. Mann and Whitney's identity."""
    return r1 - n1 * (n1 + 1) / 2


def u_pvalue(u: float, n1: int, n2: int, tie_term: float, continuity: bool) -> float:
    """Two-sided normal approximation with the tie correction, as the textbook writes it."""
    total = n1 + n2
    mu = n1 * n2 / 2
    variance = (n1 * n2 / 12) * ((total + 1) - tie_term / (total * (total - 1)))
    numerator = abs(u - mu) - (0.5 if continuity else 0.0)
    if numerator <= 0:
        return 1.0
    return 2 * float(st.norm.sf(numerator / math.sqrt(variance)))


# --- k > 2 groups of a measure ---------------------------------------------------------------

def test_anova_from_group_stats_matches_f_oneway():
    """Three groups, F and p from nine numbers against scipy reading every value."""
    groups = {
        "a": [11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
        "b": [10.0, 10.5, 13.0, 15.5, 16.0, 19.0],
        "c": [9.0, 9.5, 10.0, 10.5, 11.0, 30.0],
    }
    stats = [(len(v), statistics.fmean(v), statistics.variance(v)) for v in groups.values()]
    f_ours, p_ours, df_b, df_w = anova_from_stats(stats)
    theirs = st.f_oneway(*groups.values())
    say("F ours / theirs", f"{f_ours} / {theirs.statistic}")
    say("p ours / theirs", f"{p_ours} / {theirs.pvalue}")
    say("df between, within", f"{df_b}, {df_w}")
    assert abs(f_ours - theirs.statistic) < 1e-9
    assert abs(p_ours - theirs.pvalue) < 1e-12


def test_anova_stats_survive_the_trip_through_duckdb():
    """The same, with the nine numbers computed by SQL. var_samp is the sample form or the
    within-group sum of squares is wrong by (n-1)/n per group."""
    groups = {
        "a": [11.0, 12.0, 13.0, 14.0, 15.0, 16.0],
        "b": [10.0, 10.5, 13.0, 15.5, 16.0, 19.0],
        "c": [9.0, 9.5, 10.0, 10.5, 11.0, 30.0],
    }
    con = loaded(groups)
    rows = con.execute(
        "SELECT g, count(x), avg(x), var_samp(x), var_pop(x) FROM t GROUP BY 1 ORDER BY 1"
    ).fetchall()
    con.close()
    for name, n, mean, v_samp, v_pop in rows:
        say(f"SQL group {name}", f"n={n} mean={mean} var_samp={v_samp} var_pop={v_pop}")
        assert abs(v_samp - statistics.variance(groups[name])) < 1e-9
        assert v_samp != v_pop

    f_sql, p_sql, _, _ = anova_from_stats([(n, mean, var) for _, n, mean, var, _ in rows])
    theirs = st.f_oneway(*groups.values())
    say("p from SQL stats", p_sql)
    say("p from sequences", theirs.pvalue)
    say("absolute difference", abs(p_sql - theirs.pvalue))
    assert abs(p_sql - theirs.pvalue) < 1e-12


# --- two dimensions --------------------------------------------------------------------------

def test_a_sql_contingency_table_reaches_chi2_unchanged():
    """cross_tab already builds this shape. The test is that a table of counts assembled from
    GROUP BY, with its cells ordered by the two dimensions, is what chi2_contingency wants."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t (region VARCHAR, channel VARCHAR)")
    cells = {("north", "web"): 30, ("north", "shop"): 20,
             ("south", "web"): 14, ("south", "shop"): 36}
    con.executemany(
        "INSERT INTO t VALUES (?, ?)",
        [(r, c) for (r, c), count in cells.items() for _ in range(count)],
    )
    rows = con.execute(
        "SELECT region, channel, count(*) FROM t GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    con.close()
    regions = sorted({r for r, _, _ in rows})
    channels = sorted({c for _, c, _ in rows})
    lookup = {(r, c): n for r, c, n in rows}
    table = [[lookup[(r, c)] for c in channels] for r in regions]
    say("rows from SQL", rows)
    say("table", table)

    chi2, p, dof, expected = st.chi2_contingency(table)
    say("chi2 / dof", f"{chi2} / {dof}")
    say("p", p)
    say("smallest expected", min(v for row in expected for v in row))
    assert dof == (len(regions) - 1) * (len(channels) - 1)
    assert sum(sum(row) for row in table) == sum(cells.values())


def test_coalesce_reproduces_f_oneway_for_a_group_of_one():
    """f_oneway tolerates a one-element group and spends a degree of freedom on it. Our closed
    form takes (n - 1) * var per group, which is zero there -- but SQL returns NULL for the
    variance of one row, not zero, so coalesce is the bridge. Whether it lands on f_oneway's F
    is the question; nothing about a NULL becoming a 0 is obviously right."""
    groups = {"a": [5.0], "b": [1.0, 2.0, 3.0], "c": [4.0, 6.0, 8.0]}
    con = loaded(groups)
    rows = con.execute(
        "SELECT g, count(x), avg(x), var_samp(x), coalesce(var_samp(x), 0) "
        "FROM t GROUP BY 1 ORDER BY 1"
    ).fetchall()
    con.close()
    for name, n, mean, raw, filled in rows:
        say(f"SQL group {name}", f"n={n} mean={mean} var_samp={raw} coalesced={filled}")
    assert rows[0][3] is None
    assert rows[0][4] == 0

    f_ours, p_ours, df_b, df_w = anova_from_stats([(n, mean, filled)
                                                   for _, n, mean, _, filled in rows])
    theirs = st.f_oneway(*groups.values())
    say("F ours / theirs", f"{f_ours} / {theirs.statistic}")
    say("p ours / theirs", f"{p_ours} / {theirs.pvalue}")
    say("df between, within", f"{df_b}, {df_w}")
    assert abs(f_ours - theirs.statistic) < 1e-9
    assert abs(p_ours - theirs.pvalue) < 1e-12
