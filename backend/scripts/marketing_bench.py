"""Marketing data against the contract suggestions, the engine's caveats, the model filter and the
numbers themselves (25/09/2026).

    uv run python scripts/marketing_bench.py [--llm] [--replay OUT.json] [--json OUT.json]

Marketing tables are where aggregation goes wrong most quietly: every platform ships derived
ratios (CTR, CPC, CPM, ROAS, conversion rate, frequency) beside the counts they come from, and
distinct counts (reach) that look additive and are not. Five built tables, labels known by
construction:

- ad_daily       one row per day x ad group: impressions, clicks, spend, conversions, revenue; the
                 ratios derived from them; reach and frequency; a daily budget per campaign; average
                 position; a bid; a 0/1 brand flag; a targeting radius; store coordinates; channel
                 and campaign names written two ways; negative spend (credits); clicks above
                 impressions (tracking errors).
- ga_sessions    one row per session: '(not set)' and '(not provided)' -- which mean missing -- beside
                 '(direct)' and '(none)', which are real values; bounce and new-user flags.
- attribution    one row per touchpoint: fractional credit summing to 1 per conversion, the
                 conversion's value repeated on every touchpoint, the attributed value.
- social_daily   followers (a level on a date), posts, likes, engagement rate.
- email_sends    sends, delivered, opens, clicks, open rate, and a list size repeated per list.

Then ad_daily end to end through server.py: a contract from the filtered answers, top_n by channel
on the ratio-of-sums measures, every figure compared with SQL written here, the size of the
mean-of-ratios trap measured, and webapp/verify.py shown an answer that quotes it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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

AD_ROWS = 90 * 36


def cases() -> list[Case]:
    i = "i"
    ad = Case(
        "ad_daily",
        f"""WITH base AS (
              SELECT {i}, {i} // 36 AS day, {i} % 36 AS ag, ({i} % 36) // 3 AS camp,
                     1000 + ({i} * 37) % 5000 AS imp FROM range({AD_ROWS}) t({i})),
            counted AS (
              SELECT *, CASE WHEN {i} % 1000 = 13 THEN imp + 5
                             ELSE CAST(floor(imp * (0.01 + (ag % 7) / 200.0)) AS BIGINT) END
                        AS clk FROM base),
            spent AS (
              SELECT *, CASE WHEN {i} % 1500 = 77 THEN -50.0
                             ELSE round(clk * (0.5 + (camp % 4) * 0.4), 2) END AS sp,
                        clk // (10 + ag % 5) AS conv FROM counted)
            SELECT DATE '2024-01-01' + CAST(day AS INTEGER) AS date,
                   'CMP' || lpad(CAST(camp AS VARCHAR), 2, '0') AS campaign_id,
                   CASE WHEN camp = 4 AND day % 10 = 0 THEN 'SUMMER_SALE_' || camp
                        ELSE 'Summer_Sale_' || camp END AS campaign_name,
                   'AG' || lpad(CAST(ag AS VARCHAR), 2, '0') AS ad_group_id,
                   CASE camp % 3 WHEN 0 THEN 'google'
                        WHEN 1 THEN CASE WHEN day % 15 = 0 THEN 'Meta' ELSE 'meta' END
                        ELSE 'linkedin' END AS channel,
                   imp AS impressions, clk AS clicks, sp AS spend, conv AS conversions,
                   conv * (40.0 + camp * 3) AS revenue,
                   round(100.0 * clk / imp, 2) AS ctr_pct,
                   round(sp / clk, 4) AS cpc,
                   round(1000.0 * sp / imp, 4) AS cpm,
                   CASE WHEN sp > 0 THEN round(conv * (40.0 + camp * 3) / sp, 4) END AS roas,
                   round(1.0 * conv / clk, 4) AS conversion_rate,
                   CAST(floor(imp / (1.5 + (ag % 3) * 0.5)) AS BIGINT) AS reach,
                   round(imp / floor(imp / (1.5 + (ag % 3) * 0.5)), 4) AS frequency,
                   100.0 + camp * 25 AS daily_budget,
                   1 + (i % 30) / 10.0 AS avg_position,
                   round(0.3 + (ag % 10) * 0.1, 2) AS bid_amount,
                   CASE WHEN camp % 4 = 0 THEN 1 ELSE 0 END AS is_brand,
                   5.0 + (camp % 3) * 5 AS targeting_radius_km,
                   18.40 + camp / 100.0 AS store_lat, 73.70 + camp / 100.0 AS store_lng
            FROM spent""",
        {"impressions": Label({"sum"}), "clicks": Label({"sum"}), "spend": Label({"sum"}),
         "conversions": Label({"sum"}), "revenue": Label({"sum"}),
         "ctr_pct": Label({"none"}, ratio=(["clicks"], ["impressions"])),
         "cpc": Label({"none"}, ratio=(["spend"], ["clicks"])),
         "cpm": Label({"none"}),
         "roas": Label({"none"}, ratio=(["revenue"], ["spend"])),
         "conversion_rate": Label({"none"}, ratio=(["conversions"], ["clicks"])),
         "reach": Label({"none"}),
         "frequency": Label({"none"}, ratio=(["impressions"], ["reach"])),
         "daily_budget": Label({"mean", "none", "sum"}, per=["campaign_id"]),
         "avg_position": Label({"none", "mean"}),
         "bid_amount": Label({"none", "mean", "median"}, per=["ad_group_id"]),
         "targeting_radius_km": Label({"none", "mean"}, per=["campaign_id"]),
         "store_lat": Label({"none", "mean"}, per=["campaign_id"]),
         "store_lng": Label({"none", "mean"}, per=["campaign_id"])},
        ["spend is below zero in 3 row(s)",
         "roas is blank in 3 row(s)",
         "campaign_name writes 1 value(s) more than one way",
         "channel writes 1 value(s) more than one way ('Meta' / 'meta'",
         "ctr_pct is above 100 in 4 row(s)"],
        date_column="date",
        note="3 rows with clicks above impressions are a cross-column rule: not counted here")

    ga = Case(
        "ga_sessions",
        f"""SELECT 'S' || {i} AS session_id, 'U' || (({i} * 7) % 1800) AS user_id,
                   DATE '2024-03-01' + CAST({i} % 60 AS INTEGER) AS date,
                   CASE WHEN {i} % 20 = 0 THEN '(not set)' WHEN {i} % 7 = 0 THEN '(direct)'
                        WHEN {i} % 3 = 0 THEN 'google' ELSE 'facebook' END AS source,
                   CASE WHEN {i} % 20 <> 0 AND {i} % 7 = 0 THEN '(none)' ELSE 'cpc' END AS medium,
                   CASE WHEN {i} % 4 = 0 THEN '(not provided)' ELSE 'kw' || ({i} % 50) END
                        AS keyword,
                   CASE WHEN {i} % 3 = 0 THEN 1 ELSE 0 END AS is_bounce,
                   CASE WHEN {i} % 5 = 0 THEN 1 ELSE 0 END AS new_user,
                   CASE WHEN {i} % 3 = 0 THEN 0 ELSE 20 + ({i} * 13 + ({i} // 1800) * 7) % 600
                        END AS session_duration_sec,
                   CASE WHEN {i} % 3 = 0 THEN 1 ELSE 2 + ({i} + {i} // 1800) % 9 END
                        AS pageviews,
                   CASE WHEN {i} % 25 = 0 THEN 2 WHEN {i} % 11 = 0 THEN 1 ELSE 0 END
                        AS transactions,
                   CASE WHEN {i} % 25 = 0 THEN 180.0 WHEN {i} % 11 = 0 THEN 95.5 ELSE 0 END
                        AS transaction_revenue
            FROM range(5000) t({i})""",
        {"session_duration_sec": Label({"mean", "median", "sum"}),
         "pageviews": Label({"sum"}), "transactions": Label({"sum"}),
         "transaction_revenue": Label({"sum"})},
        ["source holds a placeholder for a missing value: '(not set)' in 250 row(s)",
         "keyword holds a placeholder for a missing value: '(not provided)' in 1,250 row(s)"],
        date_column="date",
        note="'(direct)' and '(none)' are real GA values: a caveat naming them is a false alarm")

    attribution = Case(
        "attribution",
        f"""WITH conv AS (SELECT {i} AS c, 1 + {i} % 3 AS touches,
                                 50.0 + ({i} * 17) % 400 AS value FROM range(1500) t({i}))
            SELECT 'CV' || c AS conversion_id, t AS touch_no,
                   CASE (c + t) % 4 WHEN 0 THEN 'search' WHEN 1 THEN 'social'
                        WHEN 2 THEN 'email' ELSE 'display' END AS touch_channel,
                   CASE touches WHEN 1 THEN 1.0 WHEN 2 THEN 0.5
                        ELSE CASE WHEN t = 3 THEN 0.34 ELSE 0.33 END END AS credit,
                   value AS conversion_value,
                   round(value * CASE touches WHEN 1 THEN 1.0 WHEN 2 THEN 0.5
                        ELSE CASE WHEN t = 3 THEN 0.34 ELSE 0.33 END END, 2) AS attributed_value
            FROM conv, range(1, 4) r(t) WHERE t <= touches""",
        {"credit": Label({"sum"}),
         "conversion_value": Label({"sum", "mean"}, per=["conversion_id"]),
         "attributed_value": Label({"sum"})},
        [], note="credit is a fraction per row that DOES add up: the sum is conversions")

    social = Case(
        "social_daily",
        f"""SELECT 'ACC' || ({i} % 5) AS account_id,
                   DATE '2024-01-01' + CAST({i} // 5 AS INTEGER) AS date,
                   10000 + ({i} % 5) * 5000 + ({i} // 5) * (3 + {i} % 5) AS followers,
                   {i} % 4 AS posts, (({i} * 31) % 900) + 20 AS likes,
                   round((({i} * 31) % 900 + 20) / (10000.0 + ({i} % 5) * 5000) * 100, 3)
                        AS engagement_rate
            FROM range(600) t({i})""",
        # mean accepted after the live run (25/09/2026): an average follower count is a
        # standard figure, like an average daily balance; the trap is the sum.
        {"followers": Label({"none", "mean"}), "posts": Label({"sum"}), "likes": Label({"sum"}),
         "engagement_rate": Label({"none", "mean"})},
        [], date_column="date", note="followers is a level on a date: summed over days, nothing")

    email = Case(
        "email_sends",
        f"""SELECT 'E' || {i} AS send_id, 'L' || ({i} % 6) AS list_id,
                   DATE '2024-02-01' + CAST({i} AS INTEGER) AS sent_on,
                   20000 + ({i} % 6) * 3000 AS list_size,
                   19000 + ({i} % 6) * 2800 + ({i} * 7) % 300 AS delivered,
                   CAST(floor((19000 + ({i} % 6) * 2800 + ({i} * 7) % 300) * (0.18 + ({i} % 9)
                        / 100.0)) AS BIGINT) AS opens,
                   (({i} * 13) % 900) + 100 AS clicks, ({i} * 3) % 40 AS unsubscribes,
                   round(floor((19000 + ({i} % 6) * 2800 + ({i} * 7) % 300) * (0.18 + ({i} % 9)
                        / 100.0)) / (19000 + ({i} % 6) * 2800 + ({i} * 7) % 300), 4) AS open_rate
            FROM range(120) t({i})""",
        {"delivered": Label({"sum"}), "opens": Label({"sum"}), "clicks": Label({"sum"}),
         "unsubscribes": Label({"sum"}),
         "open_rate": Label({"none"}, ratio=(["opens"], ["delivered"])),
         "list_size": Label({"none", "mean"}, per=["list_id"])},
        [], date_column="sent_on")
    return [ad, ga, attribution, social, email]


# ---- end to end ------------------------------------------------------------------------------

def end_to_end(con, ad: Case, filled_answers: dict) -> dict:
    """ad_daily through server.py: load, contract, top_n by channel, figures against SQL."""
    from backend.engine import workspace
    from backend.engine.contract import store

    out: dict = {}
    ws = "marketing_bench"
    workspace.reset(ws)
    # Exports into the bench's own workspace, removed below -- not docs/contracts/<ws>/, which
    # the first runs filled (as eval/run_eval.py does).
    saved_export = store.EXPORT_DIR
    store.EXPORT_DIR = workspace.workspace_dir(ws) / "contracts"
    try:
        return _end_to_end(con, ws, filled_answers, out)
    finally:
        store.EXPORT_DIR = saved_export
        workspace.reset(ws)
        workspace.workspace_dir(ws).rmdir()


def _end_to_end(con, ws: str, filled_answers: dict, out: dict) -> dict:
    from backend.engine import server
    from backend.engine.webapp.verify import figures, verify
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ad_daily.csv"
        con.execute(f"COPY ad_daily TO '{path}' (HEADER)")
        out["load"] = server.load_csv(str(path), "ad_daily", workspace_id=ws).split("\n")[0]
    a = dict(filled_answers)
    window = a.pop("analysis_window", None)
    measures = list(a.get("measures") or [])
    defs = {m: (a.get("measure_definitions") or {}).get(m) or f"{m} as the platform reports it"
            for m in measures}
    reply = server.propose_dataset_contract(
        "ad_daily", grain="one row = one ad group on one day",
        primary_key=["date", "ad_group_id"], date_column="date",
        measures=measures, dimensions=[d for d in (a.get("dimensions") or [])
                                       if d not in ("date", "ad_group_id")] + ["channel"],
        measure_definitions=defs, aggregations=a.get("aggregations"),
        analysis_window_start=str(window[0]) if window else None,
        analysis_window_end=str(window[1]) if window else None,
        measure_per=a.get("measure_per"), measure_columns=a.get("measure_columns"),
        ratios=a.get("ratios"), workspace_id=ws)
    if "```json" not in reply:
        out["contract"] = "NOT CONFIRMABLE: " + reply[-600:]
        return out
    body = reply.split("```json", 1)[1].split("```", 1)[0]
    out["contract"] = server.confirm_dataset_contract(body, workspace_id=ws).split("\n")[0]

    truth = {r[0]: [float(x) if x is not None else None for x in r[1:]] for r in con.execute(
        "SELECT channel, 100.0 * sum(clicks) / sum(impressions), sum(revenue) / sum(spend), "
        "avg(ctr_pct), avg(roas), sum(spend) FROM ad_daily GROUP BY channel").fetchall()}
    checks = []
    replies = {}
    for measure, k in (("ctr_pct_of_sums", 0), ("roas_of_sums", 1), ("spend", 4)):
        text = server.compute_analysis("ad_daily", "top_n", dimension="channel",
                                       measure=measure, n=10, workspace_id=ws)
        replies[measure] = text
        for ch, vals in truth.items():
            want = vals[k]
            got = [f.value for f in figures(text)]
            ok = any(abs(g - want) <= max(0.005, abs(want) * 1e-4) for g in got)
            checks.append({"measure": measure, "channel": ch, "want": round(want, 4), "ok": ok})
    out["figures"] = checks
    refused = server.compute_analysis("ad_daily", "top_n", dimension="channel",
                                      measure="ctr_pct", n=10, workspace_id=ws)
    out["total_of_a_ratio_refused"] = ("BLOCKED" in refused or "none" in refused.lower()
                                       and "declared" in refused.lower())
    out["trap"] = {ch: {"ctr_ratio_of_sums": round(v[0], 3), "ctr_mean_of_rows": round(v[2], 3),
                        "roas_ratio_of_sums": round(v[1], 3), "roas_mean_of_rows": round(v[3], 3)}
                   for ch, v in truth.items()}
    google = truth["google"]
    # The first run quoted the mean-of-rows CTR, 2.40% at two places -- the same as the ratio of
    # sums, so there was nothing to catch. ROAS is where the trap is large.
    answer = (f"On google the CTR is {google[0]:.2f}% and ROAS {google[1]:.2f}. "
              f"Averaged over rows, ROAS is {google[3]:.2f}.")
    v = verify(answer, list(replies.values()))
    out["verifier"] = {"answer": answer, "unsupported": v.unsupported, "found": v.found}
    return out


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

    # the engine's own reading of ad_daily, as a model that answered exactly that
    from backend.engine.contract import llm_filter
    from backend.engine.contract.evidence import gather, suggest_role
    ev = gather(con, "ad_daily", probe_pairs=False)
    roles = {c.name: suggest_role(c)[0] for c in ev.columns}
    rules_as_answer = {"columns": [{"name": m, "role": "measure", "agg": r[0] or "none",
                                    "per": list(r[1]), "confidence": 0.9}
                                   for m, r in results[0]["_got"].items()]}
    filled = llm_filter.check(con, "ad_daily", ev, rules_as_answer, roles)
    e2e = end_to_end(con, cs[0], filled.answers)

    for res in results:
        for k, r in enumerate(res["suggestions"]):
            head = (f"{res['case']:<14} {res['rows']:>7,} {res['seconds']:>5.2f}" if k == 0
                    else " " * 28)
            mark = "-" if r["right"] is None else "yes" if r["right"] else "NO"
            print(f"{head}  {r['measure']:<22} {str(r['agg']):<6} {'+'.join(r['per']) or '-':<12} "
                  f"{r['strength']:<7} {r['rule']:<3} {mark}")
    print()
    for strength, v in summary["by_strength"].items():
        print(f"{strength:<7} {v['count']:>3}: {v['right']} right, {v['wrong']} wrong, "
              f"{v['abstained']} no suggestion")
    print(f"strong precision {summary['strong_precision']:.1%}   strong coverage "
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
                print(f"  {r['case']:<14} error: {r['error']}")
                continue
            extra = []
            if r["wrong_but_checked"]:
                extra.append("WRONG AND MARKED CHECKED: " + ", ".join(r["wrong_but_checked"]))
            if r["wrong_left_for_review"]:
                extra.append("left for review: " + ", ".join(r["wrong_left_for_review"]))
            print(f"  {r['case']:<14} raw {r['raw']} filtered {r['filtered']} "
                  f"{r.get('model') or ''}")
            for x in extra:
                print(f"  {'':<14} {x}")
    print()
    print(f"end to end: {e2e.get('load')} | {e2e.get('contract')}")
    for f in e2e.get("figures", []):
        print(f"  {f['measure']:<16} {f['channel']:<9} want {f['want']:<12} "
              f"{'found in the reply' if f['ok'] else 'NOT FOUND'}")
    print(f"  a total of ctr_pct refused: {e2e.get('total_of_a_ratio_refused')}")
    for ch, t in (e2e.get("trap") or {}).items():
        print(f"  trap {ch:<9} CTR {t['ctr_ratio_of_sums']}% (sums) vs {t['ctr_mean_of_rows']}% "
              f"(mean of rows); ROAS {t['roas_ratio_of_sums']} vs {t['roas_mean_of_rows']}")
    if "verifier" in e2e:
        print(f"  verifier: '{e2e['verifier']['answer']}' -> unsupported "
              f"{e2e['verifier']['unsupported']}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
        args.json.write_text(json.dumps({"summary": summary, "cases": clean, "filter": filters,
                                         "end_to_end": e2e}, indent=1, default=str))
        print(f"\nwritten {args.json}")
    failed = (summary["wrong_strong"] or summary["caveats_missed"]
              or summary["caveat_lines_refuted"]
              or any(r.get("wrong_but_checked") for rows in filters.values() for r in rows)
              or not all(f["ok"] for f in e2e.get("figures", [{"ok": False}]))
              or not e2e.get("verifier", {}).get("unsupported"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
