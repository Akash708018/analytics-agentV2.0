"""API contract v0.1: every request/response model, each with an example.

This module IS the contract. `docs/api/openapi.yaml` is exported from it and a test fails on
drift. Change a model -> bump API_VERSION -> add a docs/api/CHANGELOG.md entry.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "0.9.0"


def _ex(*examples: dict) -> ConfigDict:
    return ConfigDict(json_schema_extra={"examples": list(examples)})


# --- errors ------------------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine code, e.g. not_found, version_conflict, "
                                  "not_implemented, ui_state_too_large, ui_state_rejected")
    message: str
    milestone: str | None = Field(None, description="For not_implemented: the milestone "
                                                    "that makes the endpoint live")
    model_config = _ex({"code": "not_implemented",
                        "message": "GET /datasets/{id}/profile is spec'd but not live yet",
                        "milestone": "B2"})


class ErrorBody(BaseModel):
    error: ErrorDetail
    model_config = _ex({"error": {"code": "not_found", "message": "session x not found or expired", "milestone": None}})


# --- sessions ----------------------------------------------------------------------------------

class SessionCreate(BaseModel):
    workspace_id: str | None = Field(None, description="Reuse a workspace (its datasets "
                                     "persist). Omitted: a new workspace is created.")
    model_config = _ex({}, {"workspace_id": "ws_3f9a1c2b7d10"})


class SessionCreated(BaseModel):
    sid: str = Field(description="UUID4")
    version: int
    model_config = _ex({"sid": "0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11", "version": 1})


class Session(BaseModel):
    sid: str
    version: int = Field(description="Increments on every ui-state write; send it back "
                                     "with PUT /ui-state (optimistic concurrency)")
    workspace_id: str
    ui_state: dict[str, Any]
    created_at: str
    updated_at: str
    expires_at: str = Field(description="Sliding: 30 days after the last activity")
    model_config = _ex({
        "sid": "0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11", "version": 3,
        "workspace_id": "ws_3f9a1c2b7d10",
        "ui_state": {"screen": "ask", "dataset_id": "ds_ads_2026q3", "panel_open": True},
        "created_at": "2026-09-26T10:00:00Z", "updated_at": "2026-09-26T10:05:00Z",
        "expires_at": "2026-10-26T10:05:00Z"})


class UiStatePut(BaseModel):
    ui_state: dict[str, Any] = Field(description="<=256 KB JSON. No data rows, API keys "
                                     "or PII: rejected with 422")
    version: int = Field(description="The version you last read")
    model_config = _ex({"ui_state": {"screen": "contract", "dataset_id": "ds_ads_2026q3"},
                        "version": 3})


class VersionConflict(BaseModel):
    error: ErrorDetail
    current: Session
    model_config = _ex({
        "error": {"code": "version_conflict",
                  "message": "version 2 is stale; current is 3", "milestone": None},
        "current": Session.model_config["json_schema_extra"]["examples"][0]})


# --- turns -------------------------------------------------------------------------------------

EventType = Literal["plan", "tool_call", "provider_wait", "failover", "answer",
                    "figure_check", "interpretation_check", "error"]
TurnStatus = Literal["queued", "running", "done", "failed", "interrupted"]


class TurnCreate(BaseModel):
    sid: str
    dataset_id: str
    question: str = Field(min_length=1, max_length=4000)
    model_config = _ex({"sid": "0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11",
                        "dataset_id": "ds_ads_2026q3",
                        "question": "Why did ROAS drop in September?"})


class TurnCreated(BaseModel):
    turn_id: str
    model_config = _ex({"turn_id": "t_7c1d9e0a4b2f4e59"})


class TurnEvent(BaseModel):
    seq: int
    type: EventType
    at: str
    data: dict[str, Any] = Field(description=(
        "plan: {playbook, steps[]} | tool_call: {tool_id, params, result_ref, ms} | "
        "provider_wait: {provider, seconds} | failover: {from, to, reason} | "
        "answer: {text} | figure_check: {status, checked, corrected[]} | "
        "interpretation_check: {rule, status, detail} | error: {code, message}"))
    model_config = _ex(
        {"seq": 0, "type": "plan", "at": "2026-09-26T10:06:00Z",
         "data": {"playbook": "why_roas_dropped",
                  "steps": ["marketing.roas_change_explainer", "marketing.channel_mix_shift"]}},
        {"seq": 1, "type": "tool_call", "at": "2026-09-26T10:06:01Z",
         "data": {"tool_id": "marketing.roas_change_explainer",
                  "params": {"period": "2026-09", "baseline": "2026-08"}, "ms": 412}},
        {"seq": 2, "type": "provider_wait", "at": "2026-09-26T10:06:02Z",
         "data": {"provider": "gemini", "seconds": 12}},
        {"seq": 3, "type": "answer", "at": "2026-09-26T10:06:20Z",
         "data": {"text": "ROAS fell from 4.1 to 3.2 ..."}},
        {"seq": 4, "type": "figure_check", "at": "2026-09-26T10:06:21Z",
         "data": {"status": "passed", "checked": 6, "corrected": []}})


class Turn(BaseModel):
    turn_id: str
    sid: str
    dataset_id: str
    question: str
    status: TurnStatus
    events: list[TurnEvent]
    answer: dict[str, Any] | None = Field(None, description=(
        "When done: {text, flags[] (unresolved checks, shown under the answer), playbook "
        "(id or null), results[] (ToolResult), skipped[] {tool_id, code, reason}, usage "
        "{llm_calls, tool_calls, tokens_in_est, tokens_out_est, per_call[], model}}"))
    created_at: str
    model_config = _ex({
        "turn_id": "t_7c1d9e0a4b2f4e59", "sid": "0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11",
        "dataset_id": "ds_ads_2026q3", "question": "Why did ROAS drop in September?",
        "status": "done",
        "events": [{"seq": 0, "type": "answer", "at": "2026-09-26T10:06:20Z",
                    "data": {"text": "ROAS fell from 4.1 to 3.2 ..."}}],
        "answer": {"text": "ROAS fell from 4.1 to 3.2 ...", "flags": [],
                   "playbook": "why_roas_dropped", "results": [], "skipped": [],
                   "usage": {"llm_calls": 2, "tool_calls": 3, "tokens_in_est": 2348,
                             "tokens_out_est": 164}},
        "created_at": "2026-09-26T10:06:00Z"})


class TurnList(BaseModel):
    turns: list[Turn]
    model_config = _ex({"turns": [Turn.model_config["json_schema_extra"]["examples"][0]]})


# --- results (provenance) ----------------------------------------------------------------------

class Figure(BaseModel):
    name: str
    value: float | int | str | None
    unit: str | None = None
    provenance: Literal["contract", "provisional", "derived"]
    model_config = _ex({"name": "ROAS", "value": 3.21, "unit": "x", "provenance": "contract"})


class SeriesPoint(BaseModel):
    x: str | float | int
    y: float | int | None


class ChartSeries(BaseModel):
    """Chart-ready: computed by the backend. The frontend only plots."""
    chart: Literal["line", "bar", "stacked_bar", "scatter", "table", "funnel"]
    name: str
    x_label: str
    y_label: str
    points: list[SeriesPoint]
    model_config = _ex({"chart": "bar", "name": "ROAS by channel", "x_label": "channel",
                        "y_label": "ROAS",
                        "points": [{"x": "google", "y": 3.8}, {"x": "meta", "y": 2.4}]})


class FigureCheck(BaseModel):
    status: Literal["passed", "corrected", "flagged", "not_run"]
    notes: list[str] = []


class ToolResult(BaseModel):
    tool_id: str
    dataset_id: str
    summary: str
    figures: list[Figure]
    series: list[ChartSeries] = []
    validity_filters_applied: list[str] = Field(description="By name")
    pack_rules_applied: list[str]
    forks: dict[str, str] = Field({}, description="fork id -> the option the person chose")
    caveats: list[str] = []
    figure_check: FigureCheck
    # 0.7.0 typed result contract (B9): identity, what it was computed on, and its status
    result_id: str | None = Field(None, description="Stored result id (GET /results/{id})")
    run_id: str | None = Field(None, description="The turn that ran it, else its own id")
    status: Literal["ok", "partial", "insufficient_data"] | None = Field(
        None, description="partial: a step was skipped or refused; insufficient_data: no "
        "reportable figure")
    snapshot: dict[str, Any] | None = Field(None, description="{hash, rows} of the table it "
                                            "was computed on")
    contract_version: int | None = None
    grain: str | None = None
    metrics_used: dict[str, Any] = Field({}, description="template id -> {measure}")
    model_config = _ex({
        "tool_id": "marketing.channel_efficiency", "dataset_id": "ds_ads_2026q3",
        "summary": "Google ROAS 3.8 vs Meta 2.4 on net-of-GST revenue",
        "figures": [{"name": "ROAS google", "value": 3.8, "unit": "x",
                     "provenance": "contract"}],
        "series": [{"chart": "bar", "name": "ROAS by channel", "x_label": "channel",
                    "y_label": "ROAS", "points": [{"x": "google", "y": 3.8},
                                                   {"x": "meta", "y": 2.4}]}],
        "validity_filters_applied": ["exclude_test_campaigns", "roas_spend_positive"],
        "pack_rules_applied": ["no_sum_of_rate"],
        "forks": {"roas_revenue_basis": "net_excl_gst"},
        "caveats": ["3 spend rows with 0 impressions flagged, not dropped"],
        "figure_check": {"status": "passed", "notes": []}})


class StoredResult(ToolResult):
    stale: bool = Field(description="The data, contract or a metric it read changed since")
    stale_reasons: list[str] = []
    created_at: str


class ResultSummary(BaseModel):
    result_id: str
    tool_id: str
    run_id: str
    status: str
    created_at: str
    stale: bool
    stale_reasons: list[str] = []


class ResultList(BaseModel):
    dataset_id: str
    results: list[ResultSummary]
    model_config = _ex({"dataset_id": "ds_ads_2026q3", "results": [
        {"result_id": "r_0a1b2c3d4e5f6a7b", "tool_id": "logistics.sla_compliance",
         "run_id": "r_0a1b2c3d4e5f6a7b", "status": "ok", "created_at": "2026-10-02T09:00:00+00:00",
         "stale": True, "stale_reasons": ["the data changed (cleaning or a new upload) since "
                                          "this result"]}]})


class ResultInspection(BaseModel):
    result_id: str
    step: str
    steps: list[str]
    headers: list[str]
    rows: list[list[Any]] = Field(description="Engine rows as computed: sorted / filtered "
                                  "here, never recomputed")
    total_rows: int = Field(description="Rows matching the group filter among those kept")
    rows_kept: int
    rows_in_engine_output: int
    model_config = _ex({"result_id": "r_0a1b2c3d4e5f6a7b", "step": "Breach rate",
                        "steps": ["Breach rate"], "headers": ["hub", "n", "total"],
                        "rows": [["Pune_South", 94, "63.8%"]], "total_rows": 1,
                        "rows_kept": 5, "rows_in_engine_output": 5})


class ToolRunRequest(BaseModel):
    dataset_id: str
    params: dict[str, Any] = Field({}, description=(
        "Slot overrides (a column name, e.g. by), period/baseline 'YYYY-MM-DD/YYYY-MM-DD', "
        "bindings {concept: column} when a concept is ambiguous, brand_terms, festival + year "
        "+ dates_confirmed. 409 needs_domain/needs_data; 422 forks_unanswered, "
        "ambiguous_binding, param_required, festival_dates_unconfirmed, engine_refused"))
    model_config = _ex({"dataset_id": "ds_ads_2026q3", "params": {"by": "channel"}},
                       {"dataset_id": "ds_ads_2026q3", "params": {
                           "period": "2026-09-16/2026-09-30",
                           "baseline": "2026-09-01/2026-09-15"}})


# --- datasets ----------------------------------------------------------------------------------

class Dataset(BaseModel):
    dataset_id: str
    workspace_id: str
    name: str
    rows: int
    columns: int
    created_at: str
    assumptions: list[str] = Field([], description="How the file was read (header row, "
                                   "types). Shown to the person; the upload is refused with "
                                   "422 ingest_needs_answers when the layout is ambiguous")
    model_config = _ex({"dataset_id": "ds_ads_2026q3", "workspace_id": "ws_3f9a1c2b7d10",
                        "name": "google_ads_q3", "rows": 18240, "columns": 14,
                        "created_at": "2026-09-26T10:01:00Z",
                        "assumptions": ["row 1 is the header"]})


class ColumnProfile(BaseModel):
    name: str
    type: str
    null_pct: float
    distinct: int
    sample: list[Any]


class Profile(BaseModel):
    dataset_id: str
    rows: int
    columns: list[ColumnProfile]
    warnings: list[str]
    model_config = _ex({"dataset_id": "ds_ads_2026q3", "rows": 18240,
                        "columns": [{"name": "cost", "type": "DOUBLE", "null_pct": 0.0,
                                     "distinct": 9120, "sample": [12.5, 40.0]}],
                        "warnings": ["ctr is a per-row rate: never sum it"]})


class CleaningProposal(BaseModel):
    action_id: str
    kind: str
    column: str | None
    description: str
    rows_affected: int
    lossy: bool = False
    suggested: bool = Field(False, description="Lossless and conflict-free: may be pre-ticked "
                                               "for the person; never applied without approval")
    values_lost: int = Field(0, description="0.8.0: what a lossy step loses, in loss_unit")
    loss_unit: str = ""
    samples: list[dict[str, Any]] = Field([], description="0.8.0: up to 3 of the engine's own "
                                          "examples: {row, copies} for a duplicate, {value} "
                                          "for a value the step changes")
    sql: str | None = Field(None, description="0.8.0: the exact statement the step runs")


class CleaningProposals(BaseModel):
    dataset_id: str
    proposals: list[CleaningProposal]
    model_config = _ex({"dataset_id": "ds_ads_2026q3", "proposals": [
        {"action_id": "a1", "kind": "normalise_case", "column": "utm_source",
         "description": "'Google' and 'google' are one source", "rows_affected": 311}]})


class ApproveActions(BaseModel):
    approve: list[str] = Field(description="ids to apply")
    reject: list[str] = []
    model_config = _ex({"approve": ["a1"], "reject": []})


class ApprovalResult(BaseModel):
    applied: list[str]
    rejected: list[str]
    ledger_entries: int
    model_config = _ex({"applied": ["a1"], "rejected": [], "ledger_entries": 1})


class Evidence(BaseModel):
    matched_columns: list[str]
    score: float


class DomainCandidate(BaseModel):
    domain: str
    evidence: Evidence


class SourceCandidate(BaseModel):
    source: Literal["paid_ads", "ga4", "search_console", "email", "social", "orders"]
    evidence: Evidence


class DomainSource(BaseModel):
    domain: str = Field(description="The pack the source belongs to (marketing, logistics)")
    source: str
    evidence: Evidence


class DomainDetection(BaseModel):
    dataset_id: str
    domains: list[DomainCandidate]
    marketing_sources: list[SourceCandidate]
    sources: list[DomainSource] = Field([], description="0.6.0: every pack's matched sources "
                                        "(marketing_sources is the marketing subset)")
    confirmed: list[str] = Field(description="Domains the person confirmed; a guess alone "
                                             "never enables domain tools")
    model_config = _ex({
        "dataset_id": "ds_ads_2026q3",
        "domains": [{"domain": "marketing", "evidence": {
            "matched_columns": ["campaign", "impressions", "clicks", "cost"], "score": 0.92}}],
        "marketing_sources": [{"source": "paid_ads", "evidence": {
            "matched_columns": ["campaign", "ad_group", "impressions", "cost"],
            "score": 0.88}}],
        "confirmed": []})


class DomainConfirm(BaseModel):
    domains: list[str]
    model_config = _ex({"domains": ["marketing"]})


class ForkOption(BaseModel):
    id: str
    label: str


class ForkQuestion(BaseModel):
    fork_id: str
    question: str = Field(description="Plain language")
    options: list[ForkOption]
    suggested: str | None = None
    suggested_reason: str | None = None
    model_config = _ex({
        "fork_id": "roas_revenue_basis",
        "question": "Which revenue should ROAS use?",
        "options": [{"id": "platform", "label": "What the ad platform reports"},
                    {"id": "gross_incl_gst", "label": "Order revenue including GST"},
                    {"id": "net_excl_gst", "label": "Order revenue excluding GST"},
                    {"id": "delivered_net", "label": "Net, after cancels/returns/RTO"}],
        "suggested": "net_excl_gst",
        "suggested_reason": "GST is not revenue you keep"})


class MeasureProposal(BaseModel):
    column: str
    measure_type: str = Field(description="additive | ratio_of_sums | weighted_mean | "
                              "distinct_count | percentile | semi_additive | non_additive | "
                              "unknown -- read from the suggestion, never applied")
    agg: str = Field(description="The agreed aggregation; empty until the person answers")
    suggested_agg: str | None = None
    strength: str | None = Field(None, description="strong | likely | unsure")
    reason: str = ""


class ContractProposal(BaseModel):
    dataset_id: str
    grain: str
    key: list[str]
    date: str | None
    measures: list[MeasureProposal]
    dimensions: list[str]
    caveats: list[str]
    provisional: list[str] = Field([], description="Fields that still need the person's "
                                   "answer; confirm is refused (422) while non-empty")
    questions: list[str] = []
    forks: list[ForkQuestion]
    prefill: dict[str, Any] | None = Field(None, description=(
        "0.7.0: answers suggested from the most similar dataset with a confirmed contract in "
        "the same workspace (column-name Jaccard >= 0.8): from_dataset_id, similarity, contract "
        "(fields of this file's columns, no window), fork_choices, domains, metrics, "
        "validity_rules. Never applied until the person confirms."))
    model_config = _ex({
        "dataset_id": "ds_ads_2026q3", "grain": "one row per campaign per day",
        "key": ["date", "campaign"], "date": "date",
        "measures": [{"column": "cost", "measure_type": "additive", "agg": "sum"},
                     {"column": "ctr", "measure_type": "ratio_of_sums",
                      "agg": "sum(clicks)/sum(impressions)"}],
        "dimensions": ["campaign", "channel"], "caveats": [],
        "forks": [ForkQuestion.model_config["json_schema_extra"]["examples"][0]]})


class ContractConfirm(BaseModel):
    contract: dict[str, Any] = Field(description="The proposal as edited by the person")
    fork_choices: dict[str, str]
    model_config = _ex({"contract": {"grain": "one row per campaign per day",
                                     "primary_key": ["date", "campaign"],
                                     "date_column": "date", "measures": ["cost", "clicks"],
                                     "dimensions": ["campaign"],
                                     "aggregations": {"cost": "sum", "clicks": "sum"}},
                        "fork_choices": {"roas_revenue_basis": "net_excl_gst"}})


class Confirmed(BaseModel):
    ok: bool
    version: int
    measure: str | None = Field(None, description="metrics/approve: the measure added")
    provisional: str | None = Field(None, description="metrics/approve of a comparison "
                                    "template (0/1 per row): what the engine measured about it; "
                                    "results reading it say PROVISIONAL (not in the contract)")
    model_config = _ex({"ok": True, "version": 1}, {"ok": True, "version": 2, "measure": "roas"})


class ContractMeasure(BaseModel):
    column: str
    agg: str
    definition: str
    per: list[str] = []
    ratio: dict[str, Any] | None = Field(None, description="Ratio measures: numerator, "
                                         "denominator, scale")


class ContractInForce(BaseModel):
    dataset_id: str
    version: int
    confirmed_at: str
    grain: str
    primary_key: list[str]
    date_column: str | None
    measures: list[ContractMeasure]
    dimensions: list[str]
    analysis_window_start: str | None = None
    analysis_window_end: str | None = None
    caveats: list[str] = Field([], description="Declared by the person")
    measured_caveats: list[str] = Field([], description="Counted by the engine from the table")
    fork_choices: dict[str, str] = {}
    metrics: dict[str, str] = Field({}, description="Approved template id -> measure")
    validity_rules: list[str] = Field([], description="Approved validity rule ids")
    model_config = _ex({
        "dataset_id": "ds_ads_2026q3", "version": 1,
        "confirmed_at": "2026-10-02T09:00:00+00:00", "grain": "one row per campaign per day",
        "primary_key": ["date", "campaign"], "date_column": "date",
        "measures": [{"column": "cost", "agg": "sum", "definition": "Spend in INR"}],
        "dimensions": ["campaign"], "analysis_window_start": "2026-07-01",
        "analysis_window_end": "2026-09-30", "caveats": [], "measured_caveats": [],
        "fork_choices": {"tax_basis": "net_excl_gst"}, "metrics": {}, "validity_rules": []})


class ColumnAnswer(BaseModel):
    source: str = Field(description="The column as the file names it (preview.guess.columns)")
    target: str | None = Field(None, description="The name to load it under")
    type: str | None = Field(None, description="A DuckDB type, e.g. VARCHAR, DOUBLE, DATE")


class UploadAnswers(BaseModel):
    header_rows: list[int] | None = Field(None, description="1-based rows that form the "
                                          "header; answering this settles `header_rows`")
    header_join: Literal["space", "underscore", "bottom_only", "top_only"] | None = None
    sheet: str | None = None
    data_start: int | None = Field(None, description="1-based first data row")
    footer_rows: int | None = Field(None, description="Rows at the end to leave out (totals)")
    name: str | None = Field(None, description="Dataset name")
    columns: list[ColumnAnswer] | None = None
    model_config = _ex({"header_rows": [1, 2], "header_join": "space"})


class AnalysisField(BaseModel):
    name: str
    kind: str = Field(description="measure | dimension | column | grain | period | date | "
                      "integer | number | list | text")
    required: bool
    choices: list[str] | None = Field(None, description="The contract's own options (measures,"
                                      " dimensions, columns, grains); none for free values")


class AnalysisSpec(BaseModel):
    name: str
    tier: int
    summary: str
    fields: list[AnalysisField]


class AnalysisList(BaseModel):
    dataset_id: str
    analyses: list[AnalysisSpec]
    model_config = _ex({"dataset_id": "ds_ads_2026q3", "analyses": [
        {"name": "period_compare", "tier": 3, "summary": "One measure, two periods.",
         "fields": [{"name": "measure", "kind": "measure", "required": True,
                     "choices": ["cost", "clicks"]},
                    {"name": "period", "kind": "period", "required": True},
                    {"name": "baseline", "kind": "period", "required": True},
                    {"name": "grain", "kind": "grain", "required": False,
                     "choices": ["day", "week", "month", "quarter", "year"]}]}]})


class ReportRequest(BaseModel):
    playbook: str
    slots: dict[str, str] = {}
    model_config = _ex({"playbook": "sla_where_and_why", "slots": {}})


class Report(BaseModel):
    dataset_id: str
    playbook: str
    run_id: str
    description: str
    rules: list[str] = []
    results: list[ToolResult]
    skipped: list[dict[str, Any]] = []
    model_config = _ex({
        "dataset_id": "ds_ads_2026q3", "playbook": "sla_where_and_why",
        "run_id": "rep_0a1b2c3d4e5f6a7b", "description": "Where SLA breaches concentrate.",
        "rules": ["sla_on_delivered_only"],
        "results": [ToolResult.model_config["json_schema_extra"]["examples"][0]],
        "skipped": []})


class ForkAnswers(BaseModel):
    fork_choices: dict[str, str]
    model_config = _ex({"fork_choices": {"conversion_source": "backend_orders",
                                         "roas_revenue_basis": "net_excl_gst"}})


class MetricTemplate(BaseModel):
    template_id: str
    label: str
    shape: str
    required_concepts: list[str]
    forks: list[str]
    available: bool = Field(description="Its concepts are in the data and the engine can "
                                        "compute its shape")
    approved: bool = False
    measure: str | None = Field(None, description="The contract measure it became")
    model_config = _ex({"template_id": "roas", "label": "ROAS",
                        "shape": "ratio_of_sums", "required_concepts": ["spend"],
                        "forks": ["roas_revenue_basis"], "available": True,
                        "approved": True, "measure": "roas"})


class MetricTemplates(BaseModel):
    templates: list[MetricTemplate]
    model_config = _ex({"templates": [MetricTemplate.model_config["json_schema_extra"]["examples"][0]]})


class MetricApprove(BaseModel):
    template_id: str
    bindings: dict[str, str] = Field(description="concept -> column")
    fork_choices: dict[str, str] = {}
    model_config = _ex({"template_id": "roas",
                        "bindings": {"order_revenue": "net_revenue", "spend": "cost"},
                        "fork_choices": {"roas_revenue_basis": "net_excl_gst"}})


class ValidityRule(BaseModel):
    rule_id: str
    description: str
    suggested: bool
    approved: bool
    rows_affected: int | None
    model_config = _ex({"rule_id": "exclude_test_campaigns",
                        "description": "Drop campaigns whose name contains 'test'",
                        "suggested": True, "approved": False, "rows_affected": 42})


class ValidityRules(BaseModel):
    rules: list[ValidityRule]
    model_config = _ex({"rules": [ValidityRule.model_config["json_schema_extra"]["examples"][0]]})


class RuleApprove(BaseModel):
    approve: list[str]
    reject: list[str] = []
    model_config = _ex({"approve": ["exclude_test_campaigns"], "reject": ["roas_spend_positive"]})


class ToolStatus(BaseModel):
    tool_id: str
    ui_label: str
    status: Literal["active", "needs_domain", "needs_data"]
    needs_domain: str | None = None
    missing_concepts: list[str] = []
    model_config = _ex({"tool_id": "marketing.striking_distance",
                        "ui_label": "Queries close to page one", "status": "needs_data",
                        "missing_concepts": ["position", "query"]})


class ToolList(BaseModel):
    tools: list[ToolStatus]
    model_config = _ex({"tools": [{"tool_id": "marketing.channel_efficiency", "ui_label": "Which channels pay back", "status": "active", "needs_domain": None, "missing_concepts": []}, {"tool_id": "logistics.otif", "ui_label": "On time, in full", "status": "needs_domain", "needs_domain": "logistics", "missing_concepts": []}, ToolStatus.model_config["json_schema_extra"]["examples"][0]]})


class PackSummary(BaseModel):
    pack_id: str
    version: str
    extends: list[str]
    tools: int
    model_config = _ex({"pack_id": "marketing", "version": "0.1.0", "extends": ["core"],
                        "tools": 20})


class PackList(BaseModel):
    packs: list[PackSummary]
    model_config = _ex({"packs": [{"pack_id": "core", "version": "0.1.0", "extends": [], "tools": 0}, PackSummary.model_config["json_schema_extra"]["examples"][0]]})


class PackDetail(BaseModel):
    pack_id: str
    version: str
    pack: dict[str, Any] = Field(description="The validated pack document")
    model_config = _ex({"pack_id": "marketing", "version": "0.1.0", "pack": {"pack": {"id": "marketing", "extends": ["core"]}, "forks": [], "tools": []}})


class KeywordGroup(BaseModel):
    group_id: str
    label: str
    intent: Literal["informational", "commercial", "transactional", "navigational", "local"]
    keywords: list[str]
    facets: dict[str, Any] = {}
    approved: bool
    proposed_by: str = Field("rules", description="rules | llm | person")
    generation: str | None = Field(None, description="0.7.0: the embedding generation it was "
                                   "proposed under (model, dims, normalisation, input)")
    joins: str | None = Field(None, description="0.7.0: carry-forward -- these new keywords "
                              "look like they belong to this APPROVED group; merge [that "
                              "group, this one] to accept")
    model_config = _ex({"group_id": "g1", "label": "sushi delivery", "intent": "transactional",
                        "keywords": ["sushi delivery pune", "sushi home delivery"],
                        "facets": {"delivery": ["delivery"], "area": ["baner"]},
                        "approved": False, "proposed_by": "rules"})


class KeywordGroups(BaseModel):
    dataset_id: str
    groups: list[KeywordGroup]
    run: dict[str, Any] | None = Field(None, description="The last run: column, embedding "
                                       "backend, threshold, typos merged, counts")
    model_config = _ex({"dataset_id": "ds_gsc_pune", "groups": [KeywordGroup.model_config["json_schema_extra"]["examples"][0]]})


class KeywordRun(BaseModel):
    column: str | None = Field(None, description="Text column; default: the search term / "
                                                 "query / keyword column")
    backend: Literal["auto", "chargram", "ollama"] = Field("auto", description="0.7.0: the "
                                                           "embedding backend for this run")
    model_config = _ex({"column": "search_term"})


class KeywordGroupAction(BaseModel):
    action: Literal["approve", "unapprove", "rename", "merge", "move_keyword", "split"] = Field(
        description="unapprove (0.8.0): withdraw an approval; the group goes back to proposal "
                    "and the engine stops reading it")
    group_ids: list[str]
    label: str | None = None
    keyword: str | None = None
    keywords: list[str] | None = Field(None, description="split: the keywords to move out")
    target_group_id: str | None = None
    model_config = _ex({"action": "move_keyword", "group_ids": ["g1"],
                        "keyword": "sushi near me", "target_group_id": "g4"})


class Health(BaseModel):
    status: Literal["ok"]
    model_config = _ex({"status": "ok"})


class VersionInfo(BaseModel):
    api_version: str
    engine: str
    model_config = _ex({"api_version": API_VERSION, "engine": "v1@0ba324b"})
