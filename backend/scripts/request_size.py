"""How large the Ask assistant's requests grow on a retail-like table, with the model scripted.

    uv run python scripts/request_size.py [--rows N] [--caveats-only] [path/to/retail_fixture.csv]

The retail fixture of 25/09/2026 (224,955 rows) is not in the repository. Without a path, this
builds a retail-like table here instead: the same columns, and the same kinds of fault the
fixture's generator planted (exact copies, negative lines, placeholder text, two date formats,
blanks exactly where channel = 'Store', orders under two reps, lines after a rep's exit), so the
engine measures a caveat block of the fixture's size when the contract is confirmed. The contract
carries the 13 caveats the retail web workspace held (declared, not measured).

Then `agent.answer` runs with the real Groq session whose HTTP post is replaced by a script: six
tool calls, one a round, then an answer. The tools run for real. Prints each request's size --
characters of the JSON body and characters / 4 as tokens -- and each tool reply's length as the
model was sent it. No network, no key (docs/steps/step2_free_model_reliability.md).
`--caveats-only` turns the token budget off, to measure what sending the caveats once does alone.
"""

from __future__ import annotations

import csv
import io
import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from backend.engine import workspace  # noqa: E402
from backend.engine.state import DECLARED, MEASURED  # noqa: E402
from backend.engine.webapp import agent, llm  # noqa: E402

try:  # the step-2 tree; the unchanged loop has no budget to turn off
    from backend.engine.webapp import budget  # noqa: E402
except ImportError:
    budget = None
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

QUESTION = ("For 2025 only, give revenue, cost and margin % by region, and list the data-quality "
            "issues I should know about.")
MODEL = "openai/gpt-oss-120b"
LIMIT = 8_000  # Groq's tokens a minute for MODEL, named by its 413 of 25/09/2026
NAME = "retail_fixture"
CAVEATS = [
    "120 exact duplicate rows; remove before summing",
    "8 lines have units = -1 and negative revenue/cost",
    "25 lines have unit_price 5–20× list price (likely entry errors)",
    "customer_age has 'unknown' in 3,470 rows",
    "rating has '-' in 5,353 rows",
    "delivery_date has two formats: yyyy-mm-dd and dd/mm/yyyy",
    "13 lines have delivery_date before order date",
    "Store rows have no courier, delivery_date, session or pages data",
    "order_shipping_fee, web_session_seconds, pages_viewed repeat on every line of an order",
    "24 orders have more than one rep_id",
    "112 lines are dated after the rep's exit date",
    "customer_state blank in 3,470 rows",
    "line_revenue excludes shipping and is not reduced for returns",
]
MEASURES = {
    "unit_price": ("median", "Price of one unit in ₹, before discount"),
    "line_revenue": ("sum", "Revenue for the line in ₹ = unit_price × units × (1 − discount_pct)"),
    "line_cost": ("sum", "Cost of goods for the line in ₹"),
    "margin_pct": ("none", "Margin of this line in %; for a group, from summed revenue and cost"),
    "stock_on_hand_at_order": ("none", "Units in stock when the order was placed (a snapshot)"),
    "web_session_seconds": ("none", "Length of the order's session, repeated on every line"),
    "pages_viewed": ("none", "Pages viewed in the order's session, repeated on every line"),
    "rep_monthly_salary": ("none", "Monthly salary of the rep, repeated on every line"),
}
DIMENSIONS = ["line_id", "order_id", "line_no", "customer_id", "customer_age", "customer_segment",
              "customer_city", "customer_state", "region", "city_tier", "channel",
              "payment_method", "campaign_code", "sku", "category", "sub_category",
              "list_price_display", "units", "discount_pct", "order_shipping_fee", "courier",
              "delivery_days", "delivery_date", "return_reason", "rating", "review_text", "rep_id",
              "rep_department", "rep_manager_id", "rep_performance_rating"]
TOP = {"analysis_type": "top_n", "dimension": "region", "n": 10, "period": "2025",
       "grain": "year", "dataset_name": NAME}
#: A reconstruction: the live run's six calls were not recorded (recheck command 6).
CALLS = [("get_workflow_state", {}),
         ("compute_analysis", {**TOP, "measure": "line_revenue"}),
         ("compute_analysis", {**TOP, "measure": "line_cost"}),
         ("validate_dataset", {"dataset_name": NAME}),
         ("profile_dataset", {"dataset_name": NAME}),
         ("get_cleaning_ledger", {"dataset_name": NAME})]

REGIONS = {"North": ["Delhi", "Jaipur", "Lucknow"], "South": ["Chennai", "Bengaluru", "Kochi"],
           "West": ["Mumbai", "Pune", "Ahmedabad"], "East": ["Kolkata", "Patna", "Bhubaneswar"]}
