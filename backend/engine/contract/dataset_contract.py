"""
The Dataset Contract: what a person has agreed a table means.

Build guide Section 7. `evidence.py` says what the data can be made to say;
this says what it is FOR, and almost none of that is derivable. The grain, the
definition of a measure, which rows were excluded on purpose and why -- these
are the eight decisions locked on Olist, generalised so they work on any
dataset. Locked decision 12: no analysis without one.

Three rules shape the model.

**Nothing is filled in silently.** `unresolved` holds the field paths whose
values are a guess or a blank rather than something a person stated, and
`is_confirmable` is False while it is non-empty -- exactly `IngestSpec`. The
guarantee is structural rather than a line in a docstring, and it goes further
here: a contract whose grain is blank and which does NOT list "grain" as
unresolved will not construct at all. There is no way to hold a contract that
claims to be settled and is not.

**A contract is bound to a structure, not to a moment.** `bound_to` records
the ordered columns and their types as they were when the contract was
confirmed, plus a fingerprint over them. Row count and load time are recorded
too, but they are NOT part of the fingerprint: a table that gained ten
thousand rows is the same table, and one that lost a column is not. F13 is
why this exists -- the server outlives the chat, so `sales_2024` can be
reloaded from a different file under the same name and inherit an agreement
nobody made about it. What the classes of drift MEAN is Step 3's problem; this
module only records enough for that question to be answerable.

**A reference to a column that does not exist is an error, not a warning.**
If `bound_to` is present, every column named by the primary key, the date
column, a measure or a dimension must appear in it. A measure naming a
dropped column is not a caveat to attach to a result -- the SQL cannot run,
and finding that out at the gate beats finding it out three layers into an
analysis.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.engine.contract.refusals import Reason, Refusal

# Mirrors _IDENT_RE in ingest/spec.py. A name that reaches SQL is validated,
# never quoted and hoped for.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")

# Types a date_column may have. A date written into a sheet as text stays text
# (Phase 3), and text sorts lexically -- '2024-10-01' before '2024-9-01'.
_TEMPORAL_PREFIXES = ("DATE", "TIMESTAMP", "DATETIME")

# How a measure is combined across rows. `none` means non-additive: the value
# is meaningful per row and meaningless summed -- a unit price, a rate, a
# balance. There is deliberately no default; see Measure.agg.
Aggregation = Literal[
    "sum", "mean", "median", "min", "max", "count", "count_distinct", "none", "ratio"
]

AGGREGATIONS = (
    "sum", "mean", "median", "min", "max", "count", "count_distinct", "none", "ratio"
)


#: The comparisons a measure may make (step 3). Nothing else reaches SQL from `compare`.
COMPARE_OPS = (">", ">=", "<", "<=", "=", "<>")


class Measure(BaseModel):
    """A column to aggregate, and the sentences that say what it means.

    `agg` has NO DEFAULT, which is deliberate and is what every semantic layer
    in production does: LookML requires `type:` on a measure, Cube requires
    `type`, dbt MetricFlow requires `agg`. None of them guesses, because the
    guess that gets guessed is `sum`, and summing a unit price or a rate or a
    balance produces a number with no meaning that nothing downstream can
    detect.

    A default here was tried and removed. Two live runs pushed back on it, the
    second describing it exactly: "the plausible-looking default is the kind of
    thing that gets confirmed without being read". A blank cannot be confirmed
    without being read.
    """

    name: str
    agg: Aggregation | None = Field(
        default=None,
        description="How this measure combines across rows. No default: "
        "`sum` is wrong for a price, a rate or a balance, and a wrong "
        "aggregation is invisible in the result. Use 'none' to say the value "
        "is non-additive on purpose.",
    )
    definition: str = Field(
        default="",
        description="What this number IS -- net of tax, excludes cancelled. "
        "Cannot be derived from the column. Blank is only legal while the "
        "contract lists it as unresolved.",
    )
    unit: str | None = None
    # Cleanup Step 15 (RF-O1). `column`: the table column this measure reads, when that is not its
    # own name -- units_per_line reads units with agg mean while units sums. `per`: the columns
    # whose value this is -- a fee repeated on every line of an order is one value per order_id,
    # and every statistic over it is taken over orders. `numerator`/`denominator`/`scale`: a ratio
    # of sums, agg 'ratio' -- signed column lists ("-line_cost"), summed apart, then divided.
    column: str | None = None
    per: list[str] = Field(default_factory=list)
    numerator: list[str] = Field(default_factory=list)
    denominator: list[str] = Field(default_factory=list)
    scale: float = 1.0
    # Step 3 (26/09/2026). A comparison of two columns, 1 where it holds, 0 where it does not,
    # NULL where either side is: `recorded_delivery_minutes > promised_minutes` is an SLA breach,
    # and its mean is the breach rate. Columns only, never SQL text; `right` may instead be a
    # number (`value`). `provisional` names the approval that made it, for one the assistant
    # proposed and a person approved outside the contract.
    compare: list[str] = Field(default_factory=list)
    value: float | None = None
    provisional: str = ""

    @field_validator("name")
    @classmethod
    def _name_is_an_identifier(cls, v: str) -> str:
        if not _IDENT_RE.match(v):
            raise ValueError(f"measure name {v!r} is not a valid column name.")
        return v

    @model_validator(mode="after")
    def _ratio_is_whole(self) -> "Measure":
        is_ratio = self.agg == "ratio"
        if is_ratio and not (self.numerator and self.denominator):
            raise ValueError(
                f"measure {self.name!r} is a ratio and needs both a numerator and a denominator: "
                f"lists of columns, each summed, e.g. numerator=['line_revenue', '-line_cost'].")
        if not is_ratio and (self.numerator or self.denominator):
            raise ValueError(
                f"measure {self.name!r} names a numerator or denominator but its agg is "
                f"{self.agg!r}; only agg 'ratio' divides one sum by another.")
        if self.compare:
            if len(self.compare) != 3 or self.compare[1] not in COMPARE_OPS:
                raise ValueError(
                    f"measure {self.name!r} compares two columns: compare=[left, op, right] with "
                    f"op one of {', '.join(COMPARE_OPS)}, e.g. ['delivery_minutes', '>', "
                    f"'promised_minutes']; right may be '' with value= a number.")
            if self.agg not in ("mean", "sum"):
                raise ValueError(
                    f"measure {self.name!r} is a comparison, 1 or 0 a row: its mean is a rate "
                    f"and its sum a count, so agg is 'mean' or 'sum', not {self.agg!r}.")
            if bool(self.compare[2]) == (self.value is not None):
                raise ValueError(
                    f"measure {self.name!r} compares with a column or with a value, not both "
                    f"or neither.")
            if self.column or self.per or is_ratio:
                raise ValueError(f"measure {self.name!r} is a comparison; it reads the two "
                                 f"columns it compares and nothing else.")
        if is_ratio and (self.column or self.per):
            raise ValueError(
                f"measure {self.name!r} is a ratio of sums over rows; it reads its numerator and "
                f"denominator columns, not a column of its own or a coarser unit.")
        for c in [*self.per, *(t.lstrip("-") for t in self.numerator + self.denominator),
                  *([self.column] if self.column else []),
                  *[x for i, x in enumerate(self.compare) if i != 1 and x]]:
            if not _IDENT_RE.match(c):
                raise ValueError(f"measure {self.name!r} names {c!r}, not a valid column name.")
        return self

    @property
    def source(self) -> str:
        """The table column read, for a measure that reads one."""
        return self.column or self.name

    @property
    def is_virtual(self) -> bool:
        """A name that is not a table column: an alias, a ratio or a comparison."""
        return (self.agg == "ratio" or bool(self.compare)
                or bool(self.column and self.column != self.name))

    def formula(self) -> str:
        """A comparison in words a person can check: `a > b`, or `a > 90`."""
        left, op, right = self.compare
        return f"{left} {op} {right or format(self.value, 'g')}"

    def columns_read(self) -> list[str]:
        if self.compare:
            return [c for i, c in enumerate(self.compare) if i != 1 and c]
        if self.agg == "ratio":
            return [t.lstrip("-") for t in self.numerator + self.denominator]
        return [self.source, *self.per]

    @property
    def definition_path(self) -> str:
        """How this measure's missing definition is named in `unresolved`."""
        return f"measures[{self.name}].definition"

    @property
    def agg_path(self) -> str:
        """How this measure's unstated aggregation is named in `unresolved`."""
        return f"measures[{self.name}].agg"


