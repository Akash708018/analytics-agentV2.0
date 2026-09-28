## Status (date, branch, last commit)

2026-09-27 · branch `claude/analytics-agent-v2-backend-6gcu1p` · B2 in PR #3 (open), B3 on top.
Done: B0, B1, B2, **B3** (marketing knowledge + 20 Tier-1 tools; API 0.3.0).

## API: spec version — live | stubbed (501) | changed since last handoff

**0.3.0** (additive; `docs/api/CHANGELOG.md`). Newly live: metrics templates/approve,
validity-rules list/approve, `POST /datasets/{id}/forks`, **`POST /tools/{tool_id}/run`**.
Only keyword groups are still 501 (B7).

## Tools: active tool ids per domain; tool-schema tokens (core vs core+marketing)

marketing (active once marketing is confirmed and the data has the concepts): channel_efficiency,
roas_change_explainer, spend_waste, channel_mix_shift, day_hour_performance, device_geo_split,
creative_fatigue, landing_page_performance, new_vs_returning, festive_compare,
seo_change_explainer, striking_distance, brand_vs_nonbrand, content_decay, email_performance,
list_health, social_post_performance, impression_share_loss, quality_score_vs_cpc,
utm_hygiene. Tokens (estimate): core-only 194; core+marketing (all 20 active) 1750.

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
- Period params are calendar labels at `grain` (default month): `2026-01`.

## Open api-request issues + answers

None (checked 2026-09-26).

## Test tail (pasted)

    cd backend && uv run pytest -q -rs
    SKIPPED [1] tests/test_agent.py:580: v2 B0 (D-B0-2): the v1 Streamlit app ui/app.py is not seeded; v2 screens belong to the frontend
    2287 passed, 1 skipped, 1 warning in 166.11s (0:02:46)
    eval: SCORE: 76/76 (100%)

## Next milestone

B4 — Tier-2 tools on new shared SQL engine analyses (funnel, source_reconciliation, ab_test,
pacing, text_ngrams, key_overlap, expected_rate_by_bucket, before_after_baseline, unit_economics).
