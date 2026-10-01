"""Datasets, profile, cleaning, domains, contract, tools, packs: the service behind B2's routes.

The engine does the work (v1's RealBackend: upload -> ingest -> contract -> cleaning). This
layer adds v2's parts: dataset ids, domain detection/confirmation, pack forks, tool gating.
"""
from __future__ import annotations

import secrets
from dataclasses import asdict
from datetime import datetime, timezone

from backend.engine.config import validate_workspace_id
from backend.engine.contract.evidence import gather
from backend.engine.util import db
from backend.engine.webapp.real_backend import RealBackend
from backend.packs.detector import bind, detect, normalise
from backend.packs.loader import load_all, merge
from backend.services.sessions import ServiceError
from backend.sessions.store import Store
from backend.engine.contract import store as contract_store
from backend.tools import registry, runner

SESSION_DAYS = 30


class V2Backend(RealBackend):
    @staticmethod
    def ttl_seconds() -> float:
        """Workspaces live as long as sessions (30 days idle), not v1's 72 hours (D-B2-5)."""
        return SESSION_DAYS * 24 * 3600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refusal(obj, status: int = 422, code: str = "refused") -> ServiceError:
    r = getattr(obj, "refusal", None)
    msg = (r.what if r else "") or getattr(obj, "message", "") or "refused"
    extra = {}
    if r is not None:
        extra = {"refusal": {k: v for k, v in asdict(r).items() if isinstance(v, (str, list))}}
    return ServiceError(status, code, msg, extra)


