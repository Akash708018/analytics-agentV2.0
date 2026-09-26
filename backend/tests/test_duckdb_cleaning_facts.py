"""DuckDB behaviours Phase 6 is built on, pinned so a version bump says so.

None of these is a test of our code. Each one is a claim about DuckDB that the
cleaning design depends on, measured on 1.5.5 rather than assumed, and left
here so that the day 1.6 changes one of them the suite says which one.

The four claims:

  1. A genuinely read-only handle on the workspace file is reachable ONLY by
     ATTACH ... (READ_ONLY) from a separate connection, and only when nothing
     else holds the file. connect(read_only=True) and cursor() do not give one.
  2. That handle refuses CREATE by STATEMENT TYPE, so `CREATE TABLE IF NOT
     EXISTS` is refused even where it would be a no-op -- which is what
     util/db.connect runs on every open.
  3. `* REPLACE (expr AS col)` preserves column position. `* EXCLUDE (col),
     expr AS col` moves the column to the end -- and column ORDER is identity
     to Phase 4's Binding.fingerprint.
  4. A hard CAST inside a CTAS aborts the whole rebuild on one bad value.
     TRY_CAST nulls it instead, which is a silent drop. Neither is acceptable
     unaudited, so the count comes first.
"""

from __future__ import annotations

import duckdb
import pytest

# The expression the build guide gives as its worked example, kept verbatim so
# that what is pinned here is the thing that was actually proposed.
STRIP = "REPLACE(REPLACE(amount,'$',''),',','')"


@pytest.fixture()
def wsfile(tmp_path):
    """A workspace-shaped DuckDB file on disk, with one table in it."""
    path = tmp_path / "ws.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t AS SELECT i AS id, 'x' AS s FROM range(5) r(i)")
    con.close()
    return str(path)


def columns(con, table: str) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{table}' ORDER BY ordinal_position"
        ).fetchall()
    ]


# --------------------------------------------------------------------------
# 1. what a read-only proposal connection actually costs
# --------------------------------------------------------------------------


def test_connect_read_only_conflicts_with_an_open_writer(wsfile):
    """duckdb.connect(read_only=True) is NOT a per-connection permission.

    Access mode is a property of the open database, so asking for a read-only
    view of a file another connection already holds read-write is a
    configuration conflict, not a downgrade.
    """
    writer = duckdb.connect(wsfile)
    try:
        with pytest.raises(duckdb.ConnectionException) as exc:
            duckdb.connect(wsfile, read_only=True)
        assert "different configuration" in str(exc.value)
    finally:
        writer.close()


def test_cursor_is_not_a_sandbox(wsfile):
    """con.cursor() shares the database. It can write. It is not a guard."""
    writer = duckdb.connect(wsfile)
    try:
        child = writer.cursor()
        child.execute("CREATE TABLE written_by_the_child AS SELECT 1 AS a")
        assert "written_by_the_child" in [
            r[0] for r in writer.execute("SHOW TABLES").fetchall()
        ]
    finally:
        writer.close()


def test_access_mode_cannot_be_lowered_at_runtime(wsfile):
    """There is no SET that turns a live read-write connection read-only."""
    writer = duckdb.connect(wsfile)
    try:
        with pytest.raises(duckdb.InvalidInputException) as exc:
            writer.execute("SET access_mode='READ_ONLY'")
        assert "while database is running" in str(exc.value)

        with pytest.raises(duckdb.CatalogException):
            writer.execute("SET SESSION access_mode='READ_ONLY'")
    finally:
        writer.close()


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE ws.evil AS SELECT 1 AS a",
        "INSERT INTO ws.t VALUES (99, 'z')",
        "UPDATE ws.t SET s = 'y'",
        "DROP TABLE ws.t",
    ],
)
def test_attach_read_only_refuses_every_kind_of_write(wsfile, statement):
    """ATTACH (READ_ONLY) IS a real guard, enforced by the engine.

    This is the one route to the Phase 6 hard requirement: the proposal path
    cannot write even if the code is wrong.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{wsfile}' AS ws (READ_ONLY)")
        assert con.execute("SELECT count(*) FROM ws.t").fetchone()[0] == 5
        with pytest.raises(duckdb.InvalidInputException) as exc:
            con.execute(statement)
        assert 'on database "ws"' in str(exc.value)
    finally:
        con.close()


def test_the_read_only_attach_still_needs_the_file_to_itself(wsfile):
    """The guard is available only when nothing else holds the file.

    So propose_cleaning_plan cannot run alongside a long-lived writer in the
    same process. Whether that is true of util/db.connect is the question
    Step 1 sends you to look up, and it decides the shape of the tool layer.
    """
    writer = duckdb.connect(wsfile)
    try:
        con = duckdb.connect(":memory:")
        with pytest.raises(duckdb.BinderException) as exc:
            con.execute(f"ATTACH '{wsfile}' AS ws (READ_ONLY)")
        assert "already attached" in str(exc.value)
        con.close()
    finally:
        writer.close()


def test_read_only_refuses_create_even_when_the_table_already_exists(wsfile):
    """The statement TYPE is refused, not its effect.

    This is the one that decides Step 3's shape. util/db.connect runs
    `CREATE TABLE IF NOT EXISTS _agent_datasets` on every open, which is a
    no-op against an existing table and is still refused here. So the proposal
    path cannot go through db.connect at all -- it needs its own opener.
    """
    setup = duckdb.connect(wsfile)
    setup.execute(
        "CREATE TABLE IF NOT EXISTS _agent_datasets (dataset_name VARCHAR PRIMARY KEY)"
    )
    setup.close()

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{wsfile}' AS ws (READ_ONLY)")
        with pytest.raises(duckdb.InvalidInputException) as exc:
            con.execute(
                "CREATE TABLE IF NOT EXISTS ws._agent_datasets "
                "(dataset_name VARCHAR PRIMARY KEY)"
            )
        assert 'type "CREATE"' in str(exc.value)
        assert con.execute("SELECT count(*) FROM ws.t").fetchone()[0] == 5
    finally:
        con.close()


def test_a_read_only_attach_can_still_write_to_its_own_scratch():
    """Read-only is on the attached database, not on the connection.

    So the proposal path can still build temp tables to count with; it just
    cannot touch the workspace.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE scratch AS SELECT 1 AS a")
        assert con.execute("SELECT a FROM scratch").fetchone()[0] == 1
    finally:
        con.close()


