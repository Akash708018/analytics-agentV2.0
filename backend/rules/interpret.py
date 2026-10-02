"""Interpretation filter: deterministic rules over an answer's text and the tool trace.

A rule either names a violation (with what to change) or passes. No model is asked. Rules come
from the packs (their ids); the check for each id lives here. A violation gets ONE correction
round; anything unresolved is shown to the person as a flag.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RATE = r"(ctr|cvr|roas|cpa|cpc|cpm|click[- ]through rate|conversion rate|engagement rate|" \
       r"open rate|bounce rate|unsubscribe rate|rate)"
SOURCES = r"(google|meta|facebook|instagram|ga4|analytics|platform|backend|orders?)"
CAUSAL = r"(caused|causes|drove|drives|driven|led to|leads to|resulted in|results in|" \
         r"boosted|lifted|increased|produced|generated)"
MARKETING = r"(campaign|spend|ads?|budget|promotion|launch|creative)"


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str          # what is wrong
    fix: str             # what the rewrite must do

    def text(self) -> str:
        return f"[{self.rule}] {self.detail} -> {self.fix}"


def _caveats(trace: list[dict]) -> list[str]:
    return [c for r in trace for c in r.get("caveats", [])]


def _tools(trace: list[dict]) -> set[str]:
    return {r.get("tool_id", "") for r in trace}


def no_sum_of_rate(text: str, trace: list[dict]) -> Violation | None:
    m = re.search(rf"\b(total|sum(med)? of|summ(ed|ing)|add(ed|ing)? up|combined)\s+"
                  rf"(the\s+)?{RATE}\b", text, re.I) or \
        re.search(rf"\b{RATE}s?\s+(add(ed)? up to|sum(s|med)? to|totals?)\b", text, re.I)
    if m:
        return Violation("no_sum_of_rate", f"'{m.group(0)}' adds or totals a rate",
                         "state the rate as the ratio of sums the tool reported; never total it")
    return None


def no_cross_source_conversion_sum(text: str, trace: list[dict]) -> Violation | None:
    m = re.search(rf"\b{SOURCES}\b[^.]{{0,60}}\b(plus|\+|and)\b[^.]{{0,40}}\b{SOURCES}\b[^.]{{0,60}}"
                  rf"\b(total|combined|together|in all|altogether|sum)\b", text, re.I)
    if m and re.search(r"conversion|sale|purchase|order|key event", m.group(0), re.I):
        return Violation("no_cross_source_conversion_sum",
                         f"'{m.group(0)[:80]}' adds conversions from different sources",
                         "report each source separately; they count the same sales")
    return None


def bounds(text: str, trace: list[dict]) -> Violation | None:
    for m in re.finditer(rf"\b{RATE}\b[^.%\d-]{{0,30}}(-?\d[\d,]*\.?\d*)\s*%", text, re.I):
        v = float(m.group(2).replace(",", ""))
        if not 0 <= v <= 100:
            return Violation("bounds", f"a rate given as {v}%", "a rate lies between 0 and 100%")
    for m in re.finditer(r"\b(spend|cost)\b[^.\d-]{0,20}(-\s?[₹$]?\s?\d[\d,]*\.?\d*)", text, re.I):
        return Violation("bounds", f"negative spend '{m.group(0)}'", "spend is never below 0")
    for m in re.finditer(r"\bposition\b[^.\d]{0,15}(\d+\.?\d*)", text, re.I):
        if float(m.group(1)) < 1:
            return Violation("bounds", f"position {m.group(1)}", "search position is 1 or more")
    return None


def consequence_not_cause(text: str, trace: list[dict], symptoms=("frequency", "fatigue",
                                                                   "cpm")) -> Violation | None:
    sym = "|".join(symptoms)
    m = re.search(rf"\b(because of|due to|caused by|driven by|the cause (is|was))\s+"
                  rf"(the\s+)?(high|higher|rising|increased|increasing|ad\s+)?\s*({sym})\b",
                  text, re.I)
    if m:
        return Violation("consequence_not_cause", f"'{m.group(0)}' names a symptom as the cause",
                         "say frequency/fatigue/CPM moved WITH the result; name what the data "
                         "shows changed first, or say the cause is not in the data")
    return None


def correlational_only(text: str, trace: list[dict]) -> Violation | None:
    # the engine says "caused (a holdout was marked)" only when the person marked one; its
    # usual line "no holdout was marked" must not count (C5)
    holdout = any("(a holdout was marked)" in c for c in _caveats(trace))
    if holdout:
        return None
    m = re.search(rf"\b{MARKETING}\b[^.]{{0,50}}\b{CAUSAL}\b", text, re.I) or \
        re.search(rf"\b{CAUSAL}\b[^.]{{0,20}}\bby\s+(the\s+)?{MARKETING}\b", text, re.I)
    if m and not re.search(r"associated with|correlat|coincided|alongside", text, re.I):
        return Violation("correlational_only", f"'{m.group(0)[:80]}' claims a cause",
                         "without a holdout say the change is 'associated with' the campaign")
    return None


def required_stratifier(text: str, trace: list[dict]) -> Violation | None:
    m = re.search(r"\b(best|worst|top)[- ]perform\w*\s+(channel|campaign)|\b(channel|campaign)"
                  r"\s+\w*\s*(outperform\w*|wins?|beats?)", text, re.I)
    if m and "marketing.channel_mix_shift" not in _tools(trace):
        return Violation("required_stratifier", f"'{m.group(0)}' ranks channels",
                         "check the mix (channel_mix_shift) first, or say the ranking has not "
                         "been checked for a change in mix")
    return None


def measurement_change(text: str, trace: list[dict]) -> Violation | None:
    for c in _caveats(trace):
        if c.startswith("measurement_change:"):
            date = re.search(r"\d{4}-\d{2}-\d{2}", c)
            said = (date and date.group(0) in text) or re.search(
                r"measurement|attribution (window|change)|reporting change|tracking change",
                text, re.I)
            if not said:
                return Violation("measurement_change", f"the period spans a measurement change "
                                 f"({date.group(0) if date else 'see caveats'})",
                                 "name the change and its date; part of the move may be "
                                 "measurement, not performance")
    return None


def small_sample(text: str, trace: list[dict]) -> Violation | None:
    small = [c for c in _caveats(trace) if "minimum group size" in c or "NOT ENOUGH DATA" in c]
    if small and not re.search(r"small|too few|not enough|few rows|suppressed|sample",
                               text, re.I):
        return Violation("small_sample", "some groups were too small to report",
                         "say which groups were suppressed or too small to judge")
    return None


def festival_confound(text: str, trace: list[dict]) -> Violation | None:
    for c in _caveats(trace):
        if c.startswith("festival_confound:"):
            name = c.split(":", 1)[1].strip().split(" (")[0]
            first = re.split(r"[\s(]", name)[0]
            if not re.search(re.escape(first), text, re.I) and not re.search(r"festiv",
                                                                             text, re.I):
                return Violation("festival_confound", f"{name} falls in the period",
                                 f"mention {name}: the change may be the festival")
    return None


DRIVERS = r"(weather|rain\w*|address\w*|attempts?|payment|cod|prepaid|distance|traffic)"
ASSOC = r"associat|correlat|went with|goes with|go with|alongside|coincid|not (proven|a) caus"


def drivers_are_associations(text: str, trace: list[dict]) -> Violation | None:
    m = re.search(rf"\b{DRIVERS}\b[^.]{{0,40}}\b({CAUSAL}|causing)\b", text, re.I) or \
        re.search(rf"\b(because of|due to|caused by|driven by)\s+(the\s+)?(heavy\s+|poor\s+|"
                  rf"low\s+|bad\s+)?{DRIVERS}\b", text, re.I)
    if m and not re.search(ASSOC, text, re.I):
        return Violation("drivers_are_associations", f"'{m.group(0)[:80]}' names a cause",
                         "say breaches were more common WITH that condition in this data "
                         "(an association), not that it caused them")
    return None


def sla_on_delivered_only(text: str, trace: list[dict]) -> Violation | None:
    for sent in re.split(r"(?<=[.!?])\s+", text):
        m = re.search(r"\b(rto|cancel\w*|pending|returned|undelivered)\b[^.]{0,50}\b(late|"
                      r"breach\w*|on[- ]time|sla)\b", sent, re.I)
        if m and not re.search(r"exclud|separate|not counted|not part|only delivered|out of "
                               r"delivered|delivered orders only", sent, re.I):
            return Violation("sla_on_delivered_only", f"'{m.group(0)[:80]}' mixes undelivered "
                             "orders into SLA", "SLA rates are out of delivered orders; report "
                             "RTO, cancelled and pending separately")
    return None


def compare_within_zone(text: str, trace: list[dict]) -> Violation | None:
    m = re.search(r"\b(best|worst|fastest|slowest|better|worse|most reliable)\b[^.]{0,25}\b"
                  r"(courier|carrier|partner|3pl)s?\b|\b(courier|carrier|partner)\s+\w+\s+"
                  r"(is|was)\s+(the\s+)?(best|worst|better|worse|faster|slower)\b", text, re.I)
    if m and not re.search(r"within (each |every |the same )?zone|zone by zone|in each zone|"
                           r"by zone|same zones?|stratif", text, re.I):
        return Violation("compare_within_zone", f"'{m.group(0)[:80]}' ranks couriers",
                         "compare couriers within zone (courier by zone) before naming a "
                         "winner, or say the ranking ignores zone mix")
    return None


# --- claim validation (B9 concept 6): a number can match a figure and still be claimed wrongly

UP = r"(rose|risen|increased|grew|grown|gained|climbed|jumped|went up|up by|higher by)"
DOWN = r"(fell|fallen|dropped|decreased|declined|shrank|went down|down by|lower by)"
SHARE_UNITS = {"share", "percent", "pct", "%"}


def _figures(trace: list[dict]) -> list[dict]:
    return [f for r in trace for f in r.get("figures", [])
            if isinstance(f.get("value"), (int, float))]


def claim_unit(text: str, trace: list[dict]) -> Violation | None:
    """A fraction written as a percent: the engine's rate 0.638 claimed as '0.638%'."""
    figs = _figures(trace)
    for m in re.finditer(r"(?<![\d.])(0?\.\d+)\s*%", text):
        v = float(m.group(1))
        hit = [f for f in figs if 0 < abs(f["value"]) < 1 and abs(f["value"] - v) < 0.0051
               and str(f.get("unit") or "").lower() not in SHARE_UNITS]
        if hit and not any(abs(f["value"] - v) < 1e-9 and str(f.get("unit") or "").lower()
                           in SHARE_UNITS for f in figs):
            return Violation("claim_unit", f"'{m.group(0)}' writes the fraction {hit[0]['value']}"
                             f" ({hit[0]['name']}) as a percent",
                             f"a rate of {hit[0]['value']} is {hit[0]['value'] * 100:.1f}%")
    return None


