## Status (date, branch, last commit)

2026-09-26 · branch `claude/analytics-agent-v2-backend-6gcu1p` (D-B0-1: protocol name
`backend/B0-seed` pending the user's OK) · last commit: see the B0 PR.
Milestone **B0 — Seed and baseline: done.** v1 engine @ `0ba324b` seeded into `backend/engine`
(package `backend.engine`), v1 tests/benches/eval into `backend/{tests,scripts,eval}`.

## API: spec version — live | stubbed (501) | changed since last handoff

None yet. `docs/api/openapi.yaml` v0.1 is B1's first deliverable (contract-first, with request
and response examples for every endpoint so Codex can mock).

## Tools: active tool ids per domain; tool-schema tokens (core vs core+marketing)

None yet (registry is B2). The seeded engine exposes v1's MCP tools unchanged
(`backend/engine/server.py`) and 27 analyses.

## For Codex: what to build against now; breaking changes; mock notes

Nothing to build against yet — wait for the B1 PR (openapi v0.1). Layout notes for reading:
backend code is under `backend/`; the v1 suite runs from `backend/` (`cd backend && uv run pytest`).

## Open api-request issues + answers

None (0 issues in the repo, checked 2026-09-26).

## Test tail (pasted)

    cd backend && uv run pytest -q -rs
    SKIPPED [1] tests/test_agent.py:580: v2 B0 (D-B0-2): the v1 Streamlit app ui/app.py is not seeded; v2 screens belong to the frontend
    2212 passed, 1 skipped in 195.82s (0:03:15)
    phase8 55/0/3 · phase9 0/0/1 · phase10 0/0/1 (no Postgres olist) · phase11 26/0/0 · phase12 36/0/0
    eval: SCORE: 76/76 (100%), 40 gold questions

## Next milestone

B1 — API contract v0.1 + sessions (SQLite session/turn store; session and turn endpoints live,
the rest 501 in the spec'd shape). Unblocks Codex F1.
