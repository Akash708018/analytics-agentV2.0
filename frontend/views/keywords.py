"""Keyword groups page: the engine proposes groups of search terms; a person reviews them.

Groups are proposals until a person approves them (D-B7-4): only approved groups reach the
engine (keyword group performance, page targeting). Nothing is chosen or ticked for the
person, and what each action does to approval is said before it is sent (frontend/keywords.py).
Saved: the column and the ticked group ids. Keywords and typed names stay in the browser.
See docs/steps/F6.md.
"""

import streamlit as st

from frontend import connection, datasets, keywords as kw, state
from frontend.api_client import APIError
from frontend.components.shell import needs_dataset, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]

WHAT = {"run": "No groups were proposed", "approve": "Not approved", "merge": "Not merged",
        "rename": "Not renamed", "split": "Not split", "move_keyword": "Not moved"}


def k(dataset_id: str, *parts: str) -> str:
    return ".".join(("ui.kw", dataset_id, *parts))


def p(dataset_id: str, *parts: str) -> tuple[str, ...]:
    return ("drafts", dataset_id, "keywords", *parts)


def _send(dataset_id: str, action: str, call, done: str, reseed: tuple[str, ...] = ()) -> None:
    """One request; afterwards only held objects change (state.py, C7)."""
    held, held_draft = state.work(ss), ss[state.DRAFT]
    try:
        reply = call()
    except APIError as error:
        held["results"][("kw", dataset_id)] = {"error": error, "what": WHAT[action]}
        return
    held["cache"][("keywords", dataset_id)] = reply
    held["results"][("kw", dataset_id)] = {"done": done}
    if action in ("run", "approve", "merge"):            # ticks were spent or their ids replaced
        state.drop_path(held_draft, p(dataset_id, "ticks"))
        held["reseed"].append(k(dataset_id, "tick") + ".")
    held["reseed"].extend(reseed)


def _run(dataset_id: str) -> None:
    column = ss.get(k(dataset_id, "column"))
    _send(dataset_id, "run", lambda: api.run_keyword_grouping(dataset_id, column),
          f"Groups proposed from `{column}`.")


def _approve(dataset_id: str, ids: list[str]) -> None:
    _send(dataset_id, "approve", lambda: api.apply_keyword_group_action(dataset_id, "approve", ids),
          f"Approved {len(ids)} group(s).")


def _merge(dataset_id: str, ids: list[str]) -> None:
    target = ss.get(k(dataset_id, "merge", "into"))
    label = (ss.get(k(dataset_id, "merge", "label")) or "").strip() or None
    if target not in ids:
        state.work(ss)["results"][("kw", dataset_id)] = {
            "invalid": "Choose the group to merge into first."}
        return
    order = [target, *[i for i in ids if i != target]]   # the backend merges into the first id
    _send(dataset_id, "merge",
          lambda: api.apply_keyword_group_action(dataset_id, "merge", order, label=label),
          f"Merged {len(order)} groups.", (k(dataset_id, "merge") + ".",))


def _edit(dataset_id: str, gid: str, action: str) -> None:
    e = lambda name: ss.get(k(dataset_id, "edit", gid, name))  # noqa: E731
    fields: dict = {}
    if action == "rename":
        fields["label"] = (e("label") or "").strip() or None
    elif action == "split":
        fields["keywords"] = list(e("split") or [])
        fields["label"] = (e("split_label") or "").strip() or None
    else:
        fields["keyword"], fields["target_group_id"] = e("move"), e("to")
    done = {"rename": "Renamed.", "split": "Split off as a new proposal.",
            "move_keyword": "Keyword moved."}[action]
    _send(dataset_id, action,
          lambda: api.apply_keyword_group_action(dataset_id, action, [gid], **fields),
          done, (k(dataset_id, "edit") + ".",))


def _choose(dataset_id: str) -> None:
    ss[k(dataset_id, "chosen")] = ss[k(dataset_id, "group")]