STATES = {"Delhi": "DL", "Jaipur": "RJ", "Lucknow": "UP", "Chennai": "TN", "Bengaluru": "KA",
          "Kochi": "KL", "Mumbai": "MH", "Pune": "MH", "Ahmedabad": "GJ", "Kolkata": "WB",
          "Patna": "BR", "Bhubaneswar": "OD"}
CATEGORIES = {"Electronics": ["Phones", "Audio"], "Home": ["Kitchen", "Decor"],
              "Fashion": ["Shoes", "Tops"], "Grocery": ["Staples", "Snacks"]}


def build(rows: int, seed: int = 26) -> bytes:
    """A retail-like CSV of about `rows` lines, 2023-2025, with the fixture's kinds of fault."""
    rng = random.Random(seed)
    skus = [(f"SKU{i:04d}", cat, sub, round(rng.uniform(150, 9000), 2))
            for i, (cat, sub) in enumerate([(c, s) for c, subs in CATEGORIES.items()
                                            for s in subs] * 25)]
    reps = [(f"R{i:03d}", rng.choice(["Inside", "Field"]), f"M{i % 7:02d}",
             rng.choice(["A", "B", "C"]), rng.randrange(30_000, 90_000, 500),
             date(2025, rng.randint(1, 11), rng.randint(1, 28)) if i % 6 == 0 else None)
            for i in range(40)]
    start, days = date(2023, 1, 1), (date(2025, 12, 31) - date(2023, 1, 1)).days
    header = ["line_id", "order_id", "line_no", "order_ts", "customer_id", "customer_age",
              "customer_segment", "customer_city", "customer_state", "region", "city_tier",
              "channel", "payment_method", "campaign_code", "sku", "category", "sub_category",
              "list_price_display", "unit_price", "units", "discount_pct", "line_revenue",
              "line_cost", "margin_pct", "order_shipping_fee", "courier", "delivery_days",
              "delivery_date", "return_reason", "rating", "review_text",
              "stock_on_hand_at_order", "web_session_seconds", "pages_viewed", "rep_id",
              "rep_department", "rep_manager_id", "rep_performance_rating",
              "rep_monthly_salary", "rep_exit_date"]
    out: list[list] = []
    order = 0
    while len(out) < rows:
        order += 1
        day = start + timedelta(days=rng.randrange(days + 1))
        if day.year == 2024 and day.month == 2:  # one month with no rows
            continue
        ts = datetime(day.year, day.month, day.day, rng.randrange(24), rng.randrange(60))
        region = rng.choice(list(REGIONS))
        city = rng.choice(REGIONS[region])
        channel = rng.choice(["Web", "Web", "App", "Store"])
        store = channel == "Store"
        rep = rng.choice(reps)
        session = "" if store else rng.randrange(40, 1800)
        pages = "" if store else rng.randrange(1, 40)
        fee = 0 if store else rng.choice([0, 49, 99])
        customer = f"C{rng.randrange(1, 9000):05d}"
        for line_no in range(1, rng.choice([1, 1, 2, 3, 4]) + 1):
            sku, cat, sub, price = rng.choice(skus)
            units = rng.randint(1, 5)
            disc = rng.choice([0, 0, 5, 10, 15, 20])
            unit_price = price
            if rng.random() < 0.0008:
                unit_price = round(price * rng.uniform(19.4, 20.6), 2)  # an entry error
            if rng.random() < 0.0003:
                units = -1
            revenue = round(unit_price * units * (1 - disc / 100), 2)
            cost = round(price * units * rng.uniform(0.55, 0.85), 2)
            margin = round((revenue - cost) / revenue * 100, 2) if revenue else 0
            ddays = "" if store else rng.randint(1, 9)
            if store:
                delivered = ""
            else:
                when = day + timedelta(days=ddays)
                if rng.random() < 0.0005:
                    when = day - timedelta(days=rng.randint(1, 5))
                delivered = (when.isoformat() if rng.random() < 0.8
                             else when.strftime("%d/%m/%Y"))
            line_rep = rep
            if line_no > 1 and rng.random() < 0.02:
                line_rep = rng.choice(reps)  # an order under two reps
            exit_day = line_rep[5]
            out.append([
                f"L{len(out) + 1:07d}", f"O{order:06d}", line_no, ts.isoformat(sep=" "), customer,
                "unknown" if rng.random() < 0.015 else rng.randint(18, 70),
                rng.choice(["Consumer", "Corporate", "Small Business"]),
                city.upper() if rng.random() < 0.01 else city,
                "" if rng.random() < 0.015 else STATES[city], region,
                rng.choice(["Tier 1", "Tier 2", "Tier 3"]), channel,
                rng.choice(["UPI", "Card", "COD", "Wallet"]),
                "" if rng.random() < 0.6 else f"CMP{rng.randint(1, 30):02d}",
                sku, cat, sub, f"₹{price:,.2f}", unit_price, units, disc, revenue, cost, margin,
                fee, "" if store else rng.choice(["BlueDart", "Delhivery", "Ekart"]), ddays,
                delivered,
                "" if rng.random() < 0.9 else rng.choice(["Damaged", "Late", "Wrong size"]),
                "-" if rng.random() < 0.024 else rng.randint(1, 5),
                "" if rng.random() < 0.7 else rng.choice(["Good", "Okay", "Poor packing"]),
                rng.randint(0, 400), session, pages, line_rep[0], line_rep[1], line_rep[2],
                line_rep[3], line_rep[4], exit_day.isoformat() if exit_day else ""])
    for i in rng.sample(range(len(out)), 120):  # exact copies
        out.append(list(out[i]))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(out)
    return buf.getvalue().encode("utf-8")


