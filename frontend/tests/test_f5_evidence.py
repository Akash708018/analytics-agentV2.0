"""F5: the evidence under each answer, as the backend recorded it."""

from frontend.components import evidence
from frontend.tests.test_f2_app import DS, fake, open_app, text, with_data  # noqa: F401


def line(kind, **data):
    return evidence._event({"seq": 0, "type": kind, "at": "t", "data": data})


def test_each_event_becomes_one_line_with_the_backends_values():
    assert line("plan", playbook="why_roas_dropped", slots={"period": "2026-09"},
                steps=["marketing.roas_change_explainer"]) == (
        "Playbook **why_roas_dropped** (period = 2026-09): `marketing.roas_change_explainer`")
    assert line("plan", playbook=None, steps=[], mode="tool_calling").startswith("No playbook fitted")
    assert line("tool_call", tool_id="marketing.spend_waste", params={}, status="ok", ms=167) == (
        "▶️ `marketing.spend_waste` ran · 167 ms")
    assert line("tool_call", tool_id="marketing.device_geo_split", params={"by": "city"},
                status="skipped", code="engine_refused", reason="'city' is not declared") == (
        "⏭️ `marketing.device_geo_split` (by=city) skipped · engine_refused: 'city' is not declared")
    assert line("figure_check", status="corrected", detail="1 fixed", corrected=["ROAS 3.2"]) == (
        "🛠️ Figure check corrected · 1 fixed · corrected: ROAS 3.2")
    assert line("interpretation_check", rule="correlational_only", status="flagged",
                detail="says 'caused'") == (
        "⚠️ Interpretation rules (correlational_only) flagged · says 'caused'")
    assert line("provider_wait", provider="gemini", seconds=12) == "⏳ Waited 12 s for gemini"
    assert line("error", code="agent_not_wired", message="no model") == "❌ agent_not_wired: no model"
    assert line("answer", text="x") is None


def test_usage_says_tokens_are_estimates():
    assert evidence.usage_line({"llm_calls": 2, "tool_calls": 3, "tokens_in_est": 1001,
                                "tokens_out_est": 56, "model": "scripted"}) == (
        "2 model call(s) · 3 tool call(s) · about 1001 tokens in, 56 out (estimates) · model scripted")


def ask(fake, **ui):
    sid = with_data(fake, page="ask", **ui)
    app = open_app(sid, page="views/ask.py")
    app.text_area(key="ask.question").input("Where is spend wasted?")
    app.button(key="FormSubmitter:ask.form-Ask").click().run()
    turn_id, = fake.turns
    return sid, turn_id, app


def test_an_answer_shows_its_steps_checks_cost_and_evidence(fake):
    sid, turn_id, app = ask(fake)
    fake.answer_turn(turn_id)
    app.run()
    assert not app.exception
    page = text(app)
    assert "Spend goes mostly to search: 560.0 (55.5%)." in page
    assert "Playbook **where_is_spend_wasted**: `marketing.spend_waste` → `marketing.keyword_ngrams`" in page
    assert "▶️ `marketing.spend_waste` ran · 167 ms" in page
    assert "⏭️ `marketing.keyword_ngrams` skipped · needs_data: text_ngrams: none of" in page
    assert "✅ Figure check passed · 3 of 3 figure(s) match the tool replies." in page
    assert "about 1001 tokens in, 56 out (estimates)" in page
    assert [e.label for e in app.expander][:2] == ["How this was answered",
                                                     "Evidence: Where spend is wasted: 2 step(s) run"]
    figures = app.dataframe[0].value
    assert list(figures["value"]) == ["560.0", "55.5", "suppressed"]
    refreshed = open_app(sid, page="views/ask.py")             # answers come back from the server
    assert "Spend goes mostly to search" in text(refreshed)


def test_flags_are_shown_as_unresolved_checks(fake):
    _, turn_id, app = ask(fake)
    fake.answer_turn(turn_id, flags=["causal wording without a holdout"], with_result=False)
    app.run()
    assert "Unresolved check: causal wording without a holdout" in text(app)
    assert "No tool result backs this answer: every step was skipped" in text(app)


def test_a_running_turn_shows_its_plan_and_tools_as_they_arrive(fake):
    _, turn_id, app = ask(fake)
    fake.add_event(turn_id, "plan", {"playbook": "where_is_spend_wasted", "slots": {},
                                     "steps": ["marketing.spend_waste"]})
    fake.add_event(turn_id, "tool_call", {"tool_id": "marketing.spend_waste", "params": {},
                                          "status": "ok", "ms": 90})
    app.run()
    page = text(app)
    assert "Working (running)." in page
    assert "Playbook **where_is_spend_wasted**" in page and "ran · 90 ms" in page


def test_a_failed_turn_shows_its_error(fake):
    _, turn_id, app = ask(fake)
    fake.add_event(turn_id, "error", {"code": "agent_not_wired", "message": "no model configured"})
    fake.turns[turn_id]["status"] = "failed"
    app.run()
    assert "This question could not be answered." in text(app)
    assert "❌ agent_not_wired: no model configured" in text(app)
