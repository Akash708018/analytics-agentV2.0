"""Which rows an analysis may see, and the sentence that says so.

Nine analyses arrive in Steps 5-8 and none of them decides what "the data"
means. `analysis_window`, `known_exclusions` and `excluded_columns` are the
contract's, so one module reads them and the nine read this. Nine
implementations of a WHERE clause would disagree eventually, which is P7-D6's
shape with nine copies instead of two.

**The counts are the reason this is a module and not a helper.** P8-D6 asked
for four numbers that sum to the row count, and the obvious way to write them
double-counts. A row with no date, kept by an exclusion that could not judge
it, lands in `outside_window` AND in `no_date` if the window count is written
`NOT coalesce((window), false)` -- the coalesce turns "cannot tell" into
"outside". `NOT (window)` leaves it NULL, FILTER does not count a NULL
predicate, and the row appears once, in the bucket named after why it could not
be judged. Measured on a seven-row fixture before this file existed: 8, then 7.

So a null-safe predicate and a null-safe count want opposite treatments of
NULL. The predicate has to decide; the count has to refuse to.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from ..util.sql_guard import bind_predicate, negate, quote_identifier
from .declared import adds_across_groups

__all__ = ["Scope", "ScopeError", "LostRows", "ParamsInvalid", "number", "label", "window_clause",
           "scope_for", "ShareBasis", "share_basis", "NO_MEMBER", "RECONCILE_TOLERANCE"]


# What a NULL in a dimension is called, wherever one is shown. It is a member like
# any other: the rows exist and the column does not say which member they belong to,
# which is a fact about those rows and not a gap. Three modules declared this
# separately until 21/09/2026 -- driver_analysis, growth_decomposition and mix_shift
# -- which is P9-O6's failure in a third column. P9-O8 asked which of two spellings
# for a null group wins: this one, because "(no region)" says which column was empty
# and "(no group)" does not. Callers format it with the dimension name.
NO_MEMBER = "(no {dimension})"


# The residual a decomposition may carry and still be called one. Exact equality is right when
# the measure is DECIMAL or an integer and wrong the moment it is DOUBLE: measured 21/09/2026,
# sum(revenue) over a 500-row CSV fixture is 1377896.8399999999, and a decomposition of it that
# reconciles to the last bit does not exist. growth_decomposition compared exactly and refused
# every float measure with a message printing both sides at four decimal places, so the two
# numbers it showed as unequal were identical on the page. Relative, because the absolute size
# of an acceptable residual depends on the size of the change.
RECONCILE_TOLERANCE = 1e-9


# Four decimal places, thousands separated, trailing zeros dropped. Measured in
# Step 5: avg of a DECIMAL(18,2) money column cast straight to text gives
# 23.583333333333332, and format_table renders cells with str(), so a number
# arrives in a report exactly as wide as the float made it. `:,.4g` -- the
# convention table_profile uses for outlier fences -- is wrong here: it turns
# 1234567.89 into 1.235e+06, which is fine for a fence and unreadable for money.
_PLACES = 4


def number(value: Any) -> Any:
    """A number as a reader should see it. None stays None -- see P8-D9.

    A `Decimal` keeps its own scale and is not rounded. DuckDB returns a
    DECIMAL column as `decimal.Decimal`, and that scale is a fact about the
    column -- 10.50 on a DECIMAL(18,2) money column is two decimal places
    because somebody declared two. A float's seventeen digits are an artifact
    of the type, which is why those are rounded and these are not.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return f"{value:,}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            # P8-D1: inf and nan reach a cell as text unless somebody stops
            # them. Nothing here divides, but a measure can already hold one.
            return str(value)
        rounded = round(value, _PLACES)
        text = f"{rounded:,.{_PLACES}f}".rstrip("0").rstrip(".")
        return text or "0"
    return value


def label(value: Any) -> str:
    """A group's name as a reader sees it. NULL is a group and says so.

    P8-D5: GROUP BY keeps NULL as its own group and PIVOT drops it. A blank
    cell in a frequency table reads as an empty string, which is a different
    value -- Step 1 measured that '' and NULL are two rows, not one.
    """
    return "(null)" if value is None else str(value)


