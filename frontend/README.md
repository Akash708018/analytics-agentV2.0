# Frontend

Run commands from the repository root. Use Python 3.12 or newer.

```sh
uv venv frontend/.venv --python 3.12
uv pip install --python frontend/.venv/bin/python -r frontend/requirements.txt
```

## Running the app (F2)

The app needs the backend. In one terminal (no model configured: questions end
`agent_not_wired`; with provider keys set they are answered):

```sh
uv run uvicorn --factory backend.api.app:create_app --port 8000
```

In another:

```sh
ANALYTICS_API_URL=http://127.0.0.1:8000 frontend/.venv/bin/python -m streamlit run frontend/app.py
```

Open the printed URL and click **Start a new session**. The address then holds
`?sid=<uuid>`: that link is the session. Pages: **Session** (name the analysis),
**Data** (upload a CSV/xlsx, choose the dataset, see its profile; when the reader can't settle
a file's layout, answer its questions from a blank form beside the file's first rows), **Clean**
(tick the engine's proposals, see what each loses, with examples and its SQL, then apply),
**Domain** (confirm what kind of data it is), **Contract** (the contract in force, then what
each column is, how measures add up, the questions only you can answer), **Metrics**
(approve metrics such as CTR, CPA, ROAS, and validity rules), **Keyword groups** (the engine
proposes groups of search terms; you approve, withdraw an approval, merge, rename, split or move
them; only approved groups reach the tools), **Tools** (run a marketing or logistics tool;
every figure with its source, charts as the backend computed them), **Explore** (run any of
the engine's 27 core analyses with the fields you choose, or build a report from a playbook of
a confirmed domain, no model call), **Results** (every stored
result, whether it is out of date and why, and all its rows, paged), and **Ask**
(questions and answers; a running question survives refresh and is never sent twice; each
answer shows how it was reached and the tool results behind it). Every result downloads its
figures as CSV, exactly as shown. **Session** can also start a new, empty session; the old one
stays at its link.
Nothing is ticked or chosen for you: suggestions come with reasons and their own buttons.

The look ("sunset foundry": warm paper, graphite, ember-to-dusk accents, quiet motion and
scroll effects) is the theme in `frontend/.streamlit/config.toml` plus `components/style.py`.
Run from the repository root as above and Streamlit picks the theme up. Motion stops when the
system asks for reduced motion. See [F9](../docs/steps/F9.md).

Everything the person sets is saved to the backend session (`ui_state`, an explicit
allowlist in `state.py`) and comes back after refresh or on another device. Two tabs
editing the same thing get an explicit **Load latest** / **Keep my changes** choice.
Text is saved on Enter or when the field loses focus. See
[F2 evidence](../docs/steps/F2.md).

Layout: `app.py` (router), `state.py` (hydrate/save/conflicts, schema-2 drafts),
`prep.py` (form ↔ API mappings, params, figure display), `keywords.py` (keyword-group
listing and what each action does to approval), `datasets.py` (cached server reads),
`turns.py`, `connection.py`, `components/` (`shell`, `results`, `bindings`), `views/` (pages; not
`pages/`, see D-F2-2).
Nothing here imports the backend; `api_client.py` is the only HTTP boundary.

## End-to-end browser suite (F11)

`frontend/e2e/` drives the real app in headless Chromium against the real backend. The backend
runs with a scripted model (`e2e/scripted_backend.py`); only the model's words are fixed. Each
test makes its own session through the API and checks every value the page shows against the
API's own reply.

```sh
npm ci --prefix frontend                       # once; elsewhere also: npx --prefix frontend playwright install chromium
npm run e2e --prefix frontend                  # 17 tests, about 2 minutes, 3 workers
npm run e2e --prefix frontend -- f10.spec.js -g "Explore"     # one file / one test
```

`e2e/servers.js` starts both servers on free ports, on an empty state directory, and stops them
afterwards. It also removes the workspaces the run made. Settings:
- `E2E_KEEP=1` keeps the logs and workspaces;
- `E2E_APP` and `E2E_API` reuse servers already running;
- `E2E_WORKERS` sets the number of workers;
- `E2E_LARGE_ROWS` sizes the large-file test (default 300,000 rows).

Failures keep a trace and a screenshot in `e2e/test-results/`. See [F11](../docs/steps/F11.md).

## API client and mock (F1)

The client reads `ANALYTICS_API_URL` (required). Its methods map to all 30
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
the API also uses 409 for prerequisites (`needs_domain`/`needs_data` on tool runs,
`contract_required` on tool runs and metric approval). Only
`code == "version_conflict"` belongs in the session-conflict UI. Other error
details (`forks`, `columns`, `missing`, `dates`, `questions`) sit at the top level
of `payload`, next to `error`.
The client does not retry failed actions or resolve conflicts. F2 must ask the
person whether to load the latest state or keep their changes. Do not blindly
resubmit a turn after a timeout: the server may already have accepted it.

Timeouts are 5 seconds for connect/pool and 30 seconds for read/write inactivity;
these are not a total wall-clock deadline. HTTP redirects are not followed.
Proxy environment variables are disabled; `ANALYTICS_API_URL` selects the service.
The base URL may include an API path prefix, but not credentials, query, or fragment.

Node is required for the approved Prism mock; F1 used Node 26.7.0 and 22.22.0.
From the repository root:

```sh
npm ci --prefix frontend --ignore-scripts --no-audit --no-fund
npm run mock --prefix frontend
```

Prism 5.16.0 listens on `127.0.0.1:4010` and reads the canonical YAML unchanged.
In another terminal, set `ANALYTICS_API_URL=http://127.0.0.1:4010` for client use.
Stop the mock with Ctrl-C. The mock returns static examples: it does not persist
session writes, upload data, run turns, or prove any live backend behavior.

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

For mock failures, tests inject Prism's `Prefer: code=409` / `code=422` header
through test-only transports. The product client never sends mock-control headers.
API 0.6.0 declares every path parameter and has no 501 routes, but still shares
one generic error example across HTTP statuses; see
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
[F2 evidence](../docs/steps/F2.md), and [frontend handoff](../docs/handoff/FRONTEND.md).
