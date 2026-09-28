"""The statistics a summary reports, and the one place they are spelled.

`summary_stats` computes seven numbers per measure over one undivided scope;
`group_compare` computes the same seven per group. Written twice they drift,
and C9 measured the exact way they would: `median` over a DECIMAL(18,2) column
returns Decimal('15.37') where `quantile_cont(CAST(col AS DOUBLE), 0.5)`
returns 15.375. A group median spelled the first way disagrees with the same
column's median one table up, and both are correct -- which is the failure
P8-D28 and P8-D46 are also shaped like.

So the expressions live here, in the order their cells appear, and both callers
interpolate them into their own GROUP BY (or absence of one). The declared
aggregate is NOT here: that is the contract's, and `declared.AGG_SQL` holds it.
"""

from __future__ import annotations

from typing import Any

from ..util.formatting import MAX_ROWS
from .base import number

__all__ = ["STAT_HEADERS", "MAX_GROUPS", "stat_exprs", "stat_cells",
           "ranked_totals"]

# The cells these expressions produce, in order.
STAT_HEADERS = ["n", "nulls", "min", "max", "mean", "median", "stddev"]

# A table of groups carries one more row than it has groups -- the (all) row in
# group_compare, the (total) row in cross_tab -- so the cap is one below the
# table limit. P8-D48: the limit itself lives in util/formatting.py.
MAX_GROUPS = MAX_ROWS - 1


# P10-O2: a DOUBLE column can hold NaN and infinity, and IS NOT NULL does not exclude them.
# P10-D5 measured what a nan does downstream -- it propagates silently and base.number renders
# it into a cell as the text "nan", which LostRows cannot catch because the row count is right.
# stddev over an infinity does worse: DuckDB raises "STDDEV_SAMP is out of range", and
# summary_stats, group_compare, outlier_detection and profile_dataset answered with an exception
# (P14-O6, B4). The cast is what makes one spelling work on DECIMAL and INTEGER columns.
FINITE = "NOT isnan(CAST({col} AS DOUBLE)) AND NOT isinf(CAST({col} AS DOUBLE))"


def can_be_non_finite(dtype: str) -> bool:
    """Only floating-point columns hold NaN or Infinity; an integer or DECIMAL never does.

    The filters below are applied to those alone. Measured on 200 columns x 300 rows: the plain
    aggregates took 0.07 s, the same with a FINITE filter on each 10.06 s, with isfinite() 0.47 s
    -- a profile of a wide table went from 4 s to 24 s before this guard.
    """
    return dtype.split("(")[0].strip().upper() in ("DOUBLE", "FLOAT", "REAL")


def finite_only(col: str, floating: bool = True) -> str:
    """A FILTER clause keeping the finite values of an already-quoted column, or nothing."""
    return f"FILTER (WHERE isfinite({col}))" if floating else ""


def non_finite_count(col: str, floating: bool = True) -> str:
    """How many values of an already-quoted column are NaN or infinite (0 when it cannot be)."""
    return f"count({col}) FILTER (WHERE NOT isfinite({col}))" if floating else "0"


def non_finite_note(n: int, measure: str) -> str:
    return (f"{n:,} value(s) of {measure} are not a finite number (NaN or Infinity) and were "
            f"set aside from every statistic of it; they still count as present in n. "
            f"propose_cleaning_plan offers to turn them into nulls.")


def stat_exprs(col: str, numeric: bool, floating: bool = False) -> list[str]:
    """The seven SQL expressions, for an already-quoted column.

    Non-numeric measures get NULL for the derived three rather than being
    refused: a date measure with agg=min is a legitimate declaration, its
    counts and extremes are true, and its average is not. The blank cells say
    which parts do not apply.
    """
    if numeric:
        keep = finite_only(col, floating)
        return [
            "count(*)", f"count({col})", f"min({col}) {keep}", f"max({col}) {keep}",
            f"avg({col}) {keep}",
            f"quantile_cont(CAST({col} AS DOUBLE), 0.5) {keep}",
            f"stddev({col}) {keep}",
        ]
    return ["count(*)", f"count({col})", f"min({col})", f"max({col})", "NULL", "NULL", "NULL"]


def stat_cells(raw) -> list[Any]:
    """The seven cells, from the seven values `stat_exprs` selected.

    `nulls` is computed rather than selected: count(*) minus count(col) is the
    number of analysed rows in this group with no value, and asking SQL for it
    separately is a second chance to disagree.
    """
    rows, present, lo, hi, mean, median, spread = raw
    return [
        number(present), number(rows - present),
        number(lo), number(hi), number(mean), number(median), number(spread),
    ]


def ranked_totals(con, scope, dimension: str, total_sql: str,
                  extra: str | None = None):
    """(group, total, rows) per group, biggest first, deterministically.

    P8-D4: the tiebreak is on the group name, so the same data gives the same
    answer twice -- it does not make that answer the only correct one, which is
    what top_n's tie note is for. NULLS LAST keeps a group whose total is NULL
    below every group that has one.

    top_n, pareto and concentration order groups identically. Spelled three
    times they drift, which is P8-D50's argument one module over.

    `extra` narrows WITHIN the scope rather than replacing it -- ranking_shift
    passes one period at a time. The scope's own predicate is never dropped, so
    the method note printed above a table stays true of the rows beneath it.
    """
    from ..util.sql_guard import quote_identifier

    dim = quote_identifier(dimension)
    table = scope.source
    where = scope.where if extra is None else f"({scope.where}) AND ({extra})"
    return con.execute(
        f"SELECT {dim}, {total_sql}, count(*) FROM {table} WHERE {where} "
        f"GROUP BY 1 ORDER BY 2 DESC NULLS LAST, 1 ASC"
    ).fetchall()
