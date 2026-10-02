"""Data page (F2 minimum): upload a file, choose the dataset questions use.

Only display metadata is saved with the session; the data stays on the server.
Answering layout questions, cleaning, domain and contract are later milestones.
"""

import streamlit as st

from frontend import connection, state
from frontend.api_client import APIError
from frontend.components.shell import show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]


def _upload() -> None:
    # Read session state before the upload; after it, the draft in hand is updated first
    # so a run stopped midway cannot lose the dataset reference (see state.py).
    file, draft, workspace_id = ss.get("data.file"), ss[state.DRAFT], ss[state.SERVER]["workspace_id"]
    if file is None:
        return
    try:
        dataset = api.upload_dataset(workspace_id, file.name, file.getvalue(),
                                     file.type or "application/octet-stream")
    except APIError as error:
        ss["data.result"] = {"error": error, "name": file.name}
        return
    dataset_id = state.add_dataset(draft, dataset)
    ss["data.result"] = {"dataset_id": dataset_id, "name": file.name}
    ss["ui.data.dataset_id"] = dataset_id


st.title("Data")

st.subheader("Upload a file")
chosen = st.file_uploader("CSV or Excel workbook", type=["csv", "xlsx"], key="data.file")
st.button("Upload", on_click=_upload, disabled=chosen is None, key="data.upload")
result = ss.get("data.result")
if result and "error" in result:
    error = result["error"]
    show_error(error, f"{result['name']} was not loaded")
    if error.code == "ingest_needs_answers":
        for question in (error.payload or {}).get("questions", []):
            st.markdown(f"- {question}")
        st.info("Answering layout questions here is not built yet (a later milestone).")
elif result:
    st.success(f"Loaded {result['name']}.")


def _active_dataset() -> None:
    datasets = draft["datasets"]
    if not datasets:
        st.info("No data in this session yet.")
        return
    st.subheader("Dataset for questions")
    names = {d["dataset_id"]: f"{d['name'] or d['dataset_id']} · {d['dataset_id']}"
             for d in datasets}
    state.seed(ss, "ui.data.dataset_id")
    st.selectbox(
        "Dataset for questions",
        options=list(names),
        format_func=names.get,
        key="ui.data.dataset_id",
        placeholder="Choose a dataset",
        label_visibility="collapsed",
        on_change=state.on_widget_change,
        args=(ss, "ui.data.dataset_id"),
    )
    active = draft["dataset_id"]
    if active is None:
        return
    try:
        record = api.get_dataset(active)
    except APIError as error:
        show_error(error, "Could not load this dataset's record")
        if error.status_code == 404:
            st.button("Forget it in this session", on_click=state.forget_dataset,
                      args=(ss, active), key="data.forget")
        return
    st.markdown(f"**{record['name']}** · `{record['dataset_id']}`")
    left, right = st.columns(2)
    left.metric("Rows", record["rows"], border=True)
    right.metric("Columns", record["columns"], border=True)
    for assumption in record.get("assumptions", []):
        st.caption(f"Read as: {assumption}")
    st.page_link("views/ask.py", label="Ask about this data", icon="💬",
                 query_params={"sid": ss[state.SID]})


_active_dataset()
