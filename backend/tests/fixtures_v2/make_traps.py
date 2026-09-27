"""Synthetic marketing files with planted traps (B3). Deterministic: seed 20260927.

ads_traps.csv      per-row ctr (never summed), a test campaign, spend rows with 0 impressions,
                   a tiny city (2 rows), GST-inclusive order revenue + gst, a Diwali spike
                   (2025-10-20..21), and a window spanning Meta's 2026-01-12 window removal.
conv_multi.csv     the same conversions reported by Google, Meta and GA4 (never added).
utm.csv            source case variants, (not set), missing mediums.
Run: uv run python backend/tests/fixtures_v2/make_traps.py
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
rng = random.Random(20260927)


def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def ads():
    rows = []
    camps = [("Brand_Search", "google"), ("Generic_Sushi", "google"), ("Retarget", "meta"),
             ("TEST_campaign_dummy", "google")]
    cities = ["Pune", "Mumbai", "Bengaluru"]
    for d in days(date(2025, 10, 1), date(2026, 1, 31)):
        festive = date(2025, 10, 20) <= d <= date(2025, 10, 21)
        for camp, ch in camps:
            city = cities[(d.toordinal() + len(camp)) % 3]
            imp = rng.randint(1000, 4000)
            clk = max(1, imp * rng.randint(2, 8) // 100)
            cost = round(clk * rng.uniform(10, 20), 2)
            conv = rng.randint(1, max(2, clk // 10)) * (3 if festive else 1)
            gross = round(conv * rng.uniform(600, 900), 2)
            gst = round(gross * 18 / 118, 2)
            rows.append([d.isoformat(), camp, ch, "mobile" if rng.random() < .7 else "desktop",
                         city, imp, clk, round(100 * clk / imp, 2), cost, conv,
                         round(gross * 1.1, 2), gross, gst])
    # spend on rows with 0 impressions (billing adjustments): flagged, never silently dropped
    for d in (date(2025, 11, 3), date(2025, 12, 8)):
        rows.append([d.isoformat(), "Generic_Sushi", "google", "desktop", "Pune", 0, 0, 0.0,
                     250.0, 0, 0.0, 0.0, 0.0])
    # a tiny city: 2 rows, under min_group_size 5
    for d in (date(2025, 11, 10), date(2025, 11, 11)):
        rows.append([d.isoformat(), "Brand_Search", "google", "mobile", "Nashik", 500, 40, 8.0,
                     400.0, 9, 9000.0, 8000.0, 1220.34])
    head = ["date", "campaign", "channel", "device", "city", "impressions", "clicks", "ctr",
            "cost", "conversions", "conv_value", "order_revenue", "gst"]
    rows = [[i + 1, *r] for i, r in enumerate(rows)]
    write("ads_traps.csv", ["row_id", *head], rows)


def conv_multi():
    rows = []
    for d in days(date(2026, 2, 1), date(2026, 2, 28)):
        for camp in ("Brand", "Generic"):
            sales = rng.randint(5, 20)
            rows.append([d.isoformat(), camp, round(rng.uniform(2000, 5000), 2),
                         sales + rng.randint(0, 3), sales + rng.randint(0, 4),
                         sales - rng.randint(0, 2)])
    write("conv_multi.csv", ["date", "campaign", "cost", "google_conversions",
                             "meta_conversions", "ga4_key_events"], rows)


def utm():
    srcs = ["google", "Google", "GOOGLE", "facebook", "Facebook", "(not set)", "newsletter"]
    meds = {"google": "cpc", "Google": "cpc", "GOOGLE": "", "facebook": "paid_social",
            "Facebook": "paid_social", "(not set)": "(not set)", "newsletter": "email"}
    rows = []
    for d in days(date(2026, 3, 1), date(2026, 3, 14)):
        for s in srcs:
            rows.append([d.isoformat(), s, meds[s], rng.randint(10, 400)])
    write("utm.csv", ["date", "session_source", "session_medium", "sessions"], rows)


def write(name, head, rows):
    with open(HERE / name, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)
    print(name, len(rows), "rows")


if __name__ == "__main__":
    ads()
    conv_multi()
    utm()
