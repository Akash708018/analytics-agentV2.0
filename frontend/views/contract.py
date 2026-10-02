"""Contract page: the person states what each column is and how it adds up.

The form starts from the engine's proposal (fields it is unsure of come back blank),
the person's saved draft wins over it, and nothing is confirmed until the button.
Aggregations and fork answers are never preselected: suggestions are shown with
their reasons, and "Use the suggested…" buttons are explicit clicks. The confirm
reply's 422s are shown as the questions they carry. See docs/steps/F3.md.
"""

import streamlit as st

from frontend import connection, datasets, prep, state
from frontend.api_client import APIError
from frontend.components.shell import needs_dataset, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]
ROLE_LABELS = {"date": "Date", "measure": "Measure (a number)", "dimension": "Dimension (a group)",
               "ignore": "Ignore"}


def k(dataset_id: str, *parts: str) -> str:
    return ".".join(("ui.contract", dataset_id, *parts))


def p(dataset_id: str, *parts: str) -> tuple[str, ...]:
    return ("drafts", dataset_id, "contract", *parts)


def _set(dataset_id: str, key: str, path: tuple[str, ...], value) -> None:
    ss[key] = value
    state.set_path(ss[state.DRAFT], path, value)


def _use_strong(dataset_id: str, picks: dict[str, str]) -> None:
    for measure, agg in picks.items():
        _set(dataset_id, k(dataset_id, "agg", measure), p(dataset_id, "aggregations", measure), agg)


def _use_suggested_forks(dataset_id: str, picks: dict[str, str]) -> None:
    for fork_id, option in picks.items():
        _set(dataset_id, k(dataset_id, "fork", fork_id), ("drafts", dataset_id, "forks", fork_id),
             option)


def _start_over(dataset_id: str) -> None:
    held_draft = ss[state.DRAFT]
    for part in ("contract", "forks"):
        state.drop_path(held_draft, ("drafts", dataset_id, part))
    state.work(ss)["reseed"].append(f"ui.contract.{dataset_id}.")
    datasets.invalidate(state.work(ss), dataset_id, ("proposal",))


def _confirm(dataset_id: str, columns: list[str], measures: list[str], fork_ids: list[str]) -> None:
    # Read every value first; after the POST only held objects change (state.py, C7).
    form = {
        "grain": ss.get(k(dataset_id, "grain")),
        "key": ss.get(k(dataset_id, "key")),
        "roles": {c: ss.get(k(dataset_id, "role", c)) for c in columns},
        "aggregations": {m: ss.get(k(dataset_id, "agg", m)) for m in measures},
        "definitions": {m: ss.get(k(dataset_id, "def", m)) for m in measures},
        "per": {m: ss.get(k(dataset_id, "per", m)) for m in measures},
        "window_start": prep.date_text(ss.get(k(dataset_id, "from"))),
        "window_end": prep.date_text(ss.get(k(dataset_id, "to"))),
        "caveats": ss.get(k(dataset_id, "caveats")),
        "forks": {f: ss.get(k(dataset_id, "fork", f)) for f in fork_ids},
    }
    held, held_draft = state.work(ss), ss[state.DRAFT]
    contract, choices = prep.contract_body(form)
    try:
        reply = api.confirm_contract(dataset_id, contract, choices)
    except APIError as error:
        held["results"][("contract", dataset_id)] = {"error": error}
        return
    held["results"][("contract", dataset_id)] = {"confirmed": reply}
    state.dataset_draft(held_draft, dataset_id)["confirmed_version"] = reply["version"]
    datasets.invalidate(held, dataset_id, ("proposal",))   # it now reflects the contract in force


def _result(dataset_id: str, forks: list[dict]) -> None:
    result = state.work(ss)["results"].get(("contract", dataset_id))
    if not result:
        return
    if "confirmed" in result:
        st.success(f"Contract confirmed (version {result['confirmed']['version']}). "
                   "Analyses on this dataset now run under it.")
        return
    error = result["error"]
    extra = error.payload if isinstance(error.payload, dict) else {}
    if error.code == "forks_unanswered":
        questions = {f["fork_id"]: f["question"] for f in forks}
        st.error("Not confirmed: every question below needs your answer; none is defaulted.")
        for fork_id in extra.get("missing", []):
            st.markdown(f"- {questions.get(fork_id, fork_id)}")
        for bad in extra.get("invalid", []):
            st.markdown(f"- Not a valid answer: `{bad}`")
    elif error.code == "contract_provisional":
        st.error("Not confirmed: these still need an answer.")
        for question in extra.get("questions", []):
            st.markdown(f"- {question}")
        st.caption("Fields: " + ", ".join(extra.get("provisional", [])))
    else:
        show_error(error, "Not confirmed")


