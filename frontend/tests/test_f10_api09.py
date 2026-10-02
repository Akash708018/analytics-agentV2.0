"""F10: API 0.8.0 and 0.9.0 in the screens. The fake answers in the shapes probed on the real
backend (docs/steps/F10.md): upload answers, the contract in force, cleaning losses, withdraw,
declared optional params, Explore (core analyses) and reports (playbooks)."""

import datetime as dt

import httpx
import pytest

from frontend import prep
from frontend.tests.fake_backend import FakeBackend
from frontend.tests.test_f2_app import DS, fake, open_app, text, with_data  # noqa: F401
from frontend.tests.test_f4_app import CONTRACT, ready
from frontend.tests.test_f6_keywords import actions, gid, proposed

ID = DS["dataset_id"]
WS = "ws_0123456789ab"
NAMES = ["order_id", "order_date", "region", "channel", "units", "revenue"]


# --- helpers ---------------------------------------------------------------------------------

def test_upload_answers_send_only_what_is_filled_and_the_guess_is_only_offered():
    guess = {"header_rows": [1, 2], "header_join": "bottom_only", "data_start_row": 3,
             "footer_skip_rows": 0, "name": "multiheader",
             "columns": [{"source": "order_id", "target": "order_id", "type": None}]}
    assert prep.guess_answers(guess) == {
        "header_rows": [1, 2], "header_join": "bottom_only", "data_start": 3, "footer_rows": 0,
        "name": "multiheader", "columns": {"order_id": ("order_id", None)}}
    assert prep.upload_answers({}) == {}
    assert prep.upload_answers({
        "sheet": None, "header_rows": [2, 1], "header_join": None, "data_start": None,
        "footer_rows": 0, "name": "  ", "columns": {"a": ("", None), "b": (" units ", "BIGINT")},
    }) == {"header_rows": [1, 2], "footer_rows": 0,
           "columns": [{"source": "b", "target": "units", "type": "BIGINT"}]}


def test_analysis_fields_are_sent_typed_and_only_when_filled():
    fields = [{"name": "measure", "kind": "measure"}, {"name": "n", "kind": "integer"},
              {"name": "threshold", "kind": "number"}, {"name": "groups", "kind": "list"},
              {"name": "before_start", "kind": "date"}, {"name": "period", "kind": "period"}]
    assert prep.analysis_params(fields, {
        "measure": "cost", "n": 5.0, "threshold": 0.8, "groups": " brand, generic ,",
        "before_start": dt.date(2026, 7, 1), "period": " "}) == {
        "measure": "cost", "n": 5, "threshold": 0.8, "groups": ["brand", "generic"],
        "before_start": "2026-07-01"}
    with pytest.raises(ValueError):
        prep.analysis_params(fields, {"n": 2.5})
    assert prep.analysis_label({"name": "group_compare", "tier": 2}) == "Comparative · group compare"
    assert prep.field_label({"name": "second_dimension", "required": False}) == \
        "Second dimension (optional)"


def test_a_declared_optional_param_says_what_a_blank_means():
    assert prep.optional_help({"help": "Open at least this many days before as_of",
                               "default": "3"}) == \
        "Open at least this many days before as_of. Blank: the tool uses 3."
    assert prep.optional_help({"help": "", "default": None}) == "Blank: the engine's choice."
    assert prep.coerce("as_of", dt.date(2026, 9, 30)) == "2026-09-30"      # a date widget's value


# --- Data: a refused upload is answered (#16) --------------------------------------------------

def answers(fake):
    return fake.bodies("POST", f"/workspaces/{WS}/uploads/multiheader.csv/answers")


