"""Tools page: run one domain tool and see every figure with its source.

Only domain tools are offered: core analyses run through a question (Ask). The params a
tool needs come from its pack (`GET /packs/{id}`); nothing is guessed. When the engine
needs an answer (a fork, a column, festival dates), it is asked here and the person runs
again. Results stay in the browser: they are data, not saved work. See docs/steps/F4.md.
"""

import streamlit as st

from frontend import connection, datasets, prep, state
from frontend.api_client import APIError
from frontend.components.bindings import binding_form
from frontend.components.results import render_result
from frontend.components.shell import needs_dataset, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]

# Tools that read or replace keyword groups (D-B7-4; measured in docs/steps/F6.md).
KEYWORD_NOTES = {
    "marketing.keyword_grouping": "Running this replaces every keyword group not yet approved "
                                  "(approved groups stay). Review the proposals on Keyword groups.",
    "marketing.keyword_group_performance": "Counts approved keyword groups only; every other "
                                           "keyword is “(ungrouped)”.",
    "marketing.page_targeting": "Uses approved keyword groups only.",
}


def k(dataset_id: str, tool_id: str, *parts: str) -> str:
    return ".".join(("ui.tools", dataset_id, tool_id, *parts))


def p(dataset_id: str, tool_id: str, *parts: str) -> tuple[str, ...]:
    return ("drafts", dataset_id, "params", tool_id, *parts)


def _run(dataset_id: str, tool_id: str, required: list[str], slots: list[str]) -> None:
    # Read the form first; after the POST only held objects change (state.py, C7).
    values = {name: ss.get(k(dataset_id, tool_id, "p", name)) for name in required}
    chosen = {slot: ss.get(k(dataset_id, tool_id, "slot", slot)) for slot in slots}
    dates_ok = bool(ss.get(f"tools.{dataset_id}.{tool_id}.dates_ok"))
    held, held_draft = state.work(ss), ss[state.DRAFT]
    bindings = dict(state.get_path(held_draft, p(dataset_id, tool_id, "bindings"), {}))
    try:
        params = prep.run_params(required, values, chosen, bindings, dates_ok)
    except ValueError:
        held["results"][("tool", dataset_id, tool_id)] = {"invalid": "A number field holds text."}
        return
    try:
        result = api.run_tool(tool_id, dataset_id, params)
    except APIError as error:
        held["results"][("tool", dataset_id, tool_id)] = {"error": error, "params": params}
        return
    held["results"][("tool", dataset_id, tool_id)] = {"result": result, "params": params}
    if tool_id == "marketing.keyword_grouping":
        datasets.invalidate(held, dataset_id, ("keywords",))


def _answer_forks(dataset_id: str, fork_ids: list[str]) -> None:
    answers = {f: ss.get(f"ui.tools.{dataset_id}.fork.{f}") for f in fork_ids}
    answers = {f: v for f, v in answers.items() if v}
    held = state.work(ss)
    try:
        api.answer_forks(dataset_id, answers)
    except APIError as error:
        held["results"][("forks", dataset_id)] = {"error": error}
        return
    held["results"][("forks", dataset_id)] = {"saved": answers}
    datasets.invalidate(held, dataset_id, ("tools",))


def _param_input(dataset_id: str, tool_id: str, name: str, festivals: list[dict]) -> None:
    label, kind, help_text = prep.param_spec(name)
    key, path = k(dataset_id, tool_id, "p", name), p(dataset_id, tool_id, name)
    args = (ss, key, path)
    if kind == "number":
        state.bind(ss, key, path, None)
        st.number_input(label, key=key, help=help_text, on_change=state.on_change, args=args)
    elif kind == "festival":
        names = {f["id"]: f.get("name", f["id"]) for f in festivals}
        state.bind(ss, key, path, None)
        st.selectbox(label, list(names), key=key, format_func=names.get, placeholder="choose…",
                     help=help_text, on_change=state.on_change, args=args)
    else:
        state.bind(ss, key, path, "")
        st.text_input(label, key=key, help=help_text, on_change=state.on_change, args=args)


def _error(dataset_id: str, tool_id: str, error: APIError, forks: list[dict]) -> None:
    extra = error.payload if isinstance(error.payload, dict) else {}
    if error.code == "param_required":
        st.error("Fill in: " + ", ".join(prep.param_spec(m)[0] for m in extra.get("missing", [])))
    elif error.code == "ambiguous_binding":
        concept = extra.get("concept", "")
        st.warning(f"More than one column could be '{concept}'. Pick one, then run again.")
        key, path = k(dataset_id, tool_id, "bind", concept), p(dataset_id, tool_id, "bindings", concept)
        state.bind(ss, key, path, None)
        st.selectbox(f"Column for {concept}", extra.get("columns", []), key=key,
                     placeholder="choose…", on_change=state.on_change, args=(ss, key, path))
    elif error.code == "forks_unanswered":
        st.warning("Answer these first; none is answered for you.")
        options = {f["fork_id"]: f for f in forks}
        missing = extra.get("missing", [])
        for fork in extra.get("forks", []):
            fid = fork["fork_id"]
            labels = {o["id"]: o["label"] for o in options.get(fid, {}).get("options", [])}
            key, path = f"ui.tools.{dataset_id}.fork.{fid}", ("drafts", dataset_id, "forks", fid)
            state.bind(ss, key, path, None)
            st.radio(fork["question"], list(labels), key=key, format_func=labels.get,
                     on_change=state.on_change, args=(ss, key, path))
        st.button("Save the answers", key="tools.forks", on_click=_answer_forks,
                  args=(dataset_id, missing))
    elif error.code == "festival_dates_unconfirmed":
        st.warning(error.message)
        for year, (start, end) in sorted(extra.get("dates", {}).items()):
            st.caption(f"{year}: {start} to {end}")
        st.checkbox("These dates are right for this run", key=f"tools.{dataset_id}.{tool_id}.dates_ok",
                    help="Asked on every run: festival dates move every year (D-B3-6).")
    elif error.code in ("needs_domain", "needs_data") and error.status_code == 409:
        show_error(error, "This tool cannot run on this data yet")
    elif error.code == "needs_data":
        show_error(error, "The tool did not run")
        st.caption("If the data has this under another name, name the column for the concept "
                   "above and run again.")
    else:
        show_error(error, "The tool did not run")


