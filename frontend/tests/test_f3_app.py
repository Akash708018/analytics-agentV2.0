"""F3 AppTests: Clean, Domain and Contract against the fake backend's measured rules.

A new AppTest with the same sid stands in for a browser refresh (F0, F2).
"""

import datetime as dt

import httpx
import pytest

from frontend.tests.test_f2_app import DS, fake, open_app, text, with_data  # noqa: F401

ID = DS["dataset_id"]


def ticked(app):
    return {c.label: c.value for c in app.checkbox}


# --- Clean -----------------------------------------------------------------------------------

def test_nothing_is_pre_ticked_and_tick_suggested_ticks_only_suggested_steps(fake):
    app = open_app(with_data(fake), page="views/clean.py")
    assert not app.exception
    assert set(ticked(app).values()) == {False}
    assert "Suggested: lossless and conflict-free" in text(app)
    assert app.button(key="clean.apply").disabled
    app.button(key="clean.suggest").click().run()
    assert ticked(app) == {
        "keep one of each of the 1 exactly duplicated row(s)": False,
        "strip padding from 1 value(s) in utm_source": True,
        "fold utm_source to one case": False,
    }
    assert fake.count("POST", f"/datasets/{ID}/cleaning/approve") == 0     # ticking sends nothing


def test_apply_sends_exactly_the_ticked_steps_and_shows_the_new_plan(fake):
    app = open_app(with_data(fake), page="views/clean.py")
    app.checkbox(key=f"ui.clean.{ID}.C002|TRIM_WHITESPACE|utm_source").check().run()
    app.checkbox(key=f"ui.clean.{ID}.C001|DROP_DUPLICATE_ROWS|").check().run()
    app.button(key="clean.apply").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/cleaning/approve") == [
        {"approve": ["C001", "C002"], "reject": []}]
    assert "Applied C001, C002 (2 ledger entries)" in text(app)
    assert list(ticked(app)) == ["fold utm_source to one case"]           # renumbered C001
    assert ticked(app)["fold utm_source to one case"] is False


def test_a_tick_on_a_renumbered_plan_sends_nothing(fake):
    sid = with_data(fake)
    app = open_app(sid, page="views/clean.py")
    app.checkbox(key=f"ui.clean.{ID}.C003|NORMALISE_CASE|utm_source").check().run()
    other = fake.client_factory()()                       # another tab applies C001
    other.list_cleaning_proposals(ID)
    other.approve_cleaning(ID, approve=["C001"])
    app.button(key="clean.apply").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/cleaning/approve") == [
        {"approve": ["C001"], "reject": []}]               # only the other tab's
    assert "The proposals changed since you ticked them" in text(app)
    assert set(ticked(app).values()) == {False}


def test_ticks_are_saved_and_come_back_after_a_refresh(fake):
    sid = with_data(fake)
    app = open_app(sid, page="views/clean.py")
    app.checkbox(key=f"ui.clean.{ID}.C002|TRIM_WHITESPACE|utm_source").check().run()
    saved = fake.sessions[sid]["ui_state"]["drafts"][ID]["clean"]
    assert saved == {"C002|TRIM_WHITESPACE|utm_source": True}
    refreshed = open_app(sid, page="views/clean.py")
    assert ticked(refreshed)["strip padding from 1 value(s) in utm_source"] is True


# --- Domain ----------------------------------------------------------------------------------

def test_no_domain_is_ticked_from_a_score_and_confirm_sends_the_ticks(fake):
    sid = with_data(fake)
    app = open_app(sid, page="views/domain.py")
    assert not app.exception
    assert ticked(app) == {"Marketing": False, "Logistics": False}      # marketing first
    assert "Detected · score 0.385" in text(app)
    assert app.button(key="domain.confirm").disabled
    app.checkbox(key=f"ui.domain.{ID}.marketing").check().run()
    app.button(key="domain.confirm").click().run()
    assert fake.bodies("POST", f"/datasets/{ID}/domains/confirm") == [{"domains": ["marketing"]}]
    assert "Confirmed now: marketing" in text(app)
    assert ticked(app)["Marketing"] is True
    contract = open_app(sid, page="views/contract.py")
    assert "Which revenue should ROAS use?" in [r.label for r in contract.radio]


# --- Contract --------------------------------------------------------------------------------

def role(app, column):
    return app.selectbox(key=f"ui.contract.{ID}.role.{column}")


def test_the_form_starts_from_the_proposal_and_preselects_no_answer(fake):
    app = open_app(with_data(fake), page="views/contract.py")
    assert not app.exception
    assert role(app, "cost").value == "dimension" and role(app, "ctr").value == "measure"
    assert role(app, "date").value == "date"
    assert app.text_input(key=f"ui.contract.{ID}.grain").value == ""
    assert app.selectbox(key=f"ui.contract.{ID}.agg.ctr").value is None
    assert [r.value for r in app.radio] == [None, None]
    assert "Engine suggests none (strong)" in text(app)
    assert app.date_input(key=f"ui.contract.{ID}.from").value is None


