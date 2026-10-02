"""B7: keyword grouping -- the numpy boundary, the pipeline (scored against the DRAFT gold),
and the API: proposals, the person's actions, and the engine reading approved groups only."""
from __future__ import annotations

import csv
import io
import re
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.engine import workspace
from backend.tests.fixtures_v2 import keywords_gold as G
from backend.text.embed import Cache
from backend.text.pipeline import group_keywords, score

ENGINE = Path(__file__).resolve().parents[1] / "engine"


def test_engine_never_imports_numpy_pandas_or_sklearn():
    bad = [str(p) for p in ENGINE.rglob("*.py")
           if re.search(r"^\s*(import|from)\s+(numpy|pandas|sklearn)\b", p.read_text(), re.M)]
    assert bad == []


def test_gold_is_marked_as_the_authors_draft():
    assert G.STATUS == "pending_user_approval"


@pytest.fixture(scope="module")
def grouped():
    return group_keywords(G.KEYWORDS, backend="chargram")


def test_pipeline_against_the_authors_draft_gold(grouped):
    s = score([g.keywords for g in grouped["groups"]], G.LABEL)
    print(f"\nagainst the AUTHOR'S DRAFT gold (pending user approval): {s}, "
          f"embedding {grouped['embedding']}, threshold {grouped['threshold']}")
    assert grouped["embedding"] == "chargram/tfidf-char2-4"
    assert s["purity"] >= 0.85 and s["completeness"] >= 0.75


def test_typos_and_spelling_variants_are_merged(grouped):
    assert {"sushii": "sushi", "shushi": "sushi", "japnese": "japanese",
            "raman": "ramen"}.items() <= grouped["typos_merged"].items()


def test_leading_facet_split_keeps_area_as_an_attribute(grouped):
    where = {k: g for g in grouped["groups"] for k in g.keywords}
    assert where["sushi delivery kalyani nagar"] is where["sushi delivery pune"]
    assert where["sushi near me"] is not where["sushi delivery pune"]
    g = where["sushi delivery pune"]
    assert g.intent == "transactional" and "kalyani nagar" in g.facets["area"]
    assert where["hana menu"].intent == "navigational"
    assert where["what is sushi"].intent == "informational"


def test_labeler_proposes_and_a_bad_labeler_falls_back():
    kws = ["sushi delivery pune", "sushi home delivery", "sushi near me"]
    r = group_keywords(kws, backend="chargram",
                       labeler=lambda ks: {"label": "Sushi to your door", "intent":
                                           "transactional"})
    assert {g.proposed_by for g in r["groups"]} == {"llm"}
    r = group_keywords(kws, backend="chargram", labeler=lambda ks: 1 / 0)
    assert {g.proposed_by for g in r["groups"]} == {"rules"}


def test_embedding_cache_round_trip():
    c = Cache()
    c.put("t", "sushi", [0.6, 0.8])
    assert list(c.get("t", "sushi")) == [0.6, 0.8] and c.get("t", "ramen") is None


# --- through the API ---------------------------------------------------------------------------

def _rows():
    out = []
    for i, k in enumerate(G.KEYWORDS):
        page = "/sushi-delivery" if G.LABEL[k] == "sushi_delivery" else f"/{G.LABEL[k]}"
        if k in ("sushi home delivery", "order sushi online"):
            page = "/menu"                               # the group splits over two pages
        out.append([i, "2026-09-01", k, page, len(k), 10 * len(k), 5])
    return out


@pytest.fixture
def api(tmp_path):
    c = TestClient(create_app(state_dir=tmp_path))
    ws = f"ws_{secrets.token_hex(6)}"
    buf = io.StringIO()
    csv.writer(buf).writerows([["row_id", "date", "query", "page", "clicks", "impressions",
                                "position"], *_rows()])
    did = c.post(f"/workspaces/{ws}/uploads", files={"file": ("gsc_kw.csv",
                                                               buf.getvalue().encode())}).json()[
        "dataset_id"]
    c.post(f"/datasets/{did}/domains/confirm", json={"domains": ["marketing"]})
    p = c.get(f"/datasets/{did}/contract/proposal").json()
    r = c.post(f"/datasets/{did}/contract/confirm", json={"contract": {
        "grain": "one row per query", "primary_key": ["row_id"], "date_column": "date",
        "measures": ["clicks", "impressions", "position"], "dimensions": ["query", "page"],
        "aggregations": {"clicks": "sum", "impressions": "sum", "position": "mean"},
        "measure_definitions": {"clicks": "c", "impressions": "i", "position": "p"},
        "analysis_window_start": "2026-09-01", "analysis_window_end": "2026-09-30"},
        "fork_choices": {f["fork_id"]: f["options"][0]["id"] for f in p["forks"]}})
    assert r.status_code == 200, r.text
    yield c, did
    workspace.reset(ws)


def _group_of(listing, kw):
    return next(g for g in listing["groups"] if kw in g["keywords"])