def test_a_refused_upload_is_answered_from_a_blank_form_and_joins_the_session(fake):
    sid = fake.new_session()
    app = open_app(sid, page="views/data.py")
    app.file_uploader(key="data.file").upload("multiheader.csv", b"Identifiers,,\n", "text/csv").run()
    app.button(key="data.upload").click().run()
    assert not app.exception
    page = text(app)
    assert "multiheader.csv is not loaded yet: the reader needs your answers" in page
    assert "Is row 1 part of the header, or a title?" in page
    assert "The first 4 rows as the file holds them, numbered as in the file." in page
    form = "data.answers.multiheader.csv"
    assert app.multiselect(key=f"{form}.header_rows").value == []           # nothing assumed
    assert app.multiselect(key=f"{form}.header_rows").options == ["1", "2", "3", "4"]
    assert app.selectbox(key=f"{form}.header_join").value is None
    assert app.number_input(key=f"{form}.data_start").value is None
    assert app.text_input(key=f"{form}.name").value == ""
    assert app.text_input(key=f"{form}.col.0.target").value == ""

    # A partial answer is sent as it is; the reader asks again and the form keeps it.
    app.text_input(key=f"{form}.name").input("orders").run()
    app.button(key=f"{form}.load").click().run()
    assert answers(fake) == [{"name": "orders"}]
    assert "Your answers were sent (name); the reader still asks:" in text(app)

    # The guess fills only the blanks; the name typed stays.
    app.button(key=f"{form}.guess").click().run()
    assert app.multiselect(key=f"{form}.header_rows").value == [1, 2]
    assert app.selectbox(key=f"{form}.header_join").value == "bottom_only"
    assert app.number_input(key=f"{form}.data_start").value == 3
    assert app.text_input(key=f"{form}.name").value == "orders"
    assert answers(fake) == [{"name": "orders"}]                          # filling sends nothing
    app.button(key=f"{form}.load").click().run()
    assert answers(fake)[-1] == {
        "header_rows": [1, 2], "header_join": "bottom_only", "data_start": 3, "footer_rows": 0,
        "name": "orders", "columns": [{"source": n, "target": n} for n in NAMES]}
    assert not app.exception
    assert "Loaded multiheader.csv with your answers." in text(app)
    saved = fake.sessions[sid]["ui_state"]
    loaded = saved["dataset_id"]
    assert loaded in saved["datasets"] and fake.datasets[loaded]["name"] == "orders"
    assert "**orders**" in text(app)
    assert not [w for w in app.text_input if w.key.startswith(form)]       # the form is gone


# --- Contract: the contract in force (#17) -----------------------------------------------------

def test_the_contract_in_force_shows_its_version_and_both_caveat_lists(fake):
    sid = ready(fake)
    fake._prep(ID)["metrics"]["ctr"] = "ctr"
    app = open_app(sid, page="views/contract.py")
    assert not app.exception
    panel = next(e for e in app.expander if e.label.startswith("The contract in force"))
    assert panel.label == "The contract in force: version 1, confirmed 2026-10-02T09:00:00+00:00"
    page = text(app)
    assert "- One row: one row per campaign per day" in page
    assert "**Your caveats**" in page and "• none declared" in page
    assert "• utm_source writes 3 value(s) more than one way" in page       # the engine's, apart
    assert "Your answers: conversion_source = backend_orders" in page
    assert "Approved metrics: ctr → ctr" in page
    table = app.dataframe[0].value
    assert list(table["measure"]) == ["cost", "ctr"] and list(table["combines as"]) == ["sum", "none"]


def test_before_a_confirm_there_is_no_contract_in_force(fake):
    app = open_app(with_data(fake), page="views/contract.py")
    assert not app.exception
    assert "No contract confirmed yet." in text(app)
    assert not [e for e in app.expander if e.label.startswith("The contract in force")]


# --- Clean: what a step loses (#21) -------------------------------------------------------------

def test_a_lossy_step_says_what_it_loses_with_the_engines_samples_and_sql(fake):
    app = open_app(with_data(fake), page="views/clean.py")
    assert not app.exception
    page = text(app)
    assert "Loses 1 row(s)." in page and "Loses 3 distinct value(s)." in page
    assert "Loses 0" not in page                                           # lossless: no line
    assert "Values it changes (examples): `Google`, `GOOGLE`, `google`" in page
    duplicated = app.dataframe[0].value
    assert list(duplicated["campaign"]) == ["brand"] and list(duplicated["copies"]) == ["2"]
    assert 'CREATE OR REPLACE TABLE "ads" AS SELECT DISTINCT * FROM "ads"' in [c.value for c in app.code]


