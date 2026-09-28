"""B5: every interpretation rule, with a violating and a passing answer."""
from __future__ import annotations

import pytest

from backend.rules import interpret

MEAS = [{"tool_id": "marketing.channel_efficiency", "caveats": [
    "measurement_change: Meta removed 7-day and 28-day view attribution windows on 2026-01-12 "
    "-- ..."], "pack_rules_applied": []}]
FEST = [{"tool_id": "marketing.festive_compare", "caveats": [
    "festival_confound: Diwali (Lakshmi Puja) (2025-10-20 to 2025-10-21) falls in the period; "
    "..."], "pack_rules_applied": []}]
SMALL = [{"tool_id": "marketing.device_geo_split", "caveats": [
    "Clicks by group: 'Nashik' has 2 row(s), under the minimum group size 5; its figure is "
    "suppressed."], "pack_rules_applied": []}]
EFF = [{"tool_id": "marketing.channel_efficiency", "caveats": [], "pack_rules_applied": []}]
MIX = EFF + [{"tool_id": "marketing.channel_mix_shift", "caveats": [], "pack_rules_applied": []}]
HOLD = [{"tool_id": "marketing.campaign_impact", "caveats": [
    "Any difference here is caused (a holdout was marked)."], "pack_rules_applied": []}]

CASES = [
    ("no_sum_of_rate", "The total CTR across campaigns was 14.2%.", [],
     "CTR was 4.9% across campaigns, clicks over impressions.", []),
    ("no_cross_source_conversion_sum",
     "Google reported 120 and Meta 95 conversions, 215 conversions in total.", [],
     "Google reported 120 conversions and Meta 95; they overlap, so they are not added.", []),
    ("bounds", "CTR rose to 140% in January.", [], "CTR rose to 5.1% in January.", []),
    ("consequence_not_cause", "ROAS fell because of rising frequency.", [],
     "ROAS fell while frequency rose; the data does not show the cause.", []),
    ("correlational_only", "The Diwali campaign drove a 30% lift in orders.", [],
     "Orders were 30% higher, associated with the campaign period.", []),
    ("required_stratifier", "Google is the best performing channel.", EFF,
     "Google is the best performing channel once the mix is checked.", MIX),
    ("measurement_change", "Conversions fell 20% in January.", MEAS,
     "Conversions fell 20% in January; Meta changed its attribution windows on 2026-01-12.",
     MEAS),
    ("small_sample", "Nashik converts worst.", SMALL,
     "Nashik had too few rows to judge; its figures are suppressed.", SMALL),
    ("festival_confound", "October revenue rose 18%.", FEST,
     "October revenue rose 18%, a period that includes Diwali.", FEST),
]


@pytest.mark.parametrize("rule,bad,bad_trace,good,good_trace", CASES,
                         ids=[c[0] for c in CASES])
def test_rule_fires_on_the_bad_answer_and_not_the_good(rule, bad, bad_trace, good, good_trace):
    fn = interpret.RULES[rule]
    v = fn(bad, bad_trace)
    assert v is not None and v.rule == rule
    assert fn(good, good_trace) is None


def test_holdout_makes_causal_wording_allowed():
    assert interpret.correlational_only("The campaign drove a 30% lift.", HOLD) is None


def test_no_holdout_line_does_not_allow_causal_wording():
    trace = [{"tool_id": "marketing.campaign_impact", "caveats": [
        "Any difference here is ASSOCIATED WITH the change -- correlational: no holdout was "
        "marked, so ..."], "pack_rules_applied": []}]
    assert interpret.correlational_only("The campaign drove a 30% lift.", trace) is not None


def test_check_applies_always_rules_and_pack_rules():
    trace = [{"tool_id": "t", "caveats": [], "pack_rules_applied": ["correlational_only"]}]
    got = {v.rule for v in interpret.check("The total CTR was 9%; the campaign drove sales.",
                                           trace)}
    assert got == {"no_sum_of_rate", "correlational_only"}
    assert interpret.check("CTR was 4.9%.", trace) == []
