## Status (date, branch, last commit)

2026-09-26 · branch `claude/analytics-agent-v2-backend-6gcu1p` (D-B0-1 resolved: this branch,
one commit series per milestone) · PR: https://github.com/Akash708018/analytics-agentV2.0/pull/1
Done: **B0** (v1 seed), **B1** (API 0.1 + sessions/turns), **B2** (packs, detector, tool
registry, core pack; API 0.2.0).

## API: spec version — live | stubbed (501) | changed since last handoff

`docs/api/openapi.yaml` **0.2.0** (additive, no breaking change; see `docs/api/CHANGELOG.md`).
Live now: sessions, turns, health/version, **uploads (multipart), `GET /datasets/{id}`,
profile, cleaning proposals/approve, domains detect/confirm, contract proposal/confirm, tools,
packs**. Still 501: metrics templates/approve, validity rules, `POST /tools/{id}/run` (B3),
keyword groups (B7).

## Tools: active tool ids per domain; tool-schema tokens (core vs core+marketing)

core: `core.profile`, `core.clean`, `core.contract` + `core.<analysis>` for the 27 engine
analyses — always active. marketing: none yet (B3 adds 20). LLM schema tokens: core-only
≈194 (estimate); core+marketing measured in B3.

## For Codex: what to build against now; breaking changes; mock notes

- Upload: `POST /workspaces/{ws}/uploads` multipart `file`; `ws` from the session. 422
  `ingest_needs_answers` carries `questions` to show. Show `assumptions` after upload.
- Domain step: `GET .../domains/detect` → show candidates + `evidence.matched_columns`; the
  person picks; `POST .../domains/confirm {"domains": [...]}`. Never auto-confirm.
- Contract: every `forks[]` item is a plain question with options; show `suggested` +
  `suggested_reason` as a hint only (not pre-selected as the answer without a click). Measures
  carry `suggested_agg` + `strength` + `reason`; `agg` stays empty until the person answers.
  Confirm body: `{"contract": {...}, "fork_choices": {fork_id: option_id}}`; 422 lists
  `missing`/`invalid` forks or `provisional` fields.
- Tools: `GET .../tools` → render `needs_domain` ("confirm marketing to use this") and
  `needs_data` (`missing_concepts`) as disabled with the reason.

## Open api-request issues + answers

None (checked 2026-09-26).

## Test tail (pasted)

    cd backend && uv run pytest -q -rs
    SKIPPED [1] tests/test_agent.py:580: v2 B0 (D-B0-2): the v1 Streamlit app ui/app.py is not seeded; v2 screens belong to the frontend
    2273 passed, 1 skipped, 1 warning in 201.28s (0:03:21)
    eval: SCORE: 76/76 (100%)

## Next milestone

B3 — marketing pack knowledge (templates, forks, validity rules, changelog) + 20 Tier-1
tools as presets; metrics/validity-rules/tool-run endpoints go live.
