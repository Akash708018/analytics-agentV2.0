"""The domain x analysis compatibility matrix, built from the LIVE registry.

Every registered analysis gets a class per domain: REQUIRED (named in the domain's brief), VALID,
EDGE_CASE (defined but stretched: one row per entity for a retention grid) or NOT_APPLICABLE (a
deliberate wrong attempt, expected to be refused or answered trivially). An analysis added to
the registry after this file was written has no rule here: it is run with the web menu's own
defaults and reported as UNCLASSIFIED, so nothing escapes the benchmark.
"""

from __future__ import annotations

import datetime as dt

from .domains import Domain


def _periods(domain: Domain) -> tuple[str, str]:
    end = dt.date.fromisoformat(domain.window[1])
    prev = (end.replace(day=1) - dt.timedelta(days=1))
    return f"{end.year:04d}-{end.month:02d}", f"{prev.year:04d}-{prev.month:02d}"


def _halves(domain: Domain) -> dict:
    a, b = (dt.date.fromisoformat(x) for x in domain.window)
    mid = a + (b - a) / 2
    return dict(before_start=a.isoformat(), before_end=mid.isoformat(),
                after_start=(mid + dt.timedelta(days=1)).isoformat(), after_end=b.isoformat())


def cases(domain: Domain, registered: list[str]) -> list[dict]:
    r = domain.roles
    d0, d1 = r["dims_low"][0], r["dims_low"][1]
    ms, mm, b = r["m_sum"], r["m_mean"], r["binary"]
    period, baseline = _periods(domain)
    per = dict(period=period, baseline=baseline, grain="month")
    plan: dict[str, list[tuple[str, dict]]] = {
        "summary_stats": [("", {})],
        "frequency": [("", {"column": d0}), ("hi", {"column": r["dim_hi"]})],
        "top_n": [("", {"dimension": d0, "measure": ms}),
                  ("mean", {"dimension": d1, "measure": mm})],
        "group_compare": [("", {"dimension": d0, "measure": ms}),
                          ("mean", {"dimension": d1, "measure": mm})],
        "cross_tab": [("count", {"rows": d0, "columns": d1}),
                      ("sum", {"rows": d0, "columns": d1, "measure": ms})],
        "pareto": [("", {"dimension": r["dim_mid"], "measure": ms})],
        "concentration": [("", {"dimension": r["dim_mid"], "measure": ms})],
        "distribution": [("", {"measure": ms})],
        "ranking_shift": [("", {"dimension": d0, "measure": ms, **_halves(domain)})],
        "calendar_coverage": [("", {"grain": "month"})],
        "trend": [("", {"measure": ms, "grain": "month"}),
                  ("mean", {"measure": mm, "grain": "month"})],
        "seasonality": [("", {"measure": ms, "grain": "month"})],
        "period_compare": [("", {"measure": ms, **per})],
        "growth_decomposition": [("", {"measure": ms, "dimension": d0, **per})],
        "correlation": [("", {"measure": r["m_x"], "against": r["m_y"]}),
                        ("indep", {"measure": ms, "against": r["m_indep"]})],
        "bivariate": [("", {"measure": r["m_x"], "against": r["m_y"]})],
        "driver_analysis": [("", {"measure": ms})],
        "mix_shift": [("", {"measure": ms, "dimension": d0, **per})],
        "outlier_detection": [("", {"measure": ms}), ("mean", {"measure": mm})],
        "changepoint": [("", {"measure": ms, "grain": "month"})],
        "correlated_shift": [("", {"measure": r["m_x"], "against": r["m_y"], "grain": "month"})],
        "hypothesis_test": [("t", {"dimension": b, "measure": mm}),
                            ("anova", {"dimension": d0, "measure": mm}),
                            ("chi2", {"dimension": b, "second_dimension": d0}),
                            ("rank", {"dimension": b, "measure": mm, "method": "rank"})],
        "confidence_interval": [("all", {"measure": mm}),
                                ("by", {"dimension": b, "measure": mm})],
        "effect_size": [("g", {"dimension": b, "measure": mm}),
                        ("anova", {"dimension": d0, "measure": mm}),
                        ("v", {"dimension": b, "second_dimension": d0})],
        "sample_adequacy": [("", {"dimension": b, "measure": mm})],
        "repeat_behaviour": [("", {"entity": r["entity"]})],
        "cohort_retention": [("", {"entity": r["entity"]})],
    }
    out = []
    for name in registered:
        if name not in plan:
            out.append({"analysis": name, "variant": "", "class": "UNCLASSIFIED", "params": None,
                        "reason": "registered after the matrix was written: run with the menu's "
                                  "defaults"})
            continue
        klass = ("REQUIRED" if name in domain.required else
                 "EDGE_CASE" if name in domain.edge else "VALID")
        for variant, params in plan[name]:
            out.append({"analysis": name, "variant": variant, "class": klass, "params": params,
                        "reason": ""})
    for k, na in enumerate(domain.not_applicable):
        out.append({"analysis": na["analysis"], "variant": f"na{k}", "class": "NOT_APPLICABLE",
                    "params": na["params"], "reason": na["why"], "expect": na["expect"]})
    return out


def chart_plan(domain: Domain) -> list[tuple[str, str, dict]]:
    """(analysis, chart, params) for each chart-producing analysis, including the edge
    requests of section 21: a missing y, a column that does not exist, a categorical y, a date y,
    a high-cardinality x, and a one-value column."""
    r = domain.roles
    d0, ms = r["dims_low"][0], r["m_sum"]
    period, baseline = _periods(domain)
    return [
        ("trend", "line", {"measure": ms, "grain": "month"}),
        ("top_n", "bar", {"dimension": d0, "measure": ms}),
        ("distribution", "histogram", {"measure": ms, "y": "rows"}),
        ("cross_tab", "heatmap", {"rows": d0, "columns": r["dims_low"][1], "measure": ms}),
        ("correlation", "scatter", {"measure": r["m_x"], "against": r["m_y"]}),
        ("group_compare", "box", {"dimension": d0, "measure": ms}),
        ("mix_shift", "waterfall", {"measure": ms, "dimension": d0, "period": period,
                                    "baseline": baseline, "grain": "month", "y": "contribution"}),
        ("cohort_retention", "heatmap", {"entity": r["entity"]}),
        # edges
        ("group_compare", "bar", {"dimension": d0, "measure": ms}),                 # no y
        ("trend", "line", {"measure": ms, "grain": "month", "y": "no_such_col"}),
        ("frequency", "bar", {"column": d0, "y": "value"}),                         # categorical y
        ("trend", "line", {"measure": ms, "grain": "month", "y": "period"}),        # date y
        ("top_n", "bar", {"dimension": r["dim_hi"], "measure": ms, "n": 1000}),     # high card
        ("frequency", "bar", {"column": "_constant_", "y": "rows"}),                # one value
        ("trend", "pie", {"measure": ms, "grain": "month"}),                        # unsupported
    ]
