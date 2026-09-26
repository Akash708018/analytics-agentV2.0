"""The defects the cross-domain benchmark found (Phase 14 Step 13), one test each.

D1 an analysis over a text measure (count_distinct on a VARCHAR column, averaged by mix_shift)
   raised DuckDB's BinderException out of compute_analysis;
D2 hypothesis_test with two zero-variance groups raised ZeroDivisionError;
D3 a contract accepted a text column as a sum measure, and every analysis over it then raised;
D4 threads first using one workspace raced to create a log table (TransactionException);
D5 the Mann-Whitney tie term overflowed INT64 at 5M rows.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.analysis import inferential  # noqa: E402
from backend.engine.contract import store  # noqa: E402


def _block(text: str) -> str:
    return text.split("```json", 1)[1].split("```", 1)[0]


def _load(tmp_path, ws, name, header, rows):
    p = tmp_path / f"{name}.csv"
    p.write_text(header + "\n" + "\n".join(rows) + "\n")
    out = server.confirm_ingest_spec(spec_json=_block(server.propose_ingest_spec(
        path=str(p), dataset_name=name)), workspace_id=ws)
    assert out.startswith("Loaded"), out[:200]


def _contract(ws, name, **kw):
    out = server.propose_dataset_contract(dataset_name=name, workspace_id=ws, **kw)
    if out.startswith("BLOCKED"):
        return out
    return server.confirm_dataset_contract(contract_json=_block(out), workspace_id=ws)


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "EXPORT_DIR", tmp_path / "contracts")
    wid = "defects"
    workspace.reset(wid)
    yield wid
    workspace.reset(wid)
    workspace.workspace_dir(wid).rmdir()


ROWS = [f"r{i},2024-{1 + i % 12:02d}-05,{'ab'[i % 2]},c{i % 7},{i % 5}" for i in range(120)]


def test_d1_a_text_measure_in_mix_shift_is_a_refusal(tmp_path, ws):
    _load(tmp_path, ws, "t", "id,d,g,cust,v", ROWS)
    _contract(ws, "t", grain="row", primary_key=["id"], date_column="d",
              measures=["v", "cust"], dimensions=["g"],
              aggregations={"v": "sum", "cust": "count_distinct"},
              measure_definitions={"v": "v", "cust": "customers"},
              analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    out = server.compute_analysis(dataset_name="t", analysis_type="mix_shift", measure="cust",
                                  dimension="g", period="2024-12", baseline="2024-11",
                                  workspace_id=ws)
    assert out.startswith("BLOCKED") and "cust is VARCHAR" in out
    assert "reason: ANALYSIS_NOT_POSSIBLE" in out


def test_d2_zero_variance_groups_get_a_stated_no_test(tmp_path, ws):
    rows = [f"r{i},2024-01-05,{'ab'[i % 2]},{3 if i % 2 else 5}" for i in range(20)]
    _load(tmp_path, ws, "z", "id,d,g,v", rows)
    _contract(ws, "z", grain="row", primary_key=["id"], date_column="d", measures=["v"],
              dimensions=["g"], aggregations={"v": "sum"}, measure_definitions={"v": "v"},
              analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    for analysis in ("hypothesis_test", "effect_size"):
        out = server.compute_analysis(dataset_name="z", analysis_type=analysis, dimension="g",
                                      measure="v", workspace_id=ws)
        assert not out.startswith("BLOCKED"), out[:300]
        assert "zero variance" in out or "constant" in out, out[:600]
    rank = server.compute_analysis(dataset_name="z", analysis_type="hypothesis_test",
                                   dimension="g", measure="v", method="rank", workspace_id=ws)
    assert "Mann-Whitney" in rank or "No test" in rank


def test_d3_a_text_column_cannot_be_a_sum_measure(tmp_path, ws):
    rows = [f"r{i},2024-01-05,x,,{i}" for i in range(20)]       # v is empty: it loads as VARCHAR
    _load(tmp_path, ws, "n", "id,d,g,v,w", rows)
    out = _contract(ws, "n", grain="row", primary_key=["id"], date_column="d",
                    measures=["v", "w"], dimensions=["g"], aggregations={"v": "sum", "w": "sum"},
                    measure_definitions={"v": "v", "w": "w"},
                    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    assert out.startswith("BLOCKED") and "v is declared a measure with agg='sum'" in out
    assert "propose_cleaning_plan" in out
    ok = _contract(ws, "n", grain="row", primary_key=["id"], date_column="d",
                   measures=["v", "w"], dimensions=["g"],
                   aggregations={"v": "count", "w": "sum"},
                   measure_definitions={"v": "v", "w": "w"},
                   analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    assert "Contract stored" in ok, ok[:300]


def test_d4_a_concurrent_creator_does_not_make_the_create_raise(tmp_path):
    """Measured: while one connection's CREATE TABLE IF NOT EXISTS is uncommitted, a second
    connection's raises "Catalog write-write conflict" -- what ten threads hit in the benchmark.
    The first transaction commits 50 ms later; create_if_missing retries and gets through."""
    from backend.engine.util import db

    a = duckdb.connect(str(tmp_path / "x.duckdb"))
    b = a.cursor()
    ddl = "CREATE TABLE IF NOT EXISTS t (x INT)"
    a.execute("BEGIN")
    a.execute(ddl)
    with pytest.raises(duckdb.TransactionException):
        b.execute(ddl)                                   # the defect, as measured
    timer = threading.Timer(0.05, lambda: a.execute("COMMIT"))
    a.execute("ROLLBACK")
    a.execute("BEGIN")
    a.execute(ddl)
    timer.start()
    db.create_if_missing(b, ddl)                         # retries until the creator commits
    timer.join()
    assert b.execute("SELECT count(*) FROM t").fetchone() == (0,)


def test_d5_the_tie_term_does_not_overflow_at_millions_of_ties():
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT CASE WHEN i % 2 = 0 THEN 'a' ELSE 'b' END AS g, "
                "CASE WHEN i < 2200000 THEN 0.0 ELSE i::DOUBLE END AS v "
                "FROM range(2300000) r(i)")
    scope = SimpleNamespace(dataset_name="t", where="TRUE", source='"t"')
    result = inferential.mann_whitney(con, scope, "g", "v", "a", "b")
    assert 0.0 <= result.p <= 1.0


# --- next steps the benchmark could not run (Step 13 warnings: 64 unexecutable) -------------


def test_d6_a_key_repeated_only_by_duplicate_rows_points_to_the_cleaning_plan(tmp_path, ws):
    rows = ROWS + ROWS[:3]                                   # three exact duplicate rows
    _load(tmp_path, ws, "t", "id,d,g,cust,v", rows)
    out = _contract(ws, "t", grain="row", primary_key=["id"], date_column="d", measures=["v"],
                    dimensions=["g"], aggregations={"v": "sum"}, measure_definitions={"v": "v"},
                    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    assert out.startswith("BLOCKED") and "KEY_NOT_UNIQUE" in out
    assert 'NEXT STEP: call propose_cleaning_plan(dataset_name="t")' in out, out
    assert "3 row(s) repeat another row in every column" in out


def test_d6_a_key_repeated_by_distinct_rows_keeps_the_contract_call(tmp_path, ws):
    rows = ROWS + [ROWS[0].replace(",0", ",9", 1)[:-1] + "4"]  # same id, different values
    _load(tmp_path, ws, "t", "id,d,g,cust,v", rows)
    out = _contract(ws, "t", grain="row", primary_key=["id"], date_column="d", measures=["v"],
                    dimensions=["g"], aggregations={"v": "sum"}, measure_definitions={"v": "v"},
                    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    assert "KEY_NOT_UNIQUE" in out and "primary_key=[...]" in out, out


def test_d7_correlation_with_one_declared_measure_names_the_contract(tmp_path, ws):
    _load(tmp_path, ws, "t", "id,d,g,cust,v", ROWS)
    _contract(ws, "t", grain="row", primary_key=["id"], date_column="d", measures=["v"],
              dimensions=["g"],
              aggregations={"v": "sum"}, measure_definitions={"v": "v"},
              analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    out = server.compute_analysis(dataset_name="t", analysis_type="correlation", measure="v",
                                  workspace_id=ws)
    assert out.startswith("BLOCKED") and 'against="..."' not in out, out
    assert 'propose_dataset_contract(dataset_name="t")' in out


def test_d8_a_corrupt_workbook_is_not_sent_back_through_the_same_file(tmp_path):
    from backend.engine.ingest import excel
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"PK\x03\x04 not really a workbook")
    with pytest.raises(excel.LoadRefused) as e:
        excel.load_excel(duckdb.connect(), bad, "bad")
    assert f'propose_ingest_spec(path="{bad}")' not in str(e.value)
    assert "save it again as .xlsx" in str(e.value)


def test_d9_the_only_result_file_is_named_as_a_runnable_path(tmp_path, ws):
    from backend.engine.util import results
    d = results.results_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    (d / "only.csv").write_text("a\n1\n")
    out = server.read_result_file(path="/etc/passwd", workspace_id=ws)
    assert f'read_result_file(path="{d / "only.csv"}")' in out, out


def test_d10_a_many_series_chart_reply_stays_readable():
    from datetime import datetime

    from backend.engine.charts.render import SERIES_SHOWN, Chart
    names = [f"+{k}" for k in range(49)]
    c = Chart(path=Path("x.png"), kind="heatmap", label="cohort_retention",
              created_at=datetime(2026, 9, 24), x_name="cohort", series_names=names,
              point_count=1617, drawn_count=533,
              key_values=[f"{n}: lowest 1 at 2021-01-01, highest 18 at 2021-02-01, first 1, "
                          f"last 1, 33 point(s)" for n in names])
    text = c.to_text()
    assert len(text) < 3000, len(text)
    assert f"and {49 - SERIES_SHOWN} more" in text and "37 more series" in text


def test_d11_welch_df_is_a_number_and_a_narrow_last_bin_is_said(tmp_path, ws):
    rows = [f"r{i},2024-01-05,{'ab'[i % 2]},{i % 10 + (i % 2) * 3}" for i in range(40)]
    _load(tmp_path, ws, "w", "id,d,g,v", rows)
    _contract(ws, "w", grain="row", primary_key=["id"], date_column="d", measures=["v"],
              dimensions=["g"], aggregations={"v": "sum"}, measure_definitions={"v": "v"},
              analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")
    t = server.compute_analysis(dataset_name="w", analysis_type="hypothesis_test", dimension="g",
                                measure="v", workspace_id=ws)
    assert "df " in t and "e+" not in t.split("df ", 1)[1][:12], t[:600]
    from backend.engine.analysis import inferential as inf

    def g(n, mean, var):
        return inf.GroupStats(name="x", n=n, mean=mean, variance=var, skewness=0.0,
                              kurtosis=0.0)
    small = inf.welch(g(2, 1.5, 0.5), g(2, 4.0, 2.0))
    big = inf.welch(g(10**6, 1.0, 1.0), g(10**6, 1.1, 2.0))
    assert small.df == "1.471", small.df               # four significant figures below 1,000
    assert big.df == f"{float(big.df.replace(',', '')):,.1f}" and "e+" not in big.df, big.df
    d = server.compute_analysis(dataset_name="w", analysis_type="distribution", measure="v",
                                bins=5, workspace_id=ws)
    # v spans 0..12, 13 values: 3-wide bins make 5, the last 1 value wide
    assert "The last bin is narrower: 1 value(s) wide against 3" in d, d[:1500]


def test_d12_threads_parsing_predicates_get_their_own_trees():
    """Ten threads scoping one workspace at once raised IndexError from the shared parser
    connection in sql_guard (Step 13 benchmark, H_concurrency after D4 was out of the way)."""
    from backend.engine.util import sql_guard

    errors, wrong = [], []

    def work(k):
        for i in range(200):
            want = f"col_{k}_{i}"
            try:
                tree = sql_guard._ast(f"SELECT 1 WHERE {want} > {i}")
            except Exception as exc:  # noqa: BLE001 - the defect is any exception here
                errors.append(repr(exc))
                return
            if want not in json.dumps(tree):
                wrong.append(want)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and not wrong, (errors[:3], wrong[:3])


def test_d13_a_page_cap_is_stated(tmp_path, ws):
    from backend.engine.util import results
    d = results.results_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "many.csv"
    f.write_text("k,v\n" + "".join(f"k{i},{i}\n" for i in range(984)))
    out = server.read_result_file(path=str(f), start=901, limit=200, workspace_id=ws)
    assert "rows 901 to 950 of 984" in out
    assert "limit=200 was asked; a page holds at most 50 rows" in out
    assert "34 rows after this page" in out and "start=951, limit=50" in out
    plain = server.read_result_file(path=str(f), start=1, limit=10, workspace_id=ws)
    assert "was asked" not in plain                     # control: a limit within the cap


def test_d14_deduplication_keeps_file_order_so_sums_repeat(tmp_path, ws):
    """Two identical runs printed one total two ways: DISTINCT reordered the rows and a DOUBLE
    sum depends on order. The rebuild now keeps first occurrences in file order."""
    import random
    rng = random.Random(5)
    rows = [f"r{i},2024-{1 + i % 12:02d}-05,{'ab'[i % 2]},"
            f"{rng.uniform(0, 1e6) * (1e3 if i % 997 == 0 else 1):.4f}" for i in range(150_000)]
    rows += rows[:500]                                      # exact duplicates to drop
    totals, orders = set(), set()
    for k in range(3):
        name = f"s{k}"
        _load(tmp_path, ws, name, "id,d,g,v", rows)
        server.propose_cleaning_plan(dataset_name=name, workspace_id=ws)
        out = server.apply_cleaning_plan(dataset_name=name, approved_action_ids=["C001"],
                                         workspace_id=ws)
        assert "500 row(s) removed" in out, out[:300]
        con = duckdb.connect(str(workspace.duckdb_path(ws)), read_only=True)
        totals.add(con.execute(f'SELECT sum(v)::VARCHAR FROM "{name}"').fetchone()[0])
        orders.add(tuple(r[0] for r in con.execute(f'SELECT id FROM "{name}" LIMIT 1000')
                         .fetchall()))
        first = con.execute(f'SELECT id FROM "{name}" LIMIT 3').fetchall()
        con.close()
        assert first == [("r0",), ("r1",), ("r2",)]          # file order kept
    assert len(totals) == 1 and len(orders) == 1, (totals, len(orders))


def test_d16_concurrent_results_never_share_a_file(tmp_path, ws):
    """Ten threads of one workspace writing a result in the same second each picked a name that
    did not exist yet and then opened it for writing: two could take one name, and one dataset's
    file then held another's rows (Step 14, H_concurrency's oracle error)."""
    from datetime import datetime

    from backend.engine.util import results
    now = datetime(2026, 9, 24, 23, 10, 51)
    barrier = threading.Barrier(24)
    got = [None] * 24

    def work(k):
        barrier.wait()
        got[k] = results.write_result(ws, label="top_n", headers=["who"], rows=[[f"t{k}"]] * 3,
                                      now=now)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    paths = [r.path for r in got]
    assert len(set(paths)) == 24, sorted(p.name for p in paths)
    for k, r in enumerate(got):
        assert r.path.read_text().splitlines()[1:] == [f"t{k}"] * 3, (k, r.path.name)


def test_d15_a_plan_proposed_twice_quotes_the_same_examples(tmp_path, ws):
    """Sample queries ended in LIMIT with no ORDER BY: on a table scanned in parallel, one plan
    proposed twice quoted different example values (Step 14 regression, CLEANING)."""
    rows = [f"r{i},2024-{1 + i % 12:02d}-05,{['Card', 'card', 'CARD', 'Wire', 'wire'][i % 5]},"
            f"{i % 97}" for i in range(300_000)]
    rows += rows[:4000]                                    # duplicates to sample
    texts = []
    for k in range(2):
        _load(tmp_path, ws, f"p{k}", "id,d,pay,v", rows)
        out = server.propose_cleaning_plan(dataset_name=f"p{k}", workspace_id=ws)
        texts.append(out.replace(f"p{k}", "P"))
    body = [t.split("NEXT STEP", 1)[0] for t in texts]
    assert "DROP_DUPLICATE_ROWS" in body[0] and "NORMALISE_CASE" in body[0], body[0][:800]
    assert body[0] == body[1]


def test_d17_a_long_calendar_is_labelled_sparsely_and_drawn_fast(tmp_path, ws):
    """Keeping every empty period gave 1800-2999 a tick per month: 14,400 labels, 98.8 s to draw
    14 points (stress matrix after the merge of 25/09/2026). The slots stay; the labels thin."""
    import time

    from backend.engine.charts import render
    at = [float(i) for i in range(14_400)]
    ticks, names = render._tick_subset(at, [f"p{i}" for i in range(14_400)])
    assert len(ticks) <= render.MAX_TICKS and ticks[0] == 0.0 and ticks[-1] == 14_399.0
    assert names[0] == "p0" and names[-1] == "p14399"
    assert render._tick_subset([0.0, 1.0], ["a", "b"]) == ([0.0, 1.0], ["a", "b"])

    rows = [f"r{i},{'1800-03-05' if i == 0 else '2999-11-20' if i == 1 else f'2024-{1 + i % 12:02d}-05'},"
            f"{i + 1}" for i in range(60)]
    _load(tmp_path, ws, "ex", "id,d,v", rows)
    _contract(ws, "ex", grain="row", primary_key=["id"], date_column="d", measures=["v"],
              dimensions=[], aggregations={"v": "sum"}, measure_definitions={"v": "v"},
              analysis_window_start="1800-01-01", analysis_window_end="2999-12-31")
    t0 = time.perf_counter()
    out = server.render_chart(dataset_name="ex", analysis_type="trend", chart="line",
                              measure="v", grain="month", workspace_id=ws)
    assert "of 14,400 point(s) drawn" in out, out[:400]
    assert time.perf_counter() - t0 < 15, "a long calendar must not take a minute to draw"
