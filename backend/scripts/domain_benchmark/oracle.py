"""The correctness oracle: independent of the code under test.

It imports nothing from backend.engine. It reads the generated CSV with Python's csv module
(not DuckDB), parses each column by the TYPE THE GENERATOR WROTE (not by what the engine
inferred), drops exact duplicate rows (the cleaning the workflow approves), applies the contract
window and exclusions, and recomputes each figure with plain Python arithmetic, `statistics`,
and scipy for distribution functions and tests. It reads only what the tool published: the
result CSV and the reply text.

Every comparison is a Check: expected, actual, absolute and relative error, the tolerance, and
the verdict. The tolerance for a printed figure is half its last printed digit (it cannot be
more exact than it is printed), plus 1e-9 relative for floating-point summation order. P-values
are compared to 1e-3 relative or 5e-7 absolute, the six significant digits the reply prints.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import math
import re
import statistics as st
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from scipy import optimize
from scipy import stats as sps

NULL_LABEL = "(null)"


# --------------------------------------------------------------------------- data


def _parse(kind: str, text: str):
    if text == "":
        return None
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "bool":
        return text == "true"
    if kind == "date":
        return dt.date.fromisoformat(text)
    if kind == "ts":
        return dt.datetime.fromisoformat(text)
    return text


class Data:
    """Columns of the deduplicated table, and the rows in scope."""

    def __init__(self, path, types: dict, date_col: str | None, window, exclusions=(),
                 dedupe: bool = True):
        cols: dict[str, list] = defaultdict(list)
        seen: set[bytes] = set()
        self.raw_rows = 0
        self.duplicates_dropped = 0
        with open(path, newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            parsers = [types[h] for h in header]
            for rec in reader:
                self.raw_rows += 1
                h = hashlib.blake2b("\x1f".join(rec).encode(), digest_size=16).digest()
                if dedupe and h in seen:
                    self.duplicates_dropped += 1
                    continue
                seen.add(h)
                for name, kind, text in zip(header, parsers, rec):
                    cols[name].append(_parse(kind, text))
        self.header = header
        self.cols = dict(cols)
        self.n = len(self.cols[header[0]]) if header else 0
        self.date_col = date_col
        lo, hi = (dt.date.fromisoformat(window[0]), dt.date.fromisoformat(window[1])) \
            if window else (None, None)
        self.window = (lo, hi)
        excluded = set()
        for ex in exclusions:
            for i in range(self.n):
                if ex["py"]({h: self.cols[h][i] for h in ex.get("columns", header)}):
                    excluded.add(i)
        self.excluded = len(excluded)
        dates = self.cols.get(date_col) if date_col else None
        scope = []
        undated = outside = 0
        for i in range(self.n):
            if i in excluded:
                continue
            if dates is not None:
                d = dates[i]
                if d is None:
                    undated += 1
                    continue
                d = d.date() if isinstance(d, dt.datetime) else d
                if not (lo <= d <= hi):
                    outside += 1
                    continue
            scope.append(i)
        self.scope = scope
        self.undated, self.outside = undated, outside

    def col(self, name, rows=None):
        c = self.cols[name]
        return [c[i] for i in (self.scope if rows is None else rows)]

    def vals(self, name, rows=None):
        return [v for v in self.col(name, rows) if v is not None]

    def month(self, i):
        d = self.cols[self.date_col][i]
        return f"{d.year:04d}-{d.month:02d}"

    def in_month(self, m: str):
        return [i for i in self.scope if self.month(i) == m]

    def between(self, a: str, b: str):
        lo, hi = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
        out = []
        for i in self.scope:
            d = self.cols[self.date_col][i]
            d = d.date() if isinstance(d, dt.datetime) else d
            if lo <= d <= hi:
                out.append(i)
        return out

    def window_months(self):
        lo, hi = self.window
        out, y, m = [], lo.year, lo.month
        while (y, m) <= (hi.year, hi.month):
            out.append(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out


def label(v) -> str:
    if v is None:
        return NULL_LABEL
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def norm_label(text: str) -> str:
    """The engine's group label as the oracle keys it. The NULL group is printed '(null)' by
    most analyses and '(no <dimension>)' by some (mix_shift, measured in Step 13's first run)."""
    t = (text or "").strip()
    if t.startswith("(no ") and t.endswith(")"):
        return NULL_LABEL
    return t.lower() if t.lower() in ("true", "false") else t


def aggregate(values: list, agg: str):
    present = [v for v in values if v is not None]
    if agg == "count":
        return len(present)
    if agg == "count_distinct":
        return len(set(present))
    if not present:
        return None
    if agg == "sum":
        return math.fsum(present)
    if agg == "mean":
        return st.fmean(present)
    if agg == "median":
        return st.median(present)
    if agg == "min":
        return min(present)
    if agg == "max":
        return max(present)
    raise ValueError(agg)


def quantile_cont(sorted_vals, q):
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def eta_sq(groups):
    allv = [v for g in groups for v in g]
    mu = st.fmean(allv)
    sst = math.fsum((v - mu) ** 2 for v in allv)
    ssb = math.fsum(len(g) * (st.fmean(g) - mu) ** 2 for g in groups if g)
    return ssb / sst if sst else None


# --------------------------------------------------------------------------- checks


@dataclass
class Check:
    what: str
    expected: object
    actual: object
    absolute_error: float | None
    relative_error: float | None
    tolerance: float | None
    status: str          # PASS FAIL
    note: str = ""

    def as_dict(self):
        d = asdict(self)
        for k in ("expected", "actual"):
            v = d[k]
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = str(v)
        return d


def num(text):
    """A printed figure and half its last printed digit."""
    s = str(text).strip().replace(",", "").replace("+", "")
    pct = s.endswith("%")
    s = s.rstrip("%").rstrip("x")
    v = float(s)
    dec = len(s.split(".")[1]) if "." in s and "e" not in s.lower() else 0
    if pct:
        v, dec = v / 100, dec + 2
    return v, 0.5 * 10 ** -dec


class Checker:
    def __init__(self):
        self.checks: list[Check] = []

    def value(self, what, actual_text, expected, exact=False, rel=1e-9, note=""):
        """A printed number against an expected one."""
        if expected is None:
            ok = actual_text in (None, "") or str(actual_text).strip() in ("", NULL_LABEL)
            self.checks.append(Check(what, None, actual_text, None, None, 0.0,
                                     "PASS" if ok else "FAIL", note or "expected blank"))
            return ok
        try:
            v, half = num(actual_text)
        except (ValueError, TypeError):
            self.checks.append(Check(what, expected, actual_text, None, None, None, "FAIL",
                                     "not a number"))
            return False
        tol = (0.0 if exact else half) + abs(expected) * rel + (0 if exact else 1e-12)
        err = abs(v - expected)
        rerr = err / abs(expected) if expected else None
        ok = err <= tol
        self.checks.append(Check(what, expected, actual_text, err, rerr, tol,
                                 "PASS" if ok else "FAIL", note))
        return ok

    def equal(self, what, actual, expected, note=""):
        ok = actual == expected
        self.checks.append(Check(what, expected, actual, None, None, 0.0,
                                 "PASS" if ok else "FAIL", note))
        return ok

    def pvalue(self, what, actual_text, expected):
        s = str(actual_text).strip().rstrip(".")      # "p < 1e-300." ends a sentence
        bound = s.startswith("<")
        v = float(s.lstrip("< "))
        if bound:
            ok = expected < v
            self.checks.append(Check(what, expected, s, None, None, v, "PASS" if ok else "FAIL",
                                     "reported as a bound"))
            return ok
        err = abs(v - expected)
        tol = max(5e-7, abs(expected) * 1e-3)
        ok = err <= tol
        self.checks.append(Check(what, expected, s, err, err / expected if expected else None,
                                 tol, "PASS" if ok else "FAIL"))
        return ok


def _by(d: Data, dim: str, measure: str | None, rows=None):
    groups = defaultdict(list)
    dims = d.col(dim, rows)
    ms = d.col(measure, rows) if measure else [None] * len(dims)
    for k, v in zip(dims, ms):
        groups[label(k)].append(v)
    return groups


def _col_of(rows, want):
    for h in rows[0]:
        if h == want or h.startswith(want):
            return h
    return None


def _is_measure_value(v):
    return v is not None and isinstance(v, (int, float)) and not isinstance(v, bool)


# --------------------------------------------------------------------------- per analysis


def check(analysis: str, params: dict, rows: list[dict] | None, text: str, d: Data,
          measures: dict, dimensions: list) -> tuple[list[dict], list[str]]:
    """Checks for one successful result. Returns (checks, skipped reasons)."""
    c = Checker()
    skipped: list[str] = []
    fn = ORACLE.get(analysis)
    if fn is None:
        return [], [f"no oracle for {analysis}"]
    if rows is None:
        c.equal("a result file was written", False, True)
        return [x.as_dict() for x in c.checks], skipped
    fn(c, skipped, params, rows, text, d, measures, dimensions)
    return [x.as_dict() for x in c.checks], skipped


def o_summary(c, sk, p, rows, text, d, measures, dims):
    by = {r["measure"]: r for r in rows}
    for m, agg in measures.items():
        r = by.get(m)
        if r is None:
            c.equal(f"{m} row present", False, True)
            continue
        v = d.vals(m)
        c.value(f"{m} n", r["n"], len(v), exact=True)
        c.value(f"{m} nulls", r["nulls"], len(d.scope) - len(v), exact=True)
        if agg == "none":
            c.equal(f"{m} total", r["total"].strip(), "not additive")
        else:
            c.value(f"{m} total ({agg})", r["total"], aggregate(v, agg))
        if v:
            c.value(f"{m} min", r["min"], min(v))
            c.value(f"{m} max", r["max"], max(v))
            c.value(f"{m} mean", r["mean"], st.fmean(v))
            c.value(f"{m} median", r["median"], st.median(v))
            if len(v) > 1:
                c.value(f"{m} stddev", r["stddev"], st.stdev(v))


def o_frequency(c, sk, p, rows, text, d, measures, dims):
    cnt = Counter(label(v) for v in d.col(p["column"]))
    total = len(d.scope)
    first = rows[0].keys().__iter__().__next__()
    shown = [r for r in rows if r[first] not in ("(other)", "(total)")]
    for r in shown:
        k = norm_label(r[first])
        c.value(f"rows {k}", r["rows"], cnt.get(k, 0), exact=True)
        c.value(f"share {k}", r["share"], cnt.get(k, 0) / total)
    limit = p.get("limit", 20)
    ranked = sorted(cnt.values(), reverse=True)
    if len(cnt) > limit:
        cut = ranked[limit - 1]
        shown_keys = {norm_label(r[first]) for r in shown}
        extra = [k for k in cnt if cnt[k] > cut and k not in shown_keys]
        c.equal("every value above the cut is shown", extra, [])
    else:
        c.equal("every value shown", len(shown), len(cnt))


def _ranked_ok(c, what, shown_keys, values: dict, n):
    ordered = sorted(values, key=lambda k: (-values[k], k))
    if len(ordered) <= n:
        c.equal(what, sorted(shown_keys), sorted(ordered))
        return
    cut = values[ordered[n - 1]]
    must = {k for k in ordered if values[k] > cut}
    may = {k for k in ordered if values[k] == cut}
    ok = must <= set(shown_keys) <= (must | may) and len(shown_keys) >= min(n, len(ordered))
    c.checks.append(Check(what, f"{len(must)} above the cut + ties at {cut}", sorted(shown_keys)[:12],
                          None, None, 0.0, "PASS" if ok else "FAIL", "tie-aware"))


def o_top_n(c, sk, p, rows, text, d, measures, dims):
    agg = measures[p["measure"]]
    groups = _by(d, p["dimension"], p["measure"])
    vals = {k: aggregate(v, agg) for k, v in groups.items()}
    vcol = _col_of(rows, f"{p['measure']} (")
    shown = []
    # Once, not per row: per row it was quadratic in the groups (Step 13 run 2, CRM at 100k).
    tot = math.fsum(v for v in vals.values() if v is not None) if agg == "sum" else None
    for r in rows:
        k = norm_label(r["value"])
        if k in ("(other)", "(total)"):
            continue
        shown.append(k)
        c.value(f"{k} {vcol}", r[vcol], vals.get(k))
        c.value(f"{k} rows", r["rows"], len(groups.get(k, [])), exact=True)
        if agg == "sum" and r.get("share", "").strip():
            c.value(f"{k} share", r["share"], vals[k] / tot if tot else None)
    ranked = {k: v for k, v in vals.items() if v is not None}
    _ranked_ok(c, "the top set (tie-aware)", [k for k in shown if k in ranked], ranked,
               p.get("n", 10))


def o_group_compare(c, sk, p, rows, text, d, measures, dims):
    agg = measures[p["measure"]]
    groups = _by(d, p["dimension"], p["measure"])
    groups["(all)"] = d.col(p["measure"])
    dim = p["dimension"]
    for r in rows:
        k = norm_label(r[dim])
        g = groups.get(k)
        if g is None:
            c.equal(f"group {k} exists", False, True)
            continue
        v = [x for x in g if x is not None]
        c.value(f"{k} n", r["n"], len(v), exact=True)
        c.value(f"{k} nulls", r["nulls"], len(g) - len(v), exact=True)
        c.value(f"{k} total", r["total"], aggregate(g, agg))
        if v:
            c.value(f"{k} min", r["min"], min(v))
            c.value(f"{k} max", r["max"], max(v))
            c.value(f"{k} mean", r["mean"], st.fmean(v))
            c.value(f"{k} median", r["median"], st.median(v))
            if len(v) > 1:
                c.value(f"{k} stddev", r["stddev"], st.stdev(v))


def o_cross_tab(c, sk, p, rows, text, d, measures, dims):
    rdim, cdim, m = p["rows"], p["columns"], p.get("measure")
    agg = measures[m] if m else "count"
    cells = defaultdict(list)
    for a, b, v in zip(d.col(rdim), d.col(cdim), d.col(m) if m else [1] * len(d.scope)):
        cells[(label(a), label(b))].append(v)
    head = list(rows[0].keys())
    col_keys = [h for h in head[1:] if h not in ("(total)",)]
    for r in rows:
        rk = norm_label(r[head[0]])
        if rk == "(total)":
            continue
        for ck in col_keys:
            got = cells.get((rk, norm_label(ck)), [])
            # A cell with no rows is blank under an aggregate (the engine's rule, as for a
            # period with no rows) and 0 as a count.
            want = len(got) if agg == "count" else (aggregate(got, agg) if got else None)
            c.value(f"{rk} x {ck}", r[ck], want, exact=agg == "count")


def o_pareto(c, sk, p, rows, text, d, measures, dims):
    groups = _by(d, p["dimension"], p["measure"])
    vals = {k: aggregate(v, "sum") for k, v in groups.items()}
    vals = {k: v for k, v in vals.items() if v is not None}
    total = math.fsum(vals.values())
    dim, m = p["dimension"], p["measure"]
    run = 0.0
    prev = None
    for r in rows:
        k = norm_label(r[dim])
        v = vals.get(k)
        c.value(f"{k} {m}", r[m], v)
        if v is None:
            continue
        run += v
        c.value(f"{k} share", r["share"], v / total)
        c.value(f"{k} running share", r["running share"], run / total)
        if prev is not None:
            c.equal(f"{k} ranked no higher than the row before", v <= prev + 1e-9, True)
        prev = v


def o_concentration(c, sk, p, rows, text, d, measures, dims):
    groups = _by(d, p["dimension"], p["measure"])
    ordered = sorted((v for v in (aggregate(g, "sum") for g in groups.values()) if v is not None),
                     reverse=True)
    total = math.fsum(ordered)
    share_col = _col_of(rows, "share of")
    for r in rows:
        m = re.search(r"\d+", r["largest groups"])
        if not m:
            continue
        k = int(m.group())
        c.value(f"top {k} share", r[share_col], math.fsum(ordered[:k]) / total)


def o_distribution(c, sk, p, rows, text, d, measures, dims):
    v = [x for x in d.vals(p["measure"]) if math.isfinite(x)]
    bins = p.get("bins", 10)
    c.value("rows over all bins", str(sum(int(num(r["rows"])[0]) for r in rows)), len(v), exact=True)
    lo, hi = min(v), max(v)
    c.value("first bin from", rows[0]["from"], lo)
    c.value("last bin to", rows[-1]["to"], hi)
    # The table states its bins; a row is counted in [from, to), the last [from, to]. A printed
    # edge is rounded, so values within half its last digit of an edge are ambiguous and are the
    # tolerance (Step 13 run 2: integer measures get integer-aligned widths, 3-11-19-...-75-80).
    for k, r in enumerate(rows):
        a, ha = num(r["from"])
        b, hb = num(r["to"])
        last = k == len(rows) - 1
        inside = sum(1 for x in v if a <= x < b or (last and x == b))
        ambiguous = sum(1 for x in v if abs(x - a) <= ha or abs(x - b) <= hb) \
            if (ha or hb) else 0
        got = int(num(r["rows"])[0])
        ok = abs(got - inside) <= ambiguous
        c.checks.append(Check(f"bin {k + 1} rows in its stated edges", inside, r["rows"],
                              abs(got - inside), None, float(ambiguous),
                              "PASS" if ok else "FAIL", "lower edge in, upper out, last closed"))
    widths = [num(r["to"])[0] - num(r["from"])[0] for r in rows]
    if widths and max(widths) - min(widths) > max(num(rows[0]["to"])[1], 1e-9) * 2:
        sk.append(f"distribution: bins are not equal width ({min(widths):g} to "
                  f"{max(widths):g}), while the method is described as equal-width")


def o_ranking_shift(c, sk, p, rows, text, d, measures, dims):
    agg = measures[p["measure"]]
    b = d.between(p["before_start"], p["before_end"])
    a = d.between(p["after_start"], p["after_end"])
    vb = {k: aggregate(g, agg) for k, g in _by(d, p["dimension"], p["measure"], b).items()}
    va = {k: aggregate(g, agg) for k, g in _by(d, p["dimension"], p["measure"], a).items()}
    dim, m = p["dimension"], p["measure"]
    for r in rows:
        k = norm_label(r[dim])
        c.value(f"{k} before", r[f"{m} before"], vb.get(k))
        c.value(f"{k} after", r[f"{m} after"], va.get(k))


def o_calendar(c, sk, p, rows, text, d, measures, dims):
    cnt = Counter(d.month(i) for i in d.scope)
    got = {r["period"]: r for r in rows}
    months = d.window_months()
    c.equal("periods", len(rows), len(months))
    for m in months:
        r = got.get(m)
        c.value(f"{m} rows", r["rows"] if r else None, cnt.get(m, 0), exact=True)


def _monthly(d: Data, measure, agg):
    per = defaultdict(list)
    for i in d.scope:
        per[d.month(i)].append(d.cols[measure][i])
    return per


def o_trend(c, sk, p, rows, text, d, measures, dims):
    agg = measures[p["measure"]]
    per = _monthly(d, p["measure"], agg)
    vcol = _col_of(rows, f"{p['measure']} (")
    got = {r["period"]: r for r in rows}
    months = d.window_months()
    c.equal("periods", len(rows), len(months))
    for m in months:
        r = got.get(m)
        if r is None:
            c.equal(f"{m} present", False, True)
            continue
        g = per.get(m, [])
        c.value(f"{m} {vcol}", r[vcol], aggregate(g, agg) if g else None)
        c.value(f"{m} rows", r["rows"], len(g), exact=True)


def o_seasonality(c, sk, p, rows, text, d, measures, dims):
    agg = measures[p["measure"]]
    per = _monthly(d, p["measure"], agg)
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    months = d.window_months()
    mcol = _col_of(rows, "mean ")
    for r in rows:
        pos = names.index(r["position"]) + 1
        at = [m for m in months if int(m[5:]) == pos]
        observed = [aggregate(per[m], agg) for m in at if per.get(m)]
        observed = [v for v in observed if v is not None]
        c.value(f"{r['position']} months observed", r["months observed"], len(observed), exact=True)
        c.value(f"{r['position']} months absent", r["months absent"],
                len([m for m in at if not per.get(m)]), exact=True)
        if observed:
            c.value(f"{r['position']} mean per month", r[mcol], st.fmean(observed))


def o_period_compare(c, sk, p, rows, text, d, measures, dims):
    agg = measures[p["measure"]]
    vcol = _col_of(rows, f"{p['measure']} (")
    for r in rows:
        g = d.in_month(r["period"])
        c.value(f"{r['period']} {vcol}", r[vcol],
                aggregate(d.col(p["measure"], g), agg) if g else None)
        c.value(f"{r['period']} rows", r["rows"], len(g), exact=True)


def o_growth(c, sk, p, rows, text, d, measures, dims):
    m, dim, base, per = p["measure"], p["dimension"], p["baseline"], p["period"]
    vb = {k: aggregate(g, "sum") for k, g in _by(d, dim, m, d.in_month(base)).items()}
    vp = {k: aggregate(g, "sum") for k, g in _by(d, dim, m, d.in_month(per)).items()}
    for r in rows:
        k = norm_label(r[dim])
        if k not in vb and k not in vp:
            continue
        b, a = vb.get(k) or 0.0, vp.get(k) or 0.0
        # A member with no rows in a month is blank there; its change is from nothing.
        c.value(f"{k} base", r[f"{m} in {base}"], vb.get(k))
        c.value(f"{k} period", r[f"{m} in {per}"], vp.get(k))
        c.value(f"{k} change", r["change"], a - b)


def _pairs(d, m1, m2):
    return [(a, b) for a, b in zip(d.col(m1), d.col(m2))
            if _is_measure_value(a) and _is_measure_value(b) and math.isfinite(a)
            and math.isfinite(b)]


def o_correlation(c, sk, p, rows, text, d, measures, dims):
    pr = _pairs(d, p["measure"], p["against"])
    r = rows[0]
    c.value("pairs", r["pairs"], len(pr), exact=True)
    x, y = [a for a, _ in pr], [b for _, b in pr]
    if len(pr) > 2 and len(set(x)) > 1 and len(set(y)) > 1:
        c.value("pearson r", r["pearson r"], float(sps.pearsonr(x, y)[0]))
        c.value("spearman rho", r["spearman rho"], float(sps.spearmanr(x, y)[0]))


def o_bivariate(c, sk, p, rows, text, d, measures, dims):
    pr = _pairs(d, p["measure"], p["against"])
    c.value("rows over all bins", str(sum(int(num(r["rows"])[0]) for r in rows)), len(pr),
            exact=True)
    m, a = p["measure"], p["against"]
    for r in rows:
        lo, hi = num(r[f"{m} from"])[0], num(r[f"{m} to"])[0]
        ys = [b for x, b in pr if lo <= x <= hi]
        # A printed edge is rounded: a bin is checked only when its edges are exact values.
        exact_edges = any(x == lo for x, _ in pr) and any(x == hi for x, _ in pr)
        if ys and exact_edges:
            c.value(f"bin {r['bin']} rows", r["rows"], len(ys), exact=True)
            c.value(f"bin {r['bin']} mean {a}", r[f"mean {a}"], st.fmean(ys))
        elif not exact_edges:
            sk.append(f"bivariate bin {r['bin']}: printed edges are rounded, not data values")


def o_driver(c, sk, p, rows, text, d, measures, dims):
    m = p["measure"]
    for r in rows:
        dim = r["dimension"]
        if dim not in d.cols:
            continue
        groups = defaultdict(list)
        for k, v in zip(d.col(dim), d.col(m)):
            if _is_measure_value(v):
                groups[label(k)].append(v)
        c.value(f"{dim} groups", r["groups"], len(groups), exact=True)
        e = eta_sq(list(groups.values()))
        if e is not None:
            c.value(f"{dim} share of variance", r["share of variance"], e)


def o_mix_shift(c, sk, p, rows, text, d, measures, dims):
    m, dim, base, per = p["measure"], p["dimension"], p["baseline"], p["period"]
    b, a = d.in_month(base), d.in_month(per)
    gb, ga = _by(d, dim, m, b), _by(d, dim, m, a)
    nb = sum(len([v for v in g if v is not None]) for g in gb.values())
    na = sum(len([v for v in g if v is not None]) for g in ga.values())
    for r in rows:
        k = norm_label(r[dim])
        vb = [v for v in gb.get(k, []) if v is not None]
        va = [v for v in ga.get(k, []) if v is not None]
        if vb:
            c.value(f"{k} mean in base", r[f"mean in {base}"], st.fmean(vb))
        if va:
            c.value(f"{k} mean in period", r[f"mean in {per}"], st.fmean(va))
        c.value(f"{k} share in base", r[f"share in {base}"], len(vb) / nb if nb and vb else None)
        c.value(f"{k} share in period", r[f"share in {per}"], len(va) / na if na and va else None)


def o_outliers(c, sk, p, rows, text, d, measures, dims):
    v = sorted(x for x in d.vals(p["measure"]) if math.isfinite(x))
    by = {r["method"]: r for r in rows}
    q1, q3 = quantile_cont(v, 0.25), quantile_cont(v, 0.75)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    t = by.get("Tukey's fence")
    if t:
        c.value("Tukey lower", t["lower bound"], lo)
        c.value("Tukey upper", t["upper bound"], hi)
        c.value("Tukey flagged", t["flagged"], sum(1 for x in v if x < lo or x > hi), exact=True)
    mu, sd = st.fmean(v), st.stdev(v)
    z = by.get("z-score")
    if z:
        c.value("z lower", z["lower bound"], mu - 3 * sd)
        c.value("z upper", z["upper bound"], mu + 3 * sd)
        c.value("z flagged", z["flagged"], sum(1 for x in v if abs(x - mu) > 3 * sd), exact=True)
    med = st.median(v)
    mad = st.median(abs(x - med) for x in v)
    mm = by.get("median absolute deviation")
    if mm and mad == 0:
        # A zero MAD gives no scale: the method cannot place a fence, and a blank with the
        # reason stated is the right answer, not a number.
        ok = mm["lower bound"].strip() == "" and "median absolute deviation" in text
        c.equal("MAD is zero: no fence, and the reply says so", ok, True)
    elif mm:
        k = 3 * 1.4826 * mad
        c.value("MAD lower", mm["lower bound"], med - k, note="median - 3 x 1.4826 x MAD")
        c.value("MAD upper", mm["upper bound"], med + k)
        c.value("MAD flagged", mm["flagged"], sum(1 for x in v if x < med - k or x > med + k),
                exact=True)


def o_changepoint(c, sk, p, rows, text, d, measures, dims):
    agg = measures[p["measure"]]
    per = _monthly(d, p["measure"], agg)
    series = {m: aggregate(per[m], agg) for m in d.window_months() if per.get(m)}
    series = {m: v for m, v in series.items() if v is not None}
    top = rows[0]
    split = top["split after"]
    before = [v for m, v in series.items() if m <= split]
    after = [v for m, v in series.items() if m > split]
    c.value("top split: months before", top["months before"], len(before), exact=True)
    c.value("top split: months after", top["months after"], len(after), exact=True)
    if before and after:
        c.value("top split: mean before", top["mean before"], st.fmean(before))
        c.value("top split: mean after", top["mean after"], st.fmean(after))


def o_correlated_shift(c, sk, p, rows, text, d, measures, dims):
    c.equal("series reported", len(rows), 2)
    sk.append("correlated_shift: breaks and separation have no closed-form reference here")


def _groups(d, dim, m):
    g = defaultdict(list)
    for k, v in zip(d.col(dim), d.col(m)):
        if k is not None and _is_measure_value(v) and math.isfinite(v):
            g[label(k)].append(v)
    return g


def o_hypothesis(c, sk, p, rows, text, d, measures, dims):
    line = next((ln for ln in text.splitlines() if "Test:" in ln), "")
    # df is printed with 4 significant figures, in scientific notation past 9,999 ("df 1.569e+04",
    # Step 13 run 2): the regex reads both.
    m = re.search(r"Statistic (-?[\d.,]+), df ([\d.,eE+]+?|n/a)(?:, ([\d.,eE+]+?))?, "
                  r"p (<\s?[\d.eE+-]+|[\d.eE+-]*\d)", line)
    if not m:
        c.equal("the reply states a statistic and a p value", line[:120], "Test: ... p ...")
        return
    stat = num(m.group(1))[0]  # noqa: F841 - read for the record
    if "second_dimension" in p and p.get("second_dimension"):
        a, b = p["dimension"], p["second_dimension"]
        tab = defaultdict(Counter)
        for x, y in zip(d.col(a), d.col(b)):
            if x is not None and y is not None:
                tab[label(x)][label(y)] += 1
        cols = sorted({k for r in tab.values() for k in r})
        table = [[tab[r][k] for k in cols] for r in sorted(tab)]
        chi2, pv, dof, _ = sps.chi2_contingency(table, correction=True)
        c.value("chi-square statistic", m.group(1), float(chi2))
        c.value("chi-square df", m.group(2), dof, exact=True)
        c.pvalue("chi-square p", m.group(4), float(pv))
        return
    g = _groups(d, p["dimension"], p["measure"])
    keys = sorted(g)
    if "Welch" in line:
        r = sps.ttest_ind(g[keys[0]], g[keys[1]], equal_var=False)
        c.value("Welch |t|", m.group(1).lstrip("-"), abs(float(r.statistic)))
        c.pvalue("Welch p", m.group(4), float(r.pvalue))
    elif "ANOVA" in line:
        r = sps.f_oneway(*[g[k] for k in keys])
        c.value("F", m.group(1), float(r.statistic))
        c.value("df within", m.group(3), sum(len(g[k]) for k in keys) - len(keys), exact=True)
        c.pvalue("ANOVA p", m.group(4), float(r.pvalue))
    elif "Mann-Whitney" in line:
        r = sps.mannwhitneyu(g[keys[0]], g[keys[1]], alternative="two-sided")
        c.pvalue("Mann-Whitney p", m.group(4), float(r.pvalue))
        u1 = float(r.statistic)
        u2 = len(g[keys[0]]) * len(g[keys[1]]) - u1
        got = num(m.group(1))[0]
        best = min((u1, u2), key=lambda u: abs(u - got))
        c.value("Mann-Whitney U (either orientation)", m.group(1), best,
                note=f"U1 {u1}, U2 {u2}: the reply does not say which group's U")
    elif "Kruskal" in line:
        r = sps.kruskal(*[g[k] for k in keys])
        c.value("H", m.group(1), float(r.statistic))
        c.pvalue("Kruskal p", m.group(4), float(r.pvalue))
    else:
        c.equal("a test this oracle knows", line[:80], "Welch / ANOVA / chi-square / rank")
        return
    for r in rows:
        k = norm_label(r.get("group", ""))
        if k in g:
            c.value(f"{k} n", r["n"], len(g[k]), exact=True)
            c.value(f"{k} mean", r["mean"], st.fmean(g[k]))
            if len(g[k]) > 1:
                c.value(f"{k} stddev", r["stddev"], st.stdev(g[k]))


def o_ci(c, sk, p, rows, text, d, measures, dims):
    conf = p.get("confidence", 0.95)
    if p.get("dimension"):
        g = _groups(d, p["dimension"], p["measure"])
    else:
        g = {"(all)": [v for v in d.vals(p["measure"]) if math.isfinite(v)]}
    pct = f"{conf * 100:g}%"
    for r in rows:
        k = norm_label(r["group"])
        v = g.get(k)
        if not v or len(v) < 2:
            continue
        half = float(sps.t.ppf(0.5 + conf / 2, len(v) - 1)) * st.stdev(v) / math.sqrt(len(v))
        c.value(f"{k} n", r["n"], len(v), exact=True)
        c.value(f"{k} mean", r["mean"], st.fmean(v))
        c.value(f"{k} low", r[f"low ({pct})"], st.fmean(v) - half)
        c.value(f"{k} high", r[f"high ({pct})"], st.fmean(v) + half)


def _hedges_g(a, b):
    n1, n2 = len(a), len(b)
    sp = math.sqrt(((n1 - 1) * st.variance(a) + (n2 - 1) * st.variance(b)) / (n1 + n2 - 2))
    dd = (st.fmean(a) - st.fmean(b)) / sp
    return abs(dd * (1 - 3 / (4 * (n1 + n2) - 9)))


def o_effect(c, sk, p, rows, text, d, measures, dims):
    if p.get("second_dimension"):
        m = re.search(r"Cramer's V ([\d.]+)", text)
        a, b = p["dimension"], p["second_dimension"]
        tab = defaultdict(Counter)
        for x, y in zip(d.col(a), d.col(b)):
            if x is not None and y is not None:
                tab[label(x)][label(y)] += 1
        cols = sorted({k for r in tab.values() for k in r})
        table = [[tab[r][k] for k in cols] for r in sorted(tab)]
        chi2 = sps.chi2_contingency(table, correction=True)[0]
        n = sum(map(sum, table))
        v = math.sqrt(chi2 / (n * (min(len(table), len(cols)) - 1)))
        c.value("Cramer's V", m.group(1) if m else None, v)
        return
    g = _groups(d, p["dimension"], p["measure"])
    keys = sorted(g)
    if len(keys) == 2:
        m = re.search(r"Hedges' g (-?[\d.]+)", text)
        c.value("|Hedges' g|", m.group(1).lstrip("-") if m else None,
                _hedges_g(g[keys[0]], g[keys[1]]))
    else:
        m = re.search(r"Eta squared ([\d.]+)", text)
        c.value("eta squared", m.group(1) if m else None, eta_sq([g[k] for k in keys]))


def _power(dd, n1, n2, alpha=0.05):
    df = n1 + n2 - 2
    nc = dd * math.sqrt(n1 * n2 / (n1 + n2))
    tc = sps.t.ppf(1 - alpha / 2, df)
    p = (1 - sps.nct.cdf(tc, df, nc)) + sps.nct.cdf(-tc, df, nc)
    if math.isnan(p):
        # scipy's noncentral t returns NaN at very large df (run 2, 1M rows); there the t is
        # normal to far below the printed precision
        z = sps.norm.ppf(1 - alpha / 2)
        p = sps.norm.sf(z - nc) + sps.norm.cdf(-z - nc)
    return p


def o_adequacy(c, sk, p, rows, text, d, measures, dims):
    g = _groups(d, p["dimension"], p["measure"])
    keys = sorted(g)
    if len(keys) != 2:
        sk.append("sample_adequacy: not two groups")
        return
    n1, n2 = len(g[keys[0]]), len(g[keys[1]])
    power, alpha = p.get("power", 0.8), p.get("alpha", 0.05)
    # Bracketed where scipy's noncentral t is finite: past a few standard deviations nct.cdf
    # returns NaN (the first run's ORACLE_ERROR), and power there is 1 anyway.
    hi = 0.5
    while _power(hi, n1, n2, alpha) < power and hi < 20:
        hi *= 2
    mdd = optimize.brentq(lambda x: _power(x, n1, n2, alpha) - power, 1e-9, hi)
    m = re.search(r"reliably detect is ([\d.]+) standard deviation", text)
    # Two root-finders on the same power curve agree to their tolerances, not to the printed
    # digit: 1e-3 relative (ecommerce, run 2: 0.4206 printed vs 0.420650 here).
    c.value("minimum detectable d", m.group(1) if m else None, mdd, rel=1e-3,
            note="numerical root-find: 1e-3 relative")
    small = next(n for n in range(2, 10 ** 7) if _power(0.2, n, n, alpha) >= power) \
        if power == 0.8 else None
    m2 = re.search(r"needed at the same power: ([\d,]+) for a small", text)
    if small:
        c.value("rows per group for d=0.2", m2.group(1) if m2 else None, small, exact=True)


def o_repeat(c, sk, p, rows, text, d, measures, dims):
    ent = p["entity"]
    cnt = Counter(e for i, e in zip(d.scope, d.col(ent)) if e is not None)
    buckets = {"once": (1, 1), "twice": (2, 2), "three times": (3, 3),
               "four or five times": (4, 5), "more than 5 times": (6, 10 ** 12)}
    for r in rows:
        lo, hi = buckets.get(r["how often"], (None, None))
        if lo is None:
            continue
        people = [k for k, v in cnt.items() if lo <= v <= hi]
        c.value(f"{r['how often']} people", r["people"], len(people), exact=True)
        c.value(f"{r['how often']} events", r["events"], sum(cnt[k] for k in people), exact=True)


def o_cohort(c, sk, p, rows, text, d, measures, dims):
    ent = p["entity"]
    first, active = {}, defaultdict(set)
    for i in d.scope:
        e = d.cols[ent][i]
        if e is None:
            continue
        m = d.month(i)
        first[e] = min(first.get(e, m), m)
        active[e].add(m)
    months = d.window_months()
    idx = {m: k for k, m in enumerate(months)}
    size = Counter(first.values())
    cells = Counter()
    for e, f in first.items():
        for m in active[e]:
            cells[(f, idx[m] - idx[f])] += 1
    c.equal("cohorts", len(rows), len(size))
    for r in rows:
        f = r["cohort"][:7]
        c.value(f"{f} size", r["size"], size.get(f, 0), exact=True)
        for k in range(len(months)):
            key = f"+{k}"
            if key in r and r[key] not in ("", None):
                c.value(f"{f} {key}", r[key], cells.get((f, k), 0), exact=True)


ORACLE = {
    "summary_stats": o_summary, "frequency": o_frequency, "top_n": o_top_n,
    "group_compare": o_group_compare, "cross_tab": o_cross_tab, "pareto": o_pareto,
    "concentration": o_concentration, "distribution": o_distribution,
    "ranking_shift": o_ranking_shift, "calendar_coverage": o_calendar, "trend": o_trend,
    "seasonality": o_seasonality, "period_compare": o_period_compare,
    "growth_decomposition": o_growth, "correlation": o_correlation, "bivariate": o_bivariate,
    "driver_analysis": o_driver, "mix_shift": o_mix_shift, "outlier_detection": o_outliers,
    "changepoint": o_changepoint, "correlated_shift": o_correlated_shift,
    "hypothesis_test": o_hypothesis, "confidence_interval": o_ci, "effect_size": o_effect,
    "sample_adequacy": o_adequacy, "repeat_behaviour": o_repeat, "cohort_retention": o_cohort,
}
