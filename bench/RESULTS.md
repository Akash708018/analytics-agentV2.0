# v1 vs v2 — bench results (2026-09-28)

`cd backend && uv run python ../bench/compare.py` → `bench/results/2026-09-28.json`.
Both agents run against a **scripted** model (no network): the bench measures machinery — calls,
tokens sent, checks — not a model's wording. Tokens = characters / 4 of each request, rounded
up (v1's own estimator), tool schemas and system prompts included. v1 = analytics-agent @
`0ba324b`, run from its own checkout.

## v1's numbers first
| source | figure |
|---|---|
| v1 checkout, `scripts/request_size.py` (retail-like turn, 6 scripted tool calls) | 7 requests: 2,890 / 3,144 / 4,566 / 4,973 / 5,583 / 6,151 / 5,974 = **33,281 tokens** |
| v1 recorded, `docs/steps/step2_free_model_reliability.md:165-171` | 2,488 … 5,976 (before v1's step 3 grew its schemas; superseded by the row above) |
| v1 recorded, `docs/steps/marketing_bench.md:49` | strong 28: 28 right, 0 wrong; coverage 80.0% |
| v1 recorded, `docs/steps/step3_provisional_metrics.md:95` | SLA 19/19 checks right |

## A. Same question, same table (retail-like, 30,000 rows)
"For 2025 only, give revenue, cost and margin % by region, and list the data-quality issues."

| | LLM calls | tool calls | tokens sent (all requests) | largest request | checks | wall |
|---|---|---|---|---|---|---|
| v1 | 7 | 6 | 33,359 (own tree: 33,281) | 6,169 | 3/3 | 7.2 s |
| **v2** | **2** | **2** | **3,871** | 3,549 | **3/3** | 7.0 s |

Checks: answered; every request under Groq's 8,000 TPM; revenue by region 2025 equals the
file (4/4, computed independently with Python's csv module). v2: no domain applies, so no
planner call; one tool-choice call batching two `core_analyze` runs; one explainer call. The
data-quality issues reach v2's explainer as the contract's caveats on each result. **−71% LLM
calls, −88% tokens.**

## B. Marketing
| bench | v1 vs v2 |
|---|---|
| `suggest_bench.py` (51 lines) | identical |
| `marketing_bench.py` (72 lines) | same lines, different order (per-group print order) |
| `marketing_bench_v2.py` (69 lines) | same values; one dict prints its keys in another order |

Marketing **questions** on the trap files — v1 has no marketing tools or playbooks (n/a); v2:

| question | checks | LLM calls | tool calls | tokens sent | wall |
|---|---|---|---|---|---|
| why_roas_dropped | pass | 2 | 3 | 2,348 | 3.1 s |
| where_is_spend_wasted | pass | 2 | 3 | 2,156 | 2.9 s |
| did_the_campaign_work (1 correction) | pass | 3 | 2 | 1,155 | 0.7 s |
| funnel_leak | pass | 2 | 3 | 569 | 0.7 s |
| email_health | pass | 2 | 2 | 1,051 | 2.4 s |
| why_organic_traffic_dropped | pass | 2 | 4 | 1,987 | 1.5 s |

For scale: v1's generic loop spends one LLM call per tool call plus one, each request
re-sending every earlier reply (A: 7 calls, 33k tokens for 6 tools).

## C. SLA regression
`sla_bench.py`: **19/19**, output identical to v1's.

## D. Interpretation violations caught (the 9 rule fixtures)
| | caught | named the rule |
|---|---|---|
| v1 figure check | 6 of 9 | 0 — it flags the fixtures' numbers as untraced, not the error |
| **v2 filter** | **9 of 9** | **9** |

## Verdict
Marketing: equal on every v1 check (identical outputs), and v2 answers marketing questions v1
cannot. Calls and tokens: fewer than v1 everywhere measured — 2 LLM calls vs 7, 3.9k vs 33k
tokens on the shared question. Caveat: a scripted model measures the machinery; a live model's
wording and tool choices are not measured here (keys needed; B6 is offline by design).

## B9 re-run (2026-10-02) — `bench/results/2026-10-02.json`
Rule routing (Future Concepts rank 10) skips the planner when a question names one playbook
unambiguously. Same scripted model, same checks:

| question | LLM calls 09-28 → 10-02 | tokens sent | checks |
|---|---|---|---|
| why_roas_dropped | 2 → 2 (months not pinned in the question: planner runs) | 2,348 → 2,432 | pass |
| where_is_spend_wasted | 2 → **1** | 2,156 → 1,963 | pass |
| did_the_campaign_work | 3 → 3 (needs a launch date: planner runs) | 1,155 → 1,169 | pass |
| funnel_leak | 2 → **1** | 569 → 418 | pass |
| email_health | 2 → **1** | 1,051 → 992 | pass |
| why_organic_traffic_dropped | 2 → 2 | 1,987 → 2,016 | pass |

Small token rises: each result's heading now carries its `result_id` (for inspection).
Retail (A), SLA (C: 19/19, identical to v1) and interpretation (D: 9/9) unchanged.
