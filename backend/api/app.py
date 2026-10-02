"""FastAPI routes. Thin: parse, call a service, shape the reply. No logic here."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from backend.api import schemas as S
from backend.services import datasets as D
from backend.services.sessions import Runner, ServiceError, SessionService, TurnService
from backend.sessions.store import Store

REPO = Path(__file__).resolve().parents[2]
ERR = {"model": S.ErrorBody}


def create_app(state_dir: Path | str | None = None, runner: Runner | None = None,
               now=None, llm=None) -> FastAPI:
    """`runner` overrides everything (tests); else `llm` (or v1's configured providers) answers
    turns through the playbook agent; with no model at all, turns say so (agent_not_wired)."""
    state = Path(state_dir or os.environ.get("AA_STATE_DIR", REPO / "state"))
    store = Store(state / "sessions.db")
    sessions = SessionService(store, now=now)
    ds = D.DatasetService(store)
    from backend.services.keywords import KeywordService
    ds.keywords = KeywordService(ds)
    if runner is None:
        from backend.llm.provider import EngineLLM
        from backend.playbooks.agent import runner as agent_runner
        if llm is None and os.environ.get("AA_NO_LLM") != "1":
            engine = EngineLLM()
            llm = engine if engine.available() else None
        if llm is not None:
            runner = agent_runner(ds, llm)
    turns = TurnService(store, sessions, runner=runner)

    app = FastAPI(title="analytics-agent v2", version=S.API_VERSION,
                  description="Contract-first API. Figures carry provenance; chart series are "
                              "computed by the backend. 501 = spec'd, not live yet.")
    app.state.sessions, app.state.turns, app.state.datasets = sessions, turns, ds

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, e: ServiceError) -> JSONResponse:
        body: dict[str, Any] = {"error": {"code": e.code, "message": e.message,
                                          "milestone": None}}
        body.update(e.extra)
        return JSONResponse(body, status_code=e.status)

    # --- live: meta ------------------------------------------------------------------------
    @app.get("/health", response_model=S.Health, tags=["meta"])
    def health() -> S.Health:
        return S.Health(status="ok")

    @app.get("/version", response_model=S.VersionInfo, tags=["meta"])
    def version() -> S.VersionInfo:
        return S.VersionInfo(api_version=S.API_VERSION, engine="v1@0ba324b")

    # --- live: sessions --------------------------------------------------------------------
    @app.post("/sessions", response_model=S.SessionCreated, status_code=201, tags=["sessions"])
    def create_session(body: S.SessionCreate | None = None) -> S.SessionCreated:
        s = sessions.create((body or S.SessionCreate()).workspace_id)
        return S.SessionCreated(sid=s["sid"], version=s["version"])

    @app.get("/sessions/{sid}", response_model=S.Session, responses={404: ERR},
             tags=["sessions"])
    def get_session(sid: str) -> S.Session:
        return S.Session(**sessions.get(sid))

    @app.put("/sessions/{sid}/ui-state", response_model=S.Session, tags=["sessions"],
             responses={404: ERR, 409: {"model": S.VersionConflict}, 413: ERR, 422: ERR})
    def put_ui_state(sid: str, body: S.UiStatePut) -> S.Session:
        return S.Session(**sessions.put_ui_state(sid, body.ui_state, body.version))

    @app.delete("/sessions/{sid}", status_code=204, responses={404: ERR}, tags=["sessions"])
    def delete_session(sid: str) -> Response:
        sessions.delete(sid)
        return Response(status_code=204)

    # --- live: turns -----------------------------------------------------------------------
    @app.post("/turns", response_model=S.TurnCreated, status_code=202, responses={404: ERR},
              tags=["turns"])
    def create_turn(body: S.TurnCreate) -> S.TurnCreated:
        return S.TurnCreated(turn_id=turns.create(body.sid, body.dataset_id, body.question))

    @app.get("/turns/{turn_id}", response_model=S.Turn, responses={404: ERR}, tags=["turns"])
    def get_turn(turn_id: str) -> S.Turn:
        return S.Turn(**turns.get(turn_id))

    @app.get("/sessions/{sid}/turns", response_model=S.TurnList, responses={404: ERR},
             tags=["turns"])
    def list_turns(sid: str) -> S.TurnList:
        return S.TurnList(turns=[S.Turn(**t) for t in turns.list(sid)])

    # --- spec'd, not live: 501 in the error shape ------------------------------------------
    def stub(method: str, path: str, model: Any, milestone: str, tag: str,
             body: Any = None, status: int = 200, extra: dict | None = None) -> None:
        def handler(request: Request) -> JSONResponse:
            return JSONResponse({"error": {
                "code": "not_implemented", "milestone": milestone,
                "message": f"{method} {path} is spec'd but not live until {milestone}"}},
                status_code=501)
        openapi_extra = dict(extra or {})
        if body is not None:
            openapi_extra["requestBody"] = {"required": True, "content": {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{body.__name__}"}}}}
            extra_models.append(body)
        app.add_api_route(path, handler, methods=[method], response_model=model,
                          status_code=status, tags=[tag], openapi_extra=openapi_extra or None,
                          responses={501: ERR, 404: ERR}, name=f"{method.lower()}_{path}")

    extra_models: list[type] = []
    # --- live since B2: datasets, cleaning, domains, contract ------------------------------
    R = {404: ERR, 413: ERR, 422: ERR}

    @app.post("/workspaces/{ws}/uploads", response_model=S.Dataset, status_code=201,
              responses=R, tags=["datasets"])
    async def upload(ws: str, file: UploadFile = File(...)) -> S.Dataset:
        return S.Dataset(**ds.upload(ws, file.filename or "upload.csv", await file.read()))

    @app.post("/workspaces/{ws}/uploads/{upload_id}/answers", response_model=S.Dataset,
              status_code=201, responses=R, tags=["datasets"])
    def upload_answers(ws: str, upload_id: str, body: S.UploadAnswers) -> S.Dataset:
        return S.Dataset(**ds.answer_upload(ws, upload_id, body.model_dump(exclude_none=True)))

    @app.get("/datasets/{dataset_id}", response_model=S.Dataset, responses=R, tags=["datasets"])
    def get_dataset(dataset_id: str) -> S.Dataset:
        return S.Dataset(**ds.summary(dataset_id))

    @app.get("/datasets/{dataset_id}/profile", response_model=S.Profile, responses=R,
             tags=["datasets"])
    def profile(dataset_id: str) -> S.Profile:
        return S.Profile(**ds.profile(dataset_id))

    @app.get("/datasets/{dataset_id}/cleaning/proposals", response_model=S.CleaningProposals,
             responses=R, tags=["cleaning"])
    def cleaning_proposals(dataset_id: str) -> S.CleaningProposals:
        return S.CleaningProposals(**ds.cleaning_proposals(dataset_id))

    @app.post("/datasets/{dataset_id}/cleaning/approve", response_model=S.ApprovalResult,
              responses=R, tags=["cleaning"])
    def cleaning_approve(dataset_id: str, body: S.ApproveActions) -> S.ApprovalResult:
        return S.ApprovalResult(**ds.cleaning_approve(dataset_id, body.approve, body.reject))

    @app.get("/datasets/{dataset_id}/domains/detect", response_model=S.DomainDetection,
             responses=R, tags=["domains"])
    def detect(dataset_id: str) -> S.DomainDetection:
        return S.DomainDetection(**ds.detect(dataset_id))

    @app.post("/datasets/{dataset_id}/domains/confirm", response_model=S.Confirmed,
              responses=R, tags=["domains"])
    def confirm_domains(dataset_id: str, body: S.DomainConfirm) -> S.Confirmed:
        r = ds.confirm_domains(dataset_id, body.domains)
        return S.Confirmed(ok=r["ok"], version=r["version"])

    @app.get("/datasets/{dataset_id}/contract/proposal", response_model=S.ContractProposal,
             responses=R, tags=["contract"])
    def contract_proposal(dataset_id: str) -> S.ContractProposal:
        return S.ContractProposal(**ds.contract_proposal(dataset_id))

    @app.post("/datasets/{dataset_id}/contract/confirm", response_model=S.Confirmed,
              responses=R, tags=["contract"])
    def contract_confirm(dataset_id: str, body: S.ContractConfirm) -> S.Confirmed:
        return S.Confirmed(**ds.contract_confirm(dataset_id, body.contract, body.fork_choices))

    @app.get("/datasets/{dataset_id}/tools", response_model=S.ToolList, responses=R,
             tags=["tools"])
    def tools(dataset_id: str) -> S.ToolList:
        return S.ToolList(**ds.tools(dataset_id))

    @app.post("/datasets/{dataset_id}/forks", response_model=S.Confirmed, responses=R,
              tags=["contract"])
    def answer_forks(dataset_id: str, body: S.ForkAnswers) -> S.Confirmed:
        r = ds.answer_forks(dataset_id, body.fork_choices)
        return S.Confirmed(ok=r["ok"], version=r["version"])

    @app.get("/datasets/{dataset_id}/metrics/templates", response_model=S.MetricTemplates,
             responses=R, tags=["metrics"])
    def metric_templates(dataset_id: str) -> S.MetricTemplates:
        return S.MetricTemplates(**ds.metric_templates(dataset_id))

    @app.post("/datasets/{dataset_id}/metrics/approve", response_model=S.Confirmed,
              responses={**R, 409: ERR}, tags=["metrics"])
    def approve_metric(dataset_id: str, body: S.MetricApprove) -> S.Confirmed:
        return S.Confirmed(**ds.approve_metric(dataset_id, body.template_id, body.bindings,
                                               body.fork_choices))

    @app.get("/datasets/{dataset_id}/validity-rules", response_model=S.ValidityRules,
             responses=R, tags=["rules"])
    def validity_rules(dataset_id: str) -> S.ValidityRules:
        return S.ValidityRules(**ds.validity_rules(dataset_id))

    @app.post("/datasets/{dataset_id}/validity-rules/approve", response_model=S.Confirmed,
              responses=R, tags=["rules"])
    def approve_rules(dataset_id: str, body: S.RuleApprove) -> S.Confirmed:
        r = ds.approve_rules(dataset_id, body.approve, body.reject)
        return S.Confirmed(ok=r["ok"], version=r["version"])

    @app.post("/tools/{tool_id}/run", response_model=S.ToolResult,
              responses={**R, 409: ERR}, tags=["tools"])
    def run_tool(tool_id: str, body: S.ToolRunRequest) -> S.ToolResult:
        return S.ToolResult(**ds.run_tool(tool_id, body.dataset_id, body.params))

    @app.get("/datasets/{dataset_id}/contract", response_model=S.ContractInForce,
             responses={**R, 409: ERR}, tags=["contract"])
    def contract_in_force(dataset_id: str) -> S.ContractInForce:
        return S.ContractInForce(**ds.contract_in_force(dataset_id))

    @app.get("/datasets/{dataset_id}/analyses", response_model=S.AnalysisList,
             responses={**R, 409: ERR}, tags=["tools"])
    def analyses(dataset_id: str) -> S.AnalysisList:
        return S.AnalysisList(**ds.analyses(dataset_id))

    @app.post("/datasets/{dataset_id}/reports", response_model=S.Report,
              responses={**R, 409: ERR}, tags=["tools"])
    def report(dataset_id: str, body: S.ReportRequest) -> S.Report:
        return S.Report(**ds.report(dataset_id, body.playbook, body.slots))

    @app.get("/datasets/{dataset_id}/results", response_model=S.ResultList, responses=R,
             tags=["results"])
    def list_results(dataset_id: str) -> S.ResultList:
        ds._get(dataset_id)
        return S.ResultList(dataset_id=dataset_id,
                            results=ds.results.list(dataset_id, ds.current_state))

    @app.get("/results/{result_id}", response_model=S.StoredResult, responses=R,
             tags=["results"])
    def get_result(result_id: str) -> S.StoredResult:
        return S.StoredResult(**ds.results.get(result_id, ds.current_state))

    @app.get("/results/{result_id}/inspect", response_model=S.ResultInspection, responses=R,
             tags=["results"])
    def inspect_result(result_id: str, step: str | None = None, sort_by: str | None = None,
                       descending: bool = True, limit: int = 20, offset: int = 0,
                       group: str | None = None) -> S.ResultInspection:
        return S.ResultInspection(**ds.results.inspect(
            result_id, step=step, sort_by=sort_by, descending=descending, limit=limit,
            offset=offset, group=group))

    @app.post("/datasets/{dataset_id}/keyword-groups/run", response_model=S.KeywordGroups,
              responses=R, tags=["keywords"])
    def keyword_run(dataset_id: str, body: S.KeywordRun | None = None) -> S.KeywordGroups:
        body = body or S.KeywordRun()
        return S.KeywordGroups(**ds.keywords.run(dataset_id, body.column, body.backend))

    @app.get("/datasets/{dataset_id}/keyword-groups", response_model=S.KeywordGroups,
             responses=R, tags=["keywords"])
    def keyword_groups(dataset_id: str) -> S.KeywordGroups:
        return S.KeywordGroups(**ds.keywords.list(dataset_id))

    @app.post("/datasets/{dataset_id}/keyword-groups/actions", response_model=S.KeywordGroups,
              responses=R, tags=["keywords"])
    def keyword_action(dataset_id: str, body: S.KeywordGroupAction) -> S.KeywordGroups:
        return S.KeywordGroups(**ds.keywords.act(dataset_id, body.model_dump()))

    @app.get("/packs", response_model=S.PackList, tags=["packs"])
    def packs() -> S.PackList:
        return S.PackList(**D.packs_list())

    @app.get("/packs/{pack_id}", response_model=S.PackDetail, responses=R, tags=["packs"])
    def pack(pack_id: str) -> S.PackDetail:
        return S.PackDetail(**D.pack_detail(pack_id))


    base_openapi = app.openapi

    def openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = base_openapi()
        comps = schema.setdefault("components", {}).setdefault("schemas", {})
        for m in extra_models:
            for name, sub in m.model_json_schema(
                    ref_template="#/components/schemas/{model}").get("$defs", {}).items():
                comps.setdefault(name, sub)
            js = m.model_json_schema(ref_template="#/components/schemas/{model}")
            js.pop("$defs", None)
            comps.setdefault(m.__name__, js)
        schema["components"]["schemas"] = dict(sorted(comps.items()))
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app


# Serve: uv run uvicorn --factory backend.api.app:create_app