def _roles(dataset_id: str, proposal: dict, profile: dict) -> dict[str, str]:
    st.subheader("What each column is")
    columns = [c["name"] for c in profile["columns"]]
    defaults = prep.default_roles(proposal, columns)
    grid = st.columns(3)
    for i, col in enumerate(profile["columns"]):
        name = col["name"]
        key, path = k(dataset_id, "role", name), p(dataset_id, "roles", name)
        state.bind(ss, key, path, defaults[name])
        sample = ", ".join(map(str, col["sample"]))
        grid[i % 3].selectbox(
            name, state.ROLES, key=key, format_func=ROLE_LABELS.get,
            help=f"{col['type']} · {col['distinct']} distinct · null {col['null_pct']}% · "
                 f"e.g. {sample}",
            on_change=state.on_change, args=(ss, key, path))
    key, path = k(dataset_id, "key"), p(dataset_id, "key")
    state.bind(ss, key, path, [c for c in proposal.get("key", []) if c in columns])
    st.multiselect("Which columns together identify one row?", columns, key=key,
                   placeholder="choose the key columns",
                   help="Usually the date plus the groups the grain names: for one row per "
                        "campaign per day, date and campaign. The engine checks it is unique.",
                   on_change=state.on_change, args=(ss, key, path))
    return {c: ss[k(dataset_id, "role", c)] for c in columns}


def _measures(dataset_id: str, proposal: dict, columns: list[str], measures: list[str]) -> None:
    if not measures:
        st.info("No column is a measure yet: pick at least one above.")
        return
    st.subheader("Each measure")
    suggested = prep.suggestions(proposal)
    in_force = prep.proposal_aggs(proposal)
    strong = {m: s["suggested_agg"] for m, s in suggested.items()
              if m in measures and s.get("strength") == "strong"
              and not ss.get(k(dataset_id, "agg", m)) and s["suggested_agg"] in prep.AGGREGATIONS}
    if strong:
        st.button(f"Use the engine's {len(strong)} strong suggestion(s)", key="contract.strong",
                  on_click=_use_strong, args=(dataset_id, strong),
                  help="Fills only aggregations you have not answered. Definitions stay yours.")
    for m in measures:
        with st.container(border=True):
            st.markdown(f"**{m}**")
            key, path = k(dataset_id, "agg", m), p(dataset_id, "aggregations", m)
            state.bind(ss, key, path, in_force.get(m))
            options = list(prep.AGGREGATIONS)
            if ss[key] and ss[key] not in options:
                options.append(ss[key])
            st.selectbox("How it combines across rows", options, key=key,
                         placeholder="choose…", on_change=state.on_change, args=(ss, key, path),
                         help="'none' means per row only: a price or a rate.")
            if m in suggested:
                s = suggested[m]
                st.caption(f"Engine suggests {s['suggested_agg']} ({s.get('strength') or 'unsure'})"
                           f": {s.get('reason', '')}")
            key, path = k(dataset_id, "def", m), p(dataset_id, "definitions", m)
            state.bind(ss, key, path, "")
            st.text_input("What it means (required)", key=key, max_chars=500,
                          placeholder="e.g. spend as billed by the ad platform, before GST",
                          on_change=state.on_change, args=(ss, key, path))
            key, path = k(dataset_id, "per", m), p(dataset_id, "per", m)
            state.bind(ss, key, path, [])
            st.multiselect("One value per (optional)", [c for c in columns if c != m], key=key,
                           help="If this value repeats on every row of a larger unit (an order "
                                "total on each line), name the unit so it is counted once.",
                           on_change=state.on_change, args=(ss, key, path))


