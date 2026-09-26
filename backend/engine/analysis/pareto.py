"""How much of a total sits in how few groups, two ways.

`pareto` walks the groups from biggest down and says how few reach a
threshold. `concentration` reports the same curve at fixed cuts, plus two
indices. One file, because they are one question asked twice, and because
everything hard about one is hard about the other.

**Neither runs without a denominator.** `top_n` drops its share column and
keeps its ranking, which is still an answer. A Pareto curve without shares is
not an answer at all, so `share_basis`'s refusal is raised here rather than
printed beside an empty column.

**The cumulative share accumulates the totals, not the printed shares.** Adding
'34.1%' four times is adding numbers that were rounded for a reader. The
running total is kept in the column's own type -- Decimal stays Decimal -- and
divided once per row.

**HHI is reported without its verdict.** The Herfindahl-Hirschman Index is a
term of art with antitrust thresholds attached (1500, 2500). Those thresholds
are about product markets; printing "highly concentrated" over a breakdown by
`status` or `warehouse` would be a claim about competition that nobody made.
The number, and what it is the sum of. Not the interpretation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..util.sql_guard import quote_identifier
from .base import ParamsInvalid, label, number, share_basis
from .declared import AGG_SQL, agg_of, require_dimension, require_measure
from .registry import Output, register
from .stats import ranked_totals

DEFAULT_THRESHOLD = 80.0
CUTS = (1, 3, 5, 10, 20)
# Cuts at a share of the groups, once there are enough groups for a percent to be a group or more.
PERCENT_CUTS = (1, 5, 10)
PERCENT_CUTS_FROM = 100


def _ranked_with_shares(con, gate, scope, dimension: str, measure: str):
    """The groups, biggest first, and the basis every share divides by."""
    contract = gate.contract
    require_dimension(contract, dimension)
    m = require_measure(contract, measure)
    agg = agg_of(m)
    if agg is None:
        raise ValueError(
            f"measure {measure!r} has no declared aggregate, so its groups "
            f"have no totals to concentrate. confirm_dataset_contract with agg "
            f"set is what fixes that."
        )
    if agg == "none":
        raise ValueError(
            f"measure {measure!r} is declared non-additive, so a share of its "
            f"total would be a proportion of a number the contract says is not "
            f"meaningful."
        )
    if agg not in AGG_SQL:
        raise ValueError(f"cannot concentrate by agg={agg!r}.")

    total_sql = AGG_SQL[agg].format(col=quote_identifier(measure))
    grouped = ranked_totals(con, scope, dimension, total_sql)
    # No group cap here (Cleanup Step 14, RF-O8). MAX_GROUPS keeps a per-group TABLE inside the
    # display limit; pareto's answer is a count to a threshold and concentration's is six cuts and
    # an index. The retail run's 500 SKUs and 34,836 customers were refused for a table neither
    # answer needs. pareto's full ranking goes to the result file, which pages.

    basis = share_basis(agg, [g[1] for g in grouped])
    if not basis.ok:
        raise ValueError(
            f"there is no share of {measure} by {dimension} to report: "
            f"{basis.reason}"
        )
    return agg, grouped, basis


@register(
    "pareto",
    tier=2,
    summary="How few groups of a declared dimension carry most of a declared "
            "measure: each group's share, the running share, and the smallest "
            "set reaching a threshold; with period, within one named period.",
    narrows=True,
)
def pareto(con, gate, scope, dimension: str, measure: str,
           threshold: float = DEFAULT_THRESHOLD, **params) -> Output:
    if params:
        raise TypeError(
            f"pareto takes dimension, measure, threshold, period and grain; got "
            f"{', '.join(sorted(params))}."
        )
    if not 0 < threshold <= 100:
        raise ParamsInvalid(
            f"threshold is a percentage above 0 and at most 100; got {threshold}."
        )

    summary = [scope.method_note(), *gate.caveats]
    headers = ["rank", dimension, f"{measure}", "share", "running share"]
    if not scope.analysed:
        return Output(headers=headers, rows=[], label="pareto",
                      summary=summary + [
                          "No rows are in scope, so there is nothing to rank."])

    agg, grouped, basis = _ranked_with_shares(
        con, gate, scope, dimension, measure)

    running = Decimal(0) if isinstance(basis.denominator, Decimal) else 0
    rows: list[list[Any]] = []
    reached = None
    for i, (value, total, _rows) in enumerate(grouped, start=1):
        running = running + (total if total is not None else 0)
        pct = running / basis.denominator * 100
        rows.append([number(i), label(value), number(total),
                     basis.share(total), f"{pct:.1f}%"])
        if reached is None and pct >= Decimal(str(threshold)):
            reached = i

    summary.append(
        f"The running share is of {number(basis.denominator)}, the {agg} of "
        f"{measure} across all {len(grouped):,} group(s), accumulated from the "
        f"totals rather than from the rounded shares."
    )
    if reached is not None:
        summary.append(
            f"{reached} of {len(grouped):,} group(s) reach {threshold:g}% -- "
            f"{reached / len(grouped) * 100:.1f}% of the groups carrying "
            f"{rows[reached - 1][4]} of the total."
        )
    else:
        summary.append(
            f"No set of groups reaches {threshold:g}%: the {len(grouped):,} "
            f"group(s) together come to {rows[-1][4]}, which is what a share "
            f"of a total with NULL group totals in it looks like."
        )
    tied = sum(1 for g in grouped if reached and g[1] == grouped[reached - 1][1])
    if reached and tied > 1:
        summary.append(
            f"{tied} group(s) share the total at the {threshold:g}% crossing, "
            f"so which of them is counted as the {reached}th is decided by the "
            f"tiebreak on the group name, not by the data."
        )
    return Output(headers=headers, rows=rows, summary=summary, label="pareto")


@register(
    "concentration",
    tier=2,
    summary="How much of a declared measure the largest groups of a declared "
            "dimension hold, at fixed cuts, with the Herfindahl-Hirschman "
            "Index and the effective number of groups; with period, within one "
            "named period.",
    narrows=True,
)
def concentration(con, gate, scope, dimension: str, measure: str, **params) -> Output:
    if params:
        raise TypeError(
            f"concentration takes dimension, measure, period and grain; got "
            f"{', '.join(sorted(params))}."
        )

    summary = [scope.method_note(), *gate.caveats]
    headers = ["largest groups", f"share of {measure}"]
    if not scope.analysed:
        return Output(headers=headers, rows=[], label="concentration",
                      summary=summary + [
                          "No rows are in scope, so there is nothing to "
                          "measure."])

    agg, grouped, basis = _ranked_with_shares(
        con, gate, scope, dimension, measure)
    n = len(grouped)

    shares = [(g[1] or 0) / basis.denominator for g in grouped]
    rows: list[list[Any]] = []
    running = Decimal(0) if isinstance(basis.denominator, Decimal) else 0
    cuts = [c for c in CUTS if c < n] + [n]
    percent = ({max(1, n * p // 100): p for p in PERCENT_CUTS} if n >= PERCENT_CUTS_FROM else {})
    for i, share in enumerate(shares, start=1):
        running = running + share
        if i in cuts:
            rows.append([f"top {i}" if i < n else f"all {n}",
                         f"{running * 100:.1f}%"])
        if i in percent:
            rows.append([f"top {percent[i]}% ({i:,})", f"{running * 100:.2f}%"])

    hhi = sum(float(s) ** 2 for s in shares) * 10_000
    effective = 10_000 / hhi if hhi else float("inf")
    summary.append(
        f"Shares are of {number(basis.denominator)}, the {agg} of {measure} "
        f"across all {n:,} group(s)."
    )
    # Two decimals below 100: the retail key's HHI of 1.35 printed as "1", and its floor over
    # 34,836 customers as "0" (Cleanup Step 14).
    def index(v: float) -> str:
        return f"{v:,.2f}" if v < 100 else f"{v:,.0f}"

    summary.append(
        f"HHI {index(hhi)} -- the sum of the squared percentage shares, between "
        f"{index(10_000 / n)} (every group equal) and 10,000 (one group holds "
        f"everything). It is reported as a number: the thresholds the index "
        f"carries in antitrust describe product markets, and {dimension} is "
        f"not necessarily one."
    )
    summary.append(
        f"Effective number of groups {effective:,.1f}, against {n:,} actual -- "
        f"the count of equal-sized groups that would give the same HHI."
    )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="concentration")
