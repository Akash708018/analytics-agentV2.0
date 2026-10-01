"""Data page: upload a file, choose the dataset the other pages use, see its profile.

Only dataset ids are saved with the session (C8); names and figures come from the
server and are shown as returned. Answering ingest layout questions has no endpoint
yet (api-request), so they are shown, not answered.
"""

import streamlit as st

from frontend import connection, datasets, state
from frontend.api_client import APIError
from frontend.components.shell import next_steps, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]


def _upload() -> None:
    # Session state is read before the upload; after it, only held objects change (state.py).
    file, held_draft, held = ss.get("data.file"), ss[state.DRAFT], state.work(ss)
    workspace_id = ss[state.SERVER]["workspace_id"]
    if file is None:
        return
    try:
        dataset = api.upload_dataset(workspace_id, file.name, file.getvalue(),
                                     file.type or "application/octet-stream")
    except APIError as error:
        held["results"]["upload"] = {"error": error, "name": file.name}
        return
    dataset_id = state.add_dataset(held_draft, dataset)
    held["cache"][("record", dataset_id)] = dataset
    held["results"]["upload"] = {"dataset_id": dataset_id, "name": file.name}
    held["reseed"].append("ui.data.dataset_id")


def _look_again(dataset_id: str) -> None:
    datasets.invalidate(state.work(ss), dataset_id, ("record", "profile"))


def _upload_section() -> None:
    st.subheader("Upload a file")
    chosen = st.file_uploader("CSV or Excel workbook", type=["csv", "xlsx"], key="data.file")
    st.button("Upload", on_click=_upload, disabled=chosen is None, key="data.upload")
    result = state.work(ss)["results"].get("upload")
    if result and "error" in result:
        error = result["error"]
        show_error(error, f"{result['name']} was not loaded")
        if error.code == "ingest_needs_answers":
            for question in (error.payload or {}).get("questions", []):
                st.markdown(f"- {question}")
            st.info("Answering layout questions here needs an API endpoint that does not "
                    "exist yet; it has been requested.")
    elif result:
        st.success(f"Loaded {result['name']}.")


def _profile(dataset_id: str) -> None:
    profile = datasets.cached(ss, "profile", dataset_id, lambda: api.get_profile(dataset_id))
    if isinstance(profile, APIError):
        show_error(profile, "Could not read the profile")
        return
    st.subheader("What each column holds")
    st.caption(f"{profile['rows']} rows. Values as the service reports them.")
    st.dataframe(
        [{"column": c["name"], "type": c["type"], "null %": str(c["null_pct"]),
          "distinct": str(c["distinct"]), "sample (min, max)": ", ".join(map(str, c["sample"]))}
         for c in profile["columns"]],
        hide_index=True, use_container_width=True,
    )
    for warning in profile["warnings"]:
        st.warning(warning)


def _active_dataset() -> None:
    ids = draft["datasets"]
    if not ids:
        st.info("No data in this session yet.")
        return
    st.subheader("Dataset the other pages use")
    state.bind(ss, "ui.data.dataset_id", ("dataset_id",))
    st.selectbox(
        "Dataset the other pages use",
        options=ids,
        format_func=lambda dataset_id: datasets.label(ss, api, dataset_id),
        key="ui.data.dataset_id",
        placeholder="Choose a dataset",
        label_visibility="collapsed",
        on_change=state.on_change,
        args=(ss, "ui.data.dataset_id", ("dataset_id",)),
    )
    active = draft["dataset_id"]
    if active is None:
        return
    record = datasets.cached(ss, "record", active, lambda: api.get_dataset(active))
    if isinstance(record, APIError):
        show_error(record, "Could not load this dataset's record")
        if record.status_code == 404:
            st.button("Forget it in this session", on_click=state.forget_dataset,
                      args=(ss, active), key="data.forget")
        return
    st.markdown(f"**{record['name']}** · `{record['dataset_id']}`")
    left, right = st.columns(2)
    left.metric("Rows", record["rows"], border=True)
    right.metric("Columns", record["columns"], border=True)
    for assumption in record.get("assumptions", []):
        st.caption(f"Read as: {assumption}")
    st.button("Look again", on_click=_look_again, args=(active,), key="data.refresh",
              help="Read the record and profile from the service again.")
    next_steps(ss)
    _profile(active)


st.title("Data")
_upload_section()
_active_dataset()
