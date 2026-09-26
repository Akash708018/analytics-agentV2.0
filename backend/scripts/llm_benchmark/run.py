"""The LLM analyst benchmark: Gemini against Groq on one deterministic engine (handoff Part B).

    uv run python scripts/llm_benchmark/run.py setup                 # load the ten datasets
    uv run python scripts/llm_benchmark/run.py planner  gemini|groq  # 120 questions, no tools
    uv run python scripts/llm_benchmark/run.py research gemini|groq  # 20 frozen cases
    uv run python scripts/llm_benchmark/run.py e2e      gemini|groq  # 40 questions, real tools
    uv run python scripts/llm_benchmark/run.py report

Fairness: both providers get the same system prompt, context, catalogue, question, tool results,
budget (10 analytical calls), temperature 0, JSON output, and the product's own request/retry
policy (webapp/llm.py _request). Nothing is repaired: a plan is executed as the model wrote it.
Every item is checkpointed as one JSON line; a provider error or rate limit leaves the item to
be retried on the next run, and is never scored as a reasoning failure.
Keys are read from the environment or the gitignored .env and are never written anywhere.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.analysis import registry  # noqa: E402
from backend.engine.webapp import llm  # noqa: E402
from domain_benchmark.domains import DOMAINS, contract_kwargs, write_dataset  # noqa: E402

import questions as QB  # noqa: E402
import research as RB  # noqa: E402

OUT = ROOT / "docs" / "benchmark" / "llm_benchmark"
STATE = Path(os.environ.get("LLM_BENCH_DATA", "/tmp/llm_benchmark"))
ROWS = 10_000
SEED = 20260924
MAX_CALLS = 10
RESULT_CHARS = 6000
# Seconds between two requests to one provider: free tiers count requests per minute. The wait
# is outside every measured latency.
PACE = {"gemini": float(os.environ.get("LLM_PACE_GEMINI", "13")),
        "groq": float(os.environ.get("LLM_PACE_GROQ", "3"))}
_last: dict[str, float] = {}
# Smoke runs name a few ids here (and a scratch LLM_BENCH_DATA); a real run leaves it unset.
ONLY = set(filter(None, os.environ.get("LLM_ONLY", "").split(",")))

# --------------------------------------------------------------------------- context


def catalogue() -> str:
    lines = []
    for name, a in registry.REGISTRY.items():
        ps = list(inspect.signature(a.run).parameters.values())[3:]
        req = [p.name for p in ps if p.default is inspect.Parameter.empty and p.name != "params"]
        opt = [p.name for p in ps if p.default is not inspect.Parameter.empty]
        summary = a.summary.split(". ")[0].rstrip(".")
        lines.append(f"- {name}: {summary}. required: {', '.join(req) or 'none'}; "
                     f"optional: {', '.join(opt) or 'none'}")
    return "\n".join(lines)


RULES = """Rules the engine enforces:
- Every measure/against argument must be a DECLARED MEASURE; every dimension/rows/columns/column/
  second_dimension argument must be a DECLARED DIMENSION; entity is a declared id dimension.
- Identifiers (ids) are dimensions, never measures: they can be counted, never summed or averaged.
- A measure whose aggregation is 'none' is a per-row value (a ratio): it cannot be totalled.
- pareto, concentration and growth_decomposition need an additive measure (sum or count).
- Periods: month labels are YYYY-MM, quarters YYYY-Qn (pass grain="quarter"), years YYYY
  (grain="year"). ranking_shift takes before_start/before_end/after_start/after_end dates.
- hypothesis_test/effect_size compare groups of one dimension (method="rank" for a rank test).
- The engine computes every number. You never compute or estimate a metric yourself.
- Nothing outside the dataset is known unless it is given to you."""


def dataset_context(domain: str) -> str:
    d = DOMAINS[domain]
    undeclared = [c for c in d.columns if c not in d.measures and c not in d.dimensions]
    return (f"DATASET '{domain}' ({ROWS:,} generated rows before duplicate removal; one row = "
            f"one {domain} record). Date column: {d.date}; analysis window {d.window[0]} to "
            f"{d.window[1]}.\n"
            f"DECLARED MEASURES (aggregation): "
            + ", ".join(f"{m} ({a})" for m, a in d.measures.items()) + "\n"
            f"DECLARED DIMENSIONS: {', '.join(d.dimensions)}\n"
            f"OTHER COLUMNS (not usable in analyses): {', '.join(undeclared)}\n"
            + (f"EXCLUDED ROWS: {'; '.join(e['rule'] for e in d.exclusions)}\n"
               if d.exclusions else ""))


INTENT_LIST = ", ".join(QB.INTENTS)

SYSTEM_PLANNER = f"""You are the planning layer of an analytics agent. A deterministic engine
runs the analyses; you choose them. Answer with ONE JSON object and nothing else:
{{"intent": one of [{INTENT_LIST}],
 "metrics": [declared measures the question is about],
 "dimensions": [declared dimensions the question is about],
 "time_range": null or a string, "comparison_period": null or a string,
 "hypotheses": [short strings],
 "analysis_plan": [{{"analysis": name, "params": {{argument: value}}, "purpose": string}}],
 "research_required": true if the question needs facts from outside the dataset,
 "insufficient_data": true if the dataset cannot answer the question soundly,
 "stop_conditions": [short strings]}}
