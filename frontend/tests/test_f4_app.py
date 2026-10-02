"""F4 AppTests: Metrics and Tools against the fake backend's probed rules."""

from frontend.tests.test_f2_app import DS, fake, open_app, text, with_data  # noqa: F401

ID = DS["dataset_id"]
CONTRACT = {"grain": "one row per campaign per day", "primary_key": ["date", "campaign"],
            "date_column": "date", "measures": ["cost", "ctr"], "dimensions": ["campaign"],
            "aggregations": {"cost": "sum", "ctr": "none"}}


def ready(fake, contract=True, forks=None):
    """A dataset with marketing confirmed and (optionally) a contract in force."""
    sid = with_data(fake)
    state = fake._prep(ID)
    state["domains"] = ["marketing"]
    if contract:
        state["contract"], state["version"] = CONTRACT, 1
        state["fork_choices"] = forks if forks is not None else {"conversion_source": "backend_orders"}
    return sid


def approvals(fake):
    return fake.bodies("POST", f"/datasets/{ID}/metrics/approve")


# --- Metrics --------------------------------------------------------------------------------

def test_templates_are_listed_and_unavailable_ones_say_why(fake):
    app = open_app(ready(fake), page="views/metrics.py")
    assert not app.exception
    page = text(app)
    assert "**CTR (%)**" in page and "**ROAS**" in page
    assert "Delivered ROAS: needs order_revenue, gst, spend" in page
    assert approvals(fake) == []


def test_approve_sends_the_template_and_shows_the_measure(fake):
    app = open_app(ready(fake), page="views/metrics.py")
    app.button(key=f"metrics.{ID}.ctr.approve").click().run()
    assert approvals(fake) == [{"template_id": "ctr", "bindings": {}, "fork_choices": {}}]
    assert "Approved: measure `ctr`" in text(app)


def test_an_ambiguous_column_is_asked_and_the_retry_carries_it(fake):
    app = open_app(ready(fake), page="views/metrics.py")
    app.button(key=f"metrics.{ID}.cpa.approve").click().run()
    assert "More than one column could be 'conversions'" in text(app)
    pick = app.selectbox(key=f"ui.metrics.{ID}.cpa.bind.conversions")
    assert pick.options == ["conversions", "platform_conversions"] and pick.value is None
    pick.set_value("platform_conversions").run()
    app.button(key=f"metrics.{ID}.cpa.approve").click().run()
    assert approvals(fake)[-1]["bindings"] == {"conversions": "platform_conversions"}
    assert "Approved: measure `cpa`" in text(app)


def test_a_missing_concept_can_be_named_then_approved(fake):
    app = open_app(ready(fake), page="views/metrics.py")
    app.button(key=f"metrics.{ID}.roas.approve").click().run()
    assert "ROAS: no column is bound to 'conv_value'" in text(app)
    app.text_input(key=f"metrics.{ID}.roas.concept").input("conv_value").run()
    app.selectbox(key=f"metrics.{ID}.roas.column").set_value("cost").run()
    app.button(key=f"metrics.{ID}.roas.add").click().run()
    app.button(key=f"metrics.{ID}.roas.approve").click().run()
    assert approvals(fake)[-1]["bindings"] == {"conv_value": "cost"}
    assert "Approved: measure `roas`" in text(app)


def test_approval_before_a_contract_says_to_confirm_it(fake):
    app = open_app(ready(fake, contract=False), page="views/metrics.py")
    app.button(key=f"metrics.{ID}.ctr.approve").click().run()
    assert "Confirm the dataset contract first." in text(app)


def test_rules_start_from_the_server_and_apply_sends_approve_and_reject(fake):
    sid = ready(fake)
    app = open_app(sid, page="views/metrics.py")
    rules = {c.key: c.value for c in app.checkbox}
    assert rules == {f"ui.rules.{ID}.exclude_test_campaigns": False,
                     f"ui.rules.{ID}.roas_spend_positive": False}
    assert "rows affected: 3" in text(app) and "rows affected: not counted" in text(app)
    app.button(key="rules.suggest").click().run()
    app.button(key="rules.apply").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/validity-rules/approve")[-1] == {
        "approve": ["exclude_test_campaigns", "roas_spend_positive"], "reject": []}
    assert "Approved now: exclude_test_campaigns, roas_spend_positive" in text(app)
    app.checkbox(key=f"ui.rules.{ID}.roas_spend_positive").uncheck().run()
    app.button(key="rules.apply").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/validity-rules/approve")[-1] == {
        "approve": [], "reject": ["roas_spend_positive"]}


# --- Tools ----------------------------------------------------------------------------------

def tool(app, tool_id):
    app.selectbox(key=f"ui.tools.{ID}.tool").set_value(tool_id).run()
    return app


def runs(fake):
    return fake._prep(ID)["runs"]


def test_core_is_hidden_and_unavailable_tools_say_why(fake):
    app = open_app(ready(fake), page="views/tools.py")
    assert not app.exception
    options = app.selectbox(key=f"ui.tools.{ID}.tool").options
    assert "Which channels pay back" in options and not any("Trend" == o for o in options)
    page = text(app)
    assert "On time, in full · `logistics.otif`: confirm the logistics domain" in page
    assert "Creative fatigue · `marketing.creative_fatigue`: the data has no frequency" in page


