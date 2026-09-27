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
