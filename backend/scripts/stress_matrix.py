"""A stress matrix: every tool, on many shapes of data, every outcome written down (Phase 14 Step 6).

    uv run python scripts/stress_matrix.py              # all datasets
    uv run python scripts/stress_matrix.py tab latin1   # only datasets whose name contains these

Not a pytest file, for the eval harness's reason (P13-D1): it measures and reports, and exits zero
whatever it finds. Each dataset is generated here, seeded, with its ground truth computed by the
generator in plain Python -- never by the engine under test. Each goes through the path a browser
visitor drives (RealBackend) and the tools the assistant calls (server.py):

    upload, draft, load, describe, profile, clean (propose, apply suggested), contract (draft,
    answer from suggestions, confirm), validate, 27 analyses, 3 charts, report

Outcomes: CRASH (an exception escaped), WRONG (a ground-truth check failed), SUSPECT (nan/inf as
a figure, or slower than SLOW_SECONDS), REFUSED (judged by hand: many are right), OK. Writes
docs/stress/stress_matrix.json and prints a summary.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import random
import re
import sys
import time
import traceback
from decimal import Decimal
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
os.environ["ANALYTICS_WORKSPACE_TTL_HOURS"] = "0"  # this script never sweeps anyone's workspace

import openpyxl  # noqa: E402

from backend.engine import server, workspace  # noqa: E402
from backend.engine.analysis import registry  # noqa: E402
from backend.engine.contract.refusals import reason_of  # noqa: E402
from backend.engine.util import db  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

OUT = ROOT / "docs" / "stress"
SLOW_SECONDS = 20.0
OUTCOMES = ("CRASH", "WRONG", "SUSPECT", "REFUSED", "OK")

# --- generated data ----------------------------------------------------------------------------

REGIONS = ["North", "South", "East", "West"]
PRODUCTS = ["Widget", "Cog", "Flange", "Gear"]
CHANNELS = ["Online", "Retail", "Wholesale"]
HEADER = ["order_id", "order_date", "customer_id", "region", "product", "channel", "units",
          "unit_price", "revenue"]


def sales(n: int, seed: int = 7, start=dt.date(2023, 1, 1), days: int = 730,
          skip_months: tuple[int, ...] = ()) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        while True:
            d = start + dt.timedelta(days=rng.randrange(days))
            if d.month not in skip_months:
                break
        units = rng.randint(1, 50)
        price = round(rng.uniform(5, 250), 2)
        rows.append(dict(order_id=f"ORD-{i + 1:05d}", order_date=d,
                         customer_id=f"C{rng.randrange(60):03d}",
                         region=rng.choice(REGIONS), product=rng.choice(PRODUCTS),
                         channel=rng.choice(CHANNELS), units=units, unit_price=price,
                         revenue=round(units * price, 2)))
    return rows


def to_csv(rows: list[dict], header: list[str] | None = None, *, delimiter=",", fmt=None,
           lineterminator="\r\n") -> str:
    header = header or list(rows[0])
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator=lineterminator)
    w.writerow(header)
    for r in rows:
        w.writerow([(fmt(k, r[k]) if fmt else (r[k].isoformat() if isinstance(r[k], dt.date) else r[k]))
                    for k in header])
    return buf.getvalue()


def to_xlsx(sheets: list[tuple[str, list[list]]], merges: dict[str, list[str]] | None = None) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, grid in sheets:
        ws = wb.create_sheet(title)
        for row in grid:
            ws.append(row)
        for rng in (merges or {}).get(title, []):
            ws.merge_cells(rng)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def with_cached_values(xlsx: bytes, column: str, values: dict[int, float]) -> bytes:
    """Give formula cells in `column` a cached <v> value, as Excel itself saves them (openpyxl
    writes formulas without one). values maps sheet row -> the value Excel would have cached."""
    import zipfile
    zin = zipfile.ZipFile(io.BytesIO(xlsx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = re.sub(
                    rf'<c r="{column}(\d+)"([^>]*)><f>([^<]*)</f>(?:<v\s*/>|<v></v>)?</c>',
                    lambda m: (f'<c r="{column}{m.group(1)}"{m.group(2)}><f>{m.group(3)}</f>'
                               f'<v>{values[int(m.group(1))]}</v></c>'),
                    data.decode()).encode()
            zout.writestr(item, data)
    return out.getvalue()


@dataclass
class Truth:
    rows: int | None = None
    cols: int | None = None
    sums: dict[str, float] = field(default_factory=dict)  # source column -> expected sum
    date_col: str | None = None
    date_range: tuple[str, str] | None = None
    text_values: dict[str, list[str]] = field(default_factory=dict)  # column -> values that must survive
    no_nulls: list[str] = field(default_factory=list)  # columns every row of the file fills


@dataclass
class Case:
    name: str
    filename: str
    data: bytes
    truth: Truth
    what: str


def _truth(rows, header=None, sums=("units", "revenue"), date_col="order_date") -> Truth:
    header = header or list(rows[0])
    dates = [r[date_col] for r in rows] if date_col and rows and date_col in rows[0] else []
    return Truth(rows=len(rows), cols=len(header),
                 sums={c: round(sum(r[c] for r in rows), 2) for c in sums if rows and c in rows[0]},
                 date_col=date_col if dates else None,
                 date_range=(min(dates).isoformat()[:10], max(dates).isoformat()[:10])
                 if dates else None)


def cases() -> list[Case]:
    out: list[Case] = []
    add = lambda *a: out.append(Case(*a))  # noqa: E731

    base = sales(500)
    add("baseline_csv", "baseline.csv", to_csv(base).encode(), _truth(base),
        "500 clean sales rows, comma, ISO dates -- the control")

    eu = sales(300, seed=11)
    def eu_fmt(k, v):
        if isinstance(v, dt.date):
            return v.isoformat()
        if isinstance(v, float):
            return f"{v:.2f}".replace(".", ",")
        return v
    add("semicolon_decimal_comma", "eu_sales.csv", to_csv(eu, delimiter=";", fmt=eu_fmt).encode(),
        _truth(eu), "European export: ';' delimiter, ',' decimal mark")

    tab = sales(300, seed=12)
    add("tab_separated", "tab_sales.csv", to_csv(tab, delimiter="\t").encode(), _truth(tab),
        "tab-delimited, saved with a .csv name")

    uni = sales(200, seed=13)
    for r, label in zip(uni, ["Nord-Est é", "北部", "Süd 🚀", "Ouest"] * 50):
        r["region"] = label
    t = _truth(uni)
    t.text_values = {"region": ["北部", "Süd 🚀", "Nord-Est é"]}
    add("utf8_bom_unicode", "unicode.csv", ("\ufeff" + to_csv(uni)).encode("utf-8"), t,
        "UTF-8 with BOM; accented, CJK and emoji values")

    lat = sales(200, seed=14)
    for r, label in zip(lat, ["Sünd", "Nörd", "Éast", "Wèst"] * 50):
        r["region"] = label
    t = _truth(lat)
    t.text_values = {"region": ["Sünd", "Nörd"]}
    add("latin1_encoding", "latin1.csv", to_csv(lat).encode("latin-1"), t,
        "Latin-1 (cp1252-style) bytes, not UTF-8")

    q = sales(150, seed=15)
    for i, r in enumerate(q):
        if i % 5 == 0:
            r["product"] = 'Widget, large\n"v2"'
    add("quoted_multiline", "quoted.csv", to_csv(q).encode(), _truth(q),
        "quoted fields holding commas, quotes and newlines")

    lf = sales(200, seed=16)
    add("lf_line_endings", "unix.csv", to_csv(lf, lineterminator="\n").encode(), _truth(lf),
        "LF line endings (the others are CRLF)")

    add("header_only", "header_only.csv", (",".join(HEADER) + "\n").encode(),
        Truth(rows=0, cols=len(HEADER)), "a header and no data rows")
    add("empty_file", "empty.csv", b"", Truth(), "zero bytes")

    one = sales(1, seed=17)
    add("one_row", "one_row.csv", to_csv(one).encode(), _truth(one), "exactly one data row")

    vals = [round(random.Random(18).uniform(-5, 5) + i * 0.1, 3) for i in range(100)]
    add("one_column", "one_col.csv", ("value\n" + "\n".join(map(str, vals)) + "\n").encode(),
        Truth(rows=100, cols=1, sums={"value": round(sum(vals), 3)}), "a single numeric column")

    rng = random.Random(19)
    wide_h = ["id"] + [f"m{i:03d}" for i in range(200)]
    wide = [[f"R{r}"] + [rng.randint(0, 999) for _ in range(200)] for r in range(300)]
    buf = io.StringIO()
    csv.writer(buf).writerows([wide_h] + wide)
    add("wide_201_columns", "wide.csv", buf.getvalue().encode(),
        Truth(rows=300, cols=201, sums={"m000": sum(r[1] for r in wide),
                                        "m199": sum(r[200] for r in wide)}),
        "300 rows x 201 columns")

    big = sales(200_000, seed=20)
    add("big_200k_rows", "big.csv", to_csv(big).encode(), _truth(big), "200,000 rows (~14 MB)")

    nc = sales(200, seed=21)
    for r in nc:
        r["empty_col"] = ""
        r["constant"] = "X"
    add("null_and_constant_columns", "nullconst.csv", to_csv(nc).encode(),
        _truth(nc), "one column entirely empty, one constant")

    dup_h = ["id", "value", "value", "", "region"]
    dup_rows = [[i, i * 2, i * 3, "z", REGIONS[i % 4]] for i in range(120)]
    buf = io.StringIO()
    csv.writer(buf).writerows([dup_h] + dup_rows)
    add("duplicate_and_blank_headers", "dupheads.csv", buf.getvalue().encode(),
        Truth(rows=120, cols=5), "two columns named 'value' and one with no name")

    hostile_h = ["order", "select", 'unit "price"', "a.b", "revenue ($)", " spaced name ",
                 "group", "Date"]
    hr = random.Random(22)
    hostile = [[f"O{i}", REGIONS[i % 4], round(hr.uniform(1, 90), 2), hr.randint(1, 9),
                round(hr.uniform(10, 900), 2), CHANNELS[i % 3], PRODUCTS[i % 4],
                (dt.date(2024, 1, 1) + dt.timedelta(days=i % 300)).isoformat()] for i in range(240)]
    buf = io.StringIO()
    csv.writer(buf).writerows([hostile_h] + hostile)
    add("sql_hostile_names", "hostile.csv", buf.getvalue().encode(),
        Truth(rows=240, cols=8, sums={"revenue ($)": round(sum(r[4] for r in hostile), 2)}),
        "column names that are SQL words or hold quotes, dots, spaces, symbols")

    mx = sales(200, seed=23)
    bad = {3: "n/a", 17: "twelve", 40: "", 41: "-", 90: "12 units"}
    def mx_fmt(k, v):
        return v.isoformat() if isinstance(v, dt.date) else v
    rows_txt = [dict(r) for r in mx]
    for i, token in bad.items():
        rows_txt[i]["units"] = token
    t = _truth(mx, sums=("revenue",))
    add("mixed_number_text", "mixed.csv", to_csv(rows_txt, fmt=mx_fmt).encode(), t,
        "a numeric column holding 'n/a', 'twelve', '', '-', '12 units'")

    dd = sales(300, seed=24)
    add("dates_dd_mm_yyyy", "ddmm.csv",
        to_csv(dd, fmt=lambda k, v: v.strftime("%d/%m/%Y") if isinstance(v, dt.date) else v).encode(),
        _truth(dd), "dates written 31/12/2024 (day first)")
    us = sales(300, seed=25)
    add("dates_mm_dd_yyyy", "mmdd.csv",
        to_csv(us, fmt=lambda k, v: v.strftime("%m/%d/%Y") if isinstance(v, dt.date) else v).encode(),
        _truth(us), "dates written 12/31/2024 (month first)")
    md = sales(200, seed=26)
    fmts = ["%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y", "%Y%m%d", "%Y-%m-%dT%H:%M:%S"]
    add("dates_mixed_formats", "mixdates.csv",
        to_csv(md, fmt=lambda k, v, c=iter(range(10**9)): (
            v.strftime(fmts[next(c) % len(fmts)]) if isinstance(v, dt.date) else v)).encode(),
        _truth(md), "one date column in five formats")

    amb = sales(200, seed=46, start=dt.date(2024, 1, 1), days=366)
    for r in amb:
        r["order_date"] = r["order_date"].replace(day=min(r["order_date"].day, 12))
    add("dates_ambiguous_dd_mm", "ambiguous.csv",
        to_csv(amb, fmt=lambda k, v: v.strftime("%d/%m/%Y") if isinstance(v, dt.date) else v).encode(),
        _truth(amb), "day-first dates whose day is always <= 12, so month-first also parses")

    tot = sales(150, seed=47)
    body = to_csv(tot)
    total_line = ",".join(["Total", "", "", "", "", "", str(sum(r["units"] for r in tot)), "",
                           f"{sum(r['revenue'] for r in tot):.2f}"])
    add("csv_total_row_at_bottom", "with_total.csv", (body + total_line + "\r\n").encode(),
        _truth(tot), "a spreadsheet-style 'Total' row under the data")

    rep = sales(180, seed=48)
    text = to_csv(rep[:90]) + to_csv(rep[90:])  # two exports pasted together: header twice
    add("repeated_header_mid_file", "pasted.csv", text.encode(), _truth(rep),
        "two exports concatenated, so the header line appears again mid-file")

    bl = sales(200, seed=27)
    for i, r in enumerate(bl):
        r["returned"] = ["yes", "no"][i % 2]
        r["gift"] = ["Y", "N", "Y", "N"][i % 4]
        r["paid"] = ["TRUE", "FALSE"][i % 3 == 0]
        r["flag01"] = i % 2
    add("boolean_spellings", "bools.csv", to_csv(bl).encode(), _truth(bl),
        "yes/no, Y/N, TRUE/FALSE, 1/0 columns")

    cur = sales(250, seed=28)
    def cur_fmt(k, v):
        if isinstance(v, dt.date):
            return v.isoformat()
        if k == "revenue":
            return f"${v:,.2f}"
        if k == "unit_price":
            return f"{v:,.2f}"
        return v
    for i, r in enumerate(cur):
        r["discount"] = f"{(i % 20) * 2.5}%"
    add("currency_percent_thousands", "currency.csv", to_csv(cur, fmt=cur_fmt).encode(),
        _truth(cur), "'$1,234.56', '12.5%', thousands separators")

    sci_vals = ["1e5", "2.5E-3", "NaN", "Infinity", "-inf", "42", "3.14", "-7"] * 25
    add("scientific_nan_inf", "sci.csv",
        ("id,reading\n" + "\n".join(f"{i},{v}" for i, v in enumerate(sci_vals)) + "\n").encode(),
        Truth(rows=200, cols=2), "scientific notation and NaN / Infinity / -inf text")

    zr = random.Random(29)
    zips = [f"{zr.randrange(0, 3000):05d}" for _ in range(200)]
    t = Truth(rows=200, cols=3, sums={"amount": 0})
    amounts = [zr.randint(1, 100) for _ in range(200)]
    t.sums["amount"] = sum(amounts)
    t.text_values = {"zip": [z for z in zips if z.startswith("00")][:3]}
    add("leading_zero_ids", "zips.csv",
        ("zip,region,amount\n" + "\n".join(f"{z},{REGIONS[i % 4]},{a}"
                                            for i, (z, a) in enumerate(zip(zips, amounts)))
         + "\n").encode(), t, "zip codes like 00501 that must stay text")

    mess = sales(240, seed=30)
    tokens = ["N/A", "-", "NULL", "", "none"]
    for i, r in enumerate(mess):
        r["region"] = [f"  {r['region']} ", r["region"].lower(), r["region"].upper(),
                       r["region"]][i % 4]
        if i % 17 == 0:
            r["channel"] = tokens[i % len(tokens)]
    add("messy_text_and_missing_tokens", "messy.csv", to_csv(mess).encode(), _truth(mess),
        "padding, case variants and N/A, -, NULL, none")

    dupe = sales(200, seed=31)
    dupe = dupe + dupe[:20]
    add("exact_duplicate_rows", "dupes.csv", to_csv(dupe).encode(), _truth(dupe),
        "20 rows repeated exactly")

    neg = sales(200, seed=32)
    for i, r in enumerate(neg):
        if i % 7 == 0:
            r["units"], r["revenue"] = -r["units"], -r["revenue"]
        if i % 11 == 0:
            r["units"], r["revenue"] = 0, 0.0
    add("negative_and_zero_measures", "returns.csv", to_csv(neg).encode(), _truth(neg),
        "returns as negative rows, zero rows")

    og = sales(150, seed=33)
    for r in og:
        r["region"] = "North"
    add("single_group", "onegroup.csv", to_csv(og).encode(), _truth(og),
        "every row in one region")

    gap = sales(300, seed=34, start=dt.date(2024, 1, 1), days=366, skip_months=(3, 4, 5))
    add("time_series_with_gaps", "gaps.csv", to_csv(gap).encode(), _truth(gap),
        "2024 with March-May missing")

    short = sales(40, seed=35, start=dt.date(2024, 6, 1), days=5)
    add("five_day_span", "short.csv", to_csv(short).encode(), _truth(short),
        "all dates inside five days")

    # Excel
    xs = sales(200, seed=40)
    grid = [HEADER] + [[r[k] for k in HEADER] for r in xs]
    add("xlsx_real_dates", "real_dates.xlsx", to_xlsx([("Sales", grid)]), _truth(xs),
        "Excel with real date cells and numbers")

    xm = sales(150, seed=41)
    top = ["Identifiers", None, None, "Dimensions", None, None, "Measures", None, None]
    mgrid = [top, HEADER] + [[r[k] for k in HEADER] for r in xm] + [[], ["Source: ledger, unaudited"]]
    add("xlsx_merged_header_footer", "merged.xlsx",
        to_xlsx([("Sales", mgrid)], {"Sales": ["A1:C1", "D1:F1", "G1:I1"]}), _truth(xm),
        "two-row merged header and a footer note")

    xf = sales(120, seed=42)
    fgrid = [HEADER] + [[r[k] for k in HEADER[:-1]] + [f"=G{i + 2}*H{i + 2}"] for i, r in enumerate(xf)]
    t = _truth(xf)
    add("xlsx_formulas_no_cache", "formulas.xlsx", to_xlsx([("Sales", fgrid)]), t,
        "revenue as =G2*H2 formulas with no cached value (as openpyxl/pandas write them)")

    xc = sales(120, seed=49)
    cgrid = [HEADER] + [[r[k] for k in HEADER[:-1]] + [f"=G{i + 2}*H{i + 2}"] for i, r in enumerate(xc)]
    add("xlsx_formulas_cached", "formulas_cached.xlsx",
        with_cached_values(to_xlsx([("Sales", cgrid)]), "I",
                           {i + 2: r["revenue"] for i, r in enumerate(xc)}), _truth(xc),
        "revenue as formulas WITH cached values, as Excel saves them")

    xb = sales(160, seed=43)
    bgrid = [HEADER]
    for i, r in enumerate(xb):
        bgrid.append([r[k] for k in HEADER])
        if i in (40, 41, 100):
            bgrid.append([None] * len(HEADER))
    add("xlsx_blank_rows_mid", "blanks.xlsx", to_xlsx([("Sales", bgrid)]), _truth(xb),
        "three blank rows in the middle of the data")

    xe = sales(130, seed=44)
    add("xlsx_empty_first_sheet", "twosheets.xlsx",
        to_xlsx([("Cover", []), ("Data", [HEADER] + [[r[k] for k in HEADER] for r in xe])]),
        _truth(xe), "first sheet empty, data on the second")

    xt = sales(140, seed=45)
    tgrid = [HEADER] + [[(str(r[k]) if (k == "units" and i % 3 == 0) else r[k]) for k in HEADER]
                        for i, r in enumerate(xt)]
    add("xlsx_numbers_as_text", "numtext.xlsx", to_xlsx([("Sales", tgrid)]), _truth(xt),
        "a third of the units cells stored as text")
    return out


def cases_round2() -> list[Case]:
    """Round 2 (Phase 14 Step 8): anomalies round 1 did not have."""
    out: list[Case] = []
    add = lambda *a: out.append(Case(*a))  # noqa: E731

    cp = sales(150, seed=60)
    for i, r in enumerate(cp):
        r["product"] = ["€ voucher", "“Deluxe” cog", "Gear – large", "Widget"][i % 4]
    t = _truth(cp)
    t.text_values = {"product": ["€ voucher", "“Deluxe” cog", "Gear – large"]}
    add("cp1252_euro_and_quotes", "cp1252.csv", to_csv(cp).encode("cp1252"), t,
        "Windows-1252 export with a euro sign, curly quotes and an en dash")

    u16 = sales(150, seed=61)
    t = _truth(u16)
    add("utf16_tab_export", "unicode_text.csv", to_csv(u16, delimiter="\t").encode("utf-16"), t,
        "Excel's 'Unicode text' export: UTF-16 with a byte-order mark, tab-delimited")

    tr = sales(150, seed=62)
    text = "".join(line + ",\r\n" for line in to_csv(tr).split("\r\n") if line)
    add("trailing_delimiter", "trailing.csv", text.encode(), _truth(tr),
        "every line ends with a delimiter, so an unnamed empty column appears")

    rg = sales(150, seed=63)
    lines = to_csv(rg).split("\r\n")
    for i in range(5, len(lines) - 1, 25):
        lines[i] = lines[i].rsplit(",", 1)[0]          # a row one field short
    text = "\r\n".join(lines)
    t = _truth(rg, sums=("units",))
    add("ragged_rows", "ragged.csv", text.encode(), t,
        "some rows one field short (a missing trailing revenue)")

    ch = sales(120, seed=64)
    for r in ch:
        r["Region"] = r["region"].upper()
    t = _truth(ch)
    t.cols = 10
    add("headers_differing_only_by_case", "casehdr.csv", to_csv(ch).encode(), t,
        "'region' and 'Region' as two columns")

    lt = sales(60, seed=65)
    for i, r in enumerate(lt):
        r["note"] = ("lorem ipsum " * 1700)[: 20_000 - i]
    add("very_long_text", "longtext.csv", to_csv(lt).encode(), _truth(lt),
        "a 20,000-character text field in every row")

    ac = sales(160, seed=66)
    for i, r in enumerate(ac):
        if i % 5 == 0:
            r["revenue"] = -r["revenue"]
    def acc_fmt(k, v):
        if isinstance(v, dt.date):
            return v.isoformat()
        if k == "revenue":
            return f"({-v:,.2f})" if v < 0 else f"{v:,.2f}"
        return v
    add("accounting_negatives", "accounting.csv", to_csv(ac, fmt=acc_fmt).encode(), _truth(ac),
        "negatives written (1,234.56), thousands separators")

    ws_ = sales(160, seed=67)
    rows_txt = [dict(r) for r in ws_]
    blank_idx = set(range(3, 160, 16))
    for i in blank_idx:
        rows_txt[i]["units"] = "   "
    t = _truth(ws_, sums=("revenue",))
    t.sums["units"] = sum(r["units"] for i, r in enumerate(ws_) if i not in blank_idx)
    add("whitespace_only_cells", "spaces.csv", to_csv(rows_txt, fmt=lambda k, v: v.isoformat()
        if isinstance(v, dt.date) else v).encode(), t, "numeric cells holding only spaces")

    tz = sales(150, seed=68)
    add("timestamps_with_offsets", "tz.csv", to_csv(tz, fmt=lambda k, v: (
        f"{v.isoformat()}T12:00:00+00:00" if isinstance(v, dt.date) else v)).encode(), _truth(tz),
        "ISO timestamps carrying a +00:00 offset")

    hi = [(f"A{i}", 10**19 + i * 7) for i in range(100)]
    t = Truth(rows=100, cols=2, sums={"amount": sum(v for _, v in hi)})
    add("integers_beyond_int64", "huge.csv",
        ("id,amount\n" + "".join(f"{a},{v}\n" for a, v in hi)).encode(), t,
        "20-digit integers, past BIGINT")

    xe = sales(120, seed=69)
    grid = [HEADER] + [[r[k] for k in HEADER] for r in xe]
    bad_rows = {5, 30, 77}
    for i in bad_rows:
        grid[i + 1][8] = ["#DIV/0!", "#N/A", "#VALUE!"][i % 3]
    t = _truth(xe, sums=("units",))
    t.sums["revenue"] = round(sum(r["revenue"] for i, r in enumerate(xe) if i not in bad_rows), 2)
    add("xlsx_error_cells", "errors.xlsx", to_xlsx([("Sales", grid)]), t,
        "Excel error values (#DIV/0!, #N/A, #VALUE!) in a numeric column")

    xt = sales(130, seed=70)
    tgrid = [["Quarterly sales extract"], ["Prepared by finance, unaudited"], [], [],
             HEADER] + [[r[k] for k in HEADER] for r in xt]
    add("xlsx_title_rows_above_header", "titled.xlsx", to_xlsx([("Sales", tgrid)]), _truth(xt),
        "two title lines and two blank rows above the header")

    xv = sales(120, seed=71)
    xv.sort(key=lambda r: r["region"])
    vgrid = [HEADER] + [[r[k] for k in HEADER] for r in xv]
    merges, start = [], 0
    for i in range(1, len(xv) + 1):
        if i == len(xv) or xv[i]["region"] != xv[start]["region"]:
            if i - start > 1:
                merges.append(f"D{start + 2}:D{i + 1}")
                for j in range(start + 1, i):
                    vgrid[j + 1][3] = None
            start = i
    t = _truth(xv)
    t.no_nulls = ["region"]
    add("xlsx_vertically_merged_cells", "vmerge.xlsx", to_xlsx([("Sales", vgrid)], {"Sales": merges}),
        t, "region merged down each group of rows, as a report lays it out")

    cm = sales(140, seed=72)
    add("comment_lines_above_header", "commented.csv",
        ("# exported 2024-09-01 by finance\n# currency: EUR\n" + to_csv(cm)).encode(), _truth(cm),
        "two '#' comment lines above the header")

    im = sales(150, seed=73)
    rows_txt = [dict(r) for r in im]
    for i in (4, 50, 99):
        rows_txt[i]["order_date"] = ["2024-02-30", "2024-13-01", "2023-00-10"][i % 3]
    t = _truth(im)
    t.date_col = None
    add("impossible_dates", "impossible.csv", to_csv(rows_txt, fmt=lambda k, v: v.isoformat()
        if isinstance(v, dt.date) else v).encode(), t, "2024-02-30, 2024-13-01 among valid dates")

    sp = random.Random(74)
    sh = ["id"] + [f"c{i:02d}" for i in range(60)]
    srows = [[f"S{r}"] + [(sp.randint(1, 9) if sp.random() < 0.05 else "") for _ in range(60)]
             for r in range(200)]
    buf = io.StringIO()
    csv.writer(buf).writerows([sh] + srows)
    add("sparse_wide", "sparse.csv", buf.getvalue().encode(), Truth(rows=200, cols=61),
        "61 columns, 95% empty")

    dk = sales(120, seed=75)
    dk[10]["order_id"] = dk[11]["order_id"]
    add("duplicate_keys_conflicting", "dupkeys.csv", to_csv(dk).encode(), _truth(dk),
        "one order_id on two different rows")

    yr = random.Random(76)
    ygrid = "region,2021,2022,2023\n" + "".join(
        f"{r},{yr.randint(1, 99)},{yr.randint(1, 99)},{yr.randint(1, 99)}\n" for r in REGIONS)
    add("years_as_columns", "pivot.csv", ygrid.encode(), Truth(rows=4, cols=4),
        "a pivot: one column per year")

    eu = sales(160, seed=77)
    def eu_fmt(k, v):
        if isinstance(v, dt.date):
            return v.strftime("%d.%m.%Y")
        if isinstance(v, float):
            return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return v
    add("bom_semicolon_german", "german.csv",
        ("\ufeff" + to_csv(eu, delimiter=";", fmt=eu_fmt)).encode(), _truth(eu),
        "German Excel export: BOM, ';', dates 31.12.2024, numbers 1.234,56")

    qn = sales(150, seed=78)
    add("every_value_quoted", "quoted_all.csv",
        to_csv(qn).replace(",", '","').replace("\r\n", '"\r\n"').join(['"', '']).rstrip('"').encode(),
        _truth(qn), "every field quoted, numbers included")

    ml = sales(150, seed=79)
    lines = to_csv(ml, lineterminator="\n").split("\n")
    text = "".join(line + ("\r\n" if i % 2 else "\n") for i, line in enumerate(lines) if line)
    add("mixed_line_endings", "mixed_eol.csv", text.encode(), _truth(ml), "CRLF and LF alternating")

    nh = sales(200, seed=80)
    rows_txt = [dict(r) for r in nh]
    kept = set(range(0, 200, 10))
    for i in range(200):
        if i not in kept:
            rows_txt[i]["revenue"] = ""
    t = _truth(nh, sums=("units",))
    t.sums["revenue"] = round(sum(nh[i]["revenue"] for i in kept), 2)
    add("measure_90pct_null", "mostly_null.csv", to_csv(rows_txt, fmt=lambda k, v: v.isoformat()
        if isinstance(v, dt.date) else v).encode(), t, "revenue empty in 90% of rows")

    xh = to_xlsx([("Sales", [HEADER])])
    add("xlsx_header_only", "empty.xlsx", xh, Truth(), "an Excel sheet with a header and no rows")

    un = sales(150, seed=81)
    for r in un:
        r["weight"] = f"{r['units'] * 0.5}kg"
    add("numbers_with_units", "units.csv", to_csv(un).encode(), _truth(un),
        "a weight column written 12.5kg")

    dd = sales(80, seed=82)
    for r in dd:
        r["order_date"] = dt.date(2024, 5, 17)
    add("single_day", "oneday.csv", to_csv(dd).encode(), _truth(dd), "every row on the same day")
    return out


def cases_round3() -> list[Case]:
    """Round 3 (Phase 14 Step 8): more anomalies, none of them in rounds 1 or 2."""
    out: list[Case] = []
    add = lambda *a: out.append(Case(*a))  # noqa: E731
    iso = lambda k, v: v.isoformat() if isinstance(v, dt.date) else v  # noqa: E731

    pp = sales(150, seed=90)
    add("pipe_delimited", "pipes.csv", to_csv(pp, delimiter="|").encode(), _truth(pp),
        "'|' as the delimiter")

    bs = sales(120, seed=91)
    lines = ["order_id,note,units"] + [f'{r["order_id"]},"said \\"hi\\" twice",{r["units"]}'
                                        for r in bs]
    add("backslash_escaped_quotes", "backslash.csv", ("\n".join(lines) + "\n").encode(),
        Truth(rows=120, cols=3, sums={"units": sum(r["units"] for r in bs)}),
        'quotes escaped with a backslash (\\") instead of doubled')

    ps = sales(150, seed=92)
    add("plus_signed_numbers", "plus.csv", to_csv(ps, fmt=lambda k, v: (
        f"+{v}" if k == "units" else iso(k, v))).encode(), _truth(ps), "units written +12")

    mn = sales(150, seed=93)
    t = _truth(mn)
    add("month_name_dates", "monthname.csv", to_csv(mn, fmt=lambda k, v: (
        v.strftime("%d-%b-%Y") if isinstance(v, dt.date) else v)).encode(), t,
        "dates written 12-Jan-2024")

    yy = sales(150, seed=94)
    add("two_digit_years", "yy.csv", to_csv(yy, fmt=lambda k, v: (
        v.strftime("%d/%m/%y") if isinstance(v, dt.date) else v)).encode(), _truth(yy),
        "dates written 31/12/24")

    xp = sales(120, seed=95)
    for i, r in enumerate(xp):
        r["discount"] = (i % 20) * 0.025
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    hdr = HEADER + ["discount"]
    ws.append(hdr)
    for r in xp:
        ws.append([r[k] for k in hdr])
    for row in ws.iter_rows(min_row=2, min_col=10, max_col=10):
        row[0].number_format = "0.0%"
    buf = io.BytesIO()
    wb.save(buf)
    t = _truth(xp)
    t.sums["discount"] = round(sum(r["discount"] for r in xp), 2)
    add("xlsx_percent_formatted", "pct.xlsx", buf.getvalue(), t,
        "a discount column stored 0.125 and displayed 12.5%")

    xd = sales(120, seed=96)
    for i, r in enumerate(xd):
        r["order_date"] = dt.datetime.combine(r["order_date"], dt.time(i % 24, 30))
    add("xlsx_datetimes_with_time", "datetimes.xlsx",
        to_xlsx([("Sales", [HEADER] + [[r[k] for k in HEADER] for r in xd])]), _truth(xd),
        "order_date as date-times with hours and minutes")

    xb = sales(120, seed=97)
    for i, r in enumerate(xb):
        r["returned"] = i % 3 == 0
    hb = HEADER + ["returned"]
    add("xlsx_boolean_cells", "bools.xlsx",
        to_xlsx([("Sales", [hb] + [[r[k] for k in hb] for r in xb])]), _truth(xb),
        "a TRUE/FALSE boolean column")

    xs = sales(120, seed=98)
    add("xlsx_dates_as_text", "textdates.xlsx", to_xlsx([("Sales", [HEADER] + [
        [r[k].strftime("%d/%m/%Y") if k == "order_date" else r[k] for k in HEADER] for r in xs])]),
        _truth(xs), "dates typed as text 31/12/2024 in Excel")

    nw = sales(160, seed=99)
    rows_txt = [dict(r) for r in nw]
    blanked = {i: ["NULL", "None", "nil", "NaN"][i % 4] for i in range(0, 160, 9)}
    for i, w in blanked.items():
        rows_txt[i]["units"] = w
    t = _truth(nw, sums=("revenue",))
    t.sums["units"] = sum(r["units"] for i, r in enumerate(nw) if i not in blanked)
    add("null_words_in_numbers", "nullwords.csv", to_csv(rows_txt, fmt=iso).encode(), t,
        "NULL, None, nil, NaN written into a number column")

    rng = random.Random(100)
    n = 1_000_000
    amounts = [rng.randint(1, 500) for _ in range(n)]
    big = "id,region,amount\n" + "".join(
        f"{i},{REGIONS[i % 4]},{a}\n" for i, a in enumerate(amounts))
    add("one_million_rows", "million.csv", big.encode(),
        Truth(rows=n, cols=3, sums={"amount": sum(amounts)}), "1,000,000 rows, 3 columns")

    hc = sales(12_000, seed=101)
    for i, r in enumerate(hc):
        r["sku"] = f"SKU-{i % 10_000:05d}"
    add("high_cardinality_dimension", "skus.csv", to_csv(hc).encode(), _truth(hc),
        "a 10,000-value sku column beside the usual dimensions")

    uh = sales(120, seed=102)
    zh = ["订单", "日期", "客户", "地区", "产品", "渠道", "数量 📦", "单价", "收入"]
    t = _truth(uh)
    t.sums = {}
    add("unicode_headers", "headers_zh.csv", to_csv(
        [dict(zip(zh, [r[k] for k in HEADER])) for r in uh], fmt=iso).encode(), t,
        "column names in Chinese, one with an emoji")

    nl = sales(120, seed=103)
    hdr_nl = ["order_id", "order_date", "customer_id", "region", "product", "channel",
              "Units\nSold", "  unit price  ", "revenue"]
    add("header_cells_with_newlines", "nlheader.csv", to_csv(
        [dict(zip(hdr_nl, [r[k] for k in HEADER])) for r in nl], fmt=iso).encode(),
        Truth(rows=120, cols=9, sums={"revenue": round(sum(r["revenue"] for r in nl), 2)}),
        "a header cell holding a line break, another padded with spaces")

    ri = sales(120, seed=104)
    for i, r in enumerate(ri):
        r["rowid"] = 1000 + i
    t = _truth(ri)
    t.sums["rowid"] = sum(1000 + i for i in range(120))
    add("column_named_rowid", "rowid.csv", to_csv(ri, fmt=iso).encode(), t,
        "a column called rowid, which DuckDB also uses internally")

    fr = sales(100, seed=105)
    add("unicode_file_name", "Ventes 2024 (été).csv", to_csv(fr, fmt=iso).encode(), _truth(fr),
        "a file name with spaces, brackets and an accent")

    sap = sales(150, seed=106)
    for i, r in enumerate(sap):
        if i % 6 == 0:
            r["revenue"] = -r["revenue"]
    add("trailing_minus_numbers", "sap.csv", to_csv(sap, fmt=lambda k, v: (
        (f"{-v:.2f}-" if v < 0 else f"{v:.2f}") if k == "revenue" else iso(k, v))).encode(),
        _truth(sap), "SAP-style negatives written 1234.56-")

    zd = sales(150, seed=107)
    rows_txt = [dict(r) for r in zd]
    zeroed = set(range(7, 150, 15))
    for i in zeroed:
        rows_txt[i]["order_date"] = "0000-00-00"
    t = _truth(zd)
    kept = [r["order_date"] for i, r in enumerate(zd) if i not in zeroed]
    t.date_range = (min(kept).isoformat(), max(kept).isoformat())
    add("mysql_zero_dates", "zerodates.csv", to_csv(rows_txt, fmt=iso).encode(), t,
        "'0000-00-00' for an unknown date, as MySQL exports it")
    return out


def cases_round4() -> list[Case]:
    """Round 4 (Phase 14 Step 8): values that stress the analyses rather than the loaders."""
    out: list[Case] = []
    add = lambda *a: out.append(Case(*a))  # noqa: E731
    iso = lambda k, v: v.isoformat() if isinstance(v, dt.date) else v  # noqa: E731

    cm = sales(150, seed=110)
    for r in cm:
        r["units"] = 5
    add("constant_measure", "constant.csv", to_csv(cm, fmt=iso).encode(), _truth(cm),
        "units is 5 in every row: zero variance")

    ng = sales(160, seed=111)
    rows_txt = [dict(r) for r in ng]
    for i, r in enumerate(rows_txt):
        if i % 7 == 0:
            r["channel"] = ""
        elif i % 11 == 0:
            r["channel"] = '""'
    add("empty_and_null_groups", "groups.csv", to_csv(rows_txt, fmt=iso).encode(),
        _truth(ng), "a dimension with empty cells and literal \"\" strings as groups")

    ex = sales(150, seed=112)
    for i, r in enumerate(ex):
        r["order_date"] = [dt.date(1800, 1, 1), dt.date(2999, 12, 31)][i % 2] if i < 4 \
            else r["order_date"]
    add("extreme_dates", "extreme.csv", to_csv(ex, fmt=iso).encode(), _truth(ex),
        "dates in 1800 and 2999 among ordinary ones")

    ol = sales(150, seed=113)
    ol[17]["revenue"] = 1e12
    add("extreme_outlier", "outlier.csv", to_csv(ol, fmt=iso).encode(), _truth(ol),
        "one revenue of 1,000,000,000,000 among hundreds")

    an = sales(150, seed=114)
    for r in an:
        r["units"], r["revenue"] = -r["units"], -r["revenue"]
    add("all_negative_measure", "negative.csv", to_csv(an, fmt=iso).encode(), _truth(an),
        "every units and revenue value negative")

    tw = sales(2, seed=115)
    add("two_rows", "tworows.csv", to_csv(tw, fmt=iso).encode(), _truth(tw), "exactly two rows")

    x3 = sales(120, seed=116)
    g3 = [["Sales", None, None, None, None, None, None, None, None],
          ["Keys", None, None, "Labels", None, None, "Numbers", None, None],
          HEADER] + [[r[k] for k in HEADER] for r in x3]
    t = _truth(x3)
    t.sums = {}
    add("xlsx_three_header_rows", "three.xlsx",
        to_xlsx([("Sales", g3)], {"Sales": ["A1:I1", "A2:C2", "D2:F2", "G2:I2"]}), t,
        "three stacked, merged header rows")

    xu = sales(120, seed=117)
    add("xlsx_unicode_sheet_name", "donnees.xlsx", to_xlsx([("Données 2024", [HEADER] + [
        [r[k] for k in HEADER] for r in xu])]), _truth(xu), "a sheet named 'Données 2024'")

    nm = sales(150, seed=118)
    rows_txt = [dict(r) for r in nm]
    for r in rows_txt:
        r["revenue"] = ""
    t = _truth(nm, sums=("units",))
    add("all_null_measure", "nullmeasure.csv", to_csv(rows_txt, fmt=iso).encode(), t,
        "revenue empty in every row")

    kn = sales(150, seed=119)
    rows_txt = [dict(r) for r in kn]
    for i in range(0, 150, 30):
        rows_txt[i]["order_id"] = ""
    add("key_with_nulls", "nullkey.csv", to_csv(rows_txt, fmt=iso).encode(), _truth(kn),
        "the id column empty in a few rows")

    tn = random.Random(120)
    tiny = [tn.uniform(1e-9, 9e-9) for _ in range(150)]
    add("tiny_decimals", "tiny.csv", ("id,region,dose\n" + "".join(
        f"T{i},{REGIONS[i % 4]},{v!r}\n" for i, v in enumerate(tiny))).encode(),
        Truth(rows=150, cols=3), "a measure of values around 0.000000005")

    tm = sales(150, seed=121)
    for i, r in enumerate(tm):
        r["time"] = f"{i % 24:02d}:{(i * 7) % 60:02d}"
    add("time_only_column", "times.csv", to_csv(tm, fmt=iso).encode(), _truth(tm),
        "a column of clock times 13:45")
    return out


# --- running ------------------------------------------------------------------------------------

@dataclass
class Rec:
    dataset: str
    stage: str
    call: str
    outcome: str
    reason: str = ""
    detail: str = ""
    seconds: float = 0.0


_FIGURE = re.compile(r"(?<![A-Za-z_])(nan|NaN|-?inf|-?Infinity)(?![A-Za-z_])")


def _figure_in_cells(text: str):
    """nan/inf printed as a FIGURE: in a table cell after the row's label. A note that explains
    values were set aside names them in prose, and a frequency table's label column holds the
    data's own values -- neither is a statistic gone wrong."""
    for line in text.splitlines():
        if line.lstrip().startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            for cell in cells[1:]:
                m = _FIGURE.fullmatch(cell)
                if m:
                    return re.search(re.escape(cell), text)
    return None


