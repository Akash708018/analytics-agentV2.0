"""HTTP boundary checks; fixtures contain synthetic data and need no backend."""

from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
import json

import httpx
import pytest

from frontend.api_client import APIClient, APIError, VersionConflict


BASE_URL = "https://analytics.test"


@pytest.mark.parametrize(
    "configured_url",
    [
        None,
        "",
        " ",
        "analytics.test",
        "/relative",
        "ftp://analytics.test",
        "http://",
        "https://user:password@analytics.test",
        "https://analytics.test?workspace=private",
        "https://analytics.test#session",
    ],
)
def test_invalid_environment_configuration_is_a_clear_api_error(
    monkeypatch, configured_url
):
    if configured_url is None:
        monkeypatch.delenv("ANALYTICS_API_URL", raising=False)
    else:
        monkeypatch.setenv("ANALYTICS_API_URL", configured_url)

    with pytest.raises(APIError) as raised:
        APIClient()

    assert raised.value.code == "configuration_error"
    assert raised.value.message
    assert raised.value.status_code is None


def test_environment_base_url_preserves_an_api_prefix(monkeypatch):
    monkeypatch.setenv("ANALYTICS_API_URL", "https://analytics.test/api/v1/")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with APIClient(transport=httpx.MockTransport(respond)) as client:
        assert client.health() == {"ok": True}

    assert len(requests) == 1
    assert str(requests[0].url) == "https://analytics.test/api/v1/health"


def test_client_sets_bounded_timeouts_disables_environment_and_closes(monkeypatch):
    real_client = httpx.Client
    constructed = []
    options = []
    requests = []

    def construct(*args, **kwargs):
        options.append(kwargs)
        instance = real_client(*args, **kwargs)
        constructed.append(instance)
        return instance

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "Client", construct)
    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)) as client:
        client.health()
        assert not constructed[0].is_closed

    assert constructed[0].is_closed
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 5.0,
    }


def test_explicit_timeout_reaches_the_http_request():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    timeout = httpx.Timeout(connect=1, read=2, write=3, pool=4)
    with APIClient(
        base_url=BASE_URL, transport=httpx.MockTransport(respond), timeout=timeout
    ) as client:
        client.health()

    assert requests[0].extensions["timeout"] == {
        "connect": 1,
        "read": 2,
        "write": 3,
        "pool": 4,
    }


def test_response_figures_and_chart_points_are_returned_unchanged():
    payload = {
        "dataset_id": "ds_synthetic",
        "figures": [
            {"value": 12.345678901, "display": "12.345678901", "provenance": "provisional"}
        ],
        "series": [
            {"name": "Synthetic", "points": [{"x": "C", "y": None}, {"x": "A", "y": 0}]}
        ],
        "validity_filters_applied": [],
        "pack_rules_applied": ["rule_synthetic"],
        "figure_check": {"passed": False},
    }
    with APIClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as client:
        assert client.run_tool("synthetic.tool", "ds_synthetic") == payload


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"text"', b"1", b"null"])
def test_success_requires_a_json_object(body):
    with APIClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    ) as client:
        with pytest.raises(APIError) as raised:
            client.health()

    assert raised.value.code == "invalid_response"
    assert raised.value.status_code == 200


def test_delete_accepts_the_contracts_empty_204_response():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204)

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)) as client:
        assert client.delete_session("synthetic-session") is None

    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/sessions/synthetic-session"


@pytest.mark.parametrize("status_code,code", [(404, "not_found"), (501, "not_implemented")])
def test_api_error_preserves_code_message_and_milestone(status_code, code):
    payload = {
        "error": {
            "code": code,
            "message": "Synthetic endpoint is unavailable",
            "milestone": "B2",
        }
    }
    with APIClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, json=payload)
        ),
    ) as client:
        with pytest.raises(APIError) as raised:
            client.get_profile("ds_synthetic")

    assert raised.value.status_code == status_code
    assert raised.value.code == code
    assert raised.value.message == payload["error"]["message"]
    assert raised.value.payload == payload


def test_validation_error_list_is_preserved_without_crashing():
    payload = {
        "detail": [
            {"loc": ["body", "question"], "msg": "Field required", "type": "missing"}
        ]
    }
    with APIClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(422, json=payload)),
    ) as client:
        with pytest.raises(APIError) as raised:
            client.create_turn("synthetic-session", "ds_synthetic", "Synthetic question")

    assert raised.value.status_code == 422
    assert raised.value.message
    assert raised.value.payload == payload


