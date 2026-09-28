"""The v2 answer loop. The LLM plans and explains; the engine computes; rules check.

  1. planner   (1 LLM call)  picks a playbook and fills its slots -- or none
  2. engine    (0 LLM calls) runs the playbook's tool steps, in order, within its budget
     fallback  no playbook: v1-style tool calling over the ACTIVE tools only, few calls
  3. explainer (1 LLM call)  writes the answer from the results, computing nothing
  4. checks    figure check (v1 verify.py) + interpretation rules (backend/rules)
               -> one correction call if either fails; what is left is shown as flags

The LLM never sees rows and never writes SQL: it sees tool results (figures, caveats).
"""
from __future__ import annotations

import json
import time
from typing import Callable

from backend.engine.webapp.llm import parse_json
from backend.engine.webapp.verify import verify
from backend.llm.provider import LLM, Usage, tokens
from backend.packs.loader import load_all, merge
from backend.rules import interpret
from backend.services.sessions import ServiceError
from backend.tools import registry

MAX_FIGURES_TO_MODEL = 30
FALLBACK_MAX_CALLS = 4

PLANNER = """You route a marketing analytics question to a playbook. Reply with ONE JSON object:
{"playbook": "<id>" | null, "slots": {"<slot>": "<value>"}}
Pick a playbook only if it answers the question; fill every slot it lists from the question
(months as YYYY-MM, dates as YYYY-MM-DD). If none fits, {"playbook": null, "slots": {}}.
Playbooks:
"""

EXPLAINER = """You explain analysis results to a marketer, in plain words, in under 180 words.
Rules: use ONLY figures that appear in the results, copied exactly; never compute a new number;
never add up rates or conversions from different sources; say "associated with", not
"caused", unless the results say a holdout was marked; name every caveat that starts with
festival_confound, measurement_change, or mentions small groups; if a step was skipped, say
what it would need. Results:
"""

FALLBACK = """You answer with tools. Reply with ONE JSON object per turn: either
{"call": "<tool_id>", "params": {...}} to run a tool, or {"done": true} when the results so far
answer the question. You never compute numbers. Tools (JSON schemas):
"""


def render(result: dict) -> str:
    """A tool result as the model reads it: figures (capped) and caveats. No rows."""
    lines = [f"## {result['tool_id']}: {result.get('summary', '')}"]
    for f in result.get("figures", [])[:MAX_FIGURES_TO_MODEL]:
        v = "suppressed (group too small)" if f["value"] is None else f["value"]
        lines.append(f"- {f['name']}: {v}" + (f" ({f['unit']})" if f.get("unit") else ""))
    lines += [f"caveat: {c}" for c in result.get("caveats", [])]
    return "\n".join(lines)


