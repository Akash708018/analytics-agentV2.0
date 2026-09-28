"""What mixed_types.xlsx actually contains, counted rather than quoted.

Phase 6, Step 2. Run from the repo root:

    uv run pytest tests/test_mixed_types_ground_truth.py -q

The Done-When for this phase names this fixture: "run on mixed_types.xlsx,
approve 3 of 6, ledger shows exactly those 3 with correct counts". Correct
counts need ground truth, and make_fixtures.py's docstring already states some:

    load_excel(...)                  -> LoadRefused naming row 5101
    load_excel(..., on_error='null') -> {'units': 7, 'unit_price': 3}
    load_excel(..., all_text=True)   -> every column VARCHAR, no failures

That is a CLAIM, written beside the generator. Phase 5 shipped a claim of
exactly that shape -- "the DISTINCT * duplicate scan is the slow half" -- which
was wrong by a factor of ten because nobody had run it. So this file does not
quote the docstring; it counts the rows and compares.

The row positions are the generator's own constants, mirrored here on purpose.
If make_fixtures.py changes them, this file fails and says which count moved,
which is the whole point of pinning a fixture rather than trusting it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.config import DEFAULT_NA_VALUES  # noqa: E402
from backend.engine.ingest.excel import load_excel  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "mixed_types_gt"
FIXTURE = Path("tests/fixtures/mixed_types.xlsx")

# make_fixtures.py:_BAD_UNITS_ROWS / _BAD_PRICE_ROWS, and n=6000.
N_ROWS = 6000
BAD_UNITS_ROWS = (5100, 5200, 5300, 5400, 5500, 5600, 5700)
BAD_PRICE_ROWS = (5150, 5450, 5750)
BAD_UNITS_TOKEN = "n/a"
BAD_PRICE_TOKEN = "not priced"

COLUMNS = ["order_id", "order_date", "region", "units", "unit_price", "is_return"]


def order_ids(rows: tuple[int, ...]) -> set[str]:
    """The generator writes f'ORD-{i:05d}' for row i, 1-based."""
    return {f"ORD-{i:05d}" for i in rows}


# load_excel(con, path, dataset_name, sheet=None, header_rows=1, names=None,
# na_values=None, footer_skip_rows=0, dtypes=None, on_error='stop',
# all_text=False, inference_rows=5000, replace=True) -> LoadResult.
# Read from inspect.signature, not remembered. Note na_values=None means NO
# tokens rather than DEFAULT_NA_VALUES, and replace=True means a second load
# overwrites in silence.
def load(con, table: str, **kw):
    return load_excel(con, str(FIXTURE), table, **kw)


def types_of(con, table: str) -> dict[str, str]:
    return dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{table}' ORDER BY ordinal_position"
        ).fetchall()
    )


@pytest.fixture()
def con():
    workspace.reset(WORKSPACE)
    c = db.connect(WORKSPACE)
    yield c
    c.close()
    workspace.reset(WORKSPACE)


@pytest.fixture(autouse=True)
def _fixture_present():
    if not FIXTURE.exists():
        pytest.skip(f"{FIXTURE} not present; run tests/fixtures/make_fixtures.py")


# --------------------------------------------------------------------------
# 1. the default: it does not load at all
# --------------------------------------------------------------------------


def test_the_default_load_refuses_and_names_a_row(con):
    """on_error='stop' is the default, and this fixture trips it.

    Worth stating plainly because it changes what Phase 6 is being asked to do:
    the Done-When's fixture cannot be loaded with default settings. Whatever
    Phase 6 cleans, it cleans something a person had to reach for an option to
    get in at all.
    """
    with pytest.raises(Exception) as exc:  # noqa: PT011
        load(con, "mixed_default")
    message = str(exc.value)
    assert "5101" in message, f"the refusal does not name the row: {message[:200]}"


# --------------------------------------------------------------------------
# 2. on_error='null': the text is gone before cleaning is ever called
# --------------------------------------------------------------------------


def test_on_error_null_converts_the_columns_and_nulls_the_failures(con):
    """The exact types, not just 'not VARCHAR'.

    Measured by the Step 2 probe before being written here. order_date is
    TIMESTAMP because this is the one fixture that writes real datetime
    objects rather than isoformat strings -- make_fixtures.py says so, and
    it is the difference between the Excel path and the CSV path.
    """
    load(con, "mixed_null", on_error="null")
    assert types_of(con, "mixed_null") == {
        "order_id": "VARCHAR",
        "order_date": "TIMESTAMP",
        "region": "VARCHAR",
        "units": "BIGINT",
        "unit_price": "DOUBLE",
        "is_return": "BOOLEAN",
    }


def test_the_failure_counts_are_exactly_seven_and_three(con):
    load(con, "mixed_null", on_error="null")
    units_null = con.execute(
        "SELECT count(*) FROM mixed_null WHERE units IS NULL"
    ).fetchone()[0]
    price_null = con.execute(
        "SELECT count(*) FROM mixed_null WHERE unit_price IS NULL"
    ).fetchone()[0]
    assert units_null == len(BAD_UNITS_ROWS) == 7
    assert price_null == len(BAD_PRICE_ROWS) == 3


def test_the_nulls_are_in_the_rows_the_generator_put_them_in(con):
    """Counts can be right for the wrong reason. Positions cannot."""
    load(con, "mixed_null", on_error="null")
    got_units = {
        r[0]
        for r in con.execute(
            "SELECT order_id FROM mixed_null WHERE units IS NULL"
        ).fetchall()
    }
    got_price = {
        r[0]
        for r in con.execute(
            "SELECT order_id FROM mixed_null WHERE unit_price IS NULL"
        ).fetchall()
    }
    assert got_units == order_ids(BAD_UNITS_ROWS)
    assert got_price == order_ids(BAD_PRICE_ROWS)


def test_the_region_tokens_are_not_counted_as_coercion_failures(con):
    """region holds 'N/A' in ~5% of rows. Declared missing, not a failure.

    And it stays LITERAL TEXT: load_excel's na_values defaults to None, which
    means no tokens, not DEFAULT_NA_VALUES. So the 'N/A's are still there after
    the load, and a missing-token action on region is available to Phase 6
    rather than having been applied at ingest.
    """
    load(con, "mixed_null", on_error="null")
    n_a = con.execute(
        "SELECT count(*) FROM mixed_null WHERE region = 'N/A'"
    ).fetchone()[0]
    nulls = con.execute(
        "SELECT count(*) FROM mixed_null WHERE region IS NULL"
    ).fetchone()[0]
    assert n_a > 0, "region's 'N/A' tokens were consumed at load time"
    assert nulls == 0, "load_excel nulled them without being asked to"
    assert n_a < N_ROWS // 2, "region is mostly missing; the fixture moved"


# --------------------------------------------------------------------------
# 3. all_text=True: the only mode Phase 6 has anything to do
# --------------------------------------------------------------------------


def test_all_text_gives_six_varchar_columns(con):
    load(con, "mixed_text", all_text=True)
    types = types_of(con, "mixed_text")
    assert list(types) == COLUMNS
    assert set(types.values()) == {"VARCHAR"}


def test_the_unconvertible_values_survive_as_text(con):
    """This is what a cleaning proposal will be looking at.

    Under on_error='null' these rows are already NULL and there is nothing to
    convert. Under all_text they are still 'n/a' and 'not priced', which is the
    text->decimal conversion the Done-When asks for.
    """
    load(con, "mixed_text", all_text=True)
    units_bad = con.execute(
        "SELECT count(*) FROM mixed_text WHERE units = ?", [BAD_UNITS_TOKEN]
    ).fetchone()[0]
    price_bad = con.execute(
        "SELECT count(*) FROM mixed_text WHERE unit_price = ?", [BAD_PRICE_TOKEN]
    ).fetchone()[0]
    assert units_bad == 7
    assert price_bad == 3


def test_the_rest_of_the_column_converts_cleanly(con):
    """5,993 of 6,000 parse. The proposal's numerator and denominator both."""
    load(con, "mixed_text", all_text=True)
    ok = con.execute(
        "SELECT count(*) FROM mixed_text WHERE TRY_CAST(units AS BIGINT) IS NOT NULL"
    ).fetchone()[0]
    assert ok == N_ROWS - len(BAD_UNITS_ROWS)

    ok_price = con.execute(
        "SELECT count(*) FROM mixed_text "
        "WHERE TRY_CAST(unit_price AS DECIMAL(18,2)) IS NOT NULL"
    ).fetchone()[0]
    assert ok_price == N_ROWS - len(BAD_PRICE_ROWS)


