"""The docstring of compute_analysis, pinned.

Phase 8, Step 9b. Run from the repo root:

    uv run pytest tests/test_analysis_tool_docs.py -q

test_contract_tool_docs.py's own note says a later phase's tool should add a
file rather than lengthen that one indefinitely. This is that file.

Parsed with `ast`, never imported: importing server.py starts FastMCP and
requires the whole dependency stack, and parsing needs only the file.

The sentences asserted here are the ones the conversation depends on. If one
reads as clumsy, rewrite it AND the assertion together, deliberately.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVER = Path("engine/server.py")


@pytest.fixture(scope="module")
def tool() -> ast.FunctionDef:
    if not SERVER.exists():
        pytest.skip(f"{SERVER} not found; run from the repository root")
    tree = ast.parse(SERVER.read_text())
    found = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "compute_analysis"]
    if not found:
        pytest.skip("compute_analysis is not registered yet; see Step 9b")
    return found[0]


@pytest.fixture(scope="module")
def doc(tool) -> str:
    return ast.get_docstring(tool) or ""


def test_it_names_every_analysis_and_the_arguments_each_takes(doc):
    """The schema says a parameter exists; only the docstring says which
    analysis it belongs to. An agent passing `column` to top_n gets a refusal
    it could have avoided by reading one line."""
    for name in ("summary_stats", "distribution", "frequency", "cross_tab",
                 "top_n", "group_compare", "pareto", "concentration",
                 "ranking_shift"):
        assert name in doc, f"{name} is not named in the docstring"


def test_it_distinguishes_itself_from_run_analysis(doc):
    assert "run_analysis" in doc
    assert "computes nothing" in doc


def test_it_says_an_unknown_name_comes_back_with_the_list(doc):
    """The Phase 8 Done-When. An agent that expects a traceback retries the
    same guess; one that expects a list reads it."""
    assert "list" in doc.lower()


def test_it_says_the_contract_decides_which_columns_can_be_named(doc):
    assert "contract" in doc.lower()
    assert "declare" in doc.lower()


def test_it_points_at_read_result_file_for_the_rest(doc):
    """F7: a path travels with an instruction to open it, or it does not
    travel."""
    assert "read_result_file" in doc


def _wanted() -> dict[str, list[str]]:
    """Every parameter some registered analysis takes, and which take it.

    Read from the registry rather than typed out. The version of this test that
    listed fifteen names by hand and compared with `<=` passed for two phases
    while six parameters were missing from the signature and three more were
    declared and never forwarded -- a subset assertion against a hand-typed
    roster cannot fail for the thing it exists to catch. P10-D39 removed that
    shape from the tier test and C72 removed it from the docstring roster;
    this was the third place it survived.
    """
    import inspect

    import backend.engine.analysis as _pkg  # noqa: F401  -- registers by import
    from backend.engine.analysis.registry import REGISTRY

    wanted: dict[str, list[str]] = {}
    for name, analysis in REGISTRY.items():
        if analysis.surface != "v1":   # v2 B4 (D-B4-2): reached through v2 domain tools only
            continue
        for param, spec in inspect.signature(analysis.run).parameters.items():
            if param in ("con", "gate", "scope") or spec.kind is spec.VAR_KEYWORD:
                continue
            wanted.setdefault(param, []).append(name)
    return wanted


def test_every_parameter_any_analysis_takes_is_declared(tool):
    """FastMCP builds the schema from the signature, so **params would expose
    a tool the agent cannot pass a column to. The union of every registered
    signature is declared explicitly, and compute_analysis drops the ones
    nobody gave."""
    wanted = _wanted()
    names = {a.arg for a in tool.args.args}
    missing = sorted(set(wanted) - names)
    assert not missing, (
        "compute_analysis does not declare "
        + "; ".join(f"{p} (wanted by {', '.join(sorted(wanted[p]))})" for p in missing)
        + ". FastMCP builds the schema from this signature, so a parameter absent "
        "here is one no caller can pass."
    )
    assert {"dataset_name", "analysis_type"} <= names
    assert tool.args.kwarg is None, "a **kwargs here would expose no schema"


def test_every_declared_parameter_reaches_the_analysis_layer(tool):
    """Declaring a parameter and forwarding it are two edits, and they were made
    apart. A parameter in the schema that the body drops reaches the analysis
    missing, the analysis raises TypeError, and tools.py turns that into
    ANALYSIS_PARAMS_INVALID -- "was called with arguments it cannot take."
    The caller passed it. The server dropped it. The refusal names the caller.
    """
    call = next(
        (n for n in ast.walk(tool)
         if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "compute_analysis"),
        None,
    )
    assert call is not None, "compute_analysis does not delegate to the analysis layer"
    forwarded = {k.arg for k in call.keywords}
    declared = {a.arg for a in tool.args.args} - {
        "dataset_name", "analysis_type", "workspace_id",
    }
    wanted = _wanted()
    dropped = sorted(declared - forwarded)
    assert not dropped, (
        "declared by compute_analysis and never passed on: "
        + "; ".join(f"{p} (needed by {', '.join(sorted(wanted.get(p, ['nothing'])))})"
                    for p in dropped)
    )


def test_it_is_annotated_read_only(tool):
    """It writes a result file, as profile_dataset does, and that one is
    READ_ONLY too: the annotation means it does not modify the user's data."""
    dec = next(d for d in tool.decorator_list if isinstance(d, ast.Call))
    annotations = [kw.value.id for kw in dec.keywords
                   if kw.arg == "annotations" and isinstance(kw.value, ast.Name)]
    assert annotations == ["READ_ONLY"]