def refusal_reason(text: str) -> str | None:
    r = reason_of(text)
    if r is not None:
        return r.value
    return "LOAD_REFUSED" if text.lstrip().startswith("BLOCKED") else None


def first_lines(text: str, n: int = 3) -> str:
    return " | ".join(line.strip() for line in text.strip().splitlines()[:n] if line.strip())[:400]


class Runner:
    def __init__(self) -> None:
        self.be = RealBackend()
        self.recs: list[Rec] = []

    def record(self, case: Case, stage: str, call: str, fn):
        """Call fn(); classify. Returns fn's value, or None if it raised."""
        t0 = time.perf_counter()
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - the point is to catch what escapes
            tb = traceback.extract_tb(exc.__traceback__)
            where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb)
                          if "backend/engine" in f.filename), "?")
            self.recs.append(Rec(case.name, stage, call, "CRASH", type(exc).__name__,
                                 f"{exc!s}"[:300] + f" @ {where}", time.perf_counter() - t0))
            return None
        secs = time.perf_counter() - t0
        text = value if isinstance(value, str) else getattr(value, "message", "") or ""
        refusal = getattr(value, "refusal", None)
        reason = refusal.reason if refusal is not None else (
            refusal_reason(value) if isinstance(value, str) else None)
        if reason:
            detail = first_lines(refusal.text if refusal is not None else value, 4)
            self.recs.append(Rec(case.name, stage, call, "REFUSED", reason, detail, secs))
        else:
            m = _figure_in_cells(text) if isinstance(value, str) else None
            if m:
                ctx = text[max(0, m.start() - 80):m.end() + 40].replace("\n", " / ")
                self.recs.append(Rec(case.name, stage, call, "SUSPECT", "NON_FINITE_FIGURE",
                                     f"...{ctx}...", secs))
            else:
                self.recs.append(Rec(case.name, stage, call, "OK", "", first_lines(text, 1), secs))
        if secs > SLOW_SECONDS:
            self.recs.append(Rec(case.name, stage, call, "SUSPECT", "SLOW",
                                 f"{secs:.1f}s (> {SLOW_SECONDS:.0f}s)", secs))
        return value

    def wrong(self, case: Case, stage: str, check: str, detail: str) -> None:
        self.recs.append(Rec(case.name, stage, check, "WRONG", "GROUND_TRUTH", detail))

    def ok(self, case: Case, stage: str, check: str, detail: str = "") -> None:
        self.recs.append(Rec(case.name, stage, check, "OK", "", detail))

    # -- the path ------------------------------------------------------------------------------

    def run(self, case: Case) -> None:
        be = self.be
        ws = be.new_workspace_id()
        try:
            self._run(case, be, ws)
        finally:
            workspace.reset(ws)
            try:
                workspace.workspace_dir(ws).rmdir()
            except OSError:
                pass

    def _sql(self, ws: str, sql: str, params=None):
        with self.be._workspace(ws):
            con = db.connect(ws)
            try:
                return con.execute(sql, params or []).fetchall()
            finally:
                con.close()

    def _run(self, case: Case, be: RealBackend, ws: str) -> None:
        up = self.record(case, "upload", "save_upload",
                         lambda: be.save_upload(ws, case.filename, case.data))
        if up is None or up.path is None:
            return
        d = self.record(case, "ingest", "draft_ingest", lambda: be.draft_ingest(ws, up.path))
        if d is None:
            return
        if d.refusal is None and d.unresolved:
            self.recs.append(Rec(case.name, "ingest", "draft_ingest", "REFUSED", "PROVISIONAL",
                                 f"unresolved {d.unresolved}; questions {d.questions[:2]}"))
            d = self.record(case, "ingest", "draft_ingest(header_rows=[1])",
                            lambda: be.draft_ingest(ws, up.path, header_rows=[1]))
            if d is None:
                return
        if d.refusal is not None and "header_rows" in (d.refusal.next_step or ""):
            # A draft that asks which row is the header is answered, as a person would.
            self.recs.append(Rec(case.name, "ingest", "draft_ingest", "REFUSED",
                                 d.refusal.reason, first_lines(d.refusal.text, 3)))
            d = self.record(case, "ingest", "draft_ingest(header_rows=[1])",
                            lambda: be.draft_ingest(ws, up.path, header_rows=[1]))
            if d is None:
                return
        if d.refusal is not None or d.unresolved:
            return
        loaded = self.record(case, "ingest", "confirm_ingest", lambda: be.confirm_ingest(ws, d.spec))
        if loaded is None or not loaded.ok:
            return
        name = d.dataset_name
        cols = self._sql(ws, "SELECT column_name, data_type FROM information_schema.columns "
                             "WHERE table_name = ? ORDER BY ordinal_position", [name])
        types = dict(cols)
        source_to_target = {c["source_name"]: c["target_name"] for c in d.spec.get("columns", [])}
        self._check_load(case, ws, name, cols, types, source_to_target)

        self.record(case, "inspect", "describe_dataset",
                    lambda: server.describe_dataset(dataset_name=name, workspace_id=ws))
        self.record(case, "inspect", "profile_dataset",
                    lambda: server.profile_dataset(dataset_name=name, workspace_id=ws))

        # Up to three rounds of the engine's own suggestions, as a person clicking Apply on the
        # Clean screen and looking again would: dropping a pasted header is what makes the
        # conversions behind it lossless, so they come round on the next proposal.
        cleaned = False
        for round_no in range(1, 4):
            p = self.record(case, "clean", f"propose_cleaning (round {round_no})",
                            lambda: be.propose_cleaning(ws, name))
            if p is None or not p.steps:
                break
            picked = [s.action_id for s in p.steps if s.suggested]
            self.recs.append(Rec(case.name, "clean", f"proposal (round {round_no})", "OK", "",
                                 "; ".join(f"{s.action_id} {s.kind} {s.column or ''}"
                                           f"{' LOSSY' if s.lossy else ''}" for s in p.steps)[:400]))
            if not picked:
                break
            self.record(case, "clean", f"apply_cleaning({picked})",
                        lambda: be.apply_cleaning(ws, name, picked))
            cleaned = True
        load_wrong = any(r.outcome == "WRONG" and r.dataset == case.name for r in self.recs)
        if cleaned or load_wrong:
            cols = self._sql(ws, "SELECT column_name, data_type FROM information_schema.columns "
                                 "WHERE table_name = ? ORDER BY ordinal_position", [name])
            types = dict(cols)
            self._check_load(case, ws, name, cols, types, source_to_target, "after_clean")

        answers = self._contract(case, ws, name, types)
        if answers is None:
            return
        self.record(case, "validate", "validate_dataset",
                    lambda: server.validate_dataset(dataset_name=name, workspace_id=ws))
        self._analyses(case, ws, name, answers, types)
        self.record(case, "report", "build_report",
                    lambda: server.build_report(dataset_name=name, question="What does this data show?",
                                                workspace_id=ws))

    def _check_load(self, case, ws, name, cols, types, s2t, stage="ground_truth") -> None:
        """Every ground-truth check, at load and again after the suggested cleaning."""
        t = case.truth
        n = self._sql(ws, f'SELECT count(*) FROM "{name}"')[0][0]
        if t.rows is not None:
            (self.ok if n == t.rows else self.wrong)(case, stage, "row count",
                                                     f"loaded {n}, file has {t.rows}")
        if t.cols is not None:
            (self.ok if len(cols) == t.cols else self.wrong)(
                case, stage, "column count", f"loaded {len(cols)}, file has {t.cols}: "
                f"{[c for c, _ in cols][:12]}")
        for src, want in t.sums.items():
            col = s2t.get(src, src)
            if col not in types:
                self.wrong(case, stage, f"sum({src})", f"column {src!r} not found as {col!r}; "
                           f"have {list(types)[:12]}")
                continue
            if types[col] not in ("BIGINT", "INTEGER", "DOUBLE", "HUGEINT", "SMALLINT", "FLOAT",
                                  "TINYINT", "UBIGINT") and not types[col].startswith("DECIMAL"):
                self.wrong(case, stage, f"type({src})",
                           f"{col!r} loaded as {types[col]}, the file holds numbers")
                continue
            got = self._sql(ws, f'SELECT sum("{col}") FROM "{name}"')[0][0]
            # Exact: a float sum compared in floats hid a 34,650 error on 20-digit integers.
            exact = abs(Decimal(got) - Decimal(str(want))) if got is not None else None
            (self.ok if exact is not None and exact <= Decimal("0.011") else self.wrong)(
                case, stage, f"sum({src})", f"engine {got}, file {want}")
        if t.date_col and t.date_range:
            col = s2t.get(t.date_col, t.date_col)
            if col in types:
                lo, hi = self._sql(ws, f'SELECT min("{col}")::VARCHAR, max("{col}")::VARCHAR '
                                       f'FROM "{name}"')[0]
                ok = types[col] in ("DATE", "TIMESTAMP") and (lo or "")[:10] == t.date_range[0] \
                    and (hi or "")[:10] == t.date_range[1]
                (self.ok if ok else self.wrong)(
                    case, stage, f"dates({t.date_col})",
                    f"{types[col]} {lo}..{hi}; file {t.date_range[0]}..{t.date_range[1]}")
        self._check_values(case, ws, name, types, s2t, stage)
        for src in t.no_nulls:
            col = s2t.get(src, src)
            if col not in types:
                self.wrong(case, stage, f"no_nulls({src})", f"column not found; have {list(types)[:10]}")
                continue
            n = self._sql(ws, f'SELECT count(*) FROM "{name}" WHERE "{col}" IS NULL')[0][0]
            (self.ok if n == 0 else self.wrong)(case, stage, f"no_nulls({src})",
                                                f"{n} row(s) have no {src}; the file fills every one")

    def _check_values(self, case, ws, name, types, s2t, stage) -> None:
        """Text that must survive exactly: accents, CJK, leading zeros. Run after loading and
        again after the suggested cleaning, which is where a zero could be lost."""
        for src, must in case.truth.text_values.items():
            col = s2t.get(src, src)
            if col not in types:
                continue
            have = {r[0] for r in self._sql(ws, f'SELECT DISTINCT "{col}"::VARCHAR FROM "{name}"')}
            missing = [v for v in must if v not in have]
            (self.ok if not missing else self.wrong)(
                case, stage, f"values({src})",
                f"{types[col]}; missing {missing}; have e.g. {sorted(x for x in have if x)[:5]}")

    def _contract(self, case, ws, name, types) -> dict | None:
        be = self.be
        first = self.record(case, "contract", "draft_contract", lambda: be.draft_contract(ws, name))
        if first is None or first.refusal is not None:
            return None
        roles = {c.name: c.suggested_role for c in first.columns}
        measures = [c for c, r in roles.items() if r == "measure"]
        dims = [c for c, r in roles.items() if r == "dimension"]
        date = first.date_column or next(
            (c for c, t in types.items() if t in ("DATE", "TIMESTAMP")), None)
        widest = max((x.distinct_count or 0 for x in first.columns), default=0)
        key = first.primary_key or [c.name for c in first.columns
                                    if c.distinct_count and c.null_count == 0
                                    and c.distinct_count == widest and "id" in c.name.lower()][:1]
        # Fewest groups first, as a person would pick: the group caps (49) are a correct refusal
        # for a 60-customer dimension, and the all-unique case tests that on purpose.
        distinct = {c.name: c.distinct_count or 0 for c in first.columns}
        dims = sorted((x for x in dims if x not in key and x != date), key=lambda x: distinct[x])
        measures = [x for x in measures if x not in key and x != date]
        window = (None, None)
        if date:
            lo, hi = self._sql(ws, f'SELECT min("{date}")::DATE::VARCHAR, max("{date}")::DATE::VARCHAR '
                                   f'FROM "{name}"')[0]
            window = (lo, hi)
        answers = dict(grain="one row = one record", primary_key=key, date_column=date,
                       measures=measures, dimensions=dims,
                       aggregations={m: "sum" for m in measures},
                       measure_definitions={m: f"the {m} column as supplied" for m in measures},
                       analysis_window_start=window[0], analysis_window_end=window[1], caveats=[])
        draft = self.record(case, "contract", "draft_contract(answered)",
                            lambda: be.draft_contract(ws, name, **answers))
        if draft is None or draft.refusal is not None:
            return None
        if draft.provisional:
            self.recs.append(Rec(case.name, "contract", "draft_contract(answered)", "REFUSED",
                                 "STILL_PROVISIONAL", f"{draft.provisional}"[:300]))
        res = self.record(case, "contract", "confirm_contract", lambda: be.confirm_contract(ws, draft))
        if res is None or not res.ok:
            return None
        return answers

    def _analyses(self, case, ws, name, a, types) -> None:
        ms, ds = a["measures"], a["dimensions"]
        m1 = ms[0] if ms else None
        m2 = ms[1] if len(ms) > 1 else None
        d1 = ds[0] if ds else None
        d2 = ds[1] if len(ds) > 1 else None
        date = a["date_column"]
        months = [r[0] for r in self._sql(
            ws, f'SELECT DISTINCT strftime("{date}", \'%Y-%m\') m FROM "{name}" '
                f'WHERE "{date}" IS NOT NULL ORDER BY m')] if date else []
        period, baseline = (months[-1], months[-2]) if len(months) >= 2 else (None, None)
        lo, hi = a["analysis_window_start"], a["analysis_window_end"]
        mid = None
        if lo and hi:
            a_, b_ = dt.date.fromisoformat(lo), dt.date.fromisoformat(hi)
            mid = a_ + (b_ - a_) / 2
        entity = next((c for c in ds if "customer" in c.lower()), ds[-1] if ds else None)
        args = {
            "summary_stats": {}, "distribution": dict(measure=m1), "frequency": dict(column=d1),
            "cross_tab": dict(rows=d1, columns=d2), "top_n": dict(dimension=d1, measure=m1),
            "group_compare": dict(dimension=d1, measure=m1),
            "pareto": dict(dimension=d1, measure=m1), "concentration": dict(dimension=d1, measure=m1),
            "ranking_shift": dict(dimension=d1, measure=m1, before_start=lo,
                                  before_end=mid.isoformat() if mid else None,
                                  after_start=(mid + dt.timedelta(days=1)).isoformat() if mid else None,
                                  after_end=hi),
            "calendar_coverage": dict(grain="month"), "trend": dict(measure=m1, grain="month"),
            "seasonality": dict(measure=m1, grain="month"),
            "period_compare": dict(measure=m1, period=period, baseline=baseline, grain="month"),
            "growth_decomposition": dict(measure=m1, dimension=d1, period=period, baseline=baseline,
                                         grain="month"),
            "correlation": dict(measure=m1, against=m2), "bivariate": dict(measure=m1, against=m2),
            "driver_analysis": dict(measure=m1),
            "mix_shift": dict(measure=m1, dimension=d1, period=period, baseline=baseline, grain="month"),
            "outlier_detection": dict(measure=m1), "changepoint": dict(measure=m1, grain="month"),
            "correlated_shift": dict(measure=m1, against=m2, grain="month"),
            "hypothesis_test": dict(dimension=d1, measure=m1),
            "confidence_interval": dict(measure=m1),
            "effect_size": dict(dimension=d1, measure=m1),
            "sample_adequacy": dict(dimension=d1, measure=m1),
            "repeat_behaviour": dict(entity=entity),
            "cohort_retention": dict(entity=entity, period="month"),
        }
        for kind in registry.REGISTRY:
            kw = {k: v for k, v in args.get(kind, {}).items() if v is not None}
            call = f'compute_analysis("{kind}", {", ".join(f"{k}={v!r}" for k, v in kw.items())})'
            self.record(case, "analysis", call, lambda kind=kind, kw=kw: server.compute_analysis(
                dataset_name=name, analysis_type=kind, workspace_id=ws, **kw))
        # y named: a result carrying the measure and a row count asks which one to draw.
        charts = [("top_n", "bar", dict(dimension=d1, measure=m1, y=f"{m1} (sum)" if m1 else None)),
                  ("trend", "line", dict(measure=m1, grain="month", y=f"{m1} (sum)" if m1 else None)),
                  ("distribution", "histogram", dict(measure=m1, y="rows"))]
        for kind, chart, kw in charts:
            kw = {k: v for k, v in kw.items() if v is not None}
            self.record(case, "chart", f'render_chart("{kind}", "{chart}")',
                        lambda kind=kind, chart=chart, kw=kw: server.render_chart(
                            dataset_name=name, analysis_type=kind, chart=chart, workspace_id=ws, **kw))


