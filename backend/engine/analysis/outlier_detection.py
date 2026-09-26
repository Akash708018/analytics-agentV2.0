"""Which points are unusual, by three methods that disagree on purpose.

**Picking one method silently answers a question the reader did not ask.**
`correlation` reports Pearson beside Spearman for this reason; the same holds
harder here, because the methods do not merely differ in emphasis -- they
return different sets of rows. All three are computed and reported together,
and their disagreement is the finding rather than an inconvenience.

**The z-score is computed from statistics the outliers themselves inflate.**
Measured on the fixture: ten values from 1 to 10 plus 1000 and 1010 give a
standard deviation of 389.07, so three deviations reach past 1339 and nothing
is flagged at all. IQR flags both extremes and MAD flags both. This is masking,
it is not a corner case, and two outliers are enough to produce it -- so
whenever the z-score flags fewer rows than the other two, the summary says why.

**A MAD of zero is undefined, not infinitely tight.** Measured: when more than
half the values are identical the median absolute deviation is exactly 0.0, and
the robust bound becomes the median itself. Reading that as "every unequal value
is an outlier" is arithmetic rather than a finding, so the MAD row reports no
bounds and says the column is too concentrated for the method.

**Nothing is removed and nothing is recommended for removal.** An outlier is a
question about a row, not a defect in it. The analysis reports how many rows
each method flags and where its boundaries fall; which rows deserve attention
is a matter about the data's subject, which this module knows nothing about.

The rulings this carries, measured on DuckDB 1.5.5 in Step 6a:

  P9-D52  three methods, always, reported side by side with their bounds.
  P9-D53  the z-score masks: measured, two extremes take it from flagging both
          to flagging neither.
  P9-D54  a MAD of exactly 0.0 leaves the robust method without a scale.
"""

from __future__ import annotations

from typing import Any

from ..util.sql_guard import quote_identifier
from .base import LostRows, TooManyGroups, label, number
from .declared import require_dimension, require_measure
from .stats import MAX_GROUPS
from .registry import Output, register
from .declared import column_types
from .stats import can_be_non_finite, non_finite_count, non_finite_note

__all__ = ["outlier_detection"]

# Tukey's fence, and three deviations for both the classical and the robust
# score. Conventional rather than derived, which is exactly why they are
# constants with names instead of numbers inside an expression.
IQR_FENCE = 1.5
DEVIATIONS = 3.0

# The constant that puts a median absolute deviation on the same scale as a
# standard deviation for normally distributed data. It is an assumption about
# the distribution, and it is the only one in this module.
MAD_TO_SIGMA = 1.4826

# Fewest rows for any of this to mean anything: a quartile needs a distribution
# to sit in.
MIN_ROWS = 8


def _bound(value: float) -> str:
    """A fence, rendered at this module's precision rather than the column's.

    P9-D55: quantile_cont over DECIMAL(5,1) gives 3.7 where the same values as
    DOUBLE give 3.75, so a bound already moves with the column's storage type.
    Letting its rendering move too would put two kinds of drift in one cell.
    Four places, trailing zeros dropped.
    """
    return f"{value:,.4f}".rstrip("0").rstrip(".")


