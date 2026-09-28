## Status (date, branch, last commit)

2026-09-27 · branch `claude/analytics-agent-v2-backend-6gcu1p` · B2 in PR #3 (open), B3 on top.
Done: B0–B4, **B5** (interpretation filter, 6 playbooks, turns answered; API 0.4.0).

## API: spec version — live | stubbed (501) | changed since last handoff

**0.4.0** (additive; `docs/api/CHANGELOG.md`). Turns are now answered by the playbook agent
when a model is configured (env keys as in v1); events: plan, tool_call, figure_check,
interpretation_check, answer. Earlier: metrics, validity rules, forks, tool run (0.3.0).
Only keyword groups are still 501 (B7).

## Tools: active tool ids per domain; tool-schema tokens (core vs core+marketing)

marketing (active once marketing is confirmed and the data has the concepts): channel_efficiency,
roas_change_explainer, spend_waste, channel_mix_shift, day_hour_performance, device_geo_split,
creative_fatigue, landing_page_performance, new_vs_returning, festive_compare,
seo_change_explainer, striking_distance, brand_vs_nonbrand, content_decay, email_performance,
list_health, social_post_performance, impression_share_loss, quality_score_vs_cpc,
utm_hygiene; Tier-2: funnel, conversion_reconciliation, ab_test_readout, budget_pacing,
keyword_ngrams, cannibalization, ctr_vs_position, campaign_impact, delivered_roas, cac_payback,
ltv_to_cac. Tokens (estimate): core-only 194; core+marketing (all 31 active) 1886.

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
- Period params are calendar labels at `grain` (default month): `2026-01`.
- B4 figures: every numeric cell is a figure; extra columns are named `step: row [column]`.
  Tier-2 inputs the person types: budget + month (pacing), spend per cohort month (CAC),
  funnel steps, sources + reference (reconciliation), start date (campaign impact).

## Open api-request issues + answers

None (checked 2026-09-26).

## Test tail (pasted)

    cd backend && uv run pytest -q -rs
    SKIPPED [1] tests/test_agent.py:580: v2 B0 (D-B0-2): the v1 Streamlit app ui/app.py is not seeded; v2 screens belong to the frontend
    2327 passed, 1 skipped, 1 warning in 222.84s (0:03:42)
    eval: SCORE: 76/76 (100%)

## Next milestone

B6 — v1-vs-v2 bench (checks, LLM calls, tool calls, tokens incl. schema tokens, violations,
wall time) on v1's marketing benches, the trap files, retail and SLA.