def _propose(dataset_id: str, columns: list[str] | None, listing: dict | None) -> None:
    st.subheader("Propose groups")
    key, path = k(dataset_id, "column"), p(dataset_id, "column")
    state.bind(ss, key, path, None)
    if columns is not None:                       # None: the profile could not be read
        if ss[key] is not None and ss[key] not in columns:
            ss[key] = None
        st.selectbox("Column holding the search terms", columns, key=key, placeholder="choose…",
                     help="The engine groups the distinct values of this column.",
                     on_change=state.on_change, args=(ss, key, path))
    groups = (listing or {}).get("groups", [])
    proposals = [g for g in groups if not g["approved"]]
    if proposals:
        st.warning(f"Proposing again replaces the {len(proposals)} group(s) not yet approved, "
                   "with any rename, merge, move or split you made to them. Approved groups "
                   "stay as they are.")
    st.button(f"Replace the {len(proposals)} proposals" if proposals else "Propose groups",
              key=k(dataset_id, "run"), type="primary", disabled=columns is None or ss[key] is None,
              on_click=_run, args=(dataset_id,))
    run = (listing or {}).get("run")
    if run:
        st.caption(f"Last run: column `{run.get('column')}` · {run.get('keywords')} keyword(s) "
                   f"read · {run.get('proposed_groups')} proposal(s) · embedding "
                   f"{run.get('embedding')}, threshold {run.get('threshold')}")
        typos = run.get("typos_merged") or {}
        if typos:
            with st.expander(f"Spellings merged before grouping ({len(typos)})"):
                for wrong, right in typos.items():
                    st.markdown(f"`{wrong}` → `{right}`")


def _ticked_actions(dataset_id: str, groups: list[dict], ids: list[str]) -> None:
    by_id = {g["group_id"]: g for g in groups}
    proposals = [i for i in ids if not by_id[i]["approved"]]
    st.markdown(f"**{len(ids)} ticked**" + (f" ({len(proposals)} not yet approved)" if ids else ""))
    st.button(f"Approve the {len(proposals)} ticked" if proposals else "Approve the ticked",
              key=k(dataset_id, "approve"), type="primary", disabled=not proposals,
              on_click=_approve, args=(dataset_id, proposals))
    if len(ids) < 2:
        st.caption("Tick two or more groups to merge them.")
        return
    key = k(dataset_id, "merge", "into")
    if ss.get(key) not in ids:
        ss[key] = None
    st.selectbox("Merge the ticked groups into", ids, key=key, placeholder="choose…",
                 format_func=lambda i: by_id[i]["label"])
    st.text_input("New name for the merged group (optional)", key=k(dataset_id, "merge", "label"))
    if ss[key] is not None:
        outcome, warnings = kw.merge_notes(by_id[ss[key]], [by_id[i] for i in ids if i != ss[key]])
        st.caption(outcome)
        for warning in warnings:
            st.warning(warning)
    st.button(f"Merge {len(ids)} groups", key=k(dataset_id, "merge", "go"),
              on_click=_merge, args=(dataset_id, ids))


def _edit_one(dataset_id: str, groups: list[dict]) -> None:
    by_id = {g["group_id"]: g for g in groups}
    with st.expander("Edit one group: rename, split, move a keyword"):
        # The browser hands a selectbox's choice back as its shown text, which a rename, split
        # or move changes, so the choice was lost after each edit (C12). The id is the choice.
        key, chosen = k(dataset_id, "group"), ss.get(k(dataset_id, "chosen"))
        ss[key] = chosen if chosen in by_id else None
        st.selectbox("Group", list(by_id), key=key, placeholder="choose…",
                     format_func=lambda i: f"{by_id[i]['label']} ({len(by_id[i]['keywords'])})",
                     on_change=_choose, args=(dataset_id,))
        gid = ss[key]
        if gid is None:
            return
        group = by_id[gid]
        e = lambda name: k(dataset_id, "edit", gid, name)  # noqa: E731  (fresh widgets per group)
        st.text_input("New name", key=e("label"), placeholder=group["label"])
        st.button("Rename", key=e("rename"), on_click=_edit, args=(dataset_id, gid, "rename"))
        st.divider()
        st.multiselect("Keywords to split off", group["keywords"], key=e("split"))
        st.text_input("Name for the new group (optional)", key=e("split_label"))
        st.caption(kw.split_note(group))
        st.button("Split", key=e("split_go"), on_click=_edit, args=(dataset_id, gid, "split"))
        st.divider()
        others = [i for i in by_id if i != gid]
        st.selectbox("Keyword to move", group["keywords"], key=e("move"), index=None,
                     placeholder="choose…")
        st.selectbox("Move it to", others, key=e("to"), index=None, placeholder="choose…",
                     format_func=lambda i: by_id[i]["label"])
        target = ss.get(e("to"))
        if target in by_id:
            st.caption(kw.move_note(group, by_id[target]))
        st.button("Move", key=e("move_go"), on_click=_edit, args=(dataset_id, gid, "move_keyword"))