def main(argv: list[str]) -> int:
    """--round N picks a round of datasets (1 by default, "all" for every round); other words
    filter datasets by name. Round 1 writes stress_matrix.json, round N stress_matrix_rN.json."""
    rnd = "1"
    if "--round" in argv:
        i = argv.index("--round")
        rnd = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if rnd == "all":
        for r in sorted(ROUNDS):
            run_round(r, argv)
        return 0
    return run_round(int(rnd), argv)


def run_round(rnd: int, argv: list[str]) -> int:
    print(f"=== round {rnd}")
    chosen = [c for c in ROUNDS[rnd]() if not argv or any(a in c.name for a in argv)]
    runner = Runner()
    t0 = time.perf_counter()
    for c in chosen:
        t = time.perf_counter()
        n0 = len(runner.recs)
        try:
            runner.run(c)
        except Exception as exc:  # noqa: BLE001 - a harness fault must not hide the other datasets
            runner.recs.append(Rec(c.name, "harness", "run", "CRASH", "HARNESS",
                                   f"{type(exc).__name__}: {exc}"[:300]))
        mine = runner.recs[n0:]
        counts = {o: sum(r.outcome == o for r in mine) for o in OUTCOMES}
        print(f"{c.name:32s} {time.perf_counter() - t:6.1f}s  " +
              "  ".join(f"{k} {v:3d}" for k, v in counts.items()), flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = dict(
        run_at=dt.datetime.now().isoformat(timespec="seconds"),
        seconds=round(time.perf_counter() - t0, 1),
        datasets=[dict(name=c.name, file=c.filename, bytes=len(c.data), what=c.what) for c in chosen],
        records=[asdict(r) for r in runner.recs])
    (OUT / ("stress_matrix.json" if rnd == 1 else f"stress_matrix_r{rnd}.json")).write_text(
        json.dumps(payload, indent=1, ensure_ascii=False, default=str))
    totals = {o: sum(r.outcome == o for r in runner.recs) for o in OUTCOMES}
    print(f"\n{len(chosen)} datasets, {len(runner.recs)} records in {payload['seconds']}s: " +
          ", ".join(f"{k} {v}" for k, v in totals.items()))
    return 0


ROUNDS = {1: cases, 2: cases_round2, 3: cases_round3, 4: cases_round4}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