class DatasetService:
    def __init__(self, store: Store, backend: RealBackend | None = None):
        self.store = store
        self.be = backend or V2Backend()

    # --- datasets ----------------------------------------------------------------------------
    def _get(self, dataset_id: str) -> dict:
        d = self.store.get_dataset(dataset_id)
        if d is None:
            raise ServiceError(404, "not_found", f"dataset {dataset_id} not found")
        return d

    def upload(self, workspace_id: str, filename: str, data: bytes) -> dict:
        try:
            validate_workspace_id(workspace_id)
        except ValueError as e:
            raise ServiceError(422, "bad_workspace_id", str(e)) from e
        up = self.be.save_upload(workspace_id, filename, data)
        if up.verdict == "REFUSE" or not up.path:
            raise _refusal(up, 413 if "limit" in up.message else 422, "upload_refused")
        draft = self.be.draft_ingest(workspace_id, up.path)
        if draft.refusal is not None:
            raise _refusal(draft)
        if draft.unresolved:
            raise ServiceError(422, "ingest_needs_answers",
                               "The file's layout needs answers before it can be read.",
                               {"questions": draft.questions, "unresolved": draft.unresolved})
        done = self.be.confirm_ingest(workspace_id, draft.spec)
        if not done.ok:
            raise _refusal(done)
        d = self.store.upsert_dataset({"dataset_id": f"ds_{secrets.token_hex(6)}",
                                       "workspace_id": workspace_id,
                                       "name": draft.dataset_name, "created_at": _now()})
        return self.summary(d["dataset_id"], assumptions=list(draft.assumptions))

    def summary(self, dataset_id: str, assumptions: list[str] | None = None) -> dict:
        d = self._get(dataset_id)
        ev = self._evidence(d)
        return {"dataset_id": dataset_id, "workspace_id": d["workspace_id"], "name": d["name"],
                "rows": ev.row_count, "columns": len(ev.columns), "created_at": d["created_at"],
                "assumptions": assumptions or []}

    def _evidence(self, d: dict):
        con = db.connect_read_only(d["workspace_id"])
        try:
            return gather(con, d["name"], probe_pairs=False)
        finally:
            con.close()

    def _columns(self, d: dict) -> set[str]:
        con = db.connect_read_only(d["workspace_id"])
        try:
            return {r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [d["name"]]).fetchall()}
        finally:
            con.close()

    # --- profile -------------------------------------------------------------------------------
    def profile(self, dataset_id: str) -> dict:
        d = self._get(dataset_id)
        ev = self._evidence(d)
        m = merge(load_all(), d["domains"])
        rate_words = m.word_classes.get("ratio_words", set()) | m.word_classes.get("per_row", set())
        cols, warnings = [], []
        for c in ev.columns:
            n = c.row_count or 1
            cols.append({"name": c.name, "type": c.dtype,
                         "null_pct": round(100 * (c.row_count - c.non_null) / n, 2),
                         "distinct": c.distinct,
                         "sample": [v for v in (c.min_value, c.max_value) if v is not None]})
            toks = set(normalise(c.name).split("_"))
            if toks & rate_words and c.dtype.upper() in ("DOUBLE", "FLOAT", "DECIMAL", "REAL") \
                    or toks & {"ctr", "cvr", "roas"}:
                warnings.append(f"{c.name} looks like a per-row rate: never sum or plainly "
                                f"average it (no_sum_of_rate)")
            for cls, hints in m.pii_classes.items():
                if normalise(c.name) in hints or toks & set(h for h in hints if "_" not in h):
                    warnings.append(f"{c.name} may hold personal data ({cls}): it is named, "
                                    f"never shown in results; groups under "
                                    f"{m.min_group_size} are suppressed")
                    break
        return {"dataset_id": dataset_id, "rows": ev.row_count, "columns": cols,
                "warnings": warnings}

    # --- cleaning ------------------------------------------------------------------------------
    def cleaning_proposals(self, dataset_id: str) -> dict:
        d = self._get(dataset_id)
        p = self.be.propose_cleaning(d["workspace_id"], d["name"])
        if p.refusal is not None:
            raise _refusal(p)
        return {"dataset_id": dataset_id, "proposals": [
            {"action_id": s.action_id, "kind": s.kind, "column": s.column,
             "description": s.intent, "rows_affected": s.rows_affected,
             "lossy": s.lossy, "suggested": s.suggested} for s in p.steps]}

    def cleaning_approve(self, dataset_id: str, approve: list[str], reject: list[str]) -> dict:
        d = self._get(dataset_id)
        if not approve:
            return {"applied": [], "rejected": reject, "ledger_entries": 0}
        r = self.be.apply_cleaning(d["workspace_id"], d["name"], approve)
        if not r.ok:
            raise _refusal(r)
        return {"applied": approve, "rejected": reject, "ledger_entries": len(approve)}

    # --- domains -------------------------------------------------------------------------------
    def detect(self, dataset_id: str) -> dict:
        d = self._get(dataset_id)
        cols = [c.name for c in self._evidence(d).columns]
        det = detect(cols)
        owner = {s.id: pid for pid, p in load_all().items() for s in p.sources}
        return {"dataset_id": dataset_id, "domains": det.domains,
                "marketing_sources": [x for x in det.sources if owner[x["source"]] ==
                                      "marketing"],
                "sources": [{**x, "domain": owner[x["source"]]} for x in det.sources],
                "confirmed": d["domains"]}

    def confirm_domains(self, dataset_id: str, domains: list[str]) -> dict:
        d = self._get(dataset_id)
        known = set(load_all()) - {"core"}
        unknown = [x for x in domains if x not in known]
        if unknown:
            raise ServiceError(422, "unknown_domain", f"unknown domain(s): {unknown}; "
                                                      f"known: {sorted(known)}")
        self.store.set_dataset_choices(dataset_id, domains=sorted(set(domains)))
        return {"ok": True, "version": 1, "confirmed": sorted(set(domains)),
                "dataset_id": d["dataset_id"]}

    # --- contract ------------------------------------------------------------------------------
    def _bound(self, d: dict) -> tuple[dict[str, list[str]], set[str]]:
        cols = [c.name for c in self._evidence(d).columns]
        packs = load_all()
        m = merge(packs, d["domains"])
        det = detect(cols, packs)
        return bind(cols, m), {s["source"] for s in det.sources}

    def _forks(self, d: dict) -> list[dict]:
        m = merge(load_all(), d["domains"])
        bound, _ = self._bound(d)
        out = []
        for f in m.forks.values():
            if f.applies_when and not any(c in bound for c in f.applies_when):
                continue
            out.append({"fork_id": f.id, "question": f.question,
                        "options": [o.model_dump() for o in f.options],
                        "suggested": f.suggested, "suggested_reason": f.suggested_reason})
        return out

    def contract_proposal(self, dataset_id: str) -> dict:
        d = self._get(dataset_id)
        c = self.be.draft_contract(d["workspace_id"], d["name"])
        if c.refusal is not None:
            raise _refusal(c)
        measures = []
        for m in c.measures:
            s = c.suggestions.get(m)
            measures.append({"column": m, "measure_type": _measure_type(c, m),
                             "agg": c.aggregations.get(m) or "",
                             "suggested_agg": s.agg if s else None,
                             "strength": s.strength if s else None,
                             "reason": s.reason if s else ""})
        return {"dataset_id": dataset_id, "grain": c.grain or "", "key": c.primary_key,
                "date": c.date_column, "measures": measures, "dimensions": c.dimensions,
                "caveats": list(c.caveats) + list(c.measured_caveats),
                "provisional": list(c.provisional), "questions": list(c.questions),
                "forks": self._forks(d), "prefill": self._prefill(d)}

    PREFILL_MIN_SIMILARITY = 0.8

    def _prefill(self, d: dict) -> dict | None:
        """Answers from the most similar dataset with a confirmed contract in the SAME workspace
        (B9 concept 13, D-B9-3): a suggestion the person confirms, never applied here."""
        cols = self._columns(d)
        best = None
        for o in self.store.list_datasets(d["workspace_id"]):
            if o["dataset_id"] == d["dataset_id"]:
                continue
            try:
                other = self._columns(o)
                sc = self._contract(o)
            except (ServiceError, Exception):  # noqa: BLE001 -- no contract / gone: skip it
                continue
            sim = len(cols & other) / len(cols | other) if cols | other else 0.0
            if sim >= self.PREFILL_MIN_SIMILARITY and (best is None or sim > best[0]):
                best = (sim, o, sc)
        if best is None:
            return None
        sim, o, sc = best
        kw = self._contract_kwargs(sc.contract)
        measures = [m for m in kw["measures"] if m in cols]
        dims = [x for x in kw["dimensions"] if x in cols]
        contract = {"grain": kw["grain"], "primary_key": [k for k in kw["primary_key"]
                                                          if k in cols],
                    "date_column": kw["date_column"] if kw["date_column"] in cols else None,
                    "measures": measures, "dimensions": dims,
                    "aggregations": {m: a for m, a in kw["aggregations"].items()
                                     if m in measures},
                    "measure_definitions": {m: t for m, t in kw["measure_definitions"].items()
                                            if m in measures}}
        return {"from_dataset_id": o["dataset_id"], "from_name": o["name"],
                "similarity": round(sim, 3), "contract_version": sc.version,
                "contract": contract, "fork_choices": o["fork_choices"],
                "domains": o["domains"], "metrics": sorted(o["metrics"]),
                "validity_rules": o["validity"],
                "note": "Suggested from a similar file; nothing is applied until you confirm. "
                        "Set this file's analysis window; approve metrics again after the "
                        "contract (they are versions of this file's contract)."}

    def contract_confirm(self, dataset_id: str, contract: dict, fork_choices: dict) -> dict:
        d = self._get(dataset_id)
        forks = {f["fork_id"]: f for f in self._forks(d)}
        missing = [f for f in forks if f not in fork_choices]
        bad = [f"{k}={v}" for k, v in fork_choices.items()
               if k in forks and v not in {o["id"] for o in forks[k]["options"]}]
        if missing or bad:
            raise ServiceError(422, "forks_unanswered",
                               "Every fork is answered by the person; none is defaulted.",
                               {"missing": missing, "invalid": bad})
        allowed = {"grain", "primary_key", "date_column", "measures", "dimensions",
                   "aggregations", "measure_definitions", "analysis_window_start",
                   "analysis_window_end", "caveats", "measure_per", "ratios", "measure_columns"}
        extra = sorted(set(contract) - allowed)
        if extra:
            raise ServiceError(422, "unknown_contract_fields", f"unknown fields: {extra}")
        draft = self.be.draft_contract(d["workspace_id"], d["name"], **contract)
        if draft.provisional:
            raise ServiceError(422, "contract_provisional",
                               "These fields still need an answer.",
                               {"provisional": list(draft.provisional),
                                "questions": list(draft.questions)})
        r = self.be.confirm_contract(d["workspace_id"], draft)
        if not r.ok:
            raise _refusal(r)
        self.store.set_dataset_choices(dataset_id, fork_choices={**d["fork_choices"],
                                                                 **fork_choices})
        return {"ok": True, "version": self._contract(d).version}

    # --- tools ---------------------------------------------------------------------------------
    def tools(self, dataset_id: str) -> dict:
        d = self._get(dataset_id)
        bound, sources = self._bound(d)
        states = registry.statuses(d["domains"], set(bound), sources)
        return {"tools": [{"tool_id": s.tool_id, "ui_label": s.ui_label, "status": s.status,
                           "needs_domain": s.needs_domain,
                           "missing_concepts": s.missing_concepts} for s in states]}


    # --- forks answered after the contract (a domain confirmed later brings new ones) --------
    def answer_forks(self, dataset_id: str, fork_choices: dict) -> dict:
        d = self._get(dataset_id)
        m = merge(load_all(), d["domains"])
        bad = [f"{k}={v}" for k, v in fork_choices.items()
               if k not in m.forks or v not in {o.id for o in m.forks[k].options}]
        if bad:
            raise ServiceError(422, "invalid_fork_choice", f"invalid: {bad}", {"invalid": bad})
        merged = {**d["fork_choices"], **fork_choices}
        self.store.set_dataset_choices(dataset_id, fork_choices=merged)
        return {"ok": True, "version": 1, "fork_choices": merged}

    # --- contract helpers --------------------------------------------------------------------
    def _contract(self, d: dict):
        with self.be._workspace(d["workspace_id"]):   # the engine's per-workspace lock
            con = db.connect(d["workspace_id"])
            try:
                sc = contract_store.current(con, d["name"])
            finally:
                con.close()
        if sc is None:
            raise ServiceError(409, "contract_required",
                               "Confirm the dataset contract first (POST .../contract/confirm).")
        return sc

    @staticmethod
    def _contract_kwargs(c) -> dict:
        ms = c.measures
        kw = {"grain": c.grain, "primary_key": list(c.primary_key),
              "date_column": c.date_column, "measures": [m.name for m in ms],
              "dimensions": list(c.dimensions),
              "aggregations": {m.name: m.agg for m in ms if m.agg},
              "measure_definitions": {m.name: m.definition for m in ms},
              "caveats": list(c.caveats),
              "measure_per": {m.name: list(m.per) for m in ms if m.per},
              "ratios": {m.name: {"numerator": list(m.numerator),
                                  "denominator": list(m.denominator), "scale": m.scale}
                         for m in ms if m.agg == "ratio"},
              "measure_columns": {m.name: m.column for m in ms if m.column}}
        if c.analysis_window:
            kw["analysis_window_start"] = c.analysis_window.start.isoformat()
            kw["analysis_window_end"] = c.analysis_window.end.isoformat()
        return kw

    # --- metric templates --------------------------------------------------------------------
    def metric_templates(self, dataset_id: str) -> dict:
        d = self._get(dataset_id)
        bound, _ = self._bound(d)
        m = merge(load_all(), d["domains"])
        out = []
        for t in m.templates.values():
            out.append({"template_id": t.id, "label": t.label, "shape": t.shape,
                        "required_concepts": t.required_concepts, "forks": t.forks,
                        "available": t.engine_ready and all(c in bound
                                                            for c in t.required_concepts),
                        "approved": t.id in d["metrics"],
                        "measure": d["metrics"].get(t.id)})
        return {"templates": out}

    def approve_metric(self, dataset_id: str, template_id: str, bindings: dict,
                       fork_choices: dict) -> dict:
        d = self._get(dataset_id)
        m = merge(load_all(), d["domains"])
        t = m.templates.get(template_id)
        if t is None:
            raise ServiceError(404, "not_found", f"metric template {template_id} not found")
        if not t.engine_ready:
            raise ServiceError(422, "engine_not_ready",
                               f"{t.label} is a {t.shape} metric; the engine cannot compute that "
                               f"shape yet, so it is not offered as a number.")
        forks = {**d["fork_choices"], **fork_choices}
        if t.shape == "comparison":
            return self._approve_comparison(d, t, bindings, forks)
        spec = {"numerator": t.numerator, "denominator": t.denominator}
        if t.variants_by:
            choice = forks.get(t.variants_by)
            if choice is None:
                f = m.forks[t.variants_by]
                raise ServiceError(422, "forks_unanswered", f.question,
                                   {"missing": [f.id], "options": [o.id for o in f.options]})
            v = t.variants.get(choice, {})
            if "unsupported" in v:
                raise ServiceError(422, "variant_unsupported", v["unsupported"])
            spec.update(v)
        bound, _ = self._bound(d)
        ctx = runner.Ctx(con=None, workspace_id=d["workspace_id"], table=d["name"], merged=m,
                         bound=bound, measures=set(), metrics={}, fork_choices=forks,
                         validity=[], window=None, festivals={}, params={"bindings": bindings})

        def cols(side) -> list[str]:
            out = []
            for ref in [side] if isinstance(side, str) else side:
                sign = "-" if ref.startswith("-") else ""
                try:
                    out.append(sign + runner.column_for(ctx, ref.lstrip("-")))
                except runner.Skip as e:
                    raise ServiceError(422, "needs_data", f"{t.label}: {e}") from None
            return out

        num_cols, den_cols = cols(spec["numerator"]), cols(spec["denominator"])
        sc = self._contract(d)
        kw = self._contract_kwargs(sc.contract)
        taken = set(kw["measures"]) | {c.name for c in self._evidence(d).columns}
        name = t.id if t.id not in taken else f"{t.id}_ratio"
        if template_id in d["metrics"]:
            name = d["metrics"][template_id]
            kw["measures"].remove(name)
        kw["measures"].append(name)
        kw["aggregations"][name] = "ratio"
        kw["ratios"][name] = {"numerator": num_cols, "denominator": den_cols, "scale": t.scale}
        kw["measure_definitions"][name] = (f"{t.label}: sum({' '.join(num_cols)}) / "
                                           f"sum({' '.join(den_cols)}) x {t.scale:g}, approved "
                                           f"from template {t.id}")
        draft = self.be.draft_contract(d["workspace_id"], d["name"], **kw)
        if draft.provisional or draft.refusal is not None:
            raise ServiceError(422, "metric_refused", draft.refusal.what if draft.refusal else
                               f"still provisional: {draft.provisional}")
        r = self.be.confirm_contract(d["workspace_id"], draft)
        if not r.ok:
            raise _refusal(r)
        self.store.set_dataset_choices(dataset_id, metrics={**d["metrics"], template_id: name},
                                       fork_choices=forks)
        return {"ok": True, "version": self._contract(d).version, "measure": name}

    def _approve_comparison(self, d: dict, t, bindings: dict, forks: dict) -> dict:
        """A 0/1 metric (D-B8-1): proposed and decided as a v1 provisional metric in one step;
        this API call is the person's approval. Results reading it say PROVISIONAL."""
        from backend.engine.contract import provisional
        bound, _ = self._bound(d)
        ctx = runner.Ctx(con=None, workspace_id=d["workspace_id"], table=d["name"],
                         merged=None, bound=bound, measures=set(), metrics={},
                         fork_choices=forks, validity=[], window=None, festivals={},
                         params={"bindings": bindings})
        try:
            left = runner.column_for(ctx, t.concept)
            right = runner.column_for(ctx, t.right) if t.right else None
        except runner.Skip as e:
            raise ServiceError(422, "needs_data", f"{t.label}: {e}") from None
        ws, contract = d["workspace_id"], self._contract(d).contract
        with self.be._workspace(ws):
            con = db.connect(ws)
            try:
                p = provisional.propose(con, ws, d["name"], name=t.id, left=left, op=t.op,
                                        right=right, value=t.value, agg=t.agg,
                                        definition=t.definition, contract=contract)
            except provisional.ProposalError as e:
                raise ServiceError(422, "metric_refused", str(e)) from None
            finally:
                con.close()
            provisional.decide(ws, p.id, True)
        self.store.set_dataset_choices(d["dataset_id"], metrics={**d["metrics"], t.id: t.id},
                                       fork_choices=forks)
        return {"ok": True, "version": self._contract(d).version, "measure": t.id,
                "provisional": provisional.describe(p)}

    # --- validity rules ----------------------------------------------------------------------
    def validity_rules(self, dataset_id: str) -> dict:
        d = self._get(dataset_id)
        m = merge(load_all(), d["domains"])
        bound, _ = self._bound(d)
        con = db.connect_read_only(d["workspace_id"])
        out = []
        try:
            for r in m.validity_rules.values():
                cols = bound.get(r.concept, [])
                n = None
                if len(cols) == 1:
                    c = runner.q(cols[0])
                    where = {"exclude_matching": f"regexp_matches(lower(CAST({c} AS VARCHAR)), "
                                                 f"'{r.pattern}')",
                             "flag_rows": f"regexp_matches(lower(CAST({c} AS VARCHAR)), "
                                          f"'{r.pattern}')",
                             "require_positive": f"NOT coalesce({c} > 0, false)"}.get(r.kind)
                    if r.kind == "require_after" and len(bound.get(r.other or "", [])) == 1:
                        where = "NOT " + runner.after_sql(c, runner.q(bound[r.other][0]))
                    if r.kind == "flag_zero" and len(bound.get(r.other or "", [])) == 1:
                        where = f"{c} > 0 AND coalesce({runner.q(bound[r.other][0])}, 0) = 0"
                    if where:
                        n = con.execute(f"SELECT count(*) FROM {runner.q(d['name'])} "
                                        f"WHERE {where}").fetchone()[0]
                out.append({"rule_id": r.id, "description": r.description,
                            "suggested": True, "approved": r.id in d["validity"],
                            "rows_affected": n})
        finally:
            con.close()
        return {"rules": out}

    def approve_rules(self, dataset_id: str, approve: list[str], reject: list[str]) -> dict:
        d = self._get(dataset_id)
        known = set(merge(load_all(), d["domains"]).validity_rules)
        unknown = [r for r in approve + reject if r not in known]
        if unknown:
            raise ServiceError(422, "unknown_rule", f"unknown rule(s): {unknown}")
        keep = [r for r in d["validity"] if r not in reject]
        keep += [r for r in approve if r not in keep]
        self.store.set_dataset_choices(dataset_id, validity=keep)
        return {"ok": True, "version": 1, "approved": keep}

    # --- tool runs -----------------------------------------------------------------------------
    def run_tool(self, tool_id: str, dataset_id: str, params: dict) -> dict:
        d = self._get(dataset_id)
        packs = load_all()
        m = merge(packs, d["domains"])
        states = {s.tool_id: s for s in self.tools_states(d)}
        st = states.get(tool_id)
        if st is None:
            raise ServiceError(404, "not_found", f"tool {tool_id} not found")
        if st.status != "active":
            raise ServiceError(409, st.status,
                               f"{tool_id} needs the {st.needs_domain} domain confirmed"
                               if st.status == "needs_domain" else
                               f"{tool_id} needs data for: {st.missing_concepts}",
                               {"needs_domain": st.needs_domain,
                                "missing_concepts": st.missing_concepts})
        if m.tools[tool_id].kind == "pipeline":
            got = self.keywords.run(dataset_id, params.get("column"))
            n = len(got["groups"])
            return {"tool_id": tool_id, "dataset_id": dataset_id,
                    "summary": f"{n} keyword group(s) proposed or kept; a person approves them",
                    "figures": [{"name": "groups", "value": n, "unit": "groups",
                                 "provenance": "derived"}], "series": [],
                    "validity_filters_applied": [], "pack_rules_applied": [], "forks": {},
                    "caveats": [f"embedding: {got['run']['embedding']}; threshold "
                                f"{got['run']['threshold']}; groups are PROPOSALS until approved"],
                    "figure_check": {"status": "not_run", "notes": []}}
        if st.pack == "core":
            raise ServiceError(422, "not_a_domain_tool",
                               "core steps run through their own endpoints (profile, cleaning, "
                               "contract); core analyses run through a turn")
        sc = self._contract(d)
        c = sc.contract
        bound, _ = self._bound(d)
        with self.be._workspace(d["workspace_id"]):
            con = db.connect(d["workspace_id"])
            try:
                result = self._run(con, d, m, c, bound, tool_id, params)
            finally:
                con.close()
        result["dataset_id"] = dataset_id
        return result

    def _run(self, con, d, m, c, bound, tool_id, params) -> dict:
        ctx = runner.Ctx(
            con=con, workspace_id=d["workspace_id"], table=d["name"], merged=m,
            bound=bound, measures={x.name for x in c.measures}, metrics=d["metrics"],
            fork_choices=d["fork_choices"], validity=d["validity"],
            window=(c.analysis_window.start, c.analysis_window.end)
            if c.analysis_window else None,
            festivals=m.festivals, params=dict(params))
        return runner.run(ctx, m.tools[tool_id], m.min_group_size)

    def run_core(self, dataset_id: str, analysis: str, params: dict) -> dict:
        """A core (v1-surface) analysis as a one-step tool, for the fallback's `core_analyze`.
        The model supplies column names only: `where` is dropped (the LLM never writes SQL) and
        no value may start with '@' (bindings are the packs' and the runner's)."""
        from backend.engine.analysis.registry import catalogue
        from backend.packs.models import Step, Tool
        if analysis not in {n for n, _, _ in catalogue()}:
            raise ServiceError(422, "unknown_analysis", f"{analysis!r} is not a core analysis")
        clean = {k: v for k, v in (params or {}).items()
                 if k != "where" and not (isinstance(v, str) and v.startswith("@"))}
        tool = Tool(id=f"core.{analysis}", description=analysis, ui_label=analysis,
                    base_analysis=analysis, steps=[Step(analysis=analysis, params=clean)])
        d = self._get(dataset_id)
        m = merge(load_all(), d["domains"])
        c = self._contract(d).contract
        bound, _ = self._bound(d)
        with self.be._workspace(d["workspace_id"]):
            con = db.connect(d["workspace_id"])
            try:
                ctx = runner.Ctx(
                    con=con, workspace_id=d["workspace_id"], table=d["name"], merged=m,
                    bound=bound, measures={x.name for x in c.measures}, metrics=d["metrics"],
                    fork_choices=d["fork_choices"], validity=d["validity"],
                    window=(c.analysis_window.start, c.analysis_window.end)
                    if c.analysis_window else None, festivals=m.festivals, params={})
                result = runner.run(ctx, tool, m.min_group_size)
            finally:
                con.close()
        result["dataset_id"] = dataset_id
        return result

    def tools_states(self, d: dict):
        bound, sources = self._bound(d)
        return registry.statuses(d["domains"], set(bound), sources)


def _measure_type(c, m: str) -> str:
    s = c.suggestions.get(m)
    agg = (c.aggregations.get(m) or (s.agg if s else "") or "").lower()
    if m in c.ratios or agg == "none":
        return "ratio_of_sums" if m in c.ratios else "non_additive"
    return {"sum": "additive", "mean": "weighted_mean", "count_distinct": "distinct_count",
            "median": "percentile"}.get(agg, "unknown")


def packs_list() -> dict:
    return {"packs": [{"pack_id": p.pack.id, "version": p.pack.version,
                       "extends": p.pack.extends, "tools": len(p.tools)}
                      for p in load_all().values()]}


def pack_detail(pack_id: str) -> dict:
    packs = load_all()
    if pack_id not in packs:
        raise ServiceError(404, "not_found", f"pack {pack_id} not found")
    p = packs[pack_id]
    return {"pack_id": pack_id, "version": p.pack.version,
            "pack": p.model_dump(mode="json")}
