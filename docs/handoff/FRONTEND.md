## Status (date, branch, last commit)

2026-10-01 · branch `claude/beautiful-lovelace-07dnwu` (restarted from main `f34dcd2` after
PR #12 merged) · owner **Claude Code** (D-F1-5). **F2 complete**; stopped for the user's review.
F1 (client + Prism mock, API 0.5.0) is on main via PR #12; PR #6 is closed.

## Screens: done | in progress | blocked (by which endpoint/issue)

- Done (F2): a landing page with one explicit "Start a new session". Session (name the
  analysis). Data (upload CSV/xlsx; choose the dataset questions use; a card from
  `GET /datasets/{id}`). Ask (send once, follow by id, survives refresh). Expired, invalid-sid
  and unreachable screens. Sidebar links carry `?sid=`. Conflict banner with Load latest /
  Keep my changes.
- Shown as returned but not yet acted on: `ingest_needs_answers` layout questions (Data),
  answer `results[]` (Ask shows a count; no evidence view yet).
- Next screens: ingest answers, cleaning approvals, domain confirm, contract + forks,
  metrics, validity rules, tool runs with figures/charts/caveats, the turn evidence view,
  keyword groups. Then F7: v1 parity (101 items, `docs/steps/F0.md`).
- Blocked: nothing. [#14](https://github.com/Akash708018/analytics-agentV2.0/issues/14)
  (list a workspace's datasets) would replace `ui_state.datasets[]` as the only pointer to uploads.

## API: spec version consumed; endpoints live vs mocked

**0.6.0** (B8, additive: no new operations; the suite passes, 173). F2 uses sessions (create/get/PUT ui-state), turns (create/get/list), upload and
`GET /datasets/{id}` against the real backend (browser check) and a stateful fake (tests).
`ui_state` allowlist: `schema, page, label, dataset_id, datasets[{dataset_id,name,rows,columns}]`.
Unknown server keys pass through. Saved widget keys: `ui.session.label`, `ui.data.dataset_id`.

0.6.0 items F3 will use: `DomainDetection.sources[]` (every pack's matched sources, with
`domain`; `marketing_sources` now holds marketing only), a second domain (`logistics`), and
`Confirmed.provisional` on comparison-template approvals (results then carry a PROVISIONAL caveat).

Rules the next screens must keep (see `frontend/state.py` docstring and C7):
- Session state is a cache; seed widgets with `state.seed`, record edits with
  `state.on_widget_change`, register new saved fields in `state.WIDGETS` + `normalize`.
- After any network call returns, update only objects already in hand. Never read or write
  `st.session_state` between an API write and recording its result: Streamlit can stop the run there.
- Never `st.stop()` in a view (it skips the router's save). Return early instead.
- Branch on `APIError.code`; every 409 is a `VersionConflict` but only `version_conflict`
  carries `current`.

## api-request issues (links, status)

- [#4](https://github.com/Akash708018/analytics-agentV2.0/issues/4): open. Path params fixed
  in 0.5.0; only the generic error example remains. Nonblocking.
- [#14](https://github.com/Akash708018/analytics-agentV2.0/issues/14): open, new.
  `GET /workspaces/{ws}/datasets`. Nonblocking.

## Test tail (pasted)

```text
$ frontend/.venv/bin/python -m pytest frontend/tests -q -rs
173 passed in 8.77s
```

66 client unit, 58 Prism contract, 26 F2 state/turn unit, 19 F2 AppTests, 1 fake-vs-spec, 3 F0.
No skips. Browser: 21-step check plus the B0–B3 blur check against the real backend
(headless Chromium, one-off Playwright, D-F2-1). Output is in `docs/steps/F2.md`.

## Next milestone

STOP after F2. The user reviews/merges the F2 PR. Proposed F3 (on the user's go-ahead): the
guided path from upload to a confirmed contract. Ingest layout answers, cleaning proposals →
approve, domain detect → confirm (no preselection), contract proposal + forks → confirm. Each
approval is explicit, and drafts are saved through `state.py`.
