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
