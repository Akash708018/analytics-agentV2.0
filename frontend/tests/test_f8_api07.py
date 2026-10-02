"""F8: API 0.7.0 in the screens. The fake answers in the shapes probed on the real backend."""

from frontend import prep
from frontend.components import evidence
from frontend.tests.test_f2_app import DS, fake, open_app, text, with_data  # noqa: F401
from frontend.tests.test_f4_app import CONTRACT, ready

ID = DS["dataset_id"]
DRIVERS = "logistics.sla_drivers"


# --- helpers ---------------------------------------------------------------------------------

def test_optional_params_are_the_ones_the_pack_declares():
    # F8 scanned the steps' focus_param; API 0.8.0 (#23) declares them (F10).
    focus = {"name": "focus", "kind": "text", "help": "One hub.", "default": None}
    spec = {"params_required": ["period"], "params_optional": [
        focus, {"name": "period", "kind": "text", "help": "", "default": None}],
        "steps": [{"analysis": "group_compare", "filter": [{"concept": "x", "focus_param": "zone"}]}]}
    assert prep.optional_params(spec) == [focus]          # required ones excluded; steps not read
    assert prep.optional_params({"params_required": [], "steps": []}) == []
    assert prep.optional_params({}) == []
    assert prep.blank_means(focus) == "Blank: the engine's choice."
    assert prep.blank_means({"default": "3"}) == "Blank: the tool uses 3."


PREFILL = {"from_dataset_id": "ds_aaaaaaaaaaaa", "from_name": "ads_august", "similarity": 1.0,
           "contract_version": 2,
           "contract": {"grain": "one row per campaign per day",
                        "primary_key": ["date", "campaign", "gone_column"], "date_column": "date",
                        "measures": ["cost", "clicks", "ctr"], "dimensions": ["campaign", "utm_source"],
                        "aggregations": {"cost": "sum", "clicks": "sum", "ctr": "none"},
                        "measure_definitions": {"cost": "spend as billed", "clicks": "clicks",
                                                "ctr": "clicks per impression, per row"}},
           "fork_choices": {"tax_basis": "net_excl_gst", "timezone": "nonsense"},
           "domains": ["marketing"], "metrics": ["ctr"], "validity_rules": [],
           "note": "Suggested from a similar file; nothing is applied until you confirm."}
COLUMNS = ["date", "campaign", "utm_source", "cost", "clicks", "ctr"]
FORKS = [{"fork_id": "tax_basis", "options": [{"id": "gross_incl_gst"}, {"id": "net_excl_gst"}]},
         {"fork_id": "timezone", "options": [{"id": "account_tz"}, {"id": "utc"}]}]


def test_prefill_fills_only_what_the_person_has_not_answered():
    fills, picks = prep.prefill_answers(PREFILL, COLUMNS,
                                        {"grain": "mine", "roles": {"ctr": "ignore"},
                                         "aggregations": {"cost": "mean"}}, {}, FORKS)
    assert ("grain",) not in fills                                  # answered: kept
    assert fills[("key",)] == ["date", "campaign"]                  # a column not in this file dropped
    assert {c: fills[("roles", c)] for c in ("date", "cost", "clicks", "campaign", "utm_source")} == {
        "date": "date", "cost": "measure", "clicks": "measure", "campaign": "dimension",
        "utm_source": "dimension"}
    assert ("roles", "ctr") not in fills and ("aggregations", "cost") not in fills
    assert fills[("aggregations", "ctr")] == "none" and fills[("definitions", "cost")] == "spend as billed"
    assert picks == {"tax_basis": "net_excl_gst"}                   # an option not offered: dropped
    assert prep.prefill_answers(PREFILL, COLUMNS, {}, {"tax_basis": "gross_incl_gst"}, FORKS)[1] == {}


def test_lineage_is_the_backends_values():
    result = {"result_id": "r_d9f5b2baa9d987b4", "status": "ok",
              "snapshot": {"hash": "e63766b0e708a9c4-492", "rows": 492}, "contract_version": 1,
              "grain": "one row = one order", "metrics_used": {"sla_breach": {"measure": "sla_breach"}}}
    assert prep.lineage(result) == (
        "computed on 492 rows (snapshot e63766b0e708a9c4-492) · contract v1 · grain: one row = one "
        "order · metrics: sla_breach → sla_breach · result r_d9f5b2baa9d987b4")
    assert prep.lineage({"summary": "an older result"}) is None


