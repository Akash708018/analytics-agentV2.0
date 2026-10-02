"""Screens around the pages: landing, expired, unreachable, navigation, save status."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import streamlit as st

from frontend import connection, state
from frontend.api_client import APIError

TAGLINE = "Understand your digital marketing data, with a source for every figure."


def show_error(error: APIError, what: str) -> None:
    """The service's own message and code; extra details exactly as returned."""
    st.error(f"{what}: {error.message}")
    refusal = error.payload.get("refusal") if isinstance(error.payload, dict) else None
    if isinstance(refusal, dict) and refusal.get("why"):
        st.markdown(f"**Why:** {refusal['why']}")
    st.caption(f"code: {error.code}" + (f" · HTTP {error.status_code}" if error.status_code else ""))
    details = {k: v for k, v in (error.payload or {}).items() if k != "error"} \
        if isinstance(error.payload, dict) else {}
    if details:
        with st.expander("Details from the service"):
            st.json(details)


def _start_session() -> None:
    ss = st.session_state
    ss.pop("_start_error", None)
    try:
        created = connection.get_client().create_session()
    except APIError as error:
        ss["_start_error"] = error
        return
    # Nothing from the previous session may leak into the new one (an unsent question,
    # an upload result, widget values).
    stale = [key for key in ss if key in {state.SID, state.SERVER, state.DRAFT, state.SAVE,
                                          state.RESTORE, state.WORK}
             or key.startswith(("ui.", "ask.", "data."))]
    for key in stale:
        del ss[key]
    st.query_params.clear()
    st.query_params["sid"] = created["sid"]


def _start_button(label: str) -> None:
    st.button(label, type="primary", on_click=_start_session, key="shell.start")
    error = st.session_state.get("_start_error")
    if error is not None:
        show_error(error, "Could not start a session")


def landing() -> None:
    st.title("Analytics agent")
    st.write(TAGLINE)
    st.write(
        "Your work is saved on the analytics service under a private link: this page's "
        "address once a session starts. Keep the link to come back to it."
    )
    _start_button("Start a new session")


def invalid_sid() -> None:
    st.title("Analytics agent")
    st.error("This link's session id is not valid, so nothing was loaded.")
    _start_button("Start a new session")


def expired(message: str) -> None:
    st.title("Analytics agent")
    st.warning(
        "This session was not found or has expired. Sessions are kept for 30 days after "
        "the last activity."
    )
    st.caption(message)
    _start_button("Start a new session")


def unreachable(error: APIError) -> None:
    st.title("Analytics agent")
    show_error(error, "Can't reach the analytics service")
    st.button("Try again", key="shell.retry")


def sidebar(pages: Mapping[str, Any], titles: Mapping[str, str], sid: str) -> Any:
    """Links that keep the sid (native navigation drops it, F0). Returns a status slot."""
    with st.sidebar:
        st.markdown("**Analytics agent**")
        label = st.session_state[state.DRAFT].get("label")
        if label:
            st.caption(label)
        for page_id, page in pages.items():
            st.page_link(page, label=titles[page_id], query_params={"sid": sid})
        st.divider()
        return st.empty()


def render_status(slot: Any, ss: Mapping) -> None:
    save, server = ss[state.SAVE], ss[state.SERVER]
    status = save["status"]
    with slot.container():
        if status == "saved":
            st.caption(f"Saved · version {server['version']}")
        elif status == "rejected":
            st.warning(f"Not saved: {save['message']}. Change the value to try again.")
        elif status == "error":
            st.warning(f"Not saved yet: {save['message']}")
            st.button("Try saving again", key="shell.retry_save")
        elif status == "conflict":
            st.warning("Not saved: this session changed elsewhere.")
        elif status == "expired":
            st.warning("Not saved: the session has expired.")


def _values(ui_state: Mapping) -> dict[str, str]:
    drafts = ui_state["drafts"]
    return {
        "Analysis name": ui_state["label"] or "(none)",
        "Active dataset": ui_state["dataset_id"] or "(none)",
        "Datasets": ", ".join(ui_state["datasets"]) or "(none)",
        "Preparation drafts": "; ".join(
            f"{ds}: {', '.join(sorted(parts))}" for ds, parts in sorted(drafts.items())) or "(none)",
        "Page": ui_state["page"],
    }


def needs_dataset(ss: Mapping) -> None:
    st.info("Choose or upload a dataset on Data first.")
    st.page_link("views/data.py", label="Go to Data", icon="📄",
                 query_params={"sid": ss[state.SID]})


STEPS = (
    ("views/clean.py", "Clean", "🧹", "review the engine's cleaning proposals; nothing runs "
                                     "until you apply it"),
    ("views/domain.py", "Domain", "🏷️", "confirm what kind of data this is; it turns on that "
                                        "domain's tools and questions"),
    ("views/contract.py", "Contract", "📝", "say what each column is and how it adds up; "
                                           "analyses run under it"),
    ("views/metrics.py", "Metrics", "📐", "approve the metrics (CTR, CPA, ROAS…) and the "
                                         "validity rules analyses use"),
    ("views/keywords.py", "Keyword groups", "🔤", "review the proposed search-term groups; "
                                                  "only approved groups reach the tools"),
    ("views/tools.py", "Tools", "🧰", "run a marketing tool and see every figure with its source"),
    ("views/ask.py", "Ask", "💬", "ask questions about the data"),
)


def next_steps(ss: Mapping) -> None:
    st.markdown("**Next steps**")
    for path, title, icon, why in STEPS:
        st.page_link(path, label=f"{title}: {why}", icon=icon, query_params={"sid": ss[state.SID]})


def render_banner(container: Any, ss: Mapping) -> None:
    save = ss[state.SAVE]
    if save["status"] == "conflict":
        current = save["current"]
        theirs = _values(state.normalize(current.get("ui_state")))
        mine = _values(state.normalize(ss[state.DRAFT]))
        with container:
            st.warning(
                f"This session was changed in another tab or window (now version "
                f"{current['version']}). Your changes in this tab are not saved. Choose "
                "which to keep."
            )
            cell = lambda text: text.replace("|", "\\|")  # noqa: E731
            rows = [f"| {k} | {cell(mine[k])} | {cell(theirs[k])} |"
                    for k in mine if mine[k] != theirs[k]]
            if rows:
                st.markdown("| | This tab | Saved elsewhere |\n|---|---|---|\n" + "\n".join(rows))
            left, right = st.columns(2)
            left.button("Load latest", key="shell.load_latest", on_click=state.load_latest,
                        args=(ss,), help="Show the saved version; this tab's edits are dropped.")
            right.button("Keep my changes", key="shell.keep_mine", on_click=state.keep_mine,
                         args=(ss,), help="Save this tab's version over the other one.")
    elif save["status"] == "expired":
        with container:
            st.warning("This session has expired, so your latest changes were not saved.")
            _start_button("Start a new session")
