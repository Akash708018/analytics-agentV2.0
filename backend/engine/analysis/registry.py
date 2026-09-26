"""Which analyses exist, and what happens when one is asked for by name.

The nine analyses of Tiers 1 and 2 share a signature and nothing else:
`(con, gate, scope, **params) -> Output`. The `scope` is built once by the
caller and handed in, never rebuilt here -- an analysis that computed its own
would be free to disagree with the method note printed above its own table.

**Why `Output` and not `Result`.** `util.results.Result` is locked decision
20's envelope and stays the only one; but `write_result` takes a
`workspace_id`, and pushing that into nine analysis functions would give every
one of them the filesystem and make none of them testable without a workspace.
So an analysis returns the material -- headers, rows, and the sentences that
have to travel with them -- and the tool layer writes it. One envelope, built
in one place, from what nine functions computed.

**What this module does not do.** It does not catch DuckDB's errors. P8-D8
measured that `sum` raises at the top of the DECIMAL and HUGEINT promotions,
and that refusal has to name the measure and the type -- but building a
`Refusal` here would put `analysis/` above `state.py` in the import graph for
the sake of one message. It raises; `server.py` translates. Same reason
`sql_guard` raises `UnsafeSQL` rather than a refusal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

__all__ = ["Output", "Analysis", "UnknownAnalysis", "register", "get", "run", "catalogue"]


class UnknownAnalysis(KeyError):
    """An analysis_type nobody registered. Carries the list that does exist."""

    def __init__(self, asked: str, available: Sequence[str]):
        self.asked = asked
        self.available = list(available)
        super().__init__(
            f"there is no analysis called {asked!r}. Available: "
            f"{', '.join(self.available)}."
        )

    def __str__(self) -> str:  # KeyError repr()s its argument otherwise
        return self.args[0]


@dataclass(frozen=True)
class Output:
    """What one analysis computed, before it becomes a file and an envelope."""

    headers: list[str]
    rows: list[list[Any]]
    summary: list[str] = field(default_factory=list)
    label: str = "analysis"

    @property
    def n(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class Analysis:
    """One entry: what it is called, what tier it belongs to, what it does."""

    name: str
    tier: int
    run: Callable[..., Output]
    summary: str
    #: Takes `period` (and `grain`), applied to the scope before the analysis runs, so the
    #: analysis describes the rows it was handed (Cleanup Step 9).
    narrows: bool = False
    #: Takes `groups` -- members of its `dimension` to keep -- applied to the scope likewise.
    selects: bool = False


REGISTRY: dict[str, Analysis] = {}


def register(name: str, tier: int, summary: str, narrows: bool = False, selects: bool = False):
    """Decorator. The name is the `analysis_type` a caller asks for."""

    def wrap(fn: Callable[..., Output]) -> Callable[..., Output]:
        if name in REGISTRY:
            raise ValueError(f"{name!r} is registered twice.")
        REGISTRY[name] = Analysis(name=name, tier=tier, run=fn, summary=summary,
                                  narrows=narrows, selects=selects)
        return fn

    return wrap


def catalogue() -> list[tuple[str, int, str]]:
    """Every analysis, by tier then name -- what an unknown type gets told."""
    return sorted(
        ((a.name, a.tier, a.summary) for a in REGISTRY.values()),
        key=lambda t: (t[1], t[0]),
    )


def get(analysis_type: str) -> Analysis:
    """The analysis, or UnknownAnalysis carrying the valid list.

    Phase 8's Done-When: an unknown type returns the list rather than a
    traceback, because a caller who guessed a name is one sentence away from
    the right one and a KeyError does not contain that sentence.
    """
    try:
        return REGISTRY[analysis_type]
    except KeyError:
        raise UnknownAnalysis(analysis_type, [a.name for a in REGISTRY.values()]) from None


def narrowed(con, gate, scope, analysis: Analysis, params: dict):
    """The scope and parameters an analysis actually runs with.

    For an analysis registered with narrows=True, `period` and `grain` are taken off the
    parameters and applied to the scope here, before it runs. They were first applied inside
    each analysis, and the tool layer refused every such result as ANALYSIS_RESULT_UNSOUND: its
    method note described a scope the tool layer had not built (Cleanup Step 9, 4.1). The
    narrowing belongs where the scope is made, which is here and in tools._produce.
    """
    rest = dict(params)
    if analysis.narrows:
        from .temporal import period_narrowing  # temporal imports this module's neighbours

        period, grain = rest.pop("period", None), rest.pop("grain", None)
        scope = period_narrowing(con, gate, scope, analysis.name, period, grain)
    if analysis.selects and rest.get("groups") is not None:
        from .base import select_groups

        scope = select_groups(con, gate, scope, rest.get("dimension"), rest.pop("groups"))
    scope = _per_unit(con, gate, scope, analysis, rest)
    return scope, rest


#: Analyses that read a measure's value row by row, where a ratio of sums has no value.
ROW_VALUE_ANALYSES = frozenset({
    "distribution", "correlation", "bivariate", "outlier_detection", "hypothesis_test",
    "effect_size", "confidence_interval", "sample_adequacy", "driver_analysis", "mix_shift",
})
#: Analyses that place rows on the contract's calendar.
DATED_TIERS = (3, 5, 7)
#: The parameters that name a column an analysis reads, beside its measures.
COLUMN_PARAMS = ("dimension", "second_dimension", "rows", "columns", "column", "entity", "event")


def _per_unit(con, gate, scope, analysis: Analysis, params: dict):
    """A call on a per-unit measure runs over one row per unit; a ratio is refused where a row
    value is needed (Cleanup Step 15). Both by name, before the analysis runs."""
    declared = {m.name: m for m in getattr(gate.contract, "measures", None) or []}
    named = [declared[params[k]] for k in ("measure", "against")
             if isinstance(params.get(k), str) and params[k] in declared]
    for m in named:
        if getattr(m, "agg", None) == "ratio" and analysis.name in ROW_VALUE_ANALYSES:
            raise ValueError(
                f"{m.name} is a ratio of sums: it has a value for a set of rows and none for a "
                f"row, and {analysis.name} reads row values. trend, group_compare, top_n and "
                f"summary_stats compute it; the per-row ratio, if the table has one, is its own "
                f"measure.")
    if not any(getattr(m, "per", None) for m in named):
        return scope
    from .base import unit_scope

    optional = []
    if analysis.name == "driver_analysis":
        optional = [d for d in getattr(gate.contract, "dimensions", [])]
    return unit_scope(con, gate, scope, named,
                      [params[k] for k in COLUMN_PARAMS if isinstance(params.get(k), str)],
                      date=analysis.tier in DATED_TIERS, optional=optional)


def run(con, gate, scope, analysis_type: str, **params) -> Output:
    """Look the analysis up and run it against a scope somebody else built."""
    analysis = get(analysis_type)
    scope, params = narrowed(con, gate, scope, analysis, params)
    return analysis.run(con, gate, scope, **params)
