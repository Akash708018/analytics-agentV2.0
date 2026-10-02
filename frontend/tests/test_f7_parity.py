"""F7: the v1 parity ports. Every figure downloads as shown; a new session leaves the old one."""

import copy

from frontend import prep
from frontend.tests.test_f2_app import DS, fake, open_app, text, with_data  # noqa: F401
from frontend.tests.test_f4_app import ready

ID = DS["dataset_id"]


def test_the_csv_is_the_results_table_cell_for_cell():
    figures = [{"name": "Spend by group: search", "value": 560.0, "unit": "cost (sum)",
                "provenance": "contract"},
               {"name": "CPA: tiny, group", "value": None, "unit": None, "provenance": "derived"},
               {"name": "ROAS", "value": 3.2000000001, "unit": "x", "provenance": "contract"}]
    assert prep.figures_csv(figures) == (
        "figure,value,unit,source\n"
        "Spend by group: search,560.0,cost (sum),contract\n"
        '"CPA: tiny, group",suppressed,,derived\n'           # a comma is quoted; null as shown
        "ROAS,3.2000000001,x,contract\n")                     # no rounding
    rows = [line.split(",") for line in prep.figures_csv(figures[:1]).splitlines()[1:]]
    assert rows == [list(prep.figure_rows(figures[:1])[0].values())]


def test_a_tool_result_offers_every_figure_as_csv(fake):
    app = open_app(ready(fake), page="views/tools.py")
    app.selectbox(key=f"ui.tools.{ID}.tool").select("marketing.channel_efficiency").run()
    app.button(key="tools.run").click().run()
    assert not app.exception
    buttons = app.get("download_button")
    assert [b.label for b in buttons] == ["Download every figure (CSV)"]
    assert buttons[0].key == f"tools.{ID}.marketing.channel_efficiency.result.csv"


def test_an_answer_resting_on_the_same_tool_twice_gets_two_downloads(fake):
    sid = with_data(fake, page="ask")
    app = open_app(sid, page="views/ask.py")
    app.text_area(key="ask.question").input("Where is spend wasted?")
    app.button(key="FormSubmitter:ask.form-Ask").click().run()
    turn_id, = fake.turns
    fake.answer_turn(turn_id)
    answer = fake.turns[turn_id]["answer"]
    answer["results"].append(copy.deepcopy(answer["results"][0]))
    app.run()
    assert not app.exception                                   # no duplicate widget key
    assert [b.key for b in app.get("download_button")] == [f"ask.{turn_id}.0.csv",
                                                            f"ask.{turn_id}.1.csv"]


def test_a_new_session_starts_empty_and_leaves_the_old_one_at_its_link(fake):
    old = with_data(fake, page="session", label="Q3 review")
    app = open_app(old)
    assert "copy the address first to come back to it" in text(app)
    before = copy.deepcopy(fake.sessions[old])
    app.button(key="session.new").click().run()
    assert not app.exception
    assert fake.count("POST", "/sessions") == 1
    new = app.query_params["sid"][0]
    assert new != old and new in fake.sessions
    assert fake.sessions[old] == before                        # untouched
    assert app.text_input(key="ui.session.label").value == ""   # nothing carried over
    assert fake.sessions[new]["workspace_id"] != before["workspace_id"]
