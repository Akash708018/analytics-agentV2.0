"""
The shape every refusal in the contract layer takes.

Guide 8.2 settled the prose: what was blocked, the exact next call with its
arguments, and the current state. That part is for whoever reads it -- a
person, or the model deciding what to do next.

This module adds the part that is not for reading. Every refusal also carries
a **reason code**: a short constant that never changes wording even when the
prose around it is rewritten. The pattern is RFC 7807's `type` field and
Google's `ErrorInfo.reason` -- human text and machine text side by side,
because the two have opposite requirements. Prose has to be rewritten as it is
found wanting; a test that asserts on prose fails every time someone improves
it.

Two things depend on this and neither can depend on sentences:

  tests/test_phase4.py   asserts the gate fired, not that it said a
                         particular thing while firing
  eval/ (Phase 13)       counts recovery rates per reason -- how often the
                         agent gets past NO_CONTRACT on its first retry is a
                         number, and it is the number the Phase 4 Done-When
                         is really about

The code goes on the LAST line, after a blank line. Last, because the model
reads what is nearest the end of a tool result most reliably -- the same
reason draft.render puts its questions at the bottom. A trailing constant
costs a human nothing and gives a parser an anchor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Reason(str, Enum):
    """
    Why something was refused. Stable identifiers: rename one and every
    recorded eval result stops comparing.
    """

    DATASET_NOT_LOADED = "DATASET_NOT_LOADED"
    NO_CONTRACT = "NO_CONTRACT"
    CONTRACT_PROVISIONAL = "CONTRACT_PROVISIONAL"
    CONTRACT_STALE = "CONTRACT_STALE"
    CONTRACT_INVALID = "CONTRACT_INVALID"
    KEY_NOT_UNIQUE = "KEY_NOT_UNIQUE"
    NOT_IMPLEMENTED_YET = "NOT_IMPLEMENTED_YET"
    # a result file was named and is not there.
    RESULT_NOT_FOUND = "RESULT_NOT_FOUND"
    # a path resolved outside this workspace's results, or a page was asked for that cannot exist.
    RESULT_OUT_OF_SCOPE = "RESULT_OUT_OF_SCOPE"
    # a column was named and the loaded table does not have it.
    COLUMN_NOT_FOUND = "COLUMN_NOT_FOUND"

    # cleaning: an id was approved and there is no plan it could have come from.
    NO_CLEANING_PLAN = "NO_CLEANING_PLAN"
    # cleaning: the table moved since the plan was made. Old, not wrong.
    CLEANING_PLAN_STALE = "CLEANING_PLAN_STALE"
    # cleaning: apply was called with an empty approval list.
    NOTHING_APPROVED = "NOTHING_APPROVED"
    # cleaning: an approved id is not in the plan. Refuses the whole call.
    ACTION_NOT_IN_PLAN = "ACTION_NOT_IN_PLAN"
    # cleaning: two approved actions cannot both run, in the order given.
    ACTIONS_CONFLICT = "ACTIONS_CONFLICT"

    # analysis: an analysis_type nobody registered. Recovery is the catalogue.
    ANALYSIS_NOT_FOUND = "ANALYSIS_NOT_FOUND"
    # analysis: the call named an analysis that exists and gave it arguments
    # it cannot take, or values it cannot parse. Recovery is fixing the call.
    ANALYSIS_PARAMS_INVALID = "ANALYSIS_PARAMS_INVALID"
    # analysis: the contract does not permit what was asked -- an undeclared
    # or excluded column, a measure with no aggregate, a non-additive total,
    # more groups than a table can hold. Recovery is usually the contract.
    ANALYSIS_NOT_POSSIBLE = "ANALYSIS_NOT_POSSIBLE"
    # analysis: the result did not add back to its scope, or arrived without
    # the note saying what it was computed over. Not a caller error: this is
    # the failure the whole phase is arranged against, counted rather than
    # raised as a traceback.
    ANALYSIS_RESULT_UNSOUND = "ANALYSIS_RESULT_UNSOUND"


_REASON_LINE = re.compile(r"^reason:\s*([A-Z_]+)\s*$", re.M)


@dataclass
class Refusal:
    """
    One refusal, in the only shape this codebase produces.

    what       the sentence after BLOCKED:. What could not be done.
    why        why it could not be done, in terms of the data or the state --
               never in terms of an internal exception.
    next_call  the EXACT call to make next, with its arguments filled in. Not
               "confirm the contract first" but
               confirm_dataset_contract(contract_json=...). Checked for
               parentheses on construction, because an instruction the agent
               cannot execute is how the apology loop starts.
    """

    reason: Reason
    what: str
    why: str
    next_call: str
    state: str = ""
    detail: str = ""
    outstanding: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if "(" not in self.next_call or ")" not in self.next_call:
            raise ValueError(
                f"next_call must be a call the agent can make, with "
                f"parentheses and arguments; got {self.next_call!r}. A refusal "
                f"that describes an intention rather than naming a call is "
                f"what F1 is."
            )
        if not isinstance(self.reason, Reason):
            raise ValueError(f"reason must be a Reason, got {self.reason!r}")

    def to_text(self) -> str:
        lines = [f"BLOCKED: {self.what}", f"WHY: {self.why}"]
        if self.detail:
            lines.append(f"DETAIL: {self.detail}")
        if self.outstanding:
            lines.append("OUTSTANDING:")
            lines += [f"  - {o}" for o in self.outstanding]
        if self.state:
            lines.append(f"CURRENT STATE: {self.state}")
        lines.append(f"NEXT STEP: call {self.next_call}")
        lines.append("")
        lines.append(f"reason: {self.reason.value}")
        return "\n".join(lines)

    def __str__(self) -> str:  # so raise ... and print both read the same
        return self.to_text()


def reason_of(text: str) -> Reason | None:
    """
    The reason code in a refusal, or None if the text is not one.

    Tests and the eval harness call this instead of matching on sentences.
    None means the string was a result, not a refusal -- which is itself the
    assertion worth making about a successful call.
    """
    match = _REASON_LINE.search(text or "")
    if not match:
        return None
    try:
        return Reason(match.group(1))
    except ValueError:
        return None


__all__ = ["Reason", "Refusal", "reason_of"]