class Exclusion(BaseModel):
    """Rows deliberately left out, and how many there were."""

    rule: str
    reason: str
    row_count: int | None = None

    @field_validator("rule", "reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "an exclusion needs both a rule and a reason. A rule with no "
                "reason is a number nobody can defend later."
            )
        return v

    @field_validator("row_count")
    @classmethod
    def _not_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"row_count cannot be negative; got {v}.")
        return v


class Expectation(BaseModel):
    """A statement every row must make true -- Great Expectations' word for it.

    Cleanup Step 13 (RF-O7): the retail run's negative quantities, deliveries before the order and
    sales after a rep's exit passed validation, because no check could state them. The rule is SQL
    text a person wrote, so it goes through util/sql_guard like an exclusion rule; validation
    counts the rows where it is false, and leaves apart the rows where it is NULL.
    """

    rule: str
    reason: str

    @field_validator("rule", "reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "an expectation needs both a rule and a reason. A rule with no reason is a "
                "failure count nobody can act on."
            )
        return v


class ForeignKey(BaseModel):
    """A column that must point at a row of another dataset.

    dbt's `relationships` test, declared where dbt declares it: in the schema
    file beside the model, not in the call that runs the check. The referenced
    dataset is NAMED, not resolved -- it may not be loaded when the contract is
    written, and a check with nothing to run against reports NOT RUN rather
    than failing (P7-D7).

    Composite rather than single-column, because `primary_key` already is and a
    foreign key that cannot reference a composite key cannot reference half the
    tables this project loads.
    """

    columns: list[str]
    references: str
    referenced_columns: list[str] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="after")
    def _the_two_sides_line_up(self) -> ForeignKey:
        if not self.columns:
            raise ValueError("a foreign key needs at least one column.")
        if not self.references.strip():
            raise ValueError(
                "a foreign key needs the dataset it references, by name."
            )
        if not self.referenced_columns:
            # The common case, and the one dbt writes: the same column names on
            # both sides. Filled in rather than left for the checker to guess.
            object.__setattr__(self, "referenced_columns", list(self.columns))
        if len(self.columns) != len(self.referenced_columns):
            raise ValueError(
                f"{len(self.columns)} column(s) on this side and "
                f"{len(self.referenced_columns)} on {self.references}: a "
                f"foreign key joins them in pairs, so the two lists must be "
                f"the same length."
            )
        return self

    def label(self) -> str:
        left = " + ".join(self.columns)
        right = " + ".join(self.referenced_columns)
        return f"{left} -> {self.references}.{right}"


