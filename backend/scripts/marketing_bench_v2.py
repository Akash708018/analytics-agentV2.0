"""Marketing bench, second round: funnels, subscriptions, experiments, cross-platform claims,
coupon fan-out with mixed currencies, and search rankings (25/09/2026).

    uv run python scripts/marketing_bench_v2.py [--llm] [--replay OUT.json] [--json OUT.json]

scripts/marketing_bench.py tested one row per ad, per session, per touchpoint. These tables break
the analysis in the ways a marketing team meets after that:

- crm_funnel             one row per lead, stage timestamps (a few MQL dates BEFORE the lead
                         existed), 0/1 stage flags, a deal amount on a few LOST deals, lead score.
- saas_monthly           one row per account per month: MRR is a level (adds across accounts in
                         a month, never across months) beside new, expansion, contraction and
                         churned MRR, which are flows and add; ARPU; CAC and LTV repeated.
- ab_test                one row per exposure: 151 users exposed to BOTH variants.
- platform_conversions   one row per platform claim: Google, Meta and TikTok claim the same
                         orders, so a sum across platforms counts an order once per claim.
- promo_orders           one row per coupon on an order: the order's revenue repeated on every
                         coupon row, revenue in INR, USD and AED in one column, orders in 2031.
- seo_rankings           one row per keyword per day: rank position, monthly search volume
                         repeated every day.

End to end, through server.py, against SQL written here: revenue per channel without the coupon
fan-out; conversion value per platform and the de-duplicated total; conversion rate per variant;
win rate per channel; new MRR per month; a total of MRR refused; and whether the currency and
contamination caveats reach the analysis replies.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
import tempfile
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
_spec = importlib.util.spec_from_file_location("suggest_bench", ROOT / "scripts" /
                                               "suggest_bench.py")
sb = importlib.util.module_from_spec(_spec)
sys.modules["suggest_bench"] = sb
_spec.loader.exec_module(sb)
Case, Label = sb.Case, sb.Label


def cases() -> list[Case]:
    i = "i"
    crm = Case(
        "crm_funnel",
        f"""WITH b AS (
              SELECT {i}, TIMESTAMP '2024-01-01 09:00:00' + INTERVAL ({i} % 180) DAY
                          + INTERVAL ({i} % 9) HOUR AS created,
                     CASE {i} % 5 WHEN 0 THEN 'paid_search' WHEN 1 THEN 'paid_social'
                          WHEN 2 THEN 'organic' WHEN 3 THEN 'referral' ELSE 'events' END AS ch,
                     {i} % 3 <> 0 AS mql, {i} % 3 <> 0 AND {i} % 4 <> 0 AS sq,
                     {i} % 3 <> 0 AND {i} % 4 <> 0 AND {i} % 5 IN (0, 3) AS won
              FROM range(4000) t({i}))
            SELECT 'LD' || {i} AS lead_id, created AS lead_created_at, ch AS channel,
                   'OW' || ({i} % 25) AS owner_id,
                   CASE WHEN {i} % 250 = 7 THEN '(not set)' WHEN {i} % 2 = 0 THEN 'IN' ELSE 'US'
                        END AS country,
                   CASE WHEN mql THEN CASE WHEN {i} % 333 = 5 THEN created - INTERVAL 2 DAY
                        ELSE created + INTERVAL (1 + {i} % 6) DAY END END AS mql_at,
                   CASE WHEN sq THEN created + INTERVAL (8 + {i} % 10) DAY END AS sql_at,
                   CASE WHEN won THEN created + INTERVAL (20 + {i} % 30) DAY END AS won_at,
                   CAST(mql AS INTEGER) AS is_mql, CAST(sq AS INTEGER) AS is_sql,
                   CAST(won AS INTEGER) AS is_won,
                   CASE WHEN won THEN 1000.0 + ({i} * 37) % 9000 WHEN {i} % 800 = 11 THEN 500.0
                        END AS deal_amount,
                   ({i} * 13) % 101 AS lead_score,
                   CASE WHEN won THEN 20 + {i} % 30 END AS days_to_close
            FROM b""",
        {"deal_amount": Label({"sum"}), "lead_score": Label({"none", "mean", "median"}),
         "days_to_close": Label({"mean", "median", "none"})},
        ["country holds a placeholder for a missing value: '(not set)' in 16 row(s)",
         "mql_at is before lead_created_at in 12 row(s)",
         "deal_amount holds a value in 5 row(s) where is_won = 0"],
        date_column="lead_created_at")

    saas = Case(
        "saas_monthly",
        f"""WITH a AS (SELECT acc, 1 + acc % 4 AS plan_i, 20 + (acc * 7) % 60 AS base,
                              CASE acc % 3 WHEN 0 THEN 'paid_search' WHEN 1 THEN 'partner'
                                   ELSE 'outbound' END AS acq,
                              acc % 18 AS churn_mo FROM range(300) t(acc)),
                 m AS (SELECT a.*, mo FROM a, range(18) r(mo)
                       WHERE NOT (acc % 5 = 0 AND mo > churn_mo))
            SELECT 'AC' || acc AS account_id,
                   CAST(DATE '2024-01-01' + INTERVAL (mo) MONTH AS DATE) AS month,
                   CASE plan_i WHEN 1 THEN 'starter' WHEN 2 THEN 'growth' WHEN 3 THEN 'pro'
                        ELSE 'enterprise' END AS plan,
                   acq AS acquisition_channel, base + mo AS seats,
                   (base + mo) * 10.0 * plan_i AS mrr,
                   CASE WHEN mo = 0 THEN base * 10.0 * plan_i ELSE 0 END AS new_mrr,
                   CASE WHEN mo > 0 THEN 10.0 * plan_i ELSE 0 END AS expansion_mrr,
                   CASE WHEN mo > 0 AND (acc + mo) % 11 = 0 THEN 5.0 * plan_i ELSE 0 END
                        AS contraction_mrr,
                   CASE WHEN acc % 5 = 0 AND mo = churn_mo THEN (base + mo) * 10.0 * plan_i
                        ELSE 0 END AS churned_mrr,
                   CASE WHEN acc % 5 = 0 AND mo = churn_mo THEN 1 ELSE 0 END AS is_churned,
                   round((base + mo) * 10.0 * plan_i / (base + mo), 2) + 0.001 * (acc % 2)
                        AS arpu_raw,
                   CASE acq WHEN 'paid_search' THEN 420.0 WHEN 'partner' THEN 260.0 ELSE 610.0
                        END AS cac,
                   (base * 10.0 * plan_i) * (12 + acc % 24) AS ltv
            FROM m""",
        {"mrr": Label({"none", "mean"}), "seats": Label({"none", "mean"}),
         "new_mrr": Label({"sum"}), "expansion_mrr": Label({"sum"}),
         "contraction_mrr": Label({"sum"}), "churned_mrr": Label({"sum"}),
         # per account_id after the first run (25/09/2026): CAC is one value per account repeated
         # every month; counted per row it weights long-lived accounts -- the unit is right.
         "cac": Label({"none", "mean"}, per=["account_id"]),
         "ltv": Label({"none", "mean"}, per=["account_id"])},
        [], date_column="month",
        note="mrr is semi-additive: across accounts in a month yes, across months never")

    ab = Case(
        "ab_test",
        f"""WITH u AS (SELECT {i}, CASE WHEN ({i} * 7919) % 100 < 50 THEN 'control'
                                       ELSE 'treatment' END AS v FROM range(20000) t({i})),
                 x AS (SELECT {i}, v FROM u UNION ALL
                       SELECT {i}, CASE v WHEN 'control' THEN 'treatment' ELSE 'control' END
                       FROM u WHERE {i} % 133 = 0)
            SELECT 'EX' || {i} || '_' || v AS exposure_id, 'U' || {i} AS user_id, v AS variant,
                   TIMESTAMP '2024-05-01 00:00:00' + INTERVAL ({i} % 21) DAY AS exposed_at,
                   CASE {i} % 3 WHEN 0 THEN 'mobile' WHEN 1 THEN 'desktop' ELSE 'tablet' END
                        AS device,
                   CASE WHEN ({i} * 31) % 100 < CASE v WHEN 'control' THEN 10 ELSE 12 END
                        THEN 1 ELSE 0 END AS converted,
                   CASE WHEN ({i} * 31) % 100 < CASE v WHEN 'control' THEN 10 ELSE 12 END
                        THEN 40.0 + {i} % 60 ELSE 0 END AS revenue,
                   1 + {i} % 5 AS sessions
            FROM x""",
        {"revenue": Label({"sum"}), "sessions": Label({"sum"})},
        ["user_id falls under more than one variant for 151 value(s)"],
        date_column="exposed_at")

    claims = Case(
        "platform_conversions",
        f"""WITH o AS (SELECT {i}, 30.0 + ({i} * 17) % 400 AS value,
                              1 + CAST({i} % 7 = 0 AS INTEGER) + CAST({i} % 11 = 0 AS INTEGER)
                                  AS n FROM range(6000) t({i}))
            SELECT 'CL' || {i} || '_' || p AS claim_id, 'ORD' || {i} AS conversion_id,
                   CASE ({i} + p) % 3 WHEN 0 THEN 'google' WHEN 1 THEN 'meta' ELSE 'tiktok' END
                        AS platform,
                   DATE '2024-06-01' + CAST({i} % 60 AS INTEGER) AS conversion_date,
                   value AS conversion_value
            FROM o, range(3) r(p) WHERE p < n""",
        {"conversion_value": Label({"sum"}, per=["conversion_id"])},
        ["conversion_id falls under more than one platform for 1,326 value(s)"],
        date_column="conversion_date")

    promo = Case(
        "promo_orders",
        f"""WITH o AS (SELECT {i},
                   CASE {i} % 3 WHEN 0 THEN 'INR' WHEN 1 THEN 'USD' ELSE 'AED' END AS cur,
                   CASE {i} % 3 WHEN 0 THEN 0.012 WHEN 1 THEN 1.0 ELSE 0.2723 END AS fx,
                   CASE {i} % 3 WHEN 0 THEN 2000.0 + ({i} * 37) % 8000
                        WHEN 1 THEN 25.0 + ({i} * 37) % 100 ELSE 90.0 + ({i} * 37) % 400 END
                        AS loc,
                   1 + CAST({i} % 4 = 0 AS INTEGER) + CAST({i} % 9 = 0 AS INTEGER) AS k,
                   CASE {i} % 4 WHEN 0 THEN 'email' WHEN 1 THEN 'affiliate'
                        WHEN 2 THEN 'paid_social' ELSE 'direct' END AS ch
                   FROM range(3000) t({i}))
            SELECT 'OL' || {i} || '_' || c AS row_id, 'OR' || {i} AS order_id, ch AS channel,
                   CASE WHEN {i} % 500 = 17 THEN DATE '2031-01-01' + CAST({i} % 30 AS INTEGER)
                        ELSE DATE '2024-07-01' + CAST({i} % 90 AS INTEGER) END AS order_date,
                   CASE c WHEN 0 THEN 'SAVE10' WHEN 1 THEN 'FREESHIP' ELSE 'VIP5' END
                        AS coupon_code,
                   cur AS currency, loc AS order_revenue_local, fx AS fx_rate_to_usd,
                   round(loc * fx, 2) AS order_revenue_usd,
                   round(loc * fx * CASE c WHEN 0 THEN 0.10 WHEN 1 THEN 0.03 ELSE 0.05 END, 2)
                        AS discount_usd,
                   CASE c WHEN 0 THEN 10.0 WHEN 1 THEN 3.0 ELSE 5.0 END AS discount_pct
            FROM o, range(3) r(c) WHERE c < k""",
        {"order_revenue_local": Label({"none"}, per=["order_id"]),
         "order_revenue_usd": Label({"sum"}, per=["order_id"]),
         "discount_usd": Label({"sum"}), "discount_pct": Label({"none", "mean"}),
         "fx_rate_to_usd": Label({"none", "mean"}, per=["order_id"])},
        ["order_revenue_local mixes 3 currencies",
         "order_date is in the future in 7 row(s)"],
        date_column="order_date")

    seo = Case(
        "seo_rankings",
        f"""SELECT 'KW' || ({i} % 150) AS keyword_id,
                   DATE '2024-08-01' + CAST({i} // 150 AS INTEGER) AS date,
                   round(1 + (({i} % 150) * 7 + {i} // 150) % 40 + (({i} * 13) % 10) / 10.0, 1)
                        AS avg_position,
                   (({i} % 150) * 97) % 20000 + 50 AS search_volume,
                   100 + ({i} * 29) % 3000 AS impressions,
                   CAST(floor((100 + ({i} * 29) % 3000) * (0.02 + ({i} % 7) / 100.0)) AS BIGINT)
                        AS clicks,
                   round(100.0 * floor((100 + ({i} * 29) % 3000) * (0.02 + ({i} % 7) / 100.0))
                         / (100 + ({i} * 29) % 3000), 2) AS ctr
            FROM range(9000) t({i})""",
        {"avg_position": Label({"none", "mean", "median"}),
         "search_volume": Label({"sum", "mean", "none"}, per=["keyword_id"]),
         "impressions": Label({"sum"}), "clicks": Label({"sum"}),
         "ctr": Label({"none"}, ratio=(["clicks"], ["impressions"]))},
        [], date_column="date")
    return [crm, saas, ab, claims, promo, seo]


# ---- end to end ------------------------------------------------------------------------------

class Engine:
    """One workspace, datasets loaded from the in-memory tables, contracts from the filter."""

    def __init__(self, con, ws: str = "marketing_bench_v2") -> None:
        from backend.engine import server, workspace
        from backend.engine.contract import store
        self.server, self.workspace, self.con, self.ws = server, workspace, con, ws
        workspace.reset(ws)
        # Exports into the bench's own workspace, removed by close() -- not docs/contracts/<ws>/,
        # which the first runs filled (as eval/run_eval.py does).
        self.store, self.saved_export = store, store.EXPORT_DIR
        store.EXPORT_DIR = workspace.workspace_dir(ws) / "contracts"

    def load(self, table: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{table}.csv"
            self.con.execute(f"COPY {table} TO '{path}' (HEADER)")
            return self.server.load_csv(str(path), table, workspace_id=self.ws).split("\n")[0]

    def contract(self, table: str, answers: dict, *, key: list[str], dims: list[str],
                 grain: str) -> str:
        a = dict(answers)
        window = a.pop("analysis_window", None)
        measures = list(a.get("measures") or [])
        defs = {m: (a.get("measure_definitions") or {}).get(m) or f"{m} as recorded"
                for m in measures}
        dimensions = list(dict.fromkeys([d for d in (a.get("dimensions") or []) + dims
                                         if d not in key and d not in measures]))
        reply = self.server.propose_dataset_contract(
            table, grain=grain, primary_key=key, date_column=a.get("date_column"),
            measures=measures, dimensions=dimensions, measure_definitions=defs,
            aggregations=a.get("aggregations"),
            analysis_window_start=str(window[0]) if window else None,
            analysis_window_end=str(window[1]) if window else None,
            measure_per=a.get("measure_per"), measure_columns=a.get("measure_columns"),
            ratios=a.get("ratios"), workspace_id=self.ws)
        if "```json" not in reply:
            return "NOT CONFIRMABLE: " + reply[-500:]
        body = reply.split("```json", 1)[1].split("```", 1)[0]
        return self.server.confirm_dataset_contract(body, workspace_id=self.ws).split("\n")[0]

    def top(self, table: str, dimension: str, measure: str) -> tuple[dict, str]:
        """{group: value} read from the result file the reply names, and the reply itself."""
        text = self.server.compute_analysis(table, "top_n", dimension=dimension,
                                            measure=measure, n=49, workspace_id=self.ws)
        m = re.search(r"written to\s+(\S+\.csv)", text)
        if not m:
            return {}, text
        with open(m.group(1)) as f:
            rows = list(csv.DictReader(f))
        col = next(k for k in rows[0] if k not in ("value", "rows", "share")) if rows else None
        return {r["value"]: float(r[col].replace(",", "")) for r in rows
                if r[col] not in ("", None)}, text

    def close(self) -> None:
        self.store.EXPORT_DIR = self.saved_export
        self.workspace.reset(self.ws)
        self.workspace.workspace_dir(self.ws).rmdir()


def _compare(name: str, got: dict, truth: dict, tol: float = 0.00006) -> list[dict]:
    """The result file rounds to 4 places: 0.11969 is written 0.1197 (first run, 25/09/2026)."""
    out = []
    for group, want in truth.items():
        have = got.get(str(group))
        ok = have is not None and abs(have - want) <= tol
        out.append({"check": name, "group": group, "want": round(want, 6),
                    "got": None if have is None else round(have, 6), "ok": ok})
    return out


def end_to_end(con, answers: dict[str, dict]) -> dict:
    e = Engine(con)
    out: dict = {"figures": [], "contracts": {}, "notes": {}}
    q = lambda sql: con.execute(sql).fetchall()
    try:
        spec = {"promo_orders": (["row_id"], ["channel", "currency", "coupon_code"],
                                 "one row = one coupon on one order"),
                "platform_conversions": (["claim_id"], ["platform"],
                                         "one row = one platform's claim of one conversion"),
                "ab_test": (["exposure_id"], ["variant", "device"],
                            "one row = one exposure of one user to one variant"),
                "crm_funnel": (["lead_id"], ["channel", "owner_id"], "one row = one lead"),
                "saas_monthly": (["account_id", "month"], ["plan", "acquisition_channel"],
                                 "one row = one account in one month")}
        # The claimed value per platform is its own measure: the per-conversion one cannot be split
        # by platform -- an order claimed by two would be counted in both, which the engine
        # refuses (first run, 25/09/2026).
        pc = dict(answers["platform_conversions"])
        pc["measures"] = list(pc.get("measures") or []) + ["claimed_value"]
        pc["aggregations"] = {**(pc.get("aggregations") or {}), "claimed_value": "sum"}
        pc["measure_columns"] = {**(pc.get("measure_columns") or {}),
                                 "claimed_value": "conversion_value"}
        pc["measure_definitions"] = {**(pc.get("measure_definitions") or {}),
                                     "claimed_value": "conversion value as each platform claims "
                                                      "it: an order once per claim"}
        answers = {**answers, "platform_conversions": pc}
        for table, (key, dims, grain) in spec.items():
            e.load(table)
            out["contracts"][table] = e.contract(table, answers[table], key=key, dims=dims,
                                                 grain=grain)

        # coupon fan-out: revenue per channel counted once per order
        got, text = e.top("promo_orders", "channel", "order_revenue_usd")
        truth = dict(q("SELECT channel, sum(r) FROM (SELECT DISTINCT order_id, channel, "
                       "order_revenue_usd AS r FROM promo_orders) GROUP BY channel"))
        naive = dict(q("SELECT channel, sum(order_revenue_usd) FROM promo_orders "
                       "GROUP BY channel"))
        out["figures"] += _compare("revenue_usd per channel, once per order", got,
                                   {k: float(v) for k, v in truth.items()}, 0.01)
        out["notes"]["fan_out_inflation"] = {k: round(float(naive[k]) / float(truth[k]), 3)
                                             for k in truth}
        out["notes"]["currency_caveat_in_reply"] = "mixes 3 currencies" in text

        # cross-platform claims: per platform as claimed, the de-duplicated total, and the
        # per-conversion measure refused by platform
        per_unit = e.server.compute_analysis("platform_conversions", "top_n", dimension="platform",
                                             measure="conversion_value", n=10, workspace_id=e.ws)
        out["notes"]["per_conversion_by_platform_refused"] = "varies within" in per_unit
        got, text = e.top("platform_conversions", "platform", "claimed_value")
        truth = dict(q("SELECT platform, sum(conversion_value) FROM platform_conversions "
                       "GROUP BY platform"))
        out["figures"] += _compare("conversion_value per platform", got,
                                   {k: float(v) for k, v in truth.items()}, 0.01)
        dedup = float(q("SELECT sum(v) FROM (SELECT DISTINCT conversion_id, conversion_value "
                        "AS v FROM platform_conversions)")[0][0])
        claimed = float(q("SELECT sum(conversion_value) FROM platform_conversions")[0][0])
        stats = e.server.compute_analysis("platform_conversions", "summary_stats",
                                          workspace_id=e.ws)
        from backend.engine.webapp.verify import figures
        values = [f.value for f in figures(stats)]
        out["figures"].append({"check": "conversion_value total, de-duplicated", "group": "(all)",
                               "want": round(dedup, 2), "got": None,
                               "ok": any(abs(v - dedup) <= 0.01 for v in values)})
        out["notes"]["claims_inflation"] = round(claimed / dedup, 3)
        out["notes"]["split_caveat_in_reply"] = "falls under more than one platform" in text

        # experiment: conversion rate per variant; contamination said in the reply
        got, text = e.top("ab_test", "variant", "converted_rate")
        truth = dict(q("SELECT variant, avg(converted) FROM ab_test GROUP BY variant"))
        out["figures"] += _compare("conversion rate per variant", got,
                                   {k: float(v) for k, v in truth.items()})
        out["notes"]["contamination_caveat_in_reply"] = "falls under more than one variant" in text

        # funnel: win rate per channel
        got, _ = e.top("crm_funnel", "channel", "is_won_rate")
        truth = dict(q("SELECT channel, avg(is_won) FROM crm_funnel GROUP BY channel"))
        out["figures"] += _compare("win rate per channel", got,
                                   {k: float(v) for k, v in truth.items()})

        # subscriptions: new MRR per plan (a flow), and a total of MRR (a level) refused
        got, _ = e.top("saas_monthly", "plan", "new_mrr")
        truth = dict(q("SELECT plan, sum(new_mrr) FROM saas_monthly GROUP BY plan"))
        out["figures"] += _compare("new MRR per plan", got,
                                   {k: float(v) for k, v in truth.items()}, 0.01)
        refused = e.server.compute_analysis("saas_monthly", "top_n", dimension="plan",
                                            measure="mrr", n=10, workspace_id=e.ws)
        out["notes"]["mrr_total_refused"] = "BLOCKED" in refused or "non-additive" in refused
    finally:
        e.close()
    return out


SPEC_DIMS = {"crm_funnel": ["channel", "owner_id", "country"],
             "saas_monthly": ["plan", "acquisition_channel"],
             "ab_test": ["variant", "device", "user_id"],
             "platform_conversions": ["platform", "conversion_id"],
             "promo_orders": ["channel", "currency", "coupon_code", "order_id"],
             "seo_rankings": ["keyword_id"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--llm", action="store_true", help="fill with the live model (uses quota)")
    ap.add_argument("--replay", type=Path, default=None)
    args = ap.parse_args(argv)

    con = duckdb.connect(":memory:")
    cs = cases()
    results = []
    for c in cs:
        con.execute(f"CREATE TABLE {c.name} AS {c.sql}")
        results.append(sb.run_case(con, c))
    summary = sb.summarise(results)
    filters = {"sum_everything": [sb.filter_run(con, c, sb._sum_everything(con, c, c.name))
                                  for c in cs]}
    if args.llm:
        live = []
        for c in cs:
            try:
                answer = sb._live_model(con, c, c.name)
                row = sb.filter_run(con, c, answer)
                row["model"] = answer.get("_model")
            except Exception as exc:
                row = {"case": c.name, "error": str(exc)[:200]}
            live.append(row)
            time.sleep(8)
        filters["live"] = live
    if args.replay:
        saved = json.loads(args.replay.read_text())["filter"].get("live", [])
        filters["replayed"] = []
        for c in cs:
            row = next((r for r in saved if r.get("case") == c.name and r.get("answer")), None)
            if row:
                again = sb.filter_run(con, c, {"columns": row["answer"]})
                again["model"] = row.get("model")
                filters["replayed"].append(again)

    # contracts for the end to end: the engine's own reading of every numeric column, filtered
    from backend.engine.contract import llm_filter
    from backend.engine.contract.evidence import gather, suggest_role
    answers = {}
    for c, res in zip(cs, results):
        ev = gather(con, c.name, probe_pairs=False)
        roles = {x.name: suggest_role(x)[0] for x in ev.columns}
        numeric = [x.name for x in ev.columns if x.dtype.upper() in ("BIGINT", "INTEGER",
                                                                    "DOUBLE", "DECIMAL(18,2)")
                   or x.dtype.upper().startswith("DECIMAL")]
        from backend.engine.contract import suggest as sg
        rules = sg.suggest(con, c.name, ev, [n for n in numeric if roles.get(n) != "identifier"],
                           c.date_column)
        answer = {"date_column": c.date_column, "columns": [
            {"name": m, "role": "measure", "agg": r.agg or "none", "per": list(r.per),
             "confidence": 0.9} for m, r in rules.items() if not sg.is_flag(ev.column(m))]
            + [{"name": d, "role": "dimension"} for d in SPEC_DIMS.get(c.name, [])]}
        answers[c.name] = llm_filter.check(con, c.name, ev, answer, roles).answers
    e2e = end_to_end(con, answers)

    for res in results:
        for k, r in enumerate(res["suggestions"]):
            head = (f"{res['case']:<20} {res['rows']:>7,} {res['seconds']:>5.2f}" if k == 0
                    else " " * 34)
            mark = "-" if r["right"] is None else "yes" if r["right"] else "NO"
            print(f"{head}  {r['measure']:<20} {str(r['agg']):<6} {'+'.join(r['per']) or '-':<14} "
                  f"{r['strength']:<7} {r['rule']:<3} {mark}")
    print()
    for strength, v in summary["by_strength"].items():
        print(f"{strength:<7} {v['count']:>3}: {v['right']} right, {v['wrong']} wrong, "
              f"{v['abstained']} no suggestion")
    print(f"strong precision {summary['strong_precision'] or 0:.1%}   strong coverage "
          f"{summary['strong_coverage']:.1%}")
    print(f"caveats: {summary['caveat_recall']:.1%} of expected found; missed "
          f"{summary['caveats_missed'] or 'none'}; recounted apart {summary['caveat_lines_held']} "
          f"of {summary['caveat_lines']} held, refuted {summary['caveat_lines_refuted'] or 'none'}")
    for case, lines in summary["caveats_unexpected"].items():
        for line in lines:
            print(f"  not expected ({case}): {line[:118]}")
    print()
    for name, rows in filters.items():
        ok = [r for r in rows if "raw" in r]
        tot = lambda part, k: sum(r[part][k] for r in ok)
        print(f"filter, {name}: raw {tot('raw', 'right')} right / {tot('raw', 'wrong')} wrong / "
              f"{tot('raw', 'blank')} blank -> filtered {tot('filtered', 'right')} right / "
              f"{tot('filtered', 'wrong')} wrong / {tot('filtered', 'blank')} blank")
        for r in rows:
            if "error" in r:
                print(f"  {r['case']:<20} error: {r['error']}")
                continue
            if r["wrong_but_checked"]:
                print(f"  {r['case']:<20} WRONG AND MARKED CHECKED: "
                      + ", ".join(r["wrong_but_checked"]))
            if r["wrong_left_for_review"]:
                print(f"  {r['case']:<20} left for review: " + ", ".join(r["wrong_left_for_review"]))
    print()
    for table, c in e2e["contracts"].items():
        print(f"contract {table:<21} {c[:100]}")
    for f in e2e["figures"]:
        print(f"  {f['check']:<40} {str(f['group']):<12} want {f['want']:<14} got "
              f"{str(f['got']):<14} {'ok' if f['ok'] else 'WRONG'}")
    for k, v in e2e["notes"].items():
        print(f"  {k}: {v}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
        args.json.write_text(json.dumps({"summary": summary, "cases": clean, "filter": filters,
                                         "end_to_end": e2e}, indent=1, default=str))
        print(f"\nwritten {args.json}")
    notes = e2e["notes"]
    failed = (summary["wrong_strong"] or summary["caveats_missed"]
              or summary["caveat_lines_refuted"]
              or any(r.get("wrong_but_checked") for rows in filters.values() for r in rows)
              or not e2e["figures"] or not all(f["ok"] for f in e2e["figures"])
              or not all(notes.get(k) for k in ("currency_caveat_in_reply",
                                                "split_caveat_in_reply",
                                                "contamination_caveat_in_reply",
                                                "mrr_total_refused",
                                                "per_conversion_by_platform_refused")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
