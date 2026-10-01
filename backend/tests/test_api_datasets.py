"""B2 through HTTP: upload -> profile -> detect -> confirm domain -> contract (forks) -> tools."""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.engine import workspace

ADS = Path(__file__).resolve().parent / "fixtures_v2" / "ads_small.csv"


@pytest.fixture
def env(tmp_path):
    ws = f"ws_{secrets.token_hex(6)}"
    c = TestClient(create_app(state_dir=tmp_path))
    r = c.post(f"/workspaces/{ws}/uploads", files={"file": ("ads_small.csv", ADS.read_bytes(),
                                                             "text/csv")})
    assert r.status_code == 201, r.text
    yield c, r.json()
    workspace.reset(ws)


def test_upload_returns_dataset_with_assumptions(env):
    _, d = env
    assert d["rows"] == 120 and d["columns"] == 8 and d["name"] == "ads_small"
    assert d["dataset_id"].startswith("ds_") and d["assumptions"]


def test_bad_workspace_and_bad_file_are_refused(tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    r = c.post("/workspaces/..%2Fetc/uploads", files={"file": ("a.csv", b"a\n1\n")})
    assert r.status_code in (404, 422)
    ws = f"ws_{secrets.token_hex(6)}"
    r = c.post(f"/workspaces/{ws}/uploads", files={"file": ("a.exe", b"MZ")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "upload_refused"
    workspace.reset(ws)


def test_profile(env):
    c, d = env
    p = c.get(f"/datasets/{d['dataset_id']}/profile").json()
    assert p["rows"] == 120 and [x["name"] for x in p["columns"]][:2] == ["date", "campaign"]
    assert all(0 <= x["null_pct"] <= 100 for x in p["columns"])


def test_detect_then_confirm_domain_then_tools(env):
    c, d = env
    did = d["dataset_id"]
    det = c.get(f"/datasets/{did}/domains/detect").json()
    assert det["domains"][0]["domain"] == "marketing" and det["confirmed"] == []
    assert det["marketing_sources"][0]["source"] == "paid_ads"
    assert c.post(f"/datasets/{did}/domains/confirm",
                  json={"domains": ["nonsense"]}).status_code == 422
    assert c.post(f"/datasets/{did}/domains/confirm",
                  json={"domains": ["marketing"]}).json()["ok"] is True
    assert c.get(f"/datasets/{did}/domains/detect").json()["confirmed"] == ["marketing"]
    tools = c.get(f"/datasets/{did}/tools").json()["tools"]
    ids = {t["tool_id"]: t["status"] for t in tools}
    assert ids["core.profile"] == "active" and ids["core.summary_stats"] == "active"
    assert all(t["status"] in ("active", "needs_domain", "needs_data") for t in tools)


def test_contract_proposal_asks_forks_and_suggests_without_applying(env):
    c, d = env
    p = c.get(f"/datasets/{d['dataset_id']}/contract/proposal").json()
    forks = {f["fork_id"]: f for f in p["forks"]}
    assert {"tax_basis", "fiscal_year", "timezone"} <= set(forks)
    assert forks["tax_basis"]["suggested"] == "net_excl_gst" and forks["tax_basis"][
        "suggested_reason"]
    assert forks["timezone"]["suggested"] is None           # no silent default
    cost = next(m for m in p["measures"] if m["column"] == "cost")
    assert cost["suggested_agg"] == "sum" and cost["agg"] == ""    # suggested, not applied
    assert p["provisional"]


def _contract(p: dict) -> dict:
    return {"grain": "one row per campaign per day", "primary_key": ["date", "campaign"],
            "date_column": "date",
            "measures": [m["column"] for m in p["measures"]],
            "dimensions": ["campaign", "channel"],
            "aggregations": {m["column"]: m["suggested_agg"] for m in p["measures"]},
            "measure_definitions": {m["column"]: f"{m['column']} as exported"
                                    for m in p["measures"]},
            "analysis_window_start": "2026-09-01", "analysis_window_end": "2026-09-30"}


def test_contract_confirm_requires_every_fork(env):
    c, d = env
    did = d["dataset_id"]
    p = c.get(f"/datasets/{did}/contract/proposal").json()
    r = c.post(f"/datasets/{did}/contract/confirm",
               json={"contract": _contract(p), "fork_choices": {"tax_basis": "net_excl_gst"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "forks_unanswered"
    assert set(r.json()["missing"]) == {"fiscal_year", "timezone"}
    r = c.post(f"/datasets/{did}/contract/confirm",
               json={"contract": _contract(p), "fork_choices": {
                   "tax_basis": "bogus", "fiscal_year": "april", "timezone": "ist"}})
    assert r.status_code == 422 and r.json()["invalid"] == ["tax_basis=bogus"]
    r = c.post(f"/datasets/{did}/contract/confirm",
               json={"contract": _contract(p), "fork_choices": {
                   "tax_basis": "net_excl_gst", "fiscal_year": "april", "timezone": "ist"}})
    assert r.status_code == 200, r.text


def test_cleaning_proposals_shape(env):
    c, d = env
    r = c.get(f"/datasets/{d['dataset_id']}/cleaning/proposals")
    assert r.status_code == 200 and isinstance(r.json()["proposals"], list)
    ok = c.post(f"/datasets/{d['dataset_id']}/cleaning/approve", json={"approve": []})
    assert ok.json() == {"applied": [], "rejected": [], "ledger_entries": 0}


def test_packs_endpoints(tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    ids = [p["pack_id"] for p in c.get("/packs").json()["packs"]]
    assert ids == ["core", "logistics", "marketing"]
    core = c.get("/packs/core").json()
    assert len(core["pack"]["festivals"]) == 6
    assert c.get("/packs/nope").status_code == 404


def test_unknown_dataset_is_404(tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    assert c.get("/datasets/ds_nope/profile").status_code == 404
