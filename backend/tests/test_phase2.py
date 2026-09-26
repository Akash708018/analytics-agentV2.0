"""Phase 2 acceptance test -- the Done-When clauses, run end to end.

Step 9. Run from the repo root:

    uv run python tests/test_phase2.py

Checks, from the build guide's Phase 2 Done-When:

  1. A Postgres table loads from the olist source
  2. clean_sales.csv loads
  3. multiheader.csv loads, and its numeric columns are BIGINT, NOT VARCHAR
  4. A clean xlsx loads
  5. describe_dataset is correct for all four
  6. Two server processes with different workspace ids run concurrently
     without a lock error

Exits 0 on success, 1 on any failure. Nothing here is mocked: it uses the real
fixtures, the real DuckDB file and the real Postgres server.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace                      # noqa: E402
from backend.engine.ingest import csv_loader, excel, postgres  # noqa: E402
from backend.engine.ingest.csv_loader import LoadRefused  # noqa: E402
from backend.engine.util import db                        # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
WS = "phase2_test"
NAMES = ["order_id", "order_date", "region", "product",
         "channel", "units", "unit_price", "revenue"]

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def main() -> int:
    print(f"Phase 2 acceptance test -- workspace '{WS}'")

    workspace.reset(WS)
    con = db.connect(WS)

    # -- 2. CSV ------------------------------------------------------------
    section("Clause 2: clean_sales.csv loads")
    r = csv_loader.load_csv(con, FIXTURES / "clean_sales.csv", "clean_sales")
    check("500 rows", r.row_count == 500, f"got {r.row_count}")
    check("8 columns", r.column_count == 8, f"got {r.column_count}")
    check("order_date is DATE", dict(r.columns)["order_date"] == "DATE",
          f"got {dict(r.columns)['order_date']}")

    # -- 3. THE REGRESSION -------------------------------------------------
    section("Clause 3: multiheader.csv loads as BIGINT, not VARCHAR")
    r = csv_loader.load_csv(con, FIXTURES / "multiheader.csv", "multi",
                            header_rows=2, names=NAMES)
    types = dict(r.columns)
    check("300 rows", r.row_count == 300, f"got {r.row_count}")
    check("units is BIGINT", types["units"] == "BIGINT",
          f"got {types['units']} -- header row read as data (F5)")
    check("unit_price is DOUBLE", types["unit_price"] == "DOUBLE",
          f"got {types['unit_price']}")
    check("revenue is DOUBLE", types["revenue"] == "DOUBLE",
          f"got {types['revenue']}")
    first = con.execute(
        "SELECT order_id, units, revenue FROM multi LIMIT 1"
    ).fetchone()
    check("first row intact", first == ("ORD-00001", 39, 4133.22), f"got {first}")

    # -- 4. Excel ----------------------------------------------------------
    section("Clause 4: clean xlsx loads")
    r = excel.load_excel(con, FIXTURES / "clean_sales.xlsx", "xl_sales")
    check("400 rows", r.row_count == 400, f"got {r.row_count}")
    check("units is BIGINT", dict(r.columns)["units"] == "BIGINT",
          f"got {dict(r.columns)['units']}")

    # -- 1. Postgres -------------------------------------------------------
    section("Clause 1: a Postgres table loads from 'olist'")
    try:
        r = postgres.load_table(con, "olist", "orders", "pg_orders",
                                where="order_status = 'delivered'")
        check("rows copied", r.row_count > 0, f"got {r.row_count}")
        check("registered as postgres",
              db.get_dataset(con, "pg_orders").source_type == "postgres")
        alias = postgres.attach(con, "olist")
        n = con.execute(f"SELECT count(*) FROM {alias}.public.orders").fetchone()[0]
        check("in-place query works", n == 99441, f"got {n:,}, expected 99,441")
    except LoadRefused as exc:
        check("Postgres source reachable", False, str(exc).splitlines()[0])

    # -- 5. describe_dataset -----------------------------------------------
    section("Clause 5: describe_dataset correct for all four")
    from backend.engine import server

    for name, expect_rows in [("clean_sales", 500), ("multi", 300),
                              ("xl_sales", 400), ("pg_orders", None)]:
        if name not in db.user_tables(con):
            check(f"{name} present", False, "not loaded")
            continue
        out = server.describe_dataset(name, workspace_id=WS)
        ok = (f"# {name}" in out
              and "| column | type | nulls | distinct |" in out
              and "Sample" in out)
        if expect_rows is not None:
            ok = ok and f"{expect_rows:,} rows" in out
        check(f"{name} described", ok, out[:160])

    unknown = server.describe_dataset("nope", workspace_id=WS)
    check("unknown dataset refuses", unknown.startswith("BLOCKED:"))

    con.close()

    # -- 6. Concurrency ----------------------------------------------------
    section("Clause 6: two workspaces concurrently, no lock error")
    holder = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from backend.engine.util import db
        con = db.connect("{WS}")
        con.execute("CREATE TABLE IF NOT EXISTS held AS SELECT 1")
        print("holding", flush=True)
        time.sleep(8)
    """)
    Path("/tmp/_p2_holder.py").write_text(holder)

    proc = subprocess.Popen([sys.executable, "/tmp/_p2_holder.py"],
                            stdout=subprocess.PIPE, text=True)
    try:
        proc.stdout.readline()

        try:
            c2 = db.connect(WS)
            c2.execute("SELECT 1")
            c2.close()
            check("same workspace is locked", False,
                  "a second process connected -- isolation is not real")
        except Exception as exc:
            check("same workspace is locked", "lock" in str(exc).lower(),
                  f"got {type(exc).__name__}")

        try:
            c3 = db.connect(f"{WS}_b")
            c3.execute("SELECT 1")
            c3.close()
            check("different workspace connects", True)
        except Exception as exc:
            check("different workspace connects", False, str(exc)[:120])
    finally:
        proc.terminate()
        proc.wait()
        workspace.reset(f"{WS}_b")

    # -- Summary -----------------------------------------------------------
    print(f"\n{'=' * 46}")
    print(f"  {_passed} passed, {_failed} failed")
    print("=" * 46)
    if _failed == 0:
        print("\nPhase 2 Done-When: ALL CLAUSES PASS")
        print("Clean up with: reset_workspace, or workspace.reset('%s')" % WS)
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
