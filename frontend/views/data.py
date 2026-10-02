"""Data page: upload a file, choose the dataset the other pages use, see its profile.

Only dataset ids are saved with the session (C8); names and figures come from the
server and are shown as returned. A file whose layout the reader can't settle is refused
with questions and a preview; the person answers them here (API 0.8.0, #16; F10). The
form starts blank: the reader's guess is offered as a button, never applied by itself.
"""

import streamlit as st

from frontend import connection, datasets, prep, state
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
        _refused(held, error, file.name)
        return
    _loaded(held_draft, held, dataset, file.name)


def _refused(held: dict, error: APIError, name: str, sent: dict | None = None) -> None:
    held["results"]["upload"] = {"error": error, "name": name}
    if error.code == "ingest_needs_answers" and isinstance(error.payload, dict):
        held["results"]["refused"] = {"name": name, "payload": error.payload, "sent": sent}
    elif sent is None:
        held["results"].pop("refused", None)        # a new file's failure: the old form goes


def _loaded(held_draft: dict, held: dict, dataset: dict, name: str, sent: dict | None = None) -> None:
    dataset_id = state.add_dataset(held_draft, dataset)
    held["cache"][("record", dataset_id)] = dataset
    held["results"]["upload"] = {"dataset_id": dataset_id, "name": name, "answered": sent}
    held["results"].pop("refused", None)
    held["reseed"].append("ui.data.dataset_id")


def a(upload_id: str, *parts: str) -> str:
    return ".".join(("data.answers", upload_id, *parts))


def _use_guess(upload_id: str, guess: dict) -> None:
    """Fills the blank fields with the reader's guess; answers already given stay."""
    for field, value in prep.guess_answers(guess).items():
        if field == "columns":
            for i, (target, kind) in enumerate(value.values()):
                if not ss.get(a(upload_id, "col", str(i), "target")):
                    ss[a(upload_id, "col", str(i), "target")] = target
                if not ss.get(a(upload_id, "col", str(i), "type")) and kind in prep.COLUMN_TYPES:
                    ss[a(upload_id, "col", str(i), "type")] = kind
        elif ss.get(a(upload_id, field)) in (None, "", []) and value not in (None, "", []):
            ss[a(upload_id, field)] = value


def _load_answers(upload_id: str, sources: list[str]) -> None:
    # Read the form first; after the POST only held objects change (state.py, C7).
    form = {field: ss.get(a(upload_id, field)) for field in
            ("sheet", "header_rows", "header_join", "data_start", "footer_rows", "name")}
    form["columns"] = {src: (ss.get(a(upload_id, "col", str(i), "target")) or "",
                             ss.get(a(upload_id, "col", str(i), "type")))
                       for i, src in enumerate(sources)}
    held, held_draft = state.work(ss), ss[state.DRAFT]
    workspace_id = ss[state.SERVER]["workspace_id"]
    name = (held["results"].get("refused") or {}).get("name", upload_id)
    body = prep.upload_answers(form)
    try:
        dataset = api.answer_upload(workspace_id, upload_id, **body)
    except APIError as error:
        _refused(held, error, name, sent=body)
        return
    _loaded(held_draft, held, dataset, name, sent=body)
    held["reseed"].append(a(upload_id) + ".")


def _look_again(dataset_id: str) -> None:
    datasets.invalidate(state.work(ss), dataset_id, ("record", "profile"))


def _preview(preview: dict) -> None:
    rows = preview.get("rows") or []
    if not rows:
        return
    first = preview.get("first_row_number") or 1
    width = max(len(r) for r in rows)
    st.caption(f"The first {len(rows)} rows as the file holds them, numbered as in the file"
               + (f" (sheet {preview['sheet']})" if preview.get("sheet") else "") + ".")
    st.dataframe([{"row": str(first + i),
                   **{f"col {j + 1}": "" if v is None else str(v)
                      for j, v in enumerate(list(r) + [None] * (width - len(r)))}}
                  for i, r in enumerate(rows)], hide_index=True, use_container_width=True)


def _blank(key: str, empty) -> None:
    if key not in ss:
        ss[key] = empty


