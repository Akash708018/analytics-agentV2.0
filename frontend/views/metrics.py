"""Metrics page: the person approves metric templates and validity rules.

Approving a template adds a ratio-of-sums measure to the contract (a new contract version);
approving a rule makes tools filter or flag rows by it. Nothing is approved or ticked for
the person; when the engine needs an answer (which column, which fork), it is asked here.
See docs/steps/F4.md.
"""

import streamlit as st

from frontend import connection, datasets, state
from frontend.api_client import APIError
from frontend.components.bindings import binding_form
from frontend.components.shell import needs_dataset, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]


def _approve(dataset_id: str, template_id: str) -> None:
    held, held_draft = state.work(ss), ss[state.DRAFT]
    bindings = dict(state.get_path(held_draft, ("drafts", dataset_id, "bindings", template_id), {}))
    forks = dict(state.get_path(held_draft, ("drafts", dataset_id, "forks"), {}))
    try:
        reply = api.approve_metric(dataset_id, template_id, bindings, forks)
    except APIError as error:
        held["results"][("metric", dataset_id, template_id)] = {"error": error}
        return
    held["results"][("metric", dataset_id, template_id)] = {"approved": reply}
    datasets.invalidate(held, dataset_id, ("templates", "proposal"))


def _apply_rules(dataset_id: str, approve: list[str], reject: list[str]) -> None:
    held, held_draft = state.work(ss), ss[state.DRAFT]
    try:
        api.approve_validity_rules(dataset_id, approve=approve, reject=reject)
    except APIError as error:
        held["results"][("rules", dataset_id)] = {"error": error}
        return
    held["results"][("rules", dataset_id)] = {"approved": approve, "rejected": reject}
    state.drop_path(held_draft, ("drafts", dataset_id, "rules"))
    datasets.invalidate(held, dataset_id, ("rules",))
    held["reseed"].append(f"ui.rules.{dataset_id}.")


def _tick_rules(dataset_id: str, rule_ids: list[str]) -> None:
    for rule_id in rule_ids:
        ss[f"ui.rules.{dataset_id}.{rule_id}"] = True
        state.set_path(ss[state.DRAFT], ("drafts", dataset_id, "rules", rule_id), True)


def _template_error(dataset_id: str, template: dict, error: APIError, columns: list[str]) -> None:
    extra = error.payload if isinstance(error.payload, dict) else {}
    tid = template["template_id"]
    if error.code == "ambiguous_binding":
        concept = extra.get("concept", "")
        st.warning(f"More than one column could be '{concept}'. Pick the one this metric uses, "
                   "then approve again.")
        key, path = f"ui.metrics.{dataset_id}.{tid}.bind.{concept}", ("drafts", dataset_id, "bindings", tid, concept)
        state.bind(ss, key, path, None)
        st.selectbox(f"Column for {concept}", extra.get("columns", []), key=key,
                     placeholder="choose…", on_change=state.on_change, args=(ss, key, path))
    elif error.code == "forks_unanswered":
        st.warning(f"Answer this first: {error.message}")
        for fork_id in extra.get("missing", []):
            key, path = f"ui.metrics.{dataset_id}.fork.{fork_id}", ("drafts", dataset_id, "forks", fork_id)
            state.bind(ss, key, path, None)
            st.radio(error.message, extra.get("options", []), key=key, label_visibility="collapsed",
                     on_change=state.on_change, args=(ss, key, path))
    elif error.code == "contract_required":
        show_error(error, "Not approved")
        st.page_link("views/contract.py", label="Confirm the contract", icon="📝",
                     query_params={"sid": ss[state.SID]})
    else:
        show_error(error, "Not approved")
        if error.code == "needs_data":
            st.caption("If the data has this under another name, name the column below and "
                       "approve again.")


