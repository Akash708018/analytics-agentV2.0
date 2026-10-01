"""Thin HTTP boundary for docs/api/openapi.yaml v0.6.0.

Responses are returned unchanged. No retries, figure calculations, or backend
imports: callers decide when to poll and people decide how to resolve conflicts.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any, BinaryIO
from urllib.parse import quote

import httpx

JsonObject = dict[str, Any]


class APIError(Exception):
    """One public error boundary for configuration, transport, and API failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.payload = payload


class VersionConflict(APIError):
    """HTTP 409, preserving its code and any current state without retrying.

    API 0.3 also uses 409 for tool prerequisites. Only version_conflict with a
    current session belongs in the session-conflict UI; inspect code first.
    """

    def __init__(self, payload: Any) -> None:
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        super().__init__(
            message if isinstance(message, str) else "This session changed in another tab.",
            code=code if isinstance(code, str) else "version_conflict",
            status_code=409,
            payload=payload,
        )
        current = payload.get("current") if isinstance(payload, dict) else None
        self.current: JsonObject | None = current if isinstance(current, dict) else None


def _segment(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise APIError("A nonempty resource ID is required.", code="invalid_argument")
    return quote(value, safe="")


class APIClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        configured = base_url if base_url is not None else os.environ.get("ANALYTICS_API_URL")
        try:
            url = httpx.URL(configured or "")
        except httpx.InvalidURL:
            raise APIError("ANALYTICS_API_URL must be an HTTP(S) URL.", code="configuration_error") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise APIError(
                "Set ANALYTICS_API_URL to an HTTP(S) service URL without credentials, query, or fragment.",
                code="configuration_error",
            )
        self._http = httpx.Client(
            base_url=str(url).rstrip("/") + "/",
            timeout=timeout if timeout is not None else httpx.Timeout(30.0, connect=5.0, pool=5.0),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> APIClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> JsonObject | None:
        try:
            response = self._http.request(method, path.lstrip("/"), **kwargs)
        except httpx.TimeoutException:
            raise APIError(
                "The analytics service timed out. Check its status before retrying an action.",
                code="timeout",
            ) from None
        except httpx.RequestError:
            raise APIError(
                "Could not reach the analytics service. Check the service address and connection.",
                code="transport_error",
            ) from None

        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code == 409:
            raise VersionConflict(payload)
        if not response.is_success:
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise APIError(
                message if isinstance(message, str) else f"The analytics service returned HTTP {response.status_code}.",
                code=code if isinstance(code, str) else "http_error",
                status_code=response.status_code,
                payload=payload,
            )
        if not isinstance(payload, dict):
            raise APIError(
                "The analytics service returned an invalid JSON object.",
                code="invalid_response",
                status_code=response.status_code,
            )
        return payload

    def health(self) -> JsonObject:
        return self._request("GET", "health")

    def version(self) -> JsonObject:
        return self._request("GET", "version")

    def create_session(self, workspace_id: str | None = None) -> JsonObject:
        body = {} if workspace_id is None else {"workspace_id": workspace_id}
        return self._request("POST", "sessions", json=body)

    def get_session(self, sid: str) -> JsonObject:
        return self._request("GET", f"sessions/{_segment(sid)}")

    def delete_session(self, sid: str) -> None:
        self._request("DELETE", f"sessions/{_segment(sid)}")

    def put_ui_state(self, sid: str, ui_state: JsonObject, *, version: int) -> JsonObject:
        return self._request("PUT", f"sessions/{_segment(sid)}/ui-state", json={"ui_state": ui_state, "version": version})

    def create_turn(self, sid: str, dataset_id: str, question: str) -> JsonObject:
        return self._request("POST", "turns", json={"sid": sid, "dataset_id": dataset_id, "question": question})

    def get_turn(self, turn_id: str) -> JsonObject:
        return self._request("GET", f"turns/{_segment(turn_id)}")

    def list_turns(self, sid: str) -> JsonObject:
        return self._request("GET", f"sessions/{_segment(sid)}/turns")

    def upload_dataset(
        self, workspace_id: str, filename: str, content: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> JsonObject:
        return self._request("POST", f"workspaces/{_segment(workspace_id)}/uploads", files={"file": (filename, content, content_type)})

    def get_profile(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}/profile")

    def get_dataset(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}")

    def list_cleaning_proposals(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}/cleaning/proposals")

    def approve_cleaning(self, dataset_id: str, *, approve: Sequence[str], reject: Sequence[str] = ()) -> JsonObject:
        return self._request("POST", f"datasets/{_segment(dataset_id)}/cleaning/approve", json={"approve": list(approve), "reject": list(reject)})

    def detect_domains(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}/domains/detect")

    def confirm_domains(self, dataset_id: str, domains: Sequence[str]) -> JsonObject:
        return self._request("POST", f"datasets/{_segment(dataset_id)}/domains/confirm", json={"domains": list(domains)})

    def get_contract_proposal(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}/contract/proposal")

    def confirm_contract(self, dataset_id: str, contract: JsonObject, fork_choices: Mapping[str, str]) -> JsonObject:
        return self._request("POST", f"datasets/{_segment(dataset_id)}/contract/confirm", json={"contract": contract, "fork_choices": dict(fork_choices)})

    def answer_forks(self, dataset_id: str, fork_choices: Mapping[str, str]) -> JsonObject:
        return self._request("POST", f"datasets/{_segment(dataset_id)}/forks", json={"fork_choices": dict(fork_choices)})

    def list_metric_templates(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}/metrics/templates")

    def approve_metric(
        self, dataset_id: str, template_id: str, bindings: Mapping[str, str],
        fork_choices: Mapping[str, str] | None = None,
    ) -> JsonObject:
        return self._request("POST", f"datasets/{_segment(dataset_id)}/metrics/approve", json={"template_id": template_id, "bindings": dict(bindings), "fork_choices": dict(fork_choices or {})})

    def list_validity_rules(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}/validity-rules")

    def approve_validity_rules(self, dataset_id: str, *, approve: Sequence[str], reject: Sequence[str] = ()) -> JsonObject:
        return self._request("POST", f"datasets/{_segment(dataset_id)}/validity-rules/approve", json={"approve": list(approve), "reject": list(reject)})

    def list_tools(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}/tools")

    def run_tool(self, tool_id: str, dataset_id: str, params: JsonObject | None = None) -> JsonObject:
        return self._request("POST", f"tools/{_segment(tool_id)}/run", json={"dataset_id": dataset_id, "params": params if params is not None else {}})

    def list_packs(self) -> JsonObject:
        return self._request("GET", "packs")

    def get_pack(self, pack_id: str) -> JsonObject:
        return self._request("GET", f"packs/{_segment(pack_id)}")

    def run_keyword_grouping(self, dataset_id: str, column: str | None = None) -> JsonObject:
        body: JsonObject = {} if column is None else {"column": column}
        return self._request("POST", f"datasets/{_segment(dataset_id)}/keyword-groups/run", json=body)

    def list_keyword_groups(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"datasets/{_segment(dataset_id)}/keyword-groups")

    def apply_keyword_group_action(
        self, dataset_id: str, action: str, group_ids: Sequence[str], *,
        label: str | None = None, keyword: str | None = None,
        keywords: Sequence[str] | None = None, target_group_id: str | None = None,
    ) -> JsonObject:
        body: JsonObject = {"action": action, "group_ids": list(group_ids)}
        for name, value in (
            ("label", label), ("keyword", keyword),
            ("keywords", None if keywords is None else list(keywords)),
            ("target_group_id", target_group_id),
        ):
            if value is not None:
                body[name] = value
        return self._request("POST", f"datasets/{_segment(dataset_id)}/keyword-groups/actions", json=body)
