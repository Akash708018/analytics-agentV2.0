"""Domain page: the person confirms what kind of data this is. Never guessed.

Every pack but core is offered, marketing first (the priority domain). Detection
evidence is shown as returned; ticks start from the server's `confirmed` list, which
is the person's own earlier choice, and nothing is ticked from a score.
"""

import streamlit as st

from frontend import connection, datasets, state
from frontend.api_client import APIError
from frontend.components.shell import needs_dataset, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]


def _key(dataset_id: str, domain: str) -> str:
    return f"ui.domain.{dataset_id}.{domain}"


def _confirm(dataset_id: str, chosen: list[str]) -> None:
    held, held_draft = state.work(ss), ss[state.DRAFT]
    try:
        api.confirm_domains(dataset_id, chosen)
    except APIError as error:
        held["results"][("domain", dataset_id)] = {"error": error}
        return
    held["results"][("domain", dataset_id)] = {"confirmed": chosen}
    state.drop_path(held_draft, ("drafts", dataset_id, "domains"))
    datasets.invalidate(held, dataset_id, ("detect", "proposal"))   # forks follow domains
    held["reseed"] += [f"ui.domain.{dataset_id}.", f"ui.contract.{dataset_id}."]


def _order(packs: list[dict]) -> list[str]:
    ids = [p["pack_id"] for p in packs if p["pack_id"] != "core"]
    return sorted(ids, key=lambda pack_id: (pack_id != "marketing", pack_id))


def main() -> None:
    st.title("Domain")
    dataset_id = draft["dataset_id"]
    if dataset_id is None:
        needs_dataset(ss)
        return
    st.caption(datasets.label(ss, api, dataset_id))
    result = state.work(ss)["results"].get(("domain", dataset_id))
    if result and "error" in result:
        show_error(result["error"], "The domains were not confirmed")
    elif result:
        st.success("Confirmed: " + (", ".join(result["confirmed"]) or "no domain") + ". "
                   "Its questions are now on the Contract page.")
    packs = datasets.cached(ss, "packs", None, api.list_packs)
    detection = datasets.cached(ss, "detect", dataset_id, lambda: api.detect_domains(dataset_id))
    for error in (packs, detection):
        if isinstance(error, APIError):
            show_error(error, "Could not read the domains")
            return
    confirmed = detection["confirmed"]
    evidence = {d["domain"]: d["evidence"] for d in detection["domains"]}
    st.write("Tick every domain this data belongs to. Confirming one turns on its tools and "
             "adds its questions to the contract. Nothing is ticked for you.")
    st.caption("Confirmed now: " + (", ".join(confirmed) or "none"))
    chosen = []
    for domain in _order(packs["packs"]):
        key, path = _key(dataset_id, domain), ("drafts", dataset_id, "domains", domain)
        state.bind(ss, key, path, domain in confirmed)
        st.checkbox(domain.capitalize(), key=key, on_change=state.on_change, args=(ss, key, path))
        found = evidence.get(domain)
        if found:
            st.caption(f"Detected · score {found['score']} · matched columns: "
                       f"{', '.join(found['matched_columns'])}")
        else:
            st.caption("No column matched this domain's vocabulary.")
        for source in detection.get("sources", []):
            if source.get("domain") == domain:
                st.caption(f"Looks like a {source['source']} export · score "
                           f"{source['evidence']['score']} · "
                           f"{', '.join(source['evidence']['matched_columns'])}")
        if ss[key]:
            chosen.append(domain)
    st.button("Confirm domains", type="primary", key="domain.confirm",
              disabled=sorted(chosen) == sorted(confirmed),
              on_click=_confirm, args=(dataset_id, chosen),
              help="Nothing changes until you press this.")


main()