def test_a_plan_says_how_it_was_routed_and_why_it_was_blocked():
    line = evidence._event({"seq": 0, "type": "plan", "at": "t", "data": {
        "playbook": "sla_where_and_why", "slots": {}, "steps": ["logistics.sla_compliance"],
        "routed_by": "rules"}})
    assert line == ("Playbook **sla_where_and_why**, chosen by its question patterns, no model "
                    "call: `logistics.sla_compliance`")
    line = evidence._event({"seq": 0, "type": "plan", "at": "t", "data": {
        "playbook": "sla_where_and_why", "slots": {}, "steps": [], "routed_by": "rules",
        "blocked": ["the approved metric 'sla_breach'"], "recovery": "Approve it on Metrics."}})
    assert line.endswith("⛔ blocked: needs the approved metric 'sla_breach'. Approve it on Metrics.")


# --- results: lineage, the Results page, rows ------------------------------------------------

def tool_run(fake, tool="marketing.channel_efficiency"):
    sid = ready(fake)
    app = open_app(sid, page="views/tools.py")
    app.selectbox(key=f"ui.tools.{ID}.tool").select(tool).run()
    app.button(key="tools.run").click().run()
    return sid, app


def test_a_tool_result_shows_its_lineage_and_links_to_its_rows(fake):
    sid, app = tool_run(fake)
    assert not app.exception
    rid, = fake.stored
    assert (f"computed on 12 rows (snapshot e63766b0e708a9c4-12) · contract v1 · grain: one row "
            f"per campaign per day · result {rid}") in text(app)
    linked = [e.proto for e in app.main if type(e).__name__ == "UnknownElement"
              and getattr(e.proto, "label", "") == "Page through every row on Results"]
    assert linked and linked[0].query_string == f"sid={sid}&result={rid}"


def results_page(sid, **params):
    from frontend.tests.test_f2_app import APP
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(APP, default_timeout=10)
    app.query_params["sid"] = sid
    for k, v in params.items():
        app.query_params[k] = v
    app.switch_page("views/results.py")
    return app.run()


def test_results_lists_stored_results_and_chooses_none(fake):
    sid = ready(fake)
    assert "No stored results yet" in text(results_page(sid))
    sid, _ = tool_run(fake)
    app = results_page(sid)
    assert not app.exception
    box = app.selectbox(key=f"ui.results.{ID}.chosen")
    assert box.value is None and len(box.options) == 1
    assert "1 stored result(s) · 0 out of date" in text(app)
    assert fake.count("GET", "/results/") == 0                       # nothing opened for the person


def test_choosing_a_result_renders_it_and_pages_its_rows_as_the_backend_returns_them(fake):
    sid, _ = tool_run(fake)
    rid, = fake.stored
    app = results_page(sid)
    app.selectbox(key=f"ui.results.{ID}.chosen").select(rid).run()
    assert not app.exception
    assert "#### Which channels pay back: 1 step(s) run" in text(app)
    assert fake.sessions[sid]["ui_state"]["drafts"][ID]["result"] == rid   # the id is saved
    rows = app.dataframe[-1].value
    assert list(rows.columns) == ["channel", "n", "cost (sum)"]
    assert list(rows["cost (sum)"]) == ["449.0", "560.0", "88.5"]   # the engine's order, strings
    app.selectbox(key=f"ui.results.{ID}.{rid}.Spend by group.sort").select("cost (sum)").run()
    assert list(app.dataframe[-1].value["channel"]) == ["search", "social", "video"]
    assert fake.queries[-1] == (f"/results/{rid}/inspect", {
        "step": "Spend by group", "sort_by": "cost (sum)", "descending": "true", "limit": "50",
        "offset": "0"})                                                # exactly as chosen
    assert "Rows 1–3 of 3 · 3 kept with the result of the engine's 3" in text(app)


