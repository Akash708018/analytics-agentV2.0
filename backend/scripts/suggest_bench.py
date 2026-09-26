"""A small bench for the engine's contract suggestions and its own caveats (25/09/2026).

    uv run python scripts/suggest_bench.py [--retail PATH] [--json OUT]

Labelled datasets, built here in DuckDB so every expected answer is known by construction, carry
the traps a semantic layer exists for: a fee repeated on every line of an order, a percentage that
is one amount over another, a daily balance, a year and a zip code stored as numbers, a subtotal
row, placeholders, blanks that coincide with a channel, a few negative lines, a date column in two
formats, an empty month -- and one clean table, where anything said is a false alarm. With
--retail (or the web workspace's upload, found automatically) the retail fixture runs too, graded
against the counts the external review verified.

Scored:
- suggestions by strength: how many, and how many right. A wrong STRONG suggestion is the failure
  that matters -- it is the one the form fills on a click. Right = agg in the label's accepted set
  and `per` equal to the label's (and, for a ratio, its numerator and denominator).
- coverage: the share of labelled measures the engine settles strongly.
- caveats: recall of the expected ones (exact counts), and lines nothing expected (read them).
- metamorphic: rows shuffled (nothing changes), columns renamed to c1..cn (value evidence keeps
  P1 and R1; name rules fall to unsure, never to a wrong answer), rows duplicated (per-unit still
  holds), a totals row appended (the subtotal caveat appears).

Exit code 1 when a strong suggestion is wrong or an expected caveat is missed: the floor
tests/test_suggest_bench.py holds.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from backend.engine.contract import measured_caveats as mc  # noqa: E402
from backend.engine.contract import suggest as sg  # noqa: E402
from backend.engine.contract.evidence import gather, suggest_role  # noqa: E402

N = 6_000


@dataclass
class Label:
    accept: set[str | None]          # aggs counted right; {None} = no suggestion is right
    per: list[str] = field(default_factory=list)
    ratio: tuple[list[str], list[str]] | None = None


@dataclass
class Case:
    name: str
    sql: str
    measures: dict[str, Label]
    caveats: list[str]               # substrings, counts exact
    date_column: str | None = None
    note: str = ""


def _cases() -> list[Case]:
    i = "i"
    lines = f"""SELECT 'L' || {i} AS line_id, 'O' || ({i} // 3) AS order_id, 'C' || ({i} // 3 % 400)
                   AS customer_id,
                   ({i} % 3) + 1 AS line_no,
                   CASE WHEN {i} % 4 = 0 THEN 'Store' ELSE 'Online' END AS channel,
                   CASE WHEN {i} % 4 = 0 THEN NULL ELSE 'Ekart' END AS courier,
                   CASE WHEN {i} % 4 = 0 THEN NULL ELSE 30.0 + {i} % 90 END AS session_seconds,
                   CASE WHEN {i} % 50 = 0 THEN 'unknown' ELSE CAST(18 + {i} % 60 AS VARCHAR)
                        END AS customer_age,
                   (({i} * 31) % 40) * 12.5 + 99 AS unit_price,
                   CASE WHEN {i} % 1000 = 7 THEN -1 ELSE 1 + ({i} * 7) % 5 END AS quantity,
                   round(((({i} * 31) % 40) * 12.5 + 99) *
                         (CASE WHEN {i} % 1000 = 7 THEN -1 ELSE 1 + ({i} * 7) % 5 END), 2)
                         AS line_amount,
                   round(((({i} * 31) % 40) * 12.5 + 99) *
                         (CASE WHEN {i} % 1000 = 7 THEN -1 ELSE 1 + ({i} * 7) % 5 END)
                         * (0.55 + ({i} % 7) / 100.0), 2) AS line_cost,
                   (({i} // 3) % 3) * 25.0 AS shipping_fee,
                   (({i} // 3 % 400) % 9 + 1) * 5000.0 AS customer_credit_limit,
                   CASE WHEN {i} % 97 = 0
                        THEN strftime(DATE '2024-01-01' + CAST({i} % 300 AS INTEGER), '%d/%m/%Y')
                        ELSE strftime(DATE '2024-01-01' + CAST({i} % 300 AS INTEGER), '%Y-%m-%d')
                        END
                        AS delivery_date,
                   DATE '2024-01-01' + CAST(CASE WHEN {i} % 300 BETWEEN 60 AND 90 THEN 120
                        ELSE {i} % 300 END AS INTEGER) AS order_date,
                   'North' AS region
            FROM range({N}) t({i})"""
    shop = Case(
        "shop_lines",
        f"SELECT *, round(100.0 * (line_amount - line_cost) / line_amount, 2) AS margin_pct "
        f"FROM ({lines})",
        {"unit_price": Label({"none", "mean", "median"}),
         "quantity": Label({"sum"}),
         "line_amount": Label({"sum"}),
         "line_cost": Label({"sum"}),
         "shipping_fee": Label({"sum"}, per=["order_id"]),
         "customer_credit_limit": Label({"mean", "none"}, per=["customer_id"]),
         "session_seconds": Label({"mean", "none"}, per=[]),
         "margin_pct": Label({"none"}, ratio=(["line_amount", "-line_cost"], ["line_amount"]))},
        ["customer_age holds a placeholder for a missing value: 'unknown' in 120 row(s)",
         "courier and session_seconds are blank in the same 1,500 row(s) (25.0%) -- exactly the "
         "rows where channel = 'Store'",
         "quantity is below zero in 6 row(s)",
         "line_amount is below zero in 6 row(s)",
         "delivery_date is text: 5,938 value(s) read as dates and 62 are written another way",
         "order_date has no rows in 2024-03"],
        date_column="order_date",
        note="session_seconds varies within an order here: no unit is the right answer")
    balances = Case(
        "account_balances",
        f"""SELECT 'A' || ({i} % 100) AS account_id,
                   DATE '2024-01-01' + CAST({i} // 100 AS INTEGER) AS as_of,
                   1000.0 + (({i} * 17) % 5000) AS closing_balance,
                   (({i} * 13) % 300) * 1.0 AS deposits,
                   (({i} % 100) % 5) * 0.5 + 2.0 AS interest_rate
            FROM range({N}) t({i})""",
        {"closing_balance": Label({"none", "mean"}),  # an average balance is a fair figure
         "deposits": Label({"sum"}),
         "interest_rate": Label({"none", "mean"}, per=["account_id"])},
        [], date_column="as_of",
        note="interest_rate is one value per account: per account_id is the right unit")

    payroll = Case(
        "payroll_clean",
        f"""SELECT 'E' || ({i} % 200) AS employee_id, ({i} // 200) + 1 AS month_no,
                   ((({i} % 200) * 37) % 50 + 30) * 1000.0 AS monthly_salary,
                   (({i} * 11) % 900) * 1.0 AS bonus_amount,
                   (({i} * 7 + ({i} // 200) * 3) % 40) + 140.5 AS hours_worked
            FROM range({N}) t({i})""",
        {"monthly_salary": Label({"mean", "none"}, per=["employee_id"]),
         "bonus_amount": Label({"sum"}),
         "hours_worked": Label({"mean", "sum"})},
        [], note="clean: any caveat here is a false alarm")

    places = Case(
        "places",
        f"""SELECT 'P' || {i} AS place_id, 10000 + ({i} * 7919) % 89999 AS zip,
                   2000 + {i} % 25 AS year,
                   -23.5 + ({i} % 1000) / 1000.0 AS latitude,
                   (({i} * 29) % 400) / 10.0 - 5 AS temperature,
                   (({i} * 3) % 1000) / 1000.0 + 0.0005 AS share_of_votes,
                   (({i} * 41) % 777) * 1.0 AS col_x
            FROM range({N}) t({i})""",
        {"zip": Label({"none"}), "year": Label({"none"}), "latitude": Label({"none"}),
         "temperature": Label({"none", "mean"}), "share_of_votes": Label({"none", "mean"}),
         "col_x": Label({None, "sum", "mean", "none"})},
        [], note="Power BI summed temperatures; col_x has no right answer the data can give")
    last_mile = Case(
        "last_mile",
        f"""SELECT 'D' || ({i} // 2) AS order_id, ({i} % 2) + 1 AS stop_no,
                   DATE '2024-04-01' + CAST({i} % 60 AS INTEGER) AS order_date,
                   CASE WHEN {i} % 997 = 3 THEN -250.0 ELSE ((({i} * 17) % 3000) + 150.0) END
                        AS order_value_inr,
                   CASE WHEN {i} % 1499 = 5 THEN -1.5 ELSE ((({i} * 7) % 180) / 10.0 + 0.5) END
                        AS distance_km,
                   ((({i} * 3) % 90) / 10.0 + 0.2) AS package_weight_kg,
                   CASE WHEN {i} % 8 = 0 THEN NULL ELSE 9 + ({i} * 13) % 175 END
                        AS recorded_delivery_minutes,
                   ({i} * 29) % 400 AS gps_drop_distance_m,
                   ((({i} * 11) % 900) / 10.0 + 20) AS delivery_cost_inr,
                   1 + ({i} * 3) % 5 AS customer_rating,
                   CASE WHEN {i} % 500 = 9 THEN 0 ELSE 18.45 + ({i} % 200) / 1000.0 END AS drop_lat,
                   CASE WHEN {i} % 500 = 9 THEN 0 ELSE 73.80 + ({i} % 200) / 1000.0 END AS drop_lng,
                   CASE WHEN {i} % 4 = 0 THEN 1 ELSE 0 END AS is_late,
                   CASE WHEN {i} % 50 = 0 THEN 'PUNE_WEST' WHEN {i} % 3 = 0 THEN 'Pune_West'
                        ELSE 'Pune_East' END AS zone,
                   CASE WHEN {i} % 40 = 0 THEN NULL ELSE 'R' || ({i} % 30) END AS rider_id
            FROM range({N}) t({i})""",
        {"order_value_inr": Label({"sum"}),
         "distance_km": Label({"sum", "mean"}),
         "package_weight_kg": Label({"sum"}),
         "recorded_delivery_minutes": Label({"mean", "median", "none"}),
         "gps_drop_distance_m": Label({"none", "mean", "median"}),
         "delivery_cost_inr": Label({"sum"}),
         "customer_rating": Label({"none", "mean", "median"}),
         "drop_lat": Label({"none"}),
         "drop_lng": Label({"none"})},
        ["order_value_inr is below zero in 7 row(s)",
         "distance_km is below zero in 4 row(s)",
         "recorded_delivery_minutes is blank in 750 row(s)",
         "rider_id is blank in 150 row(s)",
         "drop_lat, drop_lng is (0, 0) in 12 row(s)",
         "zone writes 1 value(s) more than one way ('PUNE_WEST' / 'Pune_West'"],
        date_column="order_date",
        note="the vNext plan's strict last-mile shape: geo, distance, a 0/1 flag, spellings")
    return [shop, balances, payroll, places, last_mile]


RETAIL = Case(
    "retail_fixture", "",
    {"unit_price": Label({"none", "mean", "median"}),
     "line_revenue": Label({"sum"}), "line_cost": Label({"sum"}),
     "margin_pct": Label({"none"}, ratio=(["line_revenue", "-line_cost"], ["line_revenue"])),
     "order_shipping_fee": Label({"sum"}, per=["order_id"]),
     "rep_monthly_salary": Label({"mean", "none"}, per=["rep_id"]),
     "web_session_seconds": Label({"mean", "none"}, per=["order_id"]),
     "pages_viewed": Label({"sum", "mean", "none"}, per=["order_id"]),
     "discount_pct": Label({"none", "mean"}, per=["order_id"]),
     "stock_on_hand_at_order": Label({"none"})},
    ["customer_age holds a placeholder for a missing value: 'unknown' in 3,471 row(s)",
     "customer_state is blank in 3,473 row(s)",
     "exactly the rows where channel = 'Store'",
     "units is below zero in 8 row(s)",
     "delivery_date is text: 154,508 value(s) read as dates and 3,030 are written another way",
     "order_ts has no rows in 2024-09"],
    date_column="order_ts",
    note="counts as the external review verified them (25/09/2026)")


def _grade(s: sg.Suggestion | None, label: Label) -> bool | None:
    """True or False for a suggestion; None for an abstention (no agg), which is neither --
    coverage counts it."""
    if s is None or s.agg is None:
        return None
    if s.agg not in label.accept or list(s.per) != list(label.per):
        return False
    if label.ratio:
        return bool(s.ratio) and (s.ratio["numerator"], s.ratio["denominator"]) == label.ratio
    return True


def _n(text: str) -> int:
    return int(text.replace(",", ""))


def recount(con, table: str, line: str) -> bool | None:
    """The caveat's count taken again with plain SQL written apart from measured_caveats.py:
    True when it holds, False when it does not, None when the line is not a shape parsed here."""
    import re
    t = f'"{table}"'

    def one(q: str) -> int:
        return con.execute(q).fetchone()[0]

    m = re.match(r"(\w+) holds a placeholder for a missing value: (.+?) row\(s\)", line)
    if m:
        col = m.group(1)
        pairs = re.findall(r"'([^']*)' in ([\d,]+)", m.group(2))
        return all(one(f"SELECT count(*) FROM {t} WHERE trim(\"{col}\") = '{v}'") == _n(k)
                   for v, k in pairs)
    m = re.match(r"(.+?) (?:is|are) blank(?: in the same| in) ([\d,]+) row\(s\)", line)
    if m:
        cols = re.split(r", | and ", m.group(1))
        n = _n(m.group(2))
        blank = " AND ".join(f"(\"{c}\" IS NULL OR trim(CAST(\"{c}\" AS VARCHAR)) = '')"
                             for c in cols)
        ok = one(f"SELECT count(*) FROM {t} WHERE {blank}") == n and all(
            one(f"SELECT count(*) FROM {t} WHERE \"{c}\" IS NULL OR "
                f"trim(CAST(\"{c}\" AS VARCHAR)) = ''") == n for c in cols)
        near = re.search(r"; (\w+) holds a value in ([\d,]+) row\(s\) where (\w+) = ('?)"
                         r"([^'; ,]+)\4", line)
        if ok and near:
            val = near.group(5)
            test = ("IS NULL" if val == "NULL" and not near.group(4)
                    else f"= '{val}'" if near.group(4) else f"= {val}")
            ok = one(f"SELECT count(*) FROM {t} WHERE \"{near.group(3)}\" {test} AND "
                     f"\"{near.group(1)}\" IS NOT NULL") == _n(near.group(2))
        w = re.search(r"exactly the rows where (\w+) = ('?)([^' .]+(?: [^' .]+)*)\2", line)
        if ok and w:
            col, val = w.group(1), w.group(3)
            test = ("IS NULL" if val == "NULL" and not w.group(2)
                    else f"= '{val}'" if w.group(2) else f"= {val}")
            ok = (one(f"SELECT count(*) FROM {t} WHERE \"{col}\" {test}") == n
                  and one(f"SELECT count(*) FROM {t} WHERE \"{col}\" {test} AND {blank}") == n)
        return ok
    m = re.match(r"(\w+) is below zero in ([\d,]+) row\(s\)", line)
    if m:
        return one(f"SELECT count(*) FROM {t} WHERE \"{m.group(1)}\" < 0") == _n(m.group(2))
    m = re.match(r"(\w+) is text: ([\d,]+) value\(s\) read as dates and ([\d,]+)", line)
    if m:
        c = f'"{m.group(1)}"'
        return (one(f"SELECT count(TRY_CAST({c} AS DATE)) FROM {t}") == _n(m.group(2))
                and one(f"SELECT count({c}) - count(TRY_CAST({c} AS DATE)) FROM {t}")
                == _n(m.group(3)))
    m = re.match(r"(\w+) has no rows in (.+?) -- inside", line)
    if m:
        months = re.findall(r"\d{4}-\d{2}", m.group(2))
        return all(one(f"SELECT count(*) FROM {t} WHERE strftime(\"{m.group(1)}\", '%Y-%m') "
                       f"= '{mo}'") == 0 for mo in months)
    m = re.match(r"(\w+) is outside \[-(\d+), \d+\] in ([\d,]+) row\(s\)", line)
    if m:
        return one(f"SELECT count(*) FROM {t} WHERE abs(\"{m.group(1)}\") > {m.group(2)}") \
            == _n(m.group(3))
    m = re.match(r"(\w+), (\w+) is \(0, 0\) in ([\d,]+) row\(s\)", line)
    if m:
        return one(f"SELECT count(*) FROM {t} WHERE \"{m.group(1)}\" = 0 AND "
                   f"\"{m.group(2)}\" = 0") == _n(m.group(3))
    m = re.match(r"(\w+) and (\w+) look swapped in ([\d,]+) row\(s\)", line)
    if m:
        a, b = f'"{m.group(1)}"', f'"{m.group(2)}"'
        return one(f"SELECT count(*) FROM {t} WHERE abs({a}) > 90 AND abs({a}) <= 180 AND "
                   f"abs({b}) <= 90") == _n(m.group(3))
    m = re.match(r"(\w+) writes ([\d,]+) value\(s\) more than one way", line)
    if m:
        c = f'"{m.group(1)}"'
        return one(f"SELECT count(DISTINCT {c}) - count(DISTINCT lower(trim({c}))) FROM {t}") \
            == _n(m.group(2))
    m = re.match(r"(\w+) is above (1|100) in ([\d,]+) row\(s\)", line)
    if m:
        return one(f"SELECT count(*) FROM {t} WHERE \"{m.group(1)}\" > {m.group(2)}") \
            == _n(m.group(3))
    m = re.match(r"(\w+) falls under more than one (\w+) for ([\d,]+) value", line)
    if m:
        i, d = f'"{m.group(1)}"', f'"{m.group(2)}"'
        return one(f"SELECT count(*) FROM (SELECT {i} FROM {t} GROUP BY {i} "
                   f"HAVING count(DISTINCT {d}) > 1)") == _n(m.group(3))
    m = re.match(r"(\w+) is in the future in ([\d,]+) row\(s\)", line)
    if m:
        return one(f"SELECT count(*) FROM {t} WHERE CAST(\"{m.group(1)}\" AS DATE) > "
                   f"current_date") == _n(m.group(2))
    m = re.match(r"(\w+) mixes (\d+) currencies \((\w+):", line)
    if m:
        return one(f"SELECT count(DISTINCT \"{m.group(3)}\") FROM {t}") == _n(m.group(2))
    m = re.match(r"(\w+) is before (\w+) in ([\d,]+) row\(s\)", line)
    if m:
        return one(f"SELECT count(*) FROM {t} WHERE \"{m.group(1)}\" < \"{m.group(2)}\"") \
            == _n(m.group(3))
    m = re.match(r"(\w+) holds (.+) in ([\d,]+) row\(s\): if those are subtotal", line)
    if m:
        vals = re.findall(r"'([^']*)'", m.group(2))
        listed = ", ".join(f"'{v}'" for v in vals)
        return one(f"SELECT count(*) FROM {t} WHERE trim(\"{m.group(1)}\") IN ({listed})") \
            == _n(m.group(3))
    return None


def run_case(con, case: Case, table: str | None = None) -> dict:
    table = table or case.name
    t0 = time.perf_counter()
    ev = gather(con, table, probe_pairs=False)
    roles = {c.name: suggest_role(c)[0] for c in ev.columns}
    got = sg.suggest(con, table, ev, list(case.measures), case.date_column)
    caveats = mc.measure(con, table, ev, date_column=case.date_column, roles=roles)
    secs = time.perf_counter() - t0
    rows = []
    for m, label in case.measures.items():
        s = got.get(m)
        rows.append({"measure": m, "agg": s.agg if s else None, "per": list(s.per) if s else [],
                     "strength": s.strength if s else "absent", "rule": s.rule if s else "",
                     "right": _grade(s, label), "accept": sorted(map(str, label.accept)),
                     "label_per": label.per})
    checked = {line: recount(con, table, line) for line in caveats}
    found = [e for e in case.caveats if any(e in line for line in caveats)]
    extra = [line for line in caveats if not any(e in line for e in case.caveats)]
    return {"case": case.name, "rows": ev.row_count, "seconds": round(secs, 3),
            "suggestions": rows, "caveats_expected": len(case.caveats),
            "caveats_found": len(found), "caveats_missed": [e for e in case.caveats
                                                            if e not in found],
            "caveats_unexpected": extra, "note": case.note,
            "caveats_recounted": {"held": sum(v is True for v in checked.values()),
                                  "refuted": [k for k, v in checked.items() if v is False],
                                  "not_parsed": [k for k, v in checked.items() if v is None]},
            "_got": {m: (s.agg, tuple(s.per), s.strength, s.rule, s.ratio)
                     for m, s in got.items()},
            "_caveats": caveats}


def metamorphic(con, case: Case, base: dict) -> dict:
    """Four transformations of one case, each compared with its untransformed run."""
    out = {}
    con.execute(f"CREATE OR REPLACE TABLE m_shuffled AS SELECT * FROM {case.name} "
                f"ORDER BY hash(rowid)")
    r = run_case(con, case, "m_shuffled")
    out["shuffle"] = r["_got"] == base["_got"] and sorted(r["_caveats"]) == sorted(base["_caveats"])

    cols = [c[0] for c in con.execute(f"DESCRIBE {case.name}").fetchall()]
    rename = {c: f"c{k}" for k, c in enumerate(cols)}
    con.execute("CREATE OR REPLACE TABLE m_renamed AS SELECT "
                + ", ".join(f'"{c}" AS {rename[c]}' for c in cols) + f" FROM {case.name}")
    renamed = Case(case.name, "", {rename[m]: lab for m, lab in case.measures.items()}, [],
                   rename.get(case.date_column) if case.date_column else None)
    r = run_case(con, renamed, "m_renamed")
    kept, wrong = [], []
    for m, (agg, per, strength, rule, _) in base["_got"].items():
        after = r["_got"].get(rename[m])
        if rule in ("P1", "R1") and after and after[3] == rule:
            kept.append(m)
        lab = case.measures[m]
        moved = Label(lab.accept, per=[rename[x] for x in lab.per],
                      ratio=None if not lab.ratio else (
                          [("-" if x.startswith("-") else "") + rename[x.lstrip("-")]
                           for x in lab.ratio[0]], [rename[x] for x in lab.ratio[1]]))
        if after and after[2] == sg.STRONG and _grade(_as_suggestion(rename[m], after),
                                                      moved) is False:
            wrong.append(m)
    value_rules = [m for m, v in base["_got"].items() if v[3] in ("P1", "R1")]
    out["rename_value_rules_kept"] = f"{len(kept)} of {len(value_rules)}"
    out["rename_wrong_strong"] = wrong

    con.execute(f"CREATE OR REPLACE TABLE m_dup AS SELECT * FROM {case.name} UNION ALL "
                f"SELECT * FROM {case.name} USING SAMPLE 5 PERCENT (reservoir, 7)")
    r = run_case(con, case, "m_dup")
    out["duplicates_keep_units"] = all(
        r["_got"][m][1] == base["_got"][m][1] for m in base["_got"] if base["_got"][m][1])

    text_dims = [c for c in cols if con.execute(
        f"SELECT typeof(\"{c}\") FROM {case.name} LIMIT 1").fetchone()[0] == "VARCHAR"]
    target = "region" if "region" in cols else (text_dims[-1] if text_dims else None)
    if target:
        con.execute(f"CREATE OR REPLACE TABLE m_total AS SELECT * FROM {case.name} UNION ALL "
                    f"(SELECT * REPLACE ('Total' AS \"{target}\") FROM {case.name} LIMIT 1)")
        r = run_case(con, case, "m_total")
        out["totals_row_caught"] = any("'Total'" in line for line in r["_caveats"])
    return out


def _as_suggestion(name: str, got: tuple) -> sg.Suggestion:
    agg, per, strength, rule, ratio = got
    return sg.Suggestion(name, agg, strength, rule, "", per=list(per), ratio=ratio)


def summarise(results: list[dict]) -> dict:
    rows = [r for res in results for r in res["suggestions"]]
    by = {}
    for strength in (sg.STRONG, sg.LIKELY, sg.UNSURE):
        these = [r for r in rows if r["strength"] == strength]
        by[strength] = {"count": len(these), "right": sum(r["right"] is True for r in these),
                        "wrong": sum(r["right"] is False for r in these),
                        "abstained": sum(r["right"] is None for r in these)}
    labelled = [r for r in rows if r["accept"] != sorted(map(str, {None, "sum", "mean", "none"}))]
    wrong_strong = [(res["case"], r["measure"], r["agg"], r["per"], r["rule"])
                    for res in results for r in res["suggestions"]
                    if r["strength"] == sg.STRONG and not r["right"]]
    exp = sum(r["caveats_expected"] for r in results)
    got = sum(r["caveats_found"] for r in results)
    return {
        "measures": len(rows),
        "by_strength": by,
        "strong_precision": (by[sg.STRONG]["right"] / (by[sg.STRONG]["right"]
                                                        + by[sg.STRONG]["wrong"])
                             if by[sg.STRONG]["count"] else None),
        "likely_precision": (by[sg.LIKELY]["right"] / (by[sg.LIKELY]["right"]
                                                        + by[sg.LIKELY]["wrong"])
                             if by[sg.LIKELY]["right"] + by[sg.LIKELY]["wrong"] else None),
        "strong_coverage": sum(1 for r in labelled if r["strength"] == sg.STRONG) / len(labelled),
        "wrong_strong": wrong_strong,
        "caveat_recall": got / exp if exp else None,
        "caveats_missed": [m for r in results for m in r["caveats_missed"]],
        "caveats_unexpected": {r["case"]: r["caveats_unexpected"] for r in results
                               if r["caveats_unexpected"]},
        "caveat_lines": sum(r["caveats_recounted"]["held"] + len(r["caveats_recounted"]["refuted"])
                            + len(r["caveats_recounted"]["not_parsed"]) for r in results),
        "caveat_lines_held": sum(r["caveats_recounted"]["held"] for r in results),
        "caveat_lines_refuted": [x for r in results for x in r["caveats_recounted"]["refuted"]],
        "false_alarms_on_clean": sum(len(r["caveats_unexpected"]) for r in results
                                     if r["case"].endswith("_clean")),
    }


def _sum_everything(con, case: Case, table: str) -> dict:
    """A model with Power BI's default: every number a measure, every measure a sum."""
    cols = con.execute(f"DESCRIBE {table}").fetchall()
    return {"columns": [{"name": c[0], "role": "measure", "agg": "sum", "confidence": 0.9}
                        for c in cols if any(k in c[1] for k in ("INT", "DOUBLE", "DECIMAL"))]}


def _live_model(con, case: Case, table: str) -> dict:
    from backend.engine.webapp import autofill, llm
    from backend.engine.contract.evidence import gather as _gather
    ev = _gather(con, table, probe_pairs=False)
    answer, model, _ = llm.complete_json(llm.configured(), autofill.SYSTEM,
                                         autofill.packet(con, table, ev))
    answer["_model"] = model
    return answer


def filter_run(con, case: Case, answer: dict, table: str | None = None) -> dict:
    """One model's answer graded raw, then graded after contract/llm_filter.py."""
    from backend.engine.contract import llm_filter
    table = table or case.name
    ev = gather(con, table, probe_pairs=False)
    roles = {c.name: suggest_role(c)[0] for c in ev.columns}
    filled = llm_filter.check(con, table, ev, answer, roles)
    said = {e["name"]: e for e in answer.get("columns") or [] if isinstance(e, dict)}
    aggs = filled.answers["aggregations"]
    per = filled.answers["measure_per"] or {}
    raw, after, status = [], [], []
    for m, label in case.measures.items():
        e = said.get(m) or {}
        agg = e.get("agg") if e.get("role") == "measure" else None
        raw.append(_grade(_as_suggestion(m, (agg, tuple(e.get("per") or []), "", "", None)),
                          label))
        final = aggs.get(m)
        ratio = None
        if label.ratio:
            r = (filled.answers["ratios"] or {}).get(f"{m}_of_sums")
            ratio = {"numerator": r["numerator"], "denominator": r["denominator"]} if r else None
        after.append(_grade(_as_suggestion(m, (final, tuple(per.get(m, [])), "", "", ratio)),
                            label))
        chk = filled.by_path().get(f"measures[{m}].agg")
        status.append(chk.status if chk else "absent")
    tally = lambda xs: {"right": xs.count(True), "wrong": xs.count(False),
                        "blank": xs.count(None)}
    # Wrong where the filter said the data checked it is the failure; wrong where it said the
    # model alone decided is shown to the person as unchecked, which is the design.
    checked_wrong = [m for m, ok, st in zip(case.measures, after, status)
                     if ok is False and st in ("agree", "overruled", "data")]
    unchecked_wrong = [m for m, ok, st in zip(case.measures, after, status)
                       if ok is False and st not in ("agree", "overruled", "data")]
    return {"case": case.name, "answer": answer.get("columns"), "raw": tally(raw),
            "filtered": tally(after),
            "wrong_but_checked": checked_wrong, "wrong_left_for_review": unchecked_wrong,
            "verdicts": filled.counts()}


def _find_retail() -> Path | None:
    for p in sorted((ROOT / "workspace").glob("ws_*/uploads/retail_fixture.csv")):
        return p
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retail", type=Path, default=None)
    ap.add_argument("--no-retail", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--llm", action="store_true",
                    help="also fill every dataset with the configured live model (uses quota)")
    ap.add_argument("--replay", type=Path, default=None,
                    help="re-grade the live answers saved in an earlier --json output")
    args = ap.parse_args(argv)

    con = duckdb.connect(":memory:")
    cases = _cases()
    results, meta = [], {}
    for case in cases:
        con.execute(f"CREATE TABLE {case.name} AS {case.sql}")
        res = run_case(con, case)
        results.append(res)
        meta[case.name] = metamorphic(con, case, res)
    retail = None if args.no_retail else (args.retail or _find_retail())
    if retail and retail.exists():
        con.execute(f"CREATE TABLE retail_fixture AS SELECT * FROM read_csv('{retail}', "
                    f"nullstr=['', 'NA', '-'])")
        results.append(run_case(con, RETAIL))
    summary = summarise(results)
    filters = {"sum_everything": [filter_run(con, c, _sum_everything(con, c, c.name))
                                  for c in cases]}
    if args.llm:
        live = []
        for c in cases:
            try:
                answer = _live_model(con, c, c.name)
                row = filter_run(con, c, answer)
                row["model"] = answer.get("_model")
            except Exception as exc:  # a provider failure is a result here, not a crash
                row = {"case": c.name, "error": str(exc)[:200]}
            live.append(row)
            time.sleep(8)  # a free tier counts tokens per minute
        filters["live"] = live
    if args.replay:
        saved = json.loads(args.replay.read_text())["filter"].get("live", [])
        replayed = []
        for c in cases:
            row = next((r for r in saved if r.get("case") == c.name and r.get("answer")), None)
            if row:
                again = filter_run(con, c, {"columns": row["answer"]})
                again["model"] = row.get("model")
                replayed.append(again)
        filters["replayed"] = replayed

    print(f"{'case':<18} {'rows':>8} {'secs':>6}  measure                  agg    per          "
          f"strength rule right")
    for res in results:
        for k, r in enumerate(res["suggestions"]):
            head = (f"{res['case']:<18} {res['rows']:>8,} {res['seconds']:>6.2f}" if k == 0
                    else " " * 34)
            print(f"{head}  {r['measure']:<24} {str(r['agg']):<6} {'+'.join(r['per']) or '-':<12} "
                  f"{r['strength']:<8} {r['rule']:<4} "
                  f"{'-' if r['right'] is None else 'yes' if r['right'] else 'NO'}")
    print()
    for strength, v in summary["by_strength"].items():
        print(f"{strength:<7} {v['count']:>3}: {v['right']} right, {v['wrong']} wrong, "
              f"{v['abstained']} no suggestion")
    sp, lp = summary["strong_precision"], summary["likely_precision"]
    print(f"strong precision {sp:.1%}   likely precision "
          f"{'-' if lp is None else f'{lp:.1%}'}   strong coverage of labelled measures "
          f"{summary['strong_coverage']:.1%}")
    print(f"caveats: {summary['caveat_recall']:.1%} of expected found (missed "
          f"{summary['caveats_missed'] or 'none'}); every line recounted apart: "
          f"{summary['caveat_lines_held']} of {summary['caveat_lines']} held, refuted "
          f"{summary['caveat_lines_refuted'] or 'none'}; false alarms on the clean table "
          f"{summary['false_alarms_on_clean']}")
    for case, lines in summary["caveats_unexpected"].items():
        for line in lines:
            print(f"  not in the expected list ({case}): {line[:120]}")
    print()
    for case, m in meta.items():
        print(f"metamorphic {case:<17} " + "  ".join(f"{k}={v}" for k, v in m.items()))

    print()
    for name, rows in filters.items():
        ok = [r for r in rows if "raw" in r]
        tot = lambda part, k: sum(r[part][k] for r in ok)
        print(f"filter bench, {name}: raw {tot('raw', 'right')} right / {tot('raw', 'wrong')} "
              f"wrong / {tot('raw', 'blank')} blank -> after the data filter "
              f"{tot('filtered', 'right')} right / {tot('filtered', 'wrong')} wrong / "
              f"{tot('filtered', 'blank')} blank")
        for r in rows:
            if "error" in r:
                print(f"  {r['case']:<18} error: {r['error']}")
            else:
                print(f"  {r['case']:<18} raw {r['raw']}  filtered {r['filtered']}  "
                      f"{r.get('model', '')}")
                if r["wrong_left_for_review"]:
                    print(f"  {'':<18} left for review, marked model-only: "
                          f"{', '.join(r['wrong_left_for_review'])}")
                if r["wrong_but_checked"]:
                    print(f"  {'':<18} WRONG AND MARKED CHECKED: "
                          f"{', '.join(r['wrong_but_checked'])}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
        args.json.write_text(json.dumps({"summary": summary, "cases": clean,
                                         "metamorphic": meta, "filter": filters}, indent=1,
                                        default=str))
        print(f"\nwritten {args.json}")
    wrong_after = sum(len(r["wrong_but_checked"]) for rows in filters.values() for r in rows
                      if "wrong_but_checked" in r)
    failed = bool(summary["wrong_strong"] or summary["caveats_missed"] or wrong_after
                  or summary["caveat_lines_refuted"] or summary["false_alarms_on_clean"]
                  or not all(m.get("shuffle") for m in meta.values())
                  or any(m.get("rename_wrong_strong") for m in meta.values()))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
