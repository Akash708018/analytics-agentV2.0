# API changelog

Every change to `docs/api/openapi.yaml` bumps `info.version` and adds an entry here.

## 0.3.0 — 2026-09-27 (B3)

Now live: `GET /datasets/{id}/metrics/templates` (+ `approved`, `measure`),
`POST /datasets/{id}/metrics/approve` (adds a ratio-of-sums measure to the contract as a new
version; returns `Confirmed.measure`; 422 `engine_not_ready`, `variant_unsupported`,
`forks_unanswered`, `ambiguous_binding`, `needs_data`), `GET /datasets/{id}/validity-rules`,
`POST /datasets/{id}/validity-rules/approve`, **`POST /tools/{tool_id}/run`** (ToolResult with
figures + provenance, chart-ready series, validity filters and pack rules applied, forks,
caveats; 409 `needs_domain` / `needs_data`; 422 `forks_unanswered`, `ambiguous_binding`,
`param_required`, `festival_dates_unconfirmed`, `engine_refused`). New:
`POST /datasets/{id}/forks` answers forks after the contract (a domain confirmed later brings
its forks). Contract confirm now returns the stored contract version. Still 501: keyword
groups (B7). No breaking changes.

## 0.2.0 — 2026-09-26 (B2)

Now live: `POST /workspaces/{ws}/uploads` (multipart; 201 Dataset, 413/422 with
`error.code` upload_refused | ingest_needs_answers + `questions`), new `GET /datasets/{id}`,
profile, cleaning proposals/approve, domains detect/confirm, contract proposal/confirm, tools,
packs. Additive fields: `Dataset.assumptions`; `CleaningProposal.lossy`, `.suggested`;
`ContractProposal.provisional`, `.questions`; `MeasureProposal.suggested_agg`, `.strength`,
`.reason`. Contract confirm answers 422 `forks_unanswered` (`missing`, `invalid`) or
`contract_provisional` (`provisional`, `questions`). Still 501: metrics, validity rules,
`POST /tools/{id}/run` (B3), keyword groups (B7). No breaking changes.

## 0.1.0 — 2026-09-26 (B1)

First contract. Live: `/health`, `/version`, sessions (`POST /sessions`, `GET/DELETE
/sessions/{sid}`, `PUT /sessions/{sid}/ui-state` with 409-on-stale-version), turns
(`POST /turns` → 202, `GET /turns/{id}`, `GET /sessions/{sid}/turns`).
Spec'd, answering 501 `{"error": {"code": "not_implemented", "milestone": ...}}`: uploads,
profile, cleaning, domains, contract, metrics, validity rules, tools, packs (B2/B3), keyword
groups (B7). Every request/response model carries an example — mock from those.
