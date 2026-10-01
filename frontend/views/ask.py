"""Ask page (F2 minimum): send a question once, follow it by id, survive refresh.

The turn list is the server's. A refresh shows a running turn and keeps polling
it; it never sends the question again. The question box is browser-only (free
text is not put in ui_state). Rendering tool results and figures is a later milestone.
"""

import streamlit as st

from frontend import connection, datasets, state, turns
from frontend.api_client import APIError
from frontend.components.shell import show_error

POLL_SECONDS = 2

ss = st.session_state
api = connection.get_client()
sid = ss[state.SID]
draft = ss[state.DRAFT]
dataset_id = draft["dataset_id"]


def render_turn(turn: dict) -> None:
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        status = turn["status"]
        if status == "done":
            answer = turn.get("answer") or {}
            st.markdown(answer.get("text", ""))
            for flag in answer.get("flags", []):
                st.warning(flag if isinstance(flag, str) else str(flag))
            results = answer.get("results", [])
            if results:
                st.caption(f"{len(results)} tool result(s) back this answer; the evidence "
                           "view arrives in a later milestone.")
        elif turns.is_active(turn):
            steps = [e["type"] for e in turn["events"]]
            st.info(f"Working ({status})" + (f": {', '.join(steps)}" if steps else "") + ".")
        else:
            st.error(turns.TERMINAL_NOTES.get(status, f"Status: {status}"))
            for event in turn["events"]:
                if event["type"] == "error":
                    st.caption(f"{event['data'].get('code')}: {event['data'].get('message')}")
        st.caption(f"{turn['turn_id']} · {turn['created_at']}")


@st.fragment(run_every=POLL_SECONDS)
def follow(turn_ids: list[str]) -> None:
    """Poll the running turns by id; when one finishes, redraw the whole page."""
    for turn_id in turn_ids:
        try:
            turn = api.get_turn(turn_id)
        except APIError as error:
            show_error(error, "Could not check on this question")
            continue
        if not turns.is_active(turn):
            st.rerun()
        render_turn(turn)


def send(question: str) -> None:
    known = ss.get("ask.known_ids", [])
    outcome = turns.submit(api, sid, dataset_id, question, known)
    if outcome.outcome in {"sent", "received"}:
        ss.pop("ask.unsent", None)
    else:
        ss["ask.unsent"] = {"question": question, "outcome": outcome}


st.title("Ask")
if dataset_id is None:
    st.info("Choose or upload a dataset first.")
    st.page_link("views/data.py", label="Go to Data", icon="📄", query_params={"sid": sid})
else:
    st.caption(f"Questions are about {datasets.label(ss, api, dataset_id)}.")

history = st.container()

unsent = ss.get("ask.unsent")
if unsent:
    outcome = unsent["outcome"]
    if outcome.outcome == "error":
        show_error(outcome.error, "The question was not stored")
    else:
        st.warning("The service did not confirm this question, and it is not in this "
                   "session's list" + (" (the list could not be checked)"
                                       if outcome.outcome == "unknown" else "") + ".")
    st.text(unsent["question"])
    again, drop = st.columns(2)
    again.button("Send again", key="ask.again", on_click=send, args=(unsent["question"],),
                 disabled=dataset_id is None)
    drop.button("Discard", key="ask.discard", on_click=ss.pop, args=("ask.unsent", None))

try:
    listed = api.list_turns(sid)["turns"]
except APIError as error:
    listed = None
    with history:
        show_error(error, "Could not load this session's questions")

active = [t for t in listed or [] if turns.is_active(t)]
with st.form("ask.form", clear_on_submit=True):
    question = st.text_area("Your question", key="ask.question", max_chars=4000,
                            placeholder="e.g. Why did ROAS drop in September?")
    sent = st.form_submit_button(
        "Ask", type="primary",
        disabled=dataset_id is None or listed is None or bool(active) or bool(unsent),
        help="One question at a time; the next can be asked when this one finishes.",
    )
if sent and question.strip() and dataset_id is not None:
    ss["ask.known_ids"] = [t["turn_id"] for t in listed or []]
    send(question.strip())
    st.rerun()

if listed is not None:
    ss["ask.known_ids"] = [t["turn_id"] for t in listed]
    with history:
        for turn in listed:
            if not turns.is_active(turn):
                render_turn(turn)
        if active:
            follow([t["turn_id"] for t in active])
        elif not listed:
            st.caption("No questions in this session yet.")