def _templates(dataset_id: str, columns: list[str]) -> None:
    st.subheader("Metrics")
    reply = datasets.cached(ss, "templates", dataset_id, lambda: api.list_metric_templates(dataset_id))
    if isinstance(reply, APIError):
        show_error(reply, "Could not read the metric templates")
        return
    templates = reply["templates"]
    available = [t for t in templates if t["available"]]
    st.write("Approving a metric adds it to the contract as a ratio of sums (a new contract "
             "version). Tools skip a metric until it is approved.")
    results = state.work(ss)["results"]
    for t in available:
        tid = t["template_id"]
        with st.container(border=True):
            st.markdown(f"**{t['label']}** · `{tid}`")
            st.caption(f"{t['shape']} · uses {', '.join(t['required_concepts']) or 'no concept'}"
                       + (f" · asks: {', '.join(t['forks'])}" if t["forks"] else ""))
            result = results.get(("metric", dataset_id, tid))
            if t.get("approved"):
                st.success(f"Approved: measure `{t.get('measure')}`")
                if result and result.get("approved", {}).get("provisional"):
                    st.warning(f"Provisional: {result['approved']['provisional']}")
                continue
            if result and "error" in result:
                _template_error(dataset_id, t, result["error"], columns)
            binding_form(ss, f"metrics.{dataset_id}.{tid}", ("drafts", dataset_id, "bindings", tid),
                         columns)
            st.button("Approve", key=f"metrics.{dataset_id}.{tid}.approve", type="primary",
                      on_click=_approve, args=(dataset_id, tid))
    unavailable = [t for t in templates if not t["available"]]
    if unavailable:
        with st.expander(f"Not available for this data ({len(unavailable)})"):
            for t in unavailable:
                st.caption(f"{t['label']}: needs {', '.join(t['required_concepts'])}")


def _rules(dataset_id: str) -> None:
    st.subheader("Validity rules")
    reply = datasets.cached(ss, "rules", dataset_id, lambda: api.list_validity_rules(dataset_id))
    if isinstance(reply, APIError):
        show_error(reply, "Could not read the validity rules")
        return
    result = state.work(ss)["results"].get(("rules", dataset_id))
    if result and "error" in result:
        show_error(result["error"], "The rules were not changed")
    elif result:
        st.success("Saved. Approved now: "
                   + (", ".join(r["rule_id"] for r in reply["rules"] if r["approved"]) or "none"))
    st.write("Approved rules filter or flag rows in every tool run. None is ticked for you.")
    approve, reject, unticked = [], [], []
    for rule in reply["rules"]:
        rid = rule["rule_id"]
        key, path = f"ui.rules.{dataset_id}.{rid}", ("drafts", dataset_id, "rules", rid)
        state.bind(ss, key, path, rule["approved"])
        st.checkbox(rule["description"], key=key, on_change=state.on_change, args=(ss, key, path))
        affected = rule["rows_affected"]
        st.caption(f"{rid} · " + ("rows affected: not counted" if affected is None
                                  else f"rows affected: {affected}")
                   + (" · approved" if rule["approved"] else "")
                   + (" · suggested" if rule["suggested"] and not rule["approved"] else ""))
        if ss[key] and not rule["approved"]:
            approve.append(rid)
        if not ss[key] and rule["approved"]:
            reject.append(rid)
        if not ss[key] and rule["suggested"] and not rule["approved"]:
            unticked.append(rid)
    left, right = st.columns(2)
    if unticked:
        left.button(f"Tick the {len(unticked)} suggested", key="rules.suggest",
                    on_click=_tick_rules, args=(dataset_id, unticked))
    right.button(f"Apply ({len(approve)} to approve, {len(reject)} to withdraw)", type="primary",
                 key="rules.apply", disabled=not (approve or reject),
                 on_click=_apply_rules, args=(dataset_id, approve, reject))


def main() -> None:
    st.title("Metrics")
    dataset_id = draft["dataset_id"]
    if dataset_id is None:
        needs_dataset(ss)
        return
    st.caption(datasets.label(ss, api, dataset_id))
    profile = datasets.cached(ss, "profile", dataset_id, lambda: api.get_profile(dataset_id))
    columns = [] if isinstance(profile, APIError) else [c["name"] for c in profile["columns"]]
    _templates(dataset_id, columns)
    _rules(dataset_id)


main()