def _window_and_caveats(dataset_id: str, proposal: dict) -> None:
    st.subheader("Period and caveats")
    left, right = st.columns(2)
    for column, side, label in ((left, "from", "Analysis window from"),
                                (right, "to", "Analysis window to")):
        key, path = k(dataset_id, side), p(dataset_id, f"window_{'start' if side == 'from' else 'end'}")
        state.bind(ss, key, path, None, to_widget=prep.text_date, from_widget=prep.date_text)
        column.date_input(label, key=key, min_value=prep.text_date("1900-01-01"),
                          max_value=prep.text_date("2100-12-31"), format="YYYY-MM-DD",
                          on_change=state.on_change, args=(ss, key, path, prep.date_text))
    if proposal.get("caveats"):
        st.markdown("**What the engine found** (kept with the contract by the engine)")
        for caveat in proposal["caveats"]:
            st.caption(caveat)
    key, path = k(dataset_id, "caveats"), p(dataset_id, "caveats")
    state.bind(ss, key, path, "")
    st.text_area("Your caveats, one per line (optional)", key=key, max_chars=2000,
                 help="A count you type here is checked against the data.",
                 on_change=state.on_change, args=(ss, key, path))


def _forks(dataset_id: str, forks: list[dict]) -> None:
    if not forks:
        return
    st.subheader("Questions only you can answer")
    st.caption("None is answered for you. A suggestion and its reason are shown where the "
               "engine has one.")
    suggestions = {f["fork_id"]: f["suggested"] for f in forks
                   if f.get("suggested") and not ss.get(k(dataset_id, "fork", f["fork_id"]))}
    if suggestions:
        st.button(f"Use the {len(suggestions)} suggested answer(s)", key="contract.forks",
                  on_click=_use_suggested_forks, args=(dataset_id, suggestions),
                  help="Fills only questions you have not answered.")
    for fork in forks:
        key, path = k(dataset_id, "fork", fork["fork_id"]), ("drafts", dataset_id, "forks", fork["fork_id"])
        state.bind(ss, key, path, None)
        labels = {o["id"]: o["label"] for o in fork["options"]}
        st.radio(fork["question"], list(labels), key=key, format_func=labels.get,
                 on_change=state.on_change, args=(ss, key, path))
        if fork.get("suggested"):
            st.caption(f"Suggested: {labels.get(fork['suggested'], fork['suggested'])}"
                       f" — {fork.get('suggested_reason') or ''}")


def main() -> None:
    st.title("Contract")
    dataset_id = draft["dataset_id"]
    if dataset_id is None:
        needs_dataset(ss)
        return
    st.caption(datasets.label(ss, api, dataset_id))
    proposal = datasets.cached(ss, "proposal", dataset_id,
                               lambda: api.get_contract_proposal(dataset_id))
    profile = datasets.cached(ss, "profile", dataset_id, lambda: api.get_profile(dataset_id))
    for reply in (proposal, profile):
        if isinstance(reply, APIError):
            show_error(reply, "Could not read the contract proposal")
            return
    forks = proposal["forks"]
    _result(dataset_id, forks)
    version = state.get_path(draft, ("drafts", dataset_id, "confirmed_version"))
    if version is not None:
        st.caption(f"Confirmed from this session: version {version}. Changing the form and "
                   "confirming again makes a new version.")
    if proposal.get("questions"):
        with st.expander(f"The engine's open questions ({len(proposal['questions'])})",
                         expanded=version is None):
            for question in proposal["questions"]:
                st.markdown(f"- {question}")
    key, path = k(dataset_id, "grain"), p(dataset_id, "grain")
    state.bind(ss, key, path, proposal.get("grain") or "")
    st.text_input("One row of this table is…", key=key, max_chars=300,
                  placeholder="e.g. one row per campaign per day",
                  on_change=state.on_change, args=(ss, key, path))
    roles = _roles(dataset_id, proposal, profile)
    dates = prep.date_columns(roles)
    if len(dates) > 1:
        st.error(f"Choose one date column; {', '.join(dates)} are all marked Date.")
    columns = list(roles)
    measures = [c for c, r in roles.items() if r == "measure"]
    _measures(dataset_id, proposal, columns, measures)
    _window_and_caveats(dataset_id, proposal)
    _forks(dataset_id, forks)
    st.divider()
    left, right = st.columns(2)
    left.button("Confirm the contract", type="primary", key="contract.confirm",
                disabled=len(dates) > 1, on_click=_confirm,
                args=(dataset_id, columns, measures, [f["fork_id"] for f in forks]),
                help="Nothing is confirmed until you press this. The engine checks every "
                     "answer against the data.")
    right.button("Start over from the engine's proposal", key="contract.reset",
                 on_click=_start_over, args=(dataset_id,),
                 help="Drops this form's saved answers for this dataset.")


main()
