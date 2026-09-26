"""
The ingest spec: one record describing how to read one file.

Everything the agent works out by looking at a file -- where the header is,
where the data starts, what the columns are called, what counts as null --
lands here as data rather than as arguments scattered across a call. It is what
`confirm_ingest_spec` shows the user, and what the loaders are driven from.

Two rules shape the design.

The spec speaks in **sheet coordinates**: `header_rows` and `data_start_row`
are the 1-indexed row numbers a person reads off Excel's row gutter, because
those are what merge ranges are expressed in and what the user is looking at
when they confirm. The Phase 2 loaders speak in **counts**: `header_rows=4`
means "four rows at the top are not data". Those are different numbers.
`to_loader_kwargs` is the single place that converts, so an off-by-one has one
place to hide (FMR F12).

And nothing is dropped in silence. If the spec asks for something the loader
signature cannot accept, `to_loader_kwargs` refuses and names the parameter
instead of quietly omitting it.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.engine.ingest.headers import JOIN_MODES, to_target_name

# Mirrors _IDENT_RE in csv_loader.py. A dataset name becomes a SQL identifier.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")

SourceType = Literal["csv", "excel"]
HeaderJoin = Literal["space", "underscore", "bottom_only", "top_only"]


class SpecNotSupported(Exception):
    """The spec asks for something the target loader cannot do yet."""


class ColumnSpec(BaseModel):
    """One column, as named on the sheet and as it will exist in DuckDB."""

    source_name: str
    target_name: str
    dtype: str | None = None

    @field_validator("target_name")
    @classmethod
    def _target_must_be_an_identifier(cls, v: str) -> str:
        if not _IDENT_RE.match(v):
            raise ValueError(
                f"target_name {v!r} is not a valid SQL identifier. "
                f"Use headers.to_target_name() to normalise it."
            )
        return v


class IngestSpec(BaseModel):
    """How to read one file. Built by preview, confirmed by the user, then run."""

    path: str
    source_type: SourceType
    dataset_name: str

    sheet: str | None = None

    header_rows: list[int] = Field(
        default_factory=lambda: [1],
        description="1-indexed sheet rows that together name the columns.",
    )
    data_start_row: int = Field(
        default=2,
        description="1-indexed row where data begins. Not derived from "
        "len(header_rows) -- a file can have junk between the header and the "
        "data.",
    )
    footer_skip_rows: int = 0

    header_join: HeaderJoin = "space"
    na_values: list[str] | None = None
    delimiter: str | None = None

    authorised_fill: bool = Field(
        default=False,
        description="A person said an upper CSV header row is a spanning "
        "group label. The file cannot establish this; only a human can.",
    )

    columns: list[ColumnSpec] = Field(min_length=1)

    assumptions: list[str] = Field(default_factory=list)

    questions: list[str] = Field(
        default_factory=list,
        description="What could not be worked out from the file. Ask these.",
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="Field names whose values are a guess, not a reading. "
        "While this is non-empty the spec must not be loaded.",
    )

    # ---------------------------------------------------------------- checks

    @field_validator("dataset_name")
    @classmethod
    def _dataset_name_is_an_identifier(cls, v: str) -> str:
        if not _IDENT_RE.match(v):
            raise ValueError(
                f"dataset_name {v!r} must start with a letter and contain only "
                f"letters, digits and underscores (max 63 characters)."
            )
        return v

    @field_validator("header_rows")
    @classmethod
    def _header_rows_are_sane(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError(
                "header_rows is empty. Use [] only if there is genuinely no "
                "header, in which case supply column names explicitly and set "
                "data_start_row to 1."
            )
        if any(r < 1 for r in v):
            raise ValueError(f"header_rows are 1-indexed sheet rows; got {v}.")
        if sorted(v) != v:
            raise ValueError(f"header_rows must be in order; got {v}.")
        if len(set(v)) != len(v):
            raise ValueError(f"header_rows contains duplicates; got {v}.")
        if v[-1] - v[0] != len(v) - 1:
            raise ValueError(
                f"header_rows must be contiguous; got {v}. A header split "
                f"across non-adjacent rows means the rows in between are "
                f"something else -- set data_start_row past them instead."
            )
        return v

    @field_validator("footer_skip_rows")
    @classmethod
    def _footer_is_not_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"footer_skip_rows cannot be negative; got {v}.")
        return v

    @model_validator(mode="after")
    def _cross_field_checks(self) -> IngestSpec:
        if self.data_start_row <= self.header_rows[-1]:
            raise ValueError(
                f"data_start_row={self.data_start_row} is not past the last "
                f"header row ({self.header_rows[-1]}). The data would include "
                f"the header."
            )

        if self.source_type == "excel":
            if self.delimiter is not None:
                raise ValueError("delimiter applies to CSV files, not worksheets.")
        else:
            if self.sheet is not None:
                raise ValueError("sheet applies to workbooks, not CSV files.")

        targets = [c.target_name for c in self.columns]
        if len(set(targets)) != len(targets):
            dupes = sorted({t for t in targets if targets.count(t) > 1})
            raise ValueError(
                f"target_name must be unique; repeated: {dupes}. "
                f"headers.to_target_names() suffixes collisions for you."
            )
        return self

    # ------------------------------------------------------------ properties

    @property
    def loader_header_rows(self) -> int:
        """
        How many rows the loader must treat as not-data.

        This is `data_start_row - 1`, a count. It is NOT `len(header_rows)`.
        For a sheet with a title in row 1, blanks, a header in row 4 and data
        from row 5, header_rows is [4] -- length 1 -- but the loader must skip
        4. Deriving this from the list length is F12.
        """
        return self.data_start_row - 1

    @property
    def is_confirmable(self) -> bool:
        """
        False while anything in `unresolved` is outstanding.

        This is the guarantee, and it is deliberately structural rather than a
        line in a docstring. A provisional spec is a guess with a default
        filled in; making it un-loadable until a person clears the field means
        no amount of eagerness can turn a guess into a load.
        """
        return not self.unresolved

    def blocking_message(self) -> str:
        """Why this spec cannot be loaded yet, and what to do."""
        fields = ", ".join(self.unresolved)
        asks = "\n".join(f"  - {q}" for q in self.questions) or "  - (none recorded)"
        return (
            f"BLOCKED: this spec is provisional. {fields} was guessed, not read "
            f"from the file.\n"
            f"WHY: nothing in the file settles it, so a default was filled in "
            f"to show you what a load would look like. Loading it now would "
            f"make that guess permanent without anyone having agreed to it.\n"
            f"OUTSTANDING:\n{asks}\n"
            f"NEXT STEP: put the question to the user. Then call "
            f"propose_ingest_spec again with their answer -- header_rows=[...], "
            f"header_join=..., authorised_fill=true/false -- and confirm the "
            f"spec it returns. Do not simply delete the unresolved field."
        )

    @property
    def column_count(self) -> int:
        return len(self.columns)

    @property
    def source_names(self) -> list[str]:
        return [c.source_name for c in self.columns]

    @property
    def target_names(self) -> list[str]:
        return [c.target_name for c in self.columns]

    # --------------------------------------------------------------- bridge

    def to_loader_kwargs(self, loader: Callable | None = None) -> dict[str, Any]:
        """
        The keyword arguments for load_csv or load_excel.

        `con` and `path` are not included; the caller supplies those.

        If `loader` is given, its signature is inspected and any spec field it
        cannot accept raises SpecNotSupported naming the parameter. Passing the
        real function is the point -- it turns a future silent drop into a loud
        failure today.
        """
        kwargs: dict[str, Any] = {
            "dataset_name": self.dataset_name,
            "header_rows": self.loader_header_rows,
            "names": self.target_names,
        }

        if self.source_type == "excel":
            kwargs["sheet"] = self.sheet
        else:
            if self.delimiter is not None:
                kwargs["delimiter"] = self.delimiter

        if self.na_values is not None:
            kwargs["na_values"] = self.na_values
        if self.footer_skip_rows:
            kwargs["footer_skip_rows"] = self.footer_skip_rows
        # A pinned type is part of what was confirmed. It was shown by to_text() and passed to
        # neither loader, so the file loaded as inferred (C96). Added only when a column pins
        # one, so a spec that pins nothing yields the kwargs it always did.
        dtypes = {c.target_name: c.dtype for c in self.columns if c.dtype}
        if dtypes:
            kwargs["dtypes"] = dtypes

        if loader is not None:
            accepted = set(inspect.signature(loader).parameters)
            missing = sorted(k for k in kwargs if k not in accepted)
            if missing:
                raise SpecNotSupported(
                    f"BLOCKED: {getattr(loader, '__name__', loader)} does not "
                    f"accept {', '.join(missing)}.\n"
                    f"WHY: the spec asks for it, and dropping it silently would "
                    f"load the file on different terms than the ones confirmed.\n"
                    f"NEXT STEP: add the parameter to the loader, or clear the "
                    f"field on the spec."
                )
        return kwargs

    # ------------------------------------------------------------ rendering

    def to_text(self) -> str:
        """A plain reading of the spec, for confirm_ingest_spec in Step 6."""
        lines = [
            f"path           {self.path}",
            f"type           {self.source_type}"
            + (f" (sheet {self.sheet!r})" if self.sheet else ""),
            f"dataset        {self.dataset_name}",
            f"header rows    {self.header_rows}  joined with {self.header_join!r}",
            f"data starts    row {self.data_start_row}"
            f"  -> loader skips {self.loader_header_rows}",
        ]
        if self.footer_skip_rows:
            lines.append(f"footer skip    {self.footer_skip_rows} row(s)")
        if self.unresolved:
            lines.append(
                f"PROVISIONAL    {', '.join(self.unresolved)} is a guess; "
                f"this spec cannot be loaded as it stands"
            )
        if self.na_values:
            lines.append(f"null tokens    {self.na_values}")
        lines.append("")
        lines.append(f"{self.column_count} columns:")
        lines.append("| source | target | type |")
        lines.append("| --- | --- | --- |")
        for c in self.columns:
            lines.append(f"| {c.source_name} | {c.target_name} | {c.dtype or 'infer'} |")
        if self.assumptions:
            lines.append("")
            lines.append("Assumptions:")
            lines.extend(f"  - {a}" for a in self.assumptions)
        return "\n".join(lines)

    # ----------------------------------------------------------- constructor

    @classmethod
    def from_header_result(
        cls,
        result,
        *,
        path: str,
        source_type: SourceType,
        dataset_name: str,
        header_rows: list[int],
        data_start_row: int,
        sheet: str | None = None,
        header_join: HeaderJoin = "space",
        questions: list[str] | None = None,
        unresolved: list[str] | None = None,
        **extra: Any,
    ) -> IngestSpec:
        """
        Build a spec from a headers.HeaderResult.

        The result's notes become the spec's assumptions, so whatever the
        header assembly had to decide travels with the spec to the point of
        confirmation rather than being reported once and lost.
        """
        seen: dict[str, int] = {}
        columns = []
        for source in result.names:
            target = to_target_name(source)
            if target in seen:
                seen[target] += 1
                target = f"{target}_{seen[target]}"
            else:
                seen[target] = 1
            columns.append(ColumnSpec(source_name=source, target_name=target))

        return cls(
            path=path,
            source_type=source_type,
            dataset_name=dataset_name,
            sheet=sheet,
            header_rows=header_rows,
            data_start_row=data_start_row,
            header_join=header_join,
            columns=columns,
            assumptions=list(result.notes),
            questions=list(questions or []),
            unresolved=list(unresolved or []),
            **extra,
        )


__all__ = [
    "ColumnSpec",
    "IngestSpec",
    "SpecNotSupported",
    "JOIN_MODES",
]
