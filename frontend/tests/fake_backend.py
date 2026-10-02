"""A stateful in-memory stand-in for the backend's session, turn and dataset routes.

It follows the backend's observable rules (backend/services/sessions.py, checked
live in F1): versions bump on each ui-state write, a stale version answers 409
with `current`, emails are refused 422 `ui_state_rejected`, unknown ids are 404
`not_found`. test_f2_fake.py pins its shapes to the spec's required keys.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

import httpx

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}")
STAMP = "2026-10-01T10:00:00Z"
EXPIRES = "2026-10-31T10:00:00Z"


def _error(status: int, code: str, message: str, **extra) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message,
                                                  "milestone": None}, **extra})


class FakeBackend:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.turns: dict[str, dict] = {}
        self.datasets: dict[str, dict] = {}
        self.calls: list[tuple[str, str, object]] = []
        self.faults: dict[tuple[str, str], tuple[type[Exception], bool]] = {}
        self.down = False
        self.after = None          # called with (method, path) once a request is answered
        self.prep: dict[str, dict] = {}   # per dataset: cleaning pool/plan, domains, contract

    # --- test controls ------------------------------------------------------------------
    def client_factory(self):
        from frontend.api_client import APIClient
        return lambda: APIClient(base_url="http://backend.test",
                                 transport=httpx.MockTransport(self.handle))

    def new_session(self, ui_state: dict | None = None, workspace_id: str = "ws_0123456789ab") -> str:
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"sid": sid, "version": 1, "workspace_id": workspace_id,
                              "ui_state": ui_state or {}, "created_at": STAMP,
                              "updated_at": STAMP, "expires_at": EXPIRES}
        return sid

    def write_elsewhere(self, sid: str, ui_state: dict) -> None:
        """Another tab saved: the version moves on."""
        s = self.sessions[sid]
        s["ui_state"], s["version"] = ui_state, s["version"] + 1

    def add_dataset(self, workspace_id: str, name: str, rows: int = 4, columns: int = 8) -> dict:
        ds = {"dataset_id": f"ds_{uuid.uuid4().hex[:12]}", "workspace_id": workspace_id,
              "name": name, "rows": rows, "columns": columns, "created_at": STAMP,
              "assumptions": ["row 1 is the header"]}
        self.datasets[ds["dataset_id"]] = ds
        return ds

    # --- preparation (shapes and rules measured on the real backend, docs/steps/F3.md) ----
    COLUMNS = [("date", "DATE"), ("campaign", "VARCHAR"), ("utm_source", "VARCHAR"),
               ("cost", "BIGINT"), ("clicks", "BIGINT"), ("ctr", "DOUBLE")]
    CLEANING = [
        {"kind": "DROP_DUPLICATE_ROWS", "column": None, "rows_affected": 1, "lossy": True,
         "suggested": False, "description": "keep one of each of the 1 exactly duplicated row(s)"},
        {"kind": "TRIM_WHITESPACE", "column": "utm_source", "rows_affected": 1, "lossy": False,
         "suggested": True, "description": "strip padding from 1 value(s) in utm_source"},
        {"kind": "NORMALISE_CASE", "column": "utm_source", "rows_affected": 11, "lossy": True,
         "suggested": False, "description": "fold utm_source to one case"},
    ]
    CORE_FORKS = [
        {"fork_id": "tax_basis", "question": "Are money columns before or after GST?",
         "options": [{"id": "gross_incl_gst", "label": "Including GST"},
                     {"id": "net_excl_gst", "label": "Excluding GST"}],
         "suggested": "net_excl_gst", "suggested_reason": "GST is not earned."},
        {"fork_id": "timezone", "question": "Which timezone are the dates in?",
         "options": [{"id": "account_tz", "label": "The ad account's timezone"},
                     {"id": "utc", "label": "UTC"}],
         "suggested": None, "suggested_reason": None},
    ]
    MARKETING_FORKS = [
        {"fork_id": "roas_revenue_basis", "question": "Which revenue should ROAS use?",
         "options": [{"id": "platform", "label": "What the ad platform reports"},
                     {"id": "net_excl_gst", "label": "Order revenue excluding GST"}],
         "suggested": None, "suggested_reason": None},
        {"fork_id": "conversion_source", "question": "Which conversions count?",
         "options": [{"id": "backend_orders", "label": "Orders in your backend"},
                     {"id": "platform", "label": "What the ad platform reports"}],
         "suggested": "backend_orders", "suggested_reason": "Platforms over-count."},
    ]
    TEMPLATES = [
        {"template_id": "ctr", "label": "CTR (%)", "shape": "ratio_of_sums",
         "required_concepts": ["clicks", "impressions"], "forks": [], "available": True},
        {"template_id": "cpa", "label": "CPA", "shape": "ratio_of_sums",
         "required_concepts": ["spend", "conversions"], "forks": ["conversion_source"],
         "available": True},
        {"template_id": "roas", "label": "ROAS", "shape": "ratio_of_sums",
         "required_concepts": ["spend"], "forks": ["roas_revenue_basis"], "available": True},
        {"template_id": "delivered_roas", "label": "Delivered ROAS", "shape": "ratio_of_sums",
         "required_concepts": ["order_revenue", "gst", "spend"], "forks": [], "available": False},
    ]
    RULES = [
        {"rule_id": "exclude_test_campaigns", "description": "Leave out test campaigns.",
         "rows_affected": 3},
        {"rule_id": "roas_spend_positive", "description": "ROAS only where spend > 0.",
         "rows_affected": None},
    ]
    TOOLS = {   # tool_id: (ui_label, needs domain, missing concepts, params_required, slots)
        "marketing.channel_efficiency": ("Which channels pay back", "marketing", [], [],
                                         {"by": ["channel", "campaign"]}),
        "marketing.roas_change_explainer": ("Why ROAS changed", "marketing", [],
                                            ["period", "baseline"], {}),
        "marketing.budget_pacing": ("Budget pacing", "marketing", [], ["budget", "month"], {}),
        "marketing.festive_compare": ("This festival vs last year", "marketing", [],
                                      ["festival", "year"], {}),
        "marketing.creative_fatigue": ("Creative fatigue", "marketing", ["frequency"], [], {}),
        "logistics.otif": ("On time, in full", "logistics", [], [], {}),
        "core.trend": ("Trend", None, [], [], {}),
    }

    def _prep(self, dataset_id: str) -> dict:
        return self.prep.setdefault(dataset_id, {
            "pool": [dict(a) for a in self.CLEANING], "plan": None, "applied": [],
            "domains": [], "contract": None, "version": 0, "confirms": [], "fork_choices": {},
            "metrics": {}, "rules": [], "runs": []})

    def _plan(self, dataset_id: str) -> list[dict]:
        state = self._prep(dataset_id)
        state["plan"] = [{"action_id": f"C{i:03d}", **a} for i, a in enumerate(state["pool"], 1)]
        return state["plan"]

    def _forks(self, dataset_id: str) -> list[dict]:
        state = self._prep(dataset_id)
        return self.CORE_FORKS + (self.MARKETING_FORKS if "marketing" in state["domains"] else [])

    def _proposal(self, dataset_id: str) -> dict:
        state = self._prep(dataset_id)
        c = state["contract"]
        if c:
            return {"dataset_id": dataset_id, "grain": c["grain"], "key": c["primary_key"],
                    "date": c.get("date_column"), "dimensions": c["dimensions"],
                    "measures": [{"column": m, "measure_type": "additive",
                                  "agg": c["aggregations"][m], "suggested_agg": "sum",
                                  "strength": "strong", "reason": "rows add up"}
                                 for m in c["measures"]],
                    "caveats": [], "provisional": [], "questions": [],
                    "forks": self._forks(dataset_id)}
        return {"dataset_id": dataset_id, "grain": "", "key": [], "date": "date",
                "measures": [{"column": "ctr", "measure_type": "non_additive", "agg": "",
                              "suggested_agg": "none", "strength": "strong",
                              "reason": "ctr is named like a per-row value"}],
                "dimensions": ["campaign", "utm_source", "cost", "clicks"],
                "caveats": ["utm_source writes 3 value(s) more than one way"],
                "provisional": ["grain", "measures[ctr].agg", "analysis_window"],
                "questions": ["What is one row of this table?", "How does ctr combine?",
                              "What period should the analysis cover?"],
                "forks": self._forks(dataset_id)}

    def _confirm_contract(self, dataset_id: str, body: dict) -> httpx.Response:
        state = self._prep(dataset_id)
        state["confirms"].append(body)
        forks = {f["fork_id"]: {o["id"] for o in f["options"]} for f in self._forks(dataset_id)}
        choices = body["fork_choices"]
        missing = [f for f in forks if f not in choices]
        invalid = [f"{k}={v}" for k, v in choices.items() if k in forks and v not in forks[k]]
        if missing or invalid:
            return _error(422, "forks_unanswered",
                          "Every fork is answered by the person; none is defaulted.",
                          missing=missing, invalid=invalid)
        c = body["contract"]
        if c.get("primary_key") and c.get("date_column") and c["date_column"] not in c["primary_key"]:
            # Measured (F3): key [campaign] for "one row per campaign per day" is refused.
            return _error(422, "refused", "the engine still finds this contract PROVISIONAL.",
                          refusal={"reason": "LOAD_REFUSED",
                                   "what": "the engine still finds this contract PROVISIONAL.",
                                   "why": "grain, analysis_window."})
        provisional = [] if c.get("grain") else ["grain"]
        provisional += [] if c.get("primary_key") else ["primary_key"]
        provisional += [f"measures[{m}].agg" for m in c.get("measures", [])
                        if m not in c.get("aggregations", {})]
        provisional += [f"measures[{m}].definition" for m in c.get("measures", [])
                        if m not in c.get("measure_definitions", {})]
        if not c.get("analysis_window_start"):
            provisional.append("analysis_window")
        if provisional:
            return _error(422, "contract_provisional", "These fields still need an answer.",
                          provisional=provisional,
                          questions=[f"Please answer: {f}" for f in provisional])
        state["contract"], state["version"] = c, state["version"] + 1
        state["fork_choices"] = {**state["fork_choices"], **choices}
        return httpx.Response(200, json={"ok": True, "version": state["version"],
                                         "measure": None, "provisional": None})

    def _tool_status(self, dataset_id: str, tool_id: str) -> dict:
        label, domain, missing, _, _ = self.TOOLS[tool_id]
        status = ("needs_domain" if domain and domain not in self._prep(dataset_id)["domains"]
                  else "needs_data" if missing else "active")
        return {"tool_id": tool_id, "ui_label": label, "status": status,
                "needs_domain": domain if status == "needs_domain" else None,
                "missing_concepts": missing}

    def _approve_metric(self, dataset_id: str, body: dict) -> httpx.Response:
        state = self._prep(dataset_id)
        template = next((t for t in self.TEMPLATES if t["template_id"] == body["template_id"]), None)
        if template is None:
            return _error(404, "not_found", f"metric template {body['template_id']} not found")
        if state["contract"] is None:
            return _error(409, "contract_required", "Confirm the dataset contract first.")
        if not template["available"]:
            return _error(422, "needs_data", f"{template['label']}: no column is bound to 'gst'")
        bindings = body.get("bindings", {})
        if template["template_id"] == "roas" and "conv_value" not in bindings:
            return _error(422, "needs_data", "ROAS: no column is bound to 'conv_value'")
        if template["template_id"] == "cpa" and "conversions" not in bindings:
            return _error(422, "ambiguous_binding", "2 columns could be 'conversions'",
                          concept="conversions", columns=["conversions", "platform_conversions"])
        state["metrics"][template["template_id"]] = template["template_id"]
        state["version"] += 1
        return httpx.Response(200, json={"ok": True, "version": state["version"],
                                         "measure": template["template_id"], "provisional": None})

    def _run_tool(self, tool_id: str, body: dict) -> httpx.Response:
        dataset_id, params = body["dataset_id"], body.get("params", {})
        state = self._prep(dataset_id)
        state["runs"].append((tool_id, params))
        if tool_id not in self.TOOLS:
            return _error(404, "not_found", f"tool {tool_id} not found")
        if tool_id.startswith("core."):
            return _error(422, "not_a_domain_tool", "core analyses run through a turn")
        status = self._tool_status(dataset_id, tool_id)
        if status["status"] != "active":
            return _error(409, status["status"], f"{tool_id} cannot run",
                          needs_domain=status["needs_domain"],
                          missing_concepts=status["missing_concepts"])
        required = self.TOOLS[tool_id][3]
        if missing := [k for k in required if k not in params]:
            return _error(422, "param_required", f"{tool_id} needs params {missing}",
                          missing=missing)
        if tool_id == "marketing.festive_compare" and params.get("dates_confirmed") is not True:
            return _error(422, "festival_dates_unconfirmed",
                          "Festival dates move every year: confirm them, then re-run.",
                          dates={"2026": ["2026-11-08", "2026-11-08"],
                                 "2025": ["2025-10-20", "2025-10-21"]})
        if tool_id == "marketing.channel_efficiency" and "conversion_source" not in state["fork_choices"]:
            return _error(422, "forks_unanswered", "Answer these before this tool runs.",
                          missing=["conversion_source"],
                          forks=[{"fork_id": "conversion_source",
                                  "question": "Which conversions count?"}])
        return httpx.Response(200, json={
            "tool_id": tool_id, "dataset_id": dataset_id,
            "summary": f"{self.TOOLS[tool_id][0]}: 1 step(s) run",
            "figures": [{"name": "Spend by group: social", "value": 449.0, "unit": "cost (sum)",
                         "provenance": "contract"},
                        {"name": "Spend by group: search", "value": 560.0, "unit": "cost (sum)",
                         "provenance": "contract"},
                        {"name": "CPA: tiny group", "value": None, "unit": None,
                         "provenance": "derived"}],
            "series": [{"chart": "bar", "name": "Spend by group", "x_label": "channel",
                        "y_label": "cost (sum)",
                        "points": [{"x": "social", "y": 449.0}, {"x": "search", "y": 560.0}]}],
            "validity_filters_applied": list(state["rules"]),
            "pack_rules_applied": ["no_sum_of_rate"],
            "forks": {k: v for k, v in state["fork_choices"].items() if k == "conversion_source"},
            "caveats": ["CTR: skipped -- metric 'ctr' is not approved for this dataset"],
            "figure_check": {"status": "not_run", "notes": ["figures come straight from the engine"]}})

    def _prep_route(self, method: str, parts: list[str], body) -> httpx.Response | None:
        if parts[0] == "packs" and method == "GET" and len(parts) == 1:
            return httpx.Response(200, json={"packs": [
                {"pack_id": p, "version": "0.1.0", "extends": [] if p == "core" else ["core"],
                 "tools": n} for p, n in (("core", 0), ("logistics", 6), ("marketing", 34))]})
        if parts[0] == "packs" and method == "GET" and len(parts) == 2:
            pack_id = parts[1]
            tools = [{"id": t, "ui_label": v[0], "description": f"{v[0]} (fake description).",
                      "params_required": v[3], "slots": v[4]}
                     for t, v in self.TOOLS.items() if t.startswith(pack_id + ".")]
            pack = {"pack": {"id": pack_id}, "tools": tools, "forks": [],
                    "festivals": [{"id": "diwali", "name": "Diwali"}] if pack_id == "core" else []}
            return httpx.Response(200, json={"pack_id": pack_id, "version": "0.1.0", "pack": pack})
        if parts[0] == "tools" and method == "POST" and parts[2:] == ["run"]:
            return self._run_tool(parts[1], body)
        if parts[0] != "datasets" or len(parts) < 3 or parts[1] not in self.datasets:
            return None
        dataset_id, rest = parts[1], parts[2:]
        state = self._prep(dataset_id)
        if method == "GET" and rest == ["profile"]:
            return httpx.Response(200, json={
                "dataset_id": dataset_id, "rows": self.datasets[dataset_id]["rows"],
                "columns": [{"name": n, "type": t, "null_pct": 0.0, "distinct": 5,
                             "sample": ["a", "b"]} for n, t in self.COLUMNS],
                "warnings": ["ctr looks like a per-row rate: never sum or plainly average it"]})
        if method == "GET" and rest == ["cleaning", "proposals"]:
            return httpx.Response(200, json={"dataset_id": dataset_id,
                                             "proposals": self._plan(dataset_id)})
        if method == "POST" and rest == ["cleaning", "approve"]:
            if state["plan"] is None:
                return _error(422, "refused", f"nothing has been proposed for {dataset_id}.",
                              refusal={"reason": "NO_CLEANING_PLAN"})
            plan = {a["action_id"]: a for a in state["plan"]}
            unknown = [a for a in body["approve"] if a not in plan]
            if unknown:
                return _error(422, "refused", f"unknown action ids {unknown}")
            chosen = [plan[a] for a in body["approve"]]
            state["applied"] += [(a["action_id"], a["kind"], a["column"]) for a in chosen]
            state["pool"] = [a for a in state["pool"]
                             if not any(a["kind"] == c["kind"] and a["column"] == c["column"]
                                        for c in chosen)]
            state["plan"] = None
            return httpx.Response(200, json={"applied": body["approve"],
                                             "rejected": body.get("reject", []),
                                             "ledger_entries": len(body["approve"])})
        if method == "GET" and rest == ["domains", "detect"]:
            evidence = {"matched_columns": ["campaign", "clicks", "cost"], "score": 0.385}
            return httpx.Response(200, json={
                "dataset_id": dataset_id, "domains": [{"domain": "marketing", "evidence": evidence}],
                "marketing_sources": [{"source": "paid_ads", "evidence": evidence}],
                "sources": [{"source": "paid_ads", "domain": "marketing", "evidence": evidence}],
                "confirmed": state["domains"]})
        if method == "POST" and rest == ["domains", "confirm"]:
            state["domains"] = sorted(set(body["domains"]))
            return httpx.Response(200, json={"ok": True, "version": 1, "measure": None,
                                             "provisional": None})
        if method == "GET" and rest == ["contract", "proposal"]:
            return httpx.Response(200, json=self._proposal(dataset_id))
        if method == "POST" and rest == ["contract", "confirm"]:
            return self._confirm_contract(dataset_id, body)
        if method == "GET" and rest == ["metrics", "templates"]:
            return httpx.Response(200, json={"templates": [
                {**t, "approved": t["template_id"] in state["metrics"],
                 "measure": state["metrics"].get(t["template_id"])} for t in self.TEMPLATES]})
        if method == "POST" and rest == ["metrics", "approve"]:
            return self._approve_metric(dataset_id, body)
        if method == "GET" and rest == ["validity-rules"]:
            return httpx.Response(200, json={"rules": [
                {**r, "suggested": True, "approved": r["rule_id"] in state["rules"]}
                for r in self.RULES]})
        if method == "POST" and rest == ["validity-rules", "approve"]:
            keep = [r for r in state["rules"] if r not in body.get("reject", [])]
            state["rules"] = keep + [r for r in body["approve"] if r not in keep]
            return httpx.Response(200, json={"ok": True, "version": 1, "measure": None,
                                             "provisional": None})
        if method == "GET" and rest == ["tools"]:
            return httpx.Response(200, json={"tools": [self._tool_status(dataset_id, t)
                                                       for t in self.TOOLS]})
        if method == "POST" and rest == ["forks"]:
            state["fork_choices"] = {**state["fork_choices"], **body["fork_choices"]}
            return httpx.Response(200, json={"ok": True, "version": 1, "measure": None,
                                             "provisional": None})
        return None

    def add_event(self, turn_id: str, kind: str, data: dict) -> None:
        t = self.turns[turn_id]
        t["events"].append({"seq": len(t["events"]), "type": kind, "at": STAMP, "data": data})

    def answer_turn(self, turn_id: str, *, flags=(), with_result: bool = True) -> None:
        """A finished playbook turn shaped like the F5 probe of the real backend."""
        steps = ["marketing.spend_waste", "marketing.keyword_ngrams"]
        self.add_event(turn_id, "plan", {"playbook": "where_is_spend_wasted", "slots": {},
                                         "steps": steps})
        self.add_event(turn_id, "tool_call", {"tool_id": steps[0], "params": {}, "status": "ok",
                                              "ms": 167})
        self.add_event(turn_id, "tool_call", {
            "tool_id": steps[1], "params": {}, "status": "skipped", "code": "needs_data",
            "reason": "text_ngrams: none of ['search_term', 'query', 'keyword'] is in this data",
            "ms": 102})
        self.add_event(turn_id, "figure_check", {"status": "passed", "checked": 3,
                                                 "detail": "3 of 3 figure(s) match the tool replies."})
        self.add_event(turn_id, "interpretation_check", {"rule": "all", "status": "passed",
                                                         "detail": ""})
        text = "Spend goes mostly to search: 560.0 (55.5%)."
        self.add_event(turn_id, "answer", {"text": text, "flags": list(flags)})
        result = {"tool_id": steps[0], "dataset_id": self.turns[turn_id]["dataset_id"],
                  "summary": "Where spend is wasted: 2 step(s) run",
                  "figures": [{"name": "Where the spend goes: 1", "value": 560.0, "unit": "cost",
                               "provenance": "contract"},
                              {"name": "Where the spend goes: 1 [share]", "value": 55.5,
                               "unit": "share", "provenance": "contract"},
                              {"name": "Conversions per group: tiny", "value": None,
                               "unit": None, "provenance": "contract"}],
                  "series": [{"chart": "bar", "name": "Where the spend goes", "x_label": "rank",
                              "y_label": "cost", "points": [{"x": "1", "y": 560.0},
                                                            {"x": "2", "y": 449.0}]}],
                  "validity_filters_applied": ["exclude_test_campaigns"],
                  "pack_rules_applied": [], "forks": {}, "caveats": ["Small groups suppressed."],
                  "figure_check": {"status": "not_run", "notes": []}}
        self.turns[turn_id].update(status="done", answer={
            "text": text, "flags": list(flags), "playbook": "where_is_spend_wasted",
            "results": [result] if with_result else [],
            "skipped": [{"tool_id": steps[1], "code": "needs_data", "reason": "no text column"}],
            "usage": {"llm_calls": 2, "tool_calls": 2, "tokens_in_est": 1001,
                      "tokens_out_est": 56, "per_call": [], "model": "scripted"}})

    def finish_turn(self, turn_id: str, text: str = "ROAS fell from 4.1 to 3.2.") -> None:
        t = self.turns[turn_id]
        t["status"] = "done"
        t["events"].append({"seq": len(t["events"]), "type": "answer", "at": STAMP,
                            "data": {"text": text}})
        t["answer"] = {"text": text, "flags": [], "playbook": None, "results": [],
                       "skipped": [], "usage": {"llm_calls": 0, "tool_calls": 0}}

    def fail_on(self, method: str, path: str, error: type[Exception], *, stored: bool) -> None:
        """Raise a transport error for the next matching request, after (stored=True) or
        before the server acts on it."""
        self.faults[(method, path)] = (error, stored)

    def count(self, method: str, prefix: str = "") -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p.startswith(prefix))

    def bodies(self, method: str, prefix: str = "") -> list[object]:
        return [b for m, p, b in self.calls if m == method and p.startswith(prefix)]

    # --- the HTTP surface --------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        method, path = request.method, request.url.path
        body: object = None
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"null")
        self.calls.append((method, path, body))
        fault = self.faults.pop((method, path), None)
        if fault and not fault[1]:
            raise fault[0]("injected", request=request)
        response = self._route(method, path, body, request)
        if self.after is not None:
            self.after(method, path)
        if fault:
            raise fault[0]("injected after the server acted", request=request)
        return response

    def _route(self, method: str, path: str, body, request) -> httpx.Response:
        parts = path.strip("/").split("/")
        if (prep := self._prep_route(method, parts, body)) is not None:
            return prep
        if method == "POST" and parts == ["sessions"]:
            sid = self.new_session(workspace_id=(body or {}).get("workspace_id")
                                   or f"ws_{uuid.uuid4().hex[:12]}")
            return httpx.Response(201, json={"sid": sid, "version": 1})
        if parts[0] == "sessions" and len(parts) >= 2:
            s = self.sessions.get(parts[1])
            if s is None:
                return _error(404, "not_found", f"session {parts[1]} not found or expired")
            if method == "GET" and len(parts) == 2:
                return httpx.Response(200, json=s)
            if method == "PUT" and parts[2:] == ["ui-state"]:
                if body["version"] != s["version"]:
                    return _error(409, "version_conflict",
                                  f"version {body['version']} is stale; current is "
                                  f"{s['version']}", current=s)
                if _EMAIL.search(json.dumps(body["ui_state"])):
                    return _error(422, "ui_state_rejected",
                                  "ui_state.label: looks like personal data (email/phone)")
                s["ui_state"], s["version"] = body["ui_state"], s["version"] + 1
                s["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                return httpx.Response(200, json=s)
            if method == "GET" and parts[2:] == ["turns"]:
                mine = [t for t in self.turns.values() if t["sid"] == s["sid"]]
                return httpx.Response(200, json={"turns": mine})
        if method == "POST" and parts == ["turns"]:
            if body["sid"] not in self.sessions:
                return _error(404, "not_found", f"session {body['sid']} not found or expired")
            turn_id = f"t_{uuid.uuid4().hex[:16]}"
            self.turns[turn_id] = {"turn_id": turn_id, "sid": body["sid"],
                                   "dataset_id": body["dataset_id"],
                                   "question": body["question"], "status": "running",
                                   "events": [], "answer": None, "created_at": STAMP}
            return httpx.Response(202, json={"turn_id": turn_id})
        if method == "GET" and parts[0] == "turns" and len(parts) == 2:
            t = self.turns.get(parts[1])
            return httpx.Response(200, json=t) if t else _error(404, "not_found",
                                                                f"turn {parts[1]} not found")
        if method == "POST" and parts[0] == "workspaces" and parts[2:] == ["uploads"]:
            filename = re.search(rb'filename="([^"]+)"', request.content).group(1).decode()
            return httpx.Response(201, json=self.add_dataset(parts[1], filename.rsplit(".", 1)[0]))
        if method == "GET" and parts[0] == "datasets" and len(parts) == 2:
            ds = self.datasets.get(parts[1])
            return httpx.Response(200, json=ds) if ds else _error(
                404, "not_found", f"dataset {parts[1]} not found")
        return _error(404, "not_found", f"{method} {path} is not in the fake")
