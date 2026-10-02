"""F6: keyword groups are proposals a person reviews; the fake copies the backend's rules."""

import httpx

from frontend import keywords as kw
from frontend.tests.fake_backend import FakeBackend
from frontend.tests.test_f2_app import DS, fake, open_app, text, with_data  # noqa: F401

ID = DS["dataset_id"]
PAGE = "views/keywords.py"


def runs(fake):
    return fake.bodies("POST", f"/datasets/{ID}/keyword-groups/run")


def actions(fake):
    return fake.bodies("POST", f"/datasets/{ID}/keyword-groups/actions")


def gid(fake, label):
    return next(g["group_id"] for g in fake._prep(ID)["kw"].values() if g["label"] == label)


def headings(app):
    """Each group's heading is its tick box's label."""
    return [c.label for c in app.checkbox if c.key.startswith(f"ui.kw.{ID}.tick.")]


def ticks(app):
    return {c.key.rsplit(".", 1)[-1]: c.value for c in app.checkbox if c.key.startswith(f"ui.kw.{ID}.tick.")}


def proposed(fake):
    """The page with groups proposed from the column the person chose."""
    sid = with_data(fake, page="keywords")
    app = open_app(sid, page=PAGE)
    app.selectbox(key=f"ui.kw.{ID}.column").select("campaign").run()
    app.button(key=f"ui.kw.{ID}.run").click().run()
    return sid, app


def test_nothing_is_chosen_or_proposed_for_the_person(fake):
    app = open_app(with_data(fake, page="keywords"), page=PAGE)
    assert not app.exception
    assert app.selectbox(key=f"ui.kw.{ID}.column").value is None
    assert app.button(key=f"ui.kw.{ID}.run").disabled
    assert "No groups yet: choose the column and propose them." in text(app)
    assert runs(fake) == []


def test_propose_sends_the_chosen_column_once_and_shows_every_group(fake):
    _, app = proposed(fake)
    assert not app.exception
    assert runs(fake) == [{"column": "campaign"}]
    page = text(app)
    assert "**sushi delivery** · transactional · 3 keyword(s) · proposal" in headings(app)
    assert "proposed by rules · area: baner · delivery: delivery" in page
    assert "sushi delivery baner, sushi delivery pune, sushi home delivery" in page
    assert "4 group(s) · 7 keyword(s) · 0 approved (0 keyword(s))" in page
    assert "Last run: column `campaign` · 7 keyword(s) read · 4 proposal(s)" in page
    assert "`sushii` → `sushi`" in page
    assert set(ticks(app).values()) == {False}                      # nothing pre-ticked
    assert app.button(key=f"ui.kw.{ID}.approve").disabled


def test_ticked_groups_are_approved_and_the_ticks_clear(fake):
    sid, app = proposed(fake)
    a, b = gid(fake, "sushi delivery"), gid(fake, "sushi · near me")
    app.checkbox(key=f"ui.kw.{ID}.tick.{b}").check().run()
    app.checkbox(key=f"ui.kw.{ID}.tick.{a}").check().run()
    saved = fake.sessions[sid]["ui_state"]["drafts"][ID]["keywords"]
    assert saved == {"column": "campaign", "ticks": {a: True, b: True}}   # ids, never keywords
    assert app.button(key=f"ui.kw.{ID}.approve").label == "Approve the 2 ticked"
    app.button(key=f"ui.kw.{ID}.approve").click().run()
    assert actions(fake) == [{"action": "approve", "group_ids": [a, b]}]
    page = text(app)
    assert "Approved 2 group(s)." in page
    assert "**sushi delivery** · transactional · 3 keyword(s) · ✅ approved" in headings(app)
    assert "4 group(s) · 7 keyword(s) · 2 approved (5 keyword(s))" in page
    assert set(ticks(app).values()) == {False}
    assert "ticks" not in fake.sessions[sid]["ui_state"]["drafts"][ID]["keywords"]


def test_ticks_and_the_column_survive_a_refresh(fake):
    sid, app = proposed(fake)
    a = gid(fake, "sushi delivery")
    app.checkbox(key=f"ui.kw.{ID}.tick.{a}").check().run()
    refreshed = open_app(sid, page=PAGE)
    assert refreshed.selectbox(key=f"ui.kw.{ID}.column").value == "campaign"
    assert ticks(refreshed)[a] is True
    assert runs(fake) == [{"column": "campaign"}]                   # a refresh sends nothing


