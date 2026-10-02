"""Keyword groups (F6): reading the backend's listing, and saying what an action will do.

The consequences stated here are the backend's own rules, measured in docs/steps/F6.md
(backend/services/keywords.py): a merge lands in the FIRST group id and takes its approval;
a keyword moved into a group takes that group's approval; split-off keywords become a new
proposal. Nothing here decides for the person; it describes before they send.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

Group = Mapping[str, Any]
STATUS = ("All", "Proposals", "Approved")


def facets_text(facets: Mapping[str, Any]) -> str:
    """{"area": ["baner"], "dish": ["sushi"]} -> "area: baner · dish: sushi", as returned."""
    parts = []
    for name, values in facets.items():
        shown = ", ".join(map(str, values)) if isinstance(values, list) else str(values)
        parts.append(f"{name}: {shown}")
    return " · ".join(parts)


def heading(group: Group) -> str:
    status = "✅ approved" if group["approved"] else "proposal"
    return (f"**{group['label']}** · {group['intent']} · {len(group['keywords'])} keyword(s) · "
            f"{status}")


def summary(groups: Sequence[Group]) -> str:
    approved = [g for g in groups if g["approved"]]
    return (f"{len(groups)} group(s) · {sum(len(g['keywords']) for g in groups)} keyword(s) · "
            f"{len(approved)} approved ({sum(len(g['keywords']) for g in approved)} keyword(s))")


def visible(groups: Iterable[Group], status: str, intents: Sequence[str], find: str) -> list[Group]:
    """The person's view filter; it never changes what is sent."""
    needle = find.strip().lower()
    out = []
    for g in groups:
        if status == "Proposals" and g["approved"] or status == "Approved" and not g["approved"]:
            continue
        if intents and g["intent"] not in intents:
            continue
        if needle and not any(needle in k.lower() for k in g["keywords"]) \
                and needle not in g["label"].lower():
            continue
        out.append(g)
    return out


def ticked(ticks: Mapping[str, Any], groups: Sequence[Group]) -> list[str]:
    """Ticked ids that still exist, in listing order (a rerun replaces proposal ids)."""
    return [g["group_id"] for g in groups if ticks.get(g["group_id"]) is True]


def merge_notes(target: Group, others: Sequence[Group]) -> tuple[str, list[str]]:
    """What a merge into `target` will do: (outcome, warnings)."""
    moved = sum(len(g["keywords"]) for g in others)
    if target["approved"]:
        outcome = (f"The merged group keeps the name and approval of '{target['label']}': the "
                   f"{moved} keyword(s) merged in count as approved.")
    else:
        outcome = (f"The merged group is a proposal, like '{target['label']}': approve it "
                   "afterwards for the tools to count it.")
    warnings = [f"'{g['label']}' is approved; merging it into a proposal withdraws its approval "
                f"({len(g['keywords'])} keyword(s))." for g in others
                if g["approved"] and not target["approved"]]
    return outcome, warnings


def move_note(source: Group, target: Group) -> str:
    if target["approved"] and not source["approved"]:
        return f"'{target['label']}' is approved, so the keyword becomes approved."
    if source["approved"] and not target["approved"]:
        return (f"'{target['label']}' is a proposal, so the keyword is no longer approved until "
                "that group is.")
    return "Its approval does not change."


def split_note(source: Group) -> str:
    return ("The keywords split off become a new proposal"
            + (", no longer approved," if source["approved"] else "")
            + " until you approve it.")
