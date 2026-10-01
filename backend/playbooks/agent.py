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
import re
import time
from typing import Callable

from backend.engine.webapp.llm import parse_json
from backend.engine.webapp.verify import verify
from backend.llm.provider import LLM, Usage
from backend.packs.loader import load_all, merge
from backend.rules import interpret
from backend.services.sessions import ServiceError
from backend.tools import registry

MAX_FIGURES_TO_MODEL = 30
FALLBACK_MAX_CALLS = 4

PLANNER = """You route an analytics question to a playbook. Reply with ONE JSON object:
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

FALLBACK = """You answer with tools. Reply with ONE JSON object: {"calls": [{"call": "<tool name>",
"params": {...}}, ...], "then": "answer"} to run every call the question needs at once (add
"then": "answer" when those will be enough), or {"done": true} when the results so far answer
it. Column names come from the question and the contract. You never compute numbers and never
write filters. Tools (JSON schemas):
"""


def _stem(w: str) -> str:
    """Crude suffix stem for routing only: dropped/drops/dropping -> drop."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    return w[:-1] if len(w) > 3 and w[-1] == w[-2] else w


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
        usable, _, states = self.playbooks_split(d)
        return usable, states

    def playbooks_split(self, d: dict) -> tuple[dict, dict, dict]:
        """usable playbooks; blocked ones {id: (playbook, [what is missing])}; tool states."""
        m = merge(load_all(), d["domains"])
        states = {s.tool_id: s for s in self.ds.tools_states(d)}
        bound, _ = self.ds._bound(d)
        usable, blocked = {}, {}
        for b in m.playbooks.values():
            lead = states.get(b.steps[0].tool)
            if lead is None or lead.status == "needs_domain":
                continue                       # another domain's playbook: not offered at all
            missing = [f"data for '{c}'" for c in b.requires_concepts if not bound.get(c)]
            missing += [f"the approved metric '{t}'" for t in b.requires_metrics
                        if t not in d["metrics"]]
            if lead.status != "active" and not missing:
                missing.append(f"data the first step needs: {lead.missing_concepts}")
            if missing:
                blocked[b.id] = (b, missing)
            else:
                usable[b.id] = b
        return usable, blocked, states

    @staticmethod
    def route(question: str, candidates: dict) -> tuple[object, dict] | None:
        """Pick a playbook without the planner when the question leaves no doubt (D-B9-2)."""
        q = question.lower()
        words = {_stem(w) for w in re.findall(r"[a-z0-9]+", q)}
        scored = []
        for b in candidates.values():
            hits = {p for p in b.patterns
                    if (_stem(p.lower()) in words if p.isalnum() else
                        re.search(rf"\b{re.escape(p.lower())}\b", q))}
            scored.append((len(hits), b))
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] < 2 or (len(scored) > 1 and
                                               scored[0][0] - scored[1][0] < 2):
            return None
        b = scored[0][1]
        slots: dict = {}
        months = sorted(set(re.findall(r"\b(20\d\d-(?:0[1-9]|1[0-2]))\b", question)))
        days = sorted(set(re.findall(r"\b(20\d\d-\d\d-\d\d)\b", question)))
        months = [x for x in months if not any(d.startswith(x + "-") for d in days)]
        for slot in b.slots:
            if slot in ("period", "baseline") and len(months) == 2:
                slots[slot] = months[1] if slot == "period" else months[0]
            elif slot == "start" and len(days) == 1:
                slots[slot] = days[0]
            else:
                return None                    # a slot the question does not pin: ask the model
        return b, slots

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

    def run_core(self, dataset_id, params, emit, trace, skipped):
        analysis = str((params or {}).get("analysis", ""))
        inner = (params or {}).get("params") or {}
        t0 = time.perf_counter()
        try:
            res = self.ds.run_core(dataset_id, analysis, inner)
            trace.append(res)
            emit("tool_call", {"tool_id": f"core.{analysis}", "params": inner, "status": "ok",
                               "ms": round((time.perf_counter() - t0) * 1000)})
            return res
        except ServiceError as e:
            skipped.append({"tool_id": f"core.{analysis}", "code": e.code, "reason": e.message})
            emit("tool_call", {"tool_id": f"core.{analysis}", "params": inner,
                               "status": "skipped", "code": e.code, "reason": e.message[:300]})
            return None

    def fallback(self, question, states, dataset_id, emit, trace, skipped, usage) -> int:
        """Tool calling over the ACTIVE tools: core_analyze (always) + active domain tools.
        A reply may carry several calls ({"calls": [...]}); {"done": true} ends it."""
        active = [s for s in states.values() if s.status == "active"]
        schemas = registry.llm_schemas(active)
        system = FALLBACK + json.dumps(schemas, separators=(",", ":"))
        calls, seen = 0, question
        for _ in range(FALLBACK_MAX_CALLS):
            try:
                got = parse_json(self._call(usage, "tool_choice", system, seen))
            except ValueError:
                break
            batch = got.get("calls") or ([{"call": got["call"], "params": got.get("params")}]
                                         if got.get("call") else [])
            if got.get("done") or not batch:
                break
            for item in batch[:FALLBACK_MAX_CALLS - calls]:
                name = str(item.get("call") or "")
                params = item.get("params") or {}
                if name == "core_analyze":
                    res = self.run_core(dataset_id, params, emit, trace, skipped)
                    label = f"core.{params.get('analysis')}"
                else:
                    label = name.replace("_", ".", 1)
                    if label not in states or states[label].status != "active":
                        skipped.append({"tool_id": label, "code": "not_active",
                                        "reason": "not an active tool for this data"})
                        continue
                    res = self.run_tool(dataset_id, label, params, emit, trace, skipped)
                calls += 1
                seen += "\n\n" + (render(res) if res else f"{label} could not run: "
                                  f"{skipped[-1]['reason'][:200]}")
            if got.get("then") == "answer" or calls >= FALLBACK_MAX_CALLS:
                break
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
        usable, blocked, states = self.playbooks_split(d)
        routed = self.route(question, {**usable, **{k: v[0] for k, v in blocked.items()}})
        if routed is not None and routed[0].id in blocked:
            b, missing = blocked[routed[0].id]
            text = (f"Before I can answer this ({b.description}), this dataset needs "
                    + "; ".join(missing) + ". " + b.recovery).strip()
            emit("plan", {"playbook": b.id, "slots": {}, "steps": [], "routed_by": "rules",
                          "blocked": missing, "recovery": b.recovery})
            emit("answer", {"text": text, "flags": []})
            return {"text": text, "flags": [], "playbook": b.id, "results": [], "skipped": [],
                    "blocked": missing,
                    "usage": {"llm_calls": 0, "tool_calls": 0, "tokens_in_est": 0,
                              "tokens_out_est": 0, "per_call": [],
                              "model": getattr(self.llm, "name", "")}}
        if routed is not None:
            pb, slots = routed
        else:
            pb, slots = self.plan(question, usable, usage)
        if pb is not None:
            emit("plan", {"playbook": pb.id, "slots": slots,
                          "steps": [s.tool for s in pb.steps],
                          "routed_by": "rules" if routed is not None else "planner"})
            calls = self.run_playbook(pb, slots, dataset_id, emit, trace, skipped)
            rules = set(pb.rules)
            if not trace and pb.recovery:
                skipped.append({"tool_id": pb.id, "code": "recovery", "reason": pb.recovery})
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
