"""Synthetic F0 measurement app; never used by the product or real datasets.

Run: streamlit run frontend/dev/state_probe.py --server.port 8510
The displayed snapshot makes browser observations auditable without devtools.
"""

import uuid

import streamlit as st

DEMO_SID = "00000000-0000-4000-8000-0000000000f0"

st.set_page_config(page_title="F0 state probe")
st.title("F0 state probe")
st.caption("Synthetic text only. This probe deliberately has no durable storage.")
if "probe.instance" not in st.session_state:
    st.session_state["probe.instance"] = str(uuid.uuid4())
st.session_state["probe.reruns"] = st.session_state.get("probe.reruns", 0) + 1

st.text_input("Shared field", key="ui.probe.shared")
if st.button("Add synthetic sid to URL", key="ui.probe.stamp"):
    st.query_params["sid"] = DEMO_SID


def page_a():
    st.header("Page A")
    st.text_input("Page A field", key="ui.probe.a")


def page_b():
    st.header("Page B")
    st.text_input("Page B field", key="ui.probe.b")


page = st.navigation(
    [
        st.Page(page_a, title="Page A", default=True),
        st.Page(page_b, title="Page B", url_path="page-b"),
    ]
)
page.run()
st.json(
    {
        "streamlit": st.__version__,
        "instance": st.session_state["probe.instance"],
        "reruns": st.session_state["probe.reruns"],
        "page": page.title,
        "query_params": st.query_params.to_dict(),
        "shared": st.session_state["ui.probe.shared"],
        "page_a": st.session_state.get("ui.probe.a", "<absent>"),
        "page_b": st.session_state.get("ui.probe.b", "<absent>"),
    }
)
