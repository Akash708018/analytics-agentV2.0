"""Load, validate and merge packs. `core` is always on; `extends` pulls parents in first."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from backend.packs.models import Concept, Fork, MetricTemplate, Pack, Tool, ValidityRule

PACKS_DIR = Path(__file__).resolve().parents[2] / "packs"


class PackError(ValueError):
    pass


def load_file(path: Path) -> Pack:
    try:
        return Pack(**(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
    except Exception as e:  # noqa: BLE001 -- re-raised with the file named
        raise PackError(f"{path}: {e}") from e


@lru_cache(maxsize=8)
def load_all(root: Path = PACKS_DIR) -> dict[str, Pack]:
    packs = {}
    for f in sorted(Path(root).glob("*/pack.yaml")):
        p = load_file(f)
        if p.pack.id != f.parent.name:
            raise PackError(f"{f}: pack id {p.pack.id!r} must equal its folder name")
        packs[p.pack.id] = p
    if "core" not in packs:
        raise PackError(f"{root}: no core pack")
    for p in packs.values():
        for parent in p.pack.extends:
            if parent not in packs:
                raise PackError(f"{p.pack.id} extends unknown pack {parent!r}")
    for pid in packs:
        merge(packs, [pid])  # every pack must resolve on its own
    return packs


@dataclass
class Merged:
    """The union of core + the requested packs (+ their parents). Later packs override by id."""
    pack_ids: list[str]
    concepts: dict[str, Concept] = field(default_factory=dict)
    concept_owner: dict[str, str] = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    forks: dict[str, Fork] = field(default_factory=dict)
    templates: dict[str, MetricTemplate] = field(default_factory=dict)
    validity_rules: dict[str, ValidityRule] = field(default_factory=dict)
    interpretation_rules: dict = field(default_factory=dict)
    playbooks: dict = field(default_factory=dict)
    tools: dict[str, Tool] = field(default_factory=dict)
    word_classes: dict[str, set[str]] = field(default_factory=dict)
    min_group_size: int = 5
    pii_classes: dict[str, list[str]] = field(default_factory=dict)
    changelog: list = field(default_factory=list)
    festivals: dict = field(default_factory=dict)
    value_aliases: dict[str, str] = field(default_factory=dict)


def _order(packs: dict[str, Pack], ids: list[str]) -> list[str]:
    out: list[str] = []

    def visit(pid: str, stack: tuple = ()) -> None:
        if pid in stack:
            raise PackError(f"extends cycle: {' -> '.join(stack + (pid,))}")
        if pid in out:
            return
        for parent in packs[pid].pack.extends:
            visit(parent, stack + (pid,))
        out.append(pid)

    for pid in ["core", *ids]:
        if pid not in packs:
            raise PackError(f"unknown pack {pid!r}")
        visit(pid)
    return out


def merge(packs: dict[str, Pack], ids: list[str]) -> Merged:
    m = Merged(pack_ids=_order(packs, ids))
    for pid in m.pack_ids:
        p = packs[pid]
        for c in p.vocabulary:
            m.concepts[c.id] = c
            m.concept_owner[c.id] = pid
        m.sources.update({s.id: s for s in p.sources})
        m.forks.update({f.id: f for f in p.forks})
        m.templates.update({t.id: t for t in p.metric_templates})
        m.validity_rules.update({r.id: r for r in p.validity_rules})
        m.interpretation_rules.update({r.id: r for r in p.interpretation_rules})
        m.playbooks.update({b.id: b for b in p.playbooks})
        m.tools.update({t.id: t for t in p.tools})
        for k, words in p.word_classes.items():
            m.word_classes.setdefault(k, set()).update(words)
        m.changelog.extend(p.changelog)
        m.festivals.update({f.id: f for f in p.festivals})
        m.value_aliases.update({k.lower(): v.lower() for k, v in p.value_aliases.items()})
        m.min_group_size = max(m.min_group_size, p.privacy.min_group_size)
        for k, v in p.privacy.pii_classes.items():
            m.pii_classes.setdefault(k, []).extend(v)
    _check_refs(m)
    return m


def step_params(t: Tool) -> set[str]:
    """Every param a tool's steps read: @param:x, @param?:x, and the filters' *_param keys."""
    import re
    out: set[str] = set()

    def walk(v):
        if isinstance(v, str):
            out.update(re.findall(r"@param\??:([a-z_]+)", v))
        elif isinstance(v, dict):
            for k, x in v.items():
                if k.endswith("_param") and isinstance(x, str):
                    out.add(x)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for s in t.steps:
        walk(s.params)
        walk(s.filter)
    return out


def _check_refs(m: Merged) -> None:
    bad = []
    for t in m.templates.values():
        bad += [f"template {t.id}: unknown concept {c}" for c in t.required_concepts
                if c not in m.concepts]
        bad += [f"template {t.id}: unknown fork {f}" for f in t.forks if f not in m.forks]
    for t in m.tools.values():
        bad += [f"tool {t.id}: unknown concept {c}" for c in t.required_concepts
                if c not in m.concepts]
        bad += [f"tool {t.id}: unknown fork {f}" for f in t.forks if f not in m.forks]
        bad += [f"tool {t.id}: unknown source {s}" for s in t.required_sources
                if s not in m.sources]
        bad += [f"tool {t.id}: optional param '{p}' read by a step is not declared in "
                f"params_required or params_optional" for p in step_params(t)
                if p not in set(t.params_required) | {x.name for x in t.params_optional}]
        known = set(m.validity_rules) | set(m.interpretation_rules)
        bad += [f"tool {t.id}: unknown rule {r}" for r in t.rules if r not in known]
    for b in m.playbooks.values():
        bad += [f"playbook {b.id}: unknown tool {s.tool}" for s in b.steps
                if s.tool not in m.tools]
        bad += [f"playbook {b.id}: unknown rule {r}" for r in b.rules
                if r not in m.interpretation_rules]
        bad += [f"playbook {b.id}: unknown metric {t}" for t in b.requires_metrics
                if t not in m.templates]
        bad += [f"playbook {b.id}: unknown concept {c}" for c in b.requires_concepts
                if c not in m.concepts]
        if len(b.steps) > b.max_tool_calls:
            bad.append(f"playbook {b.id}: {len(b.steps)} steps over its budget "
                       f"{b.max_tool_calls}")
    for s in m.sources.values():
        bad += [f"source {s.id}: unknown concept {c}" for c in s.concepts + s.required
                if c not in m.concepts]
    for f in m.forks.values():
        bad += [f"fork {f.id}: unknown concept {c}" for c in f.applies_when if c not in m.concepts]
    for r in m.validity_rules.values():
        if r.concept not in m.concepts:
            bad.append(f"rule {r.id}: unknown concept {r.concept}")
    if bad:
        raise PackError("; ".join(bad))


def core_word_classes() -> dict[str, set[str]]:
    """The engine's measure-suggester vocabulary (engine/contract/suggest.py reads this)."""
    return {k: set(v) for k, v in load_file(PACKS_DIR / "core" / "pack.yaml").word_classes.items()}
