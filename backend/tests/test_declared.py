"""Shared declarations, real aggregate types, and Phase 8 Step 7a regressions."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import duckdb
import pytest
from pydantic import ValidationError

from backend.engine.analysis import frequency, summary_stats
from backend.engine.analysis.base import (
    LostRows,
    ScopeError,
    scope_for,
    share_basis,
)
from backend.engine.analysis.declared import (
    ADDITIVE_AGGS,
    AGG_SQL,
    adds_across_groups,
    ARBITRARY_PRECISION_TYPES,
    INTEGER_TYPES,
    NUMERIC_TYPES,
    agg_of,
    column_types,
    dimension_names,
    is_arbitrary_precision,
    is_integer,
    is_numeric,
    require_dimension,
    require_measure,
)
from backend.engine.analysis.registry import catalogue
from backend.engine.contract.dataset_contract import AGGREGATIONS, Aggregation, Measure


@dataclass
class FakeMeasure:
    name: str
    agg: str | None = "sum"
    definition: str = "what was charged"
    unit: str | None = "GBP"


@dataclass
class FakeWindow:
    start: date
    end: date


@dataclass
class FakeContract:
    dataset_name: str = "sales"
    date_column: str | None = "ts"
    analysis_window: FakeWindow | None = None
    known_exclusions: list = field(default_factory=list)
    excluded_columns: list = field(default_factory=list)
    measures: list = field(default_factory=lambda: [FakeMeasure("amount")])
    dimensions: list = field(default_factory=lambda: ["region", "channel"])
    primary_key: list = field(default_factory=lambda: ["id"])


@dataclass
class FakeGate:
    contract: FakeContract
    caveats: list = field(default_factory=list)


@pytest.fixture
def con():
    with duckdb.connect(":memory:") as connection:
        connection.execute("CREATE TABLE sales(amount DECIMAL(18,2), region VARCHAR)")
        connection.execute("INSERT INTO sales VALUES (10.50, 'North'), (20.25, 'South')")
        yield connection


def test_the_contract_spells_its_aggregates_once():
    assert get_args(Aggregation) == AGGREGATIONS


def test_sql_covers_exactly_what_a_contract_can_declare():
    assert set(AGG_SQL) == set(get_args(Aggregation)) - {"none"}
    assert "none" not in AGG_SQL


def test_a_median_total_agrees_with_its_median_column(con):
    gate = FakeGate(FakeContract(
        date_column=None,
        primary_key=[],
        measures=[FakeMeasure("amount", agg="median")],
    ))
    output = summary_stats.summary_stats(con, gate, scope_for(con, gate))
    assert len(output.rows) == 1
    row = dict(zip(output.headers, output.rows[0]))
    assert row["total"] == row["median"] == "15.375"
    assert row["total"] != "15.37"
    assert row["median"] != "15.37"


def test_agg_of_reads_the_real_measure_and_the_model_refuses_avg():
    assert agg_of(Measure(name="x", agg="mean")) == "mean"
    assert agg_of(Measure(name="x")) is None
    with pytest.raises(ValidationError):
        Measure(name="x", agg="avg")


def test_dimension_names_reads_strings_and_objects():
    contract = SimpleNamespace(dimensions=["region", SimpleNamespace(name="channel")])
    assert dimension_names(contract) == ["region", "channel"]


def test_require_dimension_refuses_an_excluded_column_before_anything_else():
    contract = FakeContract(dimensions=[], excluded_columns=["region"])
    with pytest.raises(ValueError) as exc:
        require_dimension(contract, "region")
    assert "never to read it" in str(exc.value)
    assert "Declared:" not in str(exc.value)


def test_require_dimension_refuses_an_undeclared_column_with_the_list():
    with pytest.raises(ValueError) as exc:
        require_dimension(FakeContract(), "unknown")
    assert "Declared: region, channel" in str(exc.value)
    contract = SimpleNamespace(dimensions=["region"], excluded_columns=[])
    assert require_dimension(contract, "region") == "region"


def test_require_measure_returns_the_measure_object():
    measure = FakeMeasure("amount")
    contract = FakeContract(measures=[measure])
    assert require_measure(contract, "amount") is measure
    duck = SimpleNamespace(measures=[measure], excluded_columns=[])
    assert require_measure(duck, "amount") is measure


def test_require_measure_refuses_an_undeclared_measure_with_the_list():
    with pytest.raises(ValueError) as exc:
        require_measure(FakeContract(), "unknown")
    assert "Declared: amount" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        require_measure(FakeContract(measures=[]), "unknown")
    assert "Declared: (none)" in str(exc.value)


def test_require_measure_refuses_an_excluded_measure():
    for measures in [[FakeMeasure("amount")], []]:
        with pytest.raises(ValueError) as exc:
            require_measure(FakeContract(measures=measures, excluded_columns=["amount"]), "amount")
        assert "never to read it" in str(exc.value)
        assert "Declared:" not in str(exc.value)


def test_type_names_are_read_as_duckdb_writes_them():
    for dtype in ["DECIMAL(18,2)", "DOUBLE", "BIGINT"]:
        assert is_numeric(dtype)
    for dtype in ["VARCHAR", "DATE", "TIMESTAMP", "INTERVAL"]:
        assert not is_numeric(dtype)
    for dtype in ["BIGINT", "HUGEINT", "UINTEGER", "UHUGEINT", " bigint "]:
        assert is_integer(dtype)
    for dtype in ["DECIMAL(18,2)", "DOUBLE", "INTERVAL", "INTEGER[]", "BIGINT_EXTRA"]:
        assert not is_integer(dtype)


def test_column_types_are_read_in_column_order(con):
    con.execute("CREATE TABLE ordered(z INTEGER, a DECIMAL(18,2), middle VARCHAR)")
    types = column_types(con, "ordered")
    assert list(types) == ["z", "a", "middle"]
    assert types["a"] == "DECIMAL(18,2)"


def test_top_n_refuses_an_excluded_dimension_and_an_excluded_measure(con):
    for excluded in ["region", "amount"]:
        gate = FakeGate(FakeContract(
            date_column=None,
            primary_key=[],
            excluded_columns=[excluded],
        ))
        with pytest.raises(ValueError) as exc:
            frequency.top_n(con, gate, scope_for(con, gate), dimension="region", measure="amount")
        assert "never to read it" in str(exc.value)


def test_every_registered_analysis_sits_in_its_build_guide_tier():
    """P10-O5, and P9-O10 with it: the guide is the roster, and this reads it.

    The previous version compared the registry against a dict typed out of the guide. It caught
    hypothesis_test's omission in Phase 10 Step 3 and it would have kept passing if the guide
    had changed instead, because nothing tied the two together. Four copies of the roster
    existed; this removes one and makes a second answerable.
    """
    guide = Path(__file__).resolve().parents[1] / "docs" / "analytics_agent_build_guide_v1.2.md"
    assert guide.exists(), f"the build guide is not at {guide}"
    lines = guide.read_text(encoding="utf-8").splitlines()

    tiers: dict[str, int] = {}
    for i, line in enumerate(lines):
        heading = re.match(r"^#{2,4}\s+\*\*Tier (\d+)\s*[—-]", line)
        if not heading:
            continue
        tier = int(heading.group(1))
        listing = next((s for s in lines[i + 1:i + 4] if s.strip()), "")
        for name in re.findall(r"`([a-z_][a-z0-9_]*)`", listing):
            tiers[name] = tier

    assert len(tiers) >= 20, f"parsed only {len(tiers)} names from the guide: {sorted(tiers)}"
    assert tiers.get("summary_stats") == 1, "the parse found no Tier 1"
    assert tiers.get("hypothesis_test") == 6, "the parse found no Tier 6"

    entries = catalogue()
    for name, tier, summary in entries:
        assert name in tiers, (
            f"{name} is registered but the build guide's tier lists do not name it"
        )
        assert tier == tiers[name], (
            f"{name} is registered at tier {tier}; the guide puts it at {tiers[name]}"
        )
    # P9-O11: the check above runs registry -> guide, so it catches an analysis the guide does
    # not name. The other direction was computed here and thrown away on an assertion true of
    # every list, so a name the guide carries that nothing registers passed silently -- which is
    # how a half-applied wiring would have looked. Tier 8 is legitimately unbuilt (Phase 15, and
    # its listing is prose rather than backticked names, so the parse finds none of it), so only
    # tiers below 8 are required to exist.
    registered = {name for name, _, _ in entries}
    premature = sorted(n for n in set(tiers) - registered if tiers[n] < 8)
    assert not premature, (
        f"the build guide names {', '.join(premature)} below Tier 8 and nothing registers "
        f"them. An analysis in the roster that no module registers is a wiring step that did "
        f"not run."
    )


def test_lost_rows_is_a_scope_error():
    assert issubclass(LostRows, ScopeError)
    assert issubclass(LostRows, ValueError)


# Phase 8 Step 7b.0: type names as DuckDB writes them, and excluded_columns read directly.


def test_a_list_of_numbers_is_not_a_number():
    for dtype in ["INTEGER[]", "DECIMAL(18,2)[]", "DOUBLE[]", "INTEGER[3]", "BIGNUM[]"]:
        assert not is_numeric(dtype)
        assert not is_arbitrary_precision(dtype)
    assert is_numeric("INTEGER") and is_numeric("DECIMAL(18,2)")


def test_every_integer_type_is_numeric_and_real_is_gone():
    for dtype in INTEGER_TYPES:
        assert is_numeric(dtype)
    assert "REAL" not in NUMERIC_TYPES
    assert not set(NUMERIC_TYPES) & set(ARBITRARY_PRECISION_TYPES)


def test_predicates_read_the_names_of_a_real_duckdb_table(con):
    con.execute(
        "CREATE TABLE kinds(u UHUGEINT, n NUMERIC(10,2), r REAL, "
        "li INTEGER[], dt DATE, v VARINT)"
    )
    types = column_types(con, "kinds")
    assert {c for c, t in types.items() if is_numeric(t)} == {"u", "n", "r"}
    assert {c for c, t in types.items() if is_arbitrary_precision(t)} == {"v"}


def test_a_contract_without_excluded_columns_is_refused_loudly():
    with pytest.raises(AttributeError, match="excluded_columns"):
        require_dimension(SimpleNamespace(dimensions=["region"]), "region")
    with pytest.raises(AttributeError, match="excluded_columns"):
        require_measure(SimpleNamespace(measures=[FakeMeasure("amount")]), "amount")


def test_summary_stats_does_not_name_a_list_column_as_undeclared_numeric(con):
    con.execute("ALTER TABLE sales ADD COLUMN tags INTEGER[]")
    con.execute("ALTER TABLE sales ADD COLUMN extra DOUBLE")
    gate = FakeGate(FakeContract(date_column=None, primary_key=[]))
    text = " ".join(summary_stats.summary_stats(con, gate, scope_for(con, gate)).summary)
    assert "extra" in text
    assert "tags" not in text


def test_bignum_is_named_and_not_computed_over(con):
    con.execute("CREATE TABLE wide(v VARINT)")
    dtype = column_types(con, "wide")["v"]
    assert is_arbitrary_precision(dtype)
    assert not is_numeric(dtype)
    assert not is_integer(dtype)


# Phase 8 Step 8a: additivity, and the share gate it feeds.


def test_only_sum_and_count_add_across_groups():
    assert ADDITIVE_AGGS == ("sum", "count")
    for agg in ADDITIVE_AGGS:
        assert adds_across_groups(agg)
    for agg in set(get_args(Aggregation)) - set(ADDITIVE_AGGS):
        assert not adds_across_groups(agg)
    assert not adds_across_groups(None)
    assert set(ADDITIVE_AGGS) <= set(AGG_SQL)


def test_share_basis_names_which_of_the_three_refusals_applied():
    assert share_basis("sum", [Decimal("10"), Decimal("5")]).denominator == Decimal("15")
    assert share_basis("sum", [10, 30]).share(10) == "25.0%"
    assert share_basis("mean", [1, 2]).reason.startswith("mean does not add up")
    assert "not a proportion" in share_basis("sum", [5, -1]).reason
    assert "inf rather than an error" in share_basis("sum", [0, 0]).reason
    assert share_basis("sum", [None, None]).reason is not None
    assert share_basis("sum", [10, None]).share(None) == ""


def test_every_registered_analysis_is_named_in_the_tool_description():
    """C72: six analyses were built, tested, accepted, and left out of the only list a caller
    reads.

    compute_analysis's docstring is what FastMCP renders as the tool description. An analysis
    absent from it is registered, callable, and invisible to the agent deciding what to call --
    which a reader takes for a refusal rather than an omission.

    P10-D39 made the analysis *count* read the build guide instead of drifting across files.
    This is the same fix one level down, for the *roster*, which that one did not cover.

    Read from source rather than imported, the way registered_tools() does in test_phase8.py:
    what matters is the text in the file FastMCP reads, not whatever an import happens to bind.
    """
    server = Path(__file__).resolve().parents[1] / "engine" / "server.py"
    assert server.exists(), f"server.py is not at {server}"
    tree = ast.parse(server.read_text(encoding="utf-8"))
    doc = next(
        (ast.get_docstring(node) for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef) and node.name == "compute_analysis"),
        None,
    )
    assert doc, "compute_analysis has no docstring in server.py"

    missing = sorted(name for name, _, _ in catalogue() if name not in doc)
    assert not missing, (
        f"registered but absent from compute_analysis's parameter table: {', '.join(missing)}. "
        f"An analysis a caller cannot find is one that does not exist to them."
    )
