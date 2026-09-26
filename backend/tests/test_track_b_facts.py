"""Track B ground facts: concurrency, file locks, and the HTTP session id (Phase 14 Step 1).

Pinned before any Track B code depends on them, as every phase has opened. Streamlit serves each
browser session as a thread in one process; util/db.connect opens a handle per call; Claude
Desktop's Track A server is a second process. What DuckDB and FastMCP do in each of those
situations is measured here. Predictions are in docs/steps/phase14_step1_ground_facts.md.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import duckdb
import pytest
from fastmcp import Client, Context, FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.util import db  # noqa: E402

A, B = "trackb_facts_a", "trackb_facts_b"


@pytest.fixture()
def two_workspaces():
    for w in (A, B):
        workspace.reset(w)
    yield
    for w in (A, B):
        workspace.reset(w)


# --- one process ------------------------------------------------------------------------------

def test_f1_two_handles_on_one_file_in_one_process_share_it(two_workspaces):
    first, second = db.connect(A), db.connect(A)
    try:
        first.execute("CREATE TABLE t AS SELECT 42 AS x")
        assert second.execute("SELECT x FROM t").fetchone() == (42,)
    finally:
        first.close()
        second.close()


def test_f2_read_only_attach_beside_an_open_handle_raises(two_workspaces):
    rw = db.connect(A)
    try:
        with pytest.raises(duckdb.Error) as exc:
            db.connect_read_only(A).close()
        print("F2:", type(exc.value).__name__, str(exc.value).splitlines()[0])
    finally:
        rw.close()


def test_f5_two_threads_two_workspaces_do_not_touch(two_workspaces):
    errors: list[BaseException] = []
    seen: dict[str, set] = {A: set(), B: set()}
    start = threading.Barrier(2)

    def work(ws: str, value: int) -> None:
        try:
            start.wait()
            for i in range(25):
                con = db.connect(ws)
                try:
                    con.execute(f"CREATE OR REPLACE TABLE t AS SELECT {value} AS v, {i} AS i")
                    seen[ws].add(con.execute("SELECT v FROM t").fetchone()[0])
                finally:
                    con.close()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(A, 1)), threading.Thread(target=work, args=(B, 2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert seen == {A: {1}, B: {2}}


def test_f6_the_second_writer_to_one_table_is_refused_at_its_write_and_only_it_aborts(
        two_workspaces):
    """Predicted: the second COMMIT raises. Measured: the second writer's CREATE raises at once,
    its transaction is aborted until ROLLBACK, and the first commits untouched (P14-D6)."""
    first, second = db.connect(A), db.connect(A)
    try:
        first.execute("CREATE TABLE t AS SELECT 0 AS v")
        first.execute("BEGIN")
        second.execute("BEGIN")
        first.execute("CREATE OR REPLACE TABLE t AS SELECT 1 AS v")
        with pytest.raises(duckdb.TransactionException, match="write-write conflict"):
            second.execute("CREATE OR REPLACE TABLE t AS SELECT 2 AS v")
        with pytest.raises(duckdb.TransactionException, match="transaction is aborted"):
            second.execute("SELECT 1")
        first.execute("COMMIT")
        second.execute("ROLLBACK")
        assert second.execute("SELECT v FROM t").fetchone() == (1,)
    finally:
        for c in (first, second):
            try:
                c.execute("ROLLBACK")
            except duckdb.Error:
                pass
            c.close()


# --- two processes ----------------------------------------------------------------------------

_HOLDER = textwrap.dedent("""
    import sys, time, duckdb
    con = duckdb.connect(sys.argv[1])
    print("held", flush=True)
    sys.stdin.readline()
    con.close()
    print("released", flush=True)
    sys.stdin.readline()
""")


def test_f3_f4_a_second_process_is_locked_out_until_the_first_closes(two_workspaces):
    path = str(workspace.duckdb_path(A))
    db.connect(A).close()  # create the file with the schema, then let go
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, path], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(duckdb.IOException) as exc:
            duckdb.connect(path).close()
        assert "Could not set lock on file" in str(exc.value)
        print("F3:", str(exc.value).splitlines()[0][:160])

        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "released"
        duckdb.connect(path).close()  # F4: no exception
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)


# --- the HTTP session id ----------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_f7_each_http_client_has_its_own_stable_session_id():
    app = FastMCP("facts")

    @app.tool
    def whoami(ctx: Context) -> str:
        return ctx.session_id

    port = _free_port()
    server = threading.Thread(
        target=lambda: asyncio.run(app.run_http_async(host="127.0.0.1", port=port,
                                                      show_banner=False, log_level="error")),
        daemon=True)
    server.start()
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)

    async def ids() -> tuple[str, str, str]:
        async with Client(url) as one, Client(url) as two:
            a1 = (await one.call_tool("whoami", {})).data
            a2 = (await one.call_tool("whoami", {})).data
            b1 = (await two.call_tool("whoami", {})).data
            return a1, a2, b1

    a1, a2, b1 = asyncio.run(ids())
    print("F7:", a1, a2, b1)
    assert a1 == a2
    assert a1 != b1