class AnalysisWindow(BaseModel):
    """The period the analysis covers. Both ends inclusive."""

    start: date
    end: date

    @model_validator(mode="after")
    def _end_is_not_before_start(self) -> AnalysisWindow:
        if self.end < self.start:
            raise ValueError(
                f"analysis_window ends {self.end} which is before it starts "
                f"{self.start}."
            )
        return self

    def to_text(self) -> str:
        return f"{self.start.isoformat()} to {self.end.isoformat()}"


class ColumnBinding(BaseModel):
    """One column as it stood when the contract was confirmed."""

    name: str
    dtype: str


class Binding(BaseModel):
    """
    The table a contract was agreed against.

    `fingerprint` covers the ordered column list and types and NOTHING else.
    Structure is identity; volume is not. A table that gained rows is the same
    table and the contract still describes it -- with a caveat, because the
    numbers were agreed against different data. A table that lost a column is
    a different table.

    `row_count` and `loaded_at` are recorded beside it as the trigger for that
    caveat, and as a cache key: a fingerprint hit with the same row count
    means nothing has moved and the gate can skip re-validating.
    """

    columns: list[ColumnBinding] = Field(min_length=1)
    row_count: int
    loaded_at: datetime | None = None

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def dtypes(self) -> dict[str, str]:
        return {c.name: c.dtype for c in self.columns}

    @property
    def fingerprint(self) -> str:
        """
        Twelve hex characters over name:type, in order.

        Short on purpose: it is printed in refusals and read by people, and
        the full digest buys nothing when the thing it protects against is a
        reload rather than an adversary.
        """
        payload = "|".join(f"{c.name}:{c.dtype.upper()}" for c in self.columns)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    @classmethod
    def from_pairs(
        cls,
        columns: list[tuple[str, str]],
        row_count: int,
        loaded_at: datetime | None = None,
    ) -> Binding:
        """Build from what information_schema returns, in ordinal order."""
        return cls(
            columns=[ColumnBinding(name=n, dtype=t) for n, t in columns],
            row_count=row_count,
            loaded_at=loaded_at,
        )


