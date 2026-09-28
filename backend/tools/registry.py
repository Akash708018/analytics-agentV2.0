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
    tool: object = None      # the pack Tool, for domain tools


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
                       pack=pack_id, tool=tool)
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
            t = s.tool
            props = {k: {"type": "string"} for k in (*t.params_required, *t.slots)}
            schema = {"type": "object", "properties": props}
            if t.params_required:
                schema["required"] = list(t.params_required)
            schemas.append({"name": s.tool_id.replace(".", "_"), "description": s.description,
                            "parameters": schema})
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
    known = {name for name, _, _ in analyses.catalogue(surface=None)}
    bad = []
    for t in merged.tools.values():
        if t.kind == "pipeline":
            continue                    # a service run, compiled nowhere near the engine
        parts = [p.strip() for p in t.base_analysis.split("+")]
        bad += [f"{t.id}: {p!r} is not an engine analysis" for p in parts if p not in known]
        if t.steps and {s.analysis for s in t.steps} != set(parts):
            bad.append(f"{t.id}: base_analysis {parts} != its steps "
                       f"{sorted({s.analysis for s in t.steps})}")
        for s in t.steps:
            refs = [v for v in s.params.values() if isinstance(v, str) and v.startswith("@")]
            for f in ([s.filter] if isinstance(s.filter, dict) else s.filter or []):
                if f.get("date_range"):
                    refs.append(f["date_range"])
                c = str(f.get("concept", ""))
                refs.append(c if c.startswith("@") else "@" + c)
            for r in refs:
                ref = r[1:]
                kind, _, rest = ref.partition(":")
                ok = (ref in merged.concepts or ref in t.slots or ref == "revenue"
                      or (kind == "metric" and rest in merged.templates)
                      or (kind == "param" and (rest.split("=")[0] in t.params_required
                                               or "=" in rest))
                      or kind == "param?"
                      or (kind == "festival" and rest in ("this", "last")))
                if not ok:
                    bad.append(f"{t.id}: {r!r} names nothing (concept, slot, metric, param)")
        for slot, concepts in t.slots.items():
            bad += [f"{t.id}: slot {slot} names unknown concept {c}" for c in concepts
                    if c not in merged.concepts]
    for tpl in merged.templates.values():
        if tpl.shape not in {"comparison", "ratio_of_sums", "weighted_mean", "distinct_count",
                             "percentile", "semi_additive", "derived"}:
            bad.append(f"{tpl.id}: shape {tpl.shape} has no engine form")
    return bad
