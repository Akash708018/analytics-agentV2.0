"""Every HTTP method is exercised against the unmodified canonical spec."""

import json
from dataclasses import dataclass

import httpx
import pytest

from frontend.api_client import APIClient, APIError, VersionConflict


@dataclass(frozen=True)
class Operation:
    client_method: str
    method: str
    path: str
    request_schema: str | None = None


OPERATIONS = [
    Operation("health", "get", "/health"),
    Operation("version", "get", "/version"),
    Operation("create_session", "post", "/sessions", "SessionCreate"),
    Operation("get_session", "get", "/sessions/{sid}"),
    Operation("delete_session", "delete", "/sessions/{sid}"),
    Operation("put_ui_state", "put", "/sessions/{sid}/ui-state", "UiStatePut"),
    Operation("create_turn", "post", "/turns", "TurnCreate"),
    Operation("get_turn", "get", "/turns/{turn_id}"),
    Operation("list_turns", "get", "/sessions/{sid}/turns"),
    Operation("upload_dataset", "post", "/workspaces/{ws}/uploads"),
    Operation("get_dataset", "get", "/datasets/{dataset_id}"),
    Operation("get_profile", "get", "/datasets/{dataset_id}/profile"),
    Operation("list_cleaning_proposals", "get", "/datasets/{dataset_id}/cleaning/proposals"),
    Operation("approve_cleaning", "post", "/datasets/{dataset_id}/cleaning/approve", "ApproveActions"),
    Operation("detect_domains", "get", "/datasets/{dataset_id}/domains/detect"),
    Operation("confirm_domains", "post", "/datasets/{dataset_id}/domains/confirm", "DomainConfirm"),
    Operation("get_contract_proposal", "get", "/datasets/{dataset_id}/contract/proposal"),
    Operation("confirm_contract", "post", "/datasets/{dataset_id}/contract/confirm", "ContractConfirm"),
    Operation("answer_forks", "post", "/datasets/{dataset_id}/forks", "ForkAnswers"),
    Operation("list_metric_templates", "get", "/datasets/{dataset_id}/metrics/templates"),
    Operation("approve_metric", "post", "/datasets/{dataset_id}/metrics/approve", "MetricApprove"),
    Operation("list_validity_rules", "get", "/datasets/{dataset_id}/validity-rules"),
    Operation("approve_validity_rules", "post", "/datasets/{dataset_id}/validity-rules/approve", "RuleApprove"),
    Operation("list_tools", "get", "/datasets/{dataset_id}/tools"),
    Operation("run_tool", "post", "/tools/{tool_id}/run", "ToolRunRequest"),
    Operation("list_packs", "get", "/packs"),
    Operation("get_pack", "get", "/packs/{pack_id}"),
    Operation("run_keyword_grouping", "post", "/datasets/{dataset_id}/keyword-groups/run", "KeywordRun"),
    Operation("list_keyword_groups", "get", "/datasets/{dataset_id}/keyword-groups"),
    Operation("apply_keyword_group_action", "post", "/datasets/{dataset_id}/keyword-groups/actions", "KeywordGroupAction"),
    Operation("list_results", "get", "/datasets/{dataset_id}/results"),
    Operation("get_result", "get", "/results/{result_id}"),
    Operation("inspect_result", "get", "/results/{result_id}/inspect"),
]

IDS = {
    "sid": "0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11",
    "turn_id": "t_7c1d9e0a4b2f4e59",
    "ws": "ws_3f9a1c2b7d10",
    "dataset_id": "ds_ads_2026q3",
    "tool_id": "marketing.channel_efficiency",
    "pack_id": "marketing",
    "result_id": "r_0a1b2c3d4e5f6a7b",
}


def schema_example(contract, name):
    return contract["components"]["schemas"][name]["examples"][0]


def response_example(contract, operation, status):
    response = contract["paths"][operation.path][operation.method]["responses"][str(status)]
    if status == 204:
        return None
    schema = response["content"]["application/json"]["schema"]
    return schema_example(contract, schema["$ref"].split("/")[-1])


def invoke(client, operation, contract):
    args = {("workspace_id" if key == "ws" else key): value
            for key, value in IDS.items() if "{" + key + "}" in operation.path}
    if operation.request_schema:
        args.update(schema_example(contract, operation.request_schema))
    if operation.client_method == "upload_dataset":
        args.update(filename="synthetic.csv", content=b"campaign,cost\nsynthetic,1\n", content_type="text/csv")
    return getattr(client, operation.client_method)(**args)


class RecordingTransport(httpx.BaseTransport):
    """Observe the actual wire requests while still talking to real Prism."""

    def __init__(self, prefer=None):
        self.inner = httpx.HTTPTransport()
        self.prefer = prefer
        self.requests = []
        self.statuses = []

    def handle_request(self, request):
        if self.prefer:
            request.headers["Prefer"] = self.prefer  # Test-only Prism error selection.
        request.read()
        self.requests.append(request)
        response = self.inner.handle_request(request)
        self.statuses.append(response.status_code)
        return response

    def close(self):
        self.inner.close()


def test_all_spec_operations_have_a_client_case(contract):
    expected = {(method, path) for path, item in contract["paths"].items()
                for method in item if method in {"get", "post", "put", "patch", "delete"}}
    assert {(op.method, op.path) for op in OPERATIONS} == expected
    assert contract["info"]["version"] == "0.7.0"


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda op: op.client_method)
def test_client_method_matches_spec_example(prism_url, contract, operation):
    transport = RecordingTransport()
    with APIClient(base_url=prism_url, transport=transport) as client:
        actual = invoke(client, operation, contract)
    status = next(int(code) for code in contract["paths"][operation.path][operation.method]["responses"] if code.startswith("2"))
    assert transport.statuses == [status]
    assert actual == response_example(contract, operation, status)
    request, = transport.requests
    assert request.method == operation.method.upper()
    assert request.url.path == operation.path.format(**IDS)
    if operation.request_schema:
        assert json.loads(request.content) == schema_example(contract, operation.request_schema)
    elif operation.client_method == "upload_dataset":
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert b'name="file"; filename="synthetic.csv"' in request.content
        assert b"campaign,cost\nsynthetic,1\n" in request.content
    else:
        assert request.content == b""


@pytest.mark.parametrize(
    "operation",
    [op for op in OPERATIONS if op.client_method not in {"health", "version", "list_packs"}],
    ids=lambda op: op.client_method,
)
def test_declared_errors_are_never_success_or_retried(prism_url, contract, operation):
    responses = contract["paths"][operation.path][operation.method]["responses"]
    statuses = [int(status) for status in responses if int(status) >= 400]
    assert statuses
    for status in statuses:
        transport = RecordingTransport(f"code={status}")
        with APIClient(base_url=prism_url, transport=transport) as client:
            with pytest.raises(VersionConflict if status == 409 else APIError) as caught:
                invoke(client, operation, contract)
        assert transport.statuses == [status]
        assert caught.value.status_code == status
        schema = responses[str(status)]["content"]["application/json"]["schema"]
        model = contract["components"]["schemas"][schema["$ref"].split("/")[-1]]
        if "examples" in model:
            expected = model["examples"][0]
            assert caught.value.payload == expected
            assert caught.value.code == expected["error"]["code"]
            if status == 409:
                assert caught.value.current == expected.get("current")
        else:
            # HTTPValidationError has no example; Prism synthesizes its schema.
            assert isinstance(caught.value.payload, dict)
