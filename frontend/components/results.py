"""A ToolResult as the backend returned it: every figure with its source, every chart as
the backend computed it (AGENTS.md: the frontend never computes or reformats figures).

Reused by the Ask page for an answer's evidence (F5).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import streamlit as st

from frontend import prep, state


def _chart(series: Mapping[str, Any]) -> None:
    data = prep.series_data(series)
    kind = series["chart"]
    st.markdown(f"**{series['name']}**")
    if kind == "bar":
        st.bar_chart(data, x="x", y="y", x_label=series["x_label"], y_label=series["y_label"],
                     sort=False)          # the backend's order is the ranking
    elif kind == "line":
        st.line_chart(data, x="x", y="y", x_label=series["x_label"], y_label=series["y_label"])
    elif kind == "scatter":
        st.scatter_chart(data, x="x", y="y", x_label=series["x_label"], y_label=series["y_label"])
    if kind not in ("bar", "line", "scatter") or any(y is None for y in data["y"]):
        # table, funnel, stacked_bar (two columns only), or points with suppressed values
        st.dataframe({series["x_label"]: [str(x) for x in data["x"]],
                      series["y_label"]: [prep.shown(y) for y in data["y"]]},
                     hide_index=True, use_container_width=True)


def render_result(result: Mapping[str, Any], key: str, link: bool = True) -> None:
    """`key` tells apart two results on one page (an answer may rest on the same tool twice)."""
    st.markdown(f"#### {result['summary']}")
    st.caption(f"{result['tool_id']} · {result['dataset_id']}")
    if result.get("stale"):
        st.warning("Out of date: " + "; ".join(result.get("stale_reasons") or ["no reason given"])
                   + ". Run it again for current figures.")
    if (status := result.get("status")) in prep.STATUS_NOTES:
        st.warning(prep.STATUS_NOTES[status])
    if (line := prep.lineage(result)):
        st.caption(line)
    if link and result.get("result_id") and st.session_state.get(state.SID):
        st.page_link("views/results.py", label="Page through every row on Results", icon="🗂️",
                     query_params={"sid": st.session_state[state.SID],
                                   "result": result["result_id"]})
    for series in result.get("series", []):
        _chart(series)
    figures = result.get("figures", [])
    if figures:
        with st.expander(f"Every figure ({len(figures)}), with its source", expanded=not result.get("series")):
            st.dataframe(prep.figure_rows(figures), hide_index=True, use_container_width=True)
            st.download_button("Download every figure (CSV)", prep.figures_csv(figures),
                               file_name=f"{result['tool_id']}-figures.csv", mime="text/csv",
                               key=f"{key}.csv", on_click="ignore")
            st.caption(" · ".join(f"{k}: {v}" for k, v in prep.PROVENANCE.items())
                       + " · suppressed: a group too small to show")
    caveats = result.get("caveats", [])
    if caveats:
        st.markdown("**Caveats**")
        for caveat in caveats:
            st.caption(f"• {caveat}")
    applied = result.get("validity_filters_applied", [])
    st.caption("Validity rules applied: " + (", ".join(applied) or "none approved"))
    if result.get("pack_rules_applied"):
        st.caption("Pack rules checked: " + ", ".join(result["pack_rules_applied"]))
    if result.get("forks"):
        st.caption("Your answers used: " + ", ".join(f"{k} = {v}" for k, v in result["forks"].items()))
    check = result.get("figure_check") or {}
    if check:
        st.caption(f"Figure check: {check.get('status')}"
                   + (f" ({'; '.join(check['notes'])})" if check.get("notes") else ""))