@register(
    "outlier_detection",
    tier=5,
    summary="Unusual values in one declared measure by three methods at once "
            "-- Tukey's fence, the z-score and the median absolute deviation "
            "-- with their bounds, their disagreement, and the masking that "
            "makes the z-score flag fewer than the others; with dimension, "
            "within each of its groups.",
)
def outlier_detection(con, gate, scope, measure: str, dimension: str | None = None,
                      **params) -> Output:
    if params:
        raise TypeError(
            f"outlier_detection takes measure and dimension; got {', '.join(sorted(params))}."
        )

    contract = gate.contract
    # agg is not consulted (P9-D41): every statistic here is over raw values.
    require_measure(contract, measure)
    if dimension is not None:
        require_dimension(contract, dimension)
        return _within(con, gate, scope, measure, dimension)

    table = scope.source
    x = quote_identifier(measure)
    headers = ["method", "lower bound", "upper bound", "flagged",
               "share of rows"]
    summary = [scope.method_note(), *gate.caveats]

    # Finite values only: an infinity has no fence to sit outside of, and stddev over one raises
    # (P14-O6). Set aside and counted, like a missing value.
    floating = can_be_non_finite(column_types(con, scope.dataset_name).get(measure, ""))
    finite = f"isfinite({x})" if floating else "TRUE"
    bad = con.execute(f"SELECT {non_finite_count(x, floating)} FROM {table} "
                      f"WHERE {scope.where}").fetchall()[0][0]
    if bad:
        summary.append(non_finite_note(bad, measure))
    q1, q3, mean, sd, med, n = con.execute(
        f"SELECT quantile_cont({x}, 0.25), quantile_cont({x}, 0.75), "
        f"avg({x}), stddev_samp({x}), median({x}), count({x}) "
        f"FROM {table} WHERE {scope.where} AND {x} IS NOT NULL AND {finite}"
    ).fetchall()[0]
    missing = scope.analysed - n
    if n + missing != scope.analysed:
        raise LostRows(
            f"outlier_detection lost rows: {n:,} with a {measure} and "
            f"{missing:,} without, against {scope.analysed:,} analysed."
        )

    summary.append(
        f"{n:,} of {scope.analysed:,} analysed row(s) hold a finite {measure}; "
        f"{missing:,} do not and are in no method below."
    )
    if n < MIN_ROWS:
        summary.append(
            f"{n:,} value(s) is below {MIN_ROWS}, so no method is reported. A "
            f"quartile needs a distribution to sit in, and at this count every "
            f"bound would be decided by one or two rows."
        )
        return Output(headers=headers, rows=[], summary=summary,
                      label="outlier_detection")

    mad = con.execute(
        f"SELECT median(abs({x} - ?)) FROM {table} "
        f"WHERE {scope.where} AND {x} IS NOT NULL AND {finite}",
        [med],
    ).fetchall()[0][0]

    iqr = float(q3) - float(q1)
    bounds: dict[str, tuple[float, float] | None] = {
        "Tukey's fence": (float(q1) - IQR_FENCE * iqr,
                          float(q3) + IQR_FENCE * iqr),
        "z-score": ((float(mean) - DEVIATIONS * float(sd),
                     float(mean) + DEVIATIONS * float(sd)) if sd else None),
        "median absolute deviation": (
            (float(med) - DEVIATIONS * MAD_TO_SIGMA * float(mad),
             float(med) + DEVIATIONS * MAD_TO_SIGMA * float(mad))
            if mad else None),
    }

    flags: dict[str, int] = {}
    for method, pair in bounds.items():
        if pair is None:
            flags[method] = 0
            continue
        flags[method] = con.execute(
            f"SELECT count(*) FROM {table} WHERE {scope.where} "
            f"AND {x} IS NOT NULL AND {finite} AND ({x} < ? OR {x} > ?)",
            [pair[0], pair[1]],
        ).fetchall()[0][0]

    live = [p for p in bounds.values() if p is not None]
    tests = " + ".join(
        f"CASE WHEN {x} < ? OR {x} > ? THEN 1 ELSE 0 END" for _ in live)
    args: list[float] = [v for p in live for v in p]
    # Counted at every level, not only at "all" and "exactly one". Measured on
    # the fixture, both extremes are flagged by two of the three methods, and a
    # summary reporting only those two ends says 0 and 0 and never mentions
    # them -- which is the most interesting number in the table.
    spread = con.execute(
        f"WITH k AS (SELECT ({tests}) AS c FROM {table} "
        f"WHERE {scope.where} AND {x} IS NOT NULL AND {finite}) "
        f"SELECT c, count(*) FROM k WHERE c > 0 GROUP BY c ORDER BY c",
        args,
    ).fetchall()
    any_flag = sum(k for _, k in spread)

    rows: list[list[Any]] = []
    for method, pair in bounds.items():
        if pair is None:
            rows.append([method, "", "", "", ""])
            continue
        rows.append([
            method, _bound(pair[0]), _bound(pair[1]), number(flags[method]),
            f"{flags[method] / n * 100:.1f}%",
        ])

    named = ", ".join(
        f"{method} {flags[method]:,}" for method, pair in bounds.items()
        if pair is not None)
    summary.append(
        f"Flagged: {named}. The methods are reported together because they "
        f"return different rows, not different opinions about the same rows -- "
        f"choosing one of them upstream would answer a question nobody asked."
    )
    if len(live) > 1 and any_flag:
        split = ", ".join(f"{k:,} by {c} of the {len(live)}" for c, k in spread)
        summary.append(
            f"{any_flag:,} value(s) are flagged by at least one method: "
            f"{split}. A value every method flags survives the choice of "
            f"method; a value only one flags is the choice of method showing "
            f"itself; and anything in between is the methods disagreeing about "
            f"a row they can all see."
        )

    z_pair = bounds["z-score"]
    others = [flags[m] for m, p in bounds.items()
              if m != "z-score" and p is not None]
    if z_pair is not None and others and flags["z-score"] < min(others):
        summary.append(
            f"The z-score flags fewer than the others, which is what masking "
            f"looks like from the inside: its bounds are built from a mean of "
            f"{number(mean)} and a standard deviation of {number(sd)}, and the "
            f"extreme values are in both. The further out a point sits, the "
            f"more it widens the fence meant to catch it. Tukey's fence and "
            f"the median absolute deviation are built from quantiles, which a "
            f"handful of extreme values cannot move."
        )
    if bounds["median absolute deviation"] is None:
        summary.append(
            f"The median absolute deviation of {measure} is exactly 0.0, so "
            f"more than half its values sit on the median and the robust "
            f"method has no scale to work with. That is reported as no bounds "
            f"rather than as bounds of zero width -- the latter would flag "
            f"every value that differs from the median at all, which is "
            f"arithmetic and not a finding."
        )
    if bounds["z-score"] is None:
        summary.append(
            f"{measure} has no spread at all, so the z-score has no "
            f"denominator and no bounds."
        )
    summary.append(
        f"Nothing above is removed and nothing is recommended for removal. A "
        f"flagged row is a question about that row, and whether it is an error "
        f"or the most interesting thing in {scope.dataset_name} is a matter "
        f"this analysis has no access to."
    )
    return Output(headers=headers, rows=rows, summary=summary,
                  label="outlier_detection")