def claim_direction(text: str, trace: list[dict]) -> Violation | None:
    """A change claimed in the wrong direction: 'rose 12%' on a figure of -12."""
    changes = [f for f in _figures(trace)
               if re.search(r"change|delta|diff|growth|vs|lift", f["name"] + " " +
                            str(f.get("unit") or ""), re.I) and f["value"] != 0]
    for sent in re.split(r"(?<=[.!?])\s+", text):
        for words, sign in ((UP, -1), (DOWN, 1)):
            if not re.search(rf"\b{words}\b", sent, re.I):
                continue
            for m in re.finditer(r"(?<![\d.])(\d[\d,]*\.?\d*)", sent):
                v = float(m.group(1).replace(",", ""))
                for f in changes:
                    if abs(abs(f["value"]) - v) < 0.0051 * max(1, v) and \
                            (f["value"] < 0 if sign == -1 else f["value"] > 0):
                        return Violation("claim_direction", f"'{sent.strip()[:80]}' says "
                                         f"{'up' if sign == -1 else 'down'} but {f['name']} "
                                         f"is {f['value']}", "state the direction the figure "
                                         "shows (its sign)")
    return None


RULES = {
    "no_sum_of_rate": no_sum_of_rate,
    "no_cross_source_conversion_sum": no_cross_source_conversion_sum,
    "bounds": bounds,
    "consequence_not_cause": consequence_not_cause,
    "correlational_only": correlational_only,
    "required_stratifier": required_stratifier,
    "measurement_change": measurement_change,
    "small_sample": small_sample,
    "festival_confound": festival_confound,
    "drivers_are_associations": drivers_are_associations,
    "sla_on_delivered_only": sla_on_delivered_only,
    "compare_within_zone": compare_within_zone,
    "claim_unit": claim_unit,
    "claim_direction": claim_direction,
}
ALWAYS = ("no_sum_of_rate", "bounds", "measurement_change", "small_sample", "festival_confound",
          "claim_unit", "claim_direction")


def check(text: str, trace: list[dict], rule_ids: set[str] | None = None) -> list[Violation]:
    """Every violation of the rules in force: ALWAYS + the pack rules the trace's tools name."""
    ids = set(ALWAYS) | set(rule_ids or ())
    for r in trace:
        ids |= set(r.get("pack_rules_applied", []))
    out = []
    for rid in sorted(ids):
        fn = RULES.get(rid)
        if fn and (v := fn(text, trace)):
            out.append(v)
    return out
