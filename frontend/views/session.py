"""Session page: what this saved session is, and where to go next."""

import streamlit as st

from frontend import state
from frontend.components.shell import TAGLINE

ss = st.session_state
server = ss[state.SERVER]

st.title("Analytics agent")
st.write(TAGLINE)

state.bind(ss, "ui.session.label", ("label",), "")
st.text_input(
    "Name this analysis (optional)",
    key="ui.session.label",
    max_chars=state.LABEL_MAX,
    placeholder="e.g. Q3 paid search review",
    help="Saved with the session. Don't put personal data here.",
    on_change=state.on_change,
    args=(ss, "ui.session.label", ("label",)),
)

st.caption(
    f"Workspace {server['workspace_id']} · expires {server['expires_at']} "
    "(30 days after the last activity). This page's address is the link to this session."
)

if ss[state.DRAFT]["datasets"]:
    st.page_link("views/ask.py", label="Ask a question", icon="💬",
                 query_params={"sid": ss[state.SID]})
st.page_link("views/data.py", label="Add or choose data", icon="📄",
             query_params={"sid": ss[state.SID]})