def test_merging_an_approved_group_into_a_proposal_warns_and_sends_the_target_first(fake):
    sid, app = proposed(fake)
    a, b = gid(fake, "sushi delivery"), gid(fake, "sushi · near me")
    fake._prep(ID)["kw"][a]["approved"] = True
    app = open_app(sid, page=PAGE)
    app.checkbox(key=f"ui.kw.{ID}.tick.{a}").check().run()
    app.checkbox(key=f"ui.kw.{ID}.tick.{b}").check().run()
    app.button(key=f"ui.kw.{ID}.merge.go").click().run()           # no target chosen yet
    assert "Choose the group to merge into first." in text(app) and actions(fake) == []
    app.selectbox(key=f"ui.kw.{ID}.merge.into").select(b).run()
    page = text(app)
    assert "The merged group is a proposal, like 'sushi · near me'" in page
    assert ("'sushi delivery' is approved; merging it into a proposal withdraws its approval "
            "(3 keyword(s)).") in page
    app.button(key=f"ui.kw.{ID}.merge.go").click().run()
    assert actions(fake) == [{"action": "merge", "group_ids": [b, a]}]
    merged = fake._prep(ID)["kw"][b]
    assert len(merged["keywords"]) == 5 and not merged["approved"] and a not in fake._prep(ID)["kw"]
    assert "Merged 2 groups." in text(app)


def test_rename_split_and_move_send_exactly_what_the_person_chose(fake):
    _, app = proposed(fake)
    a, b = gid(fake, "sushi delivery"), gid(fake, "sushi · near me")
    e = lambda name: f"ui.kw.{ID}.edit.{a}.{name}"  # noqa: E731
    app.selectbox(key=f"ui.kw.{ID}.group").select(a).run()
    app.text_input(key=e("label")).input("Sushi to your door").run()
    app.button(key=e("rename")).click().run()
    app.multiselect(key=e("split")).select("sushi home delivery").run()
    assert "The keywords split off become a new proposal until you approve it." in text(app)
    app.button(key=e("split_go")).click().run()
    app.selectbox(key=e("move")).select("sushi delivery baner").run()
    app.selectbox(key=e("to")).select(b).run()
    assert "Its approval does not change." in text(app)
    app.button(key=e("move_go")).click().run()
    assert actions(fake) == [
        {"action": "rename", "group_ids": [a], "label": "Sushi to your door"},
        {"action": "split", "group_ids": [a], "keywords": ["sushi home delivery"]},
        {"action": "move_keyword", "group_ids": [a], "keyword": "sushi delivery baner",
         "target_group_id": b}]
    shown = headings(app)
    assert "**Sushi to your door** · transactional · 1 keyword(s) · proposal" in shown
    assert "**Sushi to your door (split)** · transactional · 1 keyword(s) · proposal" in shown
    assert "**sushi · near me** · local · 3 keyword(s) · proposal" in shown
    assert not app.exception


def test_the_backends_refusals_are_shown_as_they_come(fake):
    _, app = proposed(fake)
    c = gid(fake, "menu · brand")
    e = lambda name: f"ui.kw.{ID}.edit.{c}.{name}"  # noqa: E731
    app.selectbox(key=f"ui.kw.{ID}.group").select(c).run()
    app.multiselect(key=e("split")).select("hana menu").run()       # all of its keywords
    app.button(key=e("split_go")).click().run()
    assert "Not split: split needs some (not all) of the group's keywords" in text(app)
    assert "code: bad_action · HTTP 422" in text(app)
    app.button(key=e("rename")).click().run()                       # no name typed
    assert "Not renamed: rename needs label" in text(app)
    assert [a["action"] for a in actions(fake)] == ["split", "rename"]


