## Status (date, branch, last commit)

2026-10-02 · branch `claude/beautiful-lovelace-07dnwu` · owner **Claude Code** (D-F1-5).
**F2 + F3 complete** on this branch, in PR #15 (unmerged; F3 stacked on F2 as separate
commits, as D-B0-1). The user asked for unattended work through usage limits; scheduled resumes
continue with F4.

## Screens: done | in progress | blocked (by which endpoint/issue)

- Done (F2): landing / expired / invalid / unreachable screens, sid-carrying navigation, saved
  session work, conflict choices, Ask with turn resume.
- Done (F3): Data (upload, choose, profile), Clean (tick → Apply, re-checked plan), Domain
  (confirm, evidence, no score-ticking), Contract (grain, key, roles, measures, window,
  caveats, forks, confirm with the engine's questions).
- Next, F4: metrics templates (approve with bindings/forks), validity rules (tick → approve),
  tools list (status, needs_domain, missing concepts) and tool runs with results (figures with
  provenance, chart-ready series, caveats, validity filters applied). Run errors handled by
  `code`: forks_unanswered → POST /forks, ambiguous_binding → bindings, param_required,
  festival_dates_unconfirmed.
- Then F5: the Ask evidence view (plan, tool calls, figure checks, answer.results as ToolResults,
  flags, usage). F6: keyword groups. F7: v1 parity (101 items) + Playwright tests.
- Blocked: ingest layout answers ([#16](https://github.com/Akash708018/analytics-agentV2.0/issues/16),
  no endpoint). Nonblocking: [#17](https://github.com/Akash708018/analytics-agentV2.0/issues/17)
  (contract in force), [#14](https://github.com/Akash708018/analytics-agentV2.0/issues/14)
  (list datasets), [#4](https://github.com/Akash708018/analytics-agentV2.0/issues/4) (error examples).

## API: spec version consumed; endpoints live vs mocked

**0.6.0**. Used against the real backend (browser checks) and a stateful fake (tests):
sessions, turns, upload, datasets, profile, cleaning, domains, packs, contract.
`ui_state` schema 2: `schema, page, label, dataset_id, datasets[ids], drafts{ds: {clean,
domains, contract{grain,key,roles,aggregations,definitions,per,window_*,caveats}, forks,
confirmed_version}}`; schema 1 migrates (C8). Unknown top-level keys pass through.

Rules the next screens must keep (`frontend/state.py` docstring, C7, C10):
- Widgets: `state.bind(ss, key, path, default)` before the widget, then
  `on_change=state.on_change, args=(ss, key, path)`. Add new draft fields to `normalize`.
- After any network call, change only held objects (`ss[DRAFT]`, `state.work(ss)`); queue
  widget resets in `work(ss)["reseed"]`; drop caches with `datasets.invalidate`.
- Never `st.stop()` in a view. Every 409 is a `VersionConflict`; branch on `code`.
- Nothing preselected; suggestions are explicit buttons (D-F3-1).

## api-request issues (links, status)

#4 (error examples), #14 (list datasets), #16 (ingest answers), #17 (contract in force): all open.

## Test tail (pasted)

```text
$ frontend/.venv/bin/python -m pytest frontend/tests -q -rs
196 passed in 11.15s
$ cd backend && uv run pytest -q -rs     # after C9
2354 passed, 1 skipped, 1 warning in 286.48s (0:04:46)
```

66 client, 58 Prism, 27 F2 state, 19 F2 AppTests, 2 fake-vs-spec, 14 F3 AppTests, 7 F3 prep,
3 F0. No frontend skips. Browser: F2 21 steps + blur check; F3 16 steps (upload → confirmed
contract) on the real backend, output in `docs/steps/F2.md` and `F3.md`.

## Next milestone

F4 (above). Setup in a fresh container: `uv venv frontend/.venv --python 3.12`, `uv pip install
--python frontend/.venv/bin/python -r frontend/requirements.txt`, `npm ci --prefix frontend
--ignore-scripts`, `uv sync` (backend for browser checks).
