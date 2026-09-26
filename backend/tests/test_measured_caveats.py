"""contract/measured_caveats.py: caveats counted from the table, not typed (25/09/2026).

    uv run pytest tests/test_measured_caveats.py -q
"""

from __future__ import annotations

import json

import duckdb
import pytest

from backend.engine.contract import measured_caveats as mc
from backend.engine.contract.evidence import gather, suggest_role


TABLE = """
      CREATE TABLE t AS
      SELECT i AS id,
             CASE WHEN i % 10 = 0 THEN 'unknown' ELSE CAST(20 + i % 50 AS VARCHAR) END AS age,
             CASE WHEN i % 3 = 0 THEN 'Store' ELSE 'Online' END AS channel,
             CASE WHEN i % 3 = 0 THEN NULL ELSE 'Ekart' END AS courier,
             CASE WHEN i % 3 = 0 THEN NULL ELSE 12.0 END AS session_seconds,
             CASE WHEN i IN (5, 7) THEN -1 ELSE 1 + i % 4 END AS units,
             CASE WHEN i % 25 = 0 THEN '01/12/2024' ELSE '2024-02-03' END AS delivery_date,
             DATE '2024-01-01' + INTERVAL (CASE WHEN i < 100 THEN i ELSE i + 80 END) DAY
               AS order_date,
             CASE WHEN i = 199 THEN 'Total' ELSE 'North' END AS region
      FROM range(200) t(i)"""


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    c.execute(TABLE)
    yield c
    c.close()


def lines(con):
    ev = gather(con, "t")
    roles = {c.name: suggest_role(c)[0] for c in ev.columns}
    return mc.measure(con, "t", ev, date_column="order_date", roles=roles)


def test_every_rule_counts_what_it_names(con):
    got = "\n".join(lines(con))
    assert "age holds a placeholder for a missing value: 'unknown' in 20 row(s)" in got
    assert ("courier and session_seconds are blank in the same 67 row(s) (33.5%) -- exactly the "
            "rows where channel = 'Store'.") in got
    assert "units is below zero in 2 row(s) (as low as -1)" in got
    assert "delivery_date is text: 192 value(s) read as dates and 8 are written another way" in got
    assert "order_date has no rows in 2024-05" in got
    assert "region holds 'Total' in 1 row(s)" in got


def test_a_clean_table_has_nothing_to_say():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE t AS SELECT i AS id, i * 2.0 AS amount FROM range(50) t(i)")
    ev = gather(c, "t")
    assert mc.measure(c, "t", ev) == []


def test_confirmation_recounts_whatever_the_payload_says(con):
    from backend.engine.contract import store
    from backend.engine.contract.propose import propose_contract
    from backend.engine.contract.tools import confirm
    import tempfile
    p = propose_contract(con, "t", grain="one row = one test", primary_key=["id"],
                         date_column="order_date", measures=["units"],
                         aggregations={"units": "sum"}, measure_definitions={"units": "items"},
                         analysis_window=(__import__("datetime").date(2024, 1, 1),
                                          __import__("datetime").date(2024, 12, 31)))
    assert p.contract.measured_caveats, "a proposal carries the counts"
    payload = json.loads(p.contract.model_dump_json())
    payload["measured_caveats"] = ["age holds 'unknown' in 3,470 rows"]
    with tempfile.TemporaryDirectory() as root:
        confirm(con, json.dumps(payload), export_root=root)
    stored = store.current(con, "t").contract
    assert stored.measured_caveats == p.contract.measured_caveats
    assert not any("3,470" in c for c in stored.measured_caveats)


def test_the_gate_prints_them_as_measured_and_before_the_declared():
    import datetime
    import tempfile
    from backend.engine import workspace
    from backend.engine.contract.propose import propose_contract
    from backend.engine.contract.tools import confirm
    from backend.engine.state import DECLARED, MEASURED, require_contract
    from backend.engine.util import db
    ws = "test_measured_caveats"
    workspace.reset(ws)
    con = db.connect(ws)
    try:
        con.execute(TABLE)
        shape = db.table_shape(con, "t")
        db.register_dataset(con, "t", source_type="csv", source_detail="/tmp/t.csv",
                            row_count=shape[0], column_count=shape[1])
        p = propose_contract(con, "t", grain="one row = one test", primary_key=["id"],
                             date_column="order_date", measures=["units"],
                             aggregations={"units": "sum"},
                             measure_definitions={"units": "items"}, caveats=["a note"],
                             analysis_window=(datetime.date(2024, 1, 1),
                                              datetime.date(2024, 12, 31)))
        with tempfile.TemporaryDirectory() as root:
            confirm(con, p.contract.model_dump_json(), export_root=root)
        got = require_contract(con, "t").caveats
    finally:
        con.close()
        workspace.reset(ws)
        workspace.workspace_dir(ws).rmdir()
    measured = [c for c in got if c.startswith(MEASURED)]
    assert measured and any("'unknown' in 20 row(s)" in c for c in measured)
    assert got.index(measured[-1]) < got.index(f"{DECLARED}a note")


