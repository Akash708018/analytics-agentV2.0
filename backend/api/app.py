"""FastAPI routes. Thin: parse, call a service, shape the reply. No logic here."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from backend.api import schemas as S
from backend.services.sessions import Runner, ServiceError, SessionService, TurnService
from backend.sessions.store import Store

REPO = Path(__file__).resolve().parents[2]
ERR = {"model": S.ErrorBody}


def create_app(state_dir: Path | str | None = None, runner: Runner | None = None,
               now=None) -> FastAPI:
    state = Path(state_dir or os.environ.get("AA_STATE_DIR", REPO / "state"))
    store = Store(state / "sessions.db")
    sessions = SessionService(store, now=now)
    turns = TurnService(store, sessions, runner=runner)

    app = FastAPI(title="analytics-agent v2", version=S.API_VERSION,
                  description="Contract-first API. Figures carry provenance; chart series are "
                              "computed by the backend. 501 = spec'd, not live yet.")
    app.state.sessions, app.state.turns = sessions, turns

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
    upload = {"requestBody": {"required": True, "content": {"multipart/form-data": {
        "schema": {"type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary",
                     "description": "CSV or Excel"}}}}}}}
    stub("POST", "/workspaces/{ws}/uploads", S.Dataset, "B2", "datasets", status=201,
         extra=upload)
    stub("GET", "/datasets/{dataset_id}/profile", S.Profile, "B2", "datasets")
    stub("GET", "/datasets/{dataset_id}/cleaning/proposals", S.CleaningProposals, "B2",
         "cleaning")
    stub("POST", "/datasets/{dataset_id}/cleaning/approve", S.ApprovalResult, "B2",
         "cleaning", body=S.ApproveActions)
    stub("GET", "/datasets/{dataset_id}/domains/detect", S.DomainDetection, "B2", "domains")
    stub("POST", "/datasets/{dataset_id}/domains/confirm", S.Confirmed, "B2", "domains",
         body=S.DomainConfirm)
    stub("GET", "/datasets/{dataset_id}/contract/proposal", S.ContractProposal, "B2",
         "contract")
    stub("POST", "/datasets/{dataset_id}/contract/confirm", S.Confirmed, "B2", "contract",
         body=S.ContractConfirm)
    stub("GET", "/datasets/{dataset_id}/metrics/templates", S.MetricTemplates, "B3", "metrics")
    stub("POST", "/datasets/{dataset_id}/metrics/approve", S.Confirmed, "B3", "metrics",
         body=S.MetricApprove)
    stub("GET", "/datasets/{dataset_id}/validity-rules", S.ValidityRules, "B3", "rules")
    stub("POST", "/datasets/{dataset_id}/validity-rules/approve", S.Confirmed, "B3", "rules",
         body=S.RuleApprove)
    stub("GET", "/datasets/{dataset_id}/tools", S.ToolList, "B2", "tools")
    stub("POST", "/tools/{tool_id}/run", S.ToolResult, "B3", "tools", body=S.ToolRunRequest)
    stub("GET", "/packs", S.PackList, "B2", "packs")
    stub("GET", "/packs/{pack_id}", S.PackDetail, "B2", "packs")
    stub("GET", "/datasets/{dataset_id}/keyword-groups", S.KeywordGroups, "B7", "keywords")
    stub("POST", "/datasets/{dataset_id}/keyword-groups/actions", S.KeywordGroups, "B7",
         "keywords", body=S.KeywordGroupAction)

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