@dataclass(frozen=True)
class ShareBasis:
    """The denominator a share column divides by, or why there is not one."""

    denominator: Any = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None

    def share(self, value: Any) -> str:
        """The cell, or empty when there is no denominator or no value."""
        if not self.ok or value is None:
            return ""
        return f"{value / self.denominator * 100:.1f}%"


def share_basis(agg: str | None, group_totals) -> ShareBasis:
    """P8-D28's three cases, asked once and answered in one place.

    They are three different refusals and the sentence has to say which: an
    aggregate that does not add across groups has no total to be a share of; a
    group total below zero makes a share that can exceed 100% or change sign;
    and a denominator of zero divides to inf rather than raising (P8-D1,
    measured on 1.5.5). The order matters -- an aggregate that does not add is
    refused before its values are inspected, because their signs are not the
    reason.

    top_n, pareto and concentration all need this, which is why it is here
    beside number and label rather than inside the first caller.
    """
    if not adds_across_groups(agg):
        return ShareBasis(reason=(
            f"{agg} does not add up across groups, so there is no total for a "
            f"group to be a share of."
        ))
    values = [v for v in group_totals if v is not None]
    negatives = sum(1 for v in values if v < 0)
    if negatives:
        return ShareBasis(reason=(
            f"{negatives} group(s) total below zero, and a share of a signed "
            f"total is not a proportion -- it can exceed 100% or change sign."
        ))
    denominator = sum(values) if values else 0
    if denominator <= 0:
        return ShareBasis(reason=(
            "the totals sum to zero or less, and dividing by that gives inf "
            "rather than an error."
        ))
    return ShareBasis(denominator=denominator)


class ScopeError(ValueError):
    """The contract describes rows that cannot be selected."""


class TooManyGroups(ValueError):
    """More groups than a per-group table can show: the answer is top_n on the same columns.

    Cleanup Step 11 (CL10-O1): six sites wrote "top_n on {dimension} says which of its groups
    matter" into a plain ValueError, and the tool layer answered every ValueError with
    propose_dataset_contract -- a WHY and a NEXT STEP naming different calls. Carrying the
    columns lets the tool layer name the call the sentence names.
    """

    def __init__(self, message: str, dimension: str, measure: str | None = None):
        super().__init__(message)
        self.dimension, self.measure = dimension, measure


class ParamsInvalid(ValueError):
    """The caller's arguments are wrong, as opposed to the contract's answer.

    Step 9 measured why this is a separate class: an unparseable date and an
    undeclared dimension both raised ValueError, so the tool layer reported
    both as ANALYSIS_NOT_POSSIBLE and told the caller to change the contract.
    One of those is fixed by editing the call. The reason codes exist because
    the eval counts recovery per reason, and a code that averages two
    recoveries measures neither.

    A ValueError subclass on purpose: every test that already asserts
    pytest.raises(ValueError) on a bad limit or a bad threshold keeps
    passing, and the tool layer catches this first.
    """


class LostRows(ScopeError):
    """An analysis's output does not add back to the rows its scope allowed."""


def window_clause(column: str, start, end) -> str:
    """The inclusive window, as SQL, with the dates written in.

    P7-D2, which validate/rules.py already applies to its date checks: the
    window is inclusive at both ends and the column may be a TIMESTAMP, so the
    upper bound is `< end + INTERVAL 1 DAY`. Written `<= end` the bound casts to
    midnight and every window on a timestamp column silently loses its last day.

    The dates are interpolated rather than parameterised. `rules.py` passes them
    as `?` and is right to -- it builds and runs one query. This returns a
    fragment that nine analyses will paste into queries of their own, and a
    fragment carrying placeholders makes every one of them responsible for
    passing two parameters in the right order. A `date` cannot carry SQL: these
    come off a validated `AnalysisWindow`, and `isoformat()` of a `date` is
    three integers and two hyphens. The exclusion rules, which ARE caller text,
    go through sql_guard instead.
    """
    col = quote_identifier(column)
    return (
        f"{col} >= DATE '{start.isoformat()}' "
        f"AND {col} < DATE '{end.isoformat()}' + INTERVAL 1 DAY"
    )