class Agent:
    def __init__(self, datasets, llm: LLM):
        self.ds, self.llm = datasets, llm

    def _call(self, usage: Usage, purpose: str, system: str, user: str) -> str:
        reply = self.llm.complete(system, user)
        usage.add(purpose, system, user, reply)
        return reply

    # --- 1. plan -----------------------------------------------------------------------------
    def playbooks_for(self, d: dict) -> tuple[dict, dict]:
        m = merge(load_all(), d["domains"])
        states = {s.tool_id: s for s in self.ds.tools_states(d)}
        usable = {b.id: b for b in m.playbooks.values()
                  if states.get(b.steps[0].tool) and states[b.steps[0].tool].status == "active"}
        return usable, states

    def plan(self, question: str, usable: dict, usage: Usage) -> tuple[object, dict]:
        if not usable:
            return None, {}
        listing = "\n".join(f"- {b.id}: {b.description}"
                            + (f" slots: {json.dumps(b.slots)}" if b.slots else "")
                            for b in usable.values())
        try:
            got = parse_json(self._call(usage, "planner", PLANNER + listing, question))
        except ValueError:
            return None, {}
        pb = usable.get(got.get("playbook") or "")
        return pb, (got.get("slots") or {}) if pb else {}

    # --- 2. run ------------------------------------------------------------------------------
    def run_tool(self, dataset_id: str, tool_id: str, params: dict, emit, trace, skipped):
        t0 = time.perf_counter()
        try:
            res = self.ds.run_tool(tool_id, dataset_id, params)
            trace.append(res)
            emit("tool_call", {"tool_id": tool_id, "params": params, "status": "ok",
                               "ms": round((time.perf_counter() - t0) * 1000)})
            return res
        except ServiceError as e:
            skipped.append({"tool_id": tool_id, "code": e.code, "reason": e.message})
            emit("tool_call", {"tool_id": tool_id, "params": params, "status": "skipped",
                               "code": e.code, "reason": e.message[:300],
                               "ms": round((time.perf_counter() - t0) * 1000)})
            return None

    def run_playbook(self, pb, slots, dataset_id, emit, trace, skipped) -> int:
        calls = 0
        for step in pb.steps[:pb.max_tool_calls]:
            params = {k: (slots.get(v[6:]) if isinstance(v, str) and v.startswith("@slot:")
                          else v) for k, v in step.params.items()}
            params = {k: v for k, v in params.items() if v is not None}
            res = self.run_tool(dataset_id, step.tool, params, emit, trace, skipped)
            calls += 1
            if res is None and not step.optional:
                break                              # the lead step failed: nothing to build on
        return calls

    def fallback(self, question, states, dataset_id, emit, trace, skipped, usage) -> int:
        active = [s for s in states.values() if s.status == "active" and s.pack != "core"]
        schemas = registry.llm_schemas(active)
        if not schemas:
            return 0
        system = FALLBACK + json.dumps(schemas, separators=(",", ":"))
        calls, seen = 0, question
        for _ in range(FALLBACK_MAX_CALLS):
            try:
                got = parse_json(self._call(usage, "tool_choice", system, seen))
            except ValueError:
                break
            tool = str(got.get("call") or "").replace("_", ".", 1)
            if got.get("done") or tool not in states:
                break
            res = self.run_tool(dataset_id, tool, got.get("params") or {}, emit, trace, skipped)
            calls += 1
            seen += "\n\n" + (render(res) if res else f"{tool} could not run: "
                              f"{skipped[-1]['reason'][:200]}")
        return calls

    # --- 3 + 4. explain and check ------------------------------------------------------------
    def explain(self, question, trace, skipped, rules, usage, emit) -> tuple[str, list[str]]:
        body = "\n\n".join(render(r) for r in trace) or "(no tool ran)"
        if skipped:
            body += "\n\nSkipped: " + "; ".join(f"{s['tool_id']} ({s['reason'][:160]})"
                                                for s in skipped)
        text = self._call(usage, "explainer", EXPLAINER + body, question)
        replies = [render(r) for r in trace]
        fc = verify(text, replies)
        viol = interpret.check(text, trace, rules)
        emit("figure_check", {"status": "passed" if fc.clean else "failed",
                              "checked": fc.checked, "detail": fc.summary()})
        for v in viol:
            emit("interpretation_check", {"rule": v.rule, "status": "violated",
                                          "detail": v.detail})
        flags = []
        if not fc.clean or viol:                                # ONE correction round
            fix = [fc.correction()] if not fc.clean else []
            fix += ["[system] Also fix: " + v.text() for v in viol]
            text = self._call(usage, "correction", EXPLAINER + body,
                              question + "\n\nYour previous answer:\n" + text + "\n\n"
                              + "\n".join(fix))
            fc = verify(text, replies)
            viol = interpret.check(text, trace, rules)
            emit("figure_check", {"status": "corrected" if fc.clean else "flagged",
                                  "checked": fc.checked, "detail": fc.summary()})
            for v in viol:
                emit("interpretation_check", {"rule": v.rule, "status": "flagged",
                                              "detail": v.detail})
            flags = ([f"figure_check: {fc.summary()}"] if not fc.clean else []) + \
                    [v.text() for v in viol]
        else:
            emit("interpretation_check", {"rule": "all", "status": "passed", "detail": ""})
        return text, flags

    # --- the turn ------------------------------------------------------------------------------
    def answer(self, dataset_id: str, question: str, emit: Callable[[str, dict], None]) -> dict:
        usage, trace, skipped = Usage(), [], []
        d = self.ds._get(dataset_id)
        usable, states = self.playbooks_for(d)
        pb, slots = self.plan(question, usable, usage)
        if pb is not None:
            emit("plan", {"playbook": pb.id, "slots": slots,
                          "steps": [s.tool for s in pb.steps]})
            calls = self.run_playbook(pb, slots, dataset_id, emit, trace, skipped)
            rules = set(pb.rules)
        else:
            emit("plan", {"playbook": None, "steps": [], "mode": "tool_calling"})
            calls = self.fallback(question, states, dataset_id, emit, trace, skipped, usage)
            rules = set()
        text, flags = self.explain(question, trace, skipped, rules, usage, emit)
        emit("answer", {"text": text, "flags": flags})
        return {"text": text, "flags": flags, "playbook": pb.id if pb else None,
                "results": trace, "skipped": skipped,
                "usage": {"llm_calls": usage.calls, "tool_calls": calls,
                          "tokens_in_est": usage.tokens_in, "tokens_out_est": usage.tokens_out,
                          "per_call": usage.per_call, "model": getattr(self.llm, "name", "")}}


def runner(datasets, llm: LLM):
    """A TurnService runner: one turn, answered once."""
    agent = Agent(datasets, llm)

    def run(turn: dict, emit) -> dict:
        return agent.answer(turn["dataset_id"], turn["question"], emit)
    return run


def token_estimate(text: str) -> int:
    return tokens(text)
