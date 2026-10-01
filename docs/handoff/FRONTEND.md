## Status (date, branch, last commit)

2026-10-01 · branch `claude/beautiful-lovelace-07dnwu` · owner **Claude Code** (took over from
Codex at the user's instruction, D-F1-5). F1 is refreshed to API 0.5.0 and stopped for human
review. Base: main `42e77cd`. Codex's F1 (PR #6, `frontend/F1-api-client-mock`, API 0.3.0) is
merged into this branch with its history (`f6cd115`); the PR from this branch supersedes #6.

## Screens: done | in progress | blocked (by which endpoint/issue)

- Done: F0 landing scaffold (no product inputs) and the separate F0 state probe.
- Done: F1 HTTP client, 30 methods = every operation in `docs/api/openapi.yaml` 0.5.0;
  Prism contract tests; live-backend smoke (`frontend/dev/live_smoke.py`).
- Not started: F2 sessions (hydrate from `GET /sessions/{sid}`, versioned ui_state saves,
  Load latest / Keep mine on `version_conflict`, sid on every link, turn resume by id). Then the
  guided screens: upload, cleaning, domain, contract + forks, metrics, validity rules, tools and
  results, Ask, keyword groups. F7: v1 parity (101 items in `docs/steps/F0.md`) + Playwright.
- Blocked: nothing. Issue #4's remaining item (one generic error example) does not block.

F0 browser evidence (Streamlit 1.64.0): refresh keeps URL/sid but resets session state; native
navigation drops `sid` and page widget state; Back/Forward restore route history, not per-page
fields. F2 must hydrate from backend records and carry `sid` explicitly.

## API: spec version consumed; endpoints live vs mocked

**0.5.0** (main `42e77cd`). All 30 operations are live on the backend; none answers 501.
Tested two ways: Prism 5.16.0 serving the unmodified YAML (every operation, every declared error
status), and the real backend via `live_smoke.py` (sessions, conflict, ui_state rejection,
upload, detection, tool gating, a turn, delete — output in `docs/steps/F1.md`).

Client facts the screens rely on:
- Every 409 raises `VersionConflict`; only `code == "version_conflict"` carries `current`.
  `needs_domain`, `needs_data`, `contract_required` are prerequisites: branch on `code`.
- Error extras (`forks`, `options`, `columns`, `concept`, `missing`, `invalid`, `dates`,
  `questions`, `unresolved`, `provisional`, `needs_domain`, `missing_concepts`, `refusal`) are
  top-level keys of `APIError.payload`, next to `error`.
- No retries. Never resubmit `POST /turns` after a timeout; poll the stored turn id.
- Figures, series points and caveats are returned as decoded JSON, unchanged.

## api-request issues (links, status)

[Issue #4](https://github.com/Akash708018/analytics-agentV2.0/issues/4) — open. Path
parameters: resolved in 0.5.0. Status-specific error examples: still one generic `not_found`
example for every error status. Nonblocking.

## Test tail (pasted)

```text
$ frontend/.venv/bin/python -m pytest frontend/tests -q -rs
127 passed in 5.58s
```

66 unit (`test_api_client.py`), 58 Prism contract/error (`test_prism_contract.py`), 3 F0
AppTests. No skips. Python 3.12.3, Streamlit 1.64.0, httpx 0.28.1, pytest 9.1.1, Node 22.22.0,
Prism 5.16.0. Setup: `uv venv frontend/.venv --python 3.12`, `uv pip install --python
frontend/.venv/bin/python -r frontend/requirements.txt`, `npm ci --prefix frontend
--ignore-scripts`.

## Next milestone

STOP after the F1 refresh. The user reviews/merges the PR (and closes #6). F2 starts on the
user's instruction: re-read `docs/handoff/BACKEND.md` and the API changelog first, then build
`frontend/state.py` (hydrate, hash-checked single save per rerun, explicit conflict choice),
sid-preserving navigation, and turn resume, with AppTests plus a real-browser refresh check.