def test_a_stale_result_says_why(fake):
    sid, _ = tool_run(fake)
    rid, = fake.stored
    fake.make_stale(rid)
    app = results_page(sid)
    assert "1 stored result(s) · 1 out of date" in text(app)
    app.selectbox(key=f"ui.results.{ID}.chosen").select(rid).run()
    assert ("Out of date: the data changed (cleaning or a new upload) since this result. "
            "Run it again for current figures.") in text(app)


def test_a_results_link_opens_that_result(fake):
    sid, _ = tool_run(fake)
    rid, = fake.stored
    app = results_page(sid, result=rid)
    assert not app.exception
    assert app.selectbox(key=f"ui.results.{ID}.chosen").value == rid
    assert "result" not in app.query_params                           # used once, then dropped


def test_an_inspect_refusal_is_shown_as_returned(fake):
    sid, _ = tool_run(fake)
    rid, = fake.stored
    app = results_page(sid)
    app.selectbox(key=f"ui.results.{ID}.chosen").select(rid).run()
    fake.refuse_sort = True                       # the backend refuses this sort (422)
    app.selectbox(key=f"ui.results.{ID}.{rid}.Spend by group.sort").select("cost (sum)").run()
    assert ("Could not read these rows: sort_by 'cost (sum)'; columns: ['channel', 'n', "
            "'cost (sum)']") in text(app)
    assert "code: unknown_column · HTTP 422" in text(app)


# --- Ask: a blocked plan ----------------------------------------------------------------------

def test_a_blocked_answer_says_what_is_missing_and_what_to_do(fake):
    sid = with_data(fake, page="ask")
    app = open_app(sid, page="views/ask.py")
    app.text_area(key="ask.question").input("Which hub is worst on SLA?")
    app.button(key="FormSubmitter:ask.form-Ask").click().run()
    turn_id, = fake.turns
    fake.block_turn(turn_id)
    app.run()
    page = text(app)
    assert "Nothing was run: this question needs the approved metric 'sla_breach' first." in page
    assert "⛔ blocked: needs the approved metric 'sla_breach'. Approve the SLA breach metric" in page
    assert "No tool result backs this answer: nothing was run." in page


# --- keyword carry-forward ---------------------------------------------------------------------

def keyword_state(fake, carry):
    sid = with_data(fake, page="keywords")
    state = fake._prep(ID)
    state["kw"] = {
        "p000001": {"group_id": "p000001", "label": "sushi · delivery", "intent": "transactional",
                    "keywords": ["sushi delivery pune"], "facets": {}, "approved": True,
                    "proposed_by": "rules", "generation": "0c71d089bb46", "joins": None},
        "p000002": {"group_id": "p000002", "label": "sushi · delivery (new keywords)",
                    "intent": "transactional", "keywords": ["sushi delivery wakad", "sushi delivry aundh"],
                    "facets": {}, "approved": False, "proposed_by": "rules",
                    "generation": "0c71d089bb46", "joins": "p000001"}}
    state["kw_run"] = {"column": "campaign", "embedding": "chargram/tfidf-char2-4", "threshold": 0.6,
                       "embedding_version": {"generation": "0c71d089bb46"}, "typos_merged": {},
                       "keywords": 3, "proposed_groups": 1, "carry_forward": carry}
    return sid


def test_new_keywords_join_an_approved_group_only_by_the_persons_merge(fake):
    sid = keyword_state(fake, {"status": "ok", "suggested": 1, "note": "merge to accept"})
    app = open_app(sid, page="views/keywords.py")
    assert not app.exception
    page = text(app)
    assert "**New keywords for approved groups (1)**" in page
    assert "sushi delivery wakad, sushi delivry aundh → **sushi · delivery**" in page
    assert "generation 0c71d089bb46" in page
    assert fake.bodies("POST", f"/datasets/{ID}/keyword-groups/actions") == []
    app.button(key=f"kw.{ID}.join.p000002").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/keyword-groups/actions") == [
        {"action": "merge", "group_ids": ["p000001", "p000002"]}]
    merged = fake._prep(ID)["kw"]["p000001"]
    assert merged["approved"] and "sushi delivery wakad" in merged["keywords"]
    assert "Added to 'sushi · delivery'." in text(app)