@dataclass(frozen=True)
class Scope:
    """The rows an analysis may see, and why the others were left out."""

    dataset_name: str
    where: str
    rows: int
    excluded: int
    outside_window: int
    no_date: int
    analysed: int
    rule_unknown: int = 0
    skipped_columns: list[str] = field(default_factory=list)
    window_text: str = ""
    #: Rows of groups the caller did not choose (groups=[...], Cleanup Step 14), and which ones.
    unselected: int = 0
    selection_text: str = ""
    #: Rows a caller's filter left out (where=, step 3), and the filter as written.
    filtered: int = 0
    filter_text: str = ""
    #: The relation analyses read, when it is not the table itself (Cleanup Step 15): the table
    #: with its derived measures -- a true/false read as 0/1, an alias, a ratio's parts -- or,
    #: for a measure declared per a unit, one row per unit. "" means the table.
    relation: str = ""
    #: For a per-unit scope: what one row is, said before the counts that describe it.
    unit_text: str = ""

    @property
    def source(self) -> str:
        """What an analysis selects FROM: the relation, or the table by its quoted name."""
        return self.relation or quote_identifier(self.dataset_name)

    def __post_init__(self):
        """Four numbers that sum to the row count, refused if they do not.

        CheckResult's constructor will not let a check under-report; this will
        not let a scope lose a row. An analysis that quietly analysed 900 of
        1,000 rows and said nothing is the failure this whole phase is
        arranged against, and it would arrive as arithmetic rather than as an
        error.
        """
        total = (self.excluded + self.outside_window + self.no_date + self.unselected
                 + self.filtered + self.analysed)
        if total != self.rows:
            raise ScopeError(
                f"the scope of {self.dataset_name} loses rows: "
                f"{self.excluded} excluded + {self.outside_window} outside the "
                f"window + {self.no_date} undated + {self.unselected} unselected + "
                f"{self.filtered} filtered + "
                f"{self.analysed} analysed = "
                f"{total}, against {self.rows} row(s) in the table."
            )
        if self.rule_unknown > self.analysed + self.outside_window + self.no_date:
            raise ScopeError(
                f"{self.rule_unknown} row(s) could not be judged by an "
                f"exclusion, which is more than the {self.rows - self.excluded} "
                f"row(s) the exclusions kept."
            )

    def method_note(self) -> str:
        """What was computed over, in one paragraph a reader can check.

        Every line is a number from this object rather than a description of
        the intent, so a scope that dropped rows unexpectedly says so here
        before anybody reads the result.
        """
        parts = ([self.unit_text] if self.unit_text else []) + [
            f"{self.analysed:,} of {self.rows:,} row(s) analysed."
        ]
        if self.excluded:
            parts.append(f"{self.excluded:,} excluded by the contract.")
        if self.outside_window:
            parts.append(
                f"{self.outside_window:,} outside {self.window_text}."
                if self.window_text
                else f"{self.outside_window:,} outside the analysis window."
            )
        if self.no_date:
            parts.append(
                f"{self.no_date:,} undated and so not placed in the window."
            )
        if self.unselected:
            parts.append(f"{self.unselected:,} outside the groups {self.selection_text}.")
        if self.filtered:
            parts.append(f"{self.filtered:,} left out by the filter {self.filter_text}.")
        if self.rule_unknown:
            parts.append(
                f"{self.rule_unknown:,} kept although an exclusion rule could "
                f"not judge them."
            )
        if self.skipped_columns:
            parts.append(
                "Skipped, as the contract excludes them: "
                + ", ".join(sorted(self.skipped_columns))
                + "."
            )
        return " ".join(parts)


