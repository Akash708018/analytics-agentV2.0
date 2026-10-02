"""F0 smoke checks; real browser history is measured separately in F0.md."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

FRONTEND = Path(__file__).resolve().parents[1]
PROBE = FRONTEND / "dev" / "state_probe.py"


def test_landing_collects_no_unsaved_work_and_offers_one_explicit_start():
    # F2: the landing starts a backend session only on this click (test_f2_app.py).
    app = AppTest.from_file(str(FRONTEND / "app.py")).run()
    assert not app.exception
    assert app.title[0].value == "Analytics agent"
    assert not app.text_input
    assert [b.label for b in app.button] == ["Start a new session"]


def test_probe_shared_field_survives_a_rerun():
    app = AppTest.from_file(str(PROBE)).run()
    instance = app.session_state["probe.instance"]
    app.text_input(key="ui.probe.shared").set_value("synthetic campaign").run()
    assert not app.exception
    assert app.session_state["probe.instance"] == instance
    assert app.text_input(key="ui.probe.shared").value == "synthetic campaign"


def test_fresh_probe_session_has_no_previous_values_even_with_the_same_sid():
    sid = "00000000-0000-4000-8000-0000000000f0"
    first = AppTest.from_file(str(PROBE))
    first.query_params["sid"] = sid
    first.run().text_input(key="ui.probe.shared").set_value("synthetic campaign").run()
    second = AppTest.from_file(str(PROBE))
    second.query_params["sid"] = sid
    second.run()
    assert not first.exception
    assert not second.exception
    assert second.query_params["sid"] == [sid]
    assert second.session_state["probe.instance"] != first.session_state["probe.instance"]
    assert second.text_input(key="ui.probe.shared").value == ""
