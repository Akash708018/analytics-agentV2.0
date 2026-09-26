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
from backend.tools import registry

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
        return {"dataset_id": dataset_id, "domains": det.domains,
                "marketing_sources": det.sources, "confirmed": d["domains"]}

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
                "forks": self._forks(d)}

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
        self.store.set_dataset_choices(dataset_id, fork_choices=fork_choices)
        return {"ok": True, "version": 1}

    # --- tools ---------------------------------------------------------------------------------
    def tools(self, dataset_id: str) -> dict:
        d = self._get(dataset_id)
        bound, sources = self._bound(d)
        states = registry.statuses(d["domains"], set(bound), sources)
        return {"tools": [{"tool_id": s.tool_id, "ui_label": s.ui_label, "status": s.status,
                           "needs_domain": s.needs_domain,
                           "missing_concepts": s.missing_concepts} for s in states]}


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
