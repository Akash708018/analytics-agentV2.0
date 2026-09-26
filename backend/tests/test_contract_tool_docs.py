"""
The docstrings of the four contract tools, pinned.

Phase 3 established that tool docstrings are the agent's only instructions and
that they are load-bearing: `tests/test_tool_docs.py` exists because a sentence
dropped from a docstring changed what the live agent did, and nothing else in
the suite noticed. These are the same checks for the tools Phase 4 adds.

Parsed with `ast`, never imported. Importing `server.py` starts FastMCP and
requires the whole dependency stack; parsing it needs only the file, so this
runs anywhere and cannot be broken by an unrelated import failing.

The sentences asserted here are the ones the conversation depends on. If one
reads as clumsy, rewrite it AND the assertion together, deliberately -- that is
the point of pinning it rather than hoping.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVER = Path("engine/server.py")

NEW_TOOLS = (
    "propose_dataset_contract",
    "confirm_dataset_contract",
    "get_workflow_state",
    "run_analysis",
)


@pytest.fixture(scope="module")
def tools() -> dict[str, ast.FunctionDef]:
    if not SERVER.exists():
        pytest.skip(f"{SERVER} not found; run from the repository root")
    tree = ast.parse(SERVER.read_text())
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


@pytest.fixture(scope="module")
def wired(tools) -> dict[str, ast.FunctionDef]:
    """
    The same tools, once the contract block is actually in server.py.

    Three states, and they must not be confused:

      none of the four present   the block has not been added yet. Skip, with
                                 a message naming the step -- a guide should
                                 not leave the suite red between its own parts.
      some of the four present   the block was added and something was lost or
                                 renamed. FAIL, naming what is missing. This is
                                 the case a blanket skip would hide, and it is
                                 the one worth catching.
      all four present           run everything.
    """
    found = [name for name in NEW_TOOLS if name in tools]
    if not found:
        pytest.skip(
            "the contract tools are not in server.py yet -- add the block from "
            "Step 7 Part 3, then these run"
        )
    missing = [name for name in NEW_TOOLS if name not in tools]
    if missing:
        pytest.fail(
            f"server.py has {', '.join(found)} but not {', '.join(missing)}. "
            f"The block was added and something was lost -- restore from git "
            f"and re-append it whole rather than patching the gap."
        )
    return tools


def _doc(funcs, name: str) -> str:
    """The docstring of one tool. `funcs` is whichever mapping the test holds."""
    assert name in funcs, f"{name} is not defined in {SERVER}"
    doc = ast.get_docstring(funcs[name])
    assert doc, f"{name} has no docstring, so the agent has no instructions"
    return doc


def _decorator_names(node: ast.FunctionDef) -> list[str]:
    out = []
    for dec in node.decorator_list:
        call = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(call, ast.Attribute):
            out.append(call.attr)
        elif isinstance(call, ast.Name):
            out.append(call.id)
    return out


def _annotations(node: ast.FunctionDef) -> dict:
    """The annotations= mapping on @mcp.tool(...), as a plain dict."""
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        for kw in dec.keywords:
            if kw.arg == "annotations":
                try:
                    return ast.literal_eval(kw.value)
                except ValueError:
                    return {"__name__": getattr(kw.value, "id", "?")}
    return {}


# --------------------------------------------------------------------------
# every tool exists, is registered, and says something
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", NEW_TOOLS)
def test_the_tool_is_registered(wired, name):
    assert name in wired, f"{name} is missing from {SERVER}"
    assert "tool" in _decorator_names(wired[name])


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_the_tool_has_a_docstring_worth_reading(wired, name):
    doc = _doc(wired, name)
    assert len(doc.split()) > 40, (
        f"{name}'s docstring is too short to instruct anything. It is the only "
        f"thing the agent reads before deciding whether to call this."
    )


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_the_tool_declares_what_it_does_to_the_workspace(wired, name):
    """
    Claude Desktop uses the annotations to decide what needs confirming, so a
    tool that writes and does not say so gets called without a prompt.
    """
    ann = _annotations(wired[name])
    assert ann, f"{name} declares no annotations"


def test_read_only_tools_are_marked_read_only(wired):
    for name in ("propose_dataset_contract", "get_workflow_state", "run_analysis"):
        ann = _annotations(wired[name])
        marker = ann.get("readOnlyHint", ann.get("__name__", ""))
        assert marker in (True, "READ_ONLY"), (
            f"{name} loads nothing and stores nothing, so it must be marked "
            f"read-only"
        )


def test_confirm_is_marked_as_writing(wired):
    ann = _annotations(wired["confirm_dataset_contract"])
    marker = ann.get("readOnlyHint", ann.get("__name__", ""))
    assert marker in (False, "WRITES")


# --------------------------------------------------------------------------
# propose_dataset_contract
# --------------------------------------------------------------------------

def test_propose_says_it_stores_nothing(wired):
    doc = _doc(wired, "propose_dataset_contract")
    assert "Stores nothing" in doc or "stores nothing" in doc


def test_propose_forbids_answering_on_the_users_behalf(wired):
    """
    The single most important sentence in Phase 4. An agent that invents a
    grain produces a contract that looks agreed and was not.
    """
    doc = _doc(wired, "propose_dataset_contract")
    assert "Do not answer" in doc
    assert "grain" in doc


def test_propose_names_the_overrides_that_carry_answers_back(wired):
    doc = _doc(wired, "propose_dataset_contract")
    for override in ("grain=", "measure_definitions=", "primary_key="):
        assert override in doc, f"{override} is not documented"


def test_propose_says_to_call_it_again_rather_than_editing_json(wired):
    """Phase 3's lesson, and the one the Step 7 live run checked for."""
    doc = _doc(wired, "propose_dataset_contract").lower()
    assert "again" in doc
    assert "unresolved" in doc


