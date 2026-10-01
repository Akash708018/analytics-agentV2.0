"""Keyword grouping: clean -> facets -> embed topic words -> cluster -> facet split -> label.

Everything here PROPOSES. A person approves, renames, merges, moves or splits (services).
"""
from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from backend.text.embed import Cache, embed, version_of

FACETS_FILE = Path(__file__).resolve().parents[2] / "packs" / "marketing" / "keyword_facets.yaml"
THRESHOLD = 0.6          # cosine DISTANCE for average linkage; stated before scoring (B7.md)
MERGE_RATIO = 0.8        # a rare token within this similarity of a frequent one is a typo of it
FACET_ORDER = ("brand", "info", "delivery", "near_me", "price", "area")


def load_facets(path: Path = FACETS_FILE) -> dict[str, list[str]]:
    return {k: [str(x).lower() for x in v] for k, v in yaml.safe_load(path.read_text()).items()}


def normalise(k: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s&'-]", " ", k.lower())).strip()


def spelling_map(keywords: list[str], protected: set[str]) -> dict[str, str]:
    """token -> the frequent token it is a misspelling of. Frequent = seen in 2+ keywords, or a
    facet word. Only rare tokens move, only to a token with the same first letter."""
    counts = Counter(t for k in keywords for t in set(k.split()))
    anchors = {t for t, n in counts.items() if n >= 2} | protected
    out = {}
    for tok in counts:
        if tok in anchors or len(tok) < 4:
            continue
        best = max(((difflib.SequenceMatcher(None, tok, a).ratio(), a) for a in anchors
                    if a[0] == tok[0] and abs(len(a) - len(tok)) <= 2), default=(0, None))
        if best[0] >= MERGE_RATIO:
            out[tok] = best[1]
    return out


def _find(text: str, phrases: list[str]) -> list[str]:
    return [p for p in sorted(phrases, key=len, reverse=True)
            if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text)]


@dataclass
class Keyword:
    raw: str
    text: str                       # cleaned, typos merged
    facets: dict = field(default_factory=dict)
    topic: str = ""                 # what is left once facet words are removed


def facet(k: str, F: dict) -> Keyword:
    kw = Keyword(raw=k, text=k)
    rest = k
    for name, key in (("brand", "brands"), ("info", "informational"), ("delivery", "delivery"),
                      ("near_me", "near_me"), ("price", "price"), ("area", "areas")):
        hits = _find(rest, F.get(key, []))
        if hits:
            kw.facets[name] = hits[0]
            for h in hits:
                rest = re.sub(rf"(?<!\w){re.escape(h)}(?!\w)", " ", rest)
    dish = _find(k, F.get("dishes", []))
    if dish:
        kw.facets["dish"] = dish[0]
    for c in F.get("city", []):
        rest = re.sub(rf"(?<!\w){re.escape(c)}(?!\w)", " ", rest)
    rest = re.sub(r"\b(in|at|for|the|a|best|top|good|restaurant|restaurants|wala|ka|ki|ke|"
                  r"mein|me|pe|par|near|and|&)\b", " ", rest)
    kw.topic = re.sub(r"\s+", " ", rest).strip()
    return kw


def cluster(topics: list[str], backend: str = "auto", cache: Cache | None = None,
            threshold: float = THRESHOLD) -> tuple[dict[str, int], str]:
    from sklearn.cluster import AgglomerativeClustering
    uniq = sorted({t for t in topics if t})
    if not uniq:
        return {}, "none"
    if len(uniq) == 1:
        return {uniq[0]: 0}, "none"
    X, used = embed(uniq, backend, cache)
    labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                     distance_threshold=threshold).fit_predict(X)
    cluster.last_dims = int(X.shape[1])
    return {t: int(c) for t, c in zip(uniq, labels, strict=True)}, used


def intent_of(facets: dict) -> str:
    if "brand" in facets:
        return "navigational"
    if "info" in facets:
        return "informational"
    if "delivery" in facets:
        return "transactional"
    if "near_me" in facets or "area" in facets:
        return "local"
    return "commercial"


@dataclass
class Group:
    group_id: str
    label: str
    intent: str
    keywords: list[str]
    facets: dict
    proposed_by: str