class DatasetContract(BaseModel):
    """What one dataset means. Proposed by the agent, confirmed by a person."""

    dataset_name: str

    grain: str = Field(
        default="",
        description="What ONE ROW is. 'one row = one order line item'. The "
        "single most load-bearing sentence here: it decides whether summing "
        "a column double-counts.",
    )
    primary_key: list[str] = Field(default_factory=list)
    date_column: str | None = None
    analysis_window: AnalysisWindow | None = None

    measures: list[Measure] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    known_exclusions: list[Exclusion] = Field(default_factory=list)
    expectations: list[Expectation] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    #: Counted by the engine from the table (contract/measured_caveats.py), at proposal and again
    #: at confirmation -- never typed. `caveats` above are the person's (25/09/2026).
    measured_caveats: list[str] = Field(default_factory=list)
    #: Who decided each field: {"measures[unit_price].agg": {"status": "overruled", "llm": "sum",
    #: "final": "none", "reason": ..., "model": ...}} -- the model, the data, both, or the person
    #: (webapp/autofill.py, contract/llm_filter.py). An audit, not a definition: not tracked as a
    #: change between versions.
    provenance: dict[str, dict] = Field(default_factory=dict)

    missing_values: list[str] | None = Field(
        default=None,
        description="Strings that mean ABSENT in this dataset. None means use "
        "the project vocabulary; [] means this dataset has none and every "
        "such string is a real value. Unset is a DEFAULT, not a gap: it does "
        "not belong in `unresolved`.",
    )
    excluded_columns: list[str] = Field(
        default_factory=list,
        description="Columns never to read. Distinct from known_exclusions, "
        "which is about ROWS. Cleaning does not propose changes to these and "
        "says it skipped them.",
    )
    foreign_keys: list[ForeignKey] = Field(
        default_factory=list,
        description="Columns that must point at a row of another dataset. "
        "Empty is a DEFAULT, not a gap: most datasets reference nothing, and "
        "a field that counted as a gap would make every contract in the "
        "workspace unconfirmable the day it was added.",
    )
    domains: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Column -> the complete set of values it may hold. dbt's "
        "accepted_values. Empty is a DEFAULT, not a gap, for the same reason "
        "as foreign_keys.",
    )

    bound_to: Binding | None = None

    questions: list[str] = Field(
        default_factory=list,
        description="What the data cannot settle. Ask these.",
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="Field paths whose value is a guess or a blank: 'grain', "
        "'analysis_window', 'measures[revenue].definition'. While this is "
        "non-empty the contract must not be confirmed.",
    )

    # Written by the store on confirm (Step 4), not by whoever drafts it.
    version: int = 0
    confirmed_at: datetime | None = None

    # ---------------------------------------------------------------- checks

    @field_validator("dataset_name")
    @classmethod
    def _dataset_name_is_an_identifier(cls, v: str) -> str:
        if not _IDENT_RE.match(v):
            raise ValueError(
                f"dataset_name {v!r} must start with a letter and contain only "
                f"letters, digits and underscores."
            )
        return v

    @field_validator("primary_key", "dimensions")
    @classmethod
    def _columns_are_identifiers(cls, v: list[str]) -> list[str]:
        for name in v:
            if not _IDENT_RE.match(name):
                raise ValueError(f"{name!r} is not a valid column name.")
        if len(set(v)) != len(v):
            dupes = sorted({n for n in v if v.count(n) > 1})
            raise ValueError(f"repeated column(s): {dupes}.")
        return v

    @model_validator(mode="after")
    def _cross_field_checks(self) -> DatasetContract:
        names = [m.name for m in self.measures]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"measure names must be unique; repeated: {dupes}.")

        both = sorted(set(names) & set(self.dimensions))
        if both:
            raise ValueError(
                f"{both} appear as both a measure and a dimension. A column is "
                f"aggregated or grouped by, not both -- and an agent handed "
                f"both will pick one without telling you which."
            )

        if self.analysis_window is not None and self.date_column is None:
            raise ValueError(
                "analysis_window is set but date_column is not, so nothing "
                "says which column the window applies to."
            )

        # Blank values are legal ONLY while declared unresolved. This is the
        # whole of D2, enforced where it cannot be forgotten.
        if not self.grain.strip() and "grain" not in self.unresolved:
            raise ValueError(
                "grain is blank and is not listed in unresolved. A contract "
                "either states what one row is, or says out loud that nobody "
                "has yet."
            )
        for m in self.measures:
            if not m.definition.strip() and m.definition_path not in self.unresolved:
                raise ValueError(
                    f"measure {m.name!r} has no definition and "
                    f"{m.definition_path!r} is not listed in unresolved. What "
                    f"a number means cannot be read off the column."
                )
            if m.agg is None and m.agg_path not in self.unresolved:
                raise ValueError(
                    f"measure {m.name!r} has no aggregation and "
                    f"{m.agg_path!r} is not listed in unresolved. How a "
                    f"number combines across rows is a decision, not a "
                    f"default -- summing a unit price is meaningless and "
                    f"nothing downstream can tell that it happened. One of "
                    f"{', '.join(AGGREGATIONS)}."
                )

        if self.bound_to is not None:
            self._check_against_binding()
        return self

    def _check_against_binding(self) -> None:
        """Every column this contract names must exist in the bound table."""
        known = set(self.bound_to.column_names)
        referenced: list[tuple[str, str]] = []
        referenced += [("primary_key", c) for c in self.primary_key]
        referenced += [("dimensions", c) for c in self.dimensions]
        referenced += [("measures", c) for m in self.measures for c in m.columns_read()]
        if self.date_column:
            referenced.append(("date_column", self.date_column))

        missing = sorted({f"{c} ({where})" for where, c in referenced if c not in known})
        clash = sorted(m.name for m in self.measures if m.is_virtual and m.name in known)
        if clash:
            raise ValueError(
                f"measure(s) {', '.join(clash)} read another column or divide two sums, but are "
                f"named like a column of the table; name them apart so no reader takes one for "
                f"the other.")
        if missing:
            raise ValueError(
                f"these columns are named by the contract but are not in the "
                f"table it is bound to: {', '.join(missing)}. Present: "
                f"{', '.join(sorted(known))}."
            )

        if self.date_column:
            dtype = self.bound_to.dtypes[self.date_column].upper()
            if not dtype.startswith(_TEMPORAL_PREFIXES):
                raise ValueError(
                    f"date_column {self.date_column!r} is {dtype}, not a date. "
                    f"A date held as text sorts lexically and cannot carry an "
                    f"analysis window. Reload with "
                    f"dtypes={{'{self.date_column}': 'DATE'}} first."
                )

    # ------------------------------------------------------------ properties

    @property
    def is_confirmable(self) -> bool:
        """False while anything in `unresolved` is outstanding."""
        return not self.unresolved

    @property
    def fingerprint(self) -> str | None:
        return self.bound_to.fingerprint if self.bound_to else None

    @property
    def measure_names(self) -> list[str]:
        return [m.name for m in self.measures]

    def measure(self, name: str) -> Measure:
        for m in self.measures:
            if m.name == name:
                return m
        raise KeyError(name)

    def columns_used(self) -> list[str]:
        """Every column this contract names, deduped, in a stable order."""
        seen: dict[str, None] = {}
        for c in (
            self.primary_key
            + ([self.date_column] if self.date_column else [])
            + self.measure_names
            + self.dimensions
        ):
            seen.setdefault(c, None)
        return list(seen)

    # ------------------------------------------------------------- refusals

    def blocking_refusal(self) -> Refusal:
        """Why this contract cannot be confirmed yet, as a Refusal."""
        return Refusal(
            reason=Reason.CONTRACT_PROVISIONAL,
            what=(
                f"the contract for '{self.dataset_name}' is provisional. "
                f"{', '.join(self.unresolved)} "
                f"{'was' if len(self.unresolved) == 1 else 'were'} guessed or "
                f"left blank, not stated."
            ),
            why=(
                "nothing in the data settles these, so a value was filled in "
                "to show what the contract would look like. Confirming it now "
                "would make that guess the definition every later number is "
                "traced back to."
            ),
            outstanding=list(self.questions) or ["(none recorded)"],
            next_call=(
                f'propose_dataset_contract(dataset_name="{self.dataset_name}", '
                f"grain=..., measure_definitions=...) with the answers, then "
                f"confirm the contract it returns"
            ),
        )

    def blocking_message(self) -> str:
        return self.blocking_refusal().to_text()

    # ------------------------------------------------------------ rendering

    def to_text(self) -> str:
        """
        A plain reading of the agreement, for the confirmation step.

        `questions` are deliberately NOT rendered here. They belong to the
        conversation, not to the document, and whoever is running that
        conversation puts them at the BOTTOM of what it shows -- which is
        where an answer actually gets given. Printing them in both places
        produced two identical lists in one tool result, which is the Phase 3
        Step 8 bug exactly: the one thing guaranteed to make a user answer
        twice.
        """
        lines = [
            f"dataset        {self.dataset_name}",
            f"grain          {self.grain or '(not stated)'}",
            f"primary key    {' + '.join(self.primary_key) or '(none)'}",
            f"date column    {self.date_column or '(none)'}",
            f"window         "
            f"{self.analysis_window.to_text() if self.analysis_window else '(not set)'}",
        ]
        if self.bound_to:
            lines.append(
                f"bound to       {len(self.bound_to.columns)} columns, "
                f"{self.bound_to.row_count:,} rows, "
                f"fingerprint {self.bound_to.fingerprint}"
            )
        if self.version:
            lines.append(f"version        {self.version}")
        if self.unresolved:
            lines.append(
                f"PROVISIONAL    {', '.join(self.unresolved)} -- this contract "
                f"cannot be confirmed as it stands"
            )

        if self.measures:
            lines += ["", f"{len(self.measures)} measure(s):",
                      "| column | agg | unit | definition |",
                      "| --- | --- | --- | --- |"]
            for m in self.measures:
                lines.append(
                    f"| {m.name} | {m.agg or '(NOT STATED)'} | "
                    f"{m.unit or '-'} | {m.definition or '(NOT STATED)'} |"
                )
        if self.dimensions:
            lines += ["", f"dimensions     {', '.join(self.dimensions)}"]
        if self.known_exclusions:
            lines += ["", "Known exclusions:"]
            for e in self.known_exclusions:
                count = f" ({e.row_count:,} rows)" if e.row_count is not None else ""
                lines.append(f"  - {e.rule}{count} -- {e.reason}")
        if self.expectations:
            lines += ["", "Rules every row must satisfy:"]
            lines += [f"  - {x.rule} -- {x.reason}" for x in self.expectations]
        if self.measured_caveats:
            lines += ["", "Measured by the engine:"] + [f"  - {c}" for c in self.measured_caveats]
        if self.caveats:
            lines += ["", "Caveats (declared, not measured):"] + [f"  - {c}" for c in self.caveats]
        return "\n".join(lines)

    @model_validator(mode="after")
    def _declared_columns_exist(self) -> DatasetContract:
        """A declaration naming a column the table does not have is a typo.

        Only checkable once `bound_to` is set, which is the same condition
        `date_column`'s type check runs under. Unbound contracts are legal --
        a contract has to be readable on a machine that never loaded the table
        -- so this says nothing about them.
        """
        if self.bound_to is None:
            return self
        known = {c.name for c in self.bound_to.columns}
        for fk in self.foreign_keys:
            missing = [c for c in fk.columns if c not in known]
            if missing:
                raise ValueError(
                    f"foreign key {fk.label()} names {', '.join(missing)}, "
                    f"which {self.dataset_name} does not have."
                )
        for column in self.domains:
            if column not in known:
                raise ValueError(
                    f"a domain is declared for {column}, which "
                    f"{self.dataset_name} does not have."
                )
        return self

    def to_yaml_dict(self) -> dict[str, Any]:
        """
        The export written to docs/contracts/<dataset>.yaml on confirm.

        Definitions belong in version control -- that is where the semantic
        layer world put them, and it is what makes a report's numbers
        reviewable in a pull request rather than only inside a database. The
        stored row stays authoritative; this is a copy, regenerated every
        time, never hand-edited.
        """
        return {
            "dataset_name": self.dataset_name,
            "version": self.version,
            "confirmed_at": (
                self.confirmed_at.isoformat() if self.confirmed_at else None
            ),
            "fingerprint": self.fingerprint,
            "grain": self.grain,
            "primary_key": list(self.primary_key),
            "date_column": self.date_column,
            "analysis_window": (
                {
                    "start": self.analysis_window.start.isoformat(),
                    "end": self.analysis_window.end.isoformat(),
                }
                if self.analysis_window
                else None
            ),
            "measures": [
                {
                    "name": m.name,
                    "agg": m.agg,
                    "unit": m.unit,
                    "definition": m.definition,
                    **({"column": m.column} if m.column else {}),
                    **({"per": list(m.per)} if m.per else {}),
                    **({"numerator": list(m.numerator), "denominator": list(m.denominator),
                        "scale": m.scale} if m.agg == "ratio" else {}),
                }
                for m in self.measures
            ],
            "dimensions": list(self.dimensions),
            "known_exclusions": [
                {"rule": e.rule, "reason": e.reason, "row_count": e.row_count}
                for e in self.known_exclusions
            ],
            "expectations": [{"rule": x.rule, "reason": x.reason} for x in self.expectations],
            "caveats": list(self.caveats),
            "measured_caveats": list(self.measured_caveats),
            "provenance": {k: dict(v) for k, v in self.provenance.items()},
            "foreign_keys": [
                {
                    "columns": list(fk.columns),
                    "references": fk.references,
                    "referenced_columns": list(fk.referenced_columns),
                    "reason": fk.reason,
                }
                for fk in self.foreign_keys
            ],
            "domains": {k: list(v) for k, v in self.domains.items()},
        }


def contract_from_json(payload: str) -> DatasetContract:
    """
    Parse a confirmed contract, turning any validation error into a refusal.

    Same trip `IngestSpec` makes: the contract goes out as JSON and comes back
    as JSON, so the thing confirmed is the thing stored and any field can be
    edited in between. What cannot be edited in is a blank definition -- the
    validators above refuse it, so deleting an `unresolved` entry by hand
    fails instead of silently confirming a guess.
    """
    text = payload.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        return DatasetContract.model_validate_json(text)
    except Exception as exc:
        raise ValueError(
            Refusal(
                reason=Reason.CONTRACT_INVALID,
                what="that is not a usable dataset contract.",
                why="it did not validate, so it was not stored.",
                detail=str(exc),
                next_call=(
                    "propose_dataset_contract(dataset_name=...) to get a valid "
                    "one, edit the fields you want to change, and pass the "
                    "whole JSON object back"
                ),
            ).to_text()
        ) from exc


__all__ = [
    "Aggregation",
    "AnalysisWindow",
    "Binding",
    "ColumnBinding",
    "DatasetContract",
    "Exclusion",
    "ForeignKey",
    "Measure",
    "contract_from_json",
]
