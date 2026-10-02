## Status (date, branch, last commit)

2026-10-01 · branch `claude/analytics-agent-v2-backend-6gcu1p` · B7 merged (PR #11).
Done: B0–B7, **B8** (logistics pack: 6 tools, 2 playbooks, the 19 SLA checks reproduced through
the API; API 0.6.0). Open for the user: approve the draft keyword gold (D-B7-3).

2026-10-02, from the frontend session (branch `claude/beautiful-lovelace-07dnwu`, PR #15): **C9**
in `backend/services/sessions.py`. `ui_state_problem` no longer reads an exact server id
(`ds_/ws_` + 12 hex, `t_` + 16 hex) as a phone number; it refused 0.34% of dataset ids. 5 tests
in `test_api_sessions.py`; full suite 2354 passed, 1 skipped. No API shape change. New
api-requests from F3: #16 (answer ingest layout questions), #17 (return the contract in force);
#14 (list a workspace's datasets) is still open.

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
    2349 passed, 1 skipped, 1 warning in 237.94s (0:03:57)
    eval: SCORE: 76/76 (100%) · sla_bench: 19/19 checks right

## Next milestone

None planned after B8. Open: D-B7-3 (keyword gold approval), O-B2-1 (Claude Desktop
list_changed, needs a real desktop).
