"""Preparation helpers: pure mappings between what the API returns, what the person
chose, and what the API takes. No Streamlit and no network here; nothing is computed
about the data. See docs/steps/F3.md.
"""

from __future__ import annotations

import datetime as dt
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
