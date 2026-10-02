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
         "suggested": False, "description": "keep one of each of the 1 exactly duplicated row(s)",
         "values_lost": 1, "loss_unit": "row",          # API 0.8.0 (#21), shapes from the B10 probe
         "samples": [{"row": {"date": "2026-09-01", "campaign": "brand", "utm_source": "google",
                              "cost": 120, "clicks": 40, "ctr": 0.02}, "copies": 2}],
         "sql": 'CREATE OR REPLACE TABLE "ads" AS SELECT DISTINCT * FROM "ads"'},
        {"kind": "TRIM_WHITESPACE", "column": "utm_source", "rows_affected": 1, "lossy": False,
         "suggested": True, "description": "strip padding from 1 value(s) in utm_source",
         "values_lost": 0, "loss_unit": "value", "samples": [{"value": " google"}],
         "sql": 'CREATE OR REPLACE TABLE "ads" AS SELECT * REPLACE (trim("utm_source") AS '
                '"utm_source") FROM "ads"'},
        {"kind": "NORMALISE_CASE", "column": "utm_source", "rows_affected": 11, "lossy": True,
         "suggested": False, "description": "fold utm_source to one case",
         "values_lost": 3, "loss_unit": "distinct value",
         "samples": [{"value": "Google"}, {"value": "GOOGLE"}, {"value": "google"}],
         "sql": 'CREATE OR REPLACE TABLE "ads" AS SELECT * REPLACE (lower("utm_source") AS '
                '"utm_source") FROM "ads"'},
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
        "marketing.keyword_grouping": ("Group keywords", "marketing", [], [], {}),
        "logistics.sla_drivers": ("What goes with breaches", "logistics", [], [],
                                  {"by": ["hub", "courier", "zone"]}),
        "logistics.otif": ("On time, in full", "logistics", [], [], {}),
        "logistics.stuck_shipments": ("Stuck shipments", "logistics", [], ["as_of"], {}),
        "core.trend": ("Trend", None, [], [], {}),
    }

    # Keyword groups (shapes and rules measured on the real backend, docs/steps/F6.md).
    KEYWORD_GROUPS = [   # what a run proposes: label, intent, keywords, facets
        ("sushi delivery", "transactional",
         ["sushi delivery baner", "sushi delivery pune", "sushi home delivery"],
         {"area": ["baner"], "delivery": ["delivery"]}),
        ("sushi · near me", "local", ["sushi near me", "sushi nearby"],
         {"near_me": ["near me"], "dish": ["sushi"]}),
        ("sushi · info", "informational", ["what is sushi"], {"info": ["what is"], "dish": ["sushi"]}),
        ("menu · brand", "navigational", ["hana menu"], {"brand": ["hana"]}),
    ]

    def _prep(self, dataset_id: str) -> dict:
        return self.prep.setdefault(dataset_id, {
            "pool": [dict(a) for a in self.CLEANING], "plan": None, "applied": [],
            "domains": [], "contract": None, "version": 0, "confirms": [], "fork_choices": {},
            "metrics": {}, "rules": [], "runs": [], "kw": {}, "kw_run": None})

    def _kw_new_id(self) -> str:
        self.kw_ids = getattr(self, "kw_ids", 0) + 1
        return f"p{self.kw_ids:06x}"

    def _kw_save(self, groups: dict, g: dict) -> None:
        groups[g["group_id"]] = {**g, "keywords": sorted(set(g["keywords"]))}

    def _kw_listing(self, dataset_id: str) -> httpx.Response:
        state = self._prep(dataset_id)
        groups = sorted(state["kw"].values(), key=lambda g: (not g["approved"], g["group_id"]))
        return httpx.Response(200, json={"dataset_id": dataset_id, "groups": groups,
                                         "run": state["kw_run"]})

    def _kw_run(self, dataset_id: str, body) -> httpx.Response:
        state, column = self._prep(dataset_id), (body or {}).get("column")
        if column is None:
            return _error(422, "needs_data", "no search term, query or keyword column")
        if column not in [n for n, _ in self.COLUMNS]:                    # 500 until B10 (#20)
            return _error(422, "needs_data", f"no column {column!r} in this dataset")
        groups = state["kw"]
        kept = {k for g in groups.values() if g["approved"] for k in g["keywords"]}
        for gid in [gid for gid, g in groups.items() if not g["approved"]]:
            del groups[gid]
        n = 0
        for label, intent, keywords, facets in self.KEYWORD_GROUPS:
            if kws := [k for k in keywords if k not in kept]:
                n += 1
                self._kw_save(groups, {"group_id": self._kw_new_id(), "label": label,
                                       "intent": intent, "keywords": kws, "facets": facets,
                                       "approved": False, "proposed_by": "rules"})
        state["kw_run"] = {"column": column, "embedding": "chargram/tfidf-char2-4",
                           "threshold": 0.6, "typos_merged": {"sushii": "sushi"},
                           "keywords": sum(len(g[2]) for g in self.KEYWORD_GROUPS),
                           "proposed_groups": n}
        return self._kw_listing(dataset_id)

    def _kw_act(self, dataset_id: str, a: dict) -> httpx.Response:
        """backend/services/keywords.py KeywordService.act, rule for rule."""
        groups = self._prep(dataset_id)["kw"]
        ids = a.get("group_ids") or []
        missing = [i for i in ids if i not in groups]
        if missing:
            return _error(404, "not_found", f"unknown group(s): {missing}")
        if not ids:
            return _error(422, "bad_action", "group_ids needed")
        g = {i: {**groups[i], "keywords": list(groups[i]["keywords"]),
                 "facets": dict(groups[i]["facets"])} for i in ids}
        kind = a["action"]
        if kind in ("approve", "unapprove"):                 # unapprove: API 0.8.0 (#20)
            for i in ids:
                self._kw_save(groups, {**g[i], "approved": kind == "approve"})
        elif kind == "rename":
            if not a.get("label"):
                return _error(422, "bad_action", "rename needs label")
            self._kw_save(groups, {**g[ids[0]], "label": a["label"], "proposed_by": "person"})
        elif kind == "merge":
            if len(ids) < 2:
                return _error(422, "bad_action", "merge needs two or more group_ids")
            into = g[ids[0]]
            for i in ids[1:]:
                into["keywords"] += g[i]["keywords"]
                for f, v in g[i]["facets"].items():
                    into["facets"][f] = sorted(set(into["facets"].get(f, [])) | set(v))
                del groups[i]
            into["label"] = a.get("label") or into["label"]
            self._kw_save(groups, {**into, "proposed_by": "person"})
        elif kind == "move_keyword":
            kw, target = a.get("keyword"), a.get("target_group_id")
            src = g[ids[0]]
            if kw not in src["keywords"] or target not in groups:
                return _error(422, "bad_action", "keyword not in group, or no target group")
            src["keywords"].remove(kw)
            self._kw_save(groups, {**groups[target], "keywords": groups[target]["keywords"] + [kw]})
            if src["keywords"]:
                self._kw_save(groups, src)
            else:
                del groups[src["group_id"]]
        elif kind == "split":
            kws = a.get("keywords") or ([a["keyword"]] if a.get("keyword") else [])
            src = g[ids[0]]
            if not kws or not set(kws) <= set(src["keywords"]) or set(kws) == set(src["keywords"]):
                return _error(422, "bad_action", "split needs some (not all) of the group's keywords")
            self._kw_save(groups, {**src, "keywords": [k for k in src["keywords"] if k not in kws]})
            self._kw_save(groups, {"group_id": self._kw_new_id(),
                                   "label": a.get("label") or f"{src['label']} (split)",
                                   "intent": src["intent"], "keywords": kws, "facets": src["facets"],
                                   "approved": False, "proposed_by": "person"})
        return self._kw_listing(dataset_id)

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
                    "forks": self._forks(dataset_id), "prefill": state.get("prefill")}
        return {"dataset_id": dataset_id, "grain": "", "key": [], "date": "date",
                "measures": [{"column": "ctr", "measure_type": "non_additive", "agg": "",
                              "suggested_agg": "none", "strength": "strong",
                              "reason": "ctr is named like a per-row value"}],
                "dimensions": ["campaign", "utm_source", "cost", "clicks"],
                "caveats": ["utm_source writes 3 value(s) more than one way"],
                "provisional": ["grain", "measures[ctr].agg", "analysis_window"],
                "questions": ["What is one row of this table?", "How does ctr combine?",
                              "What period should the analysis cover?"],
                "forks": self._forks(dataset_id), "prefill": state.get("prefill")}

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
        if tool_id.startswith("core."):                     # API 0.9.0 (#22): direct runs
            return self._run_analysis(tool_id.split(".", 1)[1], dataset_id, params)
        if tool_id not in self.TOOLS:
            return _error(404, "not_found", f"tool {tool_id} not found")
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
        if tool_id == "logistics.sla_drivers" and params.get("focus") is not None:
            focus = str(params["focus"]).lower()
            matches = [h for h in self.HUBS if focus in h.lower()]
            if not matches:
                return _error(422, "unknown_value", f"no hub value matches '{params['focus']}'",
                              param="focus", column="hub", values=self.HUBS)
            if len(matches) > 1 and focus not in [h.lower() for h in matches]:
                return _error(422, "ambiguous_value",
                              f"'{params['focus']}' could be several hub values: {matches}; "
                              "pass one exactly in params.focus",
                              param="focus", column="hub", candidates=matches)
        if tool_id == "marketing.channel_efficiency" and "conversion_source" not in state["fork_choices"]:
            return _error(422, "forks_unanswered", "Answer these before this tool runs.",
                          missing=["conversion_source"],
                          forks=[{"fork_id": "conversion_source",
                                  "question": "Which conversions count?"}])
        return httpx.Response(200, json=self._store_result({
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
            "figure_check": {"status": "not_run", "notes": ["figures come straight from the engine"]}}))

    # --- API 0.8.0 / 0.9.0 (B10, B11; shapes from the F10 probe of the real backend) --------
    OPTIONAL = {   # as packs/logistics/pack.yaml declares them
        "logistics.sla_drivers": [{"name": "focus", "kind": "text", "default": None, "help":
                                   "Which hub, courier or zone to look inside; spellings and "
                                   "aliases are matched"}],
        "logistics.stuck_shipments": [{"name": "days", "kind": "number", "default": "3",
                                       "help": "Open at least this many days before as_of"}]}
    PLAYBOOKS = [
        {"id": "where_is_spend_wasted", "description": "Where ad spend buys nothing.",
         "patterns": ["wasted"], "slots": {}, "rules": ["no_sum_of_rate"], "max_tool_calls": 6,
         "steps": [{"tool": "marketing.channel_efficiency", "params": {}, "optional": False},
                   {"tool": "marketing.creative_fatigue", "params": {}, "optional": True}],
         "requires_metrics": [], "requires_concepts": [], "recovery": ""},
        {"id": "why_roas_dropped", "description": "Why ROAS fell between two months.",
         "patterns": ["roas"], "slots": {"period": "the later month, YYYY-MM",
                                         "baseline": "the earlier month, YYYY-MM"},
         "rules": [], "max_tool_calls": 6,
         "steps": [{"tool": "marketing.roas_change_explainer",
                    "params": {"period": "@slot:period", "baseline": "@slot:baseline"},
                    "optional": False}],
         "requires_metrics": ["roas"], "requires_concepts": [],
         "recovery": "Approve the ROAS metric on Metrics, then build the report again."},
        {"id": "sla_where_and_why", "description": "Where SLA breaches concentrate.",
         "patterns": ["sla"], "slots": {}, "rules": [], "max_tool_calls": 6,
         "steps": [{"tool": "logistics.sla_drivers", "params": {}, "optional": False}],
         "requires_metrics": ["sla_breach"], "requires_concepts": [], "recovery": "Approve it."},
    ]
    ANALYSES = [   # name, tier, summary, "field:kind[:req]" (the real catalogue, B11)
        ('cross_tab', 1, 'Rows of one declared dimension against the values of anot...',
         'rows:dimension:req columns:dimension:req measure:measure'),
        ('distribution', 1, "The shape of one declared measure over the contract's row...",
         'measure:measure:req bins:integer'),
        ('frequency', 1, 'How many rows carry each value of a declared dimension, w...',
         'column:column:req limit:integer'),
        ('summary_stats', 1, "Each declared measure over the contract's rows: its total...",
         ''),
        ('top_n', 1, 'The largest groups of a declared dimension by a declared...',
         'dimension:dimension:req measure:measure:req n:integer period:period grain:grain'),
        ('concentration', 2, 'How much of a declared measure the largest groups of a de...',
         'dimension:dimension:req measure:measure:req period:period grain:grain'),
        ('group_compare', 2, 'One declared measure summarised per group of a declared d...',
         'dimension:dimension:req measure:measure:req groups:list'),
        ('pareto', 2, 'How few groups of a declared dimension carry most of a de...',
         'dimension:dimension:req measure:measure:req threshold:number period:period grain:grain'),
        ('ranking_shift', 2, 'How the ranking of a declared dimension by a declared mea...',
         'dimension:dimension:req measure:measure:req '
         'before_start:date:req before_end:date:req after_start:date:req after_end:date:req'),
        ('calendar_coverage', 3, "Which periods of the contract's date column hold rows and...",
         'grain:grain'),
        ('growth_decomposition', 3, 'The change in one declared measure between two named peri...',
         'measure:measure:req dimension:dimension:req '
         'period:period:req baseline:period:req grain:grain'),
        ('period_compare', 3, 'One declared measure in two named periods, with the diffe...',
         'measure:measure:req period:period:req baseline:period:req grain:grain'),
        ('seasonality', 3, 'One declared measure folded onto the positions of its cyc...',
         'measure:measure:req grain:grain'),
        ('trend', 3, 'One declared measure per period, over a calendar that inc...',
         'measure:measure:req grain:grain dimension:dimension'),
        ('bivariate', 4, 'How one declared measure behaves across the range of anot...',
         'measure:measure:req against:measure:req bins:integer'),
        ('correlation', 4, 'Two declared measures against each other, Pearson and Spe...',
         'measure:measure:req against:measure:req'),
        ('driver_analysis', 4, 'Every declared dimension ranked by how much of one measur...',
         'measure:measure:req'),
        ('mix_shift', 4, "A change in one measure's per-row average between two per...",
         'measure:measure:req dimension:dimension:req '
         'period:period:req baseline:period:req grain:grain'),
        ('changepoint', 5, 'Where one declared measure changes level across a calenda...',
         'measure:measure:req grain:grain'),
        ('correlated_shift', 5, 'Whether two declared measures change level at the same po...',
         'measure:measure:req against:measure:req grain:grain'),
        ('outlier_detection', 5, 'Unusual values in one declared measure by three methods a...',
         'measure:measure:req dimension:dimension'),
        ('confidence_interval', 6, 'The range a mean or a share is consistent with, given how...',
         'dimension:dimension measure:measure confidence:number groups:list'),
        ('effect_size', 6, 'How large a difference is, in units that do not grow with...',
         'dimension:dimension:req measure:measure second_dimension:dimension groups:list'),
        ('hypothesis_test', 6, 'Whether groups of a declared dimension differ by more tha...',
         'dimension:dimension:req measure:measure '
         'second_dimension:dimension method:text groups:list'),
        ('sample_adequacy', 6, 'How large a difference the rows in scope could have detec...',
         'dimension:dimension:req measure:measure:req power:number alpha:number groups:list'),
        ('cohort_retention', 7, 'How many people from each starting period came back in ea...',
         'entity:column:req period:period'),
        ('repeat_behaviour', 7, 'How many people appear once and how many come back, how o...',
         'entity:column:req event:column'),
    ]
    GRAINS = ["day", "week", "month", "quarter", "year"]
    MULTIHEADER = [["Identifiers", None, "Dimensions", None, "Measures", None],
                   ["order_id", "order_date", "region", "channel", "units", "revenue"],
                   ["ORD-00001", "2024-01-22", "North", "Online", "39", "4133.22"],
                   ["ORD-00002", "2024-12-26", "South", "Online", "2", "26.7"]]

    def _needs_answers(self, upload_id: str, unresolved=("header_rows",)) -> httpx.Response:
        names = self.MULTIHEADER[1]
        guess = {"header_rows": [1, 2], "header_join": "bottom_only", "data_start_row": 3,
                 "footer_skip_rows": 0, "name": "multiheader",
                 "columns": [{"source": n, "target": n, "type": None} for n in names]}
        return _error(422, "ingest_needs_answers",
                      "The file's layout needs answers before it can be read: answer them with "
                      "POST /workspaces/{ws}/uploads/{upload_id}/answers.",
                      upload_id=upload_id, unresolved=list(unresolved),
                      questions=["Is row 1 part of the header, or a title? Answer decides "
                                 "whether the column names carry its labels."],
                      preview={"sheet": None, "sheet_names": [], "first_row_number": 1,
                               "rows": self.MULTIHEADER, "guess": guess})

    def _answer_upload(self, workspace_id: str, upload_id: str, body: dict) -> httpx.Response:
        if not upload_id.startswith("multiheader"):
            return _error(404, "not_found", f"upload {upload_id} not found")
        if "header_rows" not in (body or {}):           # as probed: still asked, same upload_id
            return self._needs_answers(upload_id)
        ds = self.add_dataset(workspace_id, body.get("name") or "multiheader", rows=300,
                              columns=len(self.MULTIHEADER[1]))
        ds["assumptions"] = [f"header rows {body['header_rows']} joined "
                             f"{body.get('header_join') or 'bottom_only'}"]
        return httpx.Response(201, json=ds)

    def _contract_in_force(self, dataset_id: str) -> httpx.Response:
        state = self._prep(dataset_id)
        c = state["contract"]
        if c is None:
            return _error(409, "contract_required", "Confirm the dataset contract first.")
        return httpx.Response(200, json={
            "dataset_id": dataset_id, "version": state["version"],
            "confirmed_at": "2026-10-02T09:00:00+00:00", "grain": c["grain"],
            "primary_key": c["primary_key"], "date_column": c.get("date_column"),
            "measures": [{"column": m, "agg": c["aggregations"][m],
                          "definition": (c.get("measure_definitions") or {}).get(m, ""),
                          "per": (c.get("measure_per") or {}).get(m, []), "ratio": None}
                         for m in c["measures"]],
            "dimensions": c["dimensions"],
            "analysis_window_start": c.get("analysis_window_start"),
            "analysis_window_end": c.get("analysis_window_end"),
            "caveats": c.get("caveats", []),
            "measured_caveats": ["utm_source writes 3 value(s) more than one way"],
            "fork_choices": dict(state["fork_choices"]), "metrics": dict(state["metrics"]),
            "validity_rules": list(state["rules"])})

    def _analyses(self, dataset_id: str) -> httpx.Response:
        c = self._prep(dataset_id)["contract"]
        if c is None:
            return _error(409, "contract_required", "Confirm the dataset contract first.")
        choices = {"measure": c["measures"], "dimension": c["dimensions"],
                   "column": sorted(n for n, _ in self.COLUMNS), "grain": self.GRAINS}
        return httpx.Response(200, json={"dataset_id": dataset_id, "analyses": [
            {"name": name, "tier": tier, "summary": summary, "fields": [
                {"name": f.split(":")[0], "kind": f.split(":")[1], "required": f.endswith(":req"),
                 "choices": choices.get(f.split(":")[1])} for f in fields.split()]}
            for name, tier, summary, fields in self.ANALYSES]})

    def _run_analysis(self, name: str, dataset_id: str, params: dict) -> httpx.Response:
        spec = next((a for a in self.ANALYSES if a[0] == name), None)
        if spec is None:
            return _error(404, "not_found", f"no core analysis {name!r}")
        state = self._prep(dataset_id)
        if state["contract"] is None:
            return _error(409, "contract_required", "Confirm the dataset contract first.")
        fields = {f.split(":")[0]: f.endswith(":req") for f in spec[3].split()}
        given = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        if unknown := sorted(set(given) - set(fields)):
            return _error(422, "unknown_params", f"{name} takes {sorted(fields)}; not {unknown}",
                          unknown=unknown, fields=sorted(fields))
        if missing := [f for f, req in fields.items() if req and f not in given]:
            return _error(422, "param_required", f"{name} needs {missing}", missing=missing)
        return httpx.Response(200, json=self._store_result({
            "tool_id": f"core.{name}", "dataset_id": dataset_id,
            "summary": f"{name}: 1 step(s) run",
            "figures": [{"name": f"{name}: campaign brand", "value": 449.0, "unit": "cost (sum)",
                         "provenance": "contract"}],
            "series": [], "validity_filters_applied": list(state["rules"]),
            "pack_rules_applied": [], "forks": {}, "caveats": [],
            "figure_check": {"status": "not_run", "notes": []}}))

    def _report(self, dataset_id: str, body: dict) -> httpx.Response:
        """backend/services/datasets.py `report`, rule for rule (B11)."""
        state = self._prep(dataset_id)
        pb = next((b for b in self.PLAYBOOKS if b["id"] == body.get("playbook")), None)
        if pb is None or pb["steps"][0]["tool"].split(".")[0] not in state["domains"]:
            return _error(404, "not_found", f"no playbook {body.get('playbook')!r} for this data")
        if missing := [f"the approved metric '{t}'" for t in pb["requires_metrics"]
                       if t not in state["metrics"]]:
            return _error(422, "playbook_blocked", f"{pb['id']} needs: " + "; ".join(missing),
                          missing=missing, recovery=pb["recovery"])
        slots = body.get("slots") or {}
        if absent := [s for s in pb["slots"] if s not in slots]:
            return _error(422, "param_required", f"{pb['id']} needs slots {absent}",
                          missing=absent, slots=pb["slots"])
        run_id, results, skipped = f"rep_{uuid.uuid4().hex[:16]}", [], []
        for step in pb["steps"]:
            params = {k: (slots.get(v[6:]) if isinstance(v, str) and v.startswith("@slot:") else v)
                      for k, v in step["params"].items()}
            reply = self._run_tool(step["tool"], {"dataset_id": dataset_id, "params": params})
            if reply.status_code == 200:
                results.append(reply.json())
                continue
            error = reply.json()["error"]
            skipped.append({"tool_id": step["tool"], "code": error["code"],
                            "reason": error["message"]})
            if not step["optional"]:
                break
        return httpx.Response(200, json={"dataset_id": dataset_id, "playbook": pb["id"],
                                         "run_id": run_id, "description": pb["description"],
                                         "rules": pb["rules"], "results": results,
                                         "skipped": skipped})


    # --- stored results (API 0.7.0; shapes from the F8 probe) ------------------------------
    HUBS = ["PUNE_CENTRAL", "PUNE_EAST", "PUNE_SOUTH", "PUNE_WEST"]
    STEPS = {"Spend by group": (["channel", "n", "cost (sum)"],
                                [["social", "40", "449.0"], ["search", "52", "560.0"],
                                 ["video", "7", "88.5"]]),
             "Spend by week": (["period", "cost (sum)"], [["2026-W36", "300.0"], ["2026-W37", "709.0"]])}

    def _store_result(self, result: dict) -> dict:
        self.stored = getattr(self, "stored", {})
        result_id = f"r_{len(self.stored) + 1:016x}"
        state = self._prep(result["dataset_id"])
        result.update(result_id=result_id, run_id=result_id, status="ok",
                      snapshot={"hash": "e63766b0e708a9c4-12", "rows": 12},
                      contract_version=state["version"] or None,
                      grain=(state["contract"] or {}).get("grain"),
                      metrics_used={t: {"measure": m} for t, m in state["metrics"].items()})
        self.stored[result_id] = {**result, "stale": False, "stale_reasons": [],
                                  "created_at": f"2026-10-02T10:0{len(self.stored)}:00+00:00"}
        return result

    def make_stale(self, result_id: str, reason: str = "the data changed (cleaning or a new "
                                                          "upload) since this result") -> None:
        self.stored[result_id].update(stale=True, stale_reasons=[reason])

    def _results_route(self, method: str, parts: list[str], params) -> httpx.Response | None:
        stored = getattr(self, "stored", {})
        if method == "GET" and parts[0] == "datasets" and parts[2:] == ["results"]:
            return httpx.Response(200, json={"dataset_id": parts[1], "results": [
                {k: r[k] for k in ("result_id", "tool_id", "run_id", "status", "created_at",
                                   "stale", "stale_reasons")}
                for r in stored.values() if r["dataset_id"] == parts[1]]})
        if parts[0] != "results" or method != "GET":
            return None
        if parts[1] not in stored:
            return _error(404, "not_found", f"result {parts[1]} not found")
        if len(parts) == 2:
            return httpx.Response(200, json=stored[parts[1]])
        step = params.get("step") or next(iter(self.STEPS))
        if step not in self.STEPS:
            return _error(422, "unknown_step", f"no step {step!r}", steps=list(self.STEPS))
        headers, rows = self.STEPS[step]
        if (group := params.get("group")):
            rows = [r for r in rows if r[0].lower() == group.lower()]
        if (sort_by := params.get("sort_by")):
            if sort_by not in headers or getattr(self, "refuse_sort", False):
                return _error(422, "unknown_column", f"sort_by '{sort_by}'; columns: {headers}",
                              columns=headers)
            i = headers.index(sort_by)
            rows = sorted(rows, key=lambda r: float(r[i]) if r[i].replace(".", "", 1).isdigit()
                          else r[i], reverse=params.get("descending") == "true")
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
        return httpx.Response(200, json={
            "result_id": parts[1], "step": step, "steps": list(self.STEPS), "headers": headers,
            "rows": rows[offset:offset + limit], "total_rows": len(rows),
            "rows_kept": len(self.STEPS[step][1]), "rows_in_engine_output": len(self.STEPS[step][1])})

    def _prep_route(self, method: str, parts: list[str], body, params=None) -> httpx.Response | None:
        if (found := self._results_route(method, parts, params or {})) is not None:
            return found
        if parts[0] == "packs" and method == "GET" and len(parts) == 1:
            return httpx.Response(200, json={"packs": [
                {"pack_id": p, "version": "0.1.0", "extends": [] if p == "core" else ["core"],
                 "tools": n} for p, n in (("core", 0), ("logistics", 6), ("marketing", 34))]})
        if parts[0] == "packs" and method == "GET" and len(parts) == 2:
            pack_id = parts[1]
            tools = [{"id": t, "ui_label": v[0], "description": f"{v[0]} (fake description).",
                      "params_required": v[3], "slots": v[4],
                      "params_optional": self.OPTIONAL.get(t, []),
                      "steps": [{"analysis": "group_compare", "filter": [
                          {"concept": "@by", "focus_param": "focus"}]}]
                      if t == "logistics.sla_drivers" else []}
                     for t, v in self.TOOLS.items() if t.startswith(pack_id + ".")]
            pack = {"pack": {"id": pack_id}, "tools": tools, "forks": [],
                    "playbooks": [b for b in self.PLAYBOOKS if b["steps"][0]["tool"].startswith(
                        pack_id + ".")],
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
        if method == "GET" and rest == ["contract"]:
            return self._contract_in_force(dataset_id)
        if method == "GET" and rest == ["analyses"]:
            return self._analyses(dataset_id)
        if method == "POST" and rest == ["reports"]:
            return self._report(dataset_id, body)
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
        if method == "POST" and rest == ["keyword-groups", "run"]:
            return self._kw_run(dataset_id, body)
        if method == "GET" and rest == ["keyword-groups"]:
            return self._kw_listing(dataset_id)
        if method == "POST" and rest == ["keyword-groups", "actions"]:
            return self._kw_act(dataset_id, body)
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

    def block_turn(self, turn_id: str) -> None:
        """A playbook whose requirements are missing: nothing runs (F8 probe, API 0.7.0)."""
        blocked = ["the approved metric 'sla_breach'"]
        recovery = ("Approve the SLA breach metric (metrics screen: sla_breach = delivery "
                    "minutes > promised minutes).")
        self.add_event(turn_id, "plan", {"playbook": "sla_where_and_why", "slots": {}, "steps": [],
                                         "routed_by": "rules", "blocked": blocked,
                                         "recovery": recovery})
        text = f"Before I can answer this, this dataset needs {blocked[0]}. {recovery}"
        self.add_event(turn_id, "answer", {"text": text, "flags": []})
        self.turns[turn_id].update(status="done", answer={
            "text": text, "flags": [], "playbook": "sla_where_and_why", "results": [],
            "skipped": [], "blocked": blocked,
            "usage": {"llm_calls": 0, "tool_calls": 0, "tokens_in_est": 0, "tokens_out_est": 0,
                      "per_call": [], "model": None}})

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
        if request.url.params:                          # F8: inspect's query, as sent
            self.queries = getattr(self, "queries", []) + [(path, dict(request.url.params))]
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
        if (prep := self._prep_route(method, parts, body, dict(request.url.params))) is not None:
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
            if filename.startswith("multiheader"):            # API 0.8.0 (#16): refused, asked
                return self._needs_answers(filename)
            return httpx.Response(201, json=self.add_dataset(parts[1], filename.rsplit(".", 1)[0]))
        if method == "POST" and parts[0] == "workspaces" and len(parts) == 5 \
                and parts[2] == "uploads" and parts[4] == "answers":
            return self._answer_upload(parts[1], parts[3], body)
        if method == "GET" and parts[0] == "datasets" and len(parts) == 2:
            ds = self.datasets.get(parts[1])
            return httpx.Response(200, json=ds) if ds else _error(
                404, "not_found", f"dataset {parts[1]} not found")
        return _error(404, "not_found", f"{method} {path} is not in the fake")
