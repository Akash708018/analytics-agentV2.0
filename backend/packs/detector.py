"""Which concepts, sources and domains a table's columns suggest -- with the evidence.

Pure over column names (and, optionally, a few sample values). Proposes; never confirms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.packs.loader import Merged, load_all, merge

_SPLIT = re.compile(r"[^a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalise(name: str) -> str:
    return "_".join(t for t in _SPLIT.split(_CAMEL.sub("_", name).lower()) if t)


def hint_matches(hint: str, norm: str) -> int:
    """0 = no match; 3 = the whole name; 2 = a multi-token run inside it; 1 = one token of it."""
    if hint == norm:
        return 3
    toks = norm.split("_")
    h = hint.split("_")
    if len(h) > 1:
        return 2 if any(toks[i:i + len(h)] == h for i in range(len(toks))) else 0
    return 1 if hint in toks else 0


@dataclass
class Detection:
    bindings: dict[str, list[str]] = field(default_factory=dict)       # concept -> columns
    sources: list[dict] = field(default_factory=list)                  # sorted by score
    domains: list[dict] = field(default_factory=list)


def bind(columns: list[str], m: Merged) -> dict[str, list[str]]:
    """Each column binds to the concepts whose best hint match is the strongest for it."""
    out: dict[str, list[str]] = {}
    for col in columns:
        norm = normalise(col)
        scored = [(max(hint_matches(h, norm) for h in c.hints), c.id)
                  for c in m.concepts.values()]
        best = max((s for s, _ in scored), default=0)
        if best == 0:
            continue
        for s, cid in scored:
            if s == best:
                out.setdefault(cid, []).append(col)
    return out


def detect(columns: list[str], packs=None) -> Detection:
    packs = packs or load_all()
    domain_ids = [p for p in packs if p != "core"]
    m = merge(packs, domain_ids)
    b = bind(columns, m)
    det = Detection(bindings=b)
    for s in m.sources.values():
        total = sum(m.concepts[c].weight for c in s.concepts)
        hit = [c for c in s.concepts if c in b]
        score = round(sum(m.concepts[c].weight for c in hit) / total, 3) if total else 0.0
        if all(r in b for r in s.required) and score >= s.min_score:
            cols = sorted({col for c in hit for col in b[c]})
            det.sources.append({"source": s.id, "evidence": {"matched_columns": cols,
                                                            "score": score}})
    det.sources.sort(key=lambda x: -x["evidence"]["score"])
    for pid in domain_ids:
        own = [c for c, owner in m.concept_owner.items() if owner == pid and c in b]
        signalling = {s.id for s in packs[pid].sources if s.signals_domain}
        src = [x for x in det.sources if x["source"] in signalling]
        if src:
            score = max(x["evidence"]["score"] for x in src)
        elif packs[pid].sources:
            score = 0.0      # only supporting sources matched (e.g. orders): not evidence
        else:
            score = round(min(1.0, sum(m.concepts[c].weight for c in own) / 4), 3)
        if own and score > 0:
            cols = sorted({col for c in own for col in b[c]})
            det.domains.append({"domain": pid, "evidence": {"matched_columns": cols,
                                                          "score": score}})
    det.domains.sort(key=lambda x: -x["evidence"]["score"])
    return det
