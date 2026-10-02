"""Preparation helpers: pure mappings between what the API returns, what the person
chose, and what the API takes. No Streamlit and no network here; nothing is computed
about the data. See docs/steps/F3.md.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterable, Mapping
from typing import Any

# The aggregations the contract question lists, minus "ratio" (a ratio needs a numerator
# and denominator, which the proposal does not offer yet).
AGGREGATIONS = ("sum", "mean", "median", "min", "max", "count", "count_distinct", "none")


# --- cleaning ------------------------------------------------------------------------------

def fingerprint(proposal: Mapping[str, Any]) -> str:
    """Action ids are renumbered when the plan changes (measured in F3: after applying
    C001+C002, the old C003 came back as C001). A tick is kept against id, kind AND
    column, so it can never move to a different action."""
    return f"{proposal['action_id']}|{proposal['kind']}|{proposal.get('column') or ''}"


def to_apply(proposals: Iterable[Mapping[str, Any]], ticked: Iterable[str]) -> tuple[list[str], list[str]]:
    """(action ids to send, in the plan's order; ticks that match no current action)."""
    ticked = set(ticked)
    current = {fingerprint(p): p["action_id"] for p in proposals}
    return [aid for fp, aid in current.items() if fp in ticked], sorted(ticked - set(current))


# --- contract ------------------------------------------------------------------------------

def default_roles(proposal: Mapping[str, Any], columns: Iterable[str]) -> dict[str, str]:
    """Each column's role as the proposal states it; columns it does not name: ignore.
    The key is not a role: it is its own list (`proposal["key"]`), and may include the
    date and dimension columns."""
    roles = {c: "ignore" for c in columns}
    roles.update({c: "dimension" for c in proposal.get("dimensions", [])})
    roles.update({m["column"]: "measure" for m in proposal.get("measures", [])})
    if proposal.get("date"):
        roles[proposal["date"]] = "date"
    return roles


def proposal_aggs(proposal: Mapping[str, Any]) -> dict[str, str]:
    """Aggregations already agreed (in force), never the suggestions."""
    return {m["column"]: m["agg"] for m in proposal.get("measures", []) if m.get("agg")}


def suggestions(proposal: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {m["column"]: m for m in proposal.get("measures", []) if m.get("suggested_agg")}


def date_text(value: dt.date | None) -> str | None:
    return value.isoformat() if isinstance(value, dt.date) else None


def text_date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def date_columns(roles: Mapping[str, str]) -> list[str]:
    return [c for c, r in roles.items() if r == "date"]


def contract_body(form: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The person's form → (ContractConfirm.contract, fork_choices), in the API's names.
    Blank answers are left out, so the engine asks for them instead of receiving ''."""
    roles: Mapping[str, str] = form.get("roles", {})
    measures = [c for c, r in roles.items() if r == "measure"]
    contract: dict[str, Any] = {
        "primary_key": list(form.get("key") or []),
        "measures": measures,
        "dimensions": [c for c, r in roles.items() if r == "dimension"],
    }
    if (grain := (form.get("grain") or "").strip()):
        contract["grain"] = grain
    if (dates := date_columns(roles)):
        contract["date_column"] = dates[0]
    aggs = {m: a for m in measures if (a := form.get("aggregations", {}).get(m))}
    if aggs:
        contract["aggregations"] = aggs
    defs = {m: d.strip() for m in measures if (d := form.get("definitions", {}).get(m) or "").strip()}
    if defs:
        contract["measure_definitions"] = defs
    per = {m: list(p) for m in measures if (p := form.get("per", {}).get(m))}
    if per:
        contract["measure_per"] = per
    for side in ("start", "end"):
        if (day := form.get(f"window_{side}")):
            contract[f"analysis_window_{side}"] = day
    # Always sent: [] is the person's statement "no caveats" (probe: confirm succeeded with it).
    contract["caveats"] = [line.strip() for line in (form.get("caveats") or "").splitlines()
                           if line.strip()]
    forks = {k: v for k, v in form.get("forks", {}).items() if v}
    return contract, forks


# --- tool params -----------------------------------------------------------------------------

# How to ask for a required param. Formats are the runner's (backend/tools/runner.py
# `_period_bounds`, `_terms`); kinds only choose the input widget and the JSON type.
PARAMS: dict[str, tuple[str, str, str]] = {
    "period": ("Period", "text", "A month (2026-09), a day (2026-09-05) or a range "
                                "(2026-09-01/2026-09-15)."),
    "baseline": ("Compared with", "text", "Same format as the period: 2026-08, or a range."),
    "month": ("Month", "text", "The month to pace, e.g. 2026-09."),
    "budget": ("Budget", "number", "The month's budget, in the data's currency."),
    "brand_terms": ("Brand terms", "text", "Words that mark a search as brand, comma-separated."),
    "festival": ("Festival", "festival", "Dates are stored per year and confirmed before use."),
    "year": ("Year", "text", "e.g. 2026; compared with the year before."),
    "start": ("Campaign start", "text", "The first day of the campaign, e.g. 2026-09-01."),
    "as_of": ("As of", "text", "The day to measure from, e.g. 2026-09-30."),
    "days": ("Days", "number", "How many days counts as stuck."),
    "focus": ("Focus on (optional)", "text", "One value of the group above, e.g. a hub. Case, "
              "spaces and the pack's aliases are matched; if it could be several, you are "
              "asked which. Blank: the engine picks the worst group and says so."),
}


def optional_params(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A tool's declared optional params, [{name, kind, help, default}] (API 0.8.0, #23).
    F8 read them from the steps' `focus_param`; the spec declares them now."""
    required = set(spec.get("params_required", []))
    return [p for p in spec.get("params_optional") or []
            if isinstance(p, Mapping) and p.get("name") and p["name"] not in required]


def blank_means(declared: Mapping[str, Any]) -> str:
    """What leaving an optional param blank does, as the pack declares it."""
    default = declared.get("default")
    return ("Blank: the engine's choice." if default is None
            else f"Blank: the tool uses {default}.")


def optional_help(declared: Mapping[str, Any]) -> str:
    said = (declared.get("help") or "").strip().rstrip(".")
    return (f"{said}. " if said else "") + blank_means(declared)


def param_spec(name: str) -> tuple[str, str, str]:
    return PARAMS.get(name, (name.replace("_", " ").capitalize(), "text",
                             "As the tool's description says."))


def coerce(name: str, raw: Any) -> Any:
    """A typed value in the JSON type the runner expects; None when left blank."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, dt.date):                    # a declared date param (F10): YYYY-MM-DD
        return date_text(raw)
    if param_spec(name)[1] == "number":
        return raw if isinstance(raw, (int, float)) else float(str(raw).strip())
    return raw.strip() if isinstance(raw, str) else raw


def run_params(required: Iterable[str], values: Mapping[str, Any], slots: Mapping[str, Any],
               bindings: Mapping[str, str], dates_confirmed: bool) -> dict[str, Any]:
    """The params a run sends: what the person filled, nothing guessed."""
    out = {k: v for k in required if (v := coerce(k, values.get(k))) is not None}
    out.update({k: v for k, v in slots.items() if v})
    if bindings:
        out["bindings"] = dict(bindings)
    if dates_confirmed:
        out["dates_confirmed"] = True
    return out


def prefill_answers(prefill: Mapping[str, Any], columns: Iterable[str],
                    answered: Mapping[str, Any], answered_forks: Mapping[str, Any],
                    forks: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple, Any], dict[str, str]]:
    """A similar dataset's confirmed answers (API 0.7.0 `prefill`) for the fields the person
    has NOT answered: {draft path under `contract`: value}, {fork id: option}. The window
    is never filled (it belongs to this file); answers the form cannot hold are dropped."""
    cols = set(columns)
    c = prefill.get("contract") or {}
    out: dict[tuple, Any] = {}
    if not (answered.get("grain") or "").strip() and c.get("grain"):
        out[("grain",)] = c["grain"]
    key = [k for k in c.get("primary_key") or [] if k in cols]
    if not answered.get("key") and key:
        out[("key",)] = key
    roles = {**{d: "dimension" for d in c.get("dimensions", [])},
             **{m: "measure" for m in c.get("measures", [])}}
    if c.get("date_column"):
        roles[c["date_column"]] = "date"
    for column, role in roles.items():
        if column in cols and column not in (answered.get("roles") or {}):
            out[("roles", column)] = role
    for measure, agg in (c.get("aggregations") or {}).items():
        if agg in AGGREGATIONS and not (answered.get("aggregations") or {}).get(measure):
            out[("aggregations", measure)] = agg
    for measure, text in (c.get("measure_definitions") or {}).items():
        if text and not ((answered.get("definitions") or {}).get(measure) or "").strip():
            out[("definitions", measure)] = text
    offered = {f["fork_id"]: {o["id"] for o in f.get("options", [])} for f in forks}
    picks = {f: v for f, v in (prefill.get("fork_choices") or {}).items()
             if v in offered.get(f, set()) and not answered_forks.get(f)}
    return out, picks


# --- results ---------------------------------------------------------------------------------

STATUS_NOTES = {
    "partial": "Partial: a step was skipped or refused (see the caveats).",
    "insufficient_data": "Insufficient data: no figure could be reported.",
}


def lineage(result: Mapping[str, Any]) -> str | None:
    """Where a result came from (API 0.7.0), as returned; None for an older result."""
    parts = []
    snapshot = result.get("snapshot") or {}
    if snapshot:
        parts.append(f"computed on {snapshot.get('rows')} rows (snapshot {snapshot.get('hash')})")
    if result.get("contract_version") is not None:
        parts.append(f"contract v{result['contract_version']}")
    if result.get("grain"):
        parts.append(f"grain: {result['grain']}")
    used = result.get("metrics_used") or {}
    if used:
        parts.append("metrics: " + ", ".join(
            f"{t} → {(m or {}).get('measure', '?')}" for t, m in used.items()))
    if result.get("result_id"):
        parts.append(f"result {result['result_id']}")
    return " · ".join(parts) or None

PROVENANCE = {
    "contract": "from the confirmed contract",
    "provisional": "from a provisional metric: not yet in the contract",
    "derived": "worked out by the tool from contract figures",
}


def shown(value: Any) -> str:
    """A figure exactly as returned; null is a suppressed figure (small group, privacy)."""
    return "suppressed" if value is None else str(value)


def figure_rows(figures: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [{"figure": f["name"], "value": shown(f.get("value")), "unit": f.get("unit") or "",
             "source": f.get("provenance", "")} for f in figures]


def figures_csv(figures: Iterable[Mapping[str, Any]]) -> str:
    """The results table as CSV, cell for cell (F7: replaces v1's downloads and Files page)."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["figure", "value", "unit", "source"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(figure_rows(figures))
    return out.getvalue()


def series_data(series: Mapping[str, Any]) -> dict[str, list]:
    """The points as two columns, in the backend's order, values untouched."""
    return {"x": [p["x"] for p in series["points"]], "y": [p["y"] for p in series["points"]]}


# --- a refused upload's answers (API 0.8.0, #16) ---------------------------------------------

HEADER_JOINS = {"space": "the header rows' words joined with a space",
                "underscore": "the header rows' words joined with _",
                "bottom_only": "the lowest header row only",
                "top_only": "the top header row only"}
COLUMN_TYPES = ("VARCHAR", "BIGINT", "DOUBLE", "DATE", "TIMESTAMP", "BOOLEAN")


def guess_answers(guess: Mapping[str, Any]) -> dict[str, Any]:
    """The reader's guess in the form's names: what "Use the reader's guess" offers."""
    return {"header_rows": list(guess.get("header_rows") or []),
            "header_join": guess.get("header_join"),
            "data_start": guess.get("data_start_row"),
            "footer_rows": guess.get("footer_skip_rows"),
            "name": guess.get("name") or "",
            "columns": {c["source"]: (c.get("target") or "", c.get("type"))
                        for c in guess.get("columns") or [] if c.get("source")}}


def upload_answers(form: Mapping[str, Any]) -> dict[str, Any]:
    """The filled answers only, in the API's names (UploadAnswers). A blank stays unanswered,
    so the reader keeps asking it rather than receiving ''."""
    body: dict[str, Any] = {}
    if form.get("sheet"):
        body["sheet"] = form["sheet"]
    if form.get("header_rows"):
        body["header_rows"] = sorted(int(r) for r in form["header_rows"])
    if form.get("header_join"):
        body["header_join"] = form["header_join"]
    for name in ("data_start", "footer_rows"):
        if form.get(name) is not None:
            body[name] = int(form[name])
    if (name := (form.get("name") or "").strip()):
        body["name"] = name
    columns = []
    for source, (target, kind) in (form.get("columns") or {}).items():
        answer = {"source": source}
        if (target or "").strip():
            answer["target"] = target.strip()
        if kind:
            answer["type"] = kind
        if len(answer) > 1:
            columns.append(answer)
    if columns:
        body["columns"] = columns
    return body


# --- Explore: core analyses run directly (API 0.9.0, #22) ------------------------------------

TIERS = {1: "Descriptive", 2: "Comparative", 3: "Temporal", 4: "Relational", 5: "Anomaly",
         6: "Inferential", 7: "Cohort"}
# Formats are the engine's: a period is a label of the calendar at the analysis's grain
# (backend/engine/analysis/temporal.py GRAINS), a list is the groups to keep.
FIELD_HELP = {
    "period": "A period as the calendar labels it at the chosen grain: 2026-07-06 (a day, or "
              "the day a week starts), 2026-07 (a month), 2026-Q3, 2026.",
    "date": "A day.",
    "list": "Values separated by commas, written as the data writes them.",
    "integer": "A whole number.",
    "number": "A number.",
    "text": "As the analysis names it.",
}


def analysis_label(spec: Mapping[str, Any]) -> str:
    tier = TIERS.get(spec.get("tier"), f"Tier {spec.get('tier')}")
    return f"{tier} · {spec['name'].replace('_', ' ')}"


def field_label(field: Mapping[str, Any]) -> str:
    name = field["name"].replace("_", " ").capitalize()
    return f"{name}" + ("" if field.get("required") else " (optional)")


def analysis_params(fields: Iterable[Mapping[str, Any]], values: Mapping[str, Any]) -> dict[str, Any]:
    """The filled fields only, typed as the engine reads them: whole numbers as integers, a list
    split on commas, a date as YYYY-MM-DD. Raises ValueError for a number field holding text."""
    out: dict[str, Any] = {}
    for field in fields:
        name, kind, raw = field["name"], field["kind"], values.get(field["name"])
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        if kind == "integer":
            number = float(raw)
            if not number.is_integer():
                raise ValueError(f"{name} takes a whole number")
            out[name] = int(number)
        elif kind == "number":
            out[name] = float(raw)
        elif kind == "list":
            if items := [x.strip() for x in str(raw).split(",") if x.strip()]:
                out[name] = items
        elif kind == "date":
            out[name] = date_text(raw) if isinstance(raw, dt.date) else str(raw).strip()
        else:
            out[name] = raw.strip() if isinstance(raw, str) else raw
    return out