def group_keywords(raw: list[str], *, backend: str = "auto", cache: Cache | None = None,
                   facets_file: Path = FACETS_FILE, threshold: float = THRESHOLD,
                   labeler: Callable[[list[str]], dict] | None = None) -> dict:
    F = load_facets(facets_file)
    cleaned = {}
    for k in raw:
        n = normalise(k)
        if n:
            cleaned.setdefault(n, k)                  # dedupe: first raw spelling kept
    protected = {t for v in F.values() for p in v for t in p.split()}
    fix = spelling_map(list(cleaned), protected)
    merged: dict[str, list[str]] = defaultdict(list)  # corrected text -> raw keywords
    for n, r in cleaned.items():
        merged[" ".join(fix.get(t, t) for t in n.split())].append(r)
    kws = {t: facet(t, F) for t in merged}
    cmap, used = cluster([k.topic for k in kws.values()], backend, cache, threshold)
    buckets: dict[tuple, list[str]] = defaultdict(list)
    for t, k in kws.items():
        # split on the LEADING facet only: "sushi delivery kalyani nagar" wants the delivery
        # page like "sushi delivery pune"; the area stays an attribute (facets), not a split
        sig = tuple(f for f in FACET_ORDER if f in k.facets)[:1]
        buckets[(cmap.get(k.topic, -1), sig)].append(t)
    groups = []
    for i, ((_, sig), texts) in enumerate(sorted(buckets.items(), key=lambda x: -len(x[1]))):
        members = [r for t in texts for r in merged[t]]
        fac = {f: sorted({kws[t].facets[f] for t in texts if f in kws[t].facets})
               for f in (*FACET_ORDER, "dish") if any(f in kws[t].facets for t in texts)}
        topic = Counter(w for t in texts for w in kws[t].topic.split()).most_common(2)
        label = " ".join(w for w, _ in topic) or "(general)"
        label += "".join(f" · {f.replace('_', ' ')}" for f in sig if f != "brand") \
            if "brand" not in sig else " · brand"
        proposal = {"label": label, "intent": intent_of(kws[texts[0]].facets),
                    "by": "rules"}
        if labeler is not None:
            try:
                got = labeler(members)
                proposal = {"label": str(got["label"])[:80], "intent": got["intent"],
                            "by": "llm"}
            except Exception:  # noqa: BLE001 -- a bad proposal falls back to the rules'
                pass
        groups.append(Group(f"g{i + 1}", proposal["label"], proposal["intent"],
                            sorted(members), {k: v for k, v in fac.items() if v},
                            proposal["by"]))
    return {"groups": groups, "embedding": used, "threshold": threshold,
            "embedding_version": version_of(used, getattr(cluster, "last_dims", 0)),
            "typos_merged": {k: v for k, v in sorted(fix.items())},
            "input": len(raw), "distinct": len(cleaned), "after_merge": len(merged)}


def _sig(k: Keyword) -> tuple:
    return tuple(f for f in FACET_ORDER if f in k.facets)[:1]


def carry_forward(new: list[str], approved: dict[str, list[str]], *, backend: str = "auto",
                  cache: Cache | None = None, facets_file: Path = FACETS_FILE,
                  threshold: float = THRESHOLD) -> tuple[dict[str, tuple[str, float]], dict]:
    """New keywords that belong to an APPROVED group (B9 concept 16): same leading facet, and
    the keyword's topic within `threshold` cosine distance of the group's centroid. Approved
    and new texts are embedded in ONE call, so they share one vector space. Proposes only."""
    import numpy as np
    F = load_facets(facets_file)
    newk = {k: facet(normalise(k), F) for k in new if normalise(k)}
    grp = {g: [facet(normalise(k), F) for k in ks if normalise(k)] for g, ks in approved.items()}
    grp = {g: ks for g, ks in grp.items() if ks}
    texts = sorted({k.topic or k.text for k in newk.values()} |
                   {k.topic or k.text for ks in grp.values() for k in ks})
    if not newk or not grp:
        return {}, version_of("none", 0)
    X, used = embed(texts, backend, cache)
    row = {t: X[i] for i, t in enumerate(texts)}
    cents, sigs = {}, {}
    for g, ks in grp.items():
        c = np.mean([row[k.topic or k.text] for k in ks], axis=0)
        cents[g] = c / max(float(np.linalg.norm(c)), 1e-12)
        sigs[g] = Counter(_sig(k) for k in ks).most_common(1)[0][0]
    out = {}
    for raw, k in newk.items():
        v = row[k.topic or k.text]
        best = min(((1 - float(v @ c), g) for g, c in cents.items() if sigs[g] == _sig(k)),
                   default=None)
        if best is not None and best[0] <= threshold:
            out[raw] = (best[1], round(best[0], 3))
    return out, version_of(used, int(X.shape[1]))


def score(groups: list[list[str]], gold: dict[str, str]) -> dict:
    """Purity: share of keywords in their group's majority gold class. Completeness (inverse
    purity): share of keywords in their gold class's majority group."""
    n = sum(len(g) for g in groups)
    purity = sum(max(Counter(gold.get(k, "?") for k in g).values()) for g in groups) / n
    where = {k: i for i, g in enumerate(groups) for k in g}
    by_gold: dict[str, list[int]] = defaultdict(list)
    for k, c in gold.items():
        if k in where:
            by_gold[c].append(where[k])
    complete = sum(max(Counter(v).values()) for v in by_gold.values()) / n
    return {"purity": round(purity, 3), "completeness": round(complete, 3),
            "groups": len(groups), "gold_classes": len(set(gold.values())), "keywords": n}
