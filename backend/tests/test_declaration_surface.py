"""Declaring a foreign key or a domain through the tool, not through the JSON.

Phase 7, Step 13. Run from the repo root:

    uv run pytest tests/test_declaration_surface.py -q

These are signature and parser tests on purpose. `propose_contract` needs a
loaded table and a full evidence pass; what this step changed is the route an
answer takes to the contract, and a route is testable without the journey.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.contract import ContractRefused, tools  # noqa: E402
from backend.engine.contract.dataset_contract import ForeignKey  # noqa: E402
from backend.engine.contract.propose import propose_contract  # noqa: E402
from backend.engine.contract.refusals import Reason, reason_of  # noqa: E402

SERVER = Path(__file__).resolve().parents[1] / "engine/server.py"


def params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------


def test_the_proposer_takes_both():
    assert {"foreign_keys", "domains"} <= params(propose_contract)


def test_the_tool_layer_takes_both():
    assert {"foreign_keys", "domains"} <= params(tools.propose)


def test_the_server_tool_takes_both():
    """Read from source rather than imported: a tool is registered when the
    file says so, and a running process cannot tell you until it restarts."""
    tree = ast.parse(SERVER.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "propose_dataset_contract"
    )
    names = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    assert {"foreign_keys", "domains"} <= names


def test_the_answer_comes_back_the_way_the_question_went_out():
    """The Phase 3 mechanism, which is the whole argument for this step: an
    answer edited into the JSON leaves the rest of the document disagreeing
    with it."""
    assert "contract_json" not in params(tools.propose)


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------


def test_nothing_declared_parses_to_nothing():
    assert tools._parse_foreign_keys(None) == []
    assert tools._parse_foreign_keys([]) == []


def test_a_declaration_becomes_a_foreign_key():
    got = tools._parse_foreign_keys(
        [{"columns": ["region"], "references": "region_lookup"}]
    )
    assert got == [ForeignKey(columns=["region"], references="region_lookup")]
    assert got[0].label() == "region -> region_lookup.region"


def test_a_string_where_an_object_belongs_is_refused_with_a_reason():
    """A pydantic traceback with a `loc` tuple in it is not an instruction."""
    with pytest.raises(ContractRefused) as caught:
        tools._parse_foreign_keys(["region_lookup"])
    text = str(caught.value)
    assert reason_of(text) is Reason.CONTRACT_INVALID
    assert "foreign key 1 is not an object" in text
    assert "propose_dataset_contract" in text


def test_a_key_with_no_columns_is_refused_and_says_which_one():
    with pytest.raises(ContractRefused) as caught:
        tools._parse_foreign_keys(
            [{"columns": ["region"], "references": "region_lookup"},
             {"columns": [], "references": "x"}]
        )
    assert "foreign key 2 is not usable" in str(caught.value)


def test_a_key_with_nothing_to_reference_is_refused():
    with pytest.raises(ContractRefused) as caught:
        tools._parse_foreign_keys([{"columns": ["region"], "references": ""}])
    assert reason_of(str(caught.value)) is Reason.CONTRACT_INVALID
