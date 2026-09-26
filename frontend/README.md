# Frontend (F0)

Run commands from the repository root. Use Python 3.12 or newer.

```sh
python3 -m venv frontend/.venv
frontend/.venv/bin/python -m pip install -r frontend/requirements.txt
frontend/.venv/bin/python -m streamlit run frontend/app.py
```

The landing page deliberately collects no user work until backend session saving
is implemented. `api_client.py` and `state.py` reserve the F1 and F2 boundaries;
they do not call guessed endpoints or import the v1 engine.

```sh
frontend/.venv/bin/python -m pytest frontend/tests -q
```

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

See [F0 evidence and inventory](../docs/steps/F0.md) and
[frontend handoff](../docs/handoff/FRONTEND.md). F1 starts only once
`docs/api/openapi.yaml` v0.1 is on `main` and the human starts the milestone.