def test_a_disabled_carry_forward_says_why(fake):
    sid = keyword_state(fake, {"status": "disabled", "suggested": 0,
                               "reason": "approved groups were built under generation 0ld0ld0ld0ld"})
    app = open_app(sid, page="views/keywords.py")
    assert ("New keywords were not matched to your approved groups: approved groups were built "
            "under generation 0ld0ld0ld0ld") in text(app)


# --- contract pre-fill -------------------------------------------------------------------------

def test_prefill_offers_a_similar_files_answers_and_keeps_the_persons(fake):
    sid = with_data(fake, page="contract")
    fake._prep(ID)["prefill"] = PREFILL
    app = open_app(sid, page="views/contract.py")
    assert not app.exception
    page = text(app)
    assert "**Answers from a similar file: ads_august**" in page
    assert "It also had domains: marketing; metrics: ctr. Those are not applied here" in page
    app.text_input(key=f"ui.contract.{ID}.grain").input("one row per ad per day").run()
    button = app.button(key="contract.prefill")
    assert button.label == "Use these answers (14 you have not answered)"
    button.click().run()
    assert app.text_input(key=f"ui.contract.{ID}.grain").value == "one row per ad per day"
    assert app.multiselect(key=f"ui.contract.{ID}.key").value == ["date", "campaign"]
    assert app.selectbox(key=f"ui.contract.{ID}.role.utm_source").value == "dimension"
    assert app.selectbox(key=f"ui.contract.{ID}.agg.ctr").value == "none"
    assert app.text_input(key=f"ui.contract.{ID}.def.cost").value == "spend as billed"
    assert app.radio(key=f"ui.contract.{ID}.fork.tax_basis").value == "net_excl_gst"
    assert app.radio(key=f"ui.contract.{ID}.fork.timezone").value is None
    assert not fake._prep(ID)["confirms"]                              # nothing confirmed
    saved = fake.sessions[sid]["ui_state"]["drafts"][ID]
    assert saved["contract"]["grain"] == "one row per ad per day" and saved["forks"] == {
        "tax_basis": "net_excl_gst"}


# --- tools: a value the backend could not match -------------------------------------------------

def drivers(fake):
    sid = with_data(fake, page="tools")
    state = fake._prep(ID)
    state["domains"], state["contract"], state["version"] = ["logistics"], CONTRACT, 1
    app = open_app(sid, page="views/tools.py")
    app.selectbox(key=f"ui.tools.{ID}.tool").select(DRIVERS).run()
    return app


def runs(fake):
    return [params for tool, params in fake._prep(ID)["runs"] if tool == DRIVERS]


def test_focus_is_offered_blank_and_an_ambiguous_value_is_asked_back(fake):
    app = drivers(fake)
    focus = app.text_input(key=f"ui.tools.{ID}.{DRIVERS}.p.focus")
    assert focus.value == "" and focus.label == "Focus on (optional)"
    app.button(key="tools.run").click().run()
    assert runs(fake) == [{}]                                           # blank: not sent
    focus.input("pune").run()
    app.button(key="tools.run").click().run()
    assert runs(fake)[-1] == {"focus": "pune"}
    pick = app.radio(key=f"ui.tools.{ID}.{DRIVERS}.pick.focus")
    assert pick.value is None and pick.options == ["PUNE_CENTRAL", "PUNE_EAST", "PUNE_SOUTH", "PUNE_WEST"]
    assert "'pune' could be several hub values" in text(app)
    pick.set_value("PUNE_EAST").run()
    assert app.text_input(key=f"ui.tools.{ID}.{DRIVERS}.p.focus").value == "PUNE_EAST"
    app.button(key="tools.run").click().run()
    assert runs(fake)[-1] == {"focus": "PUNE_EAST"}
    assert "#### What goes with breaches: 1 step(s) run" in text(app)


def test_an_unknown_value_lists_the_values_in_the_data(fake):
    app = drivers(fake)
    app.text_input(key=f"ui.tools.{ID}.{DRIVERS}.p.focus").input("nagpur").run()
    app.button(key="tools.run").click().run()
    assert "no hub value matches 'nagpur'" in text(app)
    assert app.radio(key=f"ui.tools.{ID}.{DRIVERS}.pick.focus").label == "The hub values in this data"