# --- Keyword groups: withdraw an approval (#20) -------------------------------------------------

def test_withdraw_sends_unapprove_for_the_ticked_approved_groups_only(fake):
    sid, app = proposed(fake)
    a, b = gid(fake, "sushi delivery"), gid(fake, "sushi · near me")
    app.checkbox(key=f"ui.kw.{ID}.tick.{a}").check().run()
    assert app.button(key=f"ui.kw.{ID}.withdraw").disabled                  # nothing approved yet
    app.button(key=f"ui.kw.{ID}.approve").click().run()
    app.checkbox(key=f"ui.kw.{ID}.tick.{a}").check().run()
    app.checkbox(key=f"ui.kw.{ID}.tick.{b}").check().run()
    withdraw = app.button(key=f"ui.kw.{ID}.withdraw")
    assert withdraw.label == "Withdraw approval of the 1 ticked" and not withdraw.disabled
    withdraw.click().run()
    assert actions(fake)[-1] == {"action": "unapprove", "group_ids": [a]}
    assert "Withdrew the approval of 1 group(s); they are proposals again." in text(app)
    assert not fake._prep(ID)["kw"][a]["approved"]
    assert "ticks" not in fake.sessions[sid]["ui_state"]["drafts"][ID]["keywords"]


# --- Tools: declared optional params (#23) ------------------------------------------------------

def logistics_tool(fake, tool):
    sid = with_data(fake, page="tools")
    state = fake._prep(ID)
    state["domains"], state["contract"], state["version"] = ["logistics"], CONTRACT, 1
    app = open_app(sid, page="views/tools.py")
    app.selectbox(key=f"ui.tools.{ID}.tool").select(tool).run()
    return app


def test_optional_params_come_from_the_packs_declaration_with_their_help(fake):
    app = logistics_tool(fake, "logistics.sla_drivers")
    focus = app.text_input(key=f"ui.tools.{ID}.logistics.sla_drivers.p.focus")
    assert focus.label == "Focus on (optional)" and focus.value == ""
    assert focus.help == ("Which hub, courier or zone to look inside; spellings and aliases are "
                          "matched. Blank: the engine's choice.")
    app = logistics_tool(fake, "logistics.stuck_shipments")
    days = app.number_input(key=f"ui.tools.{ID}.logistics.stuck_shipments.p.days")
    assert days.label == "Days (optional)" and days.value is None
    assert days.help.endswith("Blank: the tool uses 3.")
    app.text_input(key=f"ui.tools.{ID}.logistics.stuck_shipments.p.as_of").input("2026-09-30").run()
    app.button(key="tools.run").click().run()
    assert fake._prep(ID)["runs"][-1] == ("logistics.stuck_shipments", {"as_of": "2026-09-30"})
    days.set_value(5).run()
    app.button(key="tools.run").click().run()
    sent = fake._prep(ID)["runs"][-1][1]
    assert sent == {"as_of": "2026-09-30", "days": 5} and isinstance(sent["days"], int)


# --- Explore: core analyses (#22) ---------------------------------------------------------------

def explore(fake, **kw):
    sid = ready(fake, **kw)
    return sid, open_app(sid, page="views/explore.py")


def core_runs(fake):
    return [(t, p) for t, p in fake._prep(ID)["runs"] if t.startswith("core.")]


def f(analysis, field):
    return f"ui.explore.{ID}.f.{analysis}.{field}"


def test_explore_lists_every_analysis_by_tier_and_chooses_none(fake):
    _, app = explore(fake)
    assert not app.exception
    box = app.selectbox(key=f"ui.explore.{ID}.analysis")
    assert box.value is None and len(box.options) == 27
    assert box.options[0] == "Descriptive · cross tab" and box.options[-1] == "Cohort · repeat behaviour"
    assert "27 analyses, grouped by what they answer." in text(app)
    assert core_runs(fake) == []


