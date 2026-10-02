"""F4 helpers: params typed as the runner expects, results shown exactly as returned."""

import pytest

from frontend import prep, state


def test_params_are_typed_and_blanks_are_left_out():
    assert prep.coerce("budget", "5000") == 5000.0
    assert prep.coerce("budget", 5000) == 5000
    assert prep.coerce("period", " 2026-09 ") == "2026-09"
    assert prep.coerce("period", "  ") is None and prep.coerce("budget", None) is None
    with pytest.raises(ValueError):
        prep.coerce("budget", "five thousand")


def test_run_params_send_only_what_the_person_filled():
    params = prep.run_params(["period", "baseline"], {"period": "2026-09", "baseline": ""},
                             {"by": "campaign", "other": None}, {"conversions": "conv_a"}, True)
    assert params == {"period": "2026-09", "by": "campaign",
                      "bindings": {"conversions": "conv_a"}, "dates_confirmed": True}
    assert prep.run_params([], {}, {}, {}, False) == {}


def test_figures_are_shown_exactly_and_null_is_suppressed():
    rows = prep.figure_rows([{"name": "CTR: search", "value": 4.9716, "unit": "total",
                              "provenance": "contract"},
                             {"name": "CPA: tiny", "value": None, "unit": None,
                              "provenance": "derived"},
                             {"name": "Spend", "value": 560.0, "unit": "cost (sum)",
                              "provenance": "contract"}])
    assert [r["value"] for r in rows] == ["4.9716", "suppressed", "560.0"]
    assert rows[1]["unit"] == "" and rows[1]["source"] == "derived"


def test_series_keep_the_backends_order():
    series = {"points": [{"x": "social", "y": 449.0}, {"x": "search", "y": 560.0},
                         {"x": "display", "y": None}]}
    assert prep.series_data(series) == {"x": ["social", "search", "display"],
                                        "y": [449.0, 560.0, None]}


def test_tool_drafts_keep_scalars_and_bindings_only():
    out = state.normalize({"datasets": ["ds_0123456789ab"], "drafts": {"ds_0123456789ab": {
        "tool": "marketing.channel_efficiency",
        "params": {"marketing.budget_pacing": {"budget": 5000.0, "month": "2026-09",
                                               "rows": [[1, 2]], "bindings": {"spend": "cost"}},
                   "marketing.empty": {"rows": [[1]]}},
        "rules": {"exclude_test_campaigns": True, "x": "yes"},
        "bindings": {"roas": {"conv_value": "revenue", "bad": 3}}}}})
    d = out["drafts"]["ds_0123456789ab"]
    assert d["tool"] == "marketing.channel_efficiency"
    assert d["params"] == {"marketing.budget_pacing": {"budget": 5000.0, "month": "2026-09",
                                                       "bindings": {"spend": "cost"}}}
    assert d["rules"] == {"exclude_test_campaigns": True}
    assert d["bindings"] == {"roas": {"conv_value": "revenue"}}
