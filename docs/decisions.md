# Decisions (shared, append-only; PR required)

IDs: decisions `D-B<n>-<k>` (backend) / `D-F<n>-<k>` (frontend); open items `O-B<n>-<k> IS OPEN`;
self-corrections `C<n>` with reasoning. Every figure here was printed by a command.
v1's decision log (6,400 lines, `P<phase>-D<n>`) stays in the v1 repo @ `0ba324b`.

## B0 — Seed and baseline (2026-09-26)

**D-B0-1. Branch name deviates from the protocol — ASKING.** The protocol says
`backend/B<n>-<slug>`. This cloud session is bound to `claude/analytics-agent-v2-backend-6gcu1p`
and may not push to another branch without the user's explicit permission. Evidence: session
git instructions. Proposal: keep this branch for the backend's PRs (one PR per milestone,
retitled `B<n>: ...`), or the user grants permission to push `backend/B<n>-<slug>` branches.
Until answered, B0 is on the session branch.

**D-B0-2. One v1 test is skipped, with a reason.**
`tests/test_agent.py::test_the_screens_the_rules_name_are_the_screens_the_app_has` reads
`ui/app.py` (the v1 Streamlit app, not seeded per B0) and compares its `st.Page` titles with
`agent.SCREENS`. v2's screens are the frontend's; `agent.SCREENS` is rewritten when the v2 agent
replaces `webapp/agent.py` (B5). Skip, not delete, so the count shows it. Expected suite:
2212 passed, 1 skipped — v1's own 2213/0 cloud-box measurement minus this test.

**D-B0-3. Layout: `src/analytics_agent` → `backend/engine` (package `backend.engine`); the
engine's project root is `backend/`.** Evidence: 11 v1 test files use cwd-relative paths
(`Path("tests/fixtures")`, `Path("docs/contracts")`, `Path("src/analytics_agent/server.py")`) and
several SKIP when the path is missing ("run from the repository root") — a wrong layout shows as
skips, not failures. So `config.PROJECT_ROOT` = `backend/` (`parents[1]`), the v1 suite runs from
`backend/` (`backend/pytest.ini`), and `workspace/`, `docs/contracts/`, `.env` resolve under
`backend/` as they did under v1's root. The package rename (vs keeping `analytics_agent`) is
done now because v2 code (`backend.packs`, `backend.tools`, ...) imports the engine everywhere;
renaming later costs more. 803 replacements in 168 files by a guarded script (`docs/steps/B0.md`).

**D-B0-4. v1 benches live in `backend/scripts/`, not `bench/`.** v1 tests import them by
`parents[1] / "scripts"` (test_domain_benchmark, test_stress_fixes, test_suggest_bench,
test_marketing_bench{,_v2}). Top-level `bench/` stays for B6's v1-vs-v2 comparison.

**D-B0-5. Dependencies: v1's, at v1's versions.** Root `pyproject.toml` (package `backend`,
build backend uv_build as in v1); `uv lock` re-resolved with v1's `uv.lock` as preference: only
streamlit's tree was removed. Measured: Python 3.12.3, duckdb 1.5.5, fastmcp 3.4.7, scipy
1.18.1, statsmodels 0.15.0, pydantic 2.13.4. numpy/pandas remain transitive (statsmodels), not
imported by the engine — v1's rule, unchanged.

**D-B0-6. Note for B1/B5: the engine already contains the web agent layer.**
`backend/engine/webapp/` holds v1's providers and failover (`llm.py`), token budget
(`budget.py`), figure checker (`verify.py`), agent loop (`agent.py`), and the Streamlit-facing
`real_backend.py`. It was seeded unchanged as engine code; v2's `backend/llm/` and
`backend/services/` will wrap or move these pieces, each move logged here.

**D-B0-1 resolved (2026-09-26).** The user delegated the decision. Backend PRs use the session
branch `claude/analytics-agent-v2-backend-6gcu1p`: it is the only branch this session can push.
Milestones are separate commits (`B<n>: ...`) and the PR title lists them.

## B1 — API contract v0.1 + sessions (2026-09-26)

**D-B1-1. Contract-first = models first, YAML exported.** The contract is declared as Pydantic
models with examples (`backend/api/schemas.py`) before any logic; `docs/api/openapi.yaml` is
exported from them and `test_openapi_drift.py` fails on any difference. Hand-writing the YAML
and matching FastAPI's output byte-for-byte would make the drift test fight the generator.

**D-B1-2. Upload is spec'd without `python-multipart`.** FastAPI needs that package to parse
forms; the upload route is 501 in B1, so its multipart body is declared in the spec only.
B2 adds `python-multipart` (the user delegated dependency decisions; logged then).

**D-B1-3. A turn runs once.** Worker threads run it; GET only reads the stored row. A turn
`queued`/`running` at startup becomes `interrupted` — never re-run, never re-spending quota.
B1's default runner records `error: agent_not_wired` (no fake answers) until B5.