def _list(dataset_id: str, groups: list[dict]) -> None:
    left, mid, right = st.columns(3)
    status = left.radio("Show", kw.STATUS, key=k(dataset_id, "f", "status"), horizontal=True)
    options, key = sorted({g["intent"] for g in groups}), k(dataset_id, "f", "intent")
    if key in ss:                                  # a new run may drop an intent
        ss[key] = [i for i in ss[key] if i in options]
    intents = mid.multiselect("Intent", options, key=key, placeholder="every intent")
    find = right.text_input("Find a keyword", key=k(dataset_id, "f", "find"))
    shown = kw.visible(groups, status, intents, find or "")
    if len(shown) < len(groups):
        st.caption(f"Showing {len(shown)} of {len(groups)} groups.")
    for g in shown:
        gid = g["group_id"]
        with st.container(border=True):
            key, path = k(dataset_id, "tick", gid), p(dataset_id, "ticks", gid)
            state.bind(ss, key, path, False)
            st.checkbox(kw.heading(g), key=key, on_change=state.on_change, args=(ss, key, path))
            facets = kw.facets_text(g.get("facets") or {})
            st.caption(f"`{gid}` · proposed by {g.get('proposed_by', 'rules')}"
                       + (f" · {facets}" if facets else ""))
            st.caption(", ".join(g["keywords"]))


def main() -> None:
    st.title("Keyword groups")
    dataset_id = draft["dataset_id"]
    if dataset_id is None:
        needs_dataset(ss)
        return
    st.caption(datasets.label(ss, api, dataset_id))
    st.write("The engine proposes groups of search terms; you approve them. Only approved "
             "groups reach the engine: keyword group performance and page targeting count "
             "them, and every other keyword is “(ungrouped)”.")
    profile = datasets.cached(ss, "profile", dataset_id, lambda: api.get_profile(dataset_id))
    columns = None if isinstance(profile, APIError) else [c["name"] for c in profile["columns"]]
    if columns is None:
        show_error(profile, "Could not read the columns")
    listing = datasets.cached(ss, "keywords", dataset_id, lambda: api.list_keyword_groups(dataset_id))
    if isinstance(listing, APIError):
        show_error(listing, "Could not read the keyword groups")
        listing = None
    outcome = state.work(ss)["results"].get(("kw", dataset_id))
    if outcome and "error" in outcome:
        show_error(outcome["error"], outcome["what"])
    elif outcome and "invalid" in outcome:
        st.error(outcome["invalid"])
    elif outcome:
        st.success(outcome["done"])
    _propose(dataset_id, columns, listing)
    groups = (listing or {}).get("groups", [])
    if not groups:
        if listing is not None:
            st.caption("No groups yet: choose the column and propose them.")
        return
    st.subheader("Groups")
    st.caption(kw.summary(groups))
    st.caption("An approval cannot be withdrawn here: API 0.6.0 has no action for it (#20).")
    st.page_link("views/tools.py", label="Tools: keyword group performance, page targeting",
                 icon="🧰", query_params={"sid": ss[state.SID]})
    ids = kw.ticked(state.get_path(draft, p(dataset_id, "ticks"), {}), groups)
    _ticked_actions(dataset_id, groups, ids)
    _edit_one(dataset_id, groups)
    _list(dataset_id, groups)


main()
