"""Generate every test fixture the build needs.

Phase 2, Step 3; extended in Phase 3, Step 5. Run from the repo root:

    uv run python tests/fixtures/make_fixtures.py
    uv run python tests/fixtures/make_fixtures.py --big        # adds big_synthetic.csv
    uv run python tests/fixtures/make_fixtures.py --big --big-gb 0.5

Deterministic: a fixed seed means the same bytes every run, so a row count that
changes means the generator changed, not the dice. Every file is rewritten from
scratch; nothing is appended.

The fixtures, and what each one is for:

  clean_sales.csv          Phase 2 Step 5. The easy case, one header row.
  multiheader.csv          Phase 2 Step 5. TWO header rows. The 4.2.1 regression:
                           numeric columns must land as BIGINT/DOUBLE, not
                           VARCHAR. If a header row is read as data, DuckDB sees
                           text in column one and types the whole column VARCHAR.
  clean_sales.xlsx         Phase 2 Step 6. Excel streaming path, well-formed.
  messy_headers.xlsx       Phase 3. Title, blank rows, headers at row 5, trailing
                           notes below the data. Nothing about it is at row 1.
  merged_multiheader.xlsx  Phase 3. Merged cells spanning columns. openpyxl gives
                           the value in the top-left cell and None everywhere
                           else, which is what bounded fill has to repair.
  mixed_types.xlsx         Phase 3 Step 5. Type coercion, F9. Bad values sit
                           BELOW row 5000 on purpose, so the default
                           inference_rows=5000 never sees them: the sniffer
                           calls the column BIGINT and then meets text. That is
                           the only way a coercion failure can happen at all,
                           and it is what on_error='null' counts.
  gaps_and_dupes.csv       Phase 3 Step 5. A header row with blanks and repeats,
                           so blank-column naming and duplicate suffixing have
                           something real to work on. Also the only CSV
                           carrying a literal null sentinel: every other one
                           writes an empty field, which DuckDB nulls without
                           na_values doing anything, so the CSV side of that
                           argument had never met a fixture.
  big_synthetic.csv        Phase 3 preview performance. Gitignored. Opt-in.

A note on dates. _sales_rows writes them with .isoformat(), so they are STRINGS
in every fixture. DuckDB parses those to DATE; the Excel loader sees a str and
leaves it VARCHAR, because _duck_type only maps a real datetime to TIMESTAMP.
Neither loader is wrong -- they disagree about whether a date-shaped string is a
date. mixed_types.xlsx deliberately writes real datetime objects instead, so
there is at least one fixture where the Excel path produces TIMESTAMP.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import random
import sys
from datetime import date, timedelta
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
SEED = 20260829

REGIONS = ["North", "South", "East", "West"]
PRODUCTS = ["Widget", "Gadget", "Sprocket", "Cog", "Flange"]
CHANNELS = ["Online", "Retail", "Wholesale"]


def _rng() -> random.Random:
    return random.Random(SEED)


def _sales_rows(n: int, rng: random.Random) -> list[list]:
    """Shared row generator. Deliberately includes empty strings in one column
    so the null-token handling has something to bite on."""
    start = date(2024, 1, 1)
    rows = []
    for i in range(n):
        units = rng.randint(1, 40)
        price = round(rng.uniform(5.0, 250.0), 2)
        # ~4% blank region, so DEFAULT_NA_VALUES has something to catch
        region = "" if rng.random() < 0.04 else rng.choice(REGIONS)
        rows.append([
            f"ORD-{i + 1:05d}",
            (start + timedelta(days=rng.randint(0, 364))).isoformat(),
            region,
            rng.choice(PRODUCTS),
            rng.choice(CHANNELS),
            units,
            price,
            round(units * price, 2),
        ])
    return rows


def make_clean_sales_csv(path: Path, n: int = 500) -> None:
    rng = _rng()
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "order_date", "region", "product",
                    "channel", "units", "unit_price", "revenue"])
        w.writerows(_sales_rows(n, rng))


def make_multiheader_csv(path: Path, n: int = 300) -> None:
    """Two header rows. Row 1 groups, row 2 field names, data from row 3.

    Read naively, DuckDB treats row 2 as data, sees 'order_id' in a column that
    is otherwise integers, and types the column VARCHAR. That is failure F5 and
    the reason this fixture exists.
    """
    rng = _rng()
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Identifiers", "", "Dimensions", "", "",
                    "Measures", "", ""])
        w.writerow(["order_id", "order_date", "region", "product",
                    "channel", "units", "unit_price", "revenue"])
        w.writerows(_sales_rows(n, rng))


def make_clean_sales_xlsx(path: Path, n: int = 400) -> None:
    from openpyxl import Workbook

    rng = _rng()
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["order_id", "order_date", "region", "product",
               "channel", "units", "unit_price", "revenue"])
    for row in _sales_rows(n, rng):
        ws.append(row)
    wb.save(path)


def make_messy_headers_xlsx(path: Path, n: int = 200) -> None:
    """Everything an export from a reporting tool does wrong.

    Row 1 title, row 2 subtitle, row 3 blank, row 4 blank, row 5 the real
    headers, data from row 6, then a blank and two notes rows at the bottom.
    A loader that assumes row 1 is the header gets a single column called
    'Quarterly Sales Report'.

    The three rows at the bottom are why footer_skip_rows exists. A header
    guesser can find where the data starts from a type change; nothing about
    the top of a file says where it ends.
    """
    from openpyxl import Workbook

    rng = _rng()
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append(["Quarterly Sales Report"])
    ws.append(["Generated 2026-08-29 - Internal use only"])
    ws.append([])
    ws.append([])
    ws.append(["order_id", "order_date", "region", "product",
               "channel", "units", "unit_price", "revenue"])
    for row in _sales_rows(n, rng):
        ws.append(row)
    ws.append([])
    ws.append(["Notes: figures exclude cancelled orders."])
    ws.append(["Source: internal ERP extract."])
    wb.save(path)


def make_merged_multiheader_xlsx(path: Path, n: int = 150) -> None:
    """Merged cells spanning columns.

    Row 1 has 'Identifiers' merged across A:B, 'Dimensions' across C:E,
    'Measures' across F:H. Row 2 has the real field names.

    openpyxl reports a merged range as the value in the top-left cell and None
    in every other cell of the range. Bounded fill must carry the value right
    across the range and STOP at its edge -- filling to the end of the row is
    the bug this fixture is here to catch.
    """
    from openpyxl import Workbook

    rng = _rng()
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"

    ws["A1"] = "Identifiers"
    ws["C1"] = "Dimensions"
    ws["F1"] = "Measures"
    ws.merge_cells("A1:B1")
    ws.merge_cells("C1:E1")
    ws.merge_cells("F1:H1")

    ws.append([])  # placeholder, overwritten below
    for col, name in enumerate(
        ["order_id", "order_date", "region", "product",
         "channel", "units", "unit_price", "revenue"], start=1
    ):
        ws.cell(row=2, column=col, value=name)

    for row in _sales_rows(n, rng):
        ws.append(row)
    wb.save(path)


# Rows carrying a value that will not fit its column. All are past 5000, so
# the default inference_rows=5000 has already decided the type before it meets
# them. Fixed positions, so the counts are assertable.
_BAD_UNITS_ROWS = (5100, 5200, 5300, 5400, 5500, 5600, 5700)
_BAD_PRICE_ROWS = (5150, 5450, 5750)


def make_mixed_types_xlsx(path: Path, n: int = 6000) -> None:
    """Type coercion, F9.

    Six thousand rows so that the bad values can sit BELOW the default
    inference_rows=5000. That placement is the whole point. Put them in the
    first 5000 and the sniffer sees text in the column, widens it to VARCHAR,
    and nothing ever fails to convert -- so nothing is counted, and the fixture
    proves nothing.

    Expected with defaults:

        load_excel(...)                     -> LoadRefused naming row 5101
        load_excel(..., on_error='null')    -> {'units': 7, 'unit_price': 3}
        load_excel(..., all_text=True)      -> every column VARCHAR, no failures

    order_date holds real datetime objects here, not isoformat strings, so this
    is the one fixture where the Excel path produces TIMESTAMP. region carries
    'N/A' in about 5% of rows for na_values to catch; those are declared nulls,
    NOT coercion failures, and must never appear in the counts.
    """
    from openpyxl import Workbook

    rng = _rng()
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["order_id", "order_date", "region", "units", "unit_price",
               "is_return"])

    start = _dt.datetime(2024, 1, 1)
    bad_units = set(_BAD_UNITS_ROWS)
    bad_price = set(_BAD_PRICE_ROWS)

    for i in range(1, n + 1):
        units = rng.randint(1, 40)
        price = round(rng.uniform(5.0, 250.0), 2)
        region = "N/A" if rng.random() < 0.05 else rng.choice(REGIONS)
        ws.append([
            f"ORD-{i:05d}",
            start + timedelta(days=rng.randint(0, 364)),
            region,
            "n/a" if i in bad_units else units,
            "not priced" if i in bad_price else price,
            rng.random() < 0.1,
        ])
    wb.save(path)


def make_gaps_and_dupes_csv(path: Path, n: int = 200) -> None:
    """A header row with blanks and repeats.

    Header:  order_id, units, '', units, '', revenue

    Two columns share the name 'units' and two have no name at all. Both have
    to be resolved before the names reach DuckDB, which will not hold two
    columns of the same name and silently invents 'columnN' for a short list.

    Expected from headers.assemble_names:

        ['order_id', 'units', 'column_3', 'units_2', 'column_5', 'revenue']

    with both decisions reported: the blanks named by position, the duplicate
    suffixed. Neither is allowed to happen quietly.

    Column 3 also carries a literal 'N/A' in about 8% of rows -- a token, not
    an empty field. An empty field is already NULL to DuckDB whatever
    na_values says, so a sentinel is the only thing that argument can actually
    catch, and until this fixture had one no CSV test exercised it.
    """
    rng = _rng()
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "units", "", "units", "", "revenue"])
        for i in range(n):
            units_a = rng.randint(1, 40)
            units_b = rng.randint(1, 40)
            price = round(rng.uniform(5.0, 250.0), 2)
            region = "N/A" if rng.random() < 0.08 else rng.choice(REGIONS)
            w.writerow([
                f"ORD-{i + 1:05d}",
                units_a,
                region,
                units_b,
                rng.choice(CHANNELS),
                round((units_a + units_b) * price, 2),
            ])


def make_big_synthetic_csv(path: Path, target_gb: float = 1.5) -> int:
    """A large CSV for preview-performance testing. Gitignored.

    Written in chunks so peak memory stays flat regardless of target size --
    the same discipline the loaders use. Returns the row count.
    """
    rng = _rng()
    target_bytes = int(target_gb * 1024 ** 3)
    written = 0
    rows = 0
    chunk: list[str] = []

    with path.open("w", newline="", encoding="utf-8") as f:
        header = "row_id,event_date,region,product,channel,units,unit_price,revenue\n"
        f.write(header)
        written += len(header)
        start = date(2020, 1, 1)

        while written < target_bytes:
            for _ in range(50_000):
                rows += 1
                units = rng.randint(1, 40)
                price = round(rng.uniform(5.0, 250.0), 2)
                d = (start + timedelta(days=rng.randint(0, 2000))).isoformat()
                chunk.append(
                    f"{rows},{d},{rng.choice(REGIONS)},{rng.choice(PRODUCTS)},"
                    f"{rng.choice(CHANNELS)},{units},{price},"
                    f"{round(units * price, 2)}\n"
                )
            blob = "".join(chunk)
            f.write(blob)
            written += len(blob)
            chunk.clear()
            pct = min(100, int(100 * written / target_bytes))
            print(f"\r  big_synthetic.csv {pct:3d}%  "
                  f"({written / 1024**3:.2f} GB, {rows:,} rows)",
                  end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# Phase 7, Step 3: a fixture that is broken on purpose
# ---------------------------------------------------------------------------
# The Phase 7 Done-When is "a correct failure on a deliberately broken
# fixture", and nothing in this directory was broken in validation's terms:
# gaps_and_dupes.csv writes 200 unique ORD- ids and has no date column at all,
# its name meaning duplicate and blank COLUMN NAMES.
#
# Four rules this fixture is built to:
#
#   ONE FAULT PER ROW. Every family below is disjoint, so the totals add and a
#   checker that reports a different total is wrong rather than differently
#   right. A row that was both null-keyed and out of window would let a rule
#   be right by accident.
#
#   A DIFFERENT COUNT PER FAMILY. No two families share a number, so a rule
#   that reads the wrong column reports a number that belongs to something
#   else and is caught by inspection.
#
#   THE COUNTS ARE DECLARED, NOT DISCOVERED. BROKEN_BREAKS is the whole
#   specification; tests/test_broken_sales_ground_truth.py writes the same
#   numbers out again as literals rather than importing them, because a test
#   that reads its expectations from the generator only ever asserts that the
#   generator agrees with itself.
#
#   TIMESTAMPS, NOT DATES. P7-D2's trap -- `<= DATE 'end'` casting to midnight
#   and dropping the rest of that day -- cannot exist on a DATE column. The
#   date_last_second row is inside the window if the bound is written
#   `< end + INTERVAL 1 DAY` and outside it if it is not, which is the only
#   thing that turns that decision into something a test can fail on.

BROKEN_BASE_ROWS = 120
BROKEN_WINDOW = (date(2024, 1, 1), date(2024, 12, 31))

BROKEN_BREAKS = {
    "duplicate_key": 3,      # same order_id, everything else different
    "null_key": 2,           # no order_id at all
    "date_before": 4,        # 2023
    "date_after": 5,         # 2025
    "date_null": 6,          # no timestamp
    "date_last_second": 1,   # 2024-12-31 23:59:59 -- INSIDE, and the P7-D2 canary
    "region_orphan": 7,      # a region the lookup does not have
    "region_null": 8,        # no region
    "channel_unknown": 9,    # outside the declared vocabulary
    "units_negative": 10,    # a count that cannot be negative
    "price_null": 11,        # no price, and therefore no revenue
}

# Three misspellings across seven rows, so "7 rows" and "3 values" are
# separately checkable -- a report that prints one as the other is the P7-D6
# failure in a new place.
UNKNOWN_REGIONS = ["Nord", "Souh", "Nrth-West"]
UNKNOWN_CHANNELS = ["Partner", "Reseller"]

# Central is real and unused; the blank row is the trailing empty line every
# spreadsheet export eventually contributes to a dimension table. It is there
# on purpose: one NULL in the parent column is what makes `NOT IN` return
# UNKNOWN for every comparison and report a clean pass over seven orphans.
LOOKUP_REGIONS = REGIONS + ["Central"]

BROKEN_HEADER = ["order_id", "order_ts", "region", "product", "channel",
                 "units", "unit_price", "revenue"]


def _broken_ts(rng, start=date(2024, 1, 1), days=365) -> str:
    """A timestamp inside a span, written with a time so the column is not a
    DATE."""
    d = start + timedelta(days=rng.randrange(days))
    return (f"{d.isoformat()} {rng.randrange(8, 20):02d}:"
            f"{rng.randrange(60):02d}:{rng.randrange(60):02d}")


def _broken_row(rng, order_id, ts, region=None, channel=None, units=None,
                price=None) -> list:
    """One row, with every field defaulting to something valid.

    Each caller overrides exactly one thing, which is how the one-fault-per-row
    rule is enforced by construction rather than by review.
    """
    region = rng.choice(REGIONS) if region is None else region
    channel = rng.choice(CHANNELS) if channel is None else channel
    units = rng.randint(1, 40) if units is None else units
    price = round(rng.uniform(5.0, 250.0), 2) if price is None else price
    revenue = "" if price == "" else round(units * float(price), 2)
    return [order_id, ts, region, rng.choice(PRODUCTS), channel,
            units, price, revenue]


def make_region_lookup_csv(path: Path) -> None:
    """The parent side of the referential-integrity check.

    Referential integrity needs two datasets; a column and a hope is what the
    check would otherwise be run against.
    """
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["region", "region_name"])
        for r in LOOKUP_REGIONS:
            w.writerow([r, f"{r} sales territory"])
        w.writerow(["", "(unassigned)"])


def make_broken_sales_csv(path: Path) -> None:
    """120 clean rows and 66 injected faults. See BROKEN_BREAKS."""
    rng = _rng()
    rows = [
        _broken_row(rng, f"ORD-{i + 1:05d}", _broken_ts(rng))
        for i in range(BROKEN_BASE_ROWS)
    ]

    next_id = BROKEN_BASE_ROWS + 1

    def fresh() -> str:
        nonlocal next_id
        order_id = f"ORD-{next_id:05d}"
        next_id += 1
        return order_id

    # A repeated key, not a repeated row: everything except order_id differs,
    # so `DISTINCT *` cannot find these and only a grain check can.
    for i in range(BROKEN_BREAKS["duplicate_key"]):
        rows.append(_broken_row(rng, f"ORD-{i + 11:05d}", _broken_ts(rng)))
    for _ in range(BROKEN_BREAKS["null_key"]):
        rows.append(_broken_row(rng, "", _broken_ts(rng)))
    for _ in range(BROKEN_BREAKS["date_before"]):
        rows.append(_broken_row(rng, fresh(),
                                _broken_ts(rng, date(2023, 1, 1), 365)))
    for _ in range(BROKEN_BREAKS["date_after"]):
        rows.append(_broken_row(rng, fresh(),
                                _broken_ts(rng, date(2025, 1, 1), 200)))
    for _ in range(BROKEN_BREAKS["date_null"]):
        rows.append(_broken_row(rng, fresh(), ""))
    for _ in range(BROKEN_BREAKS["date_last_second"]):
        rows.append(_broken_row(rng, fresh(), "2024-12-31 23:59:59"))
    for i in range(BROKEN_BREAKS["region_orphan"]):
        rows.append(_broken_row(rng, fresh(), _broken_ts(rng),
                                region=UNKNOWN_REGIONS[i % len(UNKNOWN_REGIONS)]))
    for _ in range(BROKEN_BREAKS["region_null"]):
        rows.append(_broken_row(rng, fresh(), _broken_ts(rng), region=""))
    for i in range(BROKEN_BREAKS["channel_unknown"]):
        rows.append(_broken_row(rng, fresh(), _broken_ts(rng),
                                channel=UNKNOWN_CHANNELS[i % len(UNKNOWN_CHANNELS)]))
    for i in range(BROKEN_BREAKS["units_negative"]):
        rows.append(_broken_row(rng, fresh(), _broken_ts(rng), units=-(i + 1)))
    for _ in range(BROKEN_BREAKS["price_null"]):
        rows.append(_broken_row(rng, fresh(), _broken_ts(rng), price=""))

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(BROKEN_HEADER)
        w.writerows(rows)


SMALL_FIXTURES = [
    ("clean_sales.csv", make_clean_sales_csv),
    ("multiheader.csv", make_multiheader_csv),
    ("clean_sales.xlsx", make_clean_sales_xlsx),
    ("messy_headers.xlsx", make_messy_headers_xlsx),
    ("merged_multiheader.xlsx", make_merged_multiheader_xlsx),
    ("mixed_types.xlsx", make_mixed_types_xlsx),
    ("gaps_and_dupes.csv", make_gaps_and_dupes_csv),
    ("broken_sales.csv", make_broken_sales_csv),
    ("region_lookup.csv", make_region_lookup_csv),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate build test fixtures.")
    ap.add_argument("--big", action="store_true",
                    help="also generate big_synthetic.csv (large, gitignored)")
    ap.add_argument("--big-gb", type=float, default=1.5,
                    help="target size in GB for big_synthetic.csv (default 1.5)")
    args = ap.parse_args()

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Writing fixtures to {FIXTURE_DIR}")

    for name, fn in SMALL_FIXTURES:
        path = FIXTURE_DIR / name
        fn(path)
        print(f"  {name:<26} {path.stat().st_size:>10,} bytes")

    if args.big:
        import shutil
        need = int(args.big_gb * 1024 ** 3 * 1.2)
        free = shutil.disk_usage(FIXTURE_DIR).free
        if free < need:
            print(f"\nREFUSED: big_synthetic.csv needs about "
                  f"{need / 1024**3:.1f} GB free, but only "
                  f"{free / 1024**3:.1f} GB is available.\n"
                  f"NEXT STEP: free space, or use a smaller target such as "
                  f"--big-gb 0.5", file=sys.stderr)
            return 1
        path = FIXTURE_DIR / "big_synthetic.csv"
        rows = make_big_synthetic_csv(path, args.big_gb)
        print(f"  {'big_synthetic.csv':<26} {path.stat().st_size:>10,} bytes "
              f"({rows:,} rows)")
    else:
        print("\n  big_synthetic.csv skipped. Add --big to generate it.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