# --------------------------------------------------------------------------
# confirm_dataset_contract
# --------------------------------------------------------------------------

def test_confirm_says_the_user_must_have_agreed(wired):
    doc = _doc(wired, "confirm_dataset_contract")
    assert "agreed" in doc or "agree" in doc


def test_confirm_documents_that_versions_are_kept(wired):
    doc = _doc(wired, "confirm_dataset_contract")
    assert "supersed" in doc


# --------------------------------------------------------------------------
# run_analysis -- the gate
# --------------------------------------------------------------------------

def test_run_analysis_names_the_contract_requirement(wired):
    doc = _doc(wired, "run_analysis")
    assert "contract" in doc.lower()


def test_run_analysis_says_what_to_do_when_it_refuses(wired):
    doc = _doc(wired, "run_analysis")
    assert "propose_dataset_contract" in doc


def test_run_analysis_is_honest_about_what_it_computes(wired):
    """
    It must not read as though it computes analyses. An agent that believes it
    does will report an answer it never received.

    The assertion this replaces was `"Phase 8" in doc or "not yet" in doc`,
    which a docstring saying "Phase 8's analyses run from here" would also
    have passed -- a test that goes green on the opposite meaning. Phase 8
    arrived, so the sentence had to change; it is pinned now to what the tool
    does rather than to which phase it is waiting for.
    """
    doc = _doc(wired, "run_analysis")
    assert "computes nothing" in doc
    assert "compute_analysis" in doc, "it must name the tool that does"


# --------------------------------------------------------------------------
# get_workflow_state
# --------------------------------------------------------------------------

def test_workflow_state_explains_why_load_times_matter(wired):
    """
    F13. The server outlives the conversation, so the agent has to be told
    that a dataset it did not load may be sitting there.
    """
    doc = _doc(wired, "get_workflow_state")
    assert "earlier" in doc or "another" in doc or "outlives" in doc


def test_workflow_state_is_offered_as_the_recovery_call(wired):
    doc = _doc(wired, "get_workflow_state")
    assert "stuck" in doc or "next" in doc.lower()


# --------------------------------------------------------------------------
# the Phase 3 tools are still there
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["ping", "reset_workspace", "check_file", "preview_file",
     "propose_ingest_spec", "confirm_ingest_spec", "load_csv", "load_excel",
     "list_sources", "describe_source", "load_postgres_table", "query_source",
     "list_datasets", "describe_dataset", "show_limits"],
)
def test_the_phase_3_tools_survive(tools, name):
    """
    server.py is re-delivered whole in this step. Fifteen working tools is a
    lot to lose to a transcription slip, and tests/test_tool_docs.py only
    guards the docstrings of the ones it knows about.
    """
    assert name in tools, f"{name} disappeared from {SERVER}"
