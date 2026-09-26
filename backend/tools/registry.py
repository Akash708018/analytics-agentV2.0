"""Tool registry: core tools (always on) + domain tools (gated), and the LLM schemas.

Gating (a domain tool is `active` only when all hold):
  1. its pack's domain was CONFIRMED by a person (a detector guess never enables it);
  2. every required concept is bound to a column of the dataset;
  3. every required source was detected.
Otherwise it is `needs_domain` (names the domain) or `needs_data` (names what is missing).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import backend.engine.analysis  # noqa: F401 -- registers the analyses
from backend.engine.analysis import registry as analyses
from backend.packs.loader import Merged, load_all, merge

MAX_DESCRIPTION = 200

# The non-analysis core steps a UI can run directly (no LLM schema: the person drives them).
CORE_STEPS = [("core.profile", "Profile the data"), ("core.clean", "Clean the data"),
              ("core.contract", "Agree the dataset contract")]


@dataclass
class ToolState:
    tool_id: str
    ui_label: str
    status: str
    needs_domain: str | None = None
    missing_concepts: list[str] = field(default_factory=list)
    description: str = ""
    pack: str = "core"


def _label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def core_tools() -> list[ToolState]:
    out = [ToolState(t, label, "active") for t, label in CORE_STEPS]
    for name, tier, summary in analyses.catalogue():
        out.append(ToolState(f"core.{name}", _label(name), "active",
                             description=summary[:MAX_DESCRIPTION]))
    return out


def statuses(confirmed: list[str], concepts: set[str], sources: set[str],
             merged: Merged | None = None) -> list[ToolState]:
    packs = load_all()
    merged = merged or merge(packs, [p for p in packs if p != "core"])
    out = core_tools()
    for tool in merged.tools.values():
        pack_id = tool.id.split(".", 1)[0]
        st = ToolState(tool.id, tool.ui_label, "active", description=tool.description,
                       pack=pack_id)
        if pack_id not in confirmed:
            st.status, st.needs_domain = "needs_domain", pack_id
        else:
            missing = [c for c in tool.required_concepts if c not in concepts]
            missing += [f"source:{s}" for s in tool.required_sources if s not in sources]
            if missing:
                st.status, st.missing_concepts = "needs_data", missing
        out.append(st)
    return out


def llm_schemas(states: list[ToolState]) -> list[dict]:
    """Function schemas for the ACTIVE tools only. Core analyses share one schema."""
    names = [s.tool_id.split(".", 1)[1] for s in states
             if s.pack == "core" and s.status == "active" and s.description]
    schemas = [{
        "name": "core_analyze",
        "description": "Run a contract-gated analysis on the confirmed dataset. The engine "
                       "computes every number; you explain them.",
        "parameters": {"type": "object", "required": ["analysis"], "properties": {
            "analysis": {"type": "string", "enum": names},
            "params": {"type": "object", "description": "Analysis parameters (column names "
                                                        "from the contract)"}}}}]
    for s in states:
        if s.pack != "core" and s.status == "active":
            schemas.append({
                "name": s.tool_id.replace(".", "_"),
                "description": s.description,
                "parameters": {"type": "object", "properties": {
                    "period": {"type": "string", "description": "e.g. 2026-09 or a range"},
                    "where": {"type": "string", "description": "optional approved filter"}}}})
    return schemas


def schema_tokens(schemas: list[dict]) -> int:
    """Estimated tokens for the schemas as sent (compact JSON, ~4 characters per token).

    An estimate, labelled as one: no tokenizer is a dependency, and providers tokenize
    differently. It is for comparing configurations, not for billing.
    """
    return round(len(json.dumps(schemas, separators=(",", ":"))) / 4)


# Engine analyses that later milestones add (B4). A tool may name one only once it exists.
def compile_errors(merged: Merged) -> list[str]:
    """Every domain tool must compile to engine analyses: each part of `base_analysis`
    ("a + b" composes two) is a registered analysis. Returns the problems, empty if none."""
    known = {name for name, _, _ in analyses.catalogue()}
    bad = []
    for t in merged.tools.values():
        parts = [p.strip() for p in t.base_analysis.split("+")]
        bad += [f"{t.id}: {p!r} is not an engine analysis" for p in parts if p not in known]
    for tpl in merged.templates.values():
        if tpl.shape not in {"comparison", "ratio_of_sums", "weighted_mean", "distinct_count",
                             "percentile", "semi_additive", "derived"}:
            bad.append(f"{tpl.id}: shape {tpl.shape} has no engine form")
    return bad
