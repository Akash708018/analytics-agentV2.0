"""Explore: run any of the engine's core analyses directly, or build a report from a playbook.

API 0.9.0 (#22, B11). Analyses come with their fields, and fields that name a measure, a
dimension, a column or a grain come with the contract's own choices; the others are typed in.
Nothing is chosen for the person: no analysis, no field, no playbook. A report runs the
playbook's steps with no model call. Results show through the shared results view; they are
stored by the service (Results), so only the choices and answers are saved here. See
docs/steps/F10.md.
"""

import streamlit as st

from frontend import connection, datasets, prep, state
from frontend.api_client import APIError
from frontend.components.results import render_result
from frontend.components.shell import needs_dataset, show_error

ss = st.session_state
api = connection.get_client()
draft = ss[state.DRAFT]


def k(dataset_id: str, *parts: str) -> str:
    return ".".join(("ui.explore", dataset_id, *parts))


def p(dataset_id: str, *parts: str) -> tuple[str, ...]:
    return ("drafts", dataset_id, "explore", *parts)


# --- analyses ----------------------------------------------------------------------------------

def _run(dataset_id: str, name: str, fields: list[dict]) -> None:
    # Read the form first; after the POST only held objects change (state.py, C7).
    values = {f["name"]: ss.get(k(dataset_id, "f", name, f["name"])) for f in fields}
    held = state.work(ss)
    try:
        params = prep.analysis_params(fields, values)
    except ValueError as error:
        held["results"][("explore", dataset_id, name)] = {"invalid": str(error)}
        return
    try:
        result = api.run_tool(f"core.{name}", dataset_id, params)
    except APIError as error:
        held["results"][("explore", dataset_id, name)] = {"error": error, "params": params}
        return
    held["results"][("explore", dataset_id, name)] = {"result": result, "params": params}


def _field(dataset_id: str, name: str, field: dict) -> None:
    key, path = k(dataset_id, "f", name, field["name"]), p(dataset_id, "fields", name, field["name"])
    label, kind = prep.field_label(field), field["kind"]
    help_text = prep.FIELD_HELP.get(kind)
    args = (ss, key, path)
    if field.get("choices") is not None:            # the contract's own options, even none
        if not field["choices"]:
            st.caption(f"{label}: the contract names no {kind} to choose.")
            return
        state.bind(ss, key, path, None)
        if ss[key] is not None and ss[key] not in field["choices"]:
            ss[key] = None                          # the contract changed under a saved answer
        st.selectbox(label, field["choices"], key=key, on_change=state.on_change, args=args,
                     placeholder="choose…" if field.get("required") else "the analysis's own choice")
    elif kind == "date":
        state.bind(ss, key, path, None, to_widget=prep.text_date, from_widget=prep.date_text)
        st.date_input(label, key=key, min_value=prep.text_date("1900-01-01"),
                      max_value=prep.text_date("2100-12-31"), format="YYYY-MM-DD", help=help_text,
                      on_change=state.on_change, args=(ss, key, path, prep.date_text))
    elif kind == "integer":
        state.bind(ss, key, path, None)
        st.number_input(label, key=key, step=1, help=help_text, on_change=state.on_change,
                        args=args)
    elif kind == "number":
        state.bind(ss, key, path, None)
        st.number_input(label, key=key, step=0.01, help=help_text, on_change=state.on_change,
                        args=args)
    else:                                           # period, list, text
        state.bind(ss, key, path, "")
        st.text_input(label, key=key, help=help_text, on_change=state.on_change, args=args)


def _analysis_error(error: APIError) -> None:
    extra = error.payload if isinstance(error.payload, dict) else {}
    if error.code == "param_required":
        st.error("Fill in: " + ", ".join(m.replace("_", " ") for m in extra.get("missing", [])))
    elif error.code == "unknown_params":
        st.error(f"Not a field of this analysis: {', '.join(extra.get('unknown', []))}. It takes: "
                 f"{', '.join(extra.get('fields', []))}.")
    else:
        show_error(error, "The analysis did not run")


def _analyses(dataset_id: str) -> None:
    st.subheader("Run an analysis")
    listed = datasets.cached(ss, "analyses", dataset_id, lambda: api.list_analyses(dataset_id))
    if isinstance(listed, APIError):
        if listed.code == "contract_required":
            st.info("Analyses run under the dataset's contract: confirm it first.")
            st.page_link("views/contract.py", label="Contract", icon="📝",
                         query_params={"sid": ss[state.SID]})
        else:
            show_error(listed, "Could not read the analyses")
        return
    specs = {a["name"]: a for a in listed["analyses"]}
    st.caption(f"{len(specs)} analyses, grouped by what they answer. Each runs on the contract "
               "in force, and every figure keeps its source.")
    key, path = k(dataset_id, "analysis"), p(dataset_id, "analysis")
    state.bind(ss, key, path, None)
    if ss[key] not in specs:
        ss[key] = None
    order = sorted(specs, key=lambda n: (specs[n]["tier"], n))
    st.selectbox("Analysis", order, key=key, format_func=lambda n: prep.analysis_label(specs[n]),
                 placeholder="Choose an analysis", on_change=state.on_change, args=(ss, key, path))
    name = ss[key]
    if name is None:
        return
    spec = specs[name]
    st.write(spec["summary"])
    fields = spec["fields"]
    if not fields:
        st.caption("It takes no fields.")
    for field in fields:
        _field(dataset_id, name, field)
    st.button("Run the analysis", type="primary", key="explore.run", on_click=_run,
              args=(dataset_id, name, fields),
              help="Sends only the fields you filled in; a blank optional field is the "
                   "analysis's own choice.")
    outcome = state.work(ss)["results"].get(("explore", dataset_id, name))
    if not outcome:
        return
    if "invalid" in outcome:
        st.error(f"Not run: {outcome['invalid']}.")
    elif "error" in outcome:
        _analysis_error(outcome["error"])
    else:
        st.divider()
        render_result(outcome["result"], key=f"explore.{dataset_id}.{name}.result")


