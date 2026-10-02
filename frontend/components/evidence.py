"""How an answer was reached: the turn's plan, every tool call (run or skipped, and why),
the figure and interpretation checks, unresolved flags, the tool results the answer rests
on, and what it cost. All as the backend recorded them (TurnEvent, Turn.answer).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import streamlit as st

from frontend.components.results import render_result

CHECK_ICON = {"passed": "✅", "corrected": "🛠️", "flagged": "⚠️", "failed": "❌", "not_run": "➖"}


def _plan(data: Mapping[str, Any]) -> str:
    if data.get("mode") == "tool_calling" or not data.get("playbook"):
        return "No playbook fitted: the model chose tools itself, from the active ones."
    slots = ", ".join(f"{k} = {v}" for k, v in (data.get("slots") or {}).items())
    return (f"Playbook **{data['playbook']}**" + (f" ({slots})" if slots else "")
            + ": " + " → ".join(f"`{s}`" for s in data.get("steps", [])))


def _event(event: Mapping[str, Any]) -> str | None:
    kind, data = event["type"], event.get("data", {})
    if kind == "plan":
        return _plan(data)
    if kind == "tool_call":
        params = ", ".join(f"{k}={v}" for k, v in (data.get("params") or {}).items())
        call = f"`{data.get('tool_id')}`" + (f" ({params})" if params else "")
        if data.get("status") == "skipped":
            return f"⏭️ {call} skipped · {data.get('code')}: {data.get('reason')}"
        return f"▶️ {call} ran" + (f" · {data['ms']} ms" if data.get("ms") is not None else "")
    if kind == "figure_check":
        corrected = data.get("corrected") or []
        return (f"{CHECK_ICON.get(data.get('status'), '•')} Figure check {data.get('status')}"
                + (f" · {data['detail']}" if data.get("detail") else "")
                + (f" · corrected: {', '.join(map(str, corrected))}" if corrected else ""))
    if kind == "interpretation_check":
        return (f"{CHECK_ICON.get(data.get('status'), '•')} Interpretation rules "
                f"({data.get('rule')}) {data.get('status')}"
                + (f" · {data['detail']}" if data.get("detail") else ""))
    if kind == "provider_wait":
        return f"⏳ Waited {data.get('seconds')} s for {data.get('provider')}"
    if kind == "failover":
        return f"🔀 Switched from {data.get('from')} to {data.get('to')}: {data.get('reason')}"
    if kind == "error":
        return f"❌ {data.get('code')}: {data.get('message')}"
    return None                                      # `answer` is shown as the answer itself


def render_steps(events: list[Mapping[str, Any]]) -> None:
    for event in events:
        line = _event(event)
        if line:
            st.markdown(line)


def usage_line(usage: Mapping[str, Any]) -> str:
    return (f"{usage.get('llm_calls', 0)} model call(s) · {usage.get('tool_calls', 0)} tool "
            f"call(s) · about {usage.get('tokens_in_est', 0)} tokens in, "
            f"{usage.get('tokens_out_est', 0)} out (estimates)"
            + (f" · model {usage['model']}" if usage.get("model") else ""))


def render_answer(turn: Mapping[str, Any]) -> None:
    answer = turn.get("answer") or {}
    st.markdown(answer.get("text", ""))
    for flag in answer.get("flags", []):
        st.warning(f"Unresolved check: {flag}")
    results = answer.get("results", [])
    with st.expander("How this was answered", expanded=False):
        render_steps(turn.get("events", []))
        if answer.get("usage"):
            st.caption(usage_line(answer["usage"]))
    for i, result in enumerate(results):
        with st.expander(f"Evidence: {result['summary']}", expanded=False):
            render_result(result, key=f"ask.{turn.get('turn_id')}.{i}")
    if not results:
        st.caption("No tool result backs this answer"
                   + (": every step was skipped (see How this was answered)." if answer.get("skipped") else "."))