def test_suggestion_buttons_fill_only_what_has_a_suggestion(fake):
    app = open_app(with_data(fake), page="views/contract.py")
    app.button(key="contract.forks").click().run()
    assert {r.label: r.value for r in app.radio} == {
        "Are money columns before or after GST?": "net_excl_gst",
        "Which timezone are the dates in?": None}
    app.button(key="contract.strong").click().run()
    assert app.selectbox(key=f"ui.contract.{ID}.agg.ctr").value == "none"


def test_confirm_shows_the_engines_questions_then_succeeds(fake):
    sid = with_data(fake)
    app = open_app(sid, page="views/contract.py")
    app.button(key="contract.forks").click().run()
    app.button(key="contract.confirm").click().run()
    assert "every question below needs your answer" in text(app)
    assert "Which timezone are the dates in?" in text(app)

    app.radio(key=f"ui.contract.{ID}.fork.timezone").set_value("account_tz").run()
    app.button(key="contract.confirm").click().run()
    assert "these still need an answer" in text(app)
    assert "Please answer: grain" in text(app)

    app.text_input(key=f"ui.contract.{ID}.grain").input("one row per campaign per day").run()
    role(app, "cost").set_value("measure").run()
    app.multiselect(key=f"ui.contract.{ID}.key").set_value(["campaign"]).run()
    app.selectbox(key=f"ui.contract.{ID}.agg.cost").set_value("sum").run()
    app.selectbox(key=f"ui.contract.{ID}.agg.ctr").set_value("none").run()
    app.text_input(key=f"ui.contract.{ID}.def.cost").input("spend as billed").run()
    app.text_input(key=f"ui.contract.{ID}.def.ctr").input("clicks / impressions per row").run()
    app.date_input(key=f"ui.contract.{ID}.from").set_value(dt.date(2026, 8, 1)).run()
    app.button(key="contract.confirm").click().run()
    assert "the engine still finds this contract PROVISIONAL" in text(app)   # key lacks date
    assert "**Why:** grain, analysis_window." in text(app)
    app.multiselect(key=f"ui.contract.{ID}.key").set_value(["date", "campaign"]).run()
    app.button(key="contract.confirm").click().run()
    assert "Contract confirmed (version 1)" in text(app)
    body = fake.prep[ID]["confirms"][-1]
    assert body["fork_choices"] == {"tax_basis": "net_excl_gst", "timezone": "account_tz"}
    assert body["contract"] == {
        "grain": "one row per campaign per day", "primary_key": ["date", "campaign"],
        "date_column": "date", "measures": ["cost", "ctr"],
        "dimensions": ["campaign", "utm_source", "clicks"],
        "aggregations": {"cost": "sum", "ctr": "none"},
        "measure_definitions": {"cost": "spend as billed", "ctr": "clicks / impressions per row"},
        "analysis_window_start": "2026-08-01", "caveats": []}
    assert fake.sessions[sid]["ui_state"]["drafts"][ID]["confirmed_version"] == 1


def test_a_half_done_contract_comes_back_after_a_refresh(fake):
    sid = with_data(fake)
    app = open_app(sid, page="views/contract.py")
    app.text_input(key=f"ui.contract.{ID}.grain").input("one row per campaign per day").run()
    role(app, "cost").set_value("measure").run()
    app.multiselect(key=f"ui.contract.{ID}.key").set_value(["date", "campaign"]).run()
    app.selectbox(key=f"ui.contract.{ID}.agg.cost").set_value("sum").run()
    app.radio(key=f"ui.contract.{ID}.fork.timezone").set_value("utc").run()
    app.date_input(key=f"ui.contract.{ID}.from").set_value(dt.date(2026, 8, 1)).run()
    refreshed = open_app(sid, page="views/contract.py")
    assert refreshed.text_input(key=f"ui.contract.{ID}.grain").value == "one row per campaign per day"
    assert role(refreshed, "cost").value == "measure"
    assert refreshed.multiselect(key=f"ui.contract.{ID}.key").value == ["date", "campaign"]
    assert refreshed.selectbox(key=f"ui.contract.{ID}.agg.cost").value == "sum"
    assert refreshed.radio(key=f"ui.contract.{ID}.fork.timezone").value == "utc"
    assert refreshed.date_input(key=f"ui.contract.{ID}.from").value == dt.date(2026, 8, 1)
    saved = fake.sessions[sid]["ui_state"]["drafts"][ID]
    assert saved["contract"]["window_start"] == "2026-08-01"
    assert saved["forks"] == {"timezone": "utc"}


def test_two_date_columns_block_confirm(fake):
    app = open_app(with_data(fake), page="views/contract.py")
    role(app, "campaign").set_value("date").run()
    assert "Choose one date column" in text(app)
    assert "key" not in role(app, "campaign").options
    assert app.button(key="contract.confirm").disabled


@pytest.mark.parametrize("page", ["views/clean.py", "views/domain.py", "views/contract.py"])
def test_prep_pages_ask_for_a_dataset_first(fake, page):
    app = open_app(fake.new_session(), page=page)
    assert not app.exception
    assert "Choose or upload a dataset on Data first." in text(app)


def test_the_data_page_shows_the_profile_as_returned(fake):
    app = open_app(with_data(fake, page="data"), page="views/data.py")
    assert not app.exception
    assert "ctr looks like a per-row rate" in text(app)
    assert "ads · ds_0123456789ab" in [o for o in app.selectbox(key="ui.data.dataset_id").options]