def test_run_proposes_and_nothing_reaches_the_engine_before_approval(api):
    c, did = api
    got = c.post(f"/datasets/{did}/keyword-groups/run", json={}).json()
    assert got["run"]["column"] == "query" and got["run"]["embedding"].startswith("chargram")
    assert got["groups"] and not any(g["approved"] for g in got["groups"])
    r = c.post("/tools/marketing.keyword_group_performance/run",
               json={"dataset_id": did, "params": {}})
    assert r.status_code == 422 and "no keyword groups are approved" in r.json()["error"][
        "message"]


def test_approved_groups_totals_are_exact_and_the_rest_is_ungrouped(api):
    c, did = api
    got = c.post(f"/datasets/{did}/keyword-groups/run", json={}).json()
    g = _group_of(got, "sushi delivery pune")
    c.post(f"/datasets/{did}/keyword-groups/actions", json={
        "action": "rename", "group_ids": [g["group_id"]], "label": "Sushi delivery"})
    c.post(f"/datasets/{did}/keyword-groups/actions", json={
        "action": "approve", "group_ids": [g["group_id"]]})
    res = c.post("/tools/marketing.keyword_group_performance/run",
                 json={"dataset_id": did, "params": {}}).json()
    figs = {f["name"]: f["value"] for f in res["figures"]}
    want = sum(len(k) for k in g["keywords"])                  # clicks = len(keyword)
    assert figs["By group: Sushi delivery"] == want
    assert figs["By group: (ungrouped)"] == sum(len(k) for k in G.KEYWORDS) - want
    pt = c.post("/tools/marketing.page_targeting/run", json={"dataset_id": did,
                                                              "params": {}}).json()
    assert any("1 group(s) spread their clicks over more than one page (Sushi delivery)" in x
               for x in pt["caveats"])


def test_merge_move_split_and_their_errors(api):
    c, did = api
    got = c.post(f"/datasets/{did}/keyword-groups/run", json={}).json()
    a, b = _group_of(got, "sushi near me"), _group_of(got, "sushi koregaon park")
    act = lambda **kw: c.post(f"/datasets/{did}/keyword-groups/actions", json=kw)  # noqa: E731
    r = act(action="merge", group_ids=[a["group_id"], b["group_id"]], label="Sushi local")
    merged = _group_of(r.json(), "sushi near me")
    assert "sushi koregaon park" in merged["keywords"] and merged["label"] == "Sushi local"
    other = _group_of(r.json(), "sushi delivery pune")
    r = act(action="move_keyword", group_ids=[merged["group_id"]], keyword="sushi near me",
            target_group_id=other["group_id"])
    assert "sushi near me" in _group_of(r.json(), "sushi delivery pune")["keywords"]
    r = act(action="split", group_ids=[other["group_id"]], keywords=["sushi near me"],
            label="back out")
    assert _group_of(r.json(), "sushi near me")["label"] == "back out"
    assert act(action="merge", group_ids=[other["group_id"]]).status_code == 422
    assert act(action="approve", group_ids=["nope"]).status_code == 404
    assert act(action="split", group_ids=[other["group_id"]],
               keywords=_group_of(r.json(), "sushi delivery pune")["keywords"]).status_code == 422


def test_refresh_keeps_approved_groups(api):
    c, did = api
    got = c.post(f"/datasets/{did}/keyword-groups/run", json={}).json()
    g = _group_of(got, "ramen delivery pune")
    c.post(f"/datasets/{did}/keyword-groups/actions", json={"action": "approve",
                                                           "group_ids": [g["group_id"]]})
    again = c.post(f"/datasets/{did}/keyword-groups/run", json={}).json()
    kept = _group_of(again, "ramen delivery pune")
    assert kept["approved"] and kept["group_id"] == g["group_id"]
    assert sum("ramen delivery pune" in x["keywords"] for x in again["groups"]) == 1


def test_grouping_tool_runs_the_pipeline(api):
    c, did = api
    r = c.post("/tools/marketing.keyword_grouping/run", json={"dataset_id": did, "params": {}})
    assert r.status_code == 200 and "PROPOSALS until approved" in r.json()["caveats"][0]


def test_an_approval_can_be_withdrawn_and_the_engine_stops_reading_it(api):
    c, did = api
    got = c.post(f"/datasets/{did}/keyword-groups/run", json={}).json()
    g = _group_of(got, "sushi delivery pune")
    act = lambda **kw: c.post(f"/datasets/{did}/keyword-groups/actions", json=kw)  # noqa: E731
    act(action="approve", group_ids=[g["group_id"]])
    assert c.post("/tools/marketing.keyword_group_performance/run",
                  json={"dataset_id": did, "params": {}}).status_code == 200
    r = act(action="unapprove", group_ids=[g["group_id"]]).json()
    assert not _group_of(r, "sushi delivery pune")["approved"]
    r = c.post("/tools/marketing.keyword_group_performance/run",
               json={"dataset_id": did, "params": {}})
    assert r.status_code == 422 and "no keyword groups are approved" in r.text


def test_an_unknown_column_answers_422_not_500(api):
    c, did = api
    r = c.post(f"/datasets/{did}/keyword-groups/run", json={"column": "nope"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "needs_data"
