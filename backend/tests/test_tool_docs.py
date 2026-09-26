"""
The tool docstrings are the agent's instructions. Guard them.

Everything else in this suite tests what the code does. This tests what Claude
is *told*, because in an MCP server those are different things and only one of
them is normally covered.

The failure this exists to catch: someone tidies a docstring, the tests stay
green, and three weeks later the agent quietly starts loading files without
showing anyone the spec first. There is no traceback for that, and no test
anywhere else would notice.

Read from source with `ast` rather than by importing the module, so this runs
without a FastMCP install and does not care how the decorator wraps a function.

Phase 4 note: the four contract tools are in the roster below, and their own
sentences are pinned in tests/test_contract_tool_docs.py rather than here.
This file keeps the Phase 3 conversation and the tool surface as a whole; that
one keeps the contract conversation. Splitting them means a Phase 5 tool adds
a file rather than lengthening this one indefinitely.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVER = Path("engine/server.py")


def _tool_docs() -> dict[str, str]:
    """{tool name: docstring} for every @mcp.tool in server.py."""
    tree = ast.parse(SERVER.read_text())
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                out[node.name] = ast.get_docstring(node) or ""
    return out


@pytest.fixture(scope="module")
def docs():
    if not SERVER.exists():
        pytest.skip("run from the repo root")
    found = _tool_docs()
    assert found, "no @mcp.tool functions found -- has server.py moved?"
    return found


# --------------------------------------------------------------------------
# the surface
# --------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "apply_cleaning_plan",
    "check_file",
    "compute_analysis",
    "confirm_dataset_contract",
    "confirm_ingest_spec",
    "describe_dataset",
    "describe_source",
    "get_cleaning_ledger",
    "get_workflow_state",
    "list_datasets",
    "list_sources",
    "load_csv",
    "load_excel",
    "load_postgres_table",
    "ping",
    "preview_file",
    "profile_column",
    "profile_dataset",
    "propose_cleaning_plan",
    "propose_dataset_contract",
    "propose_ingest_spec",
    "query_source",
    "read_result_file",
    "reset_workspace",
    "run_analysis",
    "show_limits",
    "validate_dataset",
    "render_chart",
    "build_report",
    # Step 3, 26/09/2026: a metric the contract lacks, proposed and approved apart.
    "propose_metric",
    "decide_metric",
}


def test_the_registered_tools_are_exactly_the_expected_ones(docs):
    """
    A CLOSED set, deliberately. Not "these are present" but "these and nothing
    else" -- registering a tool without declaring it here changes what Claude
    Desktop sees, and this refuses to let that happen quietly. It is the check
    that fired when Phase 4 added four tools, which is the behaviour working.

    Adding a tool is therefore two edits: the tool, and this set. That is the
    intended friction.
    """
    extra = sorted(set(docs) - EXPECTED_TOOLS)
    missing = sorted(EXPECTED_TOOLS - set(docs))
    assert not extra, f"registered but not declared here: {extra}"
    assert not missing, f"declared here but not registered: {missing}"


def test_every_tool_has_a_docstring(docs):
    missing = sorted(name for name, doc in docs.items() if not doc.strip())
    assert not missing, f"tools with no docstring: {missing}"


def test_every_docstring_opens_with_a_one_line_summary(docs):
    """Claude reads the first line hardest. It has to say what the tool does."""
    for name, doc in docs.items():
        first = doc.strip().splitlines()[0]
        assert first.endswith("."), f"{name}: first line should be a sentence"
        assert len(first) < 100, f"{name}: first line is too long to scan"


# --------------------------------------------------------------------------
# the instructions that make the conversation work
# --------------------------------------------------------------------------

def test_propose_says_it_loads_nothing(docs):
    """
    If Claude thinks proposing might load, it will hesitate to call it, and
    the whole two-step falls back to guessing at load_csv arguments.
    """
    doc = docs["propose_ingest_spec"].lower()
    assert "loads nothing" in doc or "nothing is loaded" in doc


def test_propose_tells_claude_not_to_answer_the_questions_itself(docs):
    """
    The Done-When clause depends on this sentence. The tool returning
    questions is useless if Claude answers them on the user's behalf.
    """
    doc = docs["propose_ingest_spec"].lower()
    assert "do not guess" in doc
    assert "ask" in doc


def test_propose_explains_when_to_reach_for_it(docs):
    doc = docs["propose_ingest_spec"].lower()
    for cue in ("merged", "header"):
        assert cue in doc, f"propose_ingest_spec should mention {cue!r}"


def test_confirm_requires_the_user_to_have_seen_the_spec(docs):
    """The two-step exists so the shape is visible before the data moves."""
    doc = docs["confirm_ingest_spec"].lower()
    assert "only call this once the user has seen" in doc


def test_confirm_says_edits_are_honoured(docs):
    doc = docs["confirm_ingest_spec"].lower()
    assert "edit" in doc


def test_confirm_mentions_that_coercion_failures_are_reported(docs):
    doc = docs["confirm_ingest_spec"].lower()
    assert "coercion" in doc or "null" in doc


def test_reset_workspace_tells_claude_to_ask_first(docs):
    doc = docs["reset_workspace"].lower()
    assert "cannot be undone" in doc
    assert "confirm" in doc


def test_the_direct_loaders_point_at_the_conversation(docs):
    """
    Otherwise Claude reaches for load_csv with guessed arguments, which is the
    behaviour Phase 3 exists to replace.
    """
    for name in ("load_csv", "load_excel"):
        assert "propose_ingest_spec" in docs[name], (
            f"{name} should say propose_ingest_spec works these out"
        )


def test_preview_file_points_at_the_conversation_too(docs):
    assert "propose_ingest_spec" in docs["preview_file"]


def test_build_report_tells_claude_the_question_is_the_users_words(docs):
    """P12-D2: nothing records why a dataset was loaded, so this is the one section with no
    record behind it. An agent that tidies the question writes the wrong report."""
    doc = docs["build_report"]
    assert "in their words" in doc
    assert "Do not invent one" in doc


def test_build_report_says_it_computes_nothing(docs):
    """Otherwise an agent treats it as expensive and defers it, or reruns analyses first."""
    assert "computes nothing" in docs["build_report"]


def test_build_report_says_an_empty_section_still_appears(docs):
    doc = docs["build_report"]
    assert "always has nine" in doc
    assert "needed no cleaning" in doc


def test_build_report_tells_claude_it_cannot_read_the_file(docs):
    """Rule 4. The same instruction render_chart carries, for the same reason."""
    assert "You cannot read the file" in docs["build_report"]


def test_query_source_is_preferred_over_copying(docs):
    assert "query_source" in docs["load_postgres_table"]


# --------------------------------------------------------------------------
# annotations
# --------------------------------------------------------------------------

def _annotations() -> dict[str, str]:
    """{tool name: the constant used in @mcp.tool(annotations=...)}"""
    tree = ast.parse(SERVER.read_text())
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            target = dec.func
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                for kw in dec.keywords:
                    if kw.arg == "annotations" and isinstance(kw.value, ast.Name):
                        out[node.name] = kw.value.id
    return out


def test_proposing_is_annotated_read_only():
    """Claude Desktop should not ask permission to think."""
    assert _annotations()["propose_ingest_spec"] == "READ_ONLY"


def test_confirming_is_not_annotated_read_only():
    assert _annotations()["confirm_ingest_spec"] == "WRITES"


def test_reset_workspace_is_the_only_destructive_tool():
    ann = _annotations()
    destructive = sorted(n for n, a in ann.items() if a == "DESTRUCTIVE")
    assert destructive == ["reset_workspace"]
