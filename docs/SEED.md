# SEED — what B0 copied from v1

Source: https://github.com/Akash708018/analytics-agent.git @ `0ba324b` (v1 final).
Copied by `cp -r`, then imports and paths rewritten only (see `docs/steps/B0.md` §4).

| v1 path | v2 path |
|---|---|
| `src/analytics_agent/` | `backend/engine/` |
| `tests/` | `backend/tests/` |
| `scripts/` | `backend/scripts/` |
| `eval/` | `backend/eval/` |
| `docs/` | `backend/docs/` |
| `uv.lock`, `.python-version` | repo root (lock re-resolved; streamlit tree removed) |

**Not copied:** `ui/` (Streamlit app and its 56 tests), `.streamlit/`, `AGENTS.md`, v1 `CLAUDE.md`,
`docs/` except the build guide and `docs/contracts/` (both read by tests), `README.md`.
`scripts/targeted_regression/` is copied but NOT renamed: it imports `analytics_agent` from
a v1 tree given by `--src`, so it still measures v1 revisions only.

## Files per folder

### `backend/docs/` — 1 files

`analytics_agent_build_guide_v1.2.md`

### `backend/docs/contracts/` — 1 files

`clean_sales.yaml`

### `backend/docs/contracts/prof_extreme/` — 1 files

`extreme.yaml`

### `backend/engine/` — 5 files

`__init__.py`, `config.py`, `server.py`, `state.py`, `workspace.py`

### `backend/engine/analysis/` — 34 files

`__init__.py`, `base.py`, `bivariate.py`, `calendar_coverage.py`, `changepoint.py`, `cohort_retention.py`, `confidence_interval.py`, `correlated_shift.py`, `correlation.py`, `cross_tab.py`, `declared.py`, `distribution.py`, `driver_analysis.py`, `effect_size.py`, `frequency.py`, `group_compare.py`, `growth_decomposition.py`, `hypothesis_test.py`, `inferential.py`, `mix_shift.py`, `outlier_detection.py`, `pareto.py`, `period_compare.py`, `ranking_shift.py`, `registry.py`, `repeat_behaviour.py`, `runs.py`, `sample_adequacy.py`, `seasonality.py`, `stats.py`, `summary_stats.py`, `temporal.py`, `tools.py`, `trend.py`

### `backend/engine/charts/` — 2 files

`__init__.py`, `render.py`

### `backend/engine/clean/` — 7 files

`__init__.py`, `apply.py`, `detect.py`, `ledger.py`, `plan.py`, `sql.py`, `tools.py`

### `backend/engine/contract/` — 13 files

`__init__.py`, `caveat_check.py`, `compatibility.py`, `dataset_contract.py`, `evidence.py`, `llm_filter.py`, `measured_caveats.py`, `propose.py`, `provisional.py`, `refusals.py`, `store.py`, `suggest.py`, `tools.py`

### `backend/engine/ingest/` — 10 files

`__init__.py`, `csv_loader.py`, `draft.py`, `excel.py`, `headers.py`, `merges.py`, `postgres.py`, `preview.py`, `sizegate.py`, `spec.py`

### `backend/engine/profile/` — 6 files

`__init__.py`, `column_profile.py`, `render.py`, `runs.py`, `table_profile.py`, `tools.py`

### `backend/engine/report/` — 3 files

`__init__.py`, `assemble.py`, `tools.py`

### `backend/engine/util/` — 5 files

`__init__.py`, `db.py`, `formatting.py`, `results.py`, `sql_guard.py`

### `backend/engine/validate/` — 5 files

`__init__.py`, `report.py`, `rules.py`, `runs.py`, `tools.py`

### `backend/engine/webapp/` — 8 files

`__init__.py`, `agent.py`, `autofill.py`, `budget.py`, `contract.py`, `llm.py`, `real_backend.py`, `verify.py`

### `backend/eval/` — 2 files

`gold_questions.yaml`, `run_eval.py`

### `backend/scripts/` — 14 files

`agent_live.py`, `autofill_report.py`, `benchmark.py`, `browser_check.py`, `final_summary.py`, `marketing_bench.py`, `marketing_bench_v2.py`, `request_size.py`, `scenario_matrix.py`, `sla_bench.py`, `sla_live.py`, `stress_matrix.py`, `suggest_bench.py`, `sweep_workspaces.py`

### `backend/scripts/domain_benchmark/` — 11 files

`compare.py`, `config.py`, `domains.py`, `fixtures.py`, `matrix.py`, `oracle.py`, `report.py`, `run.py`, `summary.py`, `verify.py`, `worker.py`