def _answers(refused: dict) -> None:
    """The reader's questions, the file's first rows, and a blank form for the answers."""
    payload = refused["payload"]
    upload_id = payload.get("upload_id")
    preview = payload.get("preview") or {}
    if not upload_id:
        return
    st.warning(f"{refused['name']} is not loaded yet: the reader needs your answers about "
               "its layout.")
    if refused.get("sent") is not None:
        sent = refused["sent"]
        st.caption("Your answers were sent (" + (", ".join(sent) or "none filled")
                   + "); the reader still asks:")
    for question in payload.get("questions", []):
        st.markdown(f"- {question}")
    if payload.get("unresolved"):
        st.caption("Still open: " + ", ".join(payload["unresolved"]))
    _preview(preview)
    guess = preview.get("guess") or {}
    rows = preview.get("rows") or []
    first = preview.get("first_row_number") or 1
    sources = [c["source"] for c in guess.get("columns") or [] if c.get("source")]
    st.markdown("**Your answers**")
    st.caption("Blank means not answered: the reader asks again rather than assume.")
    if guess:
        st.button("Use the reader's guess", key=a(upload_id, "guess"), on_click=_use_guess,
                  args=(upload_id, guess),
                  help="Fills the blank fields with what the reader assumed; your answers stay. "
                       "Nothing loads until you press Load.")
    sheets = preview.get("sheet_names") or []
    if len(sheets) > 1:
        _blank(a(upload_id, "sheet"), None)
        st.selectbox("Sheet", sheets, key=a(upload_id, "sheet"), placeholder="choose…")
    _blank(a(upload_id, "header_rows"), [])
    st.multiselect("Rows that form the header", list(range(first, first + len(rows))),
                   key=a(upload_id, "header_rows"), placeholder="choose the row numbers",
                   help="A title row above the header is not part of it.")
    _blank(a(upload_id, "header_join"), None)
    st.selectbox("With several header rows, a column's name is", list(prep.HEADER_JOINS),
                 key=a(upload_id, "header_join"), format_func=prep.HEADER_JOINS.get,
                 placeholder="choose…")
    left, right = st.columns(2)
    _blank(a(upload_id, "data_start"), None)
    left.number_input("First data row", min_value=1, step=1, key=a(upload_id, "data_start"))
    _blank(a(upload_id, "footer_rows"), None)
    right.number_input("Rows at the end to leave out (totals, notes)", min_value=0, step=1,
                       key=a(upload_id, "footer_rows"))
    _blank(a(upload_id, "name"), "")
    st.text_input("Dataset name", key=a(upload_id, "name"), max_chars=120)
    if sources:
        with st.expander(f"Rename or retype columns ({len(sources)}, optional)"):
            for i, source in enumerate(sources):
                left, right = st.columns(2)
                _blank(a(upload_id, "col", str(i), "target"), "")
                left.text_input(f"{source}: load as", key=a(upload_id, "col", str(i), "target"))
                _blank(a(upload_id, "col", str(i), "type"), None)
                right.selectbox(f"{source}: type", prep.COLUMN_TYPES,
                                key=a(upload_id, "col", str(i), "type"),
                                placeholder="the reader's choice")
    st.button("Load with these answers", type="primary", key=a(upload_id, "load"),
              on_click=_load_answers, args=(upload_id, sources),
              help="Sends only the answers you filled in.")


def _upload_section() -> None:
    st.subheader("Upload a file")
    chosen = st.file_uploader("CSV or Excel workbook", type=["csv", "xlsx"], key="data.file")
    st.button("Upload", on_click=_upload, disabled=chosen is None, key="data.upload")
    results = state.work(ss)["results"]
    result, refused = results.get("upload"), results.get("refused")
    if result and "error" in result and result["error"].code != "ingest_needs_answers":
        show_error(result["error"], f"{result['name']} was not loaded")
    elif result and "error" not in result:
        st.success(f"Loaded {result['name']}" + (" with your answers." if result.get("answered")
                                                  is not None else "."))
    if refused:
        _answers(refused)


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
