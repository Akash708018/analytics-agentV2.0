"""Results page: the dataset's stored tool results (API 0.7.0), whether each is out of date and
why, the result itself, and every row the engine computed for it, paged.

Nothing is computed here: rows are sorted and filtered by the backend (`inspect`) and shown as
it returns them. The chosen result id is saved; the rest is view state. See docs/steps/F8.md.
"""

import streamlit as st

from frontend import connection, state
from frontend.api_client import APIError
from frontend.components.results import render_result
from frontend.components.shell import needs_dataset, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]
PAGE_SIZES = (25, 50, 100, 500)


def k(dataset_id: str, *parts: str) -> str:
    return ".".join(("ui.results", dataset_id, *parts))


def _page(key: str, delta: int) -> None:
    ss[key] = max(0, ss.get(key, 0) + delta)


def _label(summary: dict) -> str:
    return (f"{summary['tool_id']} · {summary['created_at']} · {summary['status']}"
            + (" · out of date" if summary.get("stale") else ""))


def _arrived_with(dataset_id: str, ids: list[str]) -> None:
    """A result's "Page through every row" link names it in the address: the person's click."""
    wanted = st.query_params.get("result")
    if wanted is None:
        return
    if wanted in ids:                               # draft and widget agree, so bind keeps it
        state.set_path(draft, ("drafts", dataset_id, "result"), wanted)
        ss[k(dataset_id, "chosen")] = wanted
    del st.query_params["result"]


def _rows(dataset_id: str, result_id: str) -> None:
    st.subheader("Every row")
    try:
        first = api.inspect_result(result_id)
    except APIError as error:
        show_error(error, "Could not read this result's rows")
        return
    steps = first["steps"]
    step_key = k(dataset_id, result_id, "step")
    if ss.get(step_key) not in steps:
        ss[step_key] = first["step"]                 # the backend's first step: a view, not a choice
    left, mid, right = st.columns(3)
    step = left.selectbox("Step", steps, key=step_key)
    headers = first["headers"] if step == first["step"] else None
    if headers is None:
        try:
            headers = api.inspect_result(result_id, step=step, limit=1)["headers"]
        except APIError as error:
            show_error(error, "Could not read this step")
            return
    sort = mid.selectbox("Sort by", headers, key=k(dataset_id, result_id, step, "sort"),
                         index=None, placeholder="the engine's order")
    descending = mid.checkbox("Largest first", key=k(dataset_id, result_id, step, "desc"),
                              value=True, disabled=sort is None)
    group = right.text_input("Only rows for", key=k(dataset_id, result_id, step, "group"),
                             placeholder="a group, e.g. a hub", help="Matched by the backend.")
    size = right.selectbox("Rows per page", PAGE_SIZES, key=k(dataset_id, result_id, "size"),
                           index=1)
    offset_key = k(dataset_id, result_id, step, "offset")
    offset = ss.get(offset_key, 0)
    try:
        page = api.inspect_result(result_id, step=step, sort_by=sort,
                                  descending=descending if sort else None,
                                  group=group.strip() or None, limit=size, offset=offset)
    except APIError as error:
        show_error(error, "Could not read these rows")
        return
    rows = page["rows"]
    st.dataframe({h: [row[i] for row in rows] for i, h in enumerate(page["headers"])},
                 hide_index=True, use_container_width=True)
    shown_to = offset + len(rows)
    st.caption(f"Rows {offset + 1 if rows else 0}–{shown_to} of {page['total_rows']}"
               + (f" matching '{group.strip()}'" if group.strip() else "")
               + f" · {page['rows_kept']} kept with the result of the engine's "
               f"{page['rows_in_engine_output']} · values as the engine returned them")
    back, forward = st.columns(2)
    back.button("Previous rows", key=f"results.prev.{result_id}", disabled=offset == 0,
                on_click=_page, args=(offset_key, -size))
    forward.button("Next rows", key=f"results.next.{result_id}",
                   disabled=shown_to >= page["total_rows"], on_click=_page, args=(offset_key, size))


def main() -> None:
    st.title("Results")
    dataset_id = draft["dataset_id"]
    if dataset_id is None:
        needs_dataset(ss)
        return
    try:
        listed = api.list_results(dataset_id)["results"]
    except APIError as error:
        show_error(error, "Could not list this dataset's results")
        return
    if not listed:
        st.info("No stored results yet. Run a tool on Tools, or ask a question on Ask: every "
                "result is kept here with where it came from.")
        return
    by_id = {r["result_id"]: r for r in listed}
    _arrived_with(dataset_id, list(by_id))
    stale = sum(bool(r.get("stale")) for r in listed)
    st.caption(f"{len(listed)} stored result(s) · {stale} out of date. A result goes out of "
               "date when the data, the contract or a metric it read changes.")
    key, path = k(dataset_id, "chosen"), ("drafts", dataset_id, "result")
    state.bind(ss, key, path, None)
    if ss[key] not in by_id:
        ss[key] = None
    st.selectbox("Result", list(by_id), key=key, format_func=lambda i: _label(by_id[i]),
                 placeholder="choose a result…", on_change=state.on_change, args=(ss, key, path))
    result_id = ss[key]
    if result_id is None:
        with st.expander(f"All {len(listed)} result(s)"):
            st.dataframe([{"result": r["result_id"], "tool": r["tool_id"], "run": r["run_id"],
                           "status": r["status"], "created": r["created_at"],
                           "out of date": "; ".join(r.get("stale_reasons") or []) if r.get("stale")
                           else "no"} for r in listed], hide_index=True, use_container_width=True)
        return
    try:
        stored = api.get_result(result_id)
    except APIError as error:
        show_error(error, "Could not read this result")
        return
    render_result(stored, key=f"results.{result_id}", link=False)
    st.caption(f"Stored {stored.get('created_at')} · run {stored.get('run_id')}")
    _rows(dataset_id, result_id)


main()