**D-B1-4. ui_state policy.** 413 above 256 KB; 422 for API-key-shaped strings (OpenAI,
Anthropic, Gemini, Groq, GitHub, Slack prefixes), emails, phone numbers, or ≥20 same-shaped
objects (data rows). Heuristic by design: it stops accidents, not a determined client.

**C1. The ui_state PII scan was quadratic.** The first run took 63.21 s in one test: the email
regex `[A-Za-z0-9._%+-]+@` rescans from every start position on a long string with no `@`.
Every assertion passed, so only `--durations` showed it — and it was a request-time DoS on a
public endpoint. Fixed with bounded quantifiers (RFC local part ≤64); the suite went 66.67 s →
2.99 s; `test_ui_state_scan_is_linear_on_a_max_size_string` pins it under 1 s.

## B2 — Pack framework + tool registry + core pack (2026-09-26)

**D-B2-1. suggest.py's keyword sets moved into `packs/core/pack.yaml: word_classes`.** They
classify measure types, which is core's job; marketing words among them (ctr, roas) stay in
core because v1's tests pin their behaviour for every dataset. Moved by a script that asserted
equality with v1's sets first; `test_packs.py` pins five of them literally; suggest_bench
verdicts identical to v1.

**D-B2-2. Upload auto-confirms the ingest spec only when nothing is unresolved.** Reading a
file is not an interpretive choice when the engine has no open question; its `assumptions` are
returned for the person to see. Any unresolved layout question → 422 `ingest_needs_answers`.

**D-B2-3. A source can support a domain without signalling it.** `orders` (order_id, status)
appears in marketing and logistics files alike; the SLA fixture scored marketing 0.544 on
orders alone. `signals_domain: false` keeps orders listed as a source but out of the domain
score.

**D-B2-4. The LLM sees core analyses as ONE schema (`core_analyze`, enum of 27).** v1 did the
same with `compute_analysis`; 27 schemas would cost every call. Domain tools get one small
schema each, only when active. Core-only ≈194 tokens (estimate).

