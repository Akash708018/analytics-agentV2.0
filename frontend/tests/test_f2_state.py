"""F2 session layer without Streamlit: allowlist, one save per change, conflicts, turns."""

import httpx
import pytest

from frontend import state, turns
from frontend.api_client import APIClient
from frontend.tests.fake_backend import FakeBackend

DS = {"dataset_id": "ds_0123456789ab", "name": "ads", "rows": 4, "columns": 8}


@pytest.fixture
def fake():
    return FakeBackend()


@pytest.fixture
def api(fake):
    with fake.client_factory()() as client:
        yield client


def hydrated(fake, api, ui_state=None):
    sid = fake.new_session(ui_state)
    ss = {}
    assert state.hydrate(ss, api, sid) is None
    return sid, ss


@pytest.mark.parametrize("raw,expected", [
    ("0B8E0C64-5B0E-4C55-9C1E-2F1C2B9D7A11", "0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11"),
    ("0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11", "0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11"),
    ("0b8e0c645b0e4c559c1e2f1c2b9d7a11", None),     # not the canonical form
    ("../sessions", None), ("", None), (None, None), (["a"], None),
])
def test_only_a_canonical_uuid_is_a_sid(raw, expected):
    assert state.parse_sid(raw) == expected


def test_normalize_keeps_only_the_allowlist_and_unknown_server_keys():
    raw = {
        "page": "nowhere", "label": "x" * 500, "dataset_id": "ds_missing",
        "datasets": [{**DS, "sample_rows": [[1, 2]]}, DS["dataset_id"], {"dataset_id": "bad id"}],
        "drafts": {DS["dataset_id"]: {"clean": {"C001|K|": True, "C002|K|": False},
                                      "contract": {"roles": {"cost": "measure", "x": "boss"},
                                                   "window_start": "not a date", "rows": [[1]]},
                                      "forks": {"tax_basis": "net_excl_gst"},
                                      "confirmed_version": 2, "rows": [[1, 2]]},
                   "ds_not_listed": {"forks": {"a": "b"}}},
        "from_a_newer_frontend": {"keep": 1},
    }
    out = state.normalize(raw)
    assert out == {
        "schema": 2, "page": "session", "label": "x" * 120, "dataset_id": None,
        "datasets": [DS["dataset_id"]],
        "drafts": {DS["dataset_id"]: {"clean": {"C001|K|": True},
                                      "contract": {"roles": {"cost": "measure"}},
                                      "forks": {"tax_basis": "net_excl_gst"},
                                      "confirmed_version": 2}},
        "from_a_newer_frontend": {"keep": 1},
    }


def test_schema_1_dataset_objects_migrate_to_ids():
    # F2 saved {dataset_id, name, rows, columns}; the backend refuses 20 of them as data
    # rows, and names can look like phone numbers (C8). Schema 2 keeps ids only.
    v1 = {"schema": 1, "dataset_id": DS["dataset_id"],
          "datasets": [DS, {**DS, "dataset_id": "ds_ffffffffffff", "name": "leads_9876543210"}]}
    out = state.normalize(v1)
    assert out["datasets"] == [DS["dataset_id"], "ds_ffffffffffff"]
    assert out["dataset_id"] == DS["dataset_id"]
    assert "9876543210" not in str(out)
    many = state.normalize({"datasets": [f"ds_{i:012x}" for i in range(60)]})
    assert len(many["datasets"]) == 50 and all(isinstance(d, str) for d in many["datasets"])


def test_an_unchanged_draft_sends_nothing_and_an_edit_sends_one_put(fake, api):
    sid, ss = hydrated(fake, api)
    assert state.maybe_save(ss, api) == {"status": "saved"}
    assert fake.count("PUT") == 0
    ss["_draft"]["label"] = "Q3"
    state.maybe_save(ss, api)
    state.maybe_save(ss, api)
    assert fake.bodies("PUT") == [{"ui_state": {"schema": 2, "page": "session", "label": "Q3",
                                                "dataset_id": None, "datasets": [], "drafts": {}},
                                   "version": 1}]
    assert ss["_server"]["version"] == 2


def test_session_state_beyond_the_allowlist_is_never_sent(fake, api):
    sid, ss = hydrated(fake, api)
    ss.update({"data.file": object(), "ask.question": "free text", "ui.session.label": "Q3",
               "_work": {"cache": {("record", "ds_x"): {"rows": 4}}, "results": {}, "reseed": []}})
    state.on_change(ss, "ui.session.label", ("label",))
    state.maybe_save(ss, api)
    body, = fake.bodies("PUT")
    assert set(body["ui_state"]) == {"schema", "page", "label", "dataset_id", "datasets", "drafts"}


