## Status (date, branch, last commit)

2026-10-02 · working on `main` (user's instruction; other branches merged, deletion left to
the user — the proxy refuses branch deletes). Done: B0–B10. **B10** = the frontend's
api-request issues #16, #17, #20, #21, #23 (closed). API **0.8.0** (additive). Open: #22
(direct core analyses + report) needs the user's product decision; D-B7-3 keyword gold.

## API: spec version — live | stubbed (501) | changed since last handoff

**0.4.0** (additive; `docs/api/CHANGELOG.md`). Turns are now answered by the playbook agent
when a model is configured (env keys as in v1); events: plan, tool_call, figure_check,
interpretation_check, answer. Earlier: metrics, validity rules, forks, tool run (0.3.0).
0.5.0: keyword groups live (run, list, actions). No endpoint answers 501.
**0.6.0** (B8, additive): logistics domain; `DomainDetection.sources[]`; `Confirmed.provisional`
for comparison metrics; frequency figures are counts; group `n` figures.

## Tools: active tool ids per domain; tool-schema tokens (core vs core+marketing)

marketing (active once marketing is confirmed and the data has the concepts): channel_efficiency,
roas_change_explainer, spend_waste, channel_mix_shift, day_hour_performance, device_geo_split,
creative_fatigue, landing_page_performance, new_vs_returning, festive_compare,
seo_change_explainer, striking_distance, brand_vs_nonbrand, content_decay, email_performance,
list_health, social_post_performance, impression_share_loss, quality_score_vs_cpc,
utm_hygiene; Tier-2: funnel, conversion_reconciliation, ab_test_readout, budget_pacing,
keyword_ngrams, cannibalization, ctr_vs_position, campaign_impact, delivered_roas, cac_payback,
ltv_to_cac.
logistics (B8): sla_compliance, sla_drivers, otif, courier_compare, stuck_shipments,
rto_analysis; playbooks sla_where_and_why, courier_scorecard. Tokens (estimate): core-only 194; core+marketing (all 31 active) 1886.

## For Codex: what to build against now; breaking changes; mock notes

- Metrics screen: `GET .../metrics/templates`; approve with `{template_id, bindings: {},
  fork_choices}`. `available: false` → show why (missing concepts or engine not ready).
- Validity rules: list with `rows_affected`; the person ticks; `POST .../approve`.
- Tool run: `POST /tools/{id}/run {dataset_id, params}`. Handle 409 `needs_domain`/`needs_data`
  and 422 by `error.code`: `forks_unanswered` (show `forks[]` questions → `POST .../forks`),
  `ambiguous_binding` (`columns[]` → let the person pick → re-run with
  `params.bindings.{concept}`), `param_required` (`missing`), `festival_dates_unconfirmed`
  (`dates` → confirm → `dates_confirmed: true`). Plot `series[].points` as given; show
  `caveats` and `validity_filters_applied` with every result; `value: null` = suppressed.
- Turn screen: render `plan` (playbook + steps), each `tool_call` (ok/skipped + reason),
  then `answer.text`; show `answer.flags` under it (unresolved checks); link
  `answer.results[]` (ToolResults) as the evidence; `usage` for a cost line.
- Keyword groups screen: `POST .../keyword-groups/run`, list with `approved`, `intent`,
  `facets`, `proposed_by`; actions approve / rename / merge / move_keyword / split. Only
  approved groups feed `marketing.keyword_group_performance` and `marketing.page_targeting`.
- Logistics: approve metrics `sla_breach` / `rto_rate` (comparison, 0/1 per row) on the
  metrics screen — show the `provisional` text; results say PROVISIONAL. `sla_drivers` takes an
  optional `focus` (a hub/courier/zone value); without it a caveat starting `Focus:` says which
  group the engine chose. `stuck_shipments` needs `as_of` (date) and takes `days`.
- Period params are calendar labels at `grain` (default month): `2026-01`.
- B4 figures: every numeric cell is a figure; extra columns are named `step: row [column]`.
  Tier-2 inputs the person types: budget + month (pacing), spend per cohort month (CAC),
  funnel steps, sources + reference (reconciliation), start date (campaign impact).

## Open api-request issues + answers

None (checked 2026-09-26).

## Test tail (pasted)

    cd backend && uv run pytest -q -rs
    SKIPPED [1] tests/test_agent.py:580: v2 B0 (D-B0-2): the v1 Streamlit app ui/app.py is not seeded; v2 screens belong to the frontend
    2384 passed, 1 skipped, 1 warning in 409.46s (0:06:49)
    eval: SCORE: 76/76 (100%) · sla_bench: 19/19 checks right · frontend: 267 passed

2026-10-02, F8 on main (no backend change): the frontend consumes 0.7.0 (results, lineage,
inspect, routing/blocked plans, joins, prefill, value errors). api-request
[#23](https://github.com/Akash708018/analytics-agentV2.0/issues/23): `logistics.sla_drivers`
reads `focus` but its spec doesn't declare it; the frontend reads `filter[].focus_param` until
it does.

## B10 for the frontend (0.8.0, additive)

- Upload: on 422 `ingest_needs_answers` show `preview.rows` and `preview.guess`; send the
  answers to `POST /workspaces/{ws}/uploads/{upload_id}/answers` (client `answer_upload`).
- Contract: `GET /datasets/{id}/contract` (client `get_contract`) is the record of what was
  confirmed; the `ui_state.drafts` stopgap can go.
- Cleaning: show `samples`, `values_lost` + `loss_unit` on lossy steps; `sql` in an expander.
- Keyword groups: `unapprove` action. Tools: read `params_optional` instead of step filters.

## B9 for the frontend (0.7.0, additive — `docs/api/CHANGELOG.md`)

- Contract screen: show `prefill` (from which file, similarity) as a one-click suggestion;
  the person still confirms with the ordinary call.
- Every tool result has `result_id`, `status`, `snapshot`, `contract_version`; a results list
  per dataset with `stale` + `stale_reasons` (show "out of date: the data changed").
  `GET /results/{id}/inspect` pages through all rows of a step (sort, group filter).
- Turn plan: `routed_by` (rules | planner); a blocked playbook answers with its `recovery`.
- Keyword groups: `generation`, `joins` (a proposal to join an approved group — accept by
  merge with the approved group first); `run.carry_forward`; run body takes `backend`.
- New 422 codes: `ambiguous_value` (`candidates`), `unknown_value` (`values`).
- Client methods already added: `list_results`, `get_result`, `inspect_result`.

## Next milestone

None planned after B8. Open: D-B7-3 (keyword gold approval), O-B2-1 (Claude Desktop
list_changed, needs a real desktop).