def _unavailable(tools: list[dict]) -> None:
    waiting = [t for t in tools if t["status"] != "active"]
    if not waiting:
        return
    with st.expander(f"Not available yet ({len(waiting)})"):
        for t in waiting:
            why = (f"confirm the {t['needs_domain']} domain" if t["status"] == "needs_domain"
                   else "the data has no " + ", ".join(t.get("missing_concepts", [])))
            st.caption(f"{t['ui_label']} · `{t['tool_id']}`: {why}")
        st.page_link("views/domain.py", label="Domain", icon="🏷️",
                     query_params={"sid": ss[state.SID]})


def main() -> None:
    st.title("Tools")
    dataset_id = draft["dataset_id"]
    if dataset_id is None:
        needs_dataset(ss)
        return
    st.caption(datasets.label(ss, api, dataset_id))
    listed = datasets.cached(ss, "tools", dataset_id, lambda: api.list_tools(dataset_id))
    if isinstance(listed, APIError):
        show_error(listed, "Could not read the tools")
        return
    tools = [t for t in listed["tools"] if not t["tool_id"].startswith("core.")]
    active = {t["tool_id"]: t for t in tools if t["status"] == "active"}
    st.caption(f"{len(active)} of {len(tools)} domain tools can run on this data. Core "
               "analyses run through a question on Ask.")
    _unavailable(tools)
    if not active:
        st.info("No tool can run yet: confirm a domain whose data this is.")
        return
    key, path = f"ui.tools.{dataset_id}.tool", ("drafts", dataset_id, "tool")
    state.bind(ss, key, path, None)
    if ss[key] not in active:
        ss[key] = None
    st.selectbox("Tool", list(active), key=key, format_func=lambda t: active[t]["ui_label"],
                 placeholder="Choose a tool", on_change=state.on_change, args=(ss, key, path))
    tool_id = ss[key]
    if tool_id is None:
        return
    pack_id = tool_id.split(".", 1)[0]
    pack = datasets.cached(ss, "pack", pack_id, lambda: api.get_pack(pack_id))
    core = datasets.cached(ss, "pack", "core", lambda: api.get_pack("core"))
    if isinstance(pack, APIError):
        show_error(pack, "Could not read the tool's description")
        return
    spec = next((t for t in pack["pack"]["tools"] if t["id"] == tool_id), {})
    festivals = [] if isinstance(core, APIError) else core["pack"].get("festivals", [])
    st.write(spec.get("description", ""))
    if note := KEYWORD_NOTES.get(tool_id):
        st.info(note)
        st.page_link("views/keywords.py", label="Keyword groups", icon="🔤",
                     query_params={"sid": ss[state.SID]})
    required = list(spec.get("params_required", []))
    for name in required:
        _param_input(dataset_id, tool_id, name, festivals)
    slots = list(spec.get("slots", {}))
    profile = datasets.cached(ss, "profile", dataset_id, lambda: api.get_profile(dataset_id))
    columns = [] if isinstance(profile, APIError) else [c["name"] for c in profile["columns"]]
    for slot in slots:
        key, path = k(dataset_id, tool_id, "slot", slot), p(dataset_id, tool_id, slot)
        state.bind(ss, key, path, None)
        st.selectbox(f"{slot.capitalize()} (optional)", columns, key=key,
                     placeholder="the tool's own choice: " + ", ".join(spec["slots"][slot]),
                     on_change=state.on_change, args=(ss, key, path))
    binding_form(ss, f"tools.{dataset_id}.{tool_id}", p(dataset_id, tool_id, "bindings"), columns)
    st.button("Run", type="primary", key="tools.run", on_click=_run,
              args=(dataset_id, tool_id, required, slots))
    saved = state.work(ss)["results"].get(("forks", dataset_id))
    if saved and "error" in saved:
        show_error(saved["error"], "The answers were not saved")
    elif saved:
        st.success("Answers saved. Run the tool again.")
    outcome = state.work(ss)["results"].get(("tool", dataset_id, tool_id))
    if not outcome:
        return
    if "invalid" in outcome:
        st.error(outcome["invalid"])
    elif "error" in outcome:
        proposal = datasets.cached(ss, "proposal", dataset_id,
                                   lambda: api.get_contract_proposal(dataset_id))
        forks = [] if isinstance(proposal, APIError) else proposal["forks"]
        _error(dataset_id, tool_id, outcome["error"], forks)
    else:
        st.divider()
        render_result(outcome["result"])


main()
