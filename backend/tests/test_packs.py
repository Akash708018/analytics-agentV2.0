"""B2: pack schema, loader, detector, registry gating, and the suggest.py vocabulary move."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

from backend.packs import loader
from backend.packs.detector import detect, hint_matches, normalise
from backend.packs.loader import PackError, load_all, merge
from backend.packs.models import Pack
from backend.tools import registry

FIX = Path(__file__).resolve().parent / "fixtures"

# v1's sets at 0ba324b (contract/suggest.py), pinned here so the pack cannot drift silently.
V1_WORDS = {
    "lat": {"lat", "latitude"}, "lng": {"lng", "lon", "long", "longitude"},
    "credit": {"credit", "attribution", "attributed", "allocation", "allocated"},
    "distinct_people": {"reach", "unique", "uniques", "distinct", "dau", "wau", "mau",
                        "audience"},
    "code": {"zip", "zipcode", "postcode", "postal", "pincode", "pin", "year", "yr", "month",
             "quarter", "week", "weekday", "dow", "hour", "code", "phone", "fips", "isbn", "ean",
             "upc"},
}


def test_every_pack_validates_and_resolves():
    packs = load_all()
    assert {"core", "marketing"} <= set(packs)
    for pid in packs:
        m = merge(packs, [pid])
        assert m.pack_ids[0] == "core"


def test_core_word_classes_equal_v1():
    w = loader.core_word_classes()
    assert len(w) == 16
    for k, v in V1_WORDS.items():
        assert w[k] == v, k


def test_suggest_reads_the_pack():
    from backend.engine.contract import suggest
    assert suggest._ADDITIVE == frozenset(loader.core_word_classes()["additive"])
    assert "roas" in suggest._RATIO_WORDS and "followers" in suggest._SNAPSHOT


def _pack(**over) -> dict:
    d = {"pack": {"id": "demo", "version": "0.0.1", "extends": ["core"]},
         "vocabulary": [{"id": "spend2", "hints": ["spend"]}]}
    d.update(over)
    return d


def test_pack_rejects_sql():
    with pytest.raises(Exception, match="never contain SQL"):
        Pack(**_pack(tools=[{"id": "demo.t", "description": "x", "ui_label": "x",
                             "base_analysis": "pareto",
                             "preset": {"filter": "SELECT * FROM t WHERE 1=1"}}]))


def test_pack_rejects_long_description_and_unnamespaced_tool():
    with pytest.raises(Exception):
        Pack(**_pack(tools=[{"id": "demo.t", "description": "x" * 201, "ui_label": "x",
                             "base_analysis": "pareto"}]))
    with pytest.raises(Exception, match="namespaced"):
        Pack(**_pack(tools=[{"id": "other.t", "description": "x", "ui_label": "x",
                             "base_analysis": "pareto"}]))


def test_fork_suggestion_needs_reason_and_valid_option():
    base = {"id": "f", "question": "q?", "options": [{"id": "a", "label": "A"}]}
    with pytest.raises(Exception, match="reason"):
        Pack(**_pack(forks=[{**base, "suggested": "a"}]))
    with pytest.raises(Exception, match="not an option"):
        Pack(**_pack(forks=[{**base, "suggested": "z", "suggested_reason": "r"}]))


def test_template_shape_needs_its_fields():
    with pytest.raises(Exception, match="needs"):
        Pack(**_pack(metric_templates=[{"id": "roas", "label": "ROAS", "shape": "ratio_of_sums",
                                        "numerator": "spend2", "required_concepts": []}]))


def _write(tmp: Path, pid: str, d: dict) -> None:
    (tmp / pid).mkdir(parents=True, exist_ok=True)
    (tmp / pid / "pack.yaml").write_text(yaml.safe_dump(d))


def _tree(tmp: Path, demo: dict) -> dict:
    core = yaml.safe_load((loader.PACKS_DIR / "core" / "pack.yaml").read_text())
    _write(tmp, "core", core)
    _write(tmp, "demo", demo)
    loader.load_all.cache_clear()
    try:
        return load_all(tmp)
    finally:
        loader.load_all.cache_clear()


def test_unknown_reference_and_cycle_are_refused(tmp_path):
    with pytest.raises(PackError, match="unknown concept"):
        _tree(tmp_path, _pack(tools=[{"id": "demo.t", "description": "x", "ui_label": "x",
                                      "base_analysis": "pareto",
                                      "required_concepts": ["nope"]}]))
    with pytest.raises(PackError, match="extends unknown"):
        _tree(tmp_path, {**_pack(), "pack": {"id": "demo", "version": "1", "extends": ["x"]}})


GATED = _pack(
    vocabulary=[{"id": "spend2", "hints": ["spend"]}, {"id": "clicks2", "hints": ["clicks"]},
                {"id": "position2", "hints": ["position"]}],
    tools=[{"id": "demo.eff", "description": "Spend efficiency", "ui_label": "Efficiency",
            "base_analysis": "group_compare + ranking_shift",
            "required_concepts": ["spend2", "clicks2"]},
           {"id": "demo.seo", "description": "Positions", "ui_label": "SEO",
            "base_analysis": "trend", "required_concepts": ["position2"]}])


def test_gating_hidden_before_confirm_visible_after_hidden_without_data(tmp_path):
    packs = _tree(tmp_path, GATED)
    m = merge(packs, ["demo"])
    got = {s.tool_id: s for s in registry.statuses([], {"spend2", "clicks2"}, set(), m)}
    assert got["demo.eff"].status == "needs_domain" and got["demo.eff"].needs_domain == "demo"
    got = {s.tool_id: s for s in registry.statuses(["demo"], {"spend2", "clicks2"}, set(), m)}
    assert got["demo.eff"].status == "active"
    assert got["demo.seo"].status == "needs_data"
    assert got["demo.seo"].missing_concepts == ["position2"]
    names = [s["name"] for s in registry.llm_schemas(list(got.values()))]
    assert "demo_eff" in names and "demo_seo" not in names


def test_every_tool_compiles_to_engine_analyses(tmp_path):
    packs = _tree(tmp_path, GATED)
    assert registry.compile_errors(merge(packs, ["demo"])) == []
    bad = _tree(tmp_path, _pack(tools=[{"id": "demo.x", "description": "x", "ui_label": "x",
                                        "base_analysis": "no_such_analysis"}]))
    assert registry.compile_errors(merge(bad, ["demo"])) == [
        "demo.x: 'no_such_analysis' is not an engine analysis"]
    real = load_all()
    assert registry.compile_errors(merge(real, list(real))) == []


def test_every_description_fits_200_chars():
    for s in registry.core_tools():
        assert len(s.description) <= 200, s.tool_id
    for s in registry.llm_schemas(registry.core_tools()):
        assert len(s["description"]) <= 200


def test_schema_tokens_core_only_measured():
    n = registry.schema_tokens(registry.llm_schemas(registry.core_tools()))
    print(f"\ncore-only LLM tool schemas: ~{n} tokens (estimate)")
    assert 50 < n < 600


@pytest.mark.parametrize("hint,name,score", [("cost", "cost", 3), ("ad_group", "ad_group_name", 2),
                                             ("clicks", "link_clicks", 1), ("cost", "costume", 0)])
def test_hint_matching(hint, name, score):
    assert hint_matches(hint, normalise(name)) == score


def test_normalise_handles_camel_and_punctuation():
    assert normalise("Conv. value") == "conv_value" and normalise("totalUsers") == "total_users"


def test_detector_finds_marketing_sources_with_evidence():
    ads = detect(["Date", "Campaign", "Ad group", "Impressions", "Clicks", "Cost", "Conversions"])
    assert ads.sources[0]["source"] == "paid_ads" and ads.domains[0]["domain"] == "marketing"
    assert "Cost" in ads.sources[0]["evidence"]["matched_columns"]
    gsc = detect(["query", "page", "clicks", "impressions", "ctr", "position"])
    assert gsc.sources[0]["source"] == "search_console"


def test_logistics_file_is_not_called_marketing():
    cols = next(csv.reader(open(FIX / "logistics_sla.csv")))
    d = detect(cols)
    assert d.domains == []        # orders matched, but orders alone do not signal marketing
