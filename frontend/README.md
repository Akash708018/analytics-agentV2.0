# Frontend

Run commands from the repository root. Use Python 3.12 or newer.

```sh
python3 -m venv frontend/.venv
frontend/.venv/bin/python -m pip install -r frontend/requirements.txt
frontend/.venv/bin/python -m streamlit run frontend/app.py
```

The landing page deliberately collects no user work until backend session saving
is connected in F2. `api_client.py` implements the HTTP boundary for OpenAPI
0.3.0; `state.py` remains the reserved F2 boundary. Neither imports the engine.

## API client and mock (F1)

The client reads `ANALYTICS_API_URL` (required). Its methods map to all 29
operations in `docs/api/openapi.yaml`. Use the client as a context manager so
connections close. Responses, figure values, and chart points are returned as
decoded JSON without calculations, sorting, or display formatting.

```python
from frontend.api_client import APIClient, APIError, VersionConflict

with APIClient() as api:
    health = api.health()
```

`APIError` exposes `code`, `message`, `status_code` (None for transport/configuration
errors), and the server's `payload`. `VersionConflict` is an `APIError` with
`current` containing the server session, or None when no valid current state was
returned. All 409s use this exception as required, but retain the server's code:
API 0.3.0 also uses 409 `needs_domain`/`needs_data` for tool prerequisites. Only
`code == "version_conflict"` belongs in the session-conflict UI.
The client does not retry failed actions or resolve conflicts. F2 must ask the
person whether to load the latest state or keep their changes. Do not blindly
resubmit a turn after a timeout: the server may already have accepted it.

Timeouts are 5 seconds for connect/pool and 30 seconds for read/write inactivity;
these are not a total wall-clock deadline. HTTP redirects are not followed.
Proxy environment variables are disabled; `ANALYTICS_API_URL` selects the service.
The base URL may include an API path prefix, but not credentials, query, or fragment.

Node is required for the approved Prism mock; Node 26.7.0 was used for F1.
From the repository root:

```sh
npm ci --prefix frontend --ignore-scripts --no-audit --no-fund
npm run mock --prefix frontend
```

Prism 5.16.0 listens on `127.0.0.1:4010` and reads the canonical YAML unchanged.
In another terminal, set `ANALYTICS_API_URL=http://127.0.0.1:4010` for client use.
Stop the mock with Ctrl-C. The mock returns static examples: it does not persist
session writes, upload data, run turns, or prove any live backend behavior.
Its success examples for 501 backend routes are for frontend contract testing.

Run all frontend tests after installing both Python and Node dependencies:

```sh
frontend/.venv/bin/python -m pytest frontend/tests -q
```

The suite starts its own Prism on a free loopback port and stops that process on
completion or failure. It reads the spec through Prism's bundled YAML parser;
no additional Python YAML dependency or copied contract is used. Missing Prism
is a test failure, not a skip. Unit-only checks, which need no listening port:

```sh
frontend/.venv/bin/python -m pytest frontend/tests/test_api_client.py frontend/tests/test_scaffold.py -q
```

For mock failures, tests inject Prism's `Prefer: code=409` / `code=501` header
through test-only transports. The product client never sends mock-control headers.
API 0.3.0 still lacks path declarations on its two keyword-group routes and
shares generic error examples across HTTP statuses; see
[API request #4](https://github.com/Akash708018/analytics-agentV2.0/issues/4).

## Original state probe (F0)

For the synthetic refresh/history measurement (no real data, no backend):

```sh
frontend/.venv/bin/python -m streamlit run frontend/dev/state_probe.py --server.address 127.0.0.1 --server.port 8510 --server.headless true --browser.gatherUsageStats false
```

Open `http://127.0.0.1:8510/?sid=00000000-0000-4000-8000-0000000000f0`.
The UUID is a fixed synthetic test value, not a backend session. Enter synthetic
text, press Enter, refresh, and use Page A / Page B and browser Back / Forward.
The on-screen snapshot shows the current session instance, fields, and URL query
parameters. The probe intentionally loses values so the failure can be measured.
Do not deploy it or use it with private data. Stop the server with Ctrl-C.

See [F0 evidence and inventory](../docs/steps/F0.md), [F1 evidence](../docs/steps/F1.md),
and [frontend handoff](../docs/handoff/FRONTEND.md). F2 starts only when the human
starts the next milestone; session persistence is not implemented in F1.