def test_a_run_sends_exactly_the_filled_fields_and_shows_the_result(fake):
    sid, app = explore(fake)
    app.selectbox(key=f"ui.explore.{ID}.analysis").select("group_compare").run()
    measure = app.selectbox(key=f("group_compare", "measure"))
    assert measure.value is None and measure.options == ["cost", "ctr"]    # the contract's measures
    assert app.selectbox(key=f("group_compare", "dimension")).options == ["campaign"]
    app.button(key="explore.run").click().run()
    assert core_runs(fake) == [("core.group_compare", {})]
    assert "Fill in: dimension, measure" in text(app)
    app.selectbox(key=f("group_compare", "dimension")).select("campaign").run()
    app.selectbox(key=f("group_compare", "measure")).select("cost").run()
    app.text_input(key=f("group_compare", "groups")).input("brand, generic").run()
    app.button(key="explore.run").click().run()
    assert core_runs(fake)[-1] == ("core.group_compare", {"dimension": "campaign", "measure": "cost",
                                                          "groups": ["brand", "generic"]})
    assert not app.exception
    page = text(app)
    assert "#### group_compare: 1 step(s) run" in page
    assert "core.group_compare · " + ID in page                              # the shared view
    assert fake.sessions[sid]["ui_state"]["drafts"][ID]["explore"] == {
        "analysis": "group_compare",
        "fields": {"group_compare": {"dimension": "campaign", "measure": "cost",
                                     "groups": "brand, generic"}}}
    again = open_app(sid, page="views/explore.py")                           # a refresh
    assert again.selectbox(key=f"ui.explore.{ID}.analysis").value == "group_compare"
    assert again.selectbox(key=f("group_compare", "measure")).value == "cost"


def test_dates_and_whole_numbers_are_sent_typed(fake):
    _, app = explore(fake)
    app.selectbox(key=f"ui.explore.{ID}.analysis").select("ranking_shift").run()
    app.date_input(key=f("ranking_shift", "before_start")).set_value(dt.date(2026, 7, 1)).run()
    app.button(key="explore.run").click().run()
    assert core_runs(fake)[-1] == ("core.ranking_shift", {"before_start": "2026-07-01"})
    assert "Fill in: dimension, measure, before end, after start, after end" in text(app)
    app.selectbox(key=f"ui.explore.{ID}.analysis").select("top_n").run()
    app.selectbox(key=f("top_n", "dimension")).select("campaign").run()
    app.selectbox(key=f("top_n", "measure")).select("cost").run()
    app.number_input(key=f("top_n", "n")).set_value(5).run()
    app.selectbox(key=f("top_n", "grain")).select("month").run()
    app.text_input(key=f("top_n", "period")).input("2026-07").run()
    app.button(key="explore.run").click().run()
    sent = core_runs(fake)[-1][1]
    assert sent == {"dimension": "campaign", "measure": "cost", "n": 5, "period": "2026-07",
                    "grain": "month"} and isinstance(sent["n"], int)


def test_an_unknown_field_reply_names_the_fields(fake):
    _, app = explore(fake)
    app.selectbox(key=f"ui.explore.{ID}.analysis").select("group_compare").run()
    fake.ANALYSES = [a if a[0] != "group_compare" else (*a[:3], "dimension:dimension:req "
                                                         "measure:measure:req")
                     for a in FakeBackend.ANALYSES]                     # the engine changed
    app.selectbox(key=f("group_compare", "dimension")).select("campaign").run()
    app.selectbox(key=f("group_compare", "measure")).select("cost").run()
    app.text_input(key=f("group_compare", "groups")).input("brand").run()
    app.button(key="explore.run").click().run()
    assert "Not a field of this analysis: groups. It takes: dimension, measure." in text(app)


def test_without_a_contract_explore_asks_for_one(fake):
    app = open_app(with_data(fake), page="views/explore.py")
    assert not app.exception
    assert "Analyses run under the dataset's contract: confirm it first." in text(app)


# --- Explore: reports (#22) ---------------------------------------------------------------------

