# API changelog

Every change to `docs/api/openapi.yaml` bumps `info.version` and adds an entry here.

## 0.7.0 — 2026-10-02 (B9, additive)

- `ContractProposal.prefill`: answers suggested from the most similar dataset (same workspace,
  column-name Jaccard >= 0.8) with a confirmed contract; never applied until confirmed.
- Turn events: `plan.routed_by` (`rules` | `planner`); a playbook whose requirements are
  missing answers with `plan.blocked` + `plan.recovery` and runs nothing (`answer.blocked`).

## 0.6.0 — 2026-10-01 (B8)

Second domain: **logistics** (`GET /packs` now lists `core, logistics, marketing`; confirm with
`POST /datasets/{id}/domains/confirm {"domains": ["logistics"]}`). Additive:
`DomainDetection.sources[]` (every pack's matched sources, with `domain`); `marketing_sources`
keeps only marketing ones (a logistics source no longer appears there). `Confirmed.provisional`:
approving a comparison template (`sla_breach`, `on_time`, `in_full`, `rto_rate`,
`repeat_attempt` — shape `comparison`) returns what the engine measured about it; results
reading it carry a PROVISIONAL caveat. New tools: `logistics.sla_compliance`,
`logistics.sla_drivers` (`params.focus` optional — without it the engine picks the worst group
and a caveat says which), `logistics.otif`, `logistics.courier_compare`,
`logistics.stuck_shipments` (`params.as_of` required, `params.days` default 3),
`logistics.rto_analysis`. Tool results: frequency steps report the row COUNT as the figure
(was the share); every group row's `n` is now also a figure (`<step>: <group> [n]`).

## 0.5.0 — 2026-09-28 (B7)

Keyword groups are live: `POST /datasets/{id}/keyword-groups/run` (new; body `{column?}`),
`GET /datasets/{id}/keyword-groups`, `POST /datasets/{id}/keyword-groups/actions` (approve |
rename | merge | move_keyword | split; `keywords[]` added for split). `KeywordGroup.facets` is
now `{facet: [values]}`; new `KeywordGroup.proposed_by` (rules | llm | person) and
`KeywordGroups.run` (column, embedding backend, threshold, typos merged, counts). No endpoint
answers 501 any more. Additive except `facets` value type (was never live).

## 0.4.0 — 2026-09-28 (B5)

Turns are answered (when a model is configured): `plan` (playbook + slots, or
`mode: tool_calling`), `tool_call` (`status` ok | skipped + `code`/`reason`), `figure_check`
(passed | failed | corrected | flagged), `interpretation_check` (`rule`, `status`), `answer`
(`text`, `flags`). `Turn.answer` documented: text, flags, playbook, results, skipped, usage
(LLM calls, tool calls, estimated tokens). With no model configured turns still end
`agent_not_wired`. No breaking changes.

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