def test_proposing_again_says_what_it_replaces_and_keeps_approved_groups(fake):
    sid, app = proposed(fake)
    assert app.button(key=f"ui.kw.{ID}.run").label == "Replace the 4 proposals"
    assert "Proposing again replaces the 4 group(s) not yet approved" in text(app)
    a = gid(fake, "sushi delivery")
    app.checkbox(key=f"ui.kw.{ID}.tick.{a}").check().run()
    app.button(key=f"ui.kw.{ID}.approve").click().run()
    app.button(key=f"ui.kw.{ID}.run").click().run()
    assert runs(fake) == [{"column": "campaign"}, {"column": "campaign"}]
    groups = fake._prep(ID)["kw"]
    assert groups[a]["approved"] and len(groups) == 4
    assert app.button(key=f"ui.kw.{ID}.run").label == "Replace the 3 proposals"


def test_the_grouping_tool_links_here_and_drops_the_cached_groups(fake):
    sid = with_data(fake, page="keywords")
    fake._prep(ID)["domains"] = ["marketing"]
    app = open_app(sid, page=PAGE)
    assert fake.count("GET", f"/datasets/{ID}/keyword-groups") == 1
    app.switch_page("views/tools.py").run()
    app.selectbox(key=f"ui.tools.{ID}.tool").select("marketing.keyword_grouping").run()
    assert "Running this replaces every keyword group not yet approved" in text(app)
    app.button(key="tools.run").click().run()
    app.switch_page(PAGE).run()
    assert fake.count("GET", f"/datasets/{ID}/keyword-groups") == 2
    assert not app.exception


def group(label, approved=False, keywords=("a", "b")):
    return {"group_id": label, "label": label, "intent": "local", "keywords": list(keywords),
            "facets": {}, "approved": approved}


def test_consequences_follow_the_backends_rules():
    approved, proposal = group("A", True), group("P")
    assert kw.merge_notes(approved, [proposal])[0].startswith("The merged group keeps the name and "
                                                              "approval of 'A'")
    assert kw.merge_notes(approved, [proposal])[1] == []
    assert kw.merge_notes(proposal, [approved])[1] == [
        "'A' is approved; merging it into a proposal withdraws its approval (2 keyword(s))."]
    assert kw.move_note(proposal, approved) == "'A' is approved, so the keyword becomes approved."
    assert kw.move_note(approved, proposal).startswith("'P' is a proposal, so the keyword is no longer")
    assert kw.move_note(proposal, group("Q")) == "Its approval does not change."
    assert "no longer approved" in kw.split_note(approved) and "no longer" not in kw.split_note(proposal)


def test_filters_and_ticks_read_the_listing_only():
    groups = [group("A", True, ["sushi near me"]), group("P", False, ["ramen pune"])]
    assert [g["label"] for g in kw.visible(groups, "Proposals", [], "")] == ["P"]
    assert [g["label"] for g in kw.visible(groups, "All", [], "SUSHI")] == ["A"]
    assert kw.visible(groups, "All", ["informational"], "") == []
    assert kw.ticked({"P": True, "gone": True, "A": False}, groups) == ["P"]
    assert kw.facets_text({"area": ["baner", "aundh"], "dish": "sushi"}) == "area: baner, aundh · dish: sushi"


def test_fake_keyword_replies_carry_the_specs_required_keys(contract):
    fake = FakeBackend()
    did = fake.add_dataset("ws_0123456789ab", "kw")["dataset_id"]
    with httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(fake.handle)) as http:
        none = http.post(f"/datasets/{did}/keyword-groups/run", json={})
        listing = http.post(f"/datasets/{did}/keyword-groups/run", json={"column": "campaign"}).json()
        bad = http.post(f"/datasets/{did}/keyword-groups/actions", json={"action": "merge",
                                                                         "group_ids": ["nope"]})
    schemas = contract["components"]["schemas"]
    assert none.status_code == 422 and none.json()["error"]["code"] == "needs_data"
    assert set(schemas["KeywordGroups"]["required"]) <= set(listing)
    assert all(set(schemas["KeywordGroup"]["required"]) <= set(g) for g in listing["groups"])
    assert {g["intent"] for g in listing["groups"]} <= set(schemas["KeywordGroup"]["properties"]
                                                          ["intent"]["enum"])
    assert bad.status_code == 404 and set(schemas["ErrorBody"]["required"]) <= set(bad.json())