# --- reports -----------------------------------------------------------------------------------

def _build(dataset_id: str, playbook_id: str, slots: list[str]) -> None:
    filled = {s: (ss.get(k(dataset_id, "slot", playbook_id, s)) or "").strip() for s in slots}
    filled = {s: v for s, v in filled.items() if v}
    held = state.work(ss)
    try:
        report = api.run_report(dataset_id, playbook_id, filled)
    except APIError as error:
        held["results"][("report", dataset_id, playbook_id)] = {"error": error}
        return
    held["results"][("report", dataset_id, playbook_id)] = {"report": report}


def _playbooks(dataset_id: str) -> dict[str, dict]:
    """Playbooks of the confirmed domains, read from their packs."""
    detected = datasets.cached(ss, "detect", dataset_id, lambda: api.detect_domains(dataset_id))
    if isinstance(detected, APIError):
        show_error(detected, "Could not read the confirmed domains")
        return {}
    found: dict[str, dict] = {}
    for domain in detected.get("confirmed") or []:
        pack = datasets.cached(ss, "pack", domain, lambda d=domain: api.get_pack(d))
        if isinstance(pack, APIError):
            show_error(pack, f"Could not read the {domain} playbooks")
            continue
        for playbook in pack["pack"].get("playbooks") or []:
            found[playbook["id"]] = {**playbook, "domain": domain}
    return found


def _report(report: dict, dataset_id: str) -> None:
    st.markdown(f"#### {report['description']}")
    st.caption(f"{report['playbook']} · report {report['run_id']} · {len(report['results'])} "
               f"result(s), {len(report.get('skipped') or [])} step(s) skipped · no model call")
    if report.get("rules"):
        st.caption("Rules this playbook holds its reading to: " + ", ".join(report["rules"]))
    for skipped in report.get("skipped") or []:
        st.warning(f"Skipped {skipped.get('tool_id')}: {skipped.get('reason')} "
                   f"({skipped.get('code')})")
    for i, result in enumerate(report["results"]):
        st.divider()
        render_result(result, key=f"report.{dataset_id}.{report['playbook']}.{i}")


def _reports(dataset_id: str) -> None:
    st.subheader("Build a report")
    playbooks = _playbooks(dataset_id)
    if not playbooks:
        st.info("Reports come from the playbooks of a confirmed domain: confirm one on Domain.")
        return
    st.caption("A playbook is a fixed set of tools for one question. It runs in order with no "
               "model call; a step that can't run is skipped and says why.")
    key, path = k(dataset_id, "playbook"), p(dataset_id, "playbook")
    state.bind(ss, key, path, None)
    if ss[key] not in playbooks:
        ss[key] = None
    st.selectbox("Playbook", sorted(playbooks), key=key,
                 format_func=lambda b: f"{playbooks[b]['description']} ({playbooks[b]['domain']})",
                 placeholder="Choose a playbook", on_change=state.on_change, args=(ss, key, path))
    playbook_id = ss[key]
    if playbook_id is None:
        return
    playbook = playbooks[playbook_id]
    steps = [s["tool"] for s in playbook.get("steps", [])]
    st.caption("Steps: " + " → ".join(f"`{t}`" for t in steps))
    needs = [f"approved metric {m}" for m in playbook.get("requires_metrics") or []]
    needs += [f"data for {c}" for c in playbook.get("requires_concepts") or []]
    if needs:
        st.caption("Needs: " + ", ".join(needs))
    slots = playbook.get("slots") or {}
    for slot, what in slots.items():
        skey, spath = k(dataset_id, "slot", playbook_id, slot), p(dataset_id, "slots", playbook_id, slot)
        state.bind(ss, skey, spath, "")
        st.text_input(f"{slot.replace('_', ' ').capitalize()}: {what}", key=skey,
                      on_change=state.on_change, args=(ss, skey, spath))
    st.button("Build the report", type="primary", key="explore.report", on_click=_build,
              args=(dataset_id, playbook_id, list(slots)))
    outcome = state.work(ss)["results"].get(("report", dataset_id, playbook_id))
    if not outcome:
        return
    if "report" in outcome:
        _report(outcome["report"], dataset_id)
        return
    error = outcome["error"]
    extra = error.payload if isinstance(error.payload, dict) else {}
    if error.code == "playbook_blocked":
        st.error("Not built: this data needs " + "; ".join(extra.get("missing", [])) + ".")
        if extra.get("recovery"):
            st.info(extra["recovery"])
    elif error.code == "param_required":
        st.error("Fill in: " + ", ".join(extra.get("missing", [])))
    else:
        show_error(error, "The report was not built")


def main() -> None:
    st.title("Explore")
    dataset_id = draft["dataset_id"]
    if dataset_id is None:
        needs_dataset(ss)
        return
    st.caption(datasets.label(ss, api, dataset_id))
    _analyses(dataset_id)
    st.divider()
    _reports(dataset_id)


main()