Use intent "refuse" when the request is invalid (for example it sums identifiers or uses a
column that does not exist); then leave analysis_plan empty or keep only what is valid.
At most {MAX_CALLS} analyses.

ANALYSES:
{{catalogue}}

{RULES}"""

SYSTEM_E2E = f"""You are an analytics agent. A deterministic engine runs analyses and returns
their results; you decide what to run next and when to stop. Each turn, answer with ONE JSON
object and nothing else:
{{"decision": "MORE_ANALYSIS" | "ANSWER" | "INSUFFICIENT_DATA" | "RESEARCH_REQUIRED",
 "call": {{"analysis": name, "params": {{argument: value}}}}   (only with MORE_ANALYSIS),
 "answer": string   (with the other decisions: the answer for a business reader),
 "note": short string}}
Every number in your answer must come from an engine result you were shown. Do not present a
correlation or a coincidence in time as a cause. You may run at most {MAX_CALLS} analyses.

ANALYSES:
{{catalogue}}

{RULES}"""

SYSTEM_RESEARCH = """You separate evidence for a business reader. You are given INTERNAL facts
(from the company's own data) and EXTERNAL facts (from outside), each with an id. Answer with
ONE JSON object and nothing else:
{"internal_fact_ids": [ids], "external_fact_ids": [ids],
 "inference": string,
 "causality_status": "ESTABLISHED" | "SUPPORTED_ASSOCIATION" | "NOT_ESTABLISHED" | "CONTRADICTED",
 "plausible_contributors": [short names in square brackets from the external facts, e.g. "price"],
 "ruled_out": [short names the facts argue against],
 "conclusion": string}
ESTABLISHED requires evidence that isolates the cause (an experiment or equivalent)."""


# --------------------------------------------------------------------------- providers


class Provider:
    def __init__(self, name: str):
        llm.load_env()
        self.name = name
        self.impl = {"gemini": llm.Gemini, "groq": llm.Groq}[name]()
        self.model_id = None
        self.served = set()

    def available(self) -> tuple[bool, str]:
        if not self.impl.available():
            return False, "no API key in the environment"
        try:
            self.model_id = self.impl.model()
            return True, self.model_id
        except llm.ProviderError as exc:
            return False, f"{exc}"[:300]

    def ask(self, system: str, turns: list[tuple[str, str]]) -> dict:
        """ask_once, with one patient retry on a 'daily quota' 429: on 24/09/2026 the first
        such answer was followed by a 200 a few minutes later, so one sighting is not proof
        the day is spent; two a minute apart are taken as that."""
        r = self.ask_once(system, turns)
        if not r["ok"] and r.get("daily_quota"):
            time.sleep(65)
            r = self.ask_once(system, turns)
        return r

    def ask_once(self, system: str, turns: list[tuple[str, str]]) -> dict:
        """One request. turns: [(role 'user'|'model', text)]. Returns text, latency, served
        model, or a classified error; the pacing wait is not in the latency."""
        gap = PACE[self.name] - (time.time() - _last.get(self.name, 0))
        if gap > 0:
            time.sleep(gap)
        t0 = time.perf_counter()
        try:
            if self.name == "gemini":
                data = self.impl.post({
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": r, "parts": [{"text": t}]} for r, t in turns],
                    "generationConfig": {"temperature": 0,
                                         "responseMimeType": "application/json"}},
                    self.model_id)
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                served = data.get("modelVersion")
            else:
                data = self.impl.post({
                    "messages": [{"role": "system", "content": system}] + [
                        {"role": "assistant" if r == "model" else "user", "content": t}
                        for r, t in turns],
                    "temperature": 0, "response_format": {"type": "json_object"}})
                text = data["choices"][0]["message"].get("content") or ""
                served = data.get("model")
            self.served.add(served)
            return {"ok": True, "text": text, "latency_s": round(time.perf_counter() - t0, 3),
                    "served_model": served}
        except llm.ProviderError as exc:
            kind = "RATE_LIMIT" if "429" in str(exc) or exc.kind == "daily_quota" \
                else "PROVIDER_ERROR"
            return {"ok": False, "error": kind, "detail": str(exc)[:900],
                    "daily_quota": exc.kind == "daily_quota",
                    "latency_s": round(time.perf_counter() - t0, 3)}
        finally:
            _last[self.name] = time.time()


def parse_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
    return None


# --------------------------------------------------------------------------- checkpoints


def ckpt(provider: str, track: str) -> Path:
    p = STATE / provider / f"{track}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def done(provider: str, track: str) -> dict:
    p = ckpt(provider, track)
    out = {}
    if p.exists():
        for line in p.read_text().splitlines():
            r = json.loads(line)
            if r.get("status") not in ("RATE_LIMIT", "PROVIDER_ERROR"):
                out[r["id"]] = r
    return out


def append(provider: str, track: str, rec: dict) -> None:
    with ckpt(provider, track).open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


# --------------------------------------------------------------------------- static validity

MEASURE_ARGS = ("measure", "against")
DIM_ARGS = ("dimension", "rows", "columns", "column", "second_dimension", "entity")
ADDITIVE_ONLY = ("pareto", "concentration", "growth_decomposition")


def step_valid(domain: str, step) -> tuple[bool, str]:
    d = DOMAINS[domain]
    if not isinstance(step, dict) or step.get("analysis") not in registry.REGISTRY:
        return False, "unknown analysis"
    name = step["analysis"]
    params = step.get("params") or {}
    if not isinstance(params, dict):
        return False, "params not an object"
    sig = list(inspect.signature(registry.REGISTRY[name].run).parameters.values())[3:]
    required = [p.name for p in sig if p.default is inspect.Parameter.empty
                and p.name != "params"]
    names = {p.name for p in sig}
    missing = [r for r in required if r not in params]
    if missing:
        return False, f"missing {', '.join(missing)}"
    extra = [k for k in params if k not in names]
    if extra:
        return False, f"takes no {', '.join(extra)}"
    for k, v in params.items():
        if k in MEASURE_ARGS and v not in d.measures:
            return False, f"{v!r} is not a declared measure"
        if k in DIM_ARGS and v not in d.dimensions:
            return False, f"{v!r} is not a declared dimension"
    m = params.get("measure")
    if m in d.measures and d.measures[m] == "none" and name not in (
            "correlation", "bivariate", "distribution", "outlier_detection", "correlated_shift"):
        return False, f"{m} has aggregation none"
    if name in ADDITIVE_ONLY and d.measures.get(m) not in ("sum", "count"):
        return False, f"{name} needs an additive measure"
    return True, ""


# --------------------------------------------------------------------------- scoring

ID_WORDS = re.compile(r"(_id$|^record_id$)")


def _dims_ok(domain, dims):
    d = DOMAINS[domain]
    return all(x in d.dimensions for x in dims)


def score_plan(qn: dict, plan: dict | None) -> dict:
    """Planner components out of 70 (evidence, stop and grounding need execution)."""
    if plan is None:
        return {"total": 0, "components": {}, "failures": ["FORMAT_FAILURE"]}
    dom = qn["domain"]
    d = DOMAINS[dom]
    fails, comp = [], {}
    flag = qn["flag"]
    intent = plan.get("intent")
    comp["intent"] = 10 if intent == qn["intent"] or (flag and intent == "refuse") else 0
    if not comp["intent"]:
        fails.append("INTENT_ERROR")
    metrics = [m for m in plan.get("metrics") or [] if isinstance(m, str)]
    invalid_m = [m for m in metrics if m not in d.measures]
    ids_summed = [m for m in metrics if ID_WORDS.search(m)]
    if flag == "REJECT":
        comp["metric"] = 10 if not ids_summed and not [m for m in invalid_m if "name" in m] \
            else 0
    elif flag:
        # declining a causal or thin-evidence question needs no metric: any valid choice,
        # none included, is right (smoke run before scoring: MAR11 lost 10 for [] -- a rubric
        # fault, fixed before the scored run)
        comp["metric"] = 10 if not invalid_m else 0
    elif not qn["metrics"]:
        comp["metric"] = 10 if not invalid_m else 5
    else:
        hit = any(m in qn["metrics"] for m in metrics)
        comp["metric"] = (10 if hit and not invalid_m else 5 if hit else 0)
    if comp["metric"] < 10:
        fails.append("METRIC_ERROR")
    dims = [x for x in plan.get("dimensions") or [] if isinstance(x, str)]
    if not qn["dims"] or flag:
        dpts = 5 if _dims_ok(dom, dims) else 2
    else:
        hit = any(x in qn["dims"] for x in dims)
        dpts = 5 if hit and _dims_ok(dom, dims) else 2 if hit else 0
    tpts = 5 if not qn["time"] or plan.get("time_range") or plan.get("comparison_period") \
        else 0
    comp["dimension_time"] = dpts + tpts
    if dpts < 5:
        fails.append("DIMENSION_ERROR")
    if tpts < 5:
        fails.append("TIME_RANGE_ERROR")
    steps = [s for s in plan.get("analysis_plan") or [] if isinstance(s, dict)]
    names = [s.get("analysis") for s in steps]
    if flag:
        good = not any(n in qn["bad"] for n in names) and (
            flag != "REJECT" or not steps or all(step_valid(dom, s)[0] for s in steps))
        comp["tool_selection"] = 15 if good else 0
    elif not steps:
        comp["tool_selection"] = 0
    else:
        in_ok = [n in qn["ok"] for n in names]
        comp["tool_selection"] = 15 if in_ok[0] and sum(in_ok) * 2 >= len(in_ok) else \
            8 if any(in_ok) else 0
    if comp["tool_selection"] < 15:
        fails.append("INVALID_TOOL")
    lo, hi = qn["calls"]
    keys = [json.dumps(s, sort_keys=True) for s in steps]
    within = (len(steps) >= min(lo, 1) or flag) and len(steps) <= hi + 2
    nodup = len(set(keys)) == len(keys)
    comp["sequencing"] = 10 if within and nodup else 5 if within or nodup else 0
    if comp["sequencing"] < 10:
        fails.append("BAD_TOOL_ORDER")
    valid = [step_valid(dom, s) for s in steps]
    comp["validity"] = round(10 * sum(v for v, _ in valid) / len(valid), 2) if valid else \
        (10 if flag else 0)
    rr, ins = bool(plan.get("research_required")), bool(plan.get("insufficient_data"))
    want = {"INSUFFICIENT_DATA": (False, True), "RESEARCH_REQUIRED": (True, None),
            "REJECT": (None, None)}.get(flag, (False, False))
    ok_u = (want[0] is None or rr == want[0]) and (want[1] is None or ins == want[1])
    if flag == "REJECT":
        ok_u = intent == "refuse" or ins
    comp["uncertainty"] = 5 if ok_u else 0
    if not ok_u and flag:
        fails.append("INTENT_ERROR" if flag == "REJECT" else "CAUSAL_OVERREACH")
    total = sum(comp.values())
    return {"total_of_70": total, "total": round(total * 100 / 70, 2), "components": comp,
            "detail": {"dimension_points": dpts, "time_points": tpts},
            "invalid_steps": [{"step": s, "why": w} for s, (v, w) in zip(steps, valid) if not v],
            "failures": sorted(set(fails))}


NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?%?")
CAUSAL = re.compile(r"\b(caused|causes|because of|due to|led to|drove|driven by|resulted in|"
                    r"is the reason|was the reason)\b", re.I)
HEDGE = re.compile(r"\b((?-i:may)|might|could|possibly|suggest|consistent with|associated|correlat|"
                   r"not (?:establish|prove|show)|cannot|can't|no evidence|unclear|coincid|"
                   r"plausibl|likely|not possible|impossible|no way to)\w*", re.I)
# Not claims of cause (measured on Gemini's 40 answers after they were scored, C112): the
# statistical phrase "due to sampling noise/chance", and an arithmetic decomposition saying which
# members a change is made of ("driven by groceries (+34.5% of the change)").
NOT_CAUSAL = re.compile(r"due to (?:sampling|chance|noise|random)|"
                        r"(?:decompos|contribution|of the (?:net )?change|mix shift|rate effect|"
                        r"mix effect|share of the change)", re.I)


def _nums(text: str) -> list[float]:
    out = []
    for t in NUM.findall(text):
        pct = t.endswith("%")
        try:
            v = float(t.rstrip("%").replace(",", ""))
        except ValueError:
            continue
        out.append((v, pct))
    return out


def grounding(answer: str, evidence: list[str], question: str) -> dict:
    """Every number in the answer must be in an engine result (to 0.5%, or as a percent of a
    share); years, small counts and the question's own numbers are not claims."""
    pool = []
    for e in evidence:
        pool += [v for v, _ in _nums(e)]
    asked = {v for v, _ in _nums(question)}
    claims, unsupported = 0, []
    for v, pct in _nums(answer):
        if (1900 <= v <= 2100 and float(v).is_integer()) or (abs(v) <= 10 and v.is_integer()) \
                or v in asked:
            continue
        claims += 1
        cands = [v] + ([v / 100] if pct else []) + ([v * 100] if not pct else [])
        ok = any(abs(c - p) <= max(0.005 * abs(p), 0.011) for c in cands for p in pool)
        if not ok:
            unsupported.append(v)
    return {"numeric_claims": claims, "unsupported": unsupported}


def overreach(answer: str) -> bool:
    for sent in re.split(r"(?<=[.!?])\s+", answer):
        if CAUSAL.search(sent) and not HEDGE.search(sent) and not NOT_CAUSAL.search(sent):
            return True
    return False


def ignored_warning(answer: str, evidence: list[str]) -> bool:
    ev = " ".join(evidence)
    weak = re.search(r"No test:|not (?:by )?more than sampling|does not differ|too few|"
                     r"No effect size", ev)
    return bool(weak) and bool(re.search(r"\bsignificant(ly)?\b", answer, re.I)) and \
        not re.search(r"\bnot (?:statistically )?significant|\bno significant|insignificant",
                      answer, re.I)


def score_e2e(qn: dict, rec: dict) -> dict:
    plan_s = score_plan(qn, rec.get("plan"))
    comp = dict(plan_s["components"])
    fails = set(plan_s["failures"])
    calls = rec["calls"]
    n = len(calls)
    executed = [c["analysis"] for c in calls]
    if not qn["flag"] and calls:
        in_ok = [a in qn["ok"] for a in executed]
        comp["tool_selection"] = 15 if in_ok[0] and sum(in_ok) * 2 >= n else \
            8 if any(in_ok) else 0
    okc = [c for c in calls if c["status"] == "OK"]
    comp["validity"] = round(10 * len(okc) / n, 2) if n else (10 if qn["flag"] else 0)
    if n and len(okc) < n:
        fails.add("INVALID_TOOL")
    answer = rec.get("answer") or ""
    ev = [c["result"] for c in okc]
    g = grounding(answer, ev, qn["text"])
    over = overreach(answer)
    ign = ignored_warning(answer, ev)
    frac = 1 - len(g["unsupported"]) / g["numeric_claims"] if g["numeric_claims"] else 1
    comp["evidence"] = round(10 * frac, 2) + (0 if ign else 5)
    lo, hi = qn["calls"]
    decision = rec.get("decision")
    premature = decision == "ANSWER" and n < lo and not qn["flag"]
    overan = n > hi + 2
    comp["stop"] = 0 if premature or overan else 5
    flag_ok = (qn["flag"] is None and decision == "ANSWER") or (
        qn["flag"] == "INSUFFICIENT_DATA" and decision in ("INSUFFICIENT_DATA", "ANSWER")
        and not over) or (qn["flag"] == "RESEARCH_REQUIRED" and decision in (
            "RESEARCH_REQUIRED", "INSUFFICIENT_DATA")) or (
        qn["flag"] == "REJECT" and decision in ("INSUFFICIENT_DATA", "ANSWER") and not any(
            a in qn["bad"] for a in executed))
    comp["uncertainty"] = 5 if flag_ok and not over else 0
    comp["grounding"] = round(10 * frac, 2) if answer else 0
    if premature:
        fails.add("PREMATURE_STOP")
    if overan:
        fails.add("OVERANALYSIS")
    if g["unsupported"]:
        fails.add("UNSUPPORTED_CLAIM")
    if over:
        fails.add("CAUSAL_OVERREACH")
    if ign:
        fails.add("IGNORED_WARNING")
    if not answer:
        fails.add("WRONG_FINAL_ANSWER")
    repeated = n - len({json.dumps([c["analysis"], c["params"]], sort_keys=True) for c in calls})
    useful = sum(1 for c in okc if c["analysis"] in qn["ok"] or qn["flag"])
    return {"total": round(sum(comp.values()), 2), "components": comp,
            "failures": sorted(fails), "grounding": g, "causal_overreach": over,
            "ignored_warning": ign, "premature_stop": premature, "overanalysis": overan,
            "calls": n, "useful_calls": useful, "invalid_calls": n - len(okc),
            "repeated_calls": repeated,
            "irrelevant_calls": sum(1 for c in okc if c["analysis"] not in qn["ok"]
                                    and not qn["flag"])}


def score_research(case: dict, ans: dict | None) -> dict:
    if ans is None:
        return {"total": 0, "failures": ["FORMAT_FAILURE"]}
    ids_i = {f"I{k + 1}" for k in range(len(case["internal"]))}
    ids_e = {f"E{k + 1}" for k in range(len(case["external"]))}
    gi, ge = set(ans.get("internal_fact_ids") or []), set(ans.get("external_fact_ids") or [])
    sep = 30 if gi == ids_i and ge == ids_e else 15 if not (gi & ids_e or ge & ids_i) else 0
    status = ans.get("causality_status")
    # The exact status earns 30; the other non-causal one 15 -- the line between "supported
    # association" and "not established" is arguable, a claim of ESTABLISHED is not (set after
    # the smoke run, before any scored item).
    soft = {"SUPPORTED_ASSOCIATION", "NOT_ESTABLISHED"}
    caus = 30 if status == case["causal"] else 15 if {status, case["causal"]} <= soft else 0

    def names(xs):
        return {str(x).strip("[] ").lower() for x in xs or []}

    def jac(a, b):
        return len(a & b) / len(a | b) if a | b else 1.0
    contrib = round(20 * jac(names(ans.get("plausible_contributors")), set(case["contributors"])),
                    2)
    ruled = round(10 * jac(names(ans.get("ruled_out")), set(case["ruled_out"])), 2)
    over = overreach(" ".join(str(ans.get(k, "")) for k in ("inference", "conclusion"))) or \
        status == "ESTABLISHED"
    fails = []
    if sep < 30:
        fails.append("FORMAT_FAILURE" if not gi and not ge else "UNSUPPORTED_CLAIM")
    if caus == 0:
        fails.append("CAUSAL_OVERREACH" if status == "ESTABLISHED" else "WRONG_FINAL_ANSWER")
    if over and "CAUSAL_OVERREACH" not in fails:
        fails.append("CAUSAL_OVERREACH")
    return {"total": sep + caus + contrib + ruled + (0 if over else 10),
            "components": {"separation": sep, "causality": caus, "contributors": contrib,
                           "ruled_out": ruled, "no_overreach": 0 if over else 10},
            "failures": fails}


# --------------------------------------------------------------------------- tracks


def _system(template: str) -> str:
    return template.replace("{catalogue}", catalogue())


def cmd_planner(pname: str) -> None:
    prov = Provider(pname)
    ok, info = prov.available()
    if not ok:
        print(f"{pname}: NOT_RUN_PROVIDER_UNAVAILABLE ({info})")
        return
    have = done(pname, "planner")
    system = _system(SYSTEM_PLANNER)
    t_start, n = time.time(), 0
    for qn in QB.QUESTIONS:
        if qn["id"] in have or (ONLY and qn["id"] not in ONLY):
            continue
        msg = f"{dataset_context(qn['domain'])}\nQUESTION: {qn['text']}"
        r = prov.ask(system, [("user", msg)])
        if not r["ok"]:
            append(pname, "planner", {"id": qn["id"], "status": r["error"], **r})
            print(f"{pname} planner {qn['id']}: {r['error']} {r['detail'][:120]}", flush=True)
            if r.get("daily_quota"):
                print(f"{pname}: daily quota spent; resume later (checkpointed)")
                return
            continue
        plan = parse_json(r["text"])
        s = score_plan(qn, plan)
        append(pname, "planner", {"id": qn["id"], "status": "DONE", "model": prov.model_id,
                                  "served_model": r["served_model"], "latency_s": r["latency_s"],
                                  "raw": r["text"][:6000], "plan": plan, "score": s})
        n += 1
        if n == 20:
            rate = (time.time() - t_start) / n
            left = sum(1 for x in QB.QUESTIONS if x["id"] not in done(pname, "planner"))
            print(f"{pname}: 20 planner questions in {time.time() - t_start:.0f}s; about "
                  f"{rate * left / 60:.0f} min for the {left} left at this rate", flush=True)
        print(f"{pname} planner {qn['id']}: {s['total']}", flush=True)


def cmd_research(pname: str) -> None:
    prov = Provider(pname)
    ok, info = prov.available()
    if not ok:
        print(f"{pname}: NOT_RUN_PROVIDER_UNAVAILABLE ({info})")
        return
    have = done(pname, "research")
    for case in RB.CASES:
        if case["id"] in have or (ONLY and case["id"] not in ONLY):
            continue
        facts = [f"I{k + 1}: {t}" for k, t in enumerate(case["internal"])] + \
                [f"E{k + 1}: {t}" for k, t in enumerate(case["external"])]
        msg = ("INTERNAL FACTS:\n" + "\n".join(f for f in facts if f.startswith("I"))
               + "\nEXTERNAL FACTS:\n" + "\n".join(f for f in facts if f.startswith("E"))
               + f"\nQUESTION: {case['question']}")
        r = prov.ask(SYSTEM_RESEARCH, [("user", msg)])
        if not r["ok"]:
            append(pname, "research", {"id": case["id"], "status": r["error"], **r})
            if r.get("daily_quota"):
                return
            continue
        ans = parse_json(r["text"])
        s = score_research(case, ans)
        append(pname, "research", {"id": case["id"], "status": "DONE", "model": prov.model_id,
                                   "served_model": r["served_model"],
                                   "latency_s": r["latency_s"], "raw": r["text"][:4000],
                                   "answer": ans, "score": s})
        print(f"{pname} research {case['id']}: {s['total']}", flush=True)


def ws_for(domain: str) -> str:
    return f"llm_{domain}"


def cmd_setup() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    for name, d in DOMAINS.items():
        ws = ws_for(name)
        workspace.reset(ws)
        p = STATE / f"{name}_{ROWS}.csv"
        write_dataset(d, ROWS, p, SEED)
        spec = server.propose_ingest_spec(path=str(p), dataset_name=name)
        blk = spec.split("```json", 1)[1].split("```", 1)[0]
        server.confirm_ingest_spec(spec_json=blk, workspace_id=ws)
        server.propose_cleaning_plan(dataset_name=name, workspace_id=ws)
        server.apply_cleaning_plan(dataset_name=name, approved_action_ids=["C001"],
                                   workspace_id=ws)
        prop = server.propose_dataset_contract(dataset_name=name, workspace_id=ws,
                                               **contract_kwargs(d, "record_id"))
        blk = prop.split("```json", 1)[1].split("```", 1)[0]
        out = server.confirm_dataset_contract(contract_json=blk, workspace_id=ws)
        print(name, out.splitlines()[0][:100])


def cmd_e2e(pname: str) -> None:
    prov = Provider(pname)
    ok, info = prov.available()
    if not ok:
        print(f"{pname}: NOT_RUN_PROVIDER_UNAVAILABLE ({info})")
        return
    have = done(pname, "e2e")
    sys_plan, sys_e2e = _system(SYSTEM_PLANNER), _system(SYSTEM_E2E)
    for qn in [x for x in QB.QUESTIONS if x["e2e"]]:
        if qn["id"] in have or (ONLY and qn["id"] not in ONLY):
            continue
        dom = qn["domain"]
        ctx = f"{dataset_context(dom)}\nQUESTION: {qn['text']}"
        r = prov.ask(sys_plan, [("user", ctx)])
        if not r["ok"]:
            append(pname, "e2e", {"id": qn["id"], "status": r["error"], **r})
            if r.get("daily_quota"):
                return
            continue
        plan = parse_json(r["text"])
        latencies = [r["latency_s"]]
        turns = [("user", ctx + "\nYour plan was:\n" + json.dumps(plan)[:3000]
                  + "\nNow begin. Reply with the JSON decision object.")]
        calls, decision, answer, err = [], None, None, None
        for _ in range(MAX_CALLS + 2):
            r = prov.ask(sys_e2e, turns)
            if not r["ok"]:
                err = r
                break
            latencies.append(r["latency_s"])
            turns.append(("model", r["text"]))
            dec = parse_json(r["text"]) or {}
            decision = dec.get("decision")
            if decision == "MORE_ANALYSIS" and len(calls) < MAX_CALLS:
                call = dec.get("call") or {}
                analysis, params = call.get("analysis"), call.get("params") or {}
                t0 = time.perf_counter()
                try:
                    res = server.compute_analysis(dataset_name=dom, analysis_type=str(analysis),
                                                  workspace_id=ws_for(dom),
                                                  **(params if isinstance(params, dict) else {}))
                except Exception as exc:  # noqa: BLE001 - an invalid call is data here
                    res = f"BLOCKED: the call could not be made ({type(exc).__name__}: {exc})"
                status = "REFUSED" if res.lstrip().startswith("BLOCKED") else "OK"
                calls.append({"analysis": analysis, "params": params, "status": status,
                              "seconds": round(time.perf_counter() - t0, 3),
                              "result": res[:RESULT_CHARS]})
                turns.append(("user", f"ENGINE RESULT (call {len(calls)} of at most "
                                      f"{MAX_CALLS}):\n{res[:RESULT_CHARS]}"))
                continue
            if decision == "MORE_ANALYSIS":
                turns.append(("user", "The analysis budget is spent. Give your final decision "
                                      "now (ANSWER, INSUFFICIENT_DATA or RESEARCH_REQUIRED)."))
                continue
            answer = dec.get("answer") or ""
            break
        if err:
            append(pname, "e2e", {"id": qn["id"], "status": err["error"], **err,
                                  "partial_calls": len(calls)})
            if err.get("daily_quota"):
                return
            continue
        rec = {"id": qn["id"], "status": "DONE", "model": prov.model_id,
               "served_model": sorted(prov.served), "plan": plan, "decision": decision,
               "answer": answer, "calls": calls, "latencies_s": latencies,
               "latency_s": round(sum(latencies), 3)}
        rec["score"] = score_e2e(qn, rec)
        append(pname, "e2e", rec)
        print(f"{pname} e2e {qn['id']}: {rec['score']['total']} ({len(calls)} calls)",
              flush=True)


# --------------------------------------------------------------------------- report


def _load(pname: str, track: str) -> list[dict]:
    p = ckpt(pname, track)
    if not p.exists():
        return []
    recs = {}
    for line in p.read_text().splitlines():
        r = json.loads(line)
        if r["id"] in recs and recs[r["id"]].get("status") == "DONE":
            continue
        recs[r["id"]] = r
    # Scores are recomputed from the stored plan/answer on every report, so one rubric scores
    # both providers; a rubric change after results were seen is recorded in the step doc.
    qmap, cmap = {x["id"]: x for x in QB.QUESTIONS}, {c["id"]: c for c in RB.CASES}
    for r in recs.values():
        if r.get("status") != "DONE":
            continue
        if track == "planner":
            r["score"] = score_plan(qmap[r["id"]], r.get("plan"))
        elif track == "e2e":
            r["score"] = score_e2e(qmap[r["id"]], r)
        else:
            r["score"] = score_research(cmap[r["id"]], r.get("answer"))
    return list(recs.values())


def _pct(xs):
    return round(100 * sum(xs) / len(xs), 2) if xs else None


def _p95(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def summarise(pname: str) -> dict:
    qmap = {x["id"]: x for x in QB.QUESTIONS}
    pl = [r for r in _load(pname, "planner") if r["status"] == "DONE"]
    e2 = [r for r in _load(pname, "e2e") if r["status"] == "DONE"]
    rs = [r for r in _load(pname, "research") if r["status"] == "DONE"]
    errs = [r for t in ("planner", "e2e", "research") for r in _load(pname, t)
            if r["status"] in ("RATE_LIMIT", "PROVIDER_ERROR")]
    comp = lambda recs, k, full: _pct([r["score"]["components"].get(k, 0) == full  # noqa
                                       for r in recs if r["score"].get("components")])
    steps = [s for r in pl for s in (r.get("plan") or {}).get("analysis_plan") or []
             if isinstance(s, dict)]
    valid = [step_valid(qmap[r["id"]]["domain"], s)[0] for r in pl
             for s in (r.get("plan") or {}).get("analysis_plan") or [] if isinstance(s, dict)]
    ecalls = sum(r["score"]["calls"] for r in e2)
    mk = [r for r in pl + e2 if qmap[r["id"]]["marketing"]]
    lat = [r["latency_s"] for r in pl + rs] + [x for r in e2 for x in r["latencies_s"]]
    served = sorted({str(r.get("served_model")) for r in pl + rs} |
                    {str(m) for r in e2 for m in r.get("served_model") or []})
    return {
        "provider": pname, "model": next((r.get("model") for r in pl + e2 + rs), None),
        "served_models": served,
        "planner_done": len(pl), "e2e_done": len(e2), "research_done": len(rs),
        "planner_score": round(statistics.fmean(r["score"]["total"] for r in pl), 2)
        if pl else None,
        "e2e_score": round(statistics.fmean(r["score"]["total"] for r in e2), 2) if e2 else None,
        "research_score": round(statistics.fmean(r["score"]["total"] for r in rs), 2)
        if rs else None,
        "marketing_score": round(statistics.fmean(r["score"]["total"] for r in mk), 2)
        if mk else None,
        "json_valid_rate": _pct([r.get("plan") is not None for r in pl]),
        "intent_accuracy": comp(pl, "intent", 10),
        "metric_accuracy": comp(pl, "metric", 10),
        "dimension_accuracy": _pct([r["score"].get("detail", {}).get("dimension_points") == 5
                                    for r in pl if r["score"].get("components")]),
        "time_accuracy": _pct([r["score"].get("detail", {}).get("time_points") == 5
                               for r in pl if r["score"].get("components")]),
        "valid_tool_selection_rate": comp(pl, "tool_selection", 15),
        "planner_step_validity": _pct(valid) if steps else None,
        "invalid_call_rate": round(100 * sum(r["score"]["invalid_calls"] for r in e2) / ecalls, 2)
        if ecalls else None,
        "tool_efficiency": round(sum(r["score"]["useful_calls"] for r in e2) / ecalls, 4)
        if ecalls else None,
        "redundant_call_rate": round(100 * sum(r["score"]["repeated_calls"] for r in e2)
                                     / ecalls, 2) if ecalls else None,
        "premature_stop_rate": _pct([r["score"]["premature_stop"] for r in e2]),
        "overanalysis_rate": _pct([r["score"]["overanalysis"] for r in e2]),
        "unsupported_claim_rate": round(100 * sum(len(r["score"]["grounding"]["unsupported"])
                                                  for r in e2) / max(1, sum(
                                                      r["score"]["grounding"]["numeric_claims"]
                                                      for r in e2)), 2) if e2 else None,
        "causal_overreach_rate": _pct([r["score"]["causal_overreach"] for r in e2]),
        "median_latency_s": round(statistics.median(lat), 3) if lat else None,
        "p95_latency_s": _p95(lat),
        "provider_failures": len(errs),
        "rate_limit_failures": sum(r["status"] == "RATE_LIMIT" for r in errs),
    }


def cmd_report() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    w = lambda n, o: (OUT / n).write_text(json.dumps(o, indent=1, default=str))  # noqa: E731
    w("questions.json", {"questions": QB.QUESTIONS, "research_cases": RB.CASES})
    names = ("gemini", "groq")
    sums = {p: summarise(p) for p in names}
    avail = {}
    for p in names:
        prov = Provider(p)
        ok, info = prov.available()
        avail[p] = {"available_now": ok, "detail": info if not ok else prov.model_id}
    for p in names:
        w(f"{p}_results.json", {"summary": sums[p], "planner": _load(p, "planner"),
                                "e2e": _load(p, "e2e"), "research": _load(p, "research")})
    fails = [{"provider": p, "track": t, "id": r["id"], "failures": r["score"]["failures"]}
             for p in names for t in ("planner", "e2e", "research") for r in _load(p, t)
             if r["status"] == "DONE" and r["score"].get("failures")]
    fails += [{"provider": p, "track": t, "id": r["id"], "failures": [r["status"]],
               "detail": r.get("detail")} for p in names
              for t in ("planner", "e2e", "research") for r in _load(p, t)
              if r["status"] in ("RATE_LIMIT", "PROVIDER_ERROR")]
    w("failures.json", fails)
    w("tool_calls.json", {p: [{"id": r["id"], "calls": [{k: v for k, v in c.items()
                                                          if k != "result"} | {
        "result_head": c["result"][:300]} for c in r["calls"]]} for r in _load(p, "e2e")
        if r["status"] == "DONE"] for p in names})
    w("latency.json", {p: {"median_s": sums[p]["median_latency_s"],
                           "p95_s": sums[p]["p95_latency_s"],
                           "planner": [r["latency_s"] for r in _load(p, "planner")
                                       if r["status"] == "DONE"],
                           "e2e_per_question": [r["latency_s"] for r in _load(p, "e2e")
                                                if r["status"] == "DONE"]} for p in names})
    w("tool_efficiency.json", {p: {k: sums[p][k] for k in (
        "tool_efficiency", "invalid_call_rate", "redundant_call_rate", "premature_stop_rate",
        "overanalysis_rate")} for p in names})
    w("research_track.json", {p: _load(p, "research") for p in names})
    bench = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "providers": avail,
             "summaries": sums, "question_counts": {
                 "planner": len(QB.QUESTIONS), "e2e": sum(x["e2e"] for x in QB.QUESTIONS),
                 "research": len(RB.CASES),
                 "marketing": sum(x["marketing"] for x in QB.QUESTIONS)},
             "fairness": {"temperature": 0, "max_calls": MAX_CALLS,
                          "result_chars": RESULT_CHARS, "retry_policy":
                          "backend.engine.webapp.llm._request (identical for both)",
                          "pace_seconds": PACE}}
    w("benchmark.json", bench)
    import report as R
    (OUT / "comparison.md").write_text(R.comparison(bench))
    (OUT / "summary.md").write_text(R.summary(bench))
    print(R.comparison(bench))


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    {"setup": cmd_setup, "planner": lambda: cmd_planner(arg),
     "research": lambda: cmd_research(arg), "e2e": lambda: cmd_e2e(arg),
     "report": cmd_report}[cmd]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