def scope_for(con, gate) -> Scope:
    """The Scope for a gate, measured against the table it names.

    Takes a Gate, never a dataset name. Locked decision 12 says no analysis
    without a confirmed contract, and a builder that accepts a string is a way
    around the gate -- the type is the enforcement.
    """
    contract = gate.contract
    name = contract.dataset_name
    ident = quote_identifier(name)

    keep_terms = []
    excluded_terms = []
    unknown_terms = []
    for exclusion in contract.known_exclusions:
        rule = bind_predicate(con, name, exclusion.rule)
        keep_terms.append(negate(rule))
        excluded_terms.append(f"coalesce(({rule}), false)")
        unknown_terms.append(f"(({rule}) IS NULL)")

    keep = " AND ".join(keep_terms) if keep_terms else "true"
    excluded_any = " OR ".join(excluded_terms) if excluded_terms else "false"
    unknown_any = " OR ".join(unknown_terms) if unknown_terms else "false"

    window = contract.analysis_window
    date_column = contract.date_column
    if window is not None and date_column:
        in_window = window_clause(date_column, window.start, window.end)
        window_text = f"{window.start.isoformat()} to {window.end.isoformat()}"
        where = f"({keep}) AND ({in_window})"
    else:
        # A contract with no window analyses everything it kept, and says so by
        # reporting zero in both window buckets rather than by omitting them.
        in_window = None
        window_text = ""
        where = f"({keep})"

    counts = con.execute(
        f"""
        SELECT
          count(*),
          count(*) FILTER (WHERE {excluded_any}),
          count(*) FILTER (WHERE NOT ({excluded_any}) AND {
              f"NOT ({in_window})" if in_window else "false"}),
          count(*) FILTER (WHERE NOT ({excluded_any}) AND {
              f"({in_window}) IS NULL" if in_window else "false"}),
          count(*) FILTER (WHERE {where}),
          count(*) FILTER (WHERE NOT ({excluded_any}) AND ({unknown_any}))
        FROM {ident}
        """
    ).fetchall()[0]

    return Scope(
        dataset_name=name,
        where=where,
        rows=counts[0],
        excluded=counts[1],
        outside_window=counts[2],
        no_date=counts[3],
        analysed=counts[4],
        rule_unknown=counts[5],
        skipped_columns=list(contract.excluded_columns),
        window_text=window_text,
        relation=measure_relation(con, contract),
    )


def _kind(dtype: str) -> str:
    from .declared import is_numeric

    base = dtype.upper().split("(")[0].strip()
    if is_numeric(dtype):
        return "number"
    if base in ("DATE", "TIMESTAMP", "DATETIME") or base.startswith("TIMESTAMP"):
        return "date"
    return base.lower() or "missing"


def compare_sql(measure, types: dict[str, str]) -> str:
    """A comparison measure as SQL (step 3): 1 where it holds, 0 where not, NULL where a side is.

    Built here from validated names and an operator from a fixed list, never from caller text.
    Both sides must be numbers, or both dates: '9' > '10' as text is true, and a comparison of
    unlike things is refused rather than cast.
    """
    left, op, right = measure.compare
    kinds = [_kind(types.get(left, ""))]
    if right:
        kinds.append(_kind(types.get(right, "")))
    else:
        kinds.append("number")
    missing = [c for c in (left, right) if c and c not in types]
    if missing:
        raise ValueError(f"measure {measure.name} compares {', '.join(missing)}, which "
                         f"{'is' if len(missing) == 1 else 'are'} not a column of the table.")
    if kinds[0] != kinds[1] or kinds[0] not in ("number", "date"):
        raise ValueError(
            f"measure {measure.name} compares {left} ({types[left]}) with "
            f"{right + ' (' + types[right] + ')' if right else 'a number'}: both sides must be "
            f"numbers, or both dates.")
    rhs = quote_identifier(right) if right else repr(float(measure.value))
    return f"CAST(({quote_identifier(left)} {op} {rhs}) AS INTEGER)"


