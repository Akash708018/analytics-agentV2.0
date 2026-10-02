## Status (date, branch, last commit)

2026-10-02 · branch **main** (the user asked for work directly on main, D-F8-0) · owner
**Claude Code** (D-F1-5). F0–F7 merged (PR #15; F3–F7 in `e405019`). **F8 done on main**: the
screens consume API 0.7.0. **F9 done on main**: the "sunset foundry" look (theme, motion, scroll
effects; `docs/steps/F9.md`). **F10 done on main**: API 0.9.0. These are the last api-requests
(#16, #17, #20–#23), so no v1 item is blocked now (`docs/steps/F7.md`, update). **F11 done on
main**: an end-to-end Playwright suite in the repo, with the user's yes (D-F11-1): 17 tests on the
real backend, 51 of 51 over three repeats. It found a backend bug,
[#24](https://github.com/Akash708018/analytics-agentV2.0/issues/24) (open).

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
- Done (F9): the look.
  - The theme lives in `frontend/.streamlit/config.toml`; motion and textures in
    `components/style.py`.
  - Keep new bordered containers as `style.card(key)` so they get the card style.
  - Keep motion inside the reduced-motion and `@supports` guards.
- Done (F10, API 0.9.0):
  - Data: answer a refused upload's layout questions. A blank form sits beside the file's first
    rows; the reader's guess is a button.
  - Contract: the contract in force.
  - Clean: what each step loses, its samples and SQL.
  - Keyword groups: withdraw an approval.
  - Tools: declared optional params, a widget per kind.
  - **Explore** (new page): the 27 core analyses run directly, and playbook reports.
  - Parity: 52 ported, 42 changed, 7 dropped, 0 blocked.
- Done (F11): `frontend/e2e/`, run by `npm run e2e --prefix frontend`. It starts the real backend
  (scripted model) and Streamlit, runs 17 tests in 3 workers (about 2 minutes), and removes the
  run's workspaces. A large-file test is included: 300,000 rows load in about 4 s, and 2,000,000
  rows (89 MB) in 5 s.
  - **From now on a milestone adds or updates e2e tests instead of a one-off script.**
  - Use the helpers' `choose`, `tick`, `multiselect`/`chips` and `eventually`; they encode what
    F2–F11 learned about Streamlit 1.64's DOM and reruns.
- Blocked: nothing. Open on the backend: #24 (concurrent requests on one workspace can answer
  500). The page shows the service's error; a reload recovers.

## API: spec version consumed; endpoints live vs mocked

**0.9.0**. Used against the real backend (browser checks) and a stateful fake (tests):
- sessions, turns, upload and upload answers, datasets, profile;
- cleaning (with `values_lost`, `samples`, `sql`), domains, packs (tools with
  `params_optional`, playbooks);
- the contract proposal (with `prefill`) and the contract in force;
- metrics, validity rules, tools, tool runs (domain and `core.*`), analyses, reports, forks;
- keyword groups: run, list, actions incl. `unapprove`, `joins`, `carry_forward`;
- stored results: list, get, inspect.
`ui_state` schema 2: `schema, page, label, dataset_id, datasets[ids], drafts{ds: {clean,
domains, contract{grain,key,roles,aggregations,definitions,per,window_*,caveats}, forks,
confirmed_version, rules, tool, params{tool: {param, bindings}}, bindings{template: {concept:
column}}, keywords{column, ticks{group_id: true}}, result, explore{analysis, fields{analysis:
{field: value}}, playbook, slots{playbook: {slot: value}}}}}`; schema 1 migrates (C8). A refused
upload's answers stay in the browser (D-F10-2). Tool results are browser-only (D-F4-2). Unknown top-level keys pass through.

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

Open: [#24](https://github.com/Akash708018/analytics-agentV2.0/issues/24). Two requests on one
workspace at once can answer 500 (a DuckDB attach conflict in `db.connect`). It was found by the
F11 suite and reproduced on a plain backend; the backend owns the fix.
Closed on 2026-10-02 after B10/B11 (checked 2026-10-02 ~20:30Z): #16 (ingest
answers), #17 (contract in force), #20 (withdraw; the unknown-column 500 is now a 422), #21
(cleaning samples and SQL), #22 (direct core analyses and reports), #23 (declared optional params).
F10 consumes all six.
Closed on 2026-10-01 at 20:40 UTC by the repo-owner account (checked 2026-10-02 06:56Z): #14
(list a workspace's datasets; closed "completed", but API 0.6.0 has no such endpoint, so the
frontend keeps dataset ids in `ui_state`, C8) and #4 (spec error examples). Earlier handoffs
listed both as open by mistake.

## Test tail (pasted)

```text
$ frontend/.venv/bin/python -m pytest frontend/tests -q -rs
289 passed in 29.29s
$ npm run e2e --prefix frontend                    # F11: real backend, headless Chromium
17 passed (1.8m)                                    # --repeat-each=3: 51 passed (5.0m)
$ cd backend && uv run pytest -q -rs     # main after B9 + C9 + C11 (F8 changes no backend file)
2376 passed, 1 skipped, 1 warning in 289.85s (0:04:49)
```

66 client, 64 Prism, 28 F2 state (incl. C13), 19 F2 AppTests, 2 fake-vs-spec, 14 F3 AppTests,
7 F3 prep, 13 F4 AppTests, 5 F4 prep, 6 F5, 12 F6, 4 F7, 16 F8, 4 F9, 18 F10, 3 F0. No frontend skips. Browser (real backend): F2 21 steps +
blur check; F3 16 steps (upload → confirmed contract); F4 15 steps (metrics, rules, tools,
results); F5 8 steps (answers with evidence, real app + scripted model); F6 18 steps (keyword groups:
propose, approve, merge, split, move, rename, refresh, performance by approved group); F7 4 steps
(CSV identical to the API; new session leaves the old one), and F2 + F6 re-run in full after C13;
F8 8 steps (lineage, Results rows, focus choices, staleness, pre-fill, a blocked plan); F9 every
screen screenshotted and reviewed, plus scroll reveal, the progress line and reduced motion
measured; F10 8 steps (refused upload answered with the guess, contract in force, losses and
SQL, withdraw, a declared optional param, an Explore run equal to the API's, a report equal to
the API's, no contract and no domain).

## Next milestone

None planned. When #24 is fixed, the e2e helpers' GET retry (`helpers.get`) and the
"read before the page loads" ordering can stay; they cost nothing. The new suite needs Node
and Chromium: `npm ci --prefix frontend`, plus `npx --prefix frontend playwright install
chromium` outside this container (Chromium build 1194 is preinstalled here). Setup in a fresh container: `uv venv frontend/.venv --python 3.12`, `uv pip install
--python frontend/.venv/bin/python -r frontend/requirements.txt`, `npm ci --prefix frontend
--ignore-scripts`, `uv sync` (backend for browser checks).
