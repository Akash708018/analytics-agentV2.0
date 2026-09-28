"""A benchmark of every tool this project has, and a list of what behaves badly (Phase 14 Step 11).

    uv run python scripts/benchmark.py                   # 1,000 / 100,000 / 1,000,000 rows
    uv run python scripts/benchmark.py --sizes 1000      # one size

One generated sales table whose answers are known by construction: order_date over 2023-2024 with
March 2024 left empty, 1% of rows dated January 2025 (outside the contract window), 0.5% with no
units and no revenue, East ~3 more units per order, wholesale ~2 more, customers from a pool of
N/5. Every MCP tool in server.py is called as the agent calls it; every one of the 27 analyses is
run and its result file compared cell by cell with the same figure computed here in plain Python
(statistics; scipy for F, t, Pearson and Spearman) over the in-window rows, to the rounding of the
printed figure. Also: wall time per call, output characters against the agent's 8,000-character
read, determinism, scaling from 100k to 1M rows, peak memory, disk, refusal quality, a dirty table
through the cleaning tools, the Postgres tools with no server, and the web layer's Explore.

Writes docs/benchmark/benchmark.json and docs/benchmark/results.md. A measurement: it exits zero
whatever it finds. Every flag is a candidate negative; docs/benchmark/BENCHMARK.md is the triage.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import random
import re
import resource
import shutil
import statistics as st
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
os.environ["ANALYTICS_WORKSPACE_TTL_HOURS"] = "0"

from scipy import stats as sps  # noqa: E402

from backend.engine import server, workspace  # noqa: E402
from backend.engine.contract import store  # noqa: E402
from backend.engine.util import db  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

OUT = ROOT / "docs" / "benchmark"
AGENT_READ = 8_000                      # webapp/agent.py RESULT_CHARS
SLOW_100K = 2.0                         # seconds, one call at 100k rows
SCALING = 15.0                          # 1M over 100k; linear is 10
REGIONS = ("North", "South", "East", "West")
CHANNELS = ("web", "store", "phone")
PRODUCTS = tuple("ABCDEF")
SEGMENTS = ("retail", "wholesale")
WINDOW = ("2023-01-01", "2024-12-31")
TOOLS = sorted(n for n in ("ping", "reset_workspace", "check_file", "preview_file",
                        "propose_ingest_spec", "confirm_ingest_spec", "load_csv", "load_excel",
                        "list_sources", "describe_source", "load_postgres_table", "query_source",
                        "list_datasets", "describe_dataset", "show_limits",
                        "propose_dataset_contract", "confirm_dataset_contract",
                        "get_workflow_state", "run_analysis", "compute_analysis", "render_chart",
                        "profile_dataset", "profile_column", "read_result_file",
                        "propose_cleaning_plan", "apply_cleaning_plan", "get_cleaning_ledger",
                        "validate_dataset", "build_report"))

CALLS: list[dict] = []
CHECKS: list[dict] = []
FLAGS: list[dict] = []
NOTES: dict = {}


def flag(kind: str, tool: str, size: int | None, what: str, detail: str = "") -> None:
    FLAGS.append(dict(kind=kind, tool=tool, size=size, what=what, detail=str(detail)[:600]))


# --------------------------------------------------------------------------- the data


class Data:
    """Columns of the generated table, and the in-window rows the analyses see."""

    def __init__(self, n: int, seed: int = 11) -> None:
        rng = random.Random(seed)
        start = dt.date(2023, 1, 1)
        self.n = n
        cols = defaultdict(list)
        for i in range(n):
            if rng.random() < 0.01:
                d = dt.date(2025, 1, 1) + dt.timedelta(days=rng.randrange(31))
            else:
                while True:
                    d = start + dt.timedelta(days=rng.randrange(731))
                    if not (d.year == 2024 and d.month == 3):
                        break
            reg, seg = rng.randrange(4), 1 if rng.random() < 0.3 else 0
            u = rng.randint(1, 20) + (3 if reg == 2 else 0) + (2 * seg)
            p = round(rng.uniform(2, 200), 2)
            if rng.random() < 0.005:
                u = None
            cols["order_id"].append(f"O{i:07d}")
            cols["order_date"].append(d.isoformat())
            cols["customer_id"].append(f"C{rng.randrange(max(1, n // 5)):06d}")
            cols["region"].append(REGIONS[reg])
            cols["channel"].append(CHANNELS[rng.randrange(3)])
            cols["product"].append(PRODUCTS[rng.randrange(6)])
            cols["segment"].append(SEGMENTS[seg])
            cols["units"].append(u)
            cols["unit_price"].append(p)
            cols["revenue"].append(None if u is None else round(u * p, 2))
        self.cols = dict(cols)
        self.names = list(cols)
        self.win = [i for i, d in enumerate(self.cols["order_date"])
                    if WINDOW[0] <= d <= WINDOW[1]]

    def write(self, path: Path) -> None:
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(self.names)
            c = self.cols
            for i in range(self.n):
                w.writerow(["" if c[k][i] is None else c[k][i] for k in self.names])

    def col(self, name: str, rows=None) -> list:
        c = self.cols[name]
        return [c[i] for i in (self.win if rows is None else rows)]

    def vals(self, name: str, rows=None) -> list:
        return [v for v in self.col(name, rows) if v is not None]

    def where(self, **eq) -> list[int]:
        out = []
        for i in self.win:
            if all((self.cols[k][i][:7] if k == "month" else self.cols[k][i]) == v
                   for k, v in eq.items() if k != "month") and \
                    ("month" not in eq or self.cols["order_date"][i][:7] == eq["month"]):
                out.append(i)
        return out


def months() -> list[str]:
    return [f"{y}-{m:02d}" for y in (2023, 2024) for m in range(1, 13)]


def eta_sq(groups: list[list[float]]) -> float:
    allv = [v for g in groups for v in g]
    mu = st.fmean(allv)
    sst = sum((v - mu) ** 2 for v in allv)
    ssb = sum(len(g) * (st.fmean(g) - mu) ** 2 for g in groups if g)
    return ssb / sst


def quantile_cont(sorted_vals: list[float], q: float) -> float:
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


# --------------------------------------------------------------------------- calling and comparing


def call(size: int, tool: str, label: str | None = None, **kw) -> str:
    fn = getattr(server, tool)
    t0 = time.perf_counter()
    crashed = None
    try:
        out = fn(**kw)
    except Exception as exc:  # noqa: BLE001 - an escaped exception is the finding
        tb = traceback.extract_tb(exc.__traceback__)
        where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb)
                      if "backend/engine" in f.filename), "?")
        crashed = f"{type(exc).__name__}: {str(exc)[:300]} @ {where}"
        out = crashed
    secs = time.perf_counter() - t0
    label = label or tool
    CALLS.append(dict(size=size, tool=tool, label=label, secs=round(secs, 4), chars=len(out),
                      crashed=crashed, head=out[:160]))
    if crashed:
        flag("CRASH", tool, size, label, crashed)
    if len(out) > AGENT_READ:
        flag("BIG", tool, size, label,
             f"{len(out):,} characters; the agent reads the first {AGENT_READ:,}")
    return out


def num(text: str) -> tuple[float, float]:
    """A printed figure and half its last printed digit (the tolerance its rounding allows)."""
    s = text.strip().replace(",", "").replace("+", "")
    pct = s.endswith("%")
    s = s.rstrip("%").rstrip("x")
    v = float(s)
    dec = len(s.split(".")[1]) if "." in s else 0
    if pct:
        v, dec = v / 100, dec + 2
    return v, 0.5 * 10 ** -dec


def expect(size: int, analysis: str, what: str, got, want, exact: bool = False) -> bool:
    try:
        if got is None or (isinstance(got, str) and got.strip() == ""):
            ok = want is None
            gv = got
        elif want is None:
            ok, gv = False, got
        elif isinstance(want, str):
            ok, gv = str(got).strip() == want, got
        else:
            gv, tol = num(str(got))
            ok = abs(gv - want) <= (0 if exact else tol) + abs(want) * 1e-9 + 1e-9
    except ValueError:
        ok, gv = False, got
    CHECKS.append(dict(size=size, analysis=analysis, what=what, got=str(got), want=repr(want),
                       ok=ok))
    if not ok:
        flag("WRONG", analysis, size, what, f"got {got!r}, ground truth {want!r}")
    return ok


def result_path(out: str) -> Path | None:
    m = re.search(r"written to\s*\n\s*(\S+\.csv)", out)
    return Path(m.group(1)) if m else None


def result_rows(out: str) -> list[dict] | None:
    p = result_path(out)
    if not p or not p.exists():
        return None
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def refused(out: str) -> bool:
    return out.startswith("BLOCKED") or "\nreason: " in out


# --------------------------------------------------------------------------- one size


PARAMS = {
    "summary_stats": {},
    "frequency": dict(column="region"),
    "top_n": dict(dimension="region", measure="revenue"),
    "group_compare": dict(dimension="region", measure="units"),
    "cross_tab": dict(rows="channel", columns="region", measure="units"),
    "pareto": dict(dimension="product", measure="revenue"),
    "concentration": dict(dimension="product", measure="revenue"),
    "distribution": dict(measure="units"),
    "ranking_shift": dict(dimension="region", measure="revenue", before_start="2023-01-01",
                          before_end="2023-12-31", after_start="2024-01-01",
                          after_end="2024-12-31"),
    "calendar_coverage": dict(grain="month"),
    "trend": dict(measure="revenue", grain="month"),
    "seasonality": dict(measure="revenue", grain="month"),
    "period_compare": dict(measure="revenue", period="2024-12", baseline="2024-11", grain="month"),
    "growth_decomposition": dict(measure="revenue", dimension="region", period="2024-12",
                                 baseline="2024-11", grain="month"),
    "correlation": dict(measure="units", against="revenue"),
    "bivariate": dict(measure="units", against="revenue"),
    "driver_analysis": dict(measure="units"),
    "mix_shift": dict(measure="revenue", dimension="region", period="2024-12",
                      baseline="2024-11", grain="month"),
    "outlier_detection": dict(measure="revenue"),
    "changepoint": dict(measure="revenue", grain="month"),
    "correlated_shift": dict(measure="units", against="revenue", grain="month"),
    "hypothesis_test": dict(dimension="region", measure="units"),
    "confidence_interval": dict(measure="units"),
    "effect_size": dict(dimension="region", measure="units"),
    "sample_adequacy": dict(dimension="segment", measure="units"),
    "repeat_behaviour": dict(entity="customer_id"),
    "cohort_retention": dict(entity="customer_id"),
}
CHART_Y = {
    "top_n": "revenue (sum)", "group_compare": "mean", "distribution": "rows",
    "ranking_shift": "change", "trend": "revenue (sum)", "seasonality": "index",
    "period_compare": "revenue (sum)", "growth_decomposition": "change", "mix_shift": "contribution",
    "calendar_coverage": "rows", "pareto": "revenue", "cross_tab": None,
}
CONTRACT = dict(
    grain="one row = one order", primary_key=["order_id"], date_column="order_date",
    measures=["units", "unit_price", "revenue"],
    dimensions=["region", "channel", "product", "segment", "customer_id"],
    aggregations={"units": "sum", "unit_price": "none", "revenue": "sum"},
    measure_definitions={"units": "items on the order", "unit_price": "price of one item",
                         "revenue": "units x unit_price"},
    analysis_window_start=WINDOW[0], analysis_window_end=WINDOW[1])


def json_block(out: str) -> str | None:
    return out.split("```json", 1)[1].split("```", 1)[0] if "```json" in out else None


def run_size(n: int, tmp: Path, charts: dict) -> None:
    print(f"\n=== {n:,} rows", flush=True)
    t0 = time.perf_counter()
    data = Data(n)
    path = tmp / f"sales_{n}.csv"
    data.write(path)
    NOTES[f"generate_{n}"] = dict(secs=round(time.perf_counter() - t0, 2),
                                  bytes=path.stat().st_size, in_window=len(data.win))
    ws = f"bench{n}"
    workspace.reset(ws)
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    try:
        _load_and_contract(n, ws, path, data)
        _analyses(n, ws, data, charts)
        _other_tools(n, ws, data)
        if n <= 100_000:
            _web(n, tmp, path)
        disk = sum(f.stat().st_size for f in workspace.workspace_dir(ws).rglob("*")
                   if f.is_file())
        NOTES[f"disk_{n}"] = dict(workspace_bytes=disk, csv_bytes=path.stat().st_size,
                                  files=sum(1 for f in workspace.workspace_dir(ws).rglob("*")
                                            if f.is_file()))
    finally:
        NOTES[f"peak_rss_kb_{n}"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        NOTES[f"peak_rss_before_kb_{n}"] = rss0
        workspace.reset(ws)
        try:
            workspace.workspace_dir(ws).rmdir()
        except OSError:
            pass
        path.unlink(missing_ok=True)


def _load_and_contract(n: int, ws: str, path: Path, data: Data) -> None:
    call(n, "check_file", path=str(path))
    call(n, "preview_file", path=str(path))
    out = call(n, "propose_ingest_spec", path=str(path), dataset_name="sales")
    spec = json_block(out)
    out = call(n, "confirm_ingest_spec", spec_json=spec, workspace_id=ws)
    expect(n, "confirm_ingest_spec", "rows loaded", re.search(r"([\d,]+) rows", out).group(1)
           if re.search(r"([\d,]+) rows", out) else None, data.n, exact=True)
    types = dict(re.findall(r"\| (\w+) \| (\w+) \|", out))
    for colname, want in (("order_date", "DATE"), ("units", "BIGINT"), ("revenue", "DOUBLE")):
        expect(n, "confirm_ingest_spec", f"type of {colname}", types.get(colname), want)
    out = call(n, "compute_analysis", "compute_analysis (no contract)", dataset_name="sales",
               analysis_type="summary_stats", workspace_id=ws)
    if not refused(out):
        flag("GATE", "compute_analysis", n, "computed with no contract", out[:200])
    out = call(n, "propose_dataset_contract", dataset_name="sales", workspace_id=ws, **CONTRACT)
    out = call(n, "confirm_dataset_contract", contract_json=json_block(out), workspace_id=ws)
    if "version 1" not in out:
        flag("WRONG", "confirm_dataset_contract", n, "contract not stored", out[:300])


def _analyses(n: int, ws: str, data: Data, charts: dict) -> None:
    for name, kw in PARAMS.items():
        out = call(n, "compute_analysis", name, dataset_name="sales", analysis_type=name,
                   workspace_id=ws, **kw)
        if refused(out):
            flag("REFUSED", name, n, "the benchmark's parameters were refused", out[:400])
            continue
        rows = result_rows(out)
        if rows is None:
            flag("WRONG", name, n, "no result file", out[:300])
            continue
        again = call(n, "compute_analysis", f"{name} (again)", dataset_name="sales",
                     analysis_type=name, workspace_id=ws, **kw)
        p1, p2 = result_path(out), result_path(again)
        if p1 == p2:
            flag("OVERWRITE", name, n, "a second run wrote over the first run's result file",
                 f"both calls answered {p1.name}; the first reply's path now names the second "
                 "run's table")
        elif p2 and p1.read_text() != p2.read_text():
            flag("NONDET", name, n, "two runs, two different result files")
        mask = re.compile(r"\d{8}-\d{6}(?:_\d+)?|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        if mask.sub("#", out) != mask.sub("#", again):
            flag("NONDET", name, n, "two runs, two different replies")
        try:
            GROUND[name](n, name, rows, out, data)
        except Exception as exc:  # noqa: BLE001 - a harness fault, recorded as one
            flag("HARNESS", name, n, f"ground-truth check raised {type(exc).__name__}: {exc}",
                 traceback.format_exc()[-400:])
        if charts.get(name):
            # As an agent calls it first: no y. Then with the measure named.
            drawn = call(n, "render_chart", f"render_chart {name} (no y)", dataset_name="sales",
                         analysis_type=name, chart=charts[name], workspace_id=ws, **kw)
            if refused(drawn):
                flag("FRICTION", "render_chart", n,
                     "the chart kind the analysis names is refused until y is given",
                     f"{name} as {charts[name]}: " + drawn.splitlines()[1][:200])
                drawn = call(n, "render_chart", f"render_chart {name}", dataset_name="sales",
                             analysis_type=name, chart=charts[name], workspace_id=ws,
                             y=CHART_Y.get(name), **kw)
            if refused(drawn):
                flag("REFUSED", "render_chart", n, f"{name} as {charts[name]}", drawn[:300])
            else:
                png = re.search(r"(\S+\.png)", drawn)
                if not png or not Path(png.group(1)).exists():
                    flag("WRONG", "render_chart", n, f"{name}: no PNG on disk", drawn[:300])
    # Two different questions within one second.
    a = call(n, "compute_analysis", "top_n region", dataset_name="sales", analysis_type="top_n",
             workspace_id=ws, dimension="region", measure="revenue")
    b = call(n, "compute_analysis", "top_n channel", dataset_name="sales", analysis_type="top_n",
             workspace_id=ws, dimension="channel", measure="revenue")
    pa, pb = result_path(a), result_path(b)
    if pa and pb and pa == pb:
        flag("OVERWRITE", "compute_analysis", n,
             "two different analyses in the same second share one result file",
             f"top_n by region then by channel both wrote {pa.name}; reading the first path "
             f"now returns the channel table: {pa.read_text().splitlines()[1][:60]!r}")


# --------------------------------------------------------------------------- ground truth


def g_summary(n, name, rows, out, d):
    by = {r["measure"]: r for r in rows}
    for m in ("units", "unit_price", "revenue"):
        v = d.vals(m)
        r = by.get(m)
        if r is None:
            expect(n, name, f"{m} row present", None, m)
            continue
        expect(n, name, f"{m} n", r["n"], len(v), exact=True)
        expect(n, name, f"{m} nulls", r["nulls"], len(d.win) - len(v), exact=True)
        if m != "unit_price":
            expect(n, name, f"{m} total", r["total"], math.fsum(v))
        for k, f in (("min", min), ("max", max), ("mean", st.fmean), ("median", st.median),
                     ("stddev", st.stdev)):
            expect(n, name, f"{m} {k}", r[k], f(v))


def g_frequency(n, name, rows, out, d):
    c = Counter(d.col("region"))
    for r in rows:
        expect(n, name, f"rows {r['value']}", r["rows"], c.get(r["value"], 0), exact=True)
        expect(n, name, f"share {r['value']}", r["share"], c.get(r["value"], 0) / len(d.win))


def _sum_by(d, dim, m, rows=None):
    s, c = defaultdict(float), Counter()
    for k, v in zip(d.col(dim, rows), d.col(m, rows)):
        c[k] += 1
        if v is not None:
            s[k] += v
    return s, c


def g_top_n(n, name, rows, out, d):
    s, c = _sum_by(d, "region", "revenue")
    for r in rows:
        expect(n, name, f"revenue {r['value']}", r["revenue (sum)"], s[r["value"]])
        expect(n, name, f"rows {r['value']}", r["rows"], c[r["value"]], exact=True)
    expect(n, name, "order (largest first)", ",".join(r["value"] for r in rows),
           ",".join(sorted(s, key=lambda k: -s[k])))


def g_group_compare(n, name, rows, out, d):
    for r in rows:
        g = list(d.win) if r["region"] == "(all)" else d.where(region=r["region"])
        v = d.vals("units", g)
        expect(n, name, f"{r['region']} n", r["n"], len(v), exact=True)
        expect(n, name, f"{r['region']} nulls", r["nulls"], len(g) - len(v), exact=True)
        expect(n, name, f"{r['region']} total", r["total"], sum(v))
        for k, f in (("mean", st.fmean), ("median", st.median), ("stddev", st.stdev),
                     ("min", min), ("max", max)):
            expect(n, name, f"{r['region']} {k}", r[k], f(v))


def g_cross_tab(n, name, rows, out, d):
    s = defaultdict(float)
    for ch, rg, u in zip(d.col("channel"), d.col("region"), d.col("units")):
        s[(ch, rg)] += u or 0
    for r in rows:
        ch = r["channel"]
        if ch == "(total)":
            for rg in REGIONS:
                expect(n, name, f"total {rg}", r[rg], sum(s[(c, rg)] for c in CHANNELS))
            continue
        for rg in REGIONS:
            expect(n, name, f"{ch} x {rg}", r[rg], s[(ch, rg)])
        expect(n, name, f"{ch} total", r["(total)"], sum(s[(ch, rg)] for rg in REGIONS))


def g_pareto(n, name, rows, out, d):
    s, _ = _sum_by(d, "product", "revenue")
    total = math.fsum(s.values())
    run = 0.0
    for r in rows:
        p = r["product"]
        run += s[p]
        expect(n, name, f"{p} revenue", r["revenue"], s[p])
        expect(n, name, f"{p} share", r["share"], s[p] / total)
        expect(n, name, f"{p} running share", r["running share"], run / total)


def g_concentration(n, name, rows, out, d):
    s, _ = _sum_by(d, "product", "revenue")
    ordered = sorted(s.values(), reverse=True)
    total = math.fsum(ordered)
    for r in rows:
        k = int(re.search(r"\d+", r["largest groups"]).group())
        expect(n, name, f"top {k} share", r["share of revenue"], math.fsum(ordered[:k]) / total)


def g_distribution(n, name, rows, out, d):
    v = d.vals("units")
    expect(n, name, "rows over all bins", str(sum(int(num(r["rows"])[0]) for r in rows)),
           len(v), exact=True)
    expect(n, name, "cumulative ends at", rows[-1]["cumulative"], 1.0)
    expect(n, name, "first bin from", rows[0]["from"], min(v))
    expect(n, name, "last bin to", rows[-1]["to"], max(v))


def g_ranking_shift(n, name, rows, out, d):
    before = [i for i in d.win if d.cols["order_date"][i] <= "2023-12-31"]
    after = [i for i in d.win if d.cols["order_date"][i] >= "2024-01-01"]
    sb, _ = _sum_by(d, "region", "revenue", before)
    sa, _ = _sum_by(d, "region", "revenue", after)
    rb = {k: i + 1 for i, k in enumerate(sorted(sb, key=lambda k: -sb[k]))}
    ra = {k: i + 1 for i, k in enumerate(sorted(sa, key=lambda k: -sa[k]))}
    for r in rows:
        g = r["region"]
        expect(n, name, f"{g} before", r["revenue before"], sb[g])
        expect(n, name, f"{g} after", r["revenue after"], sa[g])
        expect(n, name, f"{g} rank before", r["rank before"], rb[g], exact=True)
        expect(n, name, f"{g} rank after", r["rank after"], ra[g], exact=True)


def g_calendar(n, name, rows, out, d):
    c = Counter(x[:7] for x in d.col("order_date"))
    got = {r["period"]: r for r in rows}
    expect(n, name, "periods", str(len(rows)), 24, exact=True)
    for m in months():
        r = got.get(m)
        expect(n, name, f"{m} rows", None if r is None else r["rows"], c.get(m, 0), exact=True)


def g_trend(n, name, rows, out, d):
    got = {r["period"]: r for r in rows}
    expect(n, name, "periods", str(len(rows)), 24, exact=True)
    for m in months():
        g = d.where(month=m)
        v = d.vals("revenue", g)
        r = got.get(m)
        if r is None:
            expect(n, name, f"{m} present", None, m)
            continue
        expect(n, name, f"{m} revenue", r["revenue (sum)"], math.fsum(v) if v else None)
        expect(n, name, f"{m} rows", r["rows"], len(g), exact=True)
    if "2024-03" not in out:
        flag("SILENT", name, n, "the empty month 2024-03 is not named in the reply")


def g_seasonality(n, name, rows, out, d):
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for r in rows:
        mi = names.index(r["position"]) + 1
        per = [math.fsum(d.vals("revenue", d.where(month=f"{y}-{mi:02d}"))) for y in (2023, 2024)
               if d.where(month=f"{y}-{mi:02d}")]
        expect(n, name, f"{r['position']} months observed", r["months observed"], len(per),
               exact=True)
        expect(n, name, f"{r['position']} months absent", r["months absent"], 2 - len(per),
               exact=True)
        expect(n, name, f"{r['position']} mean per month", r["mean revenue per month"],
               st.fmean(per))


def g_period_compare(n, name, rows, out, d):
    for r in rows:
        g = d.where(month=r["period"])
        expect(n, name, f"{r['period']} revenue", r["revenue (sum)"],
               math.fsum(d.vals("revenue", g)))
        expect(n, name, f"{r['period']} rows", r["rows"], len(g), exact=True)


def g_growth(n, name, rows, out, d):
    sb, _ = _sum_by(d, "region", "revenue", d.where(month="2024-11"))
    sp, _ = _sum_by(d, "region", "revenue", d.where(month="2024-12"))
    total = math.fsum(sp.values()) - math.fsum(sb.values())
    for r in rows:
        g = r["region"]
        if g not in REGIONS:
            continue
        expect(n, name, f"{g} base", r["revenue in 2024-11"], sb[g])
        expect(n, name, f"{g} period", r["revenue in 2024-12"], sp[g])
        expect(n, name, f"{g} change", r["change"], sp[g] - sb[g])
        expect(n, name, f"{g} share of change", r["share of change"], (sp[g] - sb[g]) / total)


def _pairs(d):
    return [(u, r) for u, r in zip(d.col("units"), d.col("revenue"))
            if u is not None and r is not None]


def g_correlation(n, name, rows, out, d):
    pr = _pairs(d)
    x, y = [a for a, _ in pr], [b for _, b in pr]
    r = rows[0]
    expect(n, name, "pairs", r["pairs"], len(pr), exact=True)
    expect(n, name, "pearson r", r["pearson r"], float(sps.pearsonr(x, y)[0]))
    expect(n, name, "spearman rho", r["spearman rho"], float(sps.spearmanr(x, y)[0]))
    expect(n, name, "rows without both", r["rows without both"], len(d.win) - len(pr),
           exact=True)


def g_bivariate(n, name, rows, out, d):
    pr = _pairs(d)
    expect(n, name, "rows over all bins", str(sum(int(num(r["rows"])[0]) for r in rows)),
           len(pr), exact=True)
    for r in rows:
        lo, hi = num(r["units from"])[0], num(r["units to"])[0]
        ys = [b for a, b in pr if lo <= a <= hi]  # ranges of distinct values, closed
        if ys:
            expect(n, name, f"bin {r['bin']} rows", r["rows"], len(ys),
                   exact=True)
            expect(n, name, f"bin {r['bin']} mean revenue", r["mean revenue"], st.fmean(ys))


def g_driver(n, name, rows, out, d):
    for r in rows:
        dim = r["dimension"]
        if dim not in ("region", "segment", "channel", "product"):
            continue
        groups = defaultdict(list)
        for k, u in zip(d.col(dim), d.col("units")):
            if u is not None:
                groups[k].append(u)
        expect(n, name, f"{dim} share of variance", r["share of variance"],
               eta_sq(list(groups.values())))
        expect(n, name, f"{dim} groups", r["groups"], len(groups), exact=True)


def g_mix_shift(n, name, rows, out, d):
    b, p = d.where(month="2024-11"), d.where(month="2024-12")
    for r in rows:
        g = r["region"]
        if g not in REGIONS:
            continue
        vb = d.vals("revenue", [i for i in b if d.cols["region"][i] == g])
        vp = d.vals("revenue", [i for i in p if d.cols["region"][i] == g])
        expect(n, name, f"{g} mean in base", r["mean in 2024-11"], st.fmean(vb))
        expect(n, name, f"{g} mean in period", r["mean in 2024-12"], st.fmean(vp))
        expect(n, name, f"{g} share in base", r["share in 2024-11"],
               len(vb) / len(d.vals("revenue", b)))
        expect(n, name, f"{g} share in period", r["share in 2024-12"],
               len(vp) / len(d.vals("revenue", p)))


def g_outliers(n, name, rows, out, d):
    v = sorted(d.vals("revenue"))
    q1, q3 = quantile_cont(v, 0.25), quantile_cont(v, 0.75)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    by = {r["method"]: r for r in rows}
    t = by.get("Tukey's fence")
    expect(n, name, "Tukey lower", t["lower bound"], lo)
    expect(n, name, "Tukey upper", t["upper bound"], hi)
    expect(n, name, "Tukey flagged", t["flagged"], sum(1 for x in v if x < lo or x > hi),
           exact=True)
    mu, sd = st.fmean(v), st.stdev(v)
    z = by.get("z-score")
    expect(n, name, "z lower (mean - 3 sd)", z["lower bound"], mu - 3 * sd)
    expect(n, name, "z upper (mean + 3 sd)", z["upper bound"], mu + 3 * sd)
    expect(n, name, "z flagged", z["flagged"], sum(1 for x in v if abs(x - mu) > 3 * sd),
           exact=True)


def g_changepoint(n, name, rows, out, d):
    series = {m: math.fsum(d.vals("revenue", d.where(month=m))) for m in months()
              if d.where(month=m)}
    top = rows[0]
    split = top["split after"]
    before = [v for m, v in series.items() if m <= split]
    after = [v for m, v in series.items() if m > split]
    expect(n, name, "top split: months before", top["months before"], len(before), exact=True)
    expect(n, name, "top split: months after", top["months after"], len(after), exact=True)
    expect(n, name, "top split: mean before", top["mean before"], st.fmean(before))
    expect(n, name, "top split: mean after", top["mean after"], st.fmean(after))
    NOTES.setdefault("changepoint_series_months", {})[n] = len(series)


def g_correlated_shift(n, name, rows, out, d):
    expect(n, name, "series reported", str(len(rows)), 2, exact=True)


def g_hypothesis(n, name, rows, out, d):
    groups = defaultdict(list)
    for k, u in zip(d.col("region"), d.col("units")):
        if u is not None:
            groups[k].append(u)
    f, p = sps.f_oneway(*[groups[k] for k in sorted(groups)])
    # "Statistic 1,623.2097, df 3, 98509, p < 1e-300." at 100k rows: a comma and a bound.
    m = re.search(r"Statistic ([\d.,]+), df (\d+), (\d+), p (< ?)?([\d.eE+-]*\d)", out)
    expect(n, name, "F statistic", m and m.group(1), float(f))
    expect(n, name, "df within", m and m.group(3), sum(map(len, groups.values())) - len(groups),
           exact=True)
    if m:
        pv = float(m.group(5))
        ok = (float(p) < pv) if m.group(4) else abs(pv - float(p)) <= max(5e-7, abs(float(p)) * 1e-3)
        CHECKS.append(dict(size=n, analysis=name, what="p value", got=m.group(0)[-20:],
                           want=repr(p), ok=ok))
        if not ok:
            flag("WRONG", name, n, "p value", f"got {m.group(0)}, scipy {float(p)!r}")
    for r in rows:
        if r["group"] not in groups:
            continue  # the "(no units)" row: counted, in no test
        g = groups[r["group"]]
        expect(n, name, f"{r['group']} n", r["n"], len(g), exact=True)
        expect(n, name, f"{r['group']} mean", r["mean"], st.fmean(g))
        expect(n, name, f"{r['group']} stddev", r["stddev"], st.stdev(g))


def g_ci(n, name, rows, out, d):
    v = d.vals("units")
    k = len(v)
    half = float(sps.t.ppf(0.975, k - 1)) * st.stdev(v) / math.sqrt(k)
    r = rows[0]
    expect(n, name, "n", r["n"], k, exact=True)
    expect(n, name, "mean", r["mean"], st.fmean(v))
    expect(n, name, "low", r["low (95%)"], st.fmean(v) - half)
    expect(n, name, "high", r["high (95%)"], st.fmean(v) + half)


def g_effect(n, name, rows, out, d):
    groups = defaultdict(list)
    for k, u in zip(d.col("region"), d.col("units")):
        if u is not None:
            groups[k].append(u)
    m = re.search(r"Eta squared ([\d.]+)", out)
    expect(n, name, "eta squared", m and m.group(1), eta_sq(list(groups.values())))


def g_adequacy(n, name, rows, out, d):
    NOTES.setdefault("sample_adequacy_says", {})[n] = out[out.find("What this"):][:700]


def g_repeat(n, name, rows, out, d):
    c = Counter(d.col("customer_id"))
    buckets = {"once": (1, 1), "twice": (2, 2), "three times": (3, 3),
               "four or five times": (4, 5), "more than 5 times": (6, 10 ** 9)}
    for r in rows:
        lo, hi = buckets.get(r["how often"], (None, None))
        if lo is None:
            continue
        people = [k for k, v in c.items() if lo <= v <= hi]
        expect(n, name, f"{r['how often']} people", r["people"], len(people), exact=True)
        expect(n, name, f"{r['how often']} events", r["events"], sum(c[k] for k in people),
               exact=True)


def g_cohort(n, name, rows, out, d):
    first, active = {}, defaultdict(set)
    for cid, day in zip(d.col("customer_id"), d.col("order_date")):
        m = day[:7]
        first[cid] = min(first.get(cid, m), m)
        active[cid].add(m)
    idx = {m: i for i, m in enumerate(months())}
    size = Counter(first.values())
    cells = Counter()
    for cid, f in first.items():
        for m in active[cid]:
            cells[(f, idx[m] - idx[f])] += 1
    expect(n, name, "cohorts", str(len(rows)), len(size), exact=True)
    for r in rows:
        f = r["cohort"][:7]
        expect(n, name, f"{f} size", r["size"], size.get(f, 0), exact=True)
        for k in range(0, 24):
            key = f"+{k}"
            if key in r and r[key] not in ("", None):
                expect(n, name, f"{f} {key}", r[key], cells.get((f, k), 0), exact=True)


GROUND = {
    "summary_stats": g_summary, "frequency": g_frequency, "top_n": g_top_n,
    "group_compare": g_group_compare, "cross_tab": g_cross_tab, "pareto": g_pareto,
    "concentration": g_concentration, "distribution": g_distribution,
    "ranking_shift": g_ranking_shift, "calendar_coverage": g_calendar, "trend": g_trend,
    "seasonality": g_seasonality, "period_compare": g_period_compare,
    "growth_decomposition": g_growth, "correlation": g_correlation, "bivariate": g_bivariate,
    "driver_analysis": g_driver, "mix_shift": g_mix_shift, "outlier_detection": g_outliers,
    "changepoint": g_changepoint, "correlated_shift": g_correlated_shift,
    "hypothesis_test": g_hypothesis, "confidence_interval": g_ci, "effect_size": g_effect,
    "sample_adequacy": g_adequacy, "repeat_behaviour": g_repeat, "cohort_retention": g_cohort,
}


# --------------------------------------------------------------------------- every other tool


def _other_tools(n: int, ws: str, data: Data) -> None:
    call(n, "ping")
    call(n, "show_limits")
    call(n, "list_datasets", workspace_id=ws)
    out = call(n, "describe_dataset", dataset_name="sales", workspace_id=ws)
    expect(n, "describe_dataset", "rows", (re.search(r"([\d,]+) rows", out) or [None, None])[1],
           data.n, exact=True)
    out = call(n, "get_workflow_state", workspace_id=ws)
    if "contract v1" not in out:
        flag("WRONG", "get_workflow_state", n, "does not say contract v1 is in force", out[:300])
    out = call(n, "run_analysis", dataset_name="sales", workspace_id=ws)
    out = call(n, "profile_dataset", dataset_name="sales", workspace_id=ws)
    nulls = len([u for u in data.cols["units"] if u is None])
    line = next((ln for ln in out.splitlines() if ln.startswith("  - units (")), "")
    NOTES.setdefault("profile_units_line", {})[n] = line
    if f"{nulls:,} null" not in line:
        flag("WRONG", "profile_dataset", n, "units null count not on its line",
             f"{nulls} nulls in the file; line: {line[:200]}")
    out = call(n, "profile_column", dataset_name="sales", column="region", workspace_id=ws)
    c = Counter(data.cols["region"])
    for k, v in c.items():
        if f"{v:,}" not in out:
            flag("WRONG", "profile_column", n, f"count for {k} missing", f"expected {v:,}")
    out = call(n, "validate_dataset", dataset_name="sales", workspace_id=ws)
    outside = data.n - len(data.win)
    NOTES.setdefault("validate", {})[n] = out[:1500]
    if f"{outside:,}" not in out:
        flag("WRONG", "validate_dataset", n, "the rows outside the window are not counted",
             f"{outside:,} rows are dated January 2025; reply: {out[:300]}")
    out = call(n, "propose_cleaning_plan", dataset_name="sales", workspace_id=ws)
    NOTES.setdefault("clean_plan_on_clean_table", {})[n] = out[:800]
    call(n, "get_cleaning_ledger", dataset_name="sales", workspace_id=ws)
    out = call(n, "compute_analysis", "top_n for read_result_file", dataset_name="sales",
               analysis_type="top_n", workspace_id=ws, dimension="customer_id",
               measure="revenue", n=200)
    p = result_path(out)
    if p:
        page = call(n, "read_result_file", path=str(p), start=51, limit=10, workspace_id=ws)
        lines = p.read_text().splitlines()
        want = lines[51].split(",")[0] if len(lines) > 51 else None
        if want and want not in page:
            flag("WRONG", "read_result_file", n, "start=51 does not show the 51st data row",
                 f"expected {want}; got {page[:200]}")
    out = call(n, "build_report", dataset_name="sales", question="Where does revenue come from?",
               workspace_id=ws)
    NOTES.setdefault("report_reply", {})[n] = out[:600]


# --------------------------------------------------------------------------- refusals


REFUSAL_CASES = [
    ("compute_analysis", "an analysis nobody registered",
     dict(dataset_name="sales", analysis_type="regression")),
    ("compute_analysis", "a measure that is not a column",
     dict(dataset_name="sales", analysis_type="top_n", dimension="region", measure="profit")),
    ("compute_analysis", "summing an agg=none measure",
     dict(dataset_name="sales", analysis_type="top_n", dimension="region", measure="unit_price")),
    ("compute_analysis", "concentration over a 1,000-group dimension",
     dict(dataset_name="sales", analysis_type="concentration", dimension="customer_id",
          measure="revenue")),
    ("compute_analysis", "period_compare against the empty month",
     dict(dataset_name="sales", analysis_type="period_compare", measure="revenue",
          period="2024-04", baseline="2024-03", grain="month")),
    ("compute_analysis", "a period outside the window",
     dict(dataset_name="sales", analysis_type="period_compare", measure="revenue",
          period="2025-01", baseline="2024-12", grain="month")),
    ("compute_analysis", "a wrong argument for the analysis",
     dict(dataset_name="sales", analysis_type="trend", measure="revenue", grain="month", bins=5)),
    ("compute_analysis", "an unknown grain",
     dict(dataset_name="sales", analysis_type="trend", measure="revenue", grain="fortnight")),
    ("compute_analysis", "hypothesis_test with a key as the dimension",
     dict(dataset_name="sales", analysis_type="hypothesis_test", dimension="order_id",
          measure="units")),
    ("compute_analysis", "no such dataset",
     dict(dataset_name="nope", analysis_type="summary_stats")),
    ("render_chart", "a chart kind that does not exist",
     dict(dataset_name="sales", analysis_type="trend", chart="pie", measure="revenue",
          grain="month")),
    ("render_chart", "a heatmap of a trend",
     dict(dataset_name="sales", analysis_type="trend", chart="heatmap", measure="revenue",
          grain="month")),
    ("describe_dataset", "no such dataset", dict(dataset_name="nope")),
    ("profile_column", "no such column", dict(dataset_name="sales", column="nope")),
    ("profile_dataset", "no such dataset", dict(dataset_name="nope")),
    ("run_analysis", "no such dataset", dict(dataset_name="nope")),
    ("validate_dataset", "no such dataset", dict(dataset_name="nope")),
    ("apply_cleaning_plan", "an id from no plan",
     dict(dataset_name="sales", approved_action_ids=["C999"])),
    ("confirm_dataset_contract", "JSON that does not parse", dict(contract_json="{not json")),
    ("confirm_ingest_spec", "an empty spec", dict(spec_json="{}")),
    ("propose_dataset_contract", "a key that is not unique",
     dict(dataset_name="sales", grain="one row = one order", primary_key=["region"])),
    ("read_result_file", "a path outside the workspace", dict(path="/etc/passwd")),
    ("read_result_file", "start past the end",
     dict(path="RESULT", start=10_000, limit=5)),
    ("load_csv", "a file that is not there", dict(path="/no/such.csv", dataset_name="x")),
    ("load_excel", "a CSV passed as Excel", dict(path="SALES_CSV", dataset_name="x")),
    ("preview_file", "a file that is not there", dict(path="/no/such.csv")),
    ("check_file", "a file that is not there", dict(path="/no/such.csv")),
    ("reset_workspace", "without confirm", dict()),
    ("build_report", "no such dataset", dict(dataset_name="nope", question="why")),
    ("get_cleaning_ledger", "no such dataset", dict(dataset_name="nope")),
    ("list_sources", "no Postgres running", dict()),
    ("describe_source", "no Postgres running", dict(alias="olist")),
    ("query_source", "no Postgres running", dict(alias="olist", sql="select 1")),
    ("load_postgres_table", "no Postgres running", dict(alias="olist", table="orders")),
    ("describe_source", "an alias nobody configured", dict(alias="nosuch")),
]


def _refusals(tmp: Path) -> None:
    n = 1000
    ws = "benchref"
    workspace.reset(ws)
    data = Data(n, seed=3)
    csv_path = tmp / "ref_sales.csv"
    data.write(csv_path)
    tools = set(TOOLS)
    try:
        spec = json_block(server.propose_ingest_spec(path=str(csv_path), dataset_name="sales"))
        server.confirm_ingest_spec(spec_json=spec, workspace_id=ws)
        before = server.compute_analysis(dataset_name="sales", analysis_type="summary_stats",
                                         workspace_id=ws)
        _grade_refusal("compute_analysis", "before any contract", before, 0.0, tools, n)
        server.confirm_dataset_contract(contract_json=json_block(server.propose_dataset_contract(
            dataset_name="sales", workspace_id=ws, **CONTRACT)), workspace_id=ws)
        res = result_path(server.compute_analysis(dataset_name="sales", analysis_type="top_n",
                                                  workspace_id=ws, dimension="region",
                                                  measure="revenue"))
        for tool, label, kw in REFUSAL_CASES:
            kw = dict(kw)
            if kw.get("path") == "RESULT":
                kw["path"] = str(res)
            if kw.get("path") == "SALES_CSV":
                kw["path"] = str(csv_path)
            if "workspace_id" in __import__("inspect").signature(getattr(server, tool)).parameters:
                kw["workspace_id"] = ws
            before_calls = len(CALLS)
            out = call(n, tool, f"{tool}: {label}", **kw)
            _grade_refusal(tool, label, out, CALLS[before_calls]["secs"], tools, n)
    finally:
        workspace.reset(ws)
        try:
            workspace.workspace_dir(ws).rmdir()
        except OSError:
            pass
        csv_path.unlink(missing_ok=True)


REFUSALS: list[dict] = []


def _grade_refusal(tool: str, label: str, out: str, secs: float, tools: set, n: int) -> None:
    nxt = next((ln for ln in out.splitlines() if ln.startswith("NEXT STEP")), "")
    why = next((ln for ln in out.splitlines() if ln.startswith("WHY")), "")
    named = [t for t in re.findall(r"\b([a-z_]+)\b", nxt) if t in tools]
    # Only the ANALYSES a WHY names are compared with the NEXT STEP: a WHY that mentions
    # profile_dataset as background is not a recommendation (Step 11's triage of the
    # hypothesis_test case, P14-D76).
    in_why: list[str] = []
    leaks = re.findall(r"/home/\S+|/root/\S+|/tmp/\S+", out)
    rec = dict(tool=tool, case=label, secs=round(secs, 3), chars=len(out),
               refused=refused(out), next_step=nxt[:200], named=named, why=why[:200],
               other_tool_in_why=in_why, leaks=leaks[:3], head=out[:300])
    REFUSALS.append(rec)
    # An analysis the WHY recommends ("top_n on customer_id says ..."), not one it mentions.
    in_why += [a for a in PARAMS if re.search(rf"\b{a} on \w", why) and a not in nxt
               and a not in label]
    rec["other_tool_in_why"] = in_why
    if not rec["refused"]:
        return  # an answer, not a refusal: recorded, graded by eye in the report
    if not nxt:
        flag("REFUSAL", tool, n, f"{label}: no NEXT STEP", out[:300])
    elif not named and "ask the user" not in nxt.lower() and "check the" not in nxt.lower():
        flag("REFUSAL", tool, n, f"{label}: NEXT STEP names no tool", nxt[:200])
    if in_why and named and not set(in_why) & set(named):
        flag("REFUSAL", tool, n,
             f"{label}: WHY points to {', '.join(dict.fromkeys(in_why))} but NEXT STEP says "
             f"{', '.join(named)}", f"{why[:200]} / {nxt[:200]}")
    if secs > 2.0:
        flag("SLOW", tool, n, f"{label}: took {secs:.1f}s to refuse")


# --------------------------------------------------------------------------- cleaning


def _cleaning(tmp: Path) -> None:
    n = 1000
    ws = "benchclean"
    workspace.reset(ws)
    rng = random.Random(5)
    path = tmp / "dirty.csv"
    amounts, qty, na, regions, days = [], [], 0, [], []
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "amount", "qty", "region", "joined"])
        for i in range(400):
            a = round(rng.uniform(10, 5000), 2)
            amounts.append(a)
            q = rng.randint(1, 9)
            if rng.random() < 0.05:
                q = None
                na += 1
            qty.append(q)
            reg = rng.choice(REGIONS)
            regions.append(reg)
            day = dt.date(2024, 1, 1) + dt.timedelta(days=rng.randrange(366))
            days.append(day)
            w.writerow([f"R{i:04d}", f"${a:,.2f}", "N/A" if q is None else q,
                        f"  {reg} " if i % 3 == 0 else reg, day.strftime("%d/%m/%Y")])
    truth = dict(amount_sum=round(math.fsum(amounts), 2), qty_nulls=na, regions=4)
    try:
        spec = json_block(server.propose_ingest_spec(path=str(path), dataset_name="dirty"))
        call(n, "confirm_ingest_spec", "confirm_ingest_spec (dirty)", spec_json=spec,
             workspace_id=ws)
        plan = call(n, "propose_cleaning_plan", "propose_cleaning_plan (dirty)",
                    dataset_name="dirty", workspace_id=ws)
        ids = list(dict.fromkeys(re.findall(r"\b(C\d{3})\b", plan)))
        NOTES["clean_plan_dirty"] = plan[:4000]
        out = call(n, "apply_cleaning_plan", "apply_cleaning_plan (every id)",
                   dataset_name="dirty", approved_action_ids=ids, workspace_id=ws)
        NOTES["clean_apply"] = out[:1500]
        call(n, "get_cleaning_ledger", "get_cleaning_ledger (dirty)", dataset_name="dirty",
             workspace_id=ws)
        con = db.connect(ws)
        try:
            types = dict(con.execute("select column_name, data_type from information_schema.columns"
                                     " where table_name = 'dirty'").fetchall())
            got = dict(types=types)
            if "DOUBLE" in types.get("amount", "") or "DECIMAL" in types.get("amount", ""):
                got["amount_sum"] = round(con.execute("select sum(amount) from dirty").fetchone()[0],
                                          2)
            got["qty_nulls"] = con.execute("select count(*) - count(qty) from dirty").fetchone()[0]
            got["regions"] = con.execute("select count(distinct region) from dirty").fetchone()[0]
            got["joined_type"] = types.get("joined")
            if got["joined_type"] == "DATE":
                read = [r[0] for r in con.execute("select joined from dirty order by id").fetchall()]
                got["joined_right"] = sum(a == b for a, b in zip(read, days))
                truth["joined_right"] = len(days)
        finally:
            con.close()
        NOTES["clean_result"] = dict(truth=truth, got=got)
        for k, v in truth.items():
            expect(n, "cleaning", k, str(got.get(k)) if got.get(k) is not None else None, v)
        if got.get("joined_type") != "DATE":
            flag("MISSED", "apply_cleaning_plan", n, "dd/mm/yyyy dates left as text",
                 f"joined is {got.get('joined_type')} after every proposed id was applied")
    finally:
        workspace.reset(ws)
        try:
            workspace.workspace_dir(ws).rmdir()
        except OSError:
            pass
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- the web layer


def _web(n: int, tmp: Path, path: Path) -> None:
    be = RealBackend()
    ws = be.new_workspace_id()
    try:
        t0 = time.perf_counter()
        up = be.save_upload(ws, "sales.csv", path.read_bytes())
        d = be.draft_ingest(ws, up.path)
        be.confirm_ingest(ws, d.spec)
        draft = be.draft_contract(ws, "sales", **CONTRACT)
        be.confirm_contract(ws, draft)
        CALLS.append(dict(size=n, tool="web", label="upload+ingest+contract",
                          secs=round(time.perf_counter() - t0, 4), chars=0, crashed=None, head=""))
        menu = be.analysis_menu(ws, "sales")
        refusedn = 0
        for a in menu.analyses:
            params = {f.name: f.default for f in a.params if f.default is not None}
            t0 = time.perf_counter()
            try:
                run = be.run_analysis(ws, "sales", a.name, params, chart=a.chart)
                crashed = None
            except Exception as exc:  # noqa: BLE001
                run, crashed = None, f"{type(exc).__name__}: {exc}"
            secs = time.perf_counter() - t0
            CALLS.append(dict(size=n, tool="web", label=f"Explore {a.name}",
                              secs=round(secs, 4), chars=len(run.text) if run else 0,
                              crashed=crashed, head=(run.text if run else crashed or "")[:160]))
            if crashed:
                flag("CRASH", "web Explore", n, a.name, crashed)
            elif run.refusal:
                refusedn += 1
                flag("REFUSED", "web Explore", n, f"{a.name} with the menu's own defaults",
                     f"{run.refusal.reason}: {run.refusal.why[:200]}")
            if secs > SLOW_100K and n >= 100_000:
                flag("SLOW", "web Explore", n, f"{a.name}: {secs:.1f}s")
        NOTES.setdefault("web_menu", {})[n] = dict(analyses=len(menu.analyses), refused=refusedn)
        t0 = time.perf_counter()
        rep = be.build_report(ws, "sales", "Where does revenue come from?")
        CALLS.append(dict(size=n, tool="web", label="build_report",
                          secs=round(time.perf_counter() - t0, 4), chars=len(rep.text),
                          crashed=None, head=rep.text[:160]))
    finally:
        workspace.reset(ws)
        try:
            workspace.workspace_dir(ws).rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------------- report


def _scaling() -> None:
    by = defaultdict(dict)
    for c in CALLS:
        by[(c["tool"], c["label"])][c["size"]] = c["secs"]
    for (tool, label), t in by.items():
        if 100_000 in t and t[100_000] > SLOW_100K:
            flag("SLOW", tool, 100_000, label, f"{t[100_000]:.2f}s at 100,000 rows")
        if 100_000 in t and 1_000_000 in t and t[100_000] >= 0.05:
            ratio = t[1_000_000] / t[100_000]
            if ratio > SCALING:
                flag("SCALING", tool, 1_000_000, label,
                     f"{t[100_000]:.2f}s -> {t[1_000_000]:.2f}s ({ratio:.0f}x for 10x rows)")
        if 1_000_000 in t and t[1_000_000] > 10:
            flag("SLOW", tool, 1_000_000, label, f"{t[1_000_000]:.1f}s at 1,000,000 rows")


def _write(sizes: list[int]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    called = sorted({c["tool"] for c in CALLS if c["tool"] != "web"})
    missing = [t for t in TOOLS if t not in called]
    payload = dict(sizes=sizes, tools=TOOLS, tools_not_called=missing, notes=NOTES,
                   checks=CHECKS, calls=CALLS, refusals=REFUSALS, flags=FLAGS)
    (OUT / "benchmark.json").write_text(json.dumps(payload, indent=1, default=str))

    lines = ["# Benchmark results (generated by scripts/benchmark.py)", "",
             f"Sizes: {', '.join(f'{s:,}' for s in sizes)} rows. "
             f"Tools: {len(called)} of {len(TOOLS)} called"
             + (f" (not called: {', '.join(missing)})" if missing else "") + ".", ""]
    lines += ["## Correctness against ground truth", "",
              "| analysis | " + " | ".join(f"{s:,}" for s in sizes) + " |",
              "|---|" + "---|" * len(sizes)]
    names = list(dict.fromkeys(c["analysis"] for c in CHECKS))
    for a in names:
        cells = []
        for s in sizes:
            cs = [c for c in CHECKS if c["analysis"] == a and c["size"] == s]
            cells.append(f"{sum(c['ok'] for c in cs)}/{len(cs)}" if cs else "-")
        lines.append(f"| {a} | " + " | ".join(cells) + " |")
    tot = [(sum(c["ok"] for c in CHECKS if c["size"] == s),
            sum(1 for c in CHECKS if c["size"] == s)) for s in sizes]
    lines.append("| **all** | " + " | ".join(f"**{a}/{b}**" for a, b in tot) + " |")

    lines += ["", "## Time (seconds) and reply size (characters) per call", "",
              "| tool | call | " + " | ".join(f"s @ {s:,}" for s in sizes)
              + " | chars (largest) |", "|---|---|" + "---|" * (len(sizes) + 1)]
    by = defaultdict(dict)
    chars = defaultdict(int)
    for c in CALLS:
        if c["label"].startswith(c["tool"] + ": "):
            continue  # refusal cases are tabled below
        by[(c["tool"], c["label"])][c["size"]] = c["secs"]
        chars[(c["tool"], c["label"])] = max(chars[(c["tool"], c["label"])], c["chars"])
    for (tool, label), t in by.items():
        cells = [f"{t[s]:.3f}" if s in t else "-" for s in sizes]
        big = chars[(tool, label)]
        lines.append(f"| {tool} | {label} | " + " | ".join(cells)
                     + f" | {big:,}{' **over 8k**' if big > AGENT_READ else ''} |")

    lines += ["", "## Refusals", "",
              "| tool | case | s | refused | NEXT STEP |", "|---|---|---|---|---|"]
    for r in REFUSALS:
        lines.append(f"| {r['tool']} | {r['case']} | {r['secs']} | {'yes' if r['refused'] else 'no'}"
                     f" | {r['next_step'].replace('|', '/')[:110] or '(none)'} |")

    lines += ["", "## Memory and disk", ""]
    for s in sizes:
        g = NOTES.get(f"generate_{s}", {})
        dsk = NOTES.get(f"disk_{s}", {})
        lines.append(f"- {s:,} rows: CSV {g.get('bytes', 0):,} bytes; workspace "
                     f"{dsk.get('workspace_bytes', 0):,} bytes in {dsk.get('files', 0)} files "
                     f"after the run; peak RSS {NOTES.get(f'peak_rss_kb_{s}', 0) / 1024:,.0f} MiB")

    lines += ["", f"## Flags ({len(FLAGS)})", ""]
    kinds = Counter(f["kind"] for f in FLAGS)
    lines.append(", ".join(f"{k} {v}" for k, v in kinds.most_common()) or "none")
    lines.append("")
    seen = {}
    for f in FLAGS:
        key = (f["kind"], f["tool"], f["what"])
        seen.setdefault(key, []).append(f)
    for (kind, tool, what), fs in seen.items():
        sz = ", ".join(f"{f['size']:,}" for f in fs if f["size"])
        lines.append(f"- **{kind}** `{tool}` {what}" + (f" (at {sz})" if sz else "")
                     + f": {fs[-1]['detail'][:300]}")
    (OUT / "results.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="*", default=[1_000, 100_000, 1_000_000])
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="bench_"))
    # A confirmed contract is exported to docs/contracts/<workspace>/ and reset_workspace leaves
    # it there (N14): the benchmark's exports go to its own temporary directory instead.
    store.EXPORT_DIR = tmp / "contracts"
    charts = {}
    be = RealBackend()
    try:
        ws = be.new_workspace_id()
        p = tmp / "menu.csv"
        Data(200).write(p)
        up = be.save_upload(ws, "sales.csv", p.read_bytes())
        be.confirm_ingest(ws, be.draft_ingest(ws, up.path).spec)
        be.confirm_contract(ws, be.draft_contract(ws, "sales", **CONTRACT))
        charts = {a.name: a.chart for a in be.analysis_menu(ws, "sales").analyses if a.chart}
        workspace.reset(ws)
        workspace.workspace_dir(ws).rmdir()
        print("refusals", flush=True)
        _refusals(tmp)
        print("cleaning", flush=True)
        _cleaning(tmp)
        for n in args.sizes:
            run_size(n, tmp, charts)
        _scaling()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _write(args.sizes)
    ok = sum(c["ok"] for c in CHECKS)
    print(f"\n{ok}/{len(CHECKS)} ground-truth checks right; {len(CALLS)} calls; "
          f"{len(FLAGS)} flags: " + ", ".join(f"{k} {v}" for k, v in
                                              Counter(f["kind"] for f in FLAGS).most_common()))
    for f in FLAGS:
        print(f"  {f['kind']:9} {f['tool']:24} {f['size'] or '':>9} {f['what'][:70]} | "
              f"{f['detail'][:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
