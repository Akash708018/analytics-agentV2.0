"""Two declared measures against each other, linear and by rank, side by side.

**Both coefficients, always, and the gap between them named.** Pearson is the
linear one and one far point moves it; Spearman is over the ranks and a far
point is just the last rank. Reporting one means choosing which question was
asked on the reader's behalf. Reporting both means the disagreement between
them becomes the finding: measured on the fixture, Pearson 0.749 against
Spearman 1.000 is a perfectly ordered relationship with a single outlier
dragging the line, and neither number says that on its own.

**A correlation is over pairs, not over rows.** A row missing either value is
not a pair and does not enter either coefficient -- DuckDB's `corr` drops it
silently, so the count that matters is `regr_count`, not the row count of the
scope. The difference is reported rather than left as a gap between two numbers
the reader would have to notice.

**An aggregate is irrelevant here, so `agg='none'` is welcome.** This is the
inversion of Tier 3's rule: `trend` refuses a measure that declares `agg='none'`
because a value per period would have to be invented, but a correlation reads
the raw values and never combines them. A unit price that cannot be summed can
be correlated, and refusing it here would be inheriting a constraint that does
not apply.

**Nothing is called strong or weak.** What counts as a strong coefficient is a
question about the domain and the cost of being wrong, not about the number.
The analysis reports the number, the pair count and the disagreement, and stops.

The rulings this carries, measured on DuckDB 1.5.5 in Step 5a:

  P9-D38  a constant column makes both coefficients undefined, and DuckDB
          returns nan rather than NULL. Unhandled, nan reaches a cell as the
          string "nan" and reads as a value.
  P9-D39  n is regr_count, the pairwise-complete count, and it is reported
          beside every coefficient.
  P9-D40  below three pairs no coefficient is reported at all: any two points
          lie on a line, so r is +/-1 by construction and says nothing.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, ParamsInvalid, number
from .declared import require_measure
from .registry import Output, register

__all__ = ["correlation"]

# Below this many pairs the coefficients are arithmetic rather than evidence:
# two points always lie on a line, so Pearson is +/-1 whatever the data says.
MIN_PAIRS = 3

# How far Pearson and Spearman have to sit apart before the gap is called out
# as the finding rather than as rounding. Not a threshold for significance --
# there is none here -- just the point where the two are answering visibly
# different questions.
GAP = 0.1


def _defined(value) -> float | None:
    """None for a missing coefficient, including DuckDB's nan.

    P9-D38: a constant column gives nan, not NULL, and nan is neither caught by
    `is None` nor equal to itself. It reaches a cell as the string "nan" unless
    it is turned back into an absence here.
    """
    if value is None or value != value:
        return None
    return float(value)


@register(
    "correlation",
    tier=4,
    summary="Two declared measures against each other, Pearson and Spearman "
            "side by side over the rows holding both values, with the pair "
            "count and the disagreement between the two coefficients named.",
)
def correlation(con, gate, scope, measure: str, against: str, **params) -> Output:
    if params:
        raise TypeError(
            f"correlation takes measure and against; got "
            f"{', '.join(sorted(params))}."
        )

    contract = gate.contract
    # Both sides must be declared. agg is deliberately not consulted: this
    # analysis never combines values across rows, so a measure declaring
    # agg='none' is as correlatable as any other.
    require_measure(contract, measure)
    require_measure(contract, against)
    if measure == against:
        raise ParamsInvalid(
            f"measure and against are both {measure!r}. A column correlates "
            f"with itself at 1.000 by construction, which is a fact about "
            f"arithmetic and not about {contract.dataset_name}."
        )

    table = scope.source
    x = quote_identifier(measure)
    y = quote_identifier(against)
    headers = [measure, against, "pairs", "pearson r", "spearman rho",
               "rows without both"]
    summary = [scope.method_note(), *gate.caveats]

    # Spearman is Pearson over average ranks. The average matters: row_number
    # alone breaks ties arbitrarily, so tied values would get different ranks
    # and the coefficient would depend on the order rows came back in.
    row = con.execute(
        f"WITH p AS (SELECT {x} AS x, {y} AS y FROM {table} "
        f"WHERE {scope.where} AND {x} IS NOT NULL AND {y} IS NOT NULL), "
        f"n AS (SELECT row_number() OVER (ORDER BY x) AS rx, "
        f"row_number() OVER (ORDER BY y) AS ry, x, y FROM p), "
        f"r AS (SELECT avg(rx) OVER (PARTITION BY x) AS ax, "
        f"avg(ry) OVER (PARTITION BY y) AS ay FROM n) "
        f"SELECT (SELECT count(*) FROM p), "
        f"(SELECT corr(y, x) FROM p), (SELECT corr(ay, ax) FROM r), "
        f"(SELECT count(*) FROM {table} WHERE {scope.where}), "
        f"(SELECT count(DISTINCT x) FROM p), (SELECT count(DISTINCT y) FROM p)"
    ).fetchall()[0]

    pairs, pearson, spearman, in_scope, distinct_x, distinct_y = row
    pearson, spearman = _defined(pearson), _defined(spearman)
    dropped = in_scope - pairs

    if pairs + dropped != scope.analysed:
        raise LostRows(
            f"correlation lost rows: {pairs:,} pair(s) and {dropped:,} row(s) "
            f"missing a value, against {scope.analysed:,} analysed."
        )

    # Whether a coefficient may be shown is decided before the row is built,
    # not after. A summary saying no coefficient is reported, above a cell
    # holding +1.000, is worse than either on its own.
    flat = [name for name, distinct in ((measure, distinct_x),
                                        (against, distinct_y)) if distinct < 2]
    reportable = pairs >= MIN_PAIRS and not flat
    rows: list[list[Any]] = [[
        measure, against, number(pairs),
        f"{pearson:+.3f}" if reportable and pearson is not None else "",
        f"{spearman:+.3f}" if reportable and spearman is not None else "",
        number(dropped),
    ]]

    summary.append(
        f"{pairs:,} of {scope.analysed:,} analysed row(s) hold both "
        f"{measure} and {against}; {dropped:,} hold one or neither and are "
        f"in no coefficient above. A correlation is over pairs, and a row "
        f"missing either value is not a pair."
    )

    if pairs < MIN_PAIRS:
        summary.append(
            f"{pairs:,} pair(s) is below {MIN_PAIRS}, so no coefficient is "
            f"reported. Any two points lie on a line, which makes Pearson "
            f"+1.000 or -1.000 whatever the two rows happen to say."
        )
        return Output(headers=headers, rows=rows, summary=summary,
                      label="correlation")

    if flat:
        summary.append(
            f"{' and '.join(flat)} takes one value across every pair, so both "
            f"coefficients are undefined rather than zero: a column that does "
            f"not vary cannot vary with anything. The cells are blank for that "
            f"reason and not because the computation failed."
        )
        return Output(headers=headers, rows=rows, summary=summary,
                      label="correlation")

    summary.append(
        f"Pearson r is {pearson:+.3f} over the values and Spearman rho is "
        f"{spearman:+.3f} over their ranks, both across the same {pairs:,} "
        f"pairs."
    )
    gap = abs(pearson - spearman)
    if gap >= GAP:
        summary.append(
            f"The two disagree by {gap:.3f}, and the disagreement is the "
            f"finding rather than a rounding artefact. Spearman only asks "
            f"whether the order holds and Pearson asks whether a straight line "
            f"fits, so a rank coefficient well above the linear one is an "
            f"ordered relationship with a far point pulling the line -- and a "
            f"linear one well above the rank coefficient is usually a handful "
            f"of extreme pairs carrying it."
        )
    else:
        summary.append(
            f"The two agree to within {gap:.3f}, so this relationship is about "
            f"as straight as it is ordered and neither coefficient is resting "
            f"on a small number of extreme pairs."
        )
    summary.append(
        f"Neither number is called strong or weak here. What counts as strong "
        f"is a question about {contract.dataset_name} and the cost of being "
        f"wrong about it, not a property of the coefficient. Nor does either "
        f"say that {measure} moves {against}: a third column driving both "
        f"produces these same two numbers."
    )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="correlation")
