## Status (date, branch, last commit)

2026-09-26 · branch `claude/analytics-agent-v2-backend-6gcu1p` (D-B0-1 resolved: this branch,
one commit series per milestone) · PR: https://github.com/Akash708018/analytics-agentV2.0/pull/1
Done: **B0** (v1 seed, 2212/1 + eval 76/76) and **B1** (API contract v0.1 + sessions/turns).

## API: spec version — live | stubbed (501) | changed since last handoff

`docs/api/openapi.yaml` **0.1.0** (new). Live: `/health`, `/version`, `POST /sessions`,
`GET|DELETE /sessions/{sid}`, `PUT /sessions/{sid}/ui-state` (409 + `current` on stale
version, 413 >256 KB, 422 keys/PII/data rows), `POST /turns` (202), `GET /turns/{id}`,
`GET /sessions/{sid}/turns`. Stubbed 501 (`error.milestone` says when): uploads, profile,
cleaning, domains, contract, tools, packs → B2; metrics, validity rules, `POST /tools/{id}/run`
→ B3; keyword groups → B7.

## Tools: active tool ids per domain; tool-schema tokens (core vs core+marketing)

None yet (registry is B2).

## For Codex: what to build against now; breaking changes; mock notes

**Start F1.** Run the API: `uv run uvicorn --factory backend.api.app:create_app` (state in
`state/`). Mock every 501 endpoint from the `examples` on its schema in `openapi.yaml`.
- Poll `GET /turns/{id}` until `status` ∈ done|failed|interrupted; render `events` in `seq`
  order by `type`. Until B5, turns end `failed` with `error.code = agent_not_wired`.
- Figures carry `provenance` (contract|provisional|derived); results list
  `validity_filters_applied`, `pack_rules_applied`, `figure_check`. Chart `series` are
  computed by the backend — plot `points` as given, never aggregate client-side.
- Keep `version` from the last session read; send it on every ui-state PUT; on 409 adopt
  `current` and retry.

## Open api-request issues + answers

None (checked 2026-09-26).

## Test tail (pasted)

    cd backend && uv run pytest -q -rs
    SKIPPED [1] tests/test_agent.py:580: v2 B0 (D-B0-2): the v1 Streamlit app ui/app.py is not seeded; v2 screens belong to the frontend
    2256 passed, 1 skipped, 1 warning in 218.70s (0:03:38)

## Next milestone

B2 — pack framework + tool registry + `core` pack; uploads/profile/cleaning/domains/contract
endpoints go live.
