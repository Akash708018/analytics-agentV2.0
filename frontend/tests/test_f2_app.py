"""F2 AppTests: the real app script against the stateful fake backend.

A new AppTest with the same sid stands in for a browser refresh: F0 measured
that a refresh starts a fresh Streamlit session with the same URL.
"""

from pathlib import Path

import httpx
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from frontend import connection
from frontend.tests.fake_backend import FakeBackend

APP = str(Path(__file__).resolve().parents[1] / "app.py")
DS = {"dataset_id": "ds_0123456789ab", "name": "ads", "rows": 4, "columns": 8}


@pytest.fixture
def fake(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(connection, "APIClient", backend.client_factory())
    st.cache_resource.clear()
    yield backend
    st.cache_resource.clear()


def open_app(sid=None, page=None):
    app = AppTest.from_file(APP, default_timeout=10)
    if sid is not None:
        app.query_params["sid"] = sid
    if page is not None:
        app.switch_page(page)
    return app.run()


def text(app):
    """Every visible string, for presence checks."""
    parts = []
    for kind in ("title", "markdown", "caption", "info", "warning", "error", "success"):
        parts += [str(e.value) for e in getattr(app, kind)]
    return "\n".join(parts)


def links(app):
    return {e.proto.label: e.proto.query_string for e in app.sidebar
            if type(e).__name__ == "UnknownElement" and e.proto.label}


def with_data(fake, **ui_state):
    fake.datasets[DS["dataset_id"]] = {**DS, "workspace_id": "ws_0123456789ab",
                                       "created_at": "t", "assumptions": ["row 1 is the header"]}
    return fake.new_session({"datasets": [DS], "dataset_id": DS["dataset_id"], **ui_state})


def test_landing_creates_nothing_until_the_person_starts(fake):
    app = open_app()
    assert not app.exception
    assert fake.calls == []
    assert [b.label for b in app.button] == ["Start a new session"]
    assert not app.text_input
    app.button(key="shell.start").click().run()
    assert fake.count("POST", "/sessions") == 1
    sid = app.query_params["sid"][0]
    assert sid in fake.sessions
    assert app.text_input(key="ui.session.label").value == ""


def test_a_malformed_sid_sends_no_request(fake):
    app = open_app("../../sessions")
    assert not app.exception
    assert fake.calls == []
    assert "not valid" in text(app)


def test_an_unknown_sid_is_expired_and_no_session_is_created_silently(fake):
    app = open_app("0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11")
    assert "not found or has expired" in text(app)
    assert fake.count("POST") == 0
    assert not app.text_input


def test_a_new_session_from_the_expired_screen_carries_nothing_over(fake):
    app = open_app("0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11")
    app.session_state["ask.unsent"] = {"question": "an old question", "outcome": None}
    app.session_state["data.result"] = {"dataset_id": "ds_old", "name": "old.csv"}
    app.button(key="shell.start").click().run()
    sid = app.query_params["sid"][0]
    assert sid in fake.sessions
    assert "ask.unsent" not in app.session_state
    assert "data.result" not in app.session_state


def test_an_unreachable_service_shows_no_page(fake):
    sid = fake.new_session()
    fake.down = True
    app = open_app(sid)
    assert not app.exception
    assert "Can't reach the analytics service" in text(app)
    assert not app.text_input
    fake.down = False
    app.button(key="shell.retry").click().run()
    assert app.text_input(key="ui.session.label").value == ""


def test_one_edit_is_one_put_with_the_version_read(fake):
    sid = fake.new_session()
    app = open_app(sid)
    app.text_input(key="ui.session.label").input("Q3 review").run()
    app.run()
    assert fake.bodies("PUT") == [{"ui_state": {"schema": 2, "page": "session",
                                                "label": "Q3 review", "dataset_id": None,
                                                "datasets": [], "drafts": {}}, "version": 1}]
    assert "Saved · version 2" in text(app)


def test_saved_work_returns_after_a_refresh_on_the_saved_page(fake):
    sid = with_data(fake, label="Q3 review", page="data")
    app = open_app(sid)                                  # default route: restores "data"
    assert not app.exception
    assert app.title[0].value == "Data"
    assert app.selectbox(key="ui.data.dataset_id").value == DS["dataset_id"]
    assert "**ads**" in text(app)
    assert fake.count("PUT") == 0                        # nothing changed
    app.switch_page("views/session.py").run()
    assert app.text_input(key="ui.session.label").value == "Q3 review"
    assert fake.bodies("PUT")[-1]["ui_state"]["page"] == "session"


def test_an_explicit_route_wins_over_the_saved_page(fake):
    sid = with_data(fake, page="ask")
    app = open_app(sid, page="views/data.py")
    assert app.title[0].value == "Data"
    assert fake.bodies("PUT")[-1]["ui_state"]["page"] == "data"


def test_a_field_survives_navigating_away_and_back(fake):
    sid = fake.new_session()
    app = open_app(sid)
    app.text_input(key="ui.session.label").input("Q3 review").run()
    app.switch_page("views/data.py").run()
    app.switch_page("views/session.py").run()
    assert app.text_input(key="ui.session.label").value == "Q3 review"


def test_every_navigation_link_keeps_the_sid(fake):
    sid = fake.new_session()
    app = open_app(sid)
    assert links(app) == {title: f"sid={sid}" for title in
                          ("Session", "Data", "Clean", "Domain", "Contract", "Metrics",
                                    "Tools", "Ask")}


def test_a_conflict_waits_for_a_choice_then_load_latest_shows_theirs(fake):
    sid = fake.new_session()
    app = open_app(sid)
    fake.write_elsewhere(sid, {"label": "theirs"})
    app.text_input(key="ui.session.label").input("mine").run()
    assert "changed in another tab" in text(app)
    assert "| Analysis name | mine | theirs |" in text(app)
    app.run()
    assert fake.count("PUT") == 1
    app.button(key="shell.load_latest").click().run()
    assert app.text_input(key="ui.session.label").value == "theirs"
    assert "changed in another tab" not in text(app)
    assert fake.count("PUT") == 1


def test_two_tabs_that_only_navigate_never_see_a_conflict(fake):
    sid = fake.new_session()
    tab_a, tab_b = open_app(sid), open_app(sid)
    tab_a.switch_page("views/data.py").run()
    tab_b.switch_page("views/ask.py").run()
    tab_a.switch_page("views/session.py").run()
    for tab in (tab_a, tab_b):
        assert "changed in another tab" not in text(tab)
    assert fake.sessions[sid]["ui_state"]["page"] == "session"


def test_keep_my_changes_saves_over_the_newer_version(fake):
    sid = fake.new_session()
    app = open_app(sid)
    fake.write_elsewhere(sid, {"label": "theirs"})
    app.text_input(key="ui.session.label").input("mine").run()
    app.button(key="shell.keep_mine").click().run()
    assert fake.bodies("PUT")[-1]["version"] == 2
    assert fake.sessions[sid]["ui_state"]["label"] == "mine"
    assert app.text_input(key="ui.session.label").value == "mine"


def test_a_refused_value_says_not_saved_and_is_not_retried(fake):
    sid = fake.new_session()
    app = open_app(sid)
    app.text_input(key="ui.session.label").input("mail someone@example.com").run()
    app.run()
    assert "Not saved: ui_state.label: looks like personal data" in text(app)
    assert fake.count("PUT") == 1
    assert app.text_input(key="ui.session.label").value == "mail someone@example.com"


def test_ask_needs_a_dataset_first(fake):
    sid = fake.new_session()
    app = open_app(sid, page="views/ask.py")
    assert "Choose or upload a dataset first." in text(app)
    assert app.button(key="FormSubmitter:ask.form-Ask").disabled


def test_a_question_is_sent_once_and_followed_after_a_refresh(fake):
    sid = with_data(fake, page="ask")
    app = open_app(sid, page="views/ask.py")
    assert app.title[0].value == "Ask"
    app.text_area(key="ask.question").input("Why did ROAS drop?")
    app.button(key="FormSubmitter:ask.form-Ask").click().run()
    assert not app.exception
    assert fake.count("POST", "/turns") == 1
    turn_id, = fake.turns
    assert "Working (running)." in text(app)

    refreshed = open_app(sid, page="views/ask.py")       # a browser refresh on /ask mid-turn
    assert "Working (running)." in text(refreshed)
    assert refreshed.button(key="FormSubmitter:ask.form-Ask").disabled
    fake.finish_turn(turn_id)
    refreshed.run()
    assert "ROAS fell from 4.1 to 3.2." in text(refreshed)
    assert fake.count("POST", "/turns") == 1
    assert fake.count("GET", f"/turns/{turn_id}") >= 1   # followed by id


def test_a_timed_out_question_that_arrived_is_shown_not_resent(fake):
    sid = with_data(fake, page="ask")
    app = open_app(sid, page="views/ask.py")
    fake.fail_on("POST", "/turns", httpx.ReadTimeout, stored=True)
    app.text_area(key="ask.question").input("Why did ROAS drop?")
    app.button(key="FormSubmitter:ask.form-Ask").click().run()
    assert fake.count("POST", "/turns") == 1
    assert "did not confirm" not in text(app)
    assert "Working (running)." in text(app)


def test_a_timed_out_question_that_did_not_arrive_waits_for_the_person(fake):
    sid = with_data(fake, page="ask")
    app = open_app(sid, page="views/ask.py")
    fake.fail_on("POST", "/turns", httpx.ReadTimeout, stored=False)
    app.text_area(key="ask.question").input("Why did ROAS drop?")
    app.button(key="FormSubmitter:ask.form-Ask").click().run()
    app.run()
    assert "did not confirm this question" in text(app)
    assert fake.count("POST", "/turns") == 1
    assert fake.turns == {}
    app.button(key="ask.again").click().run()
    assert fake.count("POST", "/turns") == 2
    assert len(fake.turns) == 1
    assert "did not confirm" not in text(app)


def test_an_interrupted_turn_says_so_and_is_not_rerun(fake):
    sid = with_data(fake, page="ask")
    fake.turns["t_1"] = {"turn_id": "t_1", "sid": sid, "dataset_id": DS["dataset_id"],
                         "question": "Why?", "status": "interrupted", "events": [],
                         "answer": None, "created_at": "t"}
    app = open_app(sid, page="views/ask.py")
    assert "It was not run again" in text(app)
    assert fake.count("POST", "/turns") == 0
    assert not app.button(key="FormSubmitter:ask.form-Ask").disabled
