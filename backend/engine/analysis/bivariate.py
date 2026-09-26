"""How one measure behaves across the range of another, in bins.

**This exists because a coefficient is one number and a relationship is not.**
Measured on the fixture: a symmetric U correlates at exactly 0.000, and the
same pairs binned read 16.25, 4.25, 0.25, 4.25, 16.25 -- as clean a
relationship as a dataset ever holds, invisible to Pearson and to Spearman
alike. `correlation` answers how straight and how ordered; this answers what
shape, and the two are meant to be read together.

**A bin is a range of values, not a slice of rows.** The obvious
implementation, `ntile` over the rows, splits ties: measured on ten rows with
three distinct values, ntile(4) puts x=1.0 in bin 1 *and* bin 2, and the bins
it produces have overlapping ranges. Two rows holding the same x belong in the
same bin under any reading of what a bin is. So the distinct values are binned
and every row joins onto its own value's bin, which makes the bins uneven in
row count by construction -- reported, because an uneven bin is a fact about
the column's distribution and not a defect in the binning.

**Both a mean and a median per bin.** They are the same number when a bin is
symmetric and they separate when it is not, so the gap between them is skew
inside the bin -- which is exactly what a bin summary otherwise hides.

**The turns are counted and the shape is never named.** The direction of travel
between consecutive bins is compared and the changes counted. Two turns is
reported as two turns, not as "U-shaped" or "quadratic": naming a curve is
fitting one, and nothing here is fitted.

The rulings this carries, measured on DuckDB 1.5.5 in Step 5b:

  P9-D42  ntile over rows splits tied values across bins and produces
          overlapping ranges; binning the distinct values instead does not.
  P9-D43  asking for more bins than there are distinct values yields fewer
          bins, silently, so the number produced is reported beside the number
          asked for.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, ParamsInvalid, number
from .declared import require_measure
from .registry import Output, register

__all__ = ["bivariate"]

DEFAULT_BINS = 10

# Two is the fewest that can have a direction at all. Fifty is where the table
# stops being a summary of a relationship and becomes the data again.
MIN_BINS = 2
MAX_BINS = 50


@register(
    "bivariate",
    tier=4,
    summary="How one declared measure behaves across the range of another, "
            "binned by value rather than by row, with a mean and a median per "
            "bin and the turns in the relationship counted.",
)
def bivariate(con, gate, scope, measure: str, against: str,
              bins: int = DEFAULT_BINS, **params) -> Output:
    if params:
        raise TypeError(
            f"bivariate takes measure, against and bins; got "
            f"{', '.join(sorted(params))}."
        )

    contract = gate.contract
    # agg is not consulted, for the reason correlation does not consult it
    # (P9-D41): nothing here is combined across rows in a way the contract has
    # a view on -- the bins summarise, they do not aggregate a measure.
    require_measure(contract, measure)
    require_measure(contract, against)
    if measure == against:
        raise ParamsInvalid(
            f"measure and against are both {measure!r}, so every bin would "
            f"report its own range back as its own mean. Name two measures."
        )
    try:
        wanted = int(bins)
    except (TypeError, ValueError):
        raise ParamsInvalid(f"bins must be a whole number; got {bins!r}.") from None
    if not MIN_BINS <= wanted <= MAX_BINS:
        raise ParamsInvalid(
            f"bins must be between {MIN_BINS} and {MAX_BINS}; got {wanted}. "
            f"Below {MIN_BINS} there is no direction to read, and above "
            f"{MAX_BINS} the table is the data rather than a summary of it."
        )

    table = scope.source
    x = quote_identifier(measure)
    y = quote_identifier(against)
    headers = ["bin", f"{measure} from", f"{measure} to", "rows",
               f"mean {against}", f"median {against}"]
    summary = [scope.method_note(), *gate.caveats]

    # P9-D42: the DISTINCT values are binned and every row joins onto its own
    # value's bin. ntile over the rows would put two rows holding the same x in
    # different bins and hand back ranges that overlap.
    fetched = con.execute(
        f"WITH p AS (SELECT {x} AS x, {y} AS y FROM {table} "
        f"WHERE {scope.where} AND {x} IS NOT NULL AND {y} IS NOT NULL), "
        f"d AS (SELECT DISTINCT x FROM p), "
        f"b AS (SELECT x, ntile({wanted}) OVER (ORDER BY x) AS bin FROM d) "
        f"SELECT b.bin, min(p.x), max(p.x), count(*), avg(p.y), median(p.y) "
        f"FROM p JOIN b ON b.x = p.x GROUP BY b.bin ORDER BY b.bin"
    ).fetchall()

    paired = sum(r[3] for r in fetched)
    in_scope = con.execute(
        f"SELECT count(*) FROM {table} WHERE {scope.where}"
    ).fetchall()[0][0]
    dropped = in_scope - paired
    if paired + dropped != scope.analysed:
        raise LostRows(
            f"bivariate lost rows: {paired:,} row(s) across {len(fetched)} "
            f"bin(s) and {dropped:,} missing a value, against "
            f"{scope.analysed:,} analysed."
        )

    if not fetched:
        summary.append(
            f"No row in scope holds both {measure} and {against}, so there is "
            f"nothing to bin. {dropped:,} row(s) are missing one or both."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="bivariate")

    rows: list[list[Any]] = [
        [number(r[0]), number(r[1]), number(r[2]), number(r[3]),
         number(r[4]), number(r[5])]
        for r in fetched
    ]

    summary.append(
        f"{paired:,} of {scope.analysed:,} analysed row(s) hold both "
        f"{measure} and {against}; {dropped:,} hold one or neither and are in "
        f"no bin above."
    )
    summary.append(
        f"The bins are ranges of {measure}, not slices of rows: the distinct "
        f"values were divided and every row joined onto its own value's bin, "
        f"so two rows sharing a {measure} always share a bin. Dividing the "
        f"rows instead would split them and hand back bins whose ranges "
        f"overlap."
    )

    counts = [r[3] for r in fetched]
    if len(fetched) < wanted:
        summary.append(
            f"{wanted} bin(s) were asked for and {len(fetched)} produced, "
            f"because {measure} takes fewer than {wanted} distinct values in "
            f"scope. Nothing was dropped -- there is simply no way to cut a "
            f"column into more ranges than it has values."
        )
    if min(counts) != max(counts):
        summary.append(
            f"Bins hold between {min(counts):,} and {max(counts):,} row(s). "
            f"Equal ranges of value are not equal counts of row, and the "
            f"spread is a fact about how {measure} is distributed rather than "
            f"a defect in the binning. A bin resting on {min(counts):,} row(s) "
            f"carries less than one resting on {max(counts):,}."
        )

    means = [float(r[4]) for r in fetched]
    if len(means) >= 3:
        steps = [b - a for a, b in zip(means, means[1:]) if b != a]
        turns = sum(1 for a, b in zip(steps, steps[1:]) if (a > 0) != (b > 0))
        if turns:
            summary.append(
                f"Mean {against} changes direction {turns:,} time(s) across "
                f"the bins, so no single coefficient describes this: "
                f"correlation asks whether one line fits and the answer here "
                f"is that {turns + 1:,} would be needed. The turns are counted "
                f"and the curve is not named -- naming one would be fitting "
                f"one, and nothing here is fitted."
            )
        elif steps:
            summary.append(
                f"Mean {against} moves in one direction across every bin, "
                f"{'upward' if steps[0] > 0 else 'downward'}. That is a "
                f"monotone relationship at this number of bins, which is not "
                f"the same as a straight one -- correlation is where "
                f"straightness is reported."
            )

    skewed = [r for r in fetched
              if r[5] and abs(float(r[4]) - float(r[5])) > abs(float(r[5])) / 10]
    if skewed:
        summary.append(
            f"{len(skewed):,} bin(s) have a mean more than a tenth away from "
            f"their median, so {against} is lopsided inside them and the mean "
            f"is being carried by the far end of the bin rather than by its "
            f"middle."
        )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="bivariate")
