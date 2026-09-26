# analytics-agent v2 — backend agent rules

v2 = v1 engine (DuckDB, contract-gated analyses, figure checker) + domain packs, domain-gated
tools, a deterministic domain-rules filter, and a FastAPI backend. Priority domain: marketing.
v1 reference: github.com/Akash708018/analytics-agent @ `0ba324b` (read-only).

## Ownership
Backend owns `backend/`, `packs/`, `bench/`, `docs/api/`, `docs/steps/B*`. Frontend (Codex) owns
`frontend/`, `docs/steps/F*` — never edit them. Shared, append-only, PR required:
`docs/COORDINATION.md`, `docs/decisions.md`, `README.md`. Protocol: `docs/COORDINATION.md`.

## Run
Python 3.12 via `uv`; never `pip install`. The v1 suite runs FROM `backend/` (cwd-relative
fixture paths; some tests skip, not fail, from elsewhere — always count skips):

    cd backend && uv run pytest -q -rs        # B2: 2273 passed, 1 skipped (D-B0-2)
    uv run uvicorn --factory backend.api.app:create_app   # API; spec: docs/api/openapi.yaml
    uv run python -m backend.api.export_openapi           # after any schema change
    cd backend && uv run python eval/run_eval.py     # SCORE: 76/76
    cd backend && uv run python tests/test_phase11.py   # and phase8-12 acceptance scripts

## Non-negotiables
- State the expected output BEFORE running; no claim without pasted output.
- Each milestone: plan doc `docs/steps/B<n>.md` with real output (pytest tail, exact counts).
- Read a file before editing it; guarded edits (assert the match count).
- No numpy/pandas in the engine; computation in DuckDB SQL. Only exception: `backend/text/`
  (B7), logged in `docs/decisions.md` first.
- The LLM never computes and never writes SQL. Packs never contain SQL strings.
- A person approves every state change (cleaning, contract, forks, metrics, validity filters,
  domain, keyword labels). Packs may SUGGEST with a reason; never silently default.
- Disagree with a decision → log it in `docs/decisions.md` (ID, evidence, proposal), ask first.
  Log self-corrections as `C<n>` with reasoning.
- New deps allowed: fastapi, uvicorn, httpx (tests), pyyaml; numpy/scikit-learn in
  `backend/text/` only, after asking.
- Budget: read only the files a milestone names; targeted tests while working, the full
  suite once at the end.

## Milestone end
Commit → push → open/update PR → update `docs/handoff/BACKEND.md` → short summary → STOP.