@pytest.mark.parametrize("body", [b"upstream unavailable", b"[]", b"null", b'{"error":[]}'])
def test_malformed_http_error_is_still_an_api_error(body):
    with APIClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(503, content=body)),
    ) as client:
        with pytest.raises(APIError) as raised:
            client.health()

    assert raised.value.status_code == 503
    assert raised.value.message


def test_conflict_exposes_current_state_and_never_retries_the_write():
    current = {
        "sid": "synthetic-session",
        "workspace_id": "ws_synthetic",
        "version": 8,
        "ui_state": {"screen": "contract", "panel_open": True},
        "created_at": "2026-09-26T10:00:00Z",
        "updated_at": "2026-09-26T10:05:00Z",
        "expires_at": "2026-10-26T10:05:00Z",
    }
    payload = {
        "error": {"code": "version_conflict", "message": "version 7 is stale"},
        "current": current,
    }
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(409, json=payload)

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(VersionConflict) as raised:
            client.put_ui_state("synthetic-session", {"screen": "ask"}, version=7)

    assert isinstance(raised.value, APIError)
    assert raised.value.current == current
    assert raised.value.payload == payload
    assert raised.value.status_code == 409
    assert raised.value.code == "version_conflict"
    assert raised.value.message == "version 7 is stale"
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {"ui_state": {"screen": "ask"}, "version": 7}


@pytest.mark.parametrize("code", ["needs_domain", "needs_data"])
@pytest.mark.parametrize("method", ["run_tool", "approve_metric"])
def test_prerequisite_409_keeps_its_code_instead_of_becoming_a_session_conflict(code, method):
    payload = {"error": {"code": code, "message": "Confirm the required data first"}}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(409, json=payload)

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(VersionConflict) as raised:
            if method == "run_tool":
                client.run_tool("synthetic.tool", "ds_synthetic")
            else:
                client.approve_metric("ds_synthetic", "roas", {})

    assert raised.value.code == code
    assert raised.value.current is None
    assert raised.value.payload == payload
    assert len(requests) == 1


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b"{}", b'{"current":null}', b'{"current":[]}', b'{"current":"bad"}'],
)
def test_every_409_is_a_conflict_even_when_current_state_is_missing_or_malformed(body):
    with APIClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(409, content=body)),
    ) as client:
        with pytest.raises(VersionConflict) as raised:
            client.get_session("synthetic-session")

    assert raised.value.status_code == 409
    assert raised.value.current is None


@pytest.mark.parametrize(
    "error_type,expected_code",
    [
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ReadTimeout, "timeout"),
        (httpx.WriteTimeout, "timeout"),
        (httpx.PoolTimeout, "timeout"),
        (httpx.ConnectError, "transport_error"),
        (httpx.ReadError, "transport_error"),
    ],
)
def test_transport_failures_are_structured_and_never_repeat_a_submission(error_type, expected_code):
    requests = []

    def fail(request):
        requests.append(request)
        raise error_type("Synthetic connection failure", request=request)

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(APIError) as raised:
            client.create_turn("synthetic-session", "ds_synthetic", "Synthetic question")

    assert raised.value.code == expected_code
    assert raised.value.status_code is None
    assert len(requests) == 1


def test_redirect_does_not_forward_a_request_to_another_origin():
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://different.test/turns"})

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(APIError) as raised:
            client.create_turn("synthetic-session", "ds_synthetic", "Synthetic question")

    assert raised.value.status_code == 307
    assert len(requests) == 1
    assert requests[0].url.host == "analytics.test"


@pytest.mark.parametrize("session_id", ["", ".", ".."])
def test_invalid_path_segments_fail_before_sending_a_request(session_id):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(APIError):
            client.get_session(session_id)

    assert not requests


def test_identifiers_are_encoded_as_one_path_segment():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)) as client:
        client.get_pack("synthetic/a b?query#fragment%2F")

    assert requests[0].url.raw_path == b"/packs/synthetic%2Fa%20b%3Fquery%23fragment%252F"
    assert requests[0].url.query == b""
    assert requests[0].url.fragment == ""


