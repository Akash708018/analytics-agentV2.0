"""
The docstrings of the three profiling tools, pinned.

Phase 3 established that tool docstrings are the agent's only instructions and
that they are load-bearing: a sentence dropped from one changed what the live
agent did, and nothing else in the suite noticed. `test_contract_tool_docs.py`
is the same checks for Phase 4's four tools. This is them for Phase 5's three.

Parsed with `ast`, never imported, for the reason that file gives: importing
`server.py` starts FastMCP and requires the whole dependency stack, while
parsing needs only the file.

The sentences asserted here are the ones the conversation depends on. If one
reads as clumsy, rewrite it AND the assertion together, deliberately -- that is
the point of pinning it rather than hoping.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVER = Path("engine/server.py")

PROFILE_TOOLS = (
    "profile_dataset",
    "profile_column",
    "read_result_file",
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
    The same tools, once the profiling block is actually in server.py.

    Three states, exactly as test_contract_tool_docs.py has them, and they must
    not be confused:

      none of the three present   the block has not been added yet. Skip, with
                                  a message naming the step -- a guide should
                                  not leave the suite red between its own
                                  parts.
      some of the three present   the block was added and something was lost or
                                  renamed. FAIL, naming what is missing. This
                                  is the case a blanket skip would hide, and it
                                  is the one worth catching.
      all three present           run everything.
    """
    found = [name for name in PROFILE_TOOLS if name in tools]
    if not found:
        pytest.skip(
            "the profiling tools are not in server.py yet -- add the block "
            "from Step 6b, then these run"
        )
    missing = [name for name in PROFILE_TOOLS if name not in tools]
    if missing:
        pytest.fail(
            f"server.py has {', '.join(found)} but not {', '.join(missing)}. "
            f"The block was added and something was lost -- restore from git "
            f"and re-append it whole rather than patching the gap."
        )
    return tools


def _doc(funcs, name: str) -> str:
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

@pytest.mark.parametrize("name", PROFILE_TOOLS)
def test_the_tool_is_registered(wired, name):
    assert name in wired, f"{name} is missing from {SERVER}"
    assert "tool" in _decorator_names(wired[name])


@pytest.mark.parametrize("name", PROFILE_TOOLS)
def test_the_tool_has_a_docstring_worth_reading(wired, name):
    doc = _doc(wired, name)
    assert len(doc.split()) > 40, (
        f"{name}'s docstring is too short to instruct anything. It is the only "
        f"thing the agent reads before deciding whether to call this."
    )


@pytest.mark.parametrize("name", PROFILE_TOOLS)
def test_every_profiling_tool_is_read_only(wired, name):
    """
    Profiling counts. It loads nothing, cleans nothing and changes nothing, so
    none of these should ever prompt the user for confirmation. A tool that
    writes a result file still only reads the DATA -- the file is the answer,
    not a change to the workspace.
    """
    ann = _annotations(wired[name])
    assert ann, f"{name} declares no annotations"
    marker = ann.get("readOnlyHint", ann.get("__name__", ""))
    assert marker in (True, "READ_ONLY"), (
        f"{name} only counts, so it must be marked read-only"
    )


# --------------------------------------------------------------------------
# profile_dataset
# --------------------------------------------------------------------------

def test_profile_says_it_changes_nothing(wired):
    """
    The whole risk of this phase is that a finding reads as an action. A
    profile that says a column parses as an integer has not converted it, and
    an agent that believes otherwise will report a table it never made.
    """
    doc = _doc(wired, "profile_dataset").lower()
    assert "nothing" in doc
    assert "clean" in doc or "convert" in doc or "change" in doc


def test_profile_points_at_phase_6_for_fixes(wired):
    doc = _doc(wired, "profile_dataset")
    assert "Phase 6" in doc


def test_profile_says_a_file_is_written_and_how_to_read_it(wired):
    """
    F7. The agent receives a path; it has to be told the path is openable and
    with what, or it describes data it never saw.
    """
    doc = _doc(wired, "profile_dataset")
    assert "read_result_file" in doc


def test_profile_documents_the_missing_value_override(wired):
    """
    Frictionless' model at the tool surface: a source that writes NR for
    absence is handled by declaring it, not by widening the default for
    everyone. An undocumented parameter is one the agent never uses.
    """
    doc = _doc(wired, "profile_dataset")
    assert "missing_values" in doc


def test_profile_does_not_claim_to_find_errors(wired):
    """
    'Outside a fence' is not 'wrong', and a docstring promising to find
    problems invites the agent to present findings as faults.
    """
    doc = _doc(wired, "profile_dataset").lower()
    assert "error" not in doc.replace("errors are", "")


# --------------------------------------------------------------------------
# profile_column
# --------------------------------------------------------------------------

def test_column_is_offered_as_the_drill_down(wired):
    doc = _doc(wired, "profile_column")
    assert "profile_dataset" in doc


def test_column_documents_widening_the_value_list(wired):
    """The one truncation this tool has, and the call that undoes it."""
    doc = _doc(wired, "profile_column")
    assert "top_n" in doc


def test_column_says_to_read_the_values_for_judgement_calls(wired):
    """
    The promise Step 2 made when it excluded 'unknown' from the vocabulary.
    The tool that keeps that promise has to say so where the agent reads.
    """
    doc = _doc(wired, "profile_column").lower()
    assert "missing" in doc
    assert "unknown" in doc or "judge" in doc or "decide" in doc


# --------------------------------------------------------------------------
# read_result_file
# --------------------------------------------------------------------------

def test_read_result_says_where_a_path_comes_from(wired):
    """
    Paths are not invented. The only legitimate source is an earlier tool
    result, and saying so is cheaper than refusing a guessed one afterwards.
    """
    doc = _doc(wired, "read_result_file").lower()
    assert "earlier" in doc or "returned" in doc or "previous" in doc


def test_read_result_documents_the_row_numbering(wired):
    """
    Rows are numbered from 1 and the header is not row 0. An off-by-one here
    is silent: the page comes back looking exactly right.
    """
    doc = _doc(wired, "read_result_file")
    assert "1" in doc
    assert "header" in doc.lower() or "row" in doc.lower()


def test_read_result_says_it_is_how_a_big_result_is_seen(wired):
    doc = _doc(wired, "read_result_file").lower()
    assert "profile" in doc or "result" in doc


# --------------------------------------------------------------------------
# nothing earlier was lost
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["ping", "reset_workspace", "check_file", "preview_file",
     "propose_ingest_spec", "confirm_ingest_spec", "load_csv", "load_excel",
     "list_sources", "describe_source", "load_postgres_table", "query_source",
     "list_datasets", "describe_dataset", "show_limits",
     "propose_dataset_contract", "confirm_dataset_contract",
     "get_workflow_state", "run_analysis"],
)
def test_the_earlier_tools_survive(tools, name):
    """
    Nineteen working tools is a lot to lose to a transcription slip, and this
    step appends to server.py rather than re-delivering it whole precisely so
    that it cannot.
    """
    assert name in tools, f"{name} disappeared from {SERVER}"
