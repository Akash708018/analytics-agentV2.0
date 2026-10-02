"""Clean page: the engine proposes, the person ticks, nothing runs until Apply.

Nothing is pre-ticked, not even `suggested` steps (AGENTS.md: no silent defaults);
"Tick the suggested" is an explicit click. Ticks are kept by id+kind+column, and
Apply re-reads the plan first: a renumbered plan sends nothing (F3.md, measured).
"""

import streamlit as st

from frontend import connection, datasets, prep, state
from frontend.api_client import APIError
from frontend.components.shell import needs_dataset, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]


def _key(dataset_id: str, fp: str) -> str:
    return f"ui.clean.{dataset_id}.{fp}"


def _tick_suggested(dataset_id: str, fps: list[str]) -> None:
    for fp in fps:
        ss[_key(dataset_id, fp)] = True
        state.set_path(ss[state.DRAFT], ("drafts", dataset_id, "clean", fp), True)


def _apply(dataset_id: str) -> None:
    held, held_draft = state.work(ss), ss[state.DRAFT]
    results = held["results"]
    ticks = [fp for fp, on in state.get_path(held_draft, ("drafts", dataset_id, "clean"), {}).items() if on]
    try:
        fresh = api.list_cleaning_proposals(dataset_id)       # the engine's current plan
    except APIError as error:
        results[("clean", dataset_id)] = {"error": error}
        return
    held["cache"][("clean", dataset_id)] = fresh
    send, stale = prep.to_apply(fresh["proposals"], ticks)
    if stale:
        for fp in stale:
            state.drop_path(held_draft, ("drafts", dataset_id, "clean", fp))
        results[("clean", dataset_id)] = {"changed": stale}
        held["reseed"].append(f"ui.clean.{dataset_id}.")
        return
    try:
        reply = api.approve_cleaning(dataset_id, approve=send)
    except APIError as error:
        results[("clean", dataset_id)] = {"error": error}
        return
    results[("clean", dataset_id)] = {"applied": reply}
    state.drop_path(held_draft, ("drafts", dataset_id, "clean"))
    datasets.invalidate(held, dataset_id)                     # rows, profile, plan, proposal
    held["reseed"] += [f"ui.clean.{dataset_id}.", f"ui.contract.{dataset_id}."]


def _look_again(dataset_id: str) -> None:
    datasets.invalidate(state.work(ss), dataset_id, ("clean",))


def _result(dataset_id: str) -> None:
    result = state.work(ss)["results"].get(("clean", dataset_id))
    if not result:
        return
    if "error" in result:
        show_error(result["error"], "Nothing was applied")
    elif "changed" in result:
        st.warning("The proposals changed since you ticked them, so nothing was applied. "
                   "Review the list below and tick again.")
    else:
        reply = result["applied"]
        if reply["applied"]:
            st.success(f"Applied {', '.join(reply['applied'])} "
                       f"({reply['ledger_entries']} ledger entries). The list below is the "
                       "engine's new plan.")


def main() -> None:
    st.title("Clean")
    dataset_id = draft["dataset_id"]
    if dataset_id is None:
        needs_dataset(ss)
        return
    st.caption(datasets.label(ss, api, dataset_id))
    _result(dataset_id)
    plan = datasets.cached(ss, "clean", dataset_id,
                           lambda: api.list_cleaning_proposals(dataset_id))
    if isinstance(plan, APIError):
        show_error(plan, "Could not read the cleaning proposals")
        return
    proposals = plan["proposals"]
    if not proposals:
        st.success("The engine proposes no cleaning for this data.")
        return
    st.write("Tick the steps you approve. Nothing changes until you press **Apply**; ticked "
             "steps run in the order shown, all or none.")
    unticked_suggestions = []
    for proposal in proposals:
        fp = prep.fingerprint(proposal)
        key, path = _key(dataset_id, fp), ("drafts", dataset_id, "clean", fp)
        state.bind(ss, key, path, False)
        st.checkbox(proposal["description"], key=key, on_change=state.on_change,
                    args=(ss, key, path))
        where = f" · {proposal['column']}" if proposal.get("column") else ""
        st.caption(f"{proposal['action_id']} · {proposal['kind']}{where} · "
                   f"{proposal['rows_affected']} row(s) affected")
        if proposal.get("lossy"):
            st.caption(":orange[Loses data: rows or values change and the originals are "
                       "not kept in the table (the ledger records the step).]")
        if proposal.get("suggested"):
            st.caption("Suggested: lossless and conflict-free. It still needs your tick.")
            if not ss[key]:
                unticked_suggestions.append(fp)
    ticked = sum(1 for p in proposals if ss[_key(dataset_id, prep.fingerprint(p))])
    left, middle, right = st.columns(3)
    if unticked_suggestions:
        left.button(f"Tick the {len(unticked_suggestions)} suggested", key=f"clean.suggest",
                    on_click=_tick_suggested, args=(dataset_id, unticked_suggestions))
    middle.button(f"Apply {ticked} step(s)", type="primary", key="clean.apply",
                  disabled=ticked == 0, on_click=_apply, args=(dataset_id,))
    right.button("Look again", key="clean.refresh", on_click=_look_again, args=(dataset_id,),
                 help="Ask the engine for its plan again.")


main()
