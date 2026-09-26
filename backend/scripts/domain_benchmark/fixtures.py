"""Known-answer data, statistical and temporal edges, dirty variants, file formats, wrong calls.

Known answers are fixed by construction and written here as numbers -- they do not come from
the oracle -- so a plausible-looking wrong output cannot agree with a plausible-looking wrong
reference.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
from pathlib import Path

# --------------------------------------------------------------------------- helpers


def write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        if header:
            w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return path


def ingest_steps(prefix: str, path: str, name: str, ws: str) -> list[dict]:
    return [
        {"id": f"{prefix}_prop", "tool": "propose_ingest_spec", "stage": "setup",
         "kwargs": {"path": path, "dataset_name": name}},
        {"id": f"{prefix}_load", "tool": "confirm_ingest_spec", "stage": "setup",
         "kwargs": {"spec_json": f"$json:{prefix}_prop", "workspace_id": ws}},
    ]


def contract_steps(prefix: str, name: str, ws: str, **contract) -> list[dict]:
    return [
        {"id": f"{prefix}_cprop", "tool": "propose_dataset_contract", "stage": "setup",
         "kwargs": {"dataset_name": name, "workspace_id": ws, **contract}},
        {"id": f"{prefix}_cconf", "tool": "confirm_dataset_contract", "stage": "setup",
         "kwargs": {"contract_json": f"$json:{prefix}_cprop", "workspace_id": ws}},
    ]


def dedupe_steps(prefix: str, name: str, ws: str) -> list[dict]:
    """Every generated file carries exact duplicate rows, so its key is rightly refused until
    they are dropped: C001 before any contract on record_id (Step 13 run 2: H and J met the gate
    on every call because their setup skipped this)."""
    return [
        {"id": f"{prefix}_plan", "tool": "propose_cleaning_plan", "stage": "setup",
         "kwargs": {"dataset_name": name, "workspace_id": ws}},
        {"id": f"{prefix}_dedupe", "tool": "apply_cleaning_plan", "stage": "setup",
         "kwargs": {"dataset_name": name, "approved_action_ids": ["C001"], "workspace_id": ws}},
    ]


def call(sid, analysis, params, ws, name, known=None, stage="known", **extra):
    return {"id": sid, "tool": "compute_analysis", "stage": stage, "analysis": analysis,
            "kwargs": {"dataset_name": name, "analysis_type": analysis, "workspace_id": ws,
                       **params}, "verify": True, "known": known, **extra}


# --------------------------------------------------------------------------- known answers


def known_answer(root: Path, ws: str) -> dict:
    """240 rows: 10 per month over 2023-2024, plus 4 exact duplicate rows."""
    rows = []
    start = dt.date(2023, 1, 1)
    cust_rows = []
    for m in range(24):
        y, mo = 2023 + m // 12, m % 12 + 1
        for k in range(10):
            i = m * 10 + k
            d = dt.date(y, mo, 1 + k)
            g10 = i % 10
            cust = (f"K{k}" if m == 0 else f"K{k}" if (m == 1 and k < 5) else
                    f"K{k}" if (m == 2 and k < 2) else f"N{i:04d}")
            rows.append([
                f"R{i:04d}", d.isoformat(),
                10 + m,                                   # trend: monthly sum 100 + 10m
                10 if (y, mo) < (2024, 7) else 20,        # changepoint after 2024-06
                15 if mo == 12 else 10,                   # seasonality: December high
                i, 2 * i + 3, -3 * i + 7, (i - 119.5) ** 2,   # perfect +1, -1, zero correlation
                f"g{g10}", 100.0 if g10 < 2 else 6.25,    # pareto: g0 + g1 = 80%
                10 - g10,                                 # ranking: g0 > g1 > ... > g9
                f"h{i % 2}", (i // 2) % 12,               # identical groups by h
                (i % 7) + (0 if i % 2 == 0 else 100),     # strongly different groups by h
                "true" if i < 3 else "false",             # 1.25% imbalance
                None if i % 10 == 0 else 1.0,             # exactly 10% missing
                cust, [1, 2, 3, 4, 5, 6, 7, 8, 9][i % 9],  # mean 5 / median 5 over 9-cycles
            ])
    header = ["id", "d", "tr", "cp", "se", "x", "ypos", "yneg", "z0", "g10", "par", "rk", "h",
              "ident", "diff", "flag", "miss", "cust", "nine"]
    dups = [rows[5], rows[50], rows[100], rows[200]]
    path = write_csv(root / "known_answer.csv", header, rows + dups)
    name = "known"
    contract = dict(grain="one row", primary_key=["id"], date_column="d",
                    measures=["tr", "cp", "se", "x", "ypos", "yneg", "z0", "par", "rk", "ident",
                              "diff", "miss", "nine"],
                    dimensions=["g10", "h", "flag", "cust"],
                    aggregations={m: "sum" for m in ["tr", "cp", "se", "x", "ypos", "yneg", "z0",
                                                     "par", "rk", "ident", "diff", "miss",
                                                     "nine"]},
                    measure_definitions={m: m for m in ["tr", "cp", "se", "x", "ypos", "yneg",
                                                        "z0", "par", "rk", "ident", "diff",
                                                        "miss", "nine"]},
                    analysis_window_start="2023-01-01", analysis_window_end="2024-12-31")
    steps = ingest_steps("ka", str(path), name, ws)
    steps.append({"id": "ka_plan", "tool": "propose_cleaning_plan", "stage": "known",
                  "kwargs": {"dataset_name": name, "workspace_id": ws},
                  "known": {"dedupe_count": 4}})
    steps.append({"id": "ka_apply", "tool": "apply_cleaning_plan", "stage": "known",
                  "kwargs": {"dataset_name": name, "approved_action_ids": ["C001"],
                             "workspace_id": ws}})
    steps += contract_steps("ka", name, ws, **contract)
    W, N = ws, name
    steps += [
        call("ka_corr_pos", "correlation", {"measure": "x", "against": "ypos"}, W, N,
             known={"pearson r": 1.0, "spearman rho": 1.0}),
        call("ka_corr_neg", "correlation", {"measure": "x", "against": "yneg"}, W, N,
             known={"pearson r": -1.0, "spearman rho": -1.0}),
        call("ka_corr_zero", "correlation", {"measure": "x", "against": "z0"}, W, N,
             known={"pearson r": 0.0}),
        call("ka_trend", "trend", {"measure": "tr", "grain": "month"}, W, N,
             known={"monthly_sum": "100 + 10 * month_index"}),
        call("ka_cp", "changepoint", {"measure": "cp", "grain": "month"}, W, N,
             known={"top_split": "2024-06", "difference": 100}),
        call("ka_season", "seasonality", {"measure": "se", "grain": "month"}, W, N,
             known={"top_position": "Dec", "Dec_mean": 150, "other_mean": 100}),
        call("ka_pareto", "pareto", {"dimension": "g10", "measure": "par"}, W, N,
             known={"top2_running_share": 0.8}),
        call("ka_top", "top_n", {"dimension": "g10", "measure": "rk", "n": 10}, W, N,
             known={"order": [f"g{k}" for k in range(10)], "g0": 240}),
        call("ka_growth", "period_compare", {"measure": "tr", "period": "2024-12",
                                             "baseline": "2024-11", "grain": "month"}, W, N,
             known={"2024-11": 320, "2024-12": 330}),     # months 22 and 23: 100 + 10m
        call("ka_ident_t", "hypothesis_test", {"dimension": "h", "measure": "ident"}, W, N,
             known={"t": 0.0, "p": 1.0}),
        call("ka_ident_g", "effect_size", {"dimension": "h", "measure": "ident"}, W, N,
             known={"g": 0.0}),
        call("ka_diff_t", "hypothesis_test", {"dimension": "h", "measure": "diff"}, W, N,
             known={"p_below": 1e-10}),
        call("ka_imb", "frequency", {"column": "flag"}, W, N,
             known={"true": 3, "false": 237}),
        # 240 rows of a 1..9 cycle: 26 whole cycles and 1..6 -> mean 1191 / 240 = 4.9625
        call("ka_miss", "summary_stats", {}, W, N, known={"miss_nulls": 24, "miss_n": 216,
                                                          "nine_mean": 4.9625,
                                                          "nine_median": 5.0}),
        call("ka_retention", "cohort_retention", {"entity": "cust"}, W, N,
             known={"2023-01": {"size": 10, "+1": 5, "+2": 2}}),
        call("ka_outliers", "outlier_detection", {"measure": "x"}, W, N,
             known={"tukey_flagged": 0}),
    ]
    return {"name": "known_answer", "path": str(path), "steps": steps, "ws": ws}


# --------------------------------------------------------------------------- statistical edges


def stat_edges(root: Path, ws: str) -> dict:
    rows = []
    for i in range(60):
        g = "a" if i < 30 else "b"
        rows.append([f"S{i:03d}", (dt.date(2024, 1, 1) + dt.timedelta(days=i)).isoformat(),
                     g, "solo" if i == 0 else g,                   # a one-member group
                     5.0,                                          # zero variance
                     None if i else 7.0,                           # one valid observation
                     float(i % 3),                                 # ties
                     (1e6 if i == 59 else float(i)),              # an extreme outlier
                     round(random.Random(i).lognormvariate(0, 2), 4),   # severe skew
                     i * 2.0 + (0.001 if i == 7 else 0),           # near-perfect correlation
                     "big" if i < 57 else "tiny",                  # huge category imbalance
                     0.0])                                         # all zero
    header = ["id", "d", "g", "g_solo", "const", "one", "ties", "extreme", "skew", "near",
              "imb", "zero"]
    path = write_csv(root / "stat_edges.csv", header, rows)
    ms = ["const", "one", "ties", "extreme", "skew", "near", "zero"]
    contract = dict(grain="one row", primary_key=["id"], date_column="d", measures=ms,
                    dimensions=["g", "g_solo", "imb"], aggregations={m: "sum" for m in ms},
                    measure_definitions={m: m for m in ms},
                    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    W, N = ws, "stat_edges"
    steps = ingest_steps("se", str(path), N, ws) + contract_steps("se", N, ws, **contract)
    cases = [
        ("zero_variance_t", "hypothesis_test", {"dimension": "g", "measure": "const"}, "reject",
         ["variance", "const"]),
        ("zero_variance_ci", "confidence_interval", {"measure": "const"}, "either", []),
        ("zero_variance_corr", "correlation", {"measure": "const", "against": "near"}, "reject",
         ["const"]),
        ("one_valid_obs_ci", "confidence_interval", {"measure": "one"}, "reject", ["one"]),
        ("one_valid_obs_t", "hypothesis_test", {"dimension": "g", "measure": "one"}, "reject",
         ["one"]),
        ("single_member_group_t", "hypothesis_test", {"dimension": "g_solo", "measure": "ties"},
         "either", ["solo"]),
        ("single_member_effect", "effect_size", {"dimension": "g_solo", "measure": "ties"},
         "either", ["solo"]),
        ("ties_rank", "hypothesis_test", {"dimension": "g", "measure": "ties",
                                          "method": "rank"}, "accept", []),
        ("extreme_outlier", "outlier_detection", {"measure": "extreme"}, "accept", []),
        ("severe_skew_t", "hypothesis_test", {"dimension": "g", "measure": "skew"}, "accept",
         []),
        ("near_perfect_corr", "correlation", {"measure": "near", "against": "ties"}, "accept",
         []),
        ("perfect_corr_self", "correlation", {"measure": "near", "against": "near"}, "reject",
         ["near"]),
        ("imbalanced_groups_t", "hypothesis_test", {"dimension": "imb", "measure": "skew"},
         "accept", []),
        ("all_zero_ci", "confidence_interval", {"measure": "zero"}, "either", []),
        ("all_zero_corr", "correlation", {"measure": "zero", "against": "near"}, "reject",
         ["zero"]),
        ("tiny_sample_adequacy", "sample_adequacy", {"dimension": "imb", "measure": "skew"},
         "accept", []),
    ]
    for sid, analysis, params, expect, mention in cases:
        steps.append(call(f"se_{sid}", analysis, params, W, N, stage="stat_edges",
                          expect=expect, must_mention=mention, case=sid, family="statistical"))
    return {"name": "stat_edges", "path": str(path), "steps": steps, "ws": ws}


# --------------------------------------------------------------------------- temporal edges


def temporal_edges(root: Path, ws: str) -> dict:
    rng = random.Random(8)
    base = []
    for i in range(730):
        d = dt.date(2023, 1, 1) + dt.timedelta(days=i)
        weekly = 5 if d.weekday() >= 5 else 0
        yearly = 10 if d.month in (11, 12) else 0
        ramp = i / 73.0                                         # a gradual shift
        base.append([f"T{i:04d}", d.isoformat(), 10 + weekly + yearly, round(10 + ramp, 4),
                     10.0, d.isoformat() + "T10:00:00+02:00"])
    header = ["id", "d", "multi", "ramp", "flat", "tz"]
    sorted_path = write_csv(root / "temporal_sorted.csv", header, base)
    shuffled = base[:]
    rng.shuffle(shuffled)
    shuffled_path = write_csv(root / "temporal_shuffled.csv", header, shuffled)
    irregular = [r for r in base if rng.random() < 0.3]
    irregular += [[f"D{k}"] + irregular[3][1:] for k in range(3)]   # three duplicate dates
    irregular_path = write_csv(root / "temporal_irregular.csv", header, irregular)
    one_period = [[f"O{i}", f"2024-05-{1 + i % 28:02d}", i, i, 10.0,
                   f"2024-05-{1 + i % 28:02d}T10:00:00+02:00"] for i in range(50)]
    one_path = write_csv(root / "temporal_one_period.csv", header, one_period)
    ms = ["multi", "ramp", "flat"]
    contract = dict(grain="one day", primary_key=["id"], date_column="d", measures=ms,
                    dimensions=[], aggregations={m: "sum" for m in ms},
                    measure_definitions={m: m for m in ms},
                    analysis_window_start="2023-01-01", analysis_window_end="2024-12-31")
    steps = []
    for tag, path, name in (("sorted", sorted_path, "t_sorted"),
                            ("shuffled", shuffled_path, "t_shuffled"),
                            ("irregular", irregular_path, "t_irregular"),
                            ("oneperiod", one_path, "t_oneperiod")):
        steps += ingest_steps(f"te_{tag}", str(path), name, ws)
        steps += contract_steps(f"te_{tag}", name, ws, **contract)
        for sid, analysis, params, expect in (
                ("trend_day", "trend", {"measure": "multi", "grain": "day"}, "either"),
                ("trend_week", "trend", {"measure": "multi", "grain": "week"}, "either"),
                ("trend_month", "trend", {"measure": "multi", "grain": "month"}, "accept"),
                ("season_month", "seasonality", {"measure": "multi", "grain": "month"},
                 "either"),
                ("season_week", "seasonality", {"measure": "multi", "grain": "week"}, "either"),
                ("season_flat", "seasonality", {"measure": "flat", "grain": "month"}, "either"),
                ("cp_ramp", "changepoint", {"measure": "ramp", "grain": "month"}, "either"),
                ("coverage_day", "calendar_coverage", {"grain": "day"}, "either")):
            steps.append(call(f"te_{tag}_{sid}", analysis, params, ws, name, stage="temporal",
                              expect=expect, case=f"{tag}:{sid}", family="temporal"))
    # timezone-aware timestamps as the date column
    tz_contract = dict(contract, date_column="tz", primary_key=["id"])
    steps += ingest_steps("te_tz", str(sorted_path), "t_tz", ws)
    steps += contract_steps("te_tz", "t_tz", ws, **tz_contract)
    steps.append(call("te_tz_trend", "trend", {"measure": "multi", "grain": "month"}, ws,
                      "t_tz", stage="temporal", expect="either", case="tz:trend",
                      family="temporal"))
    return {"name": "temporal_edges", "steps": steps, "ws": ws,
            "paths": {"sorted": str(sorted_path), "shuffled": str(shuffled_path)}}


# --------------------------------------------------------------------------- dirty variants


def dirty_variants(root: Path, clean_csv: Path, ws: str) -> dict:
    """Five corrupted copies of a clean financial file. `injected` names every defect by column,
    so the cleaning plan can be judged action by action."""
    with clean_csv.open(newline="") as f:
        rows = list(csv.reader(f))
    header, data = rows[0], rows[1:]
    rng = random.Random(3)
    ix = {h: k for k, h in enumerate(header)}
    noise = []
    for r in data:
        r = r[:]
        if r[ix["category"]] and rng.random() < 0.3:
            r[ix["category"]] = f"  {r[ix['category']]} "
        if r[ix["payment_method"]]:
            r[ix["payment_method"]] = rng.choice([str.upper, str.lower, str.title])(
                r[ix["payment_method"]])
        if r[ix["revenue"]]:
            r[ix["revenue"]] = f"${float(r[ix['revenue']]):,.2f}"
        if r[ix["risk_score"]]:
            r[ix["risk_score"]] = f"{float(r[ix['risk_score']]) * 100:.2f}%"
        if r[ix["balance"]]:
            r[ix["balance"]] = f"{float(r[ix['balance']]):,.2f}"
        if rng.random() < 0.05:
            r[ix["expense"]] = rng.choice(["NA", "N/A", "null", "None", "unknown", ""])
        if rng.random() < 0.02:
            r[ix["branch"]] = rng.choice(["Zürich-Ω", "São Paulo", "東京", "Łódź"])
        if rng.random() < 0.01:
            r[ix["category"]] = "x" * 300
        noise.append(r + [""])
    noise_header = [("Amount (USD)" if h == "amount" else "risk score %" if h == "risk_score"
                     else h) for h in header] + ["notes"]
    v1 = write_csv(root / "dirty_format_noise.csv", noise_header, noise)
    injected_v1 = {"category": "padding and 300-character labels",
                   "payment_method": "inconsistent capitalisation",
                   "revenue": "currency symbol and thousands separator",
                   "risk score %": "percent sign", "balance": "thousands separator",
                   "expense": "NA / N/A / null / None / unknown tokens",
                   "branch": "unicode labels", "notes": "an empty column"}
    dates = []
    for r in data:
        r = r[:]
        u = rng.random()
        if u < 0.01:
            r[ix["transaction_date"]] = rng.choice(["2024-13-45", "not a date", "31/31/2024"])
        elif u < 0.02:
            r[ix["transaction_date"]] = "2023-02-30"
        elif u < 0.5 and r[ix["transaction_date"]]:
            d = dt.date.fromisoformat(r[ix["transaction_date"]])
            r[ix["transaction_date"]] = rng.choice([d.strftime("%d/%m/%Y"),
                                                    d.strftime("%b %d, %Y")])
        dates.append(r)
    v2 = write_csv(root / "dirty_bad_dates.csv", header, dates)
    dup_header = header[:]
    dup_header[ix["expense"]] = "category"
    v3 = write_csv(root / "dirty_duplicate_columns.csv", dup_header, data)
    v4 = write_csv(root / "dirty_missing_header.csv", [], data)
    order = list(reversed(range(len(header))))
    v5 = write_csv(root / "dirty_reordered_extra.csv", [header[k] for k in order] + ["extra"],
                   [[r[k] for k in order] + ["x"] for r in data])
    variants = [("format_noise", v1, "accept", injected_v1),
                ("bad_dates", v2, "warn", {"transaction_date": "malformed, impossible and "
                                                               "mixed-format dates"}),
                ("duplicate_columns", v3, "either", {"category": "duplicated column name"}),
                ("missing_header", v4, "warn", {}),
                ("reordered_extra", v5, "accept", {"extra": "an unexpected extra column"})]
    steps = []
    for tag, path, expect, injected in variants:
        name = f"dirty_{tag}"
        steps += [
            {"id": f"dv_{tag}_check", "tool": "check_file", "stage": "dirty",
             "kwargs": {"path": str(path)}, "case": tag},
            {"id": f"dv_{tag}_prop", "tool": "propose_ingest_spec", "stage": "dirty",
             "kwargs": {"path": str(path), "dataset_name": name}, "case": tag, "expect": expect},
            {"id": f"dv_{tag}_load", "tool": "confirm_ingest_spec", "stage": "dirty",
             "kwargs": {"spec_json": f"$json:dv_{tag}_prop", "workspace_id": ws}, "case": tag,
             "expect": expect},
            {"id": f"dv_{tag}_profile", "tool": "profile_dataset", "stage": "dirty",
             "kwargs": {"dataset_name": name, "workspace_id": ws}, "case": tag},
            {"id": f"dv_{tag}_plan", "tool": "propose_cleaning_plan", "stage": "dirty",
             "kwargs": {"dataset_name": name, "workspace_id": ws}, "case": tag,
             "known": {"injected": injected}},
            {"id": f"dv_{tag}_describe", "tool": "describe_dataset", "stage": "dirty",
             "kwargs": {"dataset_name": name, "workspace_id": ws}, "case": tag},
        ]
    return {"name": "dirty_variants", "steps": steps, "ws": ws,
            "variants": {t: {"path": str(p), "expect": e, "injected": i}
                         for t, p, e, i in variants}}


# --------------------------------------------------------------------------- file formats


def format_cases(root: Path, clean_csv: Path, ws: str) -> dict:
    import duckdb
    import openpyxl

    with clean_csv.open(newline="") as f:
        rows = list(csv.reader(f))
    xlsx = root / "fmt_valid.xlsx"
    wb = openpyxl.Workbook()
    sh = wb.active
    for r in rows:
        sh.append(r)
    wb.save(xlsx)
    jsn = root / "fmt_valid.json"
    jsn.write_text(json.dumps([dict(zip(rows[0], r)) for r in rows[1:]]))
    parquet = root / "fmt_valid.parquet"
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_csv_auto('{clean_csv}')) TO '{parquet}' "
                f"(FORMAT PARQUET)")
    con.close()
    empty = root / "fmt_empty.csv"
    empty.write_bytes(b"")
    header_only = write_csv(root / "fmt_header_only.csv", rows[0], [])
    zero_xlsx = root / "fmt_zero.xlsx"
    zero_xlsx.write_bytes(b"")
    corrupt = root / "fmt_corrupt.xlsx"
    corrupt.write_bytes(xlsx.read_bytes()[: len(xlsx.read_bytes()) // 3])
    bad_ext = root / "fmt_data.dat"
    bad_ext.write_bytes(clean_csv.read_bytes())
    cases = [
        ("csv_valid", "propose_ingest_spec", {"path": str(clean_csv), "dataset_name": "f_csv"},
         "accept", []),
        ("xlsx_valid", "propose_ingest_spec", {"path": str(xlsx), "dataset_name": "f_xlsx"},
         "accept", []),
        ("xlsx_load", "load_excel", {"path": str(xlsx), "dataset_name": "f_xlsx_direct"},
         "accept", []),
        ("json_unsupported", "propose_ingest_spec", {"path": str(jsn), "dataset_name": "f_json"},
         "reject", ["json"]),
        ("parquet_unsupported", "propose_ingest_spec",
         {"path": str(parquet), "dataset_name": "f_parquet"}, "reject", ["parquet"]),
        ("csv_to_excel_loader", "load_excel", {"path": str(clean_csv), "dataset_name": "f_w1"},
         "reject", ["xlsx"]),
        ("xlsx_to_csv_loader", "load_csv", {"path": str(xlsx), "dataset_name": "f_w2"},
         "reject", []),
        ("invalid_extension", "propose_ingest_spec", {"path": str(bad_ext), "dataset_name": "f_d"},
         "either", [".dat"]),
        ("corrupted_xlsx", "propose_ingest_spec", {"path": str(corrupt), "dataset_name": "f_c"},
         "reject", ["xlsx"]),
        ("corrupted_xlsx_direct", "load_excel", {"path": str(corrupt), "dataset_name": "f_c2"},
         "reject", ["xlsx"]),
        ("empty_file", "propose_ingest_spec", {"path": str(empty), "dataset_name": "f_e"},
         "reject", ["empty"]),
        ("empty_file_load", "load_csv", {"path": str(empty), "dataset_name": "f_e2"}, "reject",
         []),
        ("header_only", "propose_ingest_spec", {"path": str(header_only),
                                                "dataset_name": "f_h"}, "either", []),
        ("header_only_load", "load_csv", {"path": str(header_only), "dataset_name": "f_h2"},
         "either", []),
        ("zero_byte_xlsx", "propose_ingest_spec", {"path": str(zero_xlsx),
                                                   "dataset_name": "f_z"}, "reject", []),
        ("check_json", "check_file", {"path": str(jsn)}, "either", []),
        ("preview_parquet", "preview_file", {"path": str(parquet)}, "either", []),
    ]
    steps = []
    for sid, tool, kw, expect, mention in cases:
        if tool in ("load_excel", "load_csv"):
            kw = {**kw, "workspace_id": ws}
        steps.append({"id": f"fmt_{sid}", "tool": tool, "stage": "formats", "kwargs": kw,
                      "expect": expect, "must_mention": mention, "case": sid,
                      "family": "file_format"})
    return {"name": "file_formats", "steps": steps, "ws": ws,
            "formats": {"csv": "supported", "xlsx": "supported",
                        "json": "not supported by the project", "parquet": "not supported"}}


# --------------------------------------------------------------------------- wrong calls


def wrong_calls(root: Path, clean_csv: Path, ws: str, fresh_ws: str) -> dict:
    """Section 12's list, each with the behaviour it should get and the words a good message
    must contain (the offending parameter or value)."""
    name = "adv"
    tiny = root / "adv_tiny"
    one = write_csv(tiny / "one_row.csv", ["id", "d", "g", "v"], [["a", "2024-01-05", "x", 3]])
    two = write_csv(tiny / "two_rows.csv", ["id", "d", "g", "v"],
                    [["a", "2024-01-05", "x", 3], ["b", "2024-02-05", "y", 4]])
    allnull = write_csv(tiny / "all_null.csv", ["id", "d", "g", "v", "w"],
                        [[f"r{i}", f"2024-01-{1 + i % 28:02d}", "x", None, i] for i in range(20)])
    nulls_everywhere = write_csv(tiny / "all_null_dataset.csv", ["a", "b"],
                                 [[None, None] for _ in range(10)])
    infs = write_csv(tiny / "inf_nan.csv", ["id", "d", "g", "v"],
                     [[f"r{i}", f"2024-01-{1 + i % 28:02d}", "x" if i % 2 else "y",
                       ["inf", "-inf", "nan", "NaN", str(i)][i % 5]] for i in range(40)])
    dupid = write_csv(tiny / "dup_ids.csv", ["id", "d", "g", "v"],
                      [[f"r{i % 5}", f"2024-01-{1 + i % 28:02d}", "x", i] for i in range(20)])
    W = ws
    tc = dict(grain="one row", primary_key=["id"], date_column="d", measures=["v"],
              dimensions=["g"], aggregations={"v": "sum"}, measure_definitions={"v": "v"},
              analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    steps = [
        # wrong workflow state, in a workspace where nothing is loaded
        {"id": "st_analysis_before_ingest", "tool": "compute_analysis",
         "kwargs": {"dataset_name": name, "analysis_type": "summary_stats",
                    "workspace_id": fresh_ws}, "expect": "reject", "must_mention": [name]},
        {"id": "st_report_before_ingest", "tool": "build_report",
         "kwargs": {"dataset_name": name, "question": "q", "workspace_id": fresh_ws},
         "expect": "reject", "must_mention": [name]},
        {"id": "st_state_empty", "tool": "get_workflow_state",
         "kwargs": {"workspace_id": fresh_ws}, "expect": "accept", "must_mention": []},
    ]
    steps += ingest_steps("adv", str(clean_csv), name, W)
    steps += [
        {"id": "st_profile_before_contract", "tool": "profile_dataset",
         "kwargs": {"dataset_name": name, "workspace_id": W}, "expect": "accept",
         "must_mention": []},
        {"id": "st_analysis_before_contract", "tool": "compute_analysis",
         "kwargs": {"dataset_name": name, "analysis_type": "top_n", "dimension": "region",
                    "measure": "revenue", "workspace_id": W}, "expect": "reject",
         "must_mention": ["contract"]},
        {"id": "st_report_before_analysis", "tool": "build_report",
         "kwargs": {"dataset_name": name, "question": "q", "workspace_id": W},
         "expect": "either", "must_mention": []},
        {"id": "st_invalid_ingest_spec", "tool": "confirm_ingest_spec",
         "kwargs": {"spec_json": '{"path": 5}', "workspace_id": W}, "expect": "reject",
         "must_mention": ["path"]},
        {"id": "st_invalid_contract", "tool": "confirm_dataset_contract",
         "kwargs": {"contract_json": '{"dataset_name": "adv", "grain": 7}', "workspace_id": W},
         "expect": "reject", "must_mention": ["grain"]},
    ]
    from .domains import FINANCIAL
    fin = dict(grain="one row", primary_key=["record_id"], date_column="transaction_date",
               measures=list(FINANCIAL.measures), dimensions=list(FINANCIAL.dimensions),
               aggregations=dict(FINANCIAL.measures),
               measure_definitions={m: m for m in FINANCIAL.measures},
               analysis_window_start="2023-01-01", analysis_window_end="2024-12-31")
    # The file carries exact duplicate rows by construction: they are dropped (C001) before the
    # contract, or the key is rightly refused and every later call meets the gate instead of the
    # fault it was built to probe (Step 13 run 2's first G).
    steps += [{"id": "adv_plan", "tool": "propose_cleaning_plan", "stage": "setup",
               "kwargs": {"dataset_name": name, "workspace_id": W}},
              {"id": "adv_dedupe", "tool": "apply_cleaning_plan", "stage": "setup",
               "kwargs": {"dataset_name": name, "approved_action_ids": ["C001"],
                          "workspace_id": W}}]
    steps += contract_steps("adv", name, W, **fin)
    steps += [{"id": "st_repeat_confirm", "tool": "confirm_dataset_contract",
               "kwargs": {"contract_json": "$json:adv_cprop", "workspace_id": W},
               "expect": "either", "must_mention": []}]

    def ca(sid, expect, mention, **kw):
        steps.append({"id": sid, "tool": "compute_analysis", "expect": expect,
                      "must_mention": mention,
                      "kwargs": {"dataset_name": name, "workspace_id": W, **kw},
                      "analysis": kw.get("analysis_type")})

    ca("nonexistent_dataset", "reject", ["nope"], analysis_type="summary_stats",
       dataset_name="nope")
    ca("nonexistent_column", "reject", ["zzz"], analysis_type="top_n", dimension="zzz",
       measure="revenue")
    ca("misspelled_column", "reject", ["revnue"], analysis_type="top_n",
       dimension="payment_method", measure="revnue")
    ca("wrong_type_measure", "reject", ["category"], analysis_type="distribution",
       measure="category")
    ca("empty_parameter", "reject", ["dimension"], analysis_type="top_n", dimension="",
       measure="revenue")
    ca("null_parameter", "reject", ["dimension"], analysis_type="top_n", measure="revenue")
    ca("incorrect_enum", "reject", ["fortnight"], analysis_type="trend", measure="revenue",
       grain="fortnight")
    ca("invalid_date_range", "reject", ["2024-13-01"], analysis_type="ranking_shift",
       dimension="payment_method", measure="revenue", before_start="2024-13-01",
       before_end="2024-06-30", after_start="2024-07-01", after_end="2024-12-31")
    ca("start_after_end", "reject", ["2024-06-30"], analysis_type="ranking_shift",
       dimension="payment_method", measure="revenue", before_start="2024-06-30",
       before_end="2024-01-01", after_start="2024-07-01", after_end="2024-12-31")
    ca("n_zero", "reject", ["n"], analysis_type="top_n", dimension="payment_method",
       measure="revenue", n=0)
    ca("n_negative", "reject", ["n"], analysis_type="top_n", dimension="payment_method",
       measure="revenue", n=-1)
    ca("n_huge", "either", [], analysis_type="top_n", dimension="payment_method",
       measure="revenue", n=10 ** 9)
    ca("groupby_id", "reject", ["record_id"], analysis_type="group_compare",
       dimension="record_id", measure="revenue")
    ca("corr_same_column", "reject", ["revenue"], analysis_type="correlation",
       measure="revenue", against="revenue")
    ca("invalid_stat_group", "reject", ["category"], analysis_type="hypothesis_test",
       dimension="category", measure="category")
    ca("bins_zero", "reject", ["bins"], analysis_type="distribution", measure="revenue",
       bins=0)
    ca("confidence_out_of_range", "reject", ["confidence"], analysis_type="confidence_interval",
       measure="revenue", confidence=1.5)
    ca("unknown_method", "reject", ["method"], analysis_type="hypothesis_test",
       dimension="fraud_flag", measure="revenue", method="bayes")
    ca("unknown_analysis", "reject", ["regression"], analysis_type="regression")
    ca("period_outside_window", "reject", ["2026-01"], analysis_type="period_compare",
       measure="revenue", period="2026-01", baseline="2025-12")
    steps += [
        {"id": "chart_without_column", "tool": "render_chart", "expect": "reject",
         "must_mention": ["measure"],
         "kwargs": {"dataset_name": name, "analysis_type": "trend", "chart": "line",
                    "workspace_id": W}},
        {"id": "chart_unsupported", "tool": "render_chart", "expect": "reject",
         "must_mention": ["pie"],
         "kwargs": {"dataset_name": name, "analysis_type": "trend", "chart": "pie",
                    "measure": "revenue", "workspace_id": W}},
        {"id": "result_nonexistent", "tool": "read_result_file", "expect": "reject",
         "must_mention": [],
         "kwargs": {"path": "/home/user/analytics-agent/workspace/adv_bench/results/none.csv",
                    "workspace_id": W}},
        {"id": "result_invalid_path", "tool": "read_result_file", "expect": "reject",
         "must_mention": [], "kwargs": {"path": "../../etc/passwd", "workspace_id": W}},
        {"id": "profile_nonexistent_column", "tool": "profile_column", "expect": "reject",
         "must_mention": ["nope"],
         "kwargs": {"dataset_name": name, "column": "nope", "workspace_id": W}},
        {"id": "apply_unknown_id", "tool": "apply_cleaning_plan", "expect": "reject",
         "must_mention": ["C999"],
         "kwargs": {"dataset_name": name, "approved_action_ids": ["C999"], "workspace_id": W}},
        {"id": "validate_nonexistent", "tool": "validate_dataset", "expect": "reject",
         "must_mention": ["nope"], "kwargs": {"dataset_name": "nope", "workspace_id": W}},
    ]
    # count_distinct over a text column, fed to every analysis that computes on a measure --
    # the class of the BinderException found by the aggregation probe while designing (Step 13)
    steps += ingest_steps("cd", str(clean_csv), "adv_cd", W)
    steps += [{"id": "cd_plan", "tool": "propose_cleaning_plan", "stage": "setup",
               "kwargs": {"dataset_name": "adv_cd", "workspace_id": W}},
              {"id": "cd_dedupe", "tool": "apply_cleaning_plan", "stage": "setup",
               "kwargs": {"dataset_name": "adv_cd", "approved_action_ids": ["C001"],
                          "workspace_id": W}}]
    steps += contract_steps("cd", "adv_cd", W, **dict(
        fin, measures=["revenue", "customer_id"],
        aggregations={"revenue": "sum", "customer_id": "count_distinct"},
        measure_definitions={"revenue": "revenue", "customer_id": "distinct customers"}))
    for analysis, kw in (("mix_shift", {"dimension": "payment_method", "period": "2024-12",
                                        "baseline": "2024-11"}),
                         ("trend", {"grain": "month"}), ("top_n", {"dimension": "payment_method"}),
                         ("group_compare", {"dimension": "payment_method"}),
                         ("seasonality", {}), ("changepoint", {}), ("period_compare",
                                                                     {"period": "2024-12",
                                                                      "baseline": "2024-11"}),
                         ("outlier_detection", {}), ("distribution", {}),
                         ("correlation", {"against": "revenue"}), ("bivariate",
                                                                   {"against": "revenue"}),
                         ("driver_analysis", {}), ("confidence_interval", {}),
                         ("hypothesis_test", {"dimension": "fraud_flag"}),
                         ("ranking_shift", {"dimension": "payment_method",
                                            "before_start": "2023-01-01",
                                            "before_end": "2023-12-31",
                                            "after_start": "2024-01-01",
                                            "after_end": "2024-12-31"})):
        steps.append({"id": f"cd_{analysis}", "tool": "compute_analysis", "analysis": analysis,
                      "expect": "either", "must_mention": [],
                      "kwargs": {"dataset_name": "adv_cd", "analysis_type": analysis,
                                 "measure": "customer_id", "workspace_id": W, **kw}})
    # tiny and degenerate datasets
    for tag, path in (("one_row", one), ("two_rows", two), ("all_null_col", allnull),
                      ("inf_nan", infs), ("dup_ids", dupid)):
        steps += ingest_steps(f"t_{tag}", str(path), tag, W)
        contract = dict(tc, measures=["v"] + (["w"] if tag == "all_null_col" else []),
                        aggregations={"v": "sum", **({"w": "sum"} if tag == "all_null_col"
                                                     else {})},
                        measure_definitions={"v": "v", **({"w": "w"} if tag == "all_null_col"
                                                          else {})})
        steps.append({"id": f"t_{tag}_cprop", "tool": "propose_dataset_contract",
                      "expect": "reject" if tag == "dup_ids" else "either",
                      "must_mention": ["id"] if tag == "dup_ids" else [],
                      "kwargs": {"dataset_name": tag, "workspace_id": W, **contract}})
        steps.append({"id": f"t_{tag}_cconf", "tool": "confirm_dataset_contract",
                      "kwargs": {"contract_json": f"$json:t_{tag}_cprop", "workspace_id": W},
                      "expect": "either", "must_mention": []})
        # dup_ids: its key is rightly refused, so every analysis after it meets the gate
        gated = tag == "dup_ids"
        for sid, analysis, kw, expect, mention in (
                ("summary", "summary_stats", {}, "reject" if gated else "accept",
                 ["contract"] if gated else []),
                ("t", "hypothesis_test", {"dimension": "g", "measure": "v"}, "reject", ["g"]),
                ("ci", "confidence_interval", {"measure": "v"},
                 "reject" if tag in ("one_row", "all_null_col") else "either", ["v"]),
                ("outliers", "outlier_detection", {"measure": "v"}, "either", []),
                ("trend", "trend", {"measure": "v", "grain": "month"}, "either", []),
                ("corr", "correlation", {"measure": "v", "against": "w" if tag ==
                                         "all_null_col" else "v"}, "reject", ["v"])):
            if gated:
                expect, mention = "reject", ["contract"]
            steps.append({"id": f"t_{tag}_{sid}", "tool": "compute_analysis",
                          "analysis": analysis, "expect": expect, "must_mention": mention,
                          "kwargs": {"dataset_name": tag, "analysis_type": analysis,
                                     "workspace_id": W, **kw}})
    steps += ingest_steps("t_nulls", str(nulls_everywhere), "all_null_dataset", W)
    steps.append({"id": "t_nulls_profile", "tool": "profile_dataset", "expect": "either",
                  "must_mention": [], "kwargs": {"dataset_name": "all_null_dataset",
                                                 "workspace_id": W}})
    steps.append({"id": "t_nulls_plan", "tool": "propose_cleaning_plan", "expect": "either",
                  "must_mention": [], "kwargs": {"dataset_name": "all_null_dataset",
                                                 "workspace_id": W}})
    for s in steps:
        s.setdefault("stage", "wrong_calls")
        s.setdefault("family", "wrong_call")
        s.setdefault("case", s["id"])
    return {"name": "wrong_calls", "steps": steps, "ws": ws}