def measure_relation(con, contract) -> str:
    """The table with its derived measures as columns, or "" when it has none (Cleanup Step 15).

    One place turns declarations into columns, so no analysis has to know them: a true/false
    measure is CAST to INTEGER (DuckDB has no avg over BOOLEAN -- tests/test_measure_model_facts.py);
    an aliased measure is its source column under its own name; a ratio is STRUCT(n, d), its scale
    in n, which AGG_SQL['ratio'] sums apart and divides. Aliased as the table, so every clause the
    scope was built with still binds.
    """
    from .declared import column_types

    name = contract.dataset_name
    table = quote_identifier(name)
    types = column_types(con, name)
    replaces: list[str] = []
    extras: list[str] = []
    for m in getattr(contract, "measures", None) or []:
        agg = getattr(m, "agg", None)
        column = getattr(m, "column", None)
        if getattr(m, "compare", None):
            extras.append(f"{compare_sql(m, types)} AS {quote_identifier(m.name)}")
        elif agg == "ratio":
            def summed(terms):
                return " + ".join(
                    ("-" if t.startswith("-") else "") + quote_identifier(t.lstrip("-"))
                    for t in terms)
            scale = float(getattr(m, "scale", 1.0))
            extras.append(
                f"struct_pack(n := {scale!r} * ({summed(m.numerator)}), "
                f"d := CAST({summed(m.denominator)} AS DOUBLE)) AS {quote_identifier(m.name)}")
        elif column and column != m.name:
            src = quote_identifier(column)
            if types.get(column, "").upper() == "BOOLEAN":
                src = f"CAST({src} AS INTEGER)"
            extras.append(f"{src} AS {quote_identifier(m.name)}")
        elif types.get(m.name, "").upper() == "BOOLEAN":
            q = quote_identifier(m.name)
            replaces.append(f"CAST({q} AS INTEGER) AS {q}")
    if not replaces and not extras:
        return ""
    select = "*" + (f" REPLACE ({', '.join(replaces)})" if replaces else "")
    select += "".join(f", {e}" for e in extras)
    return f"(SELECT {select} FROM {table}) AS {table}"


def relation_types(con, scope: Scope) -> dict[str, str]:
    """Column types as an analysis sees them -- through the relation, not the table."""
    from .declared import column_types

    if not scope.relation:
        return column_types(con, scope.dataset_name)
    return {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {scope.source}").fetchall()}


def select_groups(con, gate, scope: Scope, dimension: str | None, groups) -> Scope:
    """The scope holding only the named members of `dimension` (Cleanup Step 14, H1 and H6).

    Store against Online is a question with two groups; channel has three, so hypothesis_test ran
    ANOVA and sample_adequacy refused. Members are compared as text, so a numeric dimension is named
    the way it prints. A member with no row in scope is refused naming those that exist -- a typo
    that silently selected nothing would test the other groups and look like an answer.
    """
    from .declared import require_dimension

    if not dimension:
        raise ParamsInvalid("groups names members of a dimension; pass dimension= as well.")
    require_dimension(gate.contract, dimension)
    wanted = [str(g) for g in groups]
    if not wanted:
        raise ParamsInvalid("groups is empty; name the members to keep, or leave it out.")
    table, d = scope.source, quote_identifier(dimension)
    present = sorted(r[0] for r in con.execute(
        f"SELECT DISTINCT CAST({d} AS VARCHAR) FROM {table} WHERE {scope.where} "
        f"AND {d} IS NOT NULL").fetchall())
    unknown = [g for g in wanted if g not in present]
    if unknown:
        raise ParamsInvalid(
            f"no row in scope has {dimension} {', '.join(repr(u) for u in unknown)}. Its members "
            f"are: {', '.join(present)}.")
    listed = ", ".join("'" + g.replace("'", "''") + "'" for g in wanted)
    where = f"({scope.where}) AND CAST({d} AS VARCHAR) IN ({listed})"
    kept = con.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]
    return replace(scope, where=where, analysed=kept,
                   unselected=scope.unselected + scope.analysed - kept,
                   selection_text=f"{', '.join(wanted)} of {dimension}")