def test_required_params_are_asked_and_a_run_shows_the_result_as_returned(fake):
    app = tool(open_app(ready(fake), page="views/tools.py"), "marketing.roas_change_explainer")
    assert [t.label for t in app.text_input][:2] == ["Period", "Compared with"]
    app.button(key="tools.run").click().run()
    assert "Fill in: Period, Compared with" in text(app)
    assert runs(fake)[-1] == ("marketing.roas_change_explainer", {})
    app.text_input(key=f"ui.tools.{ID}.marketing.roas_change_explainer.p.period").input("2026-09").run()
    app.text_input(key=f"ui.tools.{ID}.marketing.roas_change_explainer.p.baseline").input("2026-08").run()
    app.button(key="tools.run").click().run()
    assert runs(fake)[-1] == ("marketing.roas_change_explainer",
                              {"period": "2026-09", "baseline": "2026-08"})
    page = text(app)
    assert "#### Why ROAS changed: 1 step(s) run" in page
    assert "CTR: skipped -- metric 'ctr' is not approved for this dataset" in page
    figures = app.dataframe[0].value
    assert list(figures["value"]) == ["449.0", "560.0", "suppressed"]
    assert list(figures["source"]) == ["contract", "contract", "derived"]


def test_a_number_param_is_sent_as_a_number(fake):
    app = tool(open_app(ready(fake), page="views/tools.py"), "marketing.budget_pacing")
    app.number_input(key=f"ui.tools.{ID}.marketing.budget_pacing.p.budget").set_value(5000).run()
    app.text_input(key=f"ui.tools.{ID}.marketing.budget_pacing.p.month").input("2026-09").run()
    app.button(key="tools.run").click().run()
    assert runs(fake)[-1] == ("marketing.budget_pacing", {"budget": 5000.0, "month": "2026-09"})


def test_an_unanswered_fork_is_answered_in_place_then_the_tool_runs(fake):
    app = tool(open_app(ready(fake, forks={}), page="views/tools.py"), "marketing.channel_efficiency")
    app.button(key="tools.run").click().run()
    assert "Answer these first; none is answered for you." in text(app)
    radio = app.radio(key=f"ui.tools.{ID}.fork.conversion_source")
    assert radio.value is None and radio.options == ["Orders in your backend", "What the ad platform reports"]
    radio.set_value("backend_orders").run()
    app.button(key="tools.forks").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/forks")[-1] == {
        "fork_choices": {"conversion_source": "backend_orders"}}
    app.button(key="tools.run").click().run()
    assert "#### Which channels pay back: 1 step(s) run" in text(app)
    assert "Your answers used: conversion_source = backend_orders" in text(app)


def test_festival_dates_need_an_explicit_confirm_on_every_run(fake):
    sid = ready(fake)
    app = tool(open_app(sid, page="views/tools.py"), "marketing.festive_compare")
    app.selectbox(key=f"ui.tools.{ID}.marketing.festive_compare.p.festival").set_value("diwali").run()
    app.text_input(key=f"ui.tools.{ID}.marketing.festive_compare.p.year").input("2026").run()
    app.button(key="tools.run").click().run()
    assert "2026: 2026-11-08 to 2026-11-08" in text(app)
    assert "dates_confirmed" not in runs(fake)[-1][1]
    app.checkbox(key=f"tools.{ID}.marketing.festive_compare.dates_ok").check().run()
    app.button(key="tools.run").click().run()
    assert runs(fake)[-1][1] == {"festival": "diwali", "year": "2026", "dates_confirmed": True}
    assert "This festival vs last year: 1 step(s) run" in text(app)
    saved = fake.sessions[sid]["ui_state"]["drafts"][ID]["params"]["marketing.festive_compare"]
    assert saved == {"festival": "diwali", "year": "2026"}          # the confirm is not saved


def test_the_chosen_tool_and_its_params_come_back_after_a_refresh(fake):
    sid = ready(fake)
    app = tool(open_app(sid, page="views/tools.py"), "marketing.roas_change_explainer")
    app.text_input(key=f"ui.tools.{ID}.marketing.roas_change_explainer.p.period").input("2026-09").run()
    refreshed = open_app(sid, page="views/tools.py")
    assert refreshed.selectbox(key=f"ui.tools.{ID}.tool").value == "marketing.roas_change_explainer"
    assert refreshed.text_input(key=f"ui.tools.{ID}.marketing.roas_change_explainer.p.period").value == "2026-09"
    assert not refreshed.dataframe                                       # results are not saved


def test_a_concept_named_for_a_tool_is_sent_as_its_binding(fake):
    app = tool(open_app(ready(fake), page="views/tools.py"), "marketing.roas_change_explainer")
    prefix = f"tools.{ID}.marketing.roas_change_explainer"
    app.text_input(key=f"{prefix}.concept").input("conv_value").run()
    app.selectbox(key=f"{prefix}.column").set_value("cost").run()
    app.button(key=f"{prefix}.add").click().run()
    app.text_input(key=f"ui.tools.{ID}.marketing.roas_change_explainer.p.period").input("2026-09").run()
    app.text_input(key=f"ui.tools.{ID}.marketing.roas_change_explainer.p.baseline").input("2026-08").run()
    app.button(key="tools.run").click().run()
    assert runs(fake)[-1][1]["bindings"] == {"conv_value": "cost"}
