# Coordination protocol (backend agent: Claude Code; frontend agent: Codex)

The two agents coordinate only through GitHub: files, branches, PRs, issues. The section below is
identical in both agents' prompts. Shared file: append-only sections, PR required.

## Coordination protocol (identical text is in the Codex prompt)

**Ownership**

| Path | Owner |
|---|---|
| `backend/`, `packs/`, `bench/`, `docs/api/`, `docs/steps/B*` | backend (you) |
| `frontend/`, `docs/steps/F*` | frontend (Codex) |
| `docs/COORDINATION.md`, `docs/decisions.md`, `README.md` | shared; append-only sections, PR required |

**API contract**

- `docs/api/openapi.yaml` is the single source of truth, written **contract-first** by the backend.
- `backend/tests/test_openapi_drift.py` asserts that the generated schema equals the committed file.
- Every change bumps `info.version` and adds an entry to `docs/api/CHANGELOG.md`.
- Codex requests changes through GitHub issues labelled `api-request`.

**Branches and PRs**

- Use `backend/B<n>-<slug>` and `frontend/F<n>-<slug>`, with PRs to `main`.
- The user merges. Never force-push `main`.

**Working directories and handoff**

- Each agent works in its own clone or worktree.
- Keep `docs/handoff/BACKEND.md` current. Read `docs/handoff/FRONTEND.md`.
- Use `gh` for issues and PRs.

## Notes (append-only)

- 2026-09-26, backend: the backend's cloud session is bound to the branch
  `claude/analytics-agent-v2-backend-6gcu1p` and cannot push to `backend/B<n>-<slug>` without the
  user's permission; B0 is on that branch (see D-B0-1 in `docs/decisions.md`). The backend session
  has no `gh` CLI; it uses the GitHub API through its tools for issues and PRs — same effect.
- 2026-10-01, Claude Code: at the user's instruction, Claude Code took over the frontend from
  Codex (see D-F1-5 in `docs/decisions.md`). It now works both sides and follows `AGENTS.md`'s
  frontend rules for `frontend/`. Its pushes go to its session branch (D-F1-6); Codex's F1
  PR #6 is superseded by the PR from that branch. The protocol above stays: the API contract
  is still the backend's `docs/api/openapi.yaml`, and the frontend still talks to it only
  over HTTP.
