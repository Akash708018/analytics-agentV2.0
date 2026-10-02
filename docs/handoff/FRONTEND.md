## Status (date, branch, last commit)

2026-10-02 · branch `claude/beautiful-lovelace-07dnwu` · owner **Claude Code** (D-F1-5).
F2 merged (PR #15). **F3, F4, F5, F6 complete** on this branch, in PR #18 (unmerged; separate
commits, as D-B0-1). The user asked for unattended work through usage limits; scheduled resumes
continue with F7.

## Screens: done | in progress | blocked (by which endpoint/issue)

- Done (F2): landing / expired / invalid / unreachable screens, sid-carrying navigation, saved
  session work, conflict choices, Ask with turn resume.
- Done (F3): Data (upload, choose, profile), Clean (tick → Apply, re-checked plan), Domain
  (confirm, evidence, no score-ticking), Contract (grain, key, roles, measures, window,
  caveats, forks, confirm with the engine's questions).
- Done (F4): Metrics (templates approve with bindings/forks answered in place; validity rules
  tick → apply) and Tools (domain tools, params from the pack, every reply code answered in
  place, results via `components/results.py`).
- Done (F5): every answer on Ask shows its flags, "How this was answered" (plan, tool calls
  with skip reasons, checks, usage) and its tool results as Evidence.
- Done (F6): Keyword groups. Choose the search-term column and propose; review each group (intent,
  facets, proposer, keywords); tick → approve; merge into a chosen group; rename, split or move
  a keyword. What each action does to approval is said before it is sent. Tools links here
  from the keyword tools.
- Next, F7: v1 parity (101 items in F0.md) + Playwright tests (if the user allows a test dependency).
- Blocked: ingest layout answers ([#16](https://github.com/Akash708018/analytics-agentV2.0/issues/16),
  no endpoint). Nonblocking: [#17](https://github.com/Akash708018/analytics-agentV2.0/issues/17)
  (contract in force), [#14](https://github.com/Akash708018/analytics-agentV2.0/issues/14)
  (list datasets), [#4](https://github.com/Akash708018/analytics-agentV2.0/issues/4) (error examples),
  [#20](https://github.com/Akash708018/analytics-agentV2.0/issues/20) (withdraw a keyword-group
  approval; 500 on an unknown column).

## API: spec version consumed; endpoints live vs mocked

**0.6.0**. Used against the real backend (browser checks) and a stateful fake (tests):
sessions, turns, upload, datasets, profile, cleaning, domains, packs, contract, metrics,
validity rules, tools, tool runs, forks, keyword groups (run, list, actions).
`ui_state` schema 2: `schema, page, label, dataset_id, datasets[ids], drafts{ds: {clean,
domains, contract{grain,key,roles,aggregations,definitions,per,window_*,caveats}, forks,
confirmed_version, rules, tool, params{tool: {param, bindings}}, bindings{template: {concept:
column}}, keywords{column, ticks{group_id: true}}}}`; schema 1 migrates (C8). Tool results are browser-only (D-F4-2). Unknown top-level keys pass through.

Rules the next screens must keep (`frontend/state.py` docstring, C7, C10):
- Widgets: `state.bind(ss, key, path, default)` before the widget, then
  `on_change=state.on_change, args=(ss, key, path)`. Add new draft fields to `normalize`.
- After any network call, change only held objects (`ss[DRAFT]`, `state.work(ss)`); queue
  widget resets in `work(ss)["reseed"]`; drop caches with `datasets.invalidate`.
- Never `st.stop()` in a view. Every 409 is a `VersionConflict`; branch on `code`.
- Nothing preselected; suggestions are explicit buttons (D-F3-1).
- A selectbox whose shown text can change (a label with a count, a renamed group) loses its
  choice in the browser; hold the chosen value in a plain session key and set it each run (C12).
  AppTest does not show this; the browser check does.

## api-request issues (links, status)

#4 (error examples), #14 (list datasets), #16 (ingest answers), #17 (contract in force),
#20 (keyword groups: withdraw an approval; 500 on an unknown column): all open.

## Test tail (pasted)

```text
$ frontend/.venv/bin/python -m pytest frontend/tests -q -rs
232 passed in 15.76s
$ cd backend && uv run pytest -q -rs     # after C9, C11
2356 passed, 1 skipped, 1 warning in 245.59s (0:04:05)
```

66 client, 58 Prism, 27 F2 state, 19 F2 AppTests, 2 fake-vs-spec, 14 F3 AppTests, 7 F3 prep,
13 F4 AppTests, 5 F4 prep, 6 F5, 12 F6, 3 F0. No frontend skips. Browser (real backend): F2 21 steps +
blur check; F3 16 steps (upload → confirmed contract); F4 15 steps (metrics, rules, tools,
results); F5 8 steps (answers with evidence, real app + scripted model); F6 18 steps (keyword groups:
propose, approve, merge, split, move, rename, refresh, performance by approved group).

## Next milestone

F7: v1 parity. Walk the 101 items in `docs/steps/F0.md` against the v2 screens and mark each
done / not applicable / missing with its endpoint; build the missing ones the API supports;
file api-requests for the rest. Playwright as a test dependency needs the user's yes
(D-F2-1 chose a one-off script). Setup in a fresh container: `uv venv frontend/.venv --python 3.12`, `uv pip install
--python frontend/.venv/bin/python -r frontend/requirements.txt`, `npm ci --prefix frontend
--ignore-scripts`, `uv sync` (backend for browser checks).
