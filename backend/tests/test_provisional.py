"""Provisional metrics and row filters (step 3), on the user's own last-mile file.

tests/fixtures/logistics_sla.csv is the file a live SLA question was refused on (26/09/2026):
"breach = recorded delivery minutes > promised minutes" was in no contract. The assistant now
proposes it, a person approves it, and every result reading it says it is provisional.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.contract import provisional  # noqa: E402
from backend.engine.contract.dataset_contract import Measure  # noqa: E402
from backend.engine.webapp import agent  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "logistics_sla.csv"
NAME = "logistics_sla"
MEASURES = ["order_value_inr", "distance_km", "package_weight_kg", "recorded_delivery_minutes",
            "delivery_cost_inr"]
DIMS = ["hub", "zone", "rider_id", "payment_mode", "delivery_status", "attempt_count",
        "address_quality", "weather", "source_system", "promised_minutes"]
DELIVERED = "lower(trim(delivery_status)) = 'delivered'"


@pytest.fixture()
def sla():
    be = RealBackend()
    ws = be.new_workspace_id()
    path = be.save_upload(ws, FIXTURE.name, FIXTURE.read_bytes()).path
    assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
    draft = be.draft_contract(
        ws, NAME, grain="one row = one order", primary_key=[], date_column="order_date",
        measures=MEASURES, dimensions=DIMS,
        aggregations={m: "sum" if m.endswith("_inr") else "mean" for m in MEASURES},
        measure_definitions={m: m.replace("_", " ") for m in MEASURES},
        analysis_window_start="2026-08-01", analysis_window_end="2026-08-31")
    assert be.confirm_contract(ws, draft).ok
    yield be, ws
    workspace.reset(ws)
    workspace.workspace_dir(ws).rmdir()


def _propose(ws, **kw):
    args = dict(dataset_name=NAME, name="sla_breach", left="recorded_delivery_minutes", op=">",
                definition="delivered later than promised", right="promised_minutes")
    return server.propose_metric(**{**args, **kw}, workspace_id=ws)


def _approve(ws, text):
    return server.decide_metric(re.search(r"id (\w+)", text).group(1), True, workspace_id=ws)


# --- the measure ---------------------------------------------------------------------------------

def test_a_comparison_names_columns_and_an_operator_never_sql():
    m = Measure(name="late", agg="mean", compare=["a", ">", "b"])
    assert m.formula() == "a > b" and m.columns_read() == ["a", "b"] and m.is_virtual
    for bad in (dict(compare=["a", "> 0 OR 1", "b"]), dict(compare=["a; drop", ">", "b"]),
                dict(compare=["a", ">", "b"], value=3.0), dict(compare=["a", ">", ""]),
                dict(compare=["a", ">", "b"], agg="median")):
        with pytest.raises(ValueError):
            Measure(name="late", **{"agg": "mean", **bad})


# --- propose, approve, compute --------------------------------------------------------------------

def test_nothing_is_computed_with_a_metric_until_it_is_approved(sla):
    be, ws = sla
    text = _propose(ws)
    assert text.startswith("PROPOSED, waiting for the person's approval")
    # 126 rows lack delivery minutes and 4 a promise; 2 lack both: 500 - 128 = 372.
    assert "Judged on 372 of 500 row(s)" in text
    before = server.compute_analysis(NAME, "group_compare", dimension="hub",
                                     measure="sla_breach", workspace_id=ws)
    assert "sla_breach" in before and "PROVISIONAL" not in before and "NEXT STEP" in before
    [pending] = be.pending_metrics(ws)
    assert pending.formula == "recorded_delivery_minutes > promised_minutes"
    assert be.decide_metric(ws, pending.id, True).ok and be.pending_metrics(ws) == []


def test_the_approved_metric_answers_the_sla_question_and_says_it_is_provisional(sla):
    be, ws = sla
    _approve(ws, _propose(ws))
    out = server.compute_analysis(NAME, "group_compare", dimension="hub", measure="sla_breach",
                                  where=DELIVERED, workspace_id=ws)
    assert "PROVISIONAL metric, approved by the person but not in the contract: sla_breach = 1 "
    "where recorded_delivery_minutes > promised_minutes" in out
    assert "382 of 500 row(s) analysed. 118 left out by the filter" in out
    rates = {r[0].strip(): float(r[6]) for r in
             (line.split("|")[1:] for line in out.splitlines() if line.startswith("| Pune"))}
    # Before any cleaning: the user's key ranks the same (South 63.4, East 60.4, West 49.4,
    # Central 37.7 after dropping copies and folding spellings).
    assert max(rates, key=rates.get) == "Pune_South" and round(rates["Pune_South"], 4) == 0.6354
    assert sorted(rates, key=rates.get) == ["Pune_Central", "Pune_West", "Pune_East", "Pune_South"]


def test_a_rejected_or_unapproved_metric_joins_nothing(sla):
    be, ws = sla
    _propose(ws)
    [p] = be.pending_metrics(ws)
    assert be.decide_metric(ws, p.id, False).ok
    assert provisional.approved(ws, NAME) == []
    assert not be.decide_metric(ws, p.id, True).ok, "decided once"


@pytest.mark.parametrize("kw,why", [
    (dict(left="hub"), "both sides must be numbers"),
    (dict(right="no_such_column"), "not a column"),
    (dict(name="distance_km"), "already a column"),
    (dict(op="LIKE"), "op must be one of"),
    (dict(definition=" "), "definition in words"),
])
def test_a_proposal_the_engine_cannot_use_is_refused_with_the_reason(sla, kw, why):
    be, ws = sla
    out = _propose(ws, **kw)
    assert out.startswith("BLOCKED") and why in out and be.pending_metrics(ws) == []


def test_a_metric_against_a_number(sla):
    be, ws = sla
    _approve(ws, _propose(ws, name="far", left="distance_km", right=None, value=10.0,
                          definition="further than 10 km"))
    out = server.compute_analysis(NAME, "top_n", dimension="hub", measure="far", n=5,
                                  workspace_id=ws)
    assert "far = 1 where distance_km > 10, else 0" in out


def test_a_reset_takes_the_proposals_with_it(sla):
    be, ws = sla
    _propose(ws)
    be.reset_workspace(ws)
    assert be.pending_metrics(ws) == []


# --- where= ----------------------------------------------------------------------------------------

def test_a_filter_is_counted_and_a_filter_that_keeps_nothing_is_refused(sla):
    be, ws = sla
    out = server.compute_analysis(NAME, "frequency", column="hub", where=DELIVERED,
                                  workspace_id=ws)
    assert "382 of 500 row(s) analysed. 118 left out by the filter" in out
    none = server.compute_analysis(NAME, "frequency", column="hub",
                                   where="hub = 'Mumbai'", workspace_id=ws)
    assert "keeps none of the 500 row(s)" in none


@pytest.mark.parametrize("rule", [
    "1=1) OR (SELECT count(*) FROM read_csv('/etc/passwd')) > 0 AND (1=1",
    "hub = 'x'; DROP TABLE logistics_sla",
    "distance_km",
])
def test_a_filter_passes_the_same_guard_as_an_exclusion(sla, rule):
    be, ws = sla
    out = server.compute_analysis(NAME, "frequency", column="hub", where=rule, workspace_id=ws)
    assert "NEXT STEP" in out and "row(s) analysed" not in out


# --- the assistant -----------------------------------------------------------------------------------

def test_the_assistant_proposes_and_never_approves():
    assert "propose_metric" in agent.ALLOWED and "decide_metric" not in agent.ALLOWED
    assert "propose_metric" in agent.SYSTEM and 'never "causes"' in agent.SYSTEM