def _block(text: str) -> int:
    return sum(1 for line in text.splitlines() if MEASURED in line or DECLARED in line)


def _block_chars(text: str) -> int:
    return sum(len(line) + 1 for line in text.splitlines() if MEASURED in line or DECLARED in line)


def main(argv: list[str]) -> int:
    rows = 30_000
    if "--caveats-only" in argv:
        argv = [a for a in argv if a != "--caveats-only"]
        budget.allowance = lambda provider, model: None
        print("token budget: off")
    if "--rows" in argv:
        i = argv.index("--rows")
        rows = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if argv:
        csv_path = Path(argv[0]).expanduser()
        if not csv_path.is_file():
            print(f"no file at {csv_path}")
            return 1
        name, data = csv_path.name, csv_path.read_bytes()
        print(f"table: {csv_path}")
    else:
        name, data = f"{NAME}.csv", build(rows)
        print(f"table: built here, {data.count(b'\n') - 1:,} rows (the fixture's own file is not "
              f"in the repository)")
    be = RealBackend()
    ws = be.new_workspace_id()
    try:
        path = be.save_upload(ws, name, data).path
        assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
        draft = be.draft_contract(
            ws, NAME, grain="order_id + line_no", primary_key=[], date_column="order_ts",
            measures=list(MEASURES), dimensions=DIMENSIONS,
            aggregations={k: v[0] for k, v in MEASURES.items()},
            measure_definitions={k: v[1] for k, v in MEASURES.items()},
            analysis_window_start="2023-01-01", analysis_window_end="2025-12-31",
            caveats=CAVEATS)
        confirmed = be.confirm_contract(ws, draft)
        assert confirmed.ok, confirmed.message
        print(f"contract confirmed: {len(draft.measured_caveats)} measured caveat line(s), "
              f"{len(CAVEATS)} declared")

        script = [{"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
            for i, (name, args) in enumerate(CALLS, 1)]
        script.append({"choices": [{"message": {"role": "assistant",
                                                "content": "Answered from the replies above."}}]})
        sizes: list[tuple[int, list[tuple[str, int, int]]]] = []

        def post(body, **_):
            chars = len(json.dumps(body, ensure_ascii=False))
            replies = [(m.get("tool_call_id", ""), len(m["content"]), _block(m["content"]))
                       for m in body["messages"]
                       if m.get("role") == "tool" or str(m.get("content", "")).startswith(
                           "[Tool reply")]
            sizes.append((chars, replies))
            return script.pop(0)

        groq = llm.Groq()
        groq.model = lambda: MODEL
        groq.post = post
        turn = agent.answer(ws, [], QUESTION, lock=lambda: be._workspace(ws),
                            list_artifacts=lambda: be.list_artifacts(ws), providers=[groq])
        names = {f"c{i}": name for i, (name, _) in enumerate(CALLS, 1)}
        for n, (chars, replies) in enumerate(sizes, 1):
            tokens = -(-chars // 4)
            over = f"  OVER Groq's {LIMIT:,}" if tokens > LIMIT else ""
            print(f"request {n}: {chars:,} chars, ~{tokens:,} tokens (chars/4); "
                  f"{len(replies)} tool repl{'y' if len(replies) == 1 else 'ies'}{over}")
        print("tool replies as sent in the last request (characters, caveat lines):")
        for cid, length, block in sizes[-1][1]:
            print(f"  {cid} {names.get(cid, '?'):<20} {length:>6,} chars, {block:>2} caveat lines")
        print("\ntool replies as the tools wrote them (characters):")
        for call in turn.tool_calls:
            print(f"  {call.name:<20} {len(call.result):>6,} chars, {_block(call.result):>2} caveat "
                  f"lines ({_block_chars(call.result):,} chars){'  REFUSED' if call.refused else ''}")
        print(f"\nturn: {'ERROR ' + turn.error if turn.error else 'answered: ' + turn.reply}")
        for note in getattr(turn, "notes", []) or []:
            print(f"note: {note}")
        return 0
    finally:
        workspace.reset(ws)
        workspace.workspace_dir(ws).rmdir()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