@pytest.mark.parametrize(
    "method,args,kwargs,path,body",
    [
        (
            "create_session",
            ("ws_synthetic",),
            {},
            "/sessions",
            {"workspace_id": "ws_synthetic"},
        ),
        (
            "create_turn",
            ("synthetic-session", "ds_synthetic", "Why is synthetic ROAS 1.23456?"),
            {},
            "/turns",
            {
                "sid": "synthetic-session",
                "dataset_id": "ds_synthetic",
                "question": "Why is synthetic ROAS 1.23456?",
            },
        ),
        (
            "approve_cleaning",
            ("ds_synthetic",),
            {"approve": ["a2"], "reject": ["a1"]},
            "/datasets/ds_synthetic/cleaning/approve",
            {"approve": ["a2"], "reject": ["a1"]},
        ),
        (
            "confirm_domains",
            ("ds_synthetic", []),
            {},
            "/datasets/ds_synthetic/domains/confirm",
            {"domains": []},
        ),
        (
            "confirm_contract",
            ("ds_synthetic", {"grain": "campaign/day"}, {"basis": "net"}),
            {},
            "/datasets/ds_synthetic/contract/confirm",
            {"contract": {"grain": "campaign/day"}, "fork_choices": {"basis": "net"}},
        ),
        (
            "answer_forks",
            ("ds_synthetic", {"conversion_source": "backend_orders"}),
            {},
            "/datasets/ds_synthetic/forks",
            {"fork_choices": {"conversion_source": "backend_orders"}},
        ),
        (
            "approve_metric",
            ("ds_synthetic", "marketing.roas", {"revenue": "net", "spend": "cost"}),
            {"fork_choices": {"basis": "net"}},
            "/datasets/ds_synthetic/metrics/approve",
            {
                "template_id": "marketing.roas",
                "bindings": {"revenue": "net", "spend": "cost"},
                "fork_choices": {"basis": "net"},
            },
        ),
        (
            "approve_validity_rules",
            ("ds_synthetic",),
            {"approve": [], "reject": ["r1"]},
            "/datasets/ds_synthetic/validity-rules/approve",
            {"approve": [], "reject": ["r1"]},
        ),
        (
            "run_tool",
            ("synthetic.tool", "ds_synthetic", {"by": "channel", "limit": 3}),
            {},
            "/tools/synthetic.tool/run",
            {"dataset_id": "ds_synthetic", "params": {"by": "channel", "limit": 3}},
        ),
        (
            "apply_keyword_group_action",
            ("ds_synthetic", "move_keyword", ["g1"]),
            {"label": "Synthetic", "keyword": "synthetic keyword", "target_group_id": "g4"},
            "/datasets/ds_synthetic/keyword-groups/actions",
            {
                "action": "move_keyword",
                "group_ids": ["g1"],
                "label": "Synthetic",
                "keyword": "synthetic keyword",
                "target_group_id": "g4",
            },
        ),
    ],
)
def test_mutations_preserve_the_callers_choices_and_request_body(method, args, kwargs, path, body):
    requests = []
    original_args, original_kwargs = deepcopy(args), deepcopy(kwargs)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)) as client:
        assert getattr(client, method)(*args, **kwargs) == {"ok": True}

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == path
    assert json.loads(requests[0].content) == body
    assert args == original_args
    assert kwargs == original_kwargs


def test_upload_preserves_the_filename_content_type_and_file_bytes():
    requests = []
    content = b"campaign,spend\r\nsynthetic,1.23456\r\n"

    def respond(request):
        requests.append(request)
        return httpx.Response(201, json={"dataset_id": "ds_synthetic"})

    with APIClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)) as client:
        assert client.upload_dataset("ws_synthetic", "synthetic.csv", content, "text/csv") == {
            "dataset_id": "ds_synthetic"
        }

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/workspaces/ws_synthetic/uploads"
    multipart = BytesParser(policy=default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
    )
    parts = list(multipart.iter_parts())
    assert len(parts) == 1
    assert parts[0].get_param("name", header="content-disposition") == "file"
    assert parts[0].get_filename() == "synthetic.csv"
    assert parts[0].get_content_type() == "text/csv"
    assert parts[0].get_payload(decode=True) == content


def test_request_and_response_contents_are_not_logged(caplog):
    caplog.set_level("DEBUG")
    marker = "synthetic-private-body-marker"
    payload = {"error": {"code": "rejected", "message": marker}}
    with APIClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(lambda request: httpx.Response(422, json=payload)),
    ) as client:
        with pytest.raises(APIError):
            client.create_turn("synthetic-session", "ds_synthetic", marker)

    assert marker not in caplog.text
