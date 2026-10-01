"""The F2 fake backend answers in the spec's shapes (required keys of each schema)."""

import httpx

from frontend.tests.fake_backend import FakeBackend


def required(contract, name):
    return set(contract["components"]["schemas"][name].get("required", []))


def test_fake_responses_carry_every_required_key_of_the_spec(contract):
    fake = FakeBackend()
    with httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(fake.handle)) as http:
        created = http.post("/sessions", json={}).json()
        sid = created["sid"]
        session = http.get(f"/sessions/{sid}").json()
        saved = http.put(f"/sessions/{sid}/ui-state", json={"ui_state": {}, "version": 1}).json()
        stale = http.put(f"/sessions/{sid}/ui-state", json={"ui_state": {}, "version": 1})
        refused = http.put(f"/sessions/{sid}/ui-state",
                           json={"ui_state": {"label": "a@example.com"}, "version": 2})
        missing = http.get("/sessions/0b8e0c64-5b0e-4c55-9c1e-2f1c2b9d7a11")
        turn_id = http.post("/turns", json={"sid": sid, "dataset_id": "ds_x",
                                            "question": "Why?"}).json()["turn_id"]
        turn = http.get(f"/turns/{turn_id}").json()
        listed = http.get(f"/sessions/{sid}/turns").json()
        upload = http.post(f"/workspaces/{session['workspace_id']}/uploads",
                           files={"file": ("ads.csv", b"a,b\n1,2\n", "text/csv")}).json()
        dataset = http.get(f"/datasets/{upload['dataset_id']}").json()
    fake.finish_turn(turn_id)

    assert required(contract, "SessionCreated") <= set(created)
    assert required(contract, "Session") <= set(session) and required(contract, "Session") <= set(saved)
    assert stale.status_code == 409 and required(contract, "VersionConflict") <= set(stale.json())
    assert required(contract, "Session") <= set(stale.json()["current"])
    assert refused.status_code == 422 and required(contract, "ErrorBody") <= set(refused.json())
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert required(contract, "Turn") <= set(turn) and required(contract, "TurnList") <= set(listed)
    assert required(contract, "Turn") <= set(fake.turns[turn_id])
    assert required(contract, "Dataset") <= set(upload) and required(contract, "Dataset") <= set(dataset)
    for event in fake.turns[turn_id]["events"]:
        assert required(contract, "TurnEvent") <= set(event)
    statuses = contract["components"]["schemas"]["Turn"]["properties"]["status"]["enum"]
    assert {turn["status"], fake.turns[turn_id]["status"]} <= set(statuses)