def test_conflict_stops_saving_until_the_person_chooses(fake, api):
    sid, ss = hydrated(fake, api)
    fake.write_elsewhere(sid, {"label": "theirs"})
    ss["_draft"]["label"] = "mine"
    assert state.maybe_save(ss, api)["status"] == "conflict"
    state.maybe_save(ss, api)
    assert fake.count("PUT") == 1
    assert ss["_save"]["current"]["version"] == 2


def test_load_latest_adopts_theirs_and_keeps_both_dataset_references(fake, api):
    other = {**DS, "dataset_id": "ds_ffffffffffff", "name": "other"}
    sid, ss = hydrated(fake, api)
    fake.write_elsewhere(sid, {"label": "theirs", "datasets": [other]})
    ss["_draft"]["label"] = "mine"
    ss["_draft"]["datasets"] = [DS["dataset_id"]]
    ss["_draft"]["drafts"] = {DS["dataset_id"]: {"forks": {"tax_basis": "net_excl_gst"}}}
    ss["ui.session.label"] = "mine"
    state.maybe_save(ss, api)
    state.load_latest(ss)
    assert ss["_draft"]["label"] == "theirs"
    assert ss["_draft"]["datasets"] == ["ds_ffffffffffff", DS["dataset_id"]]
    assert ss["_draft"]["drafts"][DS["dataset_id"]] == {"forks": {"tax_basis": "net_excl_gst"}}
    assert "ui.session.label" not in ss          # reseeded from the draft on next render
    state.maybe_save(ss, api)                    # the union is new to the server: saved once
    assert fake.bodies("PUT")[-1]["version"] == 2
    assert fake.sessions[sid]["ui_state"]["label"] == "theirs"


def test_keep_mine_saves_over_the_newer_version(fake, api):
    sid, ss = hydrated(fake, api)
    fake.write_elsewhere(sid, {"label": "theirs"})
    ss["_draft"]["label"] = "mine"
    state.maybe_save(ss, api)
    state.keep_mine(ss)
    assert state.maybe_save(ss, api) == {"status": "saved"}
    assert fake.bodies("PUT")[-1]["version"] == 2
    assert fake.sessions[sid]["ui_state"]["label"] == "mine"
    assert fake.sessions[sid]["version"] == 3


def test_a_refused_value_is_not_retried_until_it_changes(fake, api):
    sid, ss = hydrated(fake, api)
    ss["_draft"]["label"] = "write to someone@example.com"
    save = state.maybe_save(ss, api)
    assert (save["status"], save["code"]) == ("rejected", "ui_state_rejected")
    state.maybe_save(ss, api)
    assert fake.count("PUT") == 1
    ss["_draft"]["label"] = "Q3"
    assert state.maybe_save(ss, api) == {"status": "saved"}
    assert fake.count("PUT") == 2


def test_a_lost_reply_to_our_own_write_is_not_a_conflict(fake, api):
    sid, ss = hydrated(fake, api)
    path = f"/sessions/{sid}/ui-state"
    fake.fail_on("PUT", path, httpx.ReadTimeout, stored=True)
    ss["_draft"]["label"] = "Q3"
    assert state.maybe_save(ss, api)["status"] == "error"
    assert fake.sessions[sid]["version"] == 2    # the server did store it
    assert state.maybe_save(ss, api) == {"status": "saved"}
    assert ss["_server"]["version"] == 2
    assert fake.count("PUT") == 2


def test_another_tab_that_only_changed_page_is_not_a_conflict(fake, api):
    sid, ss = hydrated(fake, api, {"label": "Q3"})
    fake.write_elsewhere(sid, {**fake.sessions[sid]["ui_state"], "page": "ask"})
    ss["_draft"]["page"] = "data"
    assert state.maybe_save(ss, api) == {"status": "saved"}       # adopted version 2
    assert state.maybe_save(ss, api) == {"status": "saved"}       # then this tab's page
    assert [b["version"] for b in fake.bodies("PUT")] == [1, 2]
    assert fake.sessions[sid]["ui_state"]["page"] == "data"


def test_an_expired_session_stops_saving(fake, api):
    sid, ss = hydrated(fake, api)
    del fake.sessions[sid]
    ss["_draft"]["label"] = "Q3"
    assert state.maybe_save(ss, api)["status"] == "expired"
    state.maybe_save(ss, api)
    assert fake.count("PUT") == 1