@pytest.fixture(scope="module")
def chart_tool() -> ast.FunctionDef:
    tree = ast.parse(SERVER.read_text())
    found = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "render_chart"]
    if not found:
        pytest.skip("render_chart is not registered yet; see Phase 11 Step 3")
    return found[0]


def test_render_chart_declares_every_parameter_an_analysis_takes(chart_tool):
    """It runs the same analyses through the same gate, so it needs the same roster.
    Derived from the registry for the reason C83 records: a hand-typed one goes stale."""
    wanted = _wanted()
    names = {a.arg for a in chart_tool.args.args}
    missing = sorted(set(wanted) - names)
    assert not missing, (
        "render_chart does not declare "
        + "; ".join(f"{p} (wanted by {', '.join(sorted(wanted[p]))})" for p in missing)
    )
    assert {"dataset_name", "analysis_type", "chart"} <= names
    assert chart_tool.args.kwarg is None, "a **kwargs here would expose no schema"


def test_render_chart_forwards_every_parameter_it_declares(chart_tool):
    """C83 was declared-and-dropped in one tool. This is the guard on the second one,
    written in the same step that added it rather than two phases later."""
    call = next(
        (n for n in ast.walk(chart_tool)
         if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "render_chart"),
        None,
    )
    assert call is not None, "render_chart does not delegate to the analysis layer"
    forwarded = {k.arg for k in call.keywords}
    declared = {a.arg for a in chart_tool.args.args} - {
        "dataset_name", "analysis_type", "chart", "workspace_id",
    }
    dropped = sorted(declared - forwarded)
    assert not dropped, f"declared by render_chart and never passed on: {', '.join(dropped)}"


def test_render_chart_tells_the_agent_it_cannot_see_the_image(chart_tool):
    """Rule 4 is the whole reason this tool returns prose. The docstring has to say so,
    because the model reads the docstring and not the guide."""
    doc = ast.get_docstring(chart_tool) or ""
    assert "cannot see" in doc
    assert "Do not describe it from the filename" in doc
    for kind in ("line", "bar", "grouped_bar", "scatter", "histogram", "box",
                 "heatmap", "waterfall"):
        assert kind in doc, f"{kind} is not named in the docstring"