### `backend/scripts/llm_benchmark/` — 4 files

`questions.py`, `report.py`, `research.py`, `run.py`

### `backend/scripts/targeted_regression/` — 4 files

`cases.py`, `run.py`, `runner.py`, `summary.py`

### `backend/tests/` — 123 files

`bench_merges.py`, `conftest.py`, `test_agent.py`, `test_analysis_options.py`, `test_analysis_runs.py`, `test_analysis_tool_docs.py`, `test_analysis_tools.py`, `test_autofill.py`, `test_benchmark_fix_facts.py`, `test_benchmark_fixes.py`, `test_bivariate.py`, `test_broken_sales_ground_truth.py`, `test_budget.py`, `test_calendar_coverage.py`, `test_caveat_check.py`, `test_changepoint.py`, `test_charts_render.py`, `test_cleaning_apply.py`, `test_cleaning_detect.py`, `test_cleaning_ledger.py`, `test_cleaning_plan.py`, `test_cleaning_sql.py`, `test_cohort_facts.py`, `test_cohort_retention.py`, `test_column_profile.py`, `test_compatibility.py`, `test_confidence_interval.py`, `test_contract_declarations.py`, `test_contract_model.py`, `test_contract_store.py`, `test_contract_tool_docs.py`, `test_contract_tools.py`, `test_correlated_shift.py`, `test_correlation.py`, `test_cross_tab.py`, `test_date_prescreen_facts.py`, `test_declaration_surface.py`, `test_declared.py`, `test_distribution.py`, `test_domain_benchmark.py`, `test_domain_benchmark_defects.py`, `test_domain_notes.py`, `test_draft.py`, `test_driver_analysis.py`, `test_duckdb_cleaning_facts.py`, `test_duckdb_temporal_facts.py`, `test_duckdb_validation_facts.py`, `test_duplicate_facts.py`, `test_edge_facts.py`, `test_effect_size.py`, `test_evidence.py`, `test_frequency.py`, `test_group_compare.py`, `test_growth_decomposition.py`, `test_headers.py`, `test_hypothesis_test.py`, `test_ingest_spec_dtype.py`, `test_llm_filter.py`, `test_loaders_step5.py`, `test_marketing_bench.py`, `test_marketing_bench_v2.py`, `test_matplotlib_facts.py`, `test_measure_model.py`, `test_measure_model_facts.py`, `test_measured_caveats.py`, `test_merges.py`, `test_mix_shift.py`, `test_mixed_types_ground_truth.py`, `test_nan_and_kruskal_facts.py`, `test_narrowed_load.py`, `test_outlier_detection.py`, `test_pareto.py`, `test_period_compare.py`, `test_phase10.py`, `test_phase11.py`, `test_phase12.py`, `test_phase2.py`, `test_phase3.py`, `test_phase4.py`, `test_phase5.py`, `test_phase6.py`, `test_phase7.py`, `test_phase8.py`, `test_phase9.py`, `test_preview.py`, `test_profile_runs.py`, `test_profile_state.py`, `test_profile_tool_docs.py`, `test_profile_tools.py`, `test_propose.py`, `test_provisional.py`, `test_ranking_shift.py`, `test_real_backend.py`, `test_render.py`, `test_repeat_behaviour.py`, `test_report_assemble.py`, `test_report_tools.py`, `test_results.py`, `test_sample_adequacy.py`, `test_scenarios.py`, `test_scope.py`, `test_seasonality.py`, `test_selector_facts.py`, `test_sizegate_units.py`, `test_spec.py`, `test_sql_guard.py`, `test_state.py`, `test_state_gate.py`, `test_stats_facts.py`, `test_stress_fixes.py`, `test_suggest.py`, `test_suggest_bench.py`, `test_summary_stats.py`, `test_table_profile.py`, `test_tool_docs.py`, `test_track_b_facts.py`, `test_trend.py`, `test_validate_expectations.py`, `test_validate_report.py`, `test_validate_rules.py`, `test_validate_runs.py`, `test_validate_tools.py`, `test_verify.py`

### `backend/tests/fixtures/` — 11 files

`broken_sales.csv`, `clean_sales.csv`, `clean_sales.xlsx`, `gaps_and_dupes.csv`, `logistics_sla.csv`, `make_fixtures.py`, `merged_multiheader.xlsx`, `messy_headers.xlsx`, `mixed_types.xlsx`, `multiheader.csv`, `region_lookup.csv`

**Total files copied: 270.**

Cross-check against v1's tracked files:

    git -C /tmp/av1 ls-files src tests scripts eval docs/contracts \
        docs/analytics_agent_build_guide_v1.2.md | wc -l
    270