def filter_rows(con, scope: Scope, rule: str) -> Scope:
    """The scope holding only the rows `rule` is true for (where=, step 3).

    The rule is the text after WHERE and passes the same guard as a contract's exclusion: one
    statement, no subquery, binds against the table as BOOLEAN (util/sql_guard.py; P8-D7 read a
    file through a rule that passed everything else). A row the rule is NULL for is left out, and
    counted with the rest. A filter that keeps nothing is refused, not answered with an empty
    table that reads as "none".
    """
    from backend.engine.util.sql_guard import bind_predicate

    bind_predicate(con, scope.dataset_name, rule)
    where = f"({scope.where}) AND coalesce(({rule}), false)"
    kept = con.execute(f"SELECT count(*) FROM {scope.source} WHERE {where}").fetchone()[0]
    if not kept:
        raise ParamsInvalid(
            f"the filter {rule!r} keeps none of the {scope.analysed:,} row(s) in scope. Check "
            f"the values it compares with, as the table writes them (frequency lists them).")
    return replace(scope, where=where, analysed=kept,
                   filtered=scope.filtered + scope.analysed - kept, filter_text=repr(rule))


def unit_scope(con, gate, scope: Scope, measures, columns, *, date: bool = False,
               optional=()) -> Scope:
    """The scope with one row per unit of the measures' `per` (Cleanup Step 15, RF-O1).

    A fee repeated on every line of an order is one value per order; summed over lines it counts
    each order once per line, and a rep's salary averaged over lines weighs a rep by how much they
    sold -- the retail key's 71,692.20 against 72,026.67. So the analysis reads the relation
    reduced to SELECT DISTINCT per, the measures and the columns the call names. If one of those
    varies within a unit the call is refused naming it: counting one rep in several categories is
    the same trap in another form. `optional` columns are kept where constant and left out, named,
    where not; `date` asks for the contract's date column, which must be constant (an order has
    one date; a rep has many).
    """
    per = list(measures[0].per)
    for m in measures[1:]:
        if list(getattr(m, "per", []) or []) != per:
            raise ValueError(
                f"{measures[0].name} is one value per {' + '.join(per) or 'row'} and {m.name} per "
                f"{' + '.join(getattr(m, 'per', []) or []) or 'row'}; values at two grains cannot "
                f"be paired row by row.")
    unit = " + ".join(per)
    date_col = getattr(gate.contract, "date_column", None)
    needed = [c for c in dict.fromkeys(columns) if c and c not in per]
    if date and date_col and date_col not in needed and date_col not in per:
        needed.append(date_col)
    source, where = scope.source, scope.where

    def varies(col: str) -> int:
        keys = ", ".join(quote_identifier(c) for c in per)
        return con.execute(
            f"SELECT count(*) FROM (SELECT {keys} FROM {source} WHERE {where} GROUP BY {keys} "
            f"HAVING count(DISTINCT {quote_identifier(col)}) > 1)").fetchone()[0]

    for col in needed + [m.name for m in measures]:
        n = varies(col)
        if n:
            if col == date_col and date:
                raise ValueError(
                    f"{measures[0].name} is one value per {unit}, and {col} varies within {unit} "
                    f"({n:,} unit(s)), so it has no single date to put on a calendar.")
            raise ValueError(
                f"{col} varies within {unit}: {n:,} {unit} unit(s) hold more than one {col}, so "
                f"grouping {measures[0].name} by it would count one {unit} in several groups. "
                f"Use a column constant within {unit}, or a measure at the row's own grain.")
    kept_optional = [c for c in optional if c not in per and c not in needed and not varies(c)]
    dropped = [c for c in optional if c not in per and c not in needed and c not in kept_optional]
    cols = ", ".join(quote_identifier(c) for c in
                     [*per, *needed, *kept_optional, *(m.name for m in measures)])
    relation = (f"(SELECT DISTINCT {cols} FROM {source} WHERE {where}) "
                f"AS {quote_identifier(scope.dataset_name)}")
    units = con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
    text = (f"{', '.join(m.name for m in measures)} is one value per {unit}: {units:,} {unit} "
            f"unit(s) from {scope.analysed:,} analysed row(s).")
    if dropped:
        text += f" {', '.join(dropped)} vary within {unit} and are left out."
    return replace(scope, where="true", rows=units, excluded=0, outside_window=0, no_date=0,
                   unselected=0, analysed=units, rule_unknown=0, relation=relation, unit_text=text)
