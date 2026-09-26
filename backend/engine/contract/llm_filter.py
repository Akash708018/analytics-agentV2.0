"""
A model fills the contract; this checks every choice it made against the data (25/09/2026).

Decided with the user: the model fills every field by itself, the person changes what is wrong,
and the data is the filter between the two. The model may say what a column probably MEANS; it
is never trusted on what the data can count. Each field comes out with one verdict:

- **agree**      the model's choice and the data's reading are the same (or both non-additive);
- **llm_only**   the data cannot tell (a meaning, a grain sentence, an unnamed number): kept, and
                 shown as unchecked;
- **overruled**  the data contradicts it -- a sum on a price, a ratio, a code, a coordinate, a
                 snapshot, an identifier; a unit the value is not constant within; a key that
                 repeats. The data's answer is kept, the model's is recorded beside it;
- **blocked**    impossible as stated -- a column that does not exist, arithmetic on text, a date
                 column that holds no dates. Dropped;
- **data**       filled by the data alone where the model said nothing (a unit it missed, a ratio
                 of sums, a flag's rate, the analysis window from the date column's span).

Every verdict is kept, so how the model chooses can be counted: how often it agreed, how often the
data had to overrule it, and later how often the person changed what was left.

Pure: no model call here. webapp/autofill.py asks the model and hands its JSON to `check`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.engine.clean import sql
from backend.engine.contract import suggest as sg
from backend.engine.contract.compatibility import verify_key
from backend.engine.contract.dataset_contract import AGGREGATIONS
from backend.engine.contract.evidence import (
    DatasetEvidence,
    _is_numeric,
    _is_temporal,
)

AGREE, LLM_ONLY, OVERRULED, BLOCKED, DATA = "agree", "llm_only", "overruled", "blocked", "data"
#: Below this, a choice the data cannot check is left blank for the person rather than filled.
MIN_CONFIDENCE = 0.7

_NON_ADDITIVE = {"none", "mean", "median"}
#: Rules whose reading forbids a sum whatever their strength: summing these is never a total.
_NO_SUM = {"N1", "N2", "G1", "R1", "S1", "D2", "U1", "C1"}
_NUMERIC_AGGS = {"sum", "mean", "median", "min", "max"}


@dataclass(frozen=True)
class FieldCheck:
    path: str            # "grain", "primary_key", "measures[unit_price].agg", ...
    status: str
    final: Any
    llm: Any = None
    reason: str = ""
    rule: str = ""
    confidence: float | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "final": self.final, "llm": self.llm,
                "reason": self.reason, "rule": self.rule, "confidence": self.confidence}


@dataclass
class Filled:
    """The answers to propose a contract with, and why each is what it is."""

    answers: dict
    checks: list[FieldCheck] = field(default_factory=list)
    model: str = ""

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def by_path(self) -> dict[str, FieldCheck]:
        return {c.path: c for c in self.checks}

    def summary(self) -> str:
        n = self.counts()
        parts = [f"{n[k]} {label}" for k, label in (
            (AGREE, "agree with the data"), (LLM_ONLY, "from the model only (the data cannot "
                                                       "check them)"),
            (OVERRULED, "overruled by the data"), (BLOCKED, "blocked"),
            (DATA, "filled by the data")) if n.get(k)]
        return ", ".join(parts) + "."


def _conf(v) -> float | None:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def _constant_within(con, table: str, measure: str, unit: list[str]) -> bool:
    t, m = sql.ident(table), sql.ident(measure)
    keys = ", ".join(sql.ident(u) for u in unit)
    bad = con.execute(
        f"SELECT count(*) FROM (SELECT {keys} FROM {t} GROUP BY {keys} HAVING "
        f"min({m}) IS DISTINCT FROM max({m}) OR (count({m}) > 0 AND count({m}) < count(*)))"
    ).fetchone()[0]
    return bad == 0


def check(con, table: str, ev: DatasetEvidence, llm: dict, roles: dict[str, str], *,
          model: str = "") -> Filled:
    """The model's JSON, filtered by the data. `roles` are evidence.suggest_role's."""
    cols = {c.name: c for c in ev.columns}
    checks: list[FieldCheck] = []
    entries = {}
    for e in llm.get("columns") or []:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str):
            continue
        if e["name"] not in cols:
            checks.append(FieldCheck(f"columns[{e['name']}]", BLOCKED, None, e["name"],
                                     "the table has no such column"))
            continue
        entries[e["name"]] = e

    # ---- date column: must hold dates
    date_col = llm.get("date_column") or next(
        (n for n, e in entries.items() if e.get("role") == "date"), None)
    temporal = [c.name for c in ev.columns if _is_temporal(c.dtype)]
    if date_col in temporal:
        checks.append(FieldCheck("date_column", AGREE if len(temporal) == 1 else LLM_ONLY,
                                 date_col, date_col,
                                 "the only date column" if len(temporal) == 1 else
                                 f"a date column, one of {len(temporal)}: which one the analysis "
                                 f"runs on is a choice the data cannot make"))
    else:
        fallback = temporal[0] if len(temporal) == 1 else None
        if date_col is not None:
            checks.append(FieldCheck("date_column", BLOCKED if fallback is None else OVERRULED,
                                     fallback, date_col,
                                     f"{date_col} holds no dates"
                                     + (f"; {fallback} does" if fallback else "")))
        elif fallback:
            checks.append(FieldCheck("date_column", DATA, fallback, None,
                                     "the only date column"))
        date_col = fallback

    # ---- primary key: verified, never trusted
    key = [k for k in (llm.get("primary_key") or []) if isinstance(k, str)]
    final_key: list[str] = []
    if key:
        if any(k not in cols for k in key):
            checks.append(FieldCheck("primary_key", BLOCKED, [], key, "names a column the table "
                                                                        "does not have"))
        else:
            v = verify_key(con, table, key)
            if v.holds:
                final_key = key
                checks.append(FieldCheck("primary_key", AGREE, key, key,
                                         f"{v.label} is unique in every one of {v.row_count:,} "
                                         f"rows"))
            else:
                why = (f"repeats in {v.duplicate_rows:,} row(s)" if not v.is_unique else
                       f"is blank in {', '.join(v.null_bearing)}")
                checks.append(FieldCheck("primary_key", OVERRULED, [], key,
                                         f"{v.label} {why}: not a key yet -- the Clean screen "
                                         f"shows the repeats", rule="key"))

    grain = llm.get("grain") if isinstance(llm.get("grain"), str) else None
    if grain and grain.strip():
        checks.append(FieldCheck("grain", LLM_ONLY, grain.strip(), grain,
                                 "a sentence about the business: the data can only check the key "
                                 "it names" + (" -- which it did" if final_key else "")))

    # ---- measures
    measures, dims = [], []
    aggs: dict[str, str] = {}
    per: dict[str, list[str]] = {}
    defs: dict[str, str] = {}
    wanted = [n for n, e in entries.items() if e.get("role") == "measure"]
    numeric_wanted = [n for n in wanted if _is_numeric(cols[n].dtype)
                      or cols[n].dtype.upper() == "BOOLEAN"]
    # Read over every number, not only those the model called measures: a ratio's parts may be
    # columns the model left out, and the data finds the ratio either way (live bench, 25/09/2026:
    # margin_pct's ratio was missed when the model kept line_cost off its list).
    everything = [c.name for c in ev.columns if (_is_numeric(c.dtype) or c.dtype.upper() == "BOOLEAN")
                  and roles.get(c.name) != "identifier"]
    rules = sg.suggest(con, table, ev, list(dict.fromkeys(numeric_wanted + everything)),
                       date_col) if numeric_wanted else {}
    for name in wanted:
        e, c = entries[name], cols[name]
        conf = _conf(e.get("confidence"))
        agg = e.get("agg") if e.get("agg") in AGGREGATIONS else None
        path = f"measures[{name}].agg"
        numeric = _is_numeric(c.dtype) or c.dtype.upper() == "BOOLEAN"
        if not numeric and agg in _NUMERIC_AGGS:
            checks.append(FieldCheck(path, BLOCKED, None, agg,
                                     f"{name} holds {c.dtype}: it can be counted, not added or "
                                     f"averaged; left a dimension", confidence=conf))
            dims.append(name)
            continue
        if roles.get(name) == "identifier" and agg in _NUMERIC_AGGS:
            final, status, why, rule = ("count_distinct", OVERRULED,
                                        f"{name} identifies things: only counting distinct "
                                        f"values means anything", "id")
        else:
            r = rules.get(name)
            final, status, why, rule = _agg_verdict(name, agg, r, conf)
        checks.append(FieldCheck(path, status, final, agg, why, rule, conf))
        if final is None:
            continue
        measures.append(name)
        aggs[name] = final
        # unit: the model's, if the value is constant within it; the data's, if it found one
        r = rules.get(name)
        said = [u for u in (e.get("per") or []) if isinstance(u, str) and u in cols and u != name]
        upath = f"measures[{name}].per"
        if said and _constant_within(con, table, name, said):
            per[name] = said
            checks.append(FieldCheck(upath, AGREE if r and r.per == said else LLM_ONLY, said,
                                     said, f"the same on every row of each {' + '.join(said)}",
                                     "P1", conf))
        elif said:
            fix = list(r.per) if r and r.per else []
            if fix:
                per[name] = fix
            checks.append(FieldCheck(upath, OVERRULED, fix, said,
                                     f"{name} varies within {' + '.join(said)}"
                                     + (f"; it is constant within {fix[0]}" if fix else
                                        ": it is a value per row"), "P1", conf))
        elif r and r.per:
            per[name] = list(r.per)
            checks.append(FieldCheck(upath, DATA, list(r.per), None,
                                     f"the same on every row of each {r.per[0]}: counted once "
                                     f"each, not once per row", "P1"))
        meaning = e.get("meaning")
        if isinstance(meaning, str) and meaning.strip():
            defs[name] = meaning.strip()
            checks.append(FieldCheck(f"measures[{name}].definition", LLM_ONLY, meaning.strip(),
                                     meaning, "a meaning: the model's draft; the data cannot "
                                              "check what a number includes", confidence=conf))

    for name, e in entries.items():
        if e.get("role") == "dimension" and name not in measures:
            dims.append(name)

    # ---- helpers the data derives: a ratio of sums, a flag's rate
    ratios: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    for name, r in rules.items():
        if r.ratio and name in measures and aggs.get(name) in _NON_ADDITIVE:
            rn = r.ratio["name"]
            ratios[rn] = {k: r.ratio[k] for k in ("numerator", "denominator", "scale")}
            defs[rn] = r.ratio["definition"]
            checks.append(FieldCheck(f"measures[{rn}]", DATA, ratios[rn], None,
                                     r.reason, "R1"))
    for f in sg.flag_rates(ev, measures):
        if f.column in measures or f.column in final_key:
            continue
        aliases[f.measure] = f.column
        aggs[f.measure] = "mean"
        defs[f.measure] = (f"share of rows where {f.column} is 1 (true): the mean of a 0/1 "
                           f"column")
        if f.column not in dims:
            dims.append(f.column)
        checks.append(FieldCheck(f"measures[{f.measure}]", DATA, f.column, None, f.reason, "F1"))

    # ---- window: the date column's span, which the person narrows if part is incomplete
    window = None
    if date_col:
        q = sql.ident(date_col)
        lo, hi = con.execute(f"SELECT min({q}), max({q}) FROM {sql.ident(table)}").fetchone()
        if lo is not None:
            window = (lo.date() if hasattr(lo, "date") else lo,
                      hi.date() if hasattr(hi, "date") else hi)
            checks.append(FieldCheck("analysis_window", DATA,
                                     [str(window[0]), str(window[1])], None,
                                     f"{date_col}'s whole span; narrow it if a month at either "
                                     f"end is incomplete"))

    answers = dict(
        grain=grain.strip() if grain and grain.strip() else None,
        primary_key=final_key or None, date_column=date_col,
        measures=measures + [n for n in [*ratios, *aliases] if n not in measures],
        dimensions=[d for d in dict.fromkeys(dims) if d not in final_key and d != date_col],
        aggregations={**aggs, **{n: "ratio" for n in ratios}},
        measure_definitions=defs, measure_per=per or None,
        measure_columns=aliases or None, ratios=ratios or None,
        analysis_window=window)
    return Filled(answers=answers, checks=checks, model=model)


def _agg_verdict(name: str, agg: str | None, r: sg.Suggestion | None,
                 conf: float | None) -> tuple[str | None, str, str, str]:
    """(final agg, status, reason, rule) for one measure."""
    rule = r.rule if r else ""
    if agg is None:
        if r and r.strength == sg.STRONG and r.agg:
            return r.agg, DATA, f"the model gave none; {r.reason}", rule
        return None, LLM_ONLY, "the model gave no aggregation and the data cannot settle it", rule
    if r is None or r.agg is None:
        if conf is not None and conf < MIN_CONFIDENCE:
            return (None, LLM_ONLY, f"the model was unsure ({conf:.0%}) and the data cannot "
                                    f"check it: left for you", rule)
        return agg, LLM_ONLY, "the data cannot check this one", rule
    if rule == "R1" and agg in ("mean", "median"):
        # The mean of a ratio over rows is not the group's ratio: the retail margin's mean was
        # 25.79% where the ratio of sums is 18.40% (C5). The model's mean passed as "both
        # non-additive" until the live bench caught it (25/09/2026).
        return "none", OVERRULED, f"never a mean of a ratio: {r.reason}", rule
    if agg == r.agg or (agg in _NON_ADDITIVE and r.agg in _NON_ADDITIVE):
        # Agreement with a reading of the NAME is not a check by the data: distance_km's "sum"
        # matched the engine's name rule and came out green (test, 25/09/2026).
        if r.strength == sg.STRONG:
            return agg, AGREE, r.reason, rule
        if conf is not None and conf < MIN_CONFIDENCE:
            return (None, LLM_ONLY, f"the model was unsure ({conf:.0%}) and only the name agrees: "
                                    f"left for you", rule)
        return (agg, LLM_ONLY, f"the engine reads the name the same way ({r.reason}), but the "
                               f"data itself cannot check it", rule)
    if rule == "A2" and agg in ("none", "mean", "median") and r.strength == sg.STRONG:
        return "sum", OVERRULED, r.reason, rule
    if agg == "sum" and rule in _NO_SUM:
        return r.agg, OVERRULED, f"never a sum: {r.reason}", rule
    if agg == "sum" and rule == "G2" and r.agg == "none":
        return r.agg, OVERRULED, f"never a sum: {r.reason}", rule
    if r.strength == sg.STRONG and not (rule == "F1" and agg in ("sum", "mean")):
        return r.agg, OVERRULED, r.reason, rule
    if rule == "F1":
        return agg, AGREE, r.reason, rule
    if conf is not None and conf < MIN_CONFIDENCE:
        return (None, LLM_ONLY, f"the model was unsure ({conf:.0%}); the data leans to "
                                f"{r.agg}: left for you", rule)
    return agg, LLM_ONLY, f"the data leans to {r.agg} ({r.reason}) but cannot settle it", rule


__all__ = ["AGREE", "BLOCKED", "DATA", "FieldCheck", "Filled", "LLM_ONLY", "MIN_CONFIDENCE",
           "OVERRULED", "check"]
