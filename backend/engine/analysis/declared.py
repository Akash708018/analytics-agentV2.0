"""What a contract declares, and how its aggregates are spelled in SQL.

These helpers inspect declarations and column types. They do not select a
scope, write results, or translate engine errors.
"""

from __future__ import annotations

__all__ = [
    "AGG_SQL", "agg_of", "column_types", "is_numeric", "is_integer",
    "is_arbitrary_precision",
    "dimension_names", "require_dimension", "require_measure",
    "ADDITIVE_AGGS", "adds_across_groups",
]

# What a declared aggregate becomes in SQL. `none` is deliberately absent: it
# is not an aggregate that produces nothing, it is a statement that no total is
# meaningful, and mapping it to anything at all would produce that total.
# An absent aggregate has no default: the contract must say how rows combine.
AGG_SQL: dict[str, str] = {
    "sum": "sum({col})",
    "mean": "avg({col})",
    "min": "min({col})",
    "max": "max({col})",
    "count": "count({col})",
    "count_distinct": "count(DISTINCT {col})",
    "median": "quantile_cont(CAST({col} AS DOUBLE), 0.5)",
    # A ratio of sums (Cleanup Step 15): the measure is STRUCT(n, d) in the scope's relation,
    # its parts summed apart and then divided -- never a mean of per-row ratios.
    "ratio": "(sum({col}.n) / nullif(sum({col}.d), 0))",
}


# Which aggregates add across disjoint groups, so that the group totals sum to
# a total the whole table shares. sum and count do: every analysed row lands in
# exactly one group and is counted once. count_distinct does not -- two groups
# can each hold the same distinct value and each count it once, so the group
# counts sum past the table's own distinct count unless the groups happen to
# partition by that value, which no contract states. mean, median, min and max
# do not: min and max recombine, but there is no total for a group to be part
# of, and a share of one means nothing.
ADDITIVE_AGGS = ("sum", "count")


def adds_across_groups(agg: str | None) -> bool:
    """Whether group totals of this aggregate sum to a table-wide total.

    P8-O16: top_n gave a share only for agg='sum' and told a count ranking that
    count "does not add up across groups", which is false of it -- the counts
    add to the analysed rows. This is the one place that answers the question;
    pareto and concentration ask it too (Step 8c).
    """
    return agg in ADDITIVE_AGGS


def agg_of(measure) -> str | None:
    """Read the aggregate declared by a measure.

    Aggregation is the Literal at dataset_contract.py:60. A contract can only
    produce a lowercase string or None; no enum value needs unwrapping.
    """
    agg = getattr(measure, "agg", None)
    if agg is None:
        return None
    return str(agg).strip().lower()


def column_types(con, dataset_name: str) -> dict[str, str]:
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [dataset_name],
    ).fetchall()
    return {r[0]: r[1] for r in rows}


INTEGER_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
)


def is_integer(dtype: str) -> bool:
    """Whether this is a scalar integer type, including unsigned HUGEINT."""
    return dtype.strip().upper() in INTEGER_TYPES


NUMERIC_TYPES = INTEGER_TYPES + ("FLOAT", "DOUBLE", "DECIMAL")

# DuckDB writes a declared VARINT back as BIGNUM: an integer with no fixed width.
# It is named so a caller can say what it is; nothing computes over it (S7b-r1).
ARBITRARY_PRECISION_TYPES = ("BIGNUM",)


def _base_type(name: str) -> str:
    """The type name without a trailing (...): DECIMAL(10,2) -> DECIMAL.

    A list type ends in ] and is returned whole, so DECIMAL(18,2)[] stays
    DECIMAL(18,2)[] and cannot be mistaken for DECIMAL.
    """
    if name.endswith(")") and "(" in name:
        return name[: name.index("(")]
    return name


def is_numeric(dtype: str) -> bool:
    """Whether DuckDB's name for a column type is a scalar number.

    The whole base name is compared, not its first letters: a prefix match
    read INTEGER[] and DECIMAL(18,2)[] as numbers and missed UHUGEINT
    (P8-O8). A list of numbers is not a number. BIGNUM is not numeric here;
    is_arbitrary_precision names it.
    """
    name = dtype.upper()
    return "[" not in name and _base_type(name) in NUMERIC_TYPES


def is_arbitrary_precision(dtype: str) -> bool:
    """Whether this is DuckDB's arbitrary-precision integer (declared VARINT)."""
    name = dtype.upper()
    return "[" not in name and _base_type(name) in ARBITRARY_PRECISION_TYPES


def dimension_names(contract) -> list[str]:
    """The declared dimensions, whether they are strings or objects.

    `dimensions` may hold names or small models; both are read the same way
    rather than one being assumed.
    """
    out = []
    for d in getattr(contract, "dimensions", []) or []:
        out.append(str(getattr(d, "name", d)))
    return out


def require_dimension(contract, column: str) -> str:
    """P8-O1 again: the contract says which columns are dimensions.

    profile_dataset already shows the top values of every column without a
    contract. This one answers the narrower question, so a column nobody
    declared is refused with the list of the ones somebody did -- a caller who
    wanted it is one confirm_dataset_contract away.
    """
    if column in set(contract.excluded_columns):
        raise ValueError(
            f"{column!r} is in excluded_columns: the contract says never to "
            f"read it."
        )
    declared = dimension_names(contract)
    if column in declared:
        return column
    raise ValueError(
        f"{column!r} is not a declared dimension of "
        f"{contract.dataset_name}. Declared: "
        f"{', '.join(declared) if declared else '(none)'}. "
        f"profile_dataset describes any column without a contract; this "
        f"analysis reports the ones the contract names."
    )


def require_measure(contract, name: str):
    """Return the declared measure itself, refusing excluded columns first."""
    if name in set(contract.excluded_columns):
        raise ValueError(
            f"{name!r} is in excluded_columns: the contract says never to "
            f"read it."
        )
    declared = {m.name: m for m in contract.measures}
    if name not in declared:
        raise ValueError(
            f"{name!r} is not a declared measure of {contract.dataset_name}. "
            f"Declared: {', '.join(sorted(declared)) or '(none)'}."
        )
    return declared[name]