# --------------------------------------------------------------------------
# 4. the two conversions are NOT the same action
# --------------------------------------------------------------------------


def test_the_two_bad_tokens_differ_in_the_missing_vocabulary():
    """The finding this step exists for.

    'n/a' and 'not priced' are identical in shape -- text in a numeric column,
    a handful of rows, TRY_CAST returns NULL for both. They are not the same
    decision. One is already a declared missing token, so converting it to NULL
    makes an absence that was always there explicit. The other is a value that
    means something, and converting it destroys the only record that a price
    was withheld rather than merely absent.

    Asserted against the config the loaders actually read, not against a list
    recalled from pandas.
    """
    vocabulary = {v.upper() for v in DEFAULT_NA_VALUES}
    assert BAD_UNITS_TOKEN.upper() in vocabulary, (
        f"'{BAD_UNITS_TOKEN}' is not in DEFAULT_NA_VALUES ({sorted(vocabulary)}); "
        "the P6-D4 argument in the Step 2 guide rests on it being there"
    )
    assert BAD_PRICE_TOKEN.upper() not in vocabulary


def test_the_declared_and_the_undeclared_are_separable_in_sql(con):
    """A proposal has to be able to compute this split, read-only, per column."""
    load(con, "mixed_text", all_text=True)
    tokens = ", ".join("?" for _ in DEFAULT_NA_VALUES)
    declared = con.execute(
        f"SELECT count(*) FROM mixed_text WHERE upper(units) IN ({tokens})",
        [v.upper() for v in DEFAULT_NA_VALUES],
    ).fetchone()[0]
    unconvertible = con.execute(
        "SELECT count(*) FROM mixed_text WHERE TRY_CAST(units AS BIGINT) IS NULL"
    ).fetchone()[0]
    assert unconvertible == 7
    assert declared == 7, "every unconvertible value in units is a declared token"

    price_declared = con.execute(
        f"SELECT count(*) FROM mixed_text WHERE upper(unit_price) IN ({tokens})",
        [v.upper() for v in DEFAULT_NA_VALUES],
    ).fetchone()[0]
    price_unconvertible = con.execute(
        "SELECT count(*) FROM mixed_text "
        "WHERE TRY_CAST(unit_price AS DECIMAL(18,2)) IS NULL"
    ).fetchone()[0]
    assert price_unconvertible == 3
    assert price_declared == 0, "'not priced' must not be a declared token"
