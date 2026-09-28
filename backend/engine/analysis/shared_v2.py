"""Tier 9: shared analyses added in v2 (B4). Generic and named; domain tools are presets on them.

Every number is computed in DuckDB SQL over the contract's scope (`scope.source`, which carries
declared ratios as STRUCT(n, d)), with the scope's WHERE, so the contract gate, exclusions and
approved filters apply unchanged. scipy is used only for distribution tails on scalars, as in
v1 (hypothesis_test). No numpy, no pandas.

  funnel                  ordered steps, a window, distinct entities reaching each step
  source_reconciliation   several sources' totals side by side; never added; overlap ratio
  ab_test                 per-variant rates + sample-ratio mismatch + test + MDE, or "not enough"
  pacing                  spent to date vs a person-entered budget; straight-line projection
  text_ngrams             words / word pairs of a text column, with a measure summed per n-gram
  key_overlap             keys served by several members that split the key's measure
  expected_rate_by_bucket the rate expected at each bucket of a numeric column; who falls short
  before_after_baseline   post vs a pre-period trend and the same period last year; correlational
  unit_economics          CAC per cohort, payback month, LTV:CAC to date; immature cohorts said
"""
from __future__ import annotations

import calendar
import math
from datetime import date, timedelta

from ..util.sql_guard import quote_identifier as qi
from .base import ParamsInvalid, label, number
from .declared import AGG_SQL, agg_of, require_dimension, require_measure
from .registry import Output, register

TIER = 9   # 8 is the guide's (unbuilt) forecasting tier
MIN_AB_DENOMINATOR = 100
SRM_ALPHA = 0.001


