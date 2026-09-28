"""Metrics the assistant proposes and a person approves, outside the contract (step 3, 26/09/2026).

A live SLA question on a last-mile file was refused: "breach = recorded delivery minutes >
promised minutes" was not a column and not in the contract, though both columns were. Decided
with the user: the assistant may PROPOSE such a metric; nothing is computed with it until a person
approves it on the Ask screen. An approved metric is provisional: it joins a copy of the contract
in force for the length of one analysis, and every result reading it prints its formula and says
it is not in the contract. Adding it to the contract is a separate, ordinary contract change.

A proposal is a comparison of two columns (or a column and a number) from a fixed operator list,
never SQL text; the engine validates the names and types and counts, before anyone approves, how
many rows it can judge and how many it is true for. Stored per workspace in
`provisional_metrics.json`; a reset takes it with the workspace directory.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field

from backend.engine import workspace
from backend.engine.contract.dataset_contract import COMPARE_OPS, Measure

FILE = "provisional_metrics.json"
#: The line every result reading a provisional metric carries. webapp/verify.py treats the
#: numbers it describes as measured -- they are computed -- but a person reads the label.
PROVISIONAL = "PROVISIONAL metric, approved by the person but not in the contract: "


@dataclass
class Proposal:
    id: str
    dataset_name: str
    name: str
    left: str
    op: str
    right: str = ""
    value: float | None = None
    agg: str = "mean"
    definition: str = ""
    status: str = "pending"          # pending, approved, rejected
    proposed_at: str = ""
    decided_at: str = ""
    judged: int = 0                  # rows where both sides have a value
    true_rows: int = 0
    rows: int = 0
    notes: list[str] = field(default_factory=list)

    def measure(self) -> Measure:
        return Measure(name=self.name, agg=self.agg, definition=self.definition,
                       compare=[self.left, self.op, self.right], value=self.value,
                       provisional=f"approved {self.decided_at}")

    def formula(self) -> str:
        return self.measure().formula()

    def label(self) -> str:
        return (f"{PROVISIONAL}{self.name} = 1 where {self.formula()}, else 0 "
                f"({'its mean is a rate' if self.agg == 'mean' else 'its sum is a count'}); "
                f"approved {self.decided_at}.")


class ProposalError(ValueError):
    """A proposal the engine will not store, with the reason in words."""


def _path(workspace_id: str):
    return workspace.workspace_dir(workspace_id) / FILE


def load(workspace_id: str) -> list[Proposal]:
    path = _path(workspace_id)
    if not path.is_file():
        return []
    return [Proposal(**p) for p in json.loads(path.read_text(encoding="utf-8"))]


def _save(workspace_id: str, proposals: list[Proposal]) -> None:
    _path(workspace_id).write_text(json.dumps([asdict(p) for p in proposals], indent=2),
                                   encoding="utf-8")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def propose(con, workspace_id: str, dataset_name: str, *, name: str, left: str, op: str,
            right: str | None = None, value: float | None = None, agg: str = "mean",
            definition: str = "", contract=None) -> Proposal:
    """Validate and count a proposed comparison; store it pending. ProposalError if unusable."""
    from backend.engine.analysis.base import compare_sql
    from backend.engine.analysis.declared import column_types

    if op not in COMPARE_OPS:
        raise ProposalError(f"op must be one of {', '.join(COMPARE_OPS)}, not {op!r}.")
    if not definition.strip():
        raise ProposalError("a metric needs a definition in words: what 1 means, e.g. 'the "
                            "order was delivered later than promised'.")
    try:
        measure = Measure(name=name, agg=agg, definition=definition,
                          compare=[left, op, right or ""], value=value)
    except ValueError as exc:
        raise ProposalError(str(exc).splitlines()[-1]) from None
    types = column_types(con, dataset_name)
    if not types:
        raise ProposalError(f"no table {dataset_name} is loaded.")
    taken = set(types) | {m.name for m in getattr(contract, "measures", None) or []}
    if name in taken:
        raise ProposalError(f"{name} is already a column or a measure of {dataset_name}; "
                            f"name the new metric something else.")
    try:
        expr = compare_sql(measure, types)
    except ValueError as exc:
        raise ProposalError(str(exc)) from None
    from backend.engine.util.sql_guard import quote_identifier

    rows, judged, true_rows = con.execute(
        f"SELECT count(*), count({expr}), coalesce(sum({expr}), 0) "
        f"FROM {quote_identifier(dataset_name)}").fetchone()
    if not judged:
        raise ProposalError(f"{measure.formula()} cannot be judged on any row of "
                            f"{dataset_name}: a side is blank in every row.")
    proposals = [p for p in load(workspace_id)
                 if not (p.dataset_name == dataset_name and p.name == name
                         and p.status == "pending")]
    p = Proposal(id=secrets.token_hex(4), dataset_name=dataset_name, name=name, left=left, op=op,
                 right=right or "", value=value, agg=agg, definition=definition.strip(),
                 proposed_at=_now(), judged=judged, true_rows=int(true_rows), rows=rows)
    if judged < rows:
        p.notes.append(f"{rows - judged:,} of {rows:,} row(s) have a blank side and are left out "
                       f"of the metric (neither 1 nor 0).")
    proposals.append(p)
    _save(workspace_id, proposals)
    return p


def decide(workspace_id: str, proposal_id: str, approve: bool) -> Proposal:
    proposals = load(workspace_id)
    for p in proposals:
        if p.id == proposal_id:
            if p.status != "pending":
                raise ProposalError(f"metric {p.name} is already {p.status}.")
            if approve:
                # One approved metric of a name per table: a newer approval replaces it.
                for q in proposals:
                    if (q is not p and q.dataset_name == p.dataset_name and q.name == p.name
                            and q.status == "approved"):
                        q.status = "replaced"
            p.status = "approved" if approve else "rejected"
            p.decided_at = _now()
            _save(workspace_id, proposals)
            return p
    raise ProposalError(f"no proposed metric has id {proposal_id!r}.")


def pending(workspace_id: str) -> list[Proposal]:
    return [p for p in load(workspace_id) if p.status == "pending"]


def approved(workspace_id: str, dataset_name: str) -> list[Proposal]:
    return [p for p in load(workspace_id)
            if p.dataset_name == dataset_name and p.status == "approved"]


def describe(p: Proposal) -> str:
    """What the engine measured about a proposal, in words."""
    rate = p.true_rows / p.judged if p.judged else 0
    lines = [f"{p.name} = 1 where {p.formula()}, else 0 -- {p.definition}",
             f"Judged on {p.judged:,} of {p.rows:,} row(s); true for {p.true_rows:,} "
             f"({rate:.1%}) over the whole table, before any filter."]
    return "\n".join(lines + p.notes)


__all__ = ["PROVISIONAL", "Proposal", "ProposalError", "approved", "decide", "describe", "load",
           "pending", "propose"]
