"""A column type set in an Ingest Spec reaches the loader (C96).

ColumnSpec.dtype was shown to the person confirming the spec and never passed to load_csv or
load_excel, both of which take `dtypes`. The file loaded with inferred types while the confirmed
spec said otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.ingest import draft  # noqa: E402
from backend.engine.ingest.csv_loader import load_csv  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "spec_dtype_test"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "clean_sales.csv"


def spec_with(dtypes: dict[str, str]):
    spec = draft.draft_for_path(str(FIXTURE), dataset_name="sales").spec
    columns = [c.model_copy(update={"dtype": dtypes.get(c.target_name, c.dtype)})
               for c in spec.columns]
    return spec.model_copy(update={"columns": columns})


def column_type(name: str) -> str:
    con = db.connect(WORKSPACE)
    try:
        return con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'sales' AND column_name = ?", [name]).fetchone()[0]
    finally:
        con.close()


@pytest.fixture()
def ws():
    workspace.reset(WORKSPACE)
    yield
    workspace.reset(WORKSPACE)


def test_a_pinned_column_is_in_the_loader_kwargs():
    kwargs = spec_with({"order_date": "VARCHAR"}).to_loader_kwargs(load_csv)
    assert kwargs["dtypes"] == {"order_date": "VARCHAR"}


def test_a_spec_pinning_nothing_adds_no_key():
    assert "dtypes" not in spec_with({}).to_loader_kwargs(load_csv)


def test_confirming_a_pinned_spec_loads_that_type(ws):
    """Inferred, order_date is a DATE -- measured in the same test, so VARCHAR is a change."""
    unpinned = server.confirm_ingest_spec(spec_json=spec_with({}).model_dump_json(),
                                          workspace_id=WORKSPACE)
    assert "BLOCKED" not in unpinned, unpinned
    assert column_type("order_date") == "DATE"

    pinned = server.confirm_ingest_spec(
        spec_json=spec_with({"order_date": "VARCHAR"}).model_dump_json(),
        workspace_id=WORKSPACE)
    assert "BLOCKED" not in pinned, pinned
    assert column_type("order_date") == "VARCHAR"