def _within(con, gate, scope, measure: str, dimension: str) -> Output:
    """The three methods' bounds within each group of `dimension` (Cleanup Step 14, G1).

    One fence across groups that differ in scale flags the dear group's ordinary values: the retail
    run's global Tukey fence flagged 32,467 unit prices, mostly Electronics against Grocery, while
    the 25 planted ones sat inside their own category's spread. Within a group the question is the
    one a person means -- unusual for what it is. A group under MIN_ROWS values gets no bounds.
    """
    table = scope.source
    x, d = quote_identifier(measure), quote_identifier(dimension)
    base = f"SELECT {d} AS g, {x} AS v FROM {table} WHERE {scope.where} AND {x} IS NOT NULL"
    n_groups = con.execute(f"SELECT count(DISTINCT g) + max(CASE WHEN g IS NULL THEN 1 ELSE 0 END) "
                           f"FROM ({base})").fetchone()[0] or 0
    if n_groups > MAX_GROUPS:
        raise TooManyGroups(
            f"outlier_detection within {dimension} would be {n_groups} group(s), against a cap of "
            f"{MAX_GROUPS}. top_n on {dimension} says which of its groups matter.",
            dimension, measure)
    rows_sql = (
        f"WITH s AS ({base}), "
        f"st AS (SELECT g, count(*) AS n, quantile_cont(v, 0.25) AS q1, "
        f"quantile_cont(v, 0.75) AS q3, avg(v) AS m, stddev_samp(v) AS sd, median(v) AS med "
        f"FROM s GROUP BY g), "
        f"md AS (SELECT s.g, median(abs(s.v - st.med)) AS mad FROM s JOIN st "
        f"ON s.g IS NOT DISTINCT FROM st.g GROUP BY s.g), "
        f"b AS (SELECT st.*, md.mad, st.q1 - {IQR_FENCE} * (st.q3 - st.q1) AS tlo, "
        f"st.q3 + {IQR_FENCE} * (st.q3 - st.q1) AS thi FROM st JOIN md "
        f"ON st.g IS NOT DISTINCT FROM md.g) "
        f"SELECT b.g, b.n, b.tlo, b.thi, "
        f"count(*) FILTER (WHERE s.v < b.tlo OR s.v > b.thi), "
        f"count(*) FILTER (WHERE b.sd > 0 AND abs(s.v - b.m) > {DEVIATIONS} * b.sd), "
        f"count(*) FILTER (WHERE b.mad > 0 AND abs(s.v - b.med) > "
        f"{DEVIATIONS} * {MAD_TO_SIGMA} * b.mad) "
        f"FROM s JOIN b ON s.g IS NOT DISTINCT FROM b.g "
        f"GROUP BY b.g, b.n, b.tlo, b.thi ORDER BY b.g NULLS LAST"
    )
    fetched = con.execute(rows_sql).fetchall()
    held = sum(r[1] for r in fetched)
    missing = scope.analysed - held
    headers = ["group", "values", "Tukey lower", "Tukey upper", "Tukey flagged",
               "z-score flagged", "MAD flagged"]
    rows: list[list[Any]] = []
    totals = [0, 0, 0]
    for g, n, tlo, thi, tk, z, md in fetched:
        if n < MIN_ROWS:
            rows.append([label(g), number(n), "", "", "", "", ""])
            continue
        totals = [totals[0] + tk, totals[1] + z, totals[2] + md]
        rows.append([label(g), number(n), _bound(float(tlo)), _bound(float(thi)),
                     number(tk), number(z), number(md)])
    q1, q3 = con.execute(f"SELECT quantile_cont(v, 0.25), quantile_cont(v, 0.75) FROM ({base})"
                         ).fetchone()
    global_tukey = 0
    if q1 is not None:
        iqr = float(q3) - float(q1)
        global_tukey = con.execute(
            f"SELECT count(*) FROM ({base}) WHERE v < ? OR v > ?",
            [float(q1) - IQR_FENCE * iqr, float(q3) + IQR_FENCE * iqr]).fetchone()[0]
    summary = [scope.method_note(), *gate.caveats]
    summary.append(
        f"{held:,} of {scope.analysed:,} analysed row(s) hold a {measure}, in {len(fetched)} "
        f"group(s) of {dimension}; {missing:,} do not. A group of fewer than {MIN_ROWS} values "
        f"gets no bounds.")
    summary.append(
        f"Flagged within {dimension}: Tukey's fence {totals[0]:,}, z-score {totals[1]:,}, median "
        f"absolute deviation {totals[2]:,}. One Tukey fence across all groups at once flags "
        f"{global_tukey:,} -- the difference is the spread between groups, which a single fence "
        f"reads as unusual values.")
    summary.append(
        f"Nothing above is removed and nothing is recommended for removal. A flagged row is "
        f"unusual for its own {dimension}, which is a question about that row and not a verdict.")
    return Output(headers=headers, rows=rows, summary=summary, label="outlier_detection")