# --------------------------------------------------------------------------
# 2. how a rebuilt column is put back
# --------------------------------------------------------------------------


@pytest.fixture()
def sales():
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE sales AS SELECT * FROM (VALUES
          ('ORD-1', 'North',  '$1,200.50', '2024-01-03'),
          ('ORD-2', 'north ', '$980',      '2024-01-04'),
          ('ORD-3', 'NORTH',  '(45.00)',   '2024-01-05')
        ) t(order_id, region, amount, order_date)
        """
    )
    yield con
    con.close()


def test_exclude_moves_the_rebuilt_column_to_the_end(sales):
    """The build guide's own example silently reorders the table.

    Harmless to a human, not harmless to Phase 4: Binding.fingerprint is taken
    over the ORDERED column names and types, so a value-level conversion reads
    as a structural change to the contract.
    """
    sales.execute(
        f"CREATE TABLE v_exclude AS SELECT * EXCLUDE (amount), "
        f"TRY_CAST({STRIP} AS DECIMAL(18,2)) AS amount FROM sales"
    )
    assert columns(sales, "sales") == ["order_id", "region", "amount", "order_date"]
    assert columns(sales, "v_exclude") == [
        "order_id",
        "region",
        "order_date",
        "amount",
    ]


def test_replace_preserves_position_and_changes_the_type(sales):
    """`* REPLACE` is the form Phase 6 uses. Same rebuild, same row order."""
    sales.execute(
        f"CREATE TABLE v_replace AS SELECT * REPLACE ("
        f"TRY_CAST({STRIP} AS DECIMAL(18,2)) AS amount) FROM sales"
    )
    assert columns(sales, "v_replace") == columns(sales, "sales")
    types = dict(
        sales.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'v_replace'"
        ).fetchall()
    )
    assert types["amount"] == "DECIMAL(18,2)"
    assert types["order_date"] == "VARCHAR"  # untouched columns stay untouched


# --------------------------------------------------------------------------
# 3. the two ways a conversion goes wrong
# --------------------------------------------------------------------------


def test_hard_cast_aborts_the_whole_rebuild_on_one_bad_value(sales):
    """One accounting-style negative kills the CTAS. Nothing is written.

    This is the safe failure of the two, and it is still not acceptable
    unannounced: the person approved a conversion and got an exception.
    """
    with pytest.raises(duckdb.ConversionException) as exc:
        sales.execute(
            f"CREATE TABLE v_hard AS SELECT * REPLACE ("
            f"CAST({STRIP} AS DECIMAL(18,2)) AS amount) FROM sales"
        )
    assert "(45.00)" in str(exc.value)
    assert "v_hard" not in [r[0] for r in sales.execute("SHOW TABLES").fetchall()]


def test_try_cast_converts_the_bad_value_to_null_silently(sales):
    """The dangerous failure. A value becomes absent and nothing says so."""
    sales.execute(
        f"CREATE TABLE v_soft AS SELECT * REPLACE ("
        f"TRY_CAST({STRIP} AS DECIMAL(18,2)) AS amount) FROM sales"
    )
    before = sales.execute(
        "SELECT count(*) FROM sales WHERE amount IS NOT NULL"
    ).fetchone()[0]
    after = sales.execute(
        "SELECT count(*) FROM v_soft WHERE amount IS NOT NULL"
    ).fetchone()[0]
    assert before == 3
    assert after == 2  # ORD-3 is gone, and only this assertion noticed


def test_the_unconvertible_rows_are_countable_before_anything_is_applied(sales):
    """Which is why the proposal carries the number, not the apply.

    Same expression, read-only, run at proposal time. A conversion action that
    cannot say how many values it will not convert is not a proposal.
    """
    n = sales.execute(
        f"SELECT count(*) FROM sales WHERE amount IS NOT NULL "
        f"AND TRY_CAST({STRIP} AS DECIMAL(18,2)) IS NULL"
    ).fetchone()[0]
    assert n == 1
    sample = sales.execute(
        f"SELECT amount FROM sales WHERE amount IS NOT NULL "
        f"AND TRY_CAST({STRIP} AS DECIMAL(18,2)) IS NULL LIMIT 3"
    ).fetchall()
    assert sample == [("(45.00)",)]