def _lit(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _agg(gate, name: str) -> tuple[str, str]:
    """(aggregate SQL, agg) for a declared measure; refused when it has no aggregate."""
    m = require_measure(gate.contract, name)
    agg = agg_of(m)
    if agg is None or agg == "none":
        raise ValueError(f"measure {name!r} declares no aggregate that combines rows "
                         f"(agg={agg!r}); declare sum, count or a ratio in the contract.")
    return AGG_SQL[agg].format(col=qi(name)), agg


def _additive(gate, name: str) -> str:
    sql, agg = _agg(gate, name)
    if agg not in ("sum", "count"):
        raise ValueError(f"{name!r} is agg={agg}; this analysis adds it across rows, which "
                         f"only a sum or a count allows.")
    return sql


def _date_col(gate) -> str:
    d = gate.contract.date_column
    if not d:
        raise ValueError("the contract declares no date column; this analysis needs one.")
    return d


def _pct(x) -> str | None:
    return None if x is None else f"{x * 100:.1f}%"


def _head(scope, gate) -> list[str]:
    return [scope.method_note()] + list(gate.caveats)


# --- funnel ------------------------------------------------------------------------------------

@register("funnel", tier=TIER, surface="v2", summary="Distinct entities reaching each of an ordered list of "
          "steps (values of a step column), each step after the previous, within a window.")
def funnel(con, gate, scope, entity: str, step_column: str, steps: list | str,
           window_days: int | None = None, **params) -> Output:
    if params:
        raise TypeError(f"funnel takes entity, step_column, steps, window_days; got "
                        f"{', '.join(sorted(params))}.")
    steps = [s.strip() for s in (steps.split(",") if isinstance(steps, str) else steps)]
    if len(steps) < 2:
        raise ParamsInvalid("a funnel needs at least two steps, in order.")
    require_dimension(gate.contract, step_column)
    ent, sc, dt = qi(entity), qi(step_column), qi(_date_col(gate))
    firsts = ", ".join(
        f"min(CASE WHEN {sc} = {_lit(s)} THEN {dt} END) AS s{i}" for i, s in enumerate(steps))
    # each step's time must be at or after the previous step's (ordered), and within the window
    conds, reach = [], []
    for i in range(len(steps)):
        if i:
            conds.append(f"s{i} >= s{i - 1}")
            if window_days is not None:
                conds.append(f"s{i} <= s0 + INTERVAL {int(window_days)} DAY")
        c = " AND ".join([f"s{j} IS NOT NULL" for j in range(i + 1)] + conds)
        reach.append(f"count(*) FILTER (WHERE {c})")
    sql = (f"WITH e AS (SELECT {ent} AS k, {firsts} FROM {scope.source} WHERE {scope.where} "
           f"AND {ent} IS NOT NULL GROUP BY 1) SELECT {', '.join(reach)} FROM e")
    counts = list(con.execute(sql).fetchone())
    rows = []
    for i, (s, n) in enumerate(zip(steps, counts)):
        prev = counts[i - 1] if i else None
        rows.append([s, number(n), _pct(n / prev) if prev else None,
                     _pct(n / counts[0]) if counts[0] else None,
                     number(prev - n) if prev is not None else None])
    summary = _head(scope, gate) + [
        f"Each step's FIRST occurrence per {entity} is used; an entity counts at a step only "
        f"if that is at or after the previous step's "
        f"(by {gate.contract.date_column}), each {entity} once"
        + (f", within {window_days} day(s) of the first step." if window_days else ".")]
    if counts[0] == 0:
        summary.append(f"No {entity} reached the first step ({steps[0]!r}); check the step "
                       f"names against the values of {step_column}.")
    return Output(headers=["step", "entities", "from previous", "from first", "dropped"],
                  rows=rows, summary=summary, label="funnel")


# --- source_reconciliation ---------------------------------------------------------------------

@register("source_reconciliation", tier=TIER, surface="v2", summary="Several sources' totals of the same "
          "thing side by side (never added), with each source's ratio to a reference source.")
def source_reconciliation(con, gate, scope, measures: list | str, reference: str,
                          dimension: str | None = None, **params) -> Output:
    if params:
        raise TypeError(f"source_reconciliation takes measures, reference, dimension; got "
                        f"{', '.join(sorted(params))}.")
    ms = [m.strip() for m in (measures.split(",") if isinstance(measures, str) else measures)]
    if reference not in ms:
        ms.append(reference)
    if len(ms) < 2:
        raise ParamsInvalid("reconciliation compares at least two sources.")
    exprs = [_additive(gate, m) for m in ms]
    ref_i = ms.index(reference)
    group = ""
    if dimension:
        require_dimension(gate.contract, dimension)
        group = qi(dimension)
    sel = ", ".join(exprs)
    q = (f"SELECT {group + ', ' if group else ''}{sel} FROM {scope.source} WHERE {scope.where}"
         + (f" GROUP BY 1 ORDER BY 1" if group else ""))
    out = con.execute(q).fetchall()
    rows = []
    for r in out:
        vals = list(r[1:] if group else r)
        ref = vals[ref_i]
        ratios = [_ratio(v, ref) for v in vals]
        rows.append(([label(r[0])] if group else ["(all)"])
                    + [number(v) for v in vals] + ratios)
    headers = [dimension or "scope"] + ms + [f"{m} / {reference}" for m in ms]
    summary = _head(scope, gate) + [
        "Sources are shown side by side and never added: each platform claims the same sale "
        "(no_cross_source_conversion_sum). A ratio above 1 means that source claims more than "
        f"{reference}; the sum of the platform ratios minus 1 is the over-claim."]
    return Output(headers=headers, rows=rows, summary=summary, label="source_reconciliation")


def _ratio(a, b):
    return None if not b else number(float(a or 0) / float(b))


# --- ab_test -----------------------------------------------------------------------------------

@register("ab_test", tier=TIER, surface="v2", summary="Two variants' rates with a sample-ratio-mismatch "
          "check, a two-proportion test, the effect and the minimum detectable effect; says "
          "'not enough data' rather than guess.")
def ab_test(con, gate, scope, dimension: str, measure: str, expected_split: float = 0.5,
            alpha: float = 0.05, power: float = 0.8, **params) -> Output:
    from scipy.stats import chi2, norm
    if params:
        raise TypeError(f"ab_test takes dimension, measure, expected_split, alpha, power; got "
                        f"{', '.join(sorted(params))}.")
    require_dimension(gate.contract, dimension)
    m = require_measure(gate.contract, measure)
    if agg_of(m) != "ratio":
        raise ValueError(f"{measure!r} must be a ratio measure (successes / trials), e.g. "
                         f"conversions / visitors; it is agg={agg_of(m)}.")
    d = qi(dimension)
    col = qi(measure)
    out = con.execute(f"SELECT {d}, sum({col}.n) / {float(m.scale)!r}, sum({col}.d) "
                      f"FROM {scope.source} WHERE {scope.where} GROUP BY 1 ORDER BY 1").fetchall()
    if len(out) != 2:
        raise ValueError(f"an A/B readout needs exactly two variants of {dimension}; found "
                         f"{len(out)}: {[label(r[0]) for r in out]}.")
    (a, xa, na), (b, xb, nb) = [(label(r[0]), float(r[1] or 0), float(r[2] or 0)) for r in out]
    summary = _head(scope, gate)
    rows = [[a, number(na), number(xa), _pct(xa / na) if na else None],
            [b, number(nb), number(xb), _pct(xb / nb) if nb else None]]
    headers = [dimension, "trials", "successes", "rate"]
    # sample ratio mismatch: trials split vs the expected split (chi-square, 1 df)
    tot = na + nb
    ea, eb = tot * expected_split, tot * (1 - expected_split)
    srm_p = float(chi2.sf(((na - ea) ** 2 / ea + (nb - eb) ** 2 / eb), 1)) if tot else 1.0
    verdict = []
    if min(na, nb) < MIN_AB_DENOMINATOR:
        verdict.append(f"NOT ENOUGH DATA: a variant has {int(min(na, nb))} trial(s), under "
                       f"{MIN_AB_DENOMINATOR}; no readout is given.")
    elif srm_p < SRM_ALPHA:
        verdict.append(f"SAMPLE RATIO MISMATCH: trials split {int(na)}:{int(nb)} against an "
                       f"expected {expected_split:.0%}:{1 - expected_split:.0%} (p = "
                       f"{srm_p:.2g}); assignment is broken, so no readout is given.")
    else:
        pa, pb = xa / na, xb / nb
        pp = (xa + xb) / tot
        se = math.sqrt(pp * (1 - pp) * (1 / na + 1 / nb)) if 0 < pp < 1 else 0.0
        z = (pb - pa) / se if se else 0.0
        p = float(2 * norm.sf(abs(z))) if se else 1.0
        mde = (norm.ppf(1 - alpha / 2) + norm.ppf(power)) * se
        rows.append(["effect (B - A)", None, None, f"{(pb - pa) * 100:+.2f} pts"])
        rows.append(["p-value", None, None, f"{p:.3g}"])
        rows.append(["minimum detectable effect", None, None, f"{mde * 100:.2f} pts"])
        verdict.append(
            (f"{b} differs from {a} by {(pb - pa) * 100:+.2f} points (p = {p:.3g}, alpha "
             f"{alpha})." if p < alpha else
             f"No difference detected (p = {p:.3g}); with these samples the smallest effect "
             f"this test could reliably see is {mde * 100:.2f} points.")
            + f" Sample ratio check passed (p = {srm_p:.2g}).")
    return Output(headers=headers, rows=rows, summary=summary + verdict, label="ab_test")


# --- pacing ------------------------------------------------------------------------------------

@register("pacing", tier=TIER, surface="v2", summary="Spend to date in a month against a person-entered "
          "budget, with a straight-line month-end projection labelled as one.")
def pacing(con, gate, scope, measure: str, budget: float, month: str,
           as_of: str | None = None, **params) -> Output:
    if params:
        raise TypeError(f"pacing takes measure, budget, month, as_of; got "
                        f"{', '.join(sorted(params))}.")
    try:
        y, mo = int(month[:4]), int(month[5:7])
        first = date(y, mo, 1)
    except (ValueError, TypeError):
        raise ParamsInvalid(f"month must be YYYY-MM; got {month!r}.") from None
    days = calendar.monthrange(y, mo)[1]
    last = date(y, mo, days)
    budget = float(budget)
    if budget <= 0:
        raise ParamsInvalid("budget must be above 0 (the person enters it).")
    sql = _additive(gate, measure)
    dt = qi(_date_col(gate))
    end = date.fromisoformat(as_of) if as_of else con.execute(
        f"SELECT max(CAST({dt} AS DATE)) FROM {scope.source} WHERE {scope.where} AND "
        f"CAST({dt} AS DATE) BETWEEN DATE '{first}' AND DATE '{last}'").fetchone()[0]
    if end is None:
        raise ValueError(f"no rows fall in {month}.")
    spent = con.execute(
        f"SELECT {sql} FROM {scope.source} WHERE {scope.where} AND CAST({dt} AS DATE) "
        f"BETWEEN DATE '{first}' AND DATE '{end}'").fetchone()[0] or 0
    elapsed = (end - first).days + 1
    projected = float(spent) / elapsed * days
    rows = [["budget", number(budget)], ["spent to date", number(spent)],
            ["share of budget used", _pct(float(spent) / budget)],
            ["days elapsed", f"{elapsed} of {days}"],
            ["share of month elapsed", _pct(elapsed / days)],
            ["straight-line month-end projection", number(projected)],
            ["projection vs budget", _pct(projected / budget - 1)]]
    summary = _head(scope, gate) + [
        f"Spent to date runs {first} to {end} (the last day with data, or as_of). The "
        f"projection is a STRAIGHT LINE: spend so far per day x {days} days. It knows nothing of "
        f"planned changes, weekends or festivals."]
    return Output(headers=["item", "value"], rows=rows, summary=summary, label="pacing")


# --- text_ngrams -------------------------------------------------------------------------------

@register("text_ngrams", tier=TIER, surface="v2", summary="Words or word pairs of a text column, with a "
          "declared additive measure summed per n-gram and the rows each appears in.")
def text_ngrams(con, gate, scope, column: str, measure: str, n: int = 1, limit: int = 30,
                **params) -> Output:
    if params:
        raise TypeError(f"text_ngrams takes column, measure, n, limit; got "
                        f"{', '.join(sorted(params))}.")
    if n not in (1, 2, 3):
        raise ParamsInvalid("n is 1 (words), 2 (pairs) or 3 (triples).")
    require_dimension(gate.contract, column)
    _additive(gate, measure)
    c = qi(column)
    toks = (f"list_filter(string_split(regexp_replace(lower(trim(CAST({c} AS VARCHAR))), "
            f"'[^a-z0-9\u0900-\u097f]+', ' ', 'g'), ' '), x -> x <> '')")   # Devanagari kept
    grams = ("w" if n == 1 else
             f"list_transform(range(1, greatest(len(w) - {n - 2}, 1)), "
             f"i -> array_to_string(w[i:i + {n - 1}], ' '))")
    q = (f"SELECT g, sum(v), count(*) FROM (SELECT DISTINCT rid, g, v FROM (SELECT rid, v, "
         f"unnest({grams}) AS g FROM (SELECT {toks} AS w, {qi(measure)} AS v, "
         f"row_number() OVER () AS rid FROM {scope.source} WHERE {scope.where}))) "
         f"WHERE g IS NOT NULL AND g <> '' GROUP BY g ORDER BY 2 DESC NULLS LAST, 1 "
         f"LIMIT {int(limit)}")
    out = con.execute(q).fetchall()
    rows = [[g, number(v), number(k)] for g, v, k in out]
    summary = _head(scope, gate) + [
        f"Text is lower-cased and split on anything that is not a letter or digit. A row adds "
        f"its whole {measure} to every {'word' if n == 1 else f'{n}-word run'} in it, so the "
        f"column does not sum to the total."]
    return Output(headers=["ngram", measure, "rows"], rows=rows, summary=summary,
                  label="text_ngrams")


# --- key_overlap -------------------------------------------------------------------------------

@register("key_overlap", tier=TIER, surface="v2", summary="Keys (e.g. queries) served by several members "
          "(e.g. pages) that split the key's measure, with each key's top member share.")
def key_overlap(con, gate, scope, key: str, member: str, measure: str,
                min_share: float = 0.1, limit: int = 30, **params) -> Output:
    if params:
        raise TypeError(f"key_overlap takes key, member, measure, min_share, limit; got "
                        f"{', '.join(sorted(params))}.")
    for d in (key, member):
        require_dimension(gate.contract, d)
    msum = _additive(gate, measure)
    k, mem = qi(key), qi(member)
    q = (f"WITH g AS (SELECT {k} AS k, {mem} AS m, {msum} AS v FROM {scope.source} "
         f"WHERE {scope.where} GROUP BY 1, 2), t AS (SELECT k, sum(v) AS tot, "
         f"count(*) FILTER (WHERE v >= {float(min_share)!r} * (SELECT sum(v) FROM g g2 "
         f"WHERE g2.k = g.k)) AS splitters, max(v) AS top FROM g GROUP BY k) "
         f"SELECT k, splitters, tot, top / nullif(tot, 0), "
         f"(SELECT string_agg(m, ' | ' ORDER BY v DESC) FROM g WHERE g.k = t.k) "
         f"FROM t WHERE splitters >= 2 ORDER BY tot DESC, k LIMIT {int(limit)}")
    out = con.execute(q).fetchall()
    rows = [[label(a), number(b), number(c), _pct(d), e] for a, b, c, d, e in out]
    summary = _head(scope, gate) + [
        f"A {key} is listed when at least two {member}s each take {min_share:.0%} or more of "
        f"its {measure}. The top share is the biggest {member}'s; members are listed largest "
        f"first."]
    return Output(headers=[key, f"{member}s splitting", measure, "top share", f"{member}s"],
                  rows=rows, summary=summary, label="key_overlap")


# --- expected_rate_by_bucket -------------------------------------------------------------------

DEFAULT_EDGES = [1, 2, 3, 4, 6, 11, 21]


@register("expected_rate_by_bucket", tier=TIER, surface="v2", summary="A ratio measure's own rate at each "
          "bucket of a numeric column, and the entities below the rate their bucket expects.")
def expected_rate_by_bucket(con, gate, scope, measure: str, against: str, entity: str,
                            edges: list | None = None, min_weight: float = 100,
                            limit: int = 30, **params) -> Output:
    if params:
        raise TypeError(f"expected_rate_by_bucket takes measure, against, entity, edges, "
                        f"min_weight, limit; got {', '.join(sorted(params))}.")
    m = require_measure(gate.contract, measure)
    if agg_of(m) != "ratio":
        raise ValueError(f"{measure!r} must be a ratio measure (e.g. clicks / impressions).")
    require_dimension(gate.contract, entity)
    edges = sorted(float(e) for e in (edges or DEFAULT_EDGES))
    col, ag, en = qi(measure), qi(against), qi(entity)
    # the entity's position: its own weighted mean, weighted by the rate's denominator
    case = " ".join(f"WHEN p < {edges[i + 1]!r} THEN '{edges[i]:g}-{edges[i + 1] - 1:g}'"
                    for i in range(len(edges) - 1))
    bucket = f"CASE {case} ELSE '{edges[-1]:g}+' END"
    q = (f"WITH e AS (SELECT {en} AS e, sum({col}.n) AS n, sum({col}.d) AS d, "
         f"sum(CAST({ag} AS DOUBLE) * {col}.d) / nullif(sum({col}.d), 0) AS p "
         f"FROM {scope.source} WHERE {scope.where} GROUP BY 1), "
         f"b AS (SELECT *, {bucket} AS bk FROM e WHERE p IS NOT NULL), "
         f"x AS (SELECT bk, sum(n) / nullif(sum(d), 0) AS rate FROM b GROUP BY bk) "
         f"SELECT b.e, b.bk, b.p, b.n / nullif(b.d, 0), x.rate, b.d, "
         f"(x.rate - b.n / nullif(b.d, 0)) * b.d FROM b JOIN x USING (bk) "
         f"WHERE b.d >= {float(min_weight)!r} AND b.n / nullif(b.d, 0) < x.rate "
         f"ORDER BY 7 DESC, 1 LIMIT {int(limit)}")
    out = con.execute(q).fetchall()
    buckets = con.execute(
        f"WITH e AS (SELECT {en} AS e, sum({col}.n) AS n, sum({col}.d) AS d, "
        f"sum(CAST({ag} AS DOUBLE) * {col}.d) / nullif(sum({col}.d), 0) AS p FROM "
        f"{scope.source} WHERE {scope.where} GROUP BY 1) SELECT {bucket} AS bk, "
        f"sum(n) / nullif(sum(d), 0), sum(d), min(p) FROM e WHERE p IS NOT NULL GROUP BY 1 "
        f"ORDER BY 4").fetchall()
    rows = [[label(a), b, number(c), number(d), number(e), number(f), number(g)]
            for a, b, c, d, e, f, g in out]
    summary = _head(scope, gate) + [
        "Expected rate per bucket (" + "; ".join(f"{b}: {number(r)}" for b, r, _, _ in buckets)
        + f") is this data's own {measure} at that {against}, a ratio of sums over every "
        f"{entity} in the bucket -- not an industry curve.",
        f"Listed: {entity}s under their bucket's rate with at least {min_weight:g} in the "
        f"denominator, largest shortfall (expected minus actual, times the denominator) first."]
    return Output(headers=[entity, "bucket", against, "rate", "expected rate", "weight",
                           "shortfall"], rows=rows, summary=summary,
                  label="expected_rate_by_bucket")


# --- before_after_baseline ---------------------------------------------------------------------

@register("before_after_baseline", tier=TIER, surface="v2", summary="A measure after a start date against "
          "the pre-period's own trend and the same days last year; correlational unless a "
          "holdout is marked.")
def before_after_baseline(con, gate, scope, measure: str, start: str, pre_days: int = 28,
                          post_days: int = 28, holdout: bool = False, **params) -> Output:
    if params:
        raise TypeError(f"before_after_baseline takes measure, start, pre_days, post_days, "
                        f"holdout; got {', '.join(sorted(params))}.")
    msum = _additive(gate, measure)
    dt = qi(_date_col(gate))
    s = date.fromisoformat(start)
    pre0, post1 = s - timedelta(days=pre_days), s + timedelta(days=post_days - 1)
    daily = (f"SELECT CAST({dt} AS DATE) AS d, {msum} AS v FROM {scope.source} WHERE "
             f"{scope.where} GROUP BY 1")
    q = (f"WITH t AS ({daily}), pre AS (SELECT * FROM t WHERE d >= DATE '{pre0}' AND "
         f"d < DATE '{s}'), post AS (SELECT * FROM t WHERE d >= DATE '{s}' AND "
         f"d <= DATE '{post1}') SELECT (SELECT avg(v) FROM pre), (SELECT count(*) FROM pre), "
         f"(SELECT regr_slope(v, d - DATE '{s}') FROM pre), "
         f"(SELECT regr_intercept(v, d - DATE '{s}') FROM pre), "
         f"(SELECT avg(v) FROM post), (SELECT count(*) FROM post), "
         f"(SELECT avg(v) FROM t WHERE d >= DATE '{s}' - INTERVAL 364 DAY AND "
         f"d <= DATE '{post1}' - INTERVAL 364 DAY), "
         f"(SELECT avg(v) FROM t WHERE d >= DATE '{pre0}' - INTERVAL 364 DAY AND "
         f"d < DATE '{s}' - INTERVAL 364 DAY)")
    pre_avg, pre_n, slope, icpt, post_avg, post_n, ly_post, ly_pre = con.execute(q).fetchone()
    if not pre_n or not post_n:
        raise ValueError(f"need days on both sides of {start}: {pre_n} before, {post_n} after.")
    # the pre-period line, extended over the post days: mean of intercept + slope * day
    projected = (icpt + slope * (post_days - 1) / 2) if slope is not None else pre_avg
    rows = [["pre-period mean per day", number(pre_avg)],
            ["post-period mean per day", number(post_avg)],
            ["post projected from the pre-period trend", number(projected)],
            ["post vs projection", _pct(post_avg / projected - 1) if projected else None]]
    if ly_post is not None and ly_pre:
        seasonal = ly_post / ly_pre   # last year's own post/pre lift, 52 weeks earlier
        rows += [["same days last year (post) mean", number(ly_post)],
                 ["last year's post / pre", number(seasonal)],
                 ["post vs pre x last year's lift", _pct(post_avg / (pre_avg * seasonal) - 1)]]
    word = ("caused (a holdout was marked)" if holdout else
            "ASSOCIATED WITH the change -- correlational: no holdout was marked, so trend, "
            "season and everything else that changed on those days remain possible causes")
    summary = _head(scope, gate) + [
        f"Pre {pre0} to {s - timedelta(days=1)} ({pre_n} day(s)), post {s} to {post1} "
        f"({post_n} day(s)); last year = the same weekdays 52 weeks earlier.",
        f"Any difference here is {word}."]
    return Output(headers=["item", "value"], rows=rows, summary=summary,
                  label="before_after_baseline")


# --- unit_economics ----------------------------------------------------------------------------

@register("unit_economics", tier=TIER, surface="v2", summary="Per first-order cohort month: new customers, "
          "CAC from person-entered spend, cumulative revenue per customer, payback month and "
          "LTV:CAC to date; immature cohorts said, never extrapolated.")
def unit_economics(con, gate, scope, entity: str, measure: str, spend: dict,
                   min_months: int = 3, **params) -> Output:
    if params:
        raise TypeError(f"unit_economics takes entity, measure, spend, min_months; got "
                        f"{', '.join(sorted(params))}.")
    if not isinstance(spend, dict) or not spend:
        raise ParamsInvalid("spend is {cohort month 'YYYY-MM': amount}, entered by the person "
                            "under their cac_scope choice.")
    msum = _additive(gate, measure)
    ent, dt = qi(entity), qi(_date_col(gate))
    q = (f"WITH o AS (SELECT {ent} AS c, date_trunc('month', CAST({dt} AS DATE)) AS mo, "
         f"{msum} AS v FROM {scope.source} WHERE {scope.where} AND {ent} IS NOT NULL "
         f"GROUP BY 1, 2), f AS (SELECT c, min(mo) AS co FROM o GROUP BY 1), "
         f"last AS (SELECT max(mo) AS lm FROM o), "
         f"size AS (SELECT co, count(*) AS n FROM f GROUP BY 1), "
         f"months AS (SELECT f.co, date_diff('month', f.co, o.mo) AS k, sum(o.v) AS v "
         f"FROM o JOIN f USING (c) GROUP BY 1, 2) "
         f"SELECT strftime(s.co, '%Y-%m'), s.n, "
         f"date_diff('month', s.co, (SELECT lm FROM last)) + 1, "
         f"list(struct_pack(k := m.k, v := m.v) ORDER BY m.k) "
         f"FROM size s JOIN months m USING (co) GROUP BY s.co, s.n ORDER BY s.co")
    out = con.execute(q).fetchall()
    rows, notes = [], []
    for cohort, n, observed, by_month in out:
        s = spend.get(cohort)
        cac = float(s) / n if s is not None and n else None
        cum, payback, total = 0.0, None, 0.0
        # months are indexed by their distance from the cohort month, so a month with no
        # revenue is a gap in k, never a shift of every later month
        for cell in by_month:
            k, v = cell["k"], float(cell["v"] or 0)
            cum += v / n
            if cac is not None and payback is None and cum >= cac:
                payback = k + 1
        total = cum
        immature = observed < min_months or (cac is not None and payback is None)
        rows.append([cohort, number(n), number(cac) if cac is not None else None,
                     number(total), payback if payback else ("not yet" if cac else None),
                     number(total / cac) if cac else None, observed,
                     "IMMATURE" if immature else ""])
        if s is None:
            notes.append(f"{cohort}: no spend entered, so no CAC.")
    summary = _head(scope, gate) + [
        f"A customer's cohort is the month of their first {measure} row. Revenue per customer "
        f"is cumulative to the last month in the data; LTV:CAC is TO DATE, never projected.",
        f"IMMATURE = fewer than {min_months} month(s) observed, or not yet paid back: its "
        f"figures will still grow."] + notes
    return Output(headers=["cohort", "new customers", "CAC", "revenue per customer to date",
                           "payback month", "LTV:CAC to date", "months observed", "maturity"],
                  rows=rows, summary=summary, label="unit_economics")


# --- rate_mix_shift ----------------------------------------------------------------------------

def _span(label_: str) -> tuple[date, date]:
    """'YYYY-MM' (a month), 'YYYY-MM-DD' (a day) or 'a/b' (a range) as its first and last day."""
    try:
        if "/" in label_:
            a, b = label_.split("/")
            return date.fromisoformat(a), date.fromisoformat(b)
        if len(label_) == 7:
            y, m = int(label_[:4]), int(label_[5:])
            return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
        d = date.fromisoformat(label_)
        return d, d
    except (ValueError, TypeError):
        raise ParamsInvalid(f"period {label_!r}: give YYYY-MM, YYYY-MM-DD or start/end.") from None


@register("rate_mix_shift", tier=TIER, surface="v2", summary="A change in a ratio-of-sums "
          "rate split per group into rate, mix (share of the denominator) and interaction; "
          "the Simpson check for blended rates.")
def rate_mix_shift(con, gate, scope, measure: str, dimension: str, period: str,
                   baseline: str, grain: str | None = None, **params) -> Output:
    if params:
        raise TypeError(f"rate_mix_shift takes measure, dimension, period, baseline; got "
                        f"{', '.join(sorted(params))}.")
    m = require_measure(gate.contract, measure)
    if agg_of(m) != "ratio":
        raise ValueError(f"{measure!r} is agg={agg_of(m)}; rate_mix_shift is for ratio measures "
                         f"(mix_shift handles means).")
    require_dimension(gate.contract, dimension)
    (a0, b0), (a1, b1) = _span(baseline), _span(period)
    col, dim, dt = qi(measure), qi(dimension), qi(_date_col(gate))
    rows_ = con.execute(
        f"SELECT {dim}, "
        f"sum({col}.n) FILTER (WHERE CAST({dt} AS DATE) BETWEEN DATE '{a0}' AND DATE '{b0}'), "
        f"sum({col}.d) FILTER (WHERE CAST({dt} AS DATE) BETWEEN DATE '{a0}' AND DATE '{b0}'), "
        f"sum({col}.n) FILTER (WHERE CAST({dt} AS DATE) BETWEEN DATE '{a1}' AND DATE '{b1}'), "
        f"sum({col}.d) FILTER (WHERE CAST({dt} AS DATE) BETWEEN DATE '{a1}' AND DATE '{b1}') "
        f"FROM {scope.source} WHERE {scope.where} GROUP BY 1 ORDER BY 1").fetchall()
    D0 = sum(float(r[2] or 0) for r in rows_)
    D1 = sum(float(r[4] or 0) for r in rows_)
    if not D0 or not D1:
        raise ValueError(f"{measure}'s denominator is 0 in {'the baseline' if not D0 else 'the period'}.")
    out, tot = [], [0.0, 0.0, 0.0]
    for g, n0, d0, n1, d1 in rows_:
        n0, d0, n1, d1 = (float(x or 0) for x in (n0, d0, n1, d1))
        w0, w1 = d0 / D0, d1 / D1
        if d0 and d1:
            r0, r1 = n0 / d0, n1 / d1
            parts = [w0 * (r1 - r0), r0 * (w1 - w0), (w1 - w0) * (r1 - r0)]
            for i in range(3):
                tot[i] += parts[i]
            out.append([label(g), number(r0), number(r1), _pct(w0), _pct(w1)]
                       + [number(p) for p in parts])
        else:
            out.append([label(g), number(n0 / d0) if d0 else None,
                        number(n1 / d1) if d1 else None, _pct(w0), _pct(w1), None, None, None])
    o0 = sum(float(r[1] or 0) for r in rows_) / D0
    o1 = sum(float(r[3] or 0) for r in rows_) / D1
    out.append(["(all)", number(o0), number(o1), "100.0%", "100.0%"] + [number(x) for x in tot])
    summary = _head(scope, gate) + [
        f"{measure} went from {number(o0)} in {baseline} to {number(o1)} in {period}. Per group: "
        f"rate = the group's own {measure} moved; mix = its share of the denominator moved; "
        f"interaction = both. Rate {number(tot[0])}, mix {number(tot[1])}, interaction "
        f"{number(tot[2])}; they sum to the change"
        + (" -- every group improved while the blend fell: a mix shift (Simpson)." if
           tot[0] > 0 and o1 < o0 else ".")]
    return Output(headers=[dimension, f"rate {baseline}", f"rate {period}",
                           f"share {baseline}", f"share {period}", "rate effect", "mix effect",
                           "interaction"], rows=out, summary=summary, label="rate_mix_shift")


# --- group_map_totals --------------------------------------------------------------------------

GROUP_MAP = "_kw_groups"       # written only by the approve/rename/merge/move/split actions


@register("group_map_totals", tier=TIER, surface="v2", summary="A measure by the APPROVED "
          "group of a text key (keyword groups), with pareto shares; by period with grain; "
          "with member, each group's top member share (one target page per group).")
def group_map_totals(con, gate, scope, key: str, measure: str, grain: str | None = None,
                     member: str | None = None, **params) -> Output:
    if params:
        raise TypeError(f"group_map_totals takes key, measure, grain, member; got "
                        f"{', '.join(sorted(params))}.")
    require_dimension(gate.contract, key)
    msum = _additive(gate, measure)
    exists = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                         [GROUP_MAP]).fetchone()[0]
    if not exists:
        raise ValueError("no keyword groups are approved yet: run and approve keyword grouping "
                         "first.")
    k = qi(key)
    grp = (f"coalesce((SELECT g.group_label FROM {GROUP_MAP} g WHERE g.dataset = "
           f"{_lit(gate.contract.dataset_name)} AND g.keyword = lower(trim(CAST({k} AS VARCHAR)))"
           f"), '(ungrouped)')")
    summary = _head(scope, gate)
    if member:
        require_dimension(gate.contract, member)
        mem = qi(member)
        q = (f"WITH m AS (SELECT {grp} AS g, {mem} AS p, {msum} AS v FROM {scope.source} "
             f"WHERE {scope.where} GROUP BY 1, 2) SELECT g, count(*), sum(v), "
             f"max(v) / nullif(sum(v), 0), arg_max(p, v) FROM m GROUP BY g ORDER BY 3 DESC, 1")
        out = con.execute(q).fetchall()
        rows = [[g, number(v), number(n), _pct(s), label(p)] for g, n, v, s, p in out]
        split = [r[0] for r in out if r[1] > 1 and r[0] != "(ungrouped)"]
        summary.append(f"One target page per group: {len(split)} group(s) spread their {measure} "
                       f"over more than one {member}" + (f" ({', '.join(split[:8])})." if split
                                                         else "."))
        return Output(headers=["group", measure, f"{member}s", "top share", f"top {member}"],
                      rows=rows, summary=summary, label="group_map_totals")
    if grain:
        dt = qi(_date_col(gate))
        q = (f"SELECT {grp} AS g, CAST(date_trunc({_lit(grain)}, CAST({dt} AS DATE)) AS DATE), "
             f"{msum} FROM {scope.source} WHERE {scope.where} GROUP BY 1, 2 ORDER BY 1, 2")
        rows = [[g, str(p), number(v)] for g, p, v in con.execute(q).fetchall()]
        return Output(headers=["group", "period", measure], rows=rows, summary=summary,
                      label="group_map_totals")
    q = (f"SELECT {grp} AS g, count(DISTINCT lower(trim(CAST({k} AS VARCHAR)))), {msum} "
         f"FROM {scope.source} WHERE {scope.where} GROUP BY 1 ORDER BY 3 DESC, 1")
    out = con.execute(q).fetchall()
    total = sum(float(v or 0) for _, _, v in out) or None
    run, rows = 0.0, []
    for g, n, v in out:
        run += float(v or 0)
        rows.append([g, number(v), number(n), _pct(float(v or 0) / total) if total else None,
                     _pct(run / total) if total else None])
    summary.append(f"Only approved groups count; keywords in no approved group are "
                   f"'(ungrouped)'. Shares are of {number(total)}.")
    return Output(headers=["group", measure, "keywords", "share", "running share"], rows=rows,
                  summary=summary, label="group_map_totals")
