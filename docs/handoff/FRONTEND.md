## Status (date, branch, last commit)

2026-10-02 · branch **main** (the user asked for work directly on main, D-F8-0) · owner
**Claude Code** (D-F1-5). F0–F7 merged (PR #15; F3–F7 in `e405019`). **F8 done on main**: the
screens consume API 0.7.0. What remains waits on api-requests or on the user (Playwright suite).

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
- Done (F7): the 101 v1 items assessed in `docs/steps/F7.md` (20 ported, 63 changed, 7 dropped,
  11 blocked). Every result downloads its figures as CSV, and the Session page starts a new
  session (v1's reset). C13: `bind` sets widget values on every run.
- Done (F8, API 0.7.0):
  - a **Results** page: stored results, staleness with reasons, every row through `inspect`;
  - every result's status and lineage;
  - blocked plans and routing on Ask;
  - carry-forward "Add to" on Keyword groups;
  - contract pre-fill from a similar file;
  - `focus` and the `ambiguous_value` / `unknown_value` choices on Tools.
- Next, waiting on others: the api-requests below (each unblocks named F7 items); a Playwright
  test suite if the user says yes (D-F7-3).
- Blocked: ingest layout answers ([#16](https://github.com/Akash708018/analytics-agentV2.0/issues/16),
  no endpoint). Nonblocking: [#17](https://github.com/Akash708018/analytics-agentV2.0/issues/17)
  (contract in force), [#20](https://github.com/Akash708018/analytics-agentV2.0/issues/20) (withdraw a keyword-group
  approval; 500 on an unknown column), [#21](https://github.com/Akash708018/analytics-agentV2.0/issues/21)
  (cleaning samples/SQL), [#22](https://github.com/Akash708018/analytics-agentV2.0/issues/22)
  (direct core analyses and reports: 35 v1 items).

## API: spec version consumed; endpoints live vs mocked

**0.7.0**. Used against the real backend (browser checks) and a stateful fake (tests):
sessions, turns, upload, datasets, profile, cleaning, domains, packs, contract (with
`prefill`), metrics, validity rules, tools, tool runs, forks, keyword groups (run, list, actions,
`joins`, `carry_forward`), stored results (list, get, inspect).
`ui_state` schema 2: `schema, page, label, dataset_id, datasets[ids], drafts{ds: {clean,
domains, contract{grain,key,roles,aggregations,definitions,per,window_*,caveats}, forks,
confirmed_version, rules, tool, params{tool: {param, bindings}}, bindings{template: {concept:
column}}, keywords{column, ticks{group_id: true}}, result}}`; schema 1 migrates (C8). Tool results are browser-only (D-F4-2). Unknown top-level keys pass through.

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
- `state.bind` sets the widget's value on every run (C13); keep it that way. A widget the browser
  rebuilt while the server kept its key showed its default over a saved value.

## api-request issues (links, status)

Open: #16 (ingest answers), #17 (contract in force), #20 (keyword groups: withdraw an approval;
500 on an unknown column), #21 (cleaning samples and SQL), #22 (direct core analyses and reports),
#23 (declare optional tool params such as `focus`).
Closed on 2026-10-01 at 20:40 UTC by the repo-owner account (checked 2026-10-02 06:56Z): #14
(list a workspace's datasets; closed "completed", but API 0.6.0 has no such endpoint, so the
frontend keeps dataset ids in `ui_state`, C8) and #4 (spec error examples). Earlier handoffs
listed both as open by mistake.

## Test tail (pasted)

```text
$ frontend/.venv/bin/python -m pytest frontend/tests -q -rs
259 passed in 26.75s
$ cd backend && uv run pytest -q -rs     # main after B9 + C9 + C11 (F8 changes no backend file)
2376 passed, 1 skipped, 1 warning in 289.85s (0:04:49)
```

66 client, 64 Prism, 28 F2 state (incl. C13), 19 F2 AppTests, 2 fake-vs-spec, 14 F3 AppTests,
7 F3 prep, 13 F4 AppTests, 5 F4 prep, 6 F5, 12 F6, 4 F7, 16 F8, 3 F0. No frontend skips. Browser (real backend): F2 21 steps +
blur check; F3 16 steps (upload → confirmed contract); F4 15 steps (metrics, rules, tools,
results); F5 8 steps (answers with evidence, real app + scripted model); F6 18 steps (keyword groups:
propose, approve, merge, split, move, rename, refresh, performance by approved group); F7 4 steps
(CSV identical to the API; new session leaves the old one), and F2 + F6 re-run in full after C13;
F8 8 steps (lineage, Results rows, focus choices, staleness, pre-fill, a blocked plan).

## Next milestone

None planned. When an api-request lands, build its blocked items:
- #16 → Upload & read (V1-I04–I14);
- #21 → cleaning samples and SQL;
- #22 → direct core analyses (Explore);
- #23 → read optional params from the spec, dropping `prep.optional_params`' step scan. A Playwright
test suite needs the user's yes. Setup in a fresh container: `uv venv frontend/.venv --python 3.12`, `uv pip install
--python frontend/.venv/bin/python -r frontend/requirements.txt`, `npm ci --prefix frontend
--ignore-scripts`, `uv sync` (backend for browser checks).