def test_a_report_shows_every_result_and_the_skipped_steps(fake):
    sid, app = explore(fake)
    box = app.selectbox(key=f"ui.explore.{ID}.playbook")
    assert box.value is None
    assert sorted(box.options) == ["Where ad spend buys nothing. (marketing)",
                                   "Why ROAS fell between two months. (marketing)"]  # marketing only
    box.select("where_is_spend_wasted").run()
    assert "Steps: `marketing.channel_efficiency` → `marketing.creative_fatigue`" in text(app)
    app.button(key="explore.report").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/reports") == [
        {"playbook": "where_is_spend_wasted", "slots": {}}]
    assert not app.exception
    page = text(app)
    assert "#### Where ad spend buys nothing." in page
    assert "1 result(s), 1 step(s) skipped · no model call" in page
    assert "Skipped marketing.creative_fatigue: marketing.creative_fatigue cannot run (needs_data)" in page
    assert "#### Which channels pay back: 1 step(s) run" in page
    assert "Rules this playbook holds its reading to: no_sum_of_rate" in page


def test_a_blocked_report_says_what_is_missing_and_how_to_recover(fake):
    sid, app = explore(fake)
    app.selectbox(key=f"ui.explore.{ID}.playbook").select("why_roas_dropped").run()
    assert "Needs: approved metric roas" in text(app)
    period = app.text_input(key=f"ui.explore.{ID}.slot.why_roas_dropped.period")
    assert period.label == "Period: the later month, YYYY-MM" and period.value == ""
    period.input("2026-09").run()
    app.button(key="explore.report").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/reports")[-1] == {
        "playbook": "why_roas_dropped", "slots": {"period": "2026-09"}}
    page = text(app)
    assert "Not built: this data needs the approved metric 'roas'." in page
    assert "Approve the ROAS metric on Metrics, then build the report again." in page
    fake._prep(ID)["metrics"]["roas"] = "roas"
    app.button(key="explore.report").click().run()
    assert "Fill in: baseline" in text(app)
    assert fake.sessions[sid]["ui_state"]["drafts"][ID]["explore"]["slots"] == {
        "why_roas_dropped": {"period": "2026-09"}}


def test_without_a_confirmed_domain_there_is_no_playbook(fake):
    sid = with_data(fake)
    fake._prep(ID)["contract"], fake._prep(ID)["version"] = CONTRACT, 1
    app = open_app(sid, page="views/explore.py")
    assert "Reports come from the playbooks of a confirmed domain" in text(app)


# --- the fake's replies carry the spec's required keys -------------------------------------------

def test_fake_replies_carry_the_specs_required_keys(contract):
    fake = FakeBackend()
    did = fake.add_dataset(WS, "ads")["dataset_id"]
    state = fake._prep(did)
    state["domains"], state["contract"], state["version"] = ["marketing"], CONTRACT, 1
    state["fork_choices"] = {"conversion_source": "backend_orders"}
    with httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(fake.handle)) as http:
        in_force = http.get(f"/datasets/{did}/contract").json()
        listing = http.get(f"/datasets/{did}/analyses").json()
        report = http.post(f"/datasets/{did}/reports", json={"playbook": "where_is_spend_wasted",
                                                              "slots": {}}).json()
        proposals = http.get(f"/datasets/{did}/cleaning/proposals").json()["proposals"]
        loaded = http.post(f"/workspaces/{WS}/uploads/multiheader.csv/answers",
                           json={"header_rows": [1, 2]})
    schemas = contract["components"]["schemas"]
    required = lambda name: set(schemas[name].get("required", []))  # noqa: E731
    assert required("ContractInForce") <= set(in_force)
    assert all(required("ContractMeasure") <= set(m) for m in in_force["measures"])
    assert required("AnalysisList") <= set(listing) and len(listing["analyses"]) == 27
    assert all(required("AnalysisSpec") <= set(a) for a in listing["analyses"])
    assert all(required("AnalysisField") <= set(x) for a in listing["analyses"] for x in a["fields"])
    assert required("Report") <= set(report)
    assert all(required("ToolResult") <= set(r) for r in report["results"])
    assert all(required("CleaningProposal") <= set(p) for p in proposals)
    assert loaded.status_code == 201 and required("Dataset") <= set(loaded.json())