**D-B2-5. Workspaces live 30 days idle, like sessions** (v1's web TTL was 72 h). `V2Backend`
overrides `ttl_seconds`; `ANALYTICS_WORKSPACE_TTL_HOURS` no longer applies to the v2 API.

**D-B2-6. `python-multipart` added** (FastAPI's form parser; user delegated dependency
choices). Token counts are estimates (no tokenizer dependency) and labelled so.

**O-B2-1 IS OPEN. Claude Desktop and `tools/list_changed`.** Measured: FastMCP 3.4.7 emits
`ToolListChangedNotification` per session and a re-list shows the enabled tool
(`bench/mcp_list_changed.py`). Not measurable here: whether Claude Desktop re-lists on it.
Until someone runs the probe in Desktop, the MCP server follows the spec's fallback — expose
all tools; a gated tool answers with a plain message naming the domain or data it needs.

## B3 — marketing pack + Tier-1 tools (2026-09-27)

**D-B3-1. A tool is a list of engine steps with bindings, run through the engine's `_produce`.**
The contract gate, scoping and every v1 refusal apply unchanged to domain tools. No new maths.

**D-B3-2. A metric template becomes a contract ratio measure; approval = a new contract
version.** v1's ratio measures (signed numerator/denominator lists, scale) already compute
ratio-of-sums correctly; ROAS net of GST is numerator [order_revenue, -gst]. A name clash with
a file column (the file's per-row `ctr`) gets `_ratio`.

**D-B3-3. Ambiguity is a question, never a pick.** A concept bound to >1 column (Google + Meta
conversions) answers 422 `ambiguous_binding`; the person names one via `params.bindings`.

**D-B3-4. Shapes the engine cannot compute are listed but refused.** distinct_count,
weighted_mean (GSC position), semi_additive (followers), derived (MER, POAS):
`engine_ready: false` → 422 with the reason, never a wrong number. delivered_net ROAS → B4.

**D-B3-5. Changelog sources are secondary** (vendor/trade pages found by search); the four
dates agree across several; official platform pages were not reachable by search. Recorded
per entry.

**D-B3-6. Festival dates must be confirmed per run** (`dates_confirmed`), since lunar dates move.

**C2. My measurement-change prediction was wrong, the code right.** I expected the Meta
2026-01-12 caveat on the ROAS explainer; ROAS there uses backend order revenue, which Meta's
attribution windows do not touch. The caveat correctly fires only where platform conversions
are used. Test now asserts both.

## B4 — Tier-2 tools on shared engine analyses (2026-09-27)

**D-B4-1. Nine shared analyses in one engine module, tier 9.** Generic names; the guide's own
Tier 8 (forecasting) stays reserved. Added to the backend copy of the build guide, which v1's
roster test reads.

**D-B4-2. v2 analyses are off the v1 MCP surface (`surface="v2"`).** v1 asserts every analysis
is callable through `compute_analysis` / `render_chart`, whose signatures would grow by 20
parameters — tokens on every call, the cost v2 exists to cut. They are reached through domain
tools. Three v1 tests now check the v1 surface only (each edit marked `D-B4-2`); `catalogue()`
defaults to v1, so v1's own messages and menus are unchanged.

**D-B4-3. Marketing-mix modelling and regression forecasting are out of scope for v2.0.** They
need numpy/statsmodels model fitting in the engine, against the no-numpy rule. A later
decision, if the user wants them.

**D-B4-4. Person-entered inputs stay inputs.** Budget (pacing), spend per cohort (CAC, under
the cac_scope fork) and funnel steps are params the person gives, never guessed from data.

**C3. Two engine bugs caught by hand-worked numbers.** DuckDB's RE2 rejected `\u` escapes; and
`unit_economics` joined cohort customers to month rows, doubling revenue per customer (1800 vs
900). Both fixed before any figure left the engine.

**C4. The runner reported only the first numeric column.** A cohort row's CAC was lost behind
its size. Every numeric cell is now a figure, named `step: row [column]`.

## B5 — interpretation filter + playbooks (2026-09-28)

**D-B5-1. Two LLM calls per playbook answer (+1 correction at most).** The planner picks and
fills slots; the engine runs steps; the explainer only words results. The explainer sees
figures (≤30 per tool) and caveats — no rows, no SQL.

**D-B5-2. Interpretation rules are regexes over the answer plus the trace, by design.**
Deterministic, cheap, testable; each has a violating and a passing fixture. They catch the
wordings listed, not every paraphrase — the flag list says what they checked.

**D-B5-3. `rate_mix_shift` added (tier 9)** because v1's `mix_shift` is for means and refuses
ratios; a blended rate's mix weight is its denominator share.

**C5. `correlational_only` read "no holdout was marked" as a holdout.** A substring match on
"holdout was marked". Now only the engine's "(a holdout was marked)" wording counts; a test
pins the negative.

## B6 — v1-vs-v2 bench (2026-09-28)

**D-B6-1. The bench is offline and scripted, for both agents.** Same question, same generated
table, same estimator (chars/4 of each request, v1's rounding). It measures machinery; live
model behaviour needs keys and is out of B6's scope.

**D-B6-2. v1's baseline is v1's own checkout, re-run — not its step-2 doc.** The recorded list
(2,488…) predates v1's step 3, which grew its schemas; v1 @ 0ba324b measures 2,890…, and the
seeded copy matches it within 0.5%.

**D-B6-3. The fallback runs core analyses (`core_analyze`) and batches calls.** B5's fallback
offered domain tools only, so a non-marketing question could run nothing. The model supplies
column names; `where` is dropped (the LLM writes no SQL).

**C6. Three bench bugs, found by the first run's numbers.** A stale v1 baseline (above); a
figure filter that matched `line_revenue` inside the COST result's caveats (0/4 → 4/4); a regex
for a summary line the marketing scripts do not print (replaced by a v1-vs-v2 output diff).

## B7 — keyword grouping (2026-09-28)

**D-B7-1. numpy + scikit-learn, in `backend/text/` only (the logged exception).** Approved under
the user's standing delegation ("full access to decide"). Clustering keywords is vector maths
that SQL does not do. Enforced by a test: nothing under `backend/engine/` imports numpy,
pandas or sklearn.

**D-B7-2. Embeddings: local Ollama (bge-m3) when reachable, else character n-grams.** Measured:
no Ollama server in this container (`curl localhost:11434` fails). The fallback is TF-IDF over
character 2-4-grams of the keyword's topic words (facet words removed first): deterministic,
offline, tolerant of typos and Hinglish spellings, no model download. Vectors are cached by
text hash. Which backend ran is recorded on every grouping.

**D-B7-3. The gold grouping is drafted by the backend agent and is NOT independent.** It is
marked `status: pending_user_approval`; every purity/completeness figure says "against the
author's draft gold" until the user approves (or edits) the file. Self-written labels flatter
the method that wrote them.

**D-B7-4. Groups are proposals until a person approves them.** Only approved groups reach the
engine (a `_kw_groups` table in the workspace, written by the approve/rename/merge/move/split
actions). The LLM may propose a label and intent; without a model, rules propose them, and
`proposed_by` says which.

## F1 — HTTP client and spec-driven mock (2026-09-28)

**D-F1-1. Use Prism as explicitly requested when Node is available.** Node 26.7.0
is installed. Pin `@stoplight/prism-cli` 5.16.0 and commit its lockfile; the mock
reads the canonical OpenAPI file directly. Contract tests use Prism's bundled
YAML parser, adding no Python dependency or copied API contract. Examples are
stateless fixtures, not evidence that sessions or turn execution persist.

**D-F1-2. A conflict is exposed without retry or automatic adoption.** The original
B1 handoff suggested adopting the current state and retrying. The user's explicit
two-tab choice requirement governs: `VersionConflict.current` exposes the server
state; F2 will offer the person's choices. Turn submissions are never retried.

**D-F1-3. Consume API 0.3.0 after its concurrent merge.** The additive dataset lookup
and fork-answer routes bring coverage to 29 operations. All 409 responses retain
the requested `VersionConflict` boundary, but preserve the backend's `error.code`.
`needs_domain` and `needs_data` are prerequisite errors with no current session;
callers must check the code before showing session-conflict controls.

**D-F1-4. Record mock limitations without changing browser protections.** HTTP
tests exercised every operation and declared error status. Browser navigation to
the mock's JSON response was blocked by the browser tool, so F1 makes no browser
rendering claim. Issue #4 tracks the two remaining missing path declarations and
generic error examples; the frontend does not patch backend-owned OpenAPI.

## F1 refresh — frontend handed to Claude Code; API 0.5.0 (2026-10-01)

**D-F1-5. The frontend moves from Codex to Claude Code, on the user's instruction.** On
2026-10-01 the user told the Claude Code session to "take over the front-end section from
codex and start working". Claude Code now owns `frontend/`, `docs/steps/F*` and
`docs/handoff/FRONTEND.md` as well as the backend paths. The frontend rules in `AGENTS.md`
(HTTP-only, no computing or reformatting figures, explicit choices, milestone evidence) still
govern frontend work. `CLAUDE.md`'s ownership line now says so. Codex should not push to
`frontend/` unless the user hands it back.

**D-F1-6. The branch follows the session, as in D-B0-1.** This session can push only
`claude/beautiful-lovelace-07dnwu`, not `frontend/F<n>-<slug>`. Codex's F1 branch
(`frontend/F1-api-client-mock`, PR #6, based on `305bcc0`/API 0.3.0) was merged into it with
its history kept, then updated. A new PR supersedes PR #6. Nothing was force-pushed.

**D-F1-7. Consume API 0.5.0: 30 operations.** New method `run_keyword_grouping` for
`POST /datasets/{id}/keyword-groups/run` (body `{}` or `{column}`).
`apply_keyword_group_action` takes `keywords[]` for `split`. The real backend also answers
409 `contract_required` (tool runs, metric approval); a unit case now pins that its code is
kept. Issue #4: 0.5.0 declares every path parameter and has no 501 routes; only the shared
generic error example remains open.

**D-F1-8. pyyaml was approved for the frontend but not added.** Asked before the F1 branch
was found: the user approved pinning pyyaml 6.0.3. Codex's tests already read the YAML
through Prism's bundled parser (D-F1-1), so no Python dependency was needed;
`frontend/requirements.txt` is unchanged.

**D-F1-9. First run against the real backend, not only Prism.** `frontend/dev/live_smoke.py`
drives the client against `uvicorn --factory backend.api.app:create_app` (`AA_NO_LLM=1`,
empty state dir): sessions, a stale-version conflict with `current`, ui_state rejection,
upload, domain detection, tool gating, a turn, and delete. Output is in `docs/steps/F1.md`.
Prism stays the contract test; the smoke run is evidence that the live server behaves the same.

## B8 — logistics pack (2026-10-01)

**D-B8-1. SLA breach, on-time, in-full and RTO rate are COMPARISON metric templates, approved
through v1's provisional metrics.** A template names `concept op right` (or `op value`) from the
engine's fixed operator list; approving it proposes and decides a v1 provisional metric in one
step (the person's API call is the decision). Every result reading one says PROVISIONAL, exactly
as v1's SLA bench does. Adding it to the contract is a separate contract change (the v1 contract
draft takes no comparison measures).

**D-B8-2. "Which group to drill into" is chosen by the engine, never the model.** A step filter
`{concept, focus_param, worst_by}` uses the person's `params.focus` when given; otherwise the
runner runs `group_compare` through `_produce` and takes the highest-rate group with at least
the minimum group size, and says which it took and why. The planner LLM never picks a hub.

**D-B8-3. New structured filters, no SQL in the pack:** `keep_matching` (plain words, exact
match after trim/lower), `compare {op, other}` (fixed operators), `focus_param/worst_by`, and
`older_than {as_of_param, days_param}`. One new validity rule kind, `require_after` (drop rows
whose end timestamp is not after the start), scoped by `applies_to` so it never hides
undelivered rows from RTO or stuck-shipment tools.

**D-B8-4. OTIF is reported honestly as its parts.** A joint on-time-AND-in-full flag needs a
row-level AND the comparison metric cannot express; the tool reports on-time rate, in-full
rate, and on-time rate among in-full rows, and says the joint rate needs one more approved
column. **RTO** reads the RTO flag column, and a data check names rows where the flag and the
status disagree (in the SLA fixture: delivered-but-flagged and RTO-but-unflagged rows).

## F2 — sessions, saved work, conflict choices, turn resume (2026-10-01)

**D-F2-1. The F2 browser check uses the preinstalled Playwright, once, outside the repo.**
AGENTS.md allows Playwright only in F7. This cloud session has no desktop browser. The user
chose (2026-10-01): a one-off script, run against the real backend and Streamlit, with its
output pasted into `docs/steps/F2.md`. It is not in `requirements.txt`, `package.json` or the
test suite, and the F7 rule still governs Playwright as a dependency.

**D-F2-2. Pages live in `frontend/views/`, not `frontend/pages/`.** Streamlit's legacy
multipage mode registers every file in a `pages/` folder as a standalone page. AppTest's
`switch_page` matched that registration and ran `pages/session.py` without the router, so
nothing was saved (measured: no PUT after a page switch). `st.navigation` ignores `pages/` in
the browser, but the folder name invited the ambiguity. Renamed.

**D-F2-3. A conflict that differs only in `page` is not put to the person.** Every
navigation saves `page`. Two tabs of one session that only navigate would otherwise show
conflict banners with no work at stake. If the server's and this tab's `ui_state` match on
everything except `page`, the tab adopts the server's version and saves its own page next
run. Any difference in the person's work (label, active dataset, dataset list, keys from a
newer frontend) still waits for Load latest / Keep my changes. Measured in the browser: two
tabs navigating, 0 banners; a stale label edit, 1 banner.

**D-F2-4. Dataset references are unioned on "Load latest".** The API has no dataset listing,
so `ui_state.datasets[]` is the session's only pointer to uploads (display metadata only:
id, name, rows, columns). Adopting another tab's state must not orphan an upload that exists
on the server either way. api-request
[#14](https://github.com/Akash708018/analytics-agentV2.0/issues/14) asks for
`GET /workspaces/{ws}/datasets`, which would remove the need.

**D-F2-5. A turn is followed by id; a lost reply is looked up, never resent.** A `POST /turns`
that fails at the transport level may have been stored. The session's turn list is checked
for a new turn with the same question. Only if none is found does the person get "Send again".
A refresh lists the session's turns and polls running ones (`st.fragment(run_every=2)`).
Question drafts stay in the browser: free text is not put in `ui_state`.

**C7. Streamlit can stop a run between a PUT and recording its new version.** Found only by
the browser: text typed without Enter, then a click on a sidebar link, produced two reruns.
The first PUT landed (v2), but Streamlit stopped that run at the next session-state access.
Every `SafeSessionState` read and write calls `_yield_callback`, and that access was the line
recording v2. The second run saved with v1 and showed the person a conflict with their own
tab. Fix: after a network call returns, update only objects already in hand, never
`st.session_state`. Pinned by a test that stops the run at the first session-state access
after the PUT, falsified by putting one such access back. The AppTests could not show this,
because AppTest runs one rerun at a time.

## B9 — "Implement now" concepts from the Future Concepts doc (2026-10-02)

Source: the user's doc "analytics-agent — Future Concepts for Upgrade", section "Implement now"
(ranks 1–23). The user asked to implement only the ideas that support the finished backend.

**D-B9-1. Built (rank):** 1 embedding version contract · 2 typed result contract · 5 evidence
lineage + staleness · 6 claim validation (direction and rate-as-percent) · 7 on-demand result
inspection · 9 boundary-aware playbooks · 10 playbook routing · 12 column meaning match ·
13 contract pre-fill · 14 value matching (for `focus`) · 16 keyword carry-forward ·
22 metamorphic tests. **Already done before B9:** 3 (measure types, ratio-of-sums templates,
B2–B4), 20 (`marketing.conversion_reconciliation`, B4), 21 (logistics, B8).
**Not built, with the reason:** 4 bitemporal definitions (needs valid-time on every
definition — a contract-store redesign, not a fold-in); 8 Desktop gating (needs a real Claude
Desktop, O-B2-1); 11 example retrieval (no store of approved examples exists yet); 15 hybrid
retrieval (no retrieval index to fuse); 17 cleaning variants by meaning (cleaning proposals are
the v1 engine's; a separate milestone); 18 creative themes, 19 paid vs organic (new tools on
data shapes with no fixture); 23 synthetic ledger/payroll/inventory data (no such packs).

**D-B9-2. Routing never guesses.** A question skips the planner only when exactly one usable
playbook matches at least two of its pattern words, leads the next by two, and every slot it
needs is filled unambiguously by a pattern in the question (two YYYY-MM months: later =
period; one YYYY-MM-DD date = start). Otherwise the planner runs as before.

**D-B9-3. Pre-fill stays inside one workspace and is only a suggestion.** The most similar
dataset (column-name Jaccard ≥ 0.8) with a confirmed contract, in the SAME workspace, supplies
`prefill` on the contract proposal; nothing is applied until the person confirms it with the
ordinary call. Across workspaces would leak one client's definitions to another.

**D-B9-4. Stored results.** Every domain tool run is stored (SQLite) with a snapshot hash of
the table (order-independent hash computed in DuckDB), the contract version and the metric
versions; up to 500 rows per step are kept for inspection. A result is `stale` when the table
or contract has changed since. Inspection sorts and filters stored engine rows; it computes
nothing new.

**C7. Two wrong test expectations in B9, both mine.** Routing tests used "drop"/"fall" where
the playbook patterns say "dropped"/"fell" (led to stem matching, an improvement); the
totals-row test expected a caveat the v1 loader makes unnecessary (it drops a trailing totals
row at upload) and then matched 'Total' after cleaning had folded it to 'TOTAL'.

## F3 — guided preparation: clean, domain, contract (2026-10-02)

**D-F3-1. Nothing is pre-ticked or preselected; suggestions are explicit clicks.** Cleaning
steps the API marks `suggested`, forks with a `suggested` option, and strong aggregation
suggestions all start blank. Each shows its reason, and a "Tick the N suggested" / "Use the N
suggested answer(s)" / "Use the engine's N strong suggestion(s)" button fills only what is
still unanswered. The API's own text allows pre-ticking (`CleaningProposal.suggested`: "may be
pre-ticked"); AGENTS.md and CLAUDE.md ("never silently default") are stricter, and win.

**D-F3-2. Cleaning ticks are kept by id+kind+column, and Apply re-reads the plan.** Measured:
after applying C001+C002, the engine renumbered the remaining action to C001. A tick kept by
id alone would have applied a different step. If any ticked step no longer matches, Apply
sends nothing and asks for a review.

**D-F3-3. Domain ticks start from the server's `confirmed` list, never from a score.**
Marketing (0.385 on the F3 file) starts unticked, like every pack. Marketing is listed first
(the priority domain); core is never offered.

**D-F3-4. Contract answers stay in `ui_state.drafts` after a confirm.** The proposal does
not return the definitions, window, `measure_per` or fork choices in force, so dropping the
draft would blank those fields. That is api-request
[#17](https://github.com/Akash708018/analytics-agentV2.0/issues/17). "Start over from the
engine's proposal" drops the draft explicitly.

**C8. F2 stored dataset metadata objects in `ui_state`, which the backend refuses.** Measured
against the backend's own guard: 20 `{dataset_id, name, rows, columns}` objects →
"20 same-shaped objects look like data rows", and a name like `leads_9876543210` → "looks like
personal data". Schema 2 stores ids only (50 pass) and fetches names; schema 1 migrates.

**C9. The backend refused 0.34% of its own dataset ids (0.51% of turn ids) as phone numbers.**
Found by the F3 browser check: `ds_2a7207443888` holds "7207443888", so every save after
that upload answered 422, and the session forgot the dataset. Fixed in
`backend/services/sessions.py`: an exact server id (`ds_/ws_` + 12 hex, `t_` + 16 hex) skips
the phone check, while a phone number in free text is still refused. Measured 0 of 20,000
after the fix. 5 new tests; full backend suite 2354 passed, 1 skipped. This is a backend file,
changed by the frontend owner because the user asked for unattended work: logged here, and in
the PR, for the backend owner to review.

**C10. The key cannot be a column role.** With one role per column, the date column could
not also be in the key, so "one row per campaign per day" was sent as `primary_key
["campaign"]` and refused (`LOAD_REFUSED`, why "grain, analysis_window."). Probed: `[date,
campaign]` passes, and a key column may also be a dimension. The key is now its own
multiselect; roles are date/measure/dimension/ignore.

## F4 — metrics, validity rules, tools and results (2026-10-02)

**D-F4-1. The Tools page offers domain tools only.** `GET /tools` lists core analyses as active,
but they run through a question (turn). Measured: running one as a tool answered 500 (fixed,
C11). The page says so and points to Ask.

**D-F4-2. Tool results stay in the browser; params are saved.** A result is data (figures,
series), so it is not put in `ui_state`. The chosen tool, its params and its bindings are saved,
so after a refresh the person runs again with one click. Festival-date confirmation is never
saved: dates are confirmed per run (D-B3-6).

**D-F4-3. Results are shown exactly as returned.** Figure values via `str()` (`null` → "suppressed"),
provenance per figure, series in the backend's order (`bar` with `sort=False`; table/funnel/
stacked_bar and series with suppressed points as tables), and every caveat, validity filter,
pack rule, fork answer and figure-check note.

**D-F4-4. "Name the column for a concept" is the person's answer to `needs_data`.** Metric
approval sends it as `bindings`, tool runs as `params.bindings` (the runner's override). The
frontend never infers a column. Measured: a binding given for a metric is not reused by tool
runs (noted for the backend owner).

**C11. A core analysis run as a tool answered 500.** `POST /tools/core.*/run` reached
`m.tools[tool_id]` (KeyError) before the existing `not_a_domain_tool` check. The check runs
first now; 2 tests fail without it. Backend file changed from the frontend session (unattended
run); full suite 2356 passed, 1 skipped.

## F5 — the evidence behind each answer (2026-10-02)

**D-F5-1. Every answer carries its trail, collapsed by default.** The answer text and its
unresolved flags are always visible. "How this was answered" (plan, every tool call with its
skip reason, the checks, usage) and one "Evidence" expander per tool result sit under it. An
answer with no result says so. The trail is the backend's record (TurnEvent, Turn.answer),
shown as returned; tokens are labelled as estimates, as the backend labels them.

**D-F5-2. Answered turns for evidence come from the real app with a scripted model.** This
container has no model keys. `create_app(llm=ScriptedLLM(...))` (the backend tests' seam)
gives real playbook runs, tool results, skips, checks and usage with scripted wording. The
harness lives in the scratchpad, not the repo; its code is in `docs/steps/F5.md`.

## F6 — keyword groups (2026-10-02)

**D-F6-1. The person chooses the search-term column; the run always sends it.** No endpoint says
which column the pack binds to the search-term concept before a run, and `run {}` uses that
binding silently. So the column box starts empty, Propose is disabled until a column is chosen,
and the run's own record (column, embedding, threshold, keywords read, proposals, spellings
merged) is shown as returned.

**D-F6-2. What an action does to approval is said before it is sent.** Measured: a merge lands
in the first id and takes its approval; a keyword takes the approval of the group it lands in;
split-off keywords become a proposal; proposing again drops every unapproved group with the
person's edits. The person picks the merge target (no default), the request is sent as
`[target, others…]`, and the screen warns when an approved group would lose its approval and
when a new proposal run would replace edits. There is no withdraw action (#20); the screen
says so.

**D-F6-3. Saved: the column and ticked group ids.** Keywords, typed names and view filters
stay in the browser. Ticks are cleared after approve, merge and a new run (their ids are
spent or replaced); ticks for ids that no longer exist are ignored.

**C12. The "Edit one group" choice was lost after every edit (browser check).** The browser
hands a selectbox's choice back as its shown text ("Sushi local (21)"). A split, move or rename
changes that text, so after the edit the choice matched no option and the edit controls
vanished. AppTest re-sends with the current labels and passed. The chosen group id is now held
in a plain session key and set on the selector each run. Before: the browser check failed at
step 12 in two runs. After: 18 of 18 steps.

## F7 — v1 parity (2026-10-02)

**D-F7-1. v1 parity is assessed item by item, not copied.** `docs/steps/F7.md` gives each of the
101 F0 items a verdict with where and why: 20 ported, 63 changed, 7 dropped, 11 blocked. Changes
follow v2's rules (nothing preselected, figures as returned, work saved on the server).
Blocked items are api-requests (#16, #21, #22).

**D-F7-2. Results download as CSV; a new session replaces "Reset workspace".** The CSV is the
results table cell for cell (no formatting), on Tools and on each Evidence. A new session
destroys nothing (the old one stays at its link), so it needs no confirmation.

**D-F7-3. No Playwright test suite without the user's yes.** D-F2-1 chose one-off scripts, and a
test dependency is the user's call. The scripts stay in the scratchpad; their output is pasted
into each milestone doc.

**C13. A saved value could show as empty after a cut-short run (browser check).** `state.bind`
set a widget's value only when its key was new. When the browser rebuilt a widget while the
server kept the key, it showed the widget's default (an empty name over "F4 check": 2 of 4 F7
runs; the draft and server were untouched). `bind` now sets the key on every run, keeping the
"cut-short edit wins" rule. Before: 1 of 4 instrumented runs wrong. After: 8 of 8 right, and the
F2 (21), F6 (18) and F7 checks pass. A unit test fails without the fix.

**ID note (main merge of F3–F7 with B9, 2026-10-02).** Two entries are named C7: F2's (a
Streamlit run cut short between the PUT and its bookkeeping) and B9's (two wrong test
expectations). C8–C13 are the frontend's (F3–F7). The next self-correction takes C14.

## F8 — API 0.7.0 in the screens (2026-10-02, on main)

**D-F8-0. Frontend work is committed to main.** The user asked for work directly on main after
F3–F7 were merged (`e405019`). Shared files are still appended, not rewritten.

**D-F8-1. Stored results get their own page; nothing about them is recomputed.** Results
lists `GET /datasets/{id}/results`. The chosen one renders through the same results view, and
its rows come from `inspect` (the backend sorts, filters and pages). Staleness is shown with
every reason as returned. Only the chosen result id is saved. D-F4-2 still holds: the browser
keeps no result, and the server now does.

**D-F8-2. Every result says where it came from.** Status, snapshot rows and hash, contract
version, grain, metrics used and the result id are shown as returned, with a link to its rows.

**D-F8-3. Suggestions from 0.7.0 stay suggestions.**
- A similar file's contract fills only unanswered fields when clicked, and is confirmed as usual.
- Its domains, metrics and rules are listed, not applied.
- Carry-forward keywords join an approved group only through the person's "Add to" (merge with
  the approved id first).
- An ambiguous or unknown value offers the backend's values, none selected.

**D-F8-4. Optional tool params come from the pack's steps until the spec declares them.**
`sla_drivers` reads `focus` in its step filters, but its spec does not list it. The Tools page
reads `filter[].focus_param` (`prep.optional_params`); #23 asks for a declaration.


**C14. A question recorded as a decision.** The user wrote "froentend is complete", asking
whether it is. I recorded it as their declaration (D-F8-5, commit `77f63e3`, now reverted).
Whether the frontend is complete was answered in chat; the handoff stands as after F8.

## F9 — the look: "sunset foundry" (2026-10-02, on main)

**D-F9-1. Theme in Streamlit's config; motion in one CSS-only stylesheet.**
- `frontend/.streamlit/config.toml` (script-level config) sets colours, fonts, radii and the chart
  palette.
- `components/style.py` adds textures, motion and scroll-driven effects as style-only `st.html`
  (no space, no script).
- Bordered containers that should look like cards are keyed (`style.card`) so the stylesheet can
  find them.

**D-F9-2. Motion never hides content and can be turned off.**
- Scroll effects sit inside `@supports (animation-timeline: view())`.
- `prefers-reduced-motion: reduce` stops every animation and transition (measured: 0
  animations, the farthest card fully visible).

**D-F9-3. Chart colours come from the dataviz validator, not taste.**
- The reference steps are re-ordered with orange first; all gates pass on `#FBF7F2`.
- Slot 1 is 3.0:1. Sub-3:1 slots are relieved by every result's figures table.
- Text pairs are ≥ 4.5:1 (captions are un-dimmed for this).

## B11 — direct analyses and reports (issue #22, 2026-10-02)

**D-B11-1. Core analyses run directly, and reports run playbooks without the model.** The user
asked for the industry-standard option. Self-serve BI tools (Looker Explore, Mode, Metabase)
let a person run any governed analysis directly; the AI assistant is an extra, not the only
door. Here every direct run still passes the contract gate, the approved validity filters and
the engine (`_produce`), so nothing the rules protect is bypassed. The person picks each field
(no defaults are invented: an optional field left empty uses the analysis's own documented
behaviour); `where` is never accepted. A "report" is a playbook the PERSON chooses, run step
by step with no LLM call; its results share one `run_id`. Fields are read from each analysis's
own signature, so the declaration cannot drift from the code.

## F10 — upload answers, contract in force, losses, withdraw, optional params, Explore (2026-10-02)

**D-F10-1. Core analyses and reports get their own page, Explore, after Tools.**
- Tools stays the domain tools.
- Explore holds the 27 core analyses (B11) and the playbook reports.
- Both pages share `render_result`, so lineage, the link to every row, CSV and caveats are the
  same.

Nothing is chosen for the person: no analysis, no field, no playbook. The contract's own
choices are the only options for measure, dimension, column and grain fields.

**D-F10-2. A refused upload's answers are not saved with the session.**
- They belong to one upload, not to a dataset, and the reader asks again with the same
  `upload_id`.
- The form lives in the browser (`data.answers.<upload_id>.*`) until the file loads.
- "Use the reader's guess" fills only blank fields: an explicit button, never applied by
  itself (D-F3-1).

**D-F10-3. A declared number param is a whole number unless its default is fractional.** The
runner reads the declared counts (days, n, months) as integers; it converts a digit default with
`int`. A float 5.0 would reach analyses that expect 5. A pack that wants decimals says so with a
fractional default.

**D-F10-4. The contract in force sits above the form, collapsed.**
- Its label carries the version and the confirmation time.
- The person's caveats and the engine's counted caveats are separate lists, as the API keeps
  them.
- Its cache drops on everything that changes it: confirm, metric approval, rule approval and
  answered forks.

## B12 — QA suite (2026-10-02)

**C8. The engine rounded every float to 4 places, so a $0.00001 CPC read as 0 — and a v1 test
pinned that ("a reader sees zero where the value was not zero"; pinned as "invisible").** The QA
suite showed it as a reporting defect. Below 1e-4 a value now keeps four significant digits,
never scientific notation; values of 1e-4 and above are unchanged. The v1 pin was updated.

**D-B12-1. A label is data, never evidence.** The figure check masks the digits of free-text
group labels wherever they appear, so a number planted in a campaign name cannot vouch for a
claim. Pure-number labels (an attempt count) and dates keep their digits.
