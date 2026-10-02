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
}


def param_spec(name: str) -> tuple[str, str, str]:
    return PARAMS.get(name, (name.replace("_", " ").capitalize(), "text",
                             "As the tool's description says."))


def coerce(name: str, raw: Any) -> Any:
    """A typed value in the JSON type the runner expects; None when left blank."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
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


# --- results ---------------------------------------------------------------------------------

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