def test_coordinates_and_spellings():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE g AS SELECT i AS id,
        CASE WHEN i = 1 THEN 95.0 WHEN i = 2 THEN 0 ELSE 18.5 END AS drop_lat,
        CASE WHEN i = 2 THEN 0 WHEN i = 4 THEN 200.0 ELSE 73.8 END AS drop_lng,
        CASE WHEN i % 3 = 0 THEN 'Pune_West' WHEN i % 7 = 0 THEN 'PUNE_WEST' ELSE 'Pune_East' END
            AS zone
        FROM range(100) t(i)""")
    ev = gather(c, "g")
    got = "\n".join(mc.measure(c, "g", ev, roles={x.name: suggest_role(x)[0]
                                                   for x in ev.columns}))
    assert "drop_lat is outside [-90, 90] in 1 row(s)" in got
    assert "drop_lng is outside [-180, 180] in 1 row(s)" in got
    assert "drop_lat and drop_lng look swapped in 1 row(s)" in got
    assert "drop_lat, drop_lng is (0, 0) in 1 row(s)" in got
    assert "zone writes 1 value(s) more than one way ('PUNE_WEST' / 'Pune_West'" in got


def test_ga_placeholders_and_rates_over_100():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE s AS SELECT i AS id,
        CASE WHEN i % 20 = 0 THEN '(not set)' WHEN i % 7 = 0 THEN '(direct)' ELSE 'google' END
            AS source,
        CASE WHEN i % 7 = 0 THEN '(none)' ELSE 'cpc' END AS medium,
        CASE WHEN i % 4 = 0 THEN '(not provided)' ELSE 'kw' END AS keyword,
        CASE WHEN i = 13 THEN 104.2 ELSE 2.5 + (i % 10) / 10.0 END AS ctr_pct
        FROM range(400) t(i)""")
    ev = gather(c, "s")
    got = "\n".join(mc.measure(c, "s", ev, roles={x.name: suggest_role(x)[0]
                                                   for x in ev.columns}))
    assert "source holds a placeholder for a missing value: '(not set)' in 20 row(s)" in got
    assert "keyword holds a placeholder for a missing value: '(not provided)' in 100 row(s)" in got
    assert "(direct)" not in got and "(none)" not in got, "real GA values are not missing"
    assert "ctr_pct is above 100 in 1 row(s) (as high as 104.2)" in got


def test_marketing_v2_caveats():
    """Split entities, future dates, mixed currencies, dates out of order, and a value where a
    flag says there should be none (scripts/marketing_bench_v2.py, 25/09/2026)."""
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE f AS SELECT 'LD' || i AS lead_id, 'U' || (i // 2) AS user_id,
        CASE WHEN i % 2 = 0 THEN 'control' WHEN i IN (1, 3, 5) THEN 'control' ELSE 'treatment'
            END AS variant,
        TIMESTAMP '2024-01-01 09:00:00' + INTERVAL (i % 90) DAY AS created_at,
        CASE WHEN i % 199 = 4 THEN TIMESTAMP '2024-01-01 09:00:00' + INTERVAL (i % 90) DAY
                                  - INTERVAL 1 DAY
             ELSE TIMESTAMP '2024-01-01 09:00:00' + INTERVAL (i % 90) DAY + INTERVAL 2 DAY
             END AS qualified_at,
        CASE WHEN i = 7 THEN DATE '2031-02-01' ELSE DATE '2024-02-01' END AS order_date,
        CASE i % 3 WHEN 0 THEN 'INR' WHEN 1 THEN 'USD' ELSE 'AED' END AS currency,
        100.0 + i AS amount_local,
        CASE WHEN i % 4 = 0 THEN 1 ELSE 0 END AS is_won,
        CASE WHEN i % 4 = 0 THEN 900.0 + i WHEN i IN (1, 2) THEN 50.0 END AS deal_amount
        FROM range(400) t(i)""")
    ev = gather(c, "f")
    got = "\n".join(mc.measure(c, "f", ev, roles={x.name: suggest_role(x)[0]
                                                   for x in ev.columns}))
    assert "user_id falls under more than one variant for 197 value(s)" not in got, \
        "most users are split here: not an exception, so not said"
    assert "qualified_at is before created_at in 2 row(s)" in got, "0.5%: an exception"
    assert "order_date is in the future in 1 row(s) (as late as 2031-02-01)" in got
    assert "amount_local mixes 3 currencies (currency: AED, INR, USD)" in got
    assert "deal_amount holds a value in 2 row(s) where is_won = 0" in got


def test_a_few_users_in_both_variants_are_said():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE x AS
        SELECT 'U' || i AS user_id, CASE WHEN i % 2 = 0 THEN 'control' ELSE 'treatment' END
               AS variant FROM range(1000) t(i)
        UNION ALL SELECT 'U' || i, 'treatment' FROM range(0, 1000, 100) t(i)""")
    ev = gather(c, "x")
    got = "\n".join(mc.measure(c, "x", ev, roles={x.name: suggest_role(x)[0]
                                                   for x in ev.columns}))
    assert "user_id falls under more than one variant for 10 value(s)" in got