def test_the_saved_page_is_restored_once_and_only_from_the_default_route(fake, api):
    sid, ss = hydrated(fake, api, {"page": "ask"})
    assert state.take_restore_target(ss, "session") == "ask"
    assert state.take_restore_target(ss, "session") is None
    sid, ss = hydrated(fake, api, {"page": "ask"})
    assert state.take_restore_target(ss, "data") is None


def test_an_upload_records_display_metadata_and_becomes_active(fake, api):
    sid, ss = hydrated(fake, api)
    added = state.add_dataset(ss["_draft"], {**DS, "workspace_id": "ws_x",
                                             "assumptions": ["a"], "created_at": "t"})
    assert added == DS["dataset_id"]
    assert ss["_draft"]["datasets"] == [DS["dataset_id"]]          # ids only (C8)
    assert ss["_draft"]["dataset_id"] == DS["dataset_id"]


class StoppedRun(Exception):
    """Streamlit's RerunException, raised at a session-state access."""


class Interruptible(dict):
    """Session state that, once armed, stops the run at the next access (as Streamlit does
    when a newer rerun is waiting)."""
    armed = False

    def __getitem__(self, key):
        if self.armed:
            raise StoppedRun(key)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if self.armed:
            raise StoppedRun(key)
        super().__setitem__(key, value)


def test_a_save_stopped_after_its_put_still_records_the_new_version(fake, api):
    # F2 browser finding: the PUT landed, the run stopped before recording v2, and the next
    # run reported a conflict with its own write.
    sid = fake.new_session()
    ss = Interruptible()
    assert state.hydrate(ss, api, sid) is None
    ss["_draft"]["page"] = "data"
    fake.after = lambda method, path: setattr(ss, "armed", method == "PUT")
    state.maybe_save(ss, api)                    # must not touch ss after the PUT
    ss.armed, fake.after = False, None
    assert ss["_server"]["version"] == 2
    ss["_draft"]["label"] = "typed, no Enter"
    assert state.maybe_save(ss, api) == {"status": "saved"}
    assert [b["version"] for b in fake.bodies("PUT")] == [1, 2]


def test_a_widget_edit_whose_callback_was_cut_short_is_kept(fake, api):
    sid, ss = hydrated(fake, api)
    ss["ui.session.label"] = "edited"            # the widget has it; the draft does not
    state.bind(ss, "ui.session.label", ("label",), "")
    assert ss["_draft"]["label"] == "edited"


class Recording(dict):
    def __init__(self, *args):
        super().__init__(*args)
        self.writes = []

    def __setitem__(self, key, value):
        self.writes.append((key, value))
        super().__setitem__(key, value)


def test_bind_sends_the_saved_value_on_every_run_not_only_the_first():
    """C13: a widget the browser rebuilt while the server kept its key showed its empty
    default over the saved name. Setting the key each run makes the server send the value."""
    ss = Recording({state.DRAFT: {"label": "F4 check"}})
    state.bind(ss, "ui.session.label", ("label",), "")
    state.bind(ss, "ui.session.label", ("label",), "")      # the key is already there
    assert ss.writes == [("ui.session.label", "F4 check")] * 2
    assert ss[state.DRAFT]["label"] == "F4 check"


def test_submit_sends_once(fake, api):
    sid = fake.new_session()
    out = turns.submit(api, sid, "ds_x", "Why?", [])
    assert out.outcome == "sent" and out.turn_id in fake.turns
    assert fake.count("POST", "/turns") == 1


def test_a_refused_question_is_an_error_not_a_lookup(fake, api):
    out = turns.submit(api, "0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11", "ds_x", "Why?", [])
    assert (out.outcome, out.error.code) == ("error", "not_found")
    assert fake.count("GET") == 0


@pytest.mark.parametrize("stored,expected", [(True, "received"), (False, "not_received")])
def test_a_timed_out_question_is_looked_up_and_never_resent(fake, api, stored, expected):
    sid = fake.new_session()
    earlier = turns.submit(api, sid, "ds_x", "Why?", []).turn_id     # same text, older turn
    fake.fail_on("POST", "/turns", httpx.ReadTimeout, stored=stored)
    out = turns.submit(api, sid, "ds_x", "Why?", [earlier])
    assert out.outcome == expected
    assert out.turn_id != earlier
    assert fake.count("POST", "/turns") == 2                         # one each; no resend


def test_a_lookup_that_fails_too_is_unknown(fake, api):
    sid = fake.new_session()
    fake.fail_on("POST", "/turns", httpx.ConnectError, stored=False)
    fake.fail_on("GET", f"/sessions/{sid}/turns", httpx.ConnectError, stored=False)
    assert turns.submit(api, sid, "ds_x", "Why?", []).outcome == "unknown"
