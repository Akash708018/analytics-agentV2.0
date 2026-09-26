"""webapp/autofill.py through the real backend, with a scripted model (25/09/2026).

    uv run pytest tests/test_autofill.py -q
"""

from __future__ import annotations

import json

import pytest

from backend.engine import workspace
from backend.engine.contract import store
from backend.engine.util import db
from backend.engine.webapp import autofill, llm
from backend.engine.webapp.real_backend import RealBackend

CSV = ("order_id,line_no,order_date,unit_price,qty,amount,delivery_fee,drop_lat,drop_lng,"
       "is_late\n") + "\n".join(
    f"O{i // 2},{i % 2 + 1},2024-0{1 + i % 3}-1{i % 9},{50 + i % 7 * 10}.0,{1 + i % 4},"
    f"{(50 + i % 7 * 10) * (1 + i % 4)}.0,{(i // 2) % 3 * 20}.0,{18.5 + (i % 9) / 100:.2f},"
    f"{73.8 + (i % 9) / 100:.2f},{1 if i % 5 == 0 else 0}"
    for i in range(120))

ANSWER = {
    "grain": "one row = one line of a delivery order",
    "primary_key": ["order_id", "line_no"], "date_column": "order_date",
    "columns": [
        {"name": "unit_price", "role": "measure", "agg": "sum", "confidence": 0.8,
         "meaning": "price of one item"},
        {"name": "qty", "role": "measure", "agg": "sum", "confidence": 0.9,
         "meaning": "items on the line"},
        {"name": "amount", "role": "measure", "agg": "sum", "confidence": 0.95,
         "meaning": "line value in INR"},
        {"name": "delivery_fee", "role": "measure", "agg": "sum", "confidence": 0.9,
         "meaning": "fee charged per order"},
        {"name": "drop_lat", "role": "measure", "agg": "sum", "confidence": 0.5},
        {"name": "drop_lng", "role": "dimension"},
        {"name": "is_late", "role": "dimension"},
    ]}


class Model:
    name = "scripted"

    def __init__(self, answer=ANSWER, fail=False):
        self.answer, self.fail, self.calls = answer, fail, 0

    def available(self):
        return True

    def complete(self, system, prompt, *, max_wait):
        self.calls += 1
        if self.fail:
            raise llm.ProviderError("scripted", "HTTP 429", True, summary="rate limited")
        assert "unit_price" in prompt and "Measured repetition" in prompt
        assert "strong" not in prompt, "the engine's readings are not shown to the model"
        return json.dumps(self.answer), "m-1"


@pytest.fixture()
def space(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYTICS_AUTOFILL", "on")
    model = Model()
    monkeypatch.setattr(llm, "configured", lambda: [model])
    be = RealBackend()
    ws = be.new_workspace_id()
    path = be.save_upload(ws, "deliveries.csv", CSV.encode()).path
    assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
    yield be, ws, model
    workspace.reset(ws)
    workspace.workspace_dir(ws).rmdir()


def test_a_first_draft_is_filled_and_filtered(space):
    be, ws, model = space
    d = be.draft_contract(ws, "deliveries")
    assert model.calls == 1 and d.filled_by == "scripted/m-1"
    assert d.aggregations["unit_price"] == "none"          # the model's sum, overruled
    assert d.sources["measures[unit_price].agg"].status == "overruled"
    assert d.sources["measures[unit_price].agg"].llm == "sum"
    assert d.aggregations["amount"] == "sum"
    assert d.measure_per["delivery_fee"] == ["order_id"]    # the unit the model missed
    assert d.sources["measures[delivery_fee].per"].status == "data"
    assert d.aggregations["drop_lat"] == "none"
    assert d.measure_columns == {"is_late_rate": "is_late"}
    assert d.sources["primary_key"].status == "agree"
    assert d.grain == "one row = one line of a delivery order"
    # The one blank is a meaning the model did not give: the data cannot write one.
    assert d.provisional == ["measures[drop_lat].definition"], d.provisional
    assert "overruled by the data" in d.fill_note


def test_the_fill_is_kept_and_an_edit_is_the_persons(space):
    be, ws, model = space
    d = be.draft_contract(ws, "deliveries")
    again = be.draft_contract(ws, "deliveries")
    assert model.calls == 1 and again.sources == d.sources
    edited = be.draft_contract(
        ws, "deliveries", grain=d.grain, primary_key=d.primary_key, date_column=d.date_column,
        measures=d.measures, dimensions=d.dimensions,
        aggregations={**d.aggregations, "unit_price": "mean"},
        measure_definitions={**d.measure_definitions, "drop_lat": "drop point latitude"},
        analysis_window_start=d.analysis_window_start, analysis_window_end=d.analysis_window_end,
        measure_per=d.measure_per, ratios=d.ratios, measure_columns=d.measure_columns)
    assert model.calls == 1 and not edited.provisional, edited.provisional
    src = edited.sources["measures[unit_price].agg"]
    assert src.status == "user" and "you changed it from 'none'" in src.reason
    assert edited.sources["measures[amount].agg"].status == "agree"
    result = be.confirm_contract(ws, edited)
    assert result.ok, result.message
    con = db.connect(ws)
    try:
        kept = store.current(con, "deliveries").contract
    finally:
        con.close()
    assert kept.provenance["measures[unit_price].agg"]["status"] == "user"
    assert kept.provenance["measures[amount].agg"]["model"] == "scripted/m-1"
    assert {m.name: m.agg for m in kept.measures}["unit_price"] == "mean"


def test_fill_again_asks_the_model_once_more(space):
    be, ws, model = space
    be.draft_contract(ws, "deliveries")
    be.refill_contract(ws, "deliveries")
    be.draft_contract(ws, "deliveries")
    assert model.calls == 2


def test_a_failing_model_leaves_the_datas_own_suggestions(space, monkeypatch):
    be, ws, _ = space
    broken = Model(fail=True)
    monkeypatch.setattr(llm, "configured", lambda: [broken])
    d = be.draft_contract(ws, "deliveries")
    assert d.filled_by == "" and "could not fill this" in d.fill_note
    assert "rate limited" in d.fill_note
    assert d.suggestions["unit_price"].agg == "none" and "measures[unit_price].agg" in d.provisional
    assert not autofill.kept(ws, "deliveries"), "a failure is not kept"


def test_off_means_no_call(space, monkeypatch):
    be, ws, model = space
    monkeypatch.setenv("ANALYTICS_AUTOFILL", "off")
    d = be.draft_contract(ws, "deliveries")
    assert model.calls == 0 and d.filled_by == "" and d.fill_note == ""


def test_ready_says_how_many_fields_are_the_models_alone(space):
    be, ws, model = space
    cols = [dict(e, meaning=e.get("meaning") or f"{e['name']} as recorded")
            for e in ANSWER["columns"]]
    model.answer = {**ANSWER, "columns": cols}
    d = be.draft_contract(ws, "deliveries")
    assert not d.provisional, d.provisional
    alone = sum(1 for src in d.sources.values() if src.status == "llm_only")
    assert alone and d.message == (f"Ready to confirm. {alone} field(s) are the model's alone -- "
                                   f"marked 'from the model' above: read those before you "
                                   f"confirm.")
