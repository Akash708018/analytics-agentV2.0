"""
What the four contract tools actually do.

`server.py` holds no logic -- every tool there calls one function and returns
what it gets back. These are those functions, and they live here rather than in
the server for two reasons that both come down to testing: they can be called
without FastMCP running, and they can be asserted on as strings, which is what
the agent will actually read.

Everything returns a string. Refusals come back as text rather than raised, at
this boundary only, because a raised exception inside a FastMCP tool becomes a
traceback and a traceback is not an instruction. Inside the layers below,
refusals raise -- that is what stops a caller continuing past one.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from backend.engine.config import DEFAULT_WORKSPACE_ID, validate_workspace_id
from backend.engine.contract import ContractRefused, measured_caveats, store
from backend.engine.contract.dataset_contract import (
    DatasetContract,
    Exclusion,
    Expectation,
    ForeignKey,
    contract_from_json,
)
from backend.engine.contract.propose import propose_contract
from backend.engine.contract.refusals import Reason, Refusal
from backend.engine.state import describe_workflow_state, require_contract
from backend.engine.analysis.registry import catalogue
from backend.engine.util import db


def _parse_window(
    start: str | None, end: str | None
) -> tuple[date, date] | None:
    """Two ISO strings, or neither. One of the two is an error, not a default."""
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ContractRefused(
            Refusal(
                reason=Reason.CONTRACT_INVALID,
                what="an analysis window needs both ends.",
                why=(
                    "a window with one end open is not a window, and guessing "
                    "the other end would put a date nobody chose into the "
                    "contract every later number is traced back to."
                ),
                next_call=(
                    "propose_dataset_contract(..., analysis_window_start="
                    '"YYYY-MM-DD", analysis_window_end="YYYY-MM-DD")'
                ),
            ).to_text()
        )
    try:
        return date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise ContractRefused(
            Refusal(
                reason=Reason.CONTRACT_INVALID,
                what=f"{start!r} to {end!r} is not a pair of dates.",
                why="the window is stored as dates, not as text.",
                detail=str(exc),
                next_call=(
                    'propose_dataset_contract(..., analysis_window_start='
                    '"2024-01-01", analysis_window_end="2024-09-30")'
                ),
            ).to_text()
        ) from exc


def _parse_expectations(raw: list[dict] | None) -> list[Expectation]:
    """The tool's list-of-objects as Expectations, refusing a bad shape (Cleanup Step 13)."""
    out: list[Expectation] = []
    for i, item in enumerate(raw or []):
        try:
            if not isinstance(item, dict):
                raise ValueError("it is not an object")
            out.append(Expectation(rule=item.get("rule", ""), reason=item.get("reason", "")))
        except Exception as exc:
            raise ContractRefused(Refusal(
                reason=Reason.CONTRACT_INVALID,
                what=f"expectation {i + 1} is not usable: {exc}",
                why="each expectation is a rule every row must satisfy, and the reason it must.",
                next_call=('propose_dataset_contract(..., expectations=[{"rule": "units > 0", '
                           '"reason": "a line sells at least one item"}])'),
            ).to_text()) from None
    return out


def _parse_exclusions(raw: list[dict] | None) -> list[Exclusion]:
    """Turn the tool's list-of-objects into Exclusions, refusing a bad shape."""
    out: list[Exclusion] = []
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict):
            raise ContractRefused(
                Refusal(
                    reason=Reason.CONTRACT_INVALID,
                    what=f"exclusion {i + 1} is not an object.",
                    why="each exclusion needs a rule and a reason.",
                    next_call=(
                        'propose_dataset_contract(..., known_exclusions=[{"rule": '
                        '"status = \'cancelled\'", "reason": "not real revenue"}])'
                    ),
                ).to_text()
            )
        try:
            out.append(
                Exclusion(
                    rule=item.get("rule", ""),
                    reason=item.get("reason", ""),
                    row_count=item.get("row_count"),
                )
            )
        except Exception as exc:
            raise ContractRefused(
                Refusal(
                    reason=Reason.CONTRACT_INVALID,
                    what=f"exclusion {i + 1} is not usable.",
                    why=(
                        "a rule with no reason is a number nobody can defend "
                        "later."
                    ),
                    detail=str(exc),
                    next_call=(
                        'propose_dataset_contract(..., known_exclusions=[{"rule": '
                        '"...", "reason": "..."}])'
                    ),
                ).to_text()
            ) from exc
    return out


def _parse_foreign_keys(raw: list[dict] | None) -> list[ForeignKey]:
    """Turn the tool's list-of-objects into ForeignKeys, refusing a bad shape.

    Same shape as `_parse_exclusions`, and the same reason: an agent passing a
    string where a list belongs should read a sentence naming the field, not a
    pydantic traceback with a `loc` tuple in it.
    """
    out: list[ForeignKey] = []
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict):
            raise ContractRefused(
                Refusal(
                    reason=Reason.CONTRACT_INVALID,
                    what=f"foreign key {i + 1} is not an object.",
                    why=(
                        "a foreign key needs the column(s) on this side and "
                        "the dataset they point at."
                    ),
                    next_call=(
                        'propose_dataset_contract(..., foreign_keys=[{"columns": '
                        '["region"], "references": "region_lookup"}])'
                    ),
                ).to_text()
            )
        try:
            out.append(
                ForeignKey(
                    columns=item.get("columns") or [],
                    references=item.get("references", ""),
                    referenced_columns=item.get("referenced_columns") or [],
                    reason=item.get("reason", ""),
                )
            )
        except Exception as exc:
            raise ContractRefused(
                Refusal(
                    reason=Reason.CONTRACT_INVALID,
                    what=f"foreign key {i + 1} is not usable.",
                    why=(
                        "a reference the checker cannot join is a check that "
                        "can only report that it did not run."
                    ),
                    detail=str(exc),
                    next_call=(
                        'propose_dataset_contract(..., foreign_keys=[{"columns": '
                        '["region"], "references": "region_lookup"}])'
                    ),
                ).to_text()
            ) from exc
    return out

def propose(
    con,
    dataset_name: str,
    *,
    grain: str | None = None,
    primary_key: list[str] | None = None,
    date_column: str | None = None,
    measures: list[str] | None = None,
    dimensions: list[str] | None = None,
    measure_definitions: dict[str, str] | None = None,
    aggregations: dict[str, str] | None = None,
    analysis_window_start: str | None = None,
    analysis_window_end: str | None = None,
    known_exclusions: list[dict] | None = None,
    caveats: list[str] | None = None,
    foreign_keys: list[dict] | None = None,
    domains: dict[str, list[str]] | None = None,
    expectations: list[dict] | None = None,
    measure_columns: dict[str, str] | None = None,
    measure_per: dict[str, list[str]] | None = None,
    ratios: dict[str, dict] | None = None,
) -> str:
    """Draft a contract and render it. Stores nothing."""
    try:
        window = _parse_window(analysis_window_start, analysis_window_end)
        exclusions = _parse_exclusions(known_exclusions)
        rules = _parse_expectations(expectations)
        keys = _parse_foreign_keys(foreign_keys)
        record = db.get_dataset(con, dataset_name)
        proposal = propose_contract(
            con,
            dataset_name,
            grain=grain,
            primary_key=primary_key,
            date_column=date_column,
            measures=measures,
            dimensions=dimensions,
            measure_definitions=measure_definitions,
            aggregations=aggregations,
            analysis_window=window,
            known_exclusions=exclusions,
            expectations=rules,
            measure_columns=measure_columns, measure_per=measure_per, ratios=ratios,
            caveats=caveats,
            foreign_keys=keys,
            domains=domains,
            loaded_at=getattr(record, "loaded_at", None) if record else None,
        )
        return proposal.to_text()
    except ContractRefused as exc:
        return str(exc)


def workspace_export_root(workspace_id: str | None) -> Path:
    """Where a workspace's contract exports go.

    The default workspace keeps docs/contracts/<dataset>.yaml, as it always has. Any other
    workspace gets docs/contracts/<workspace_id>/, so two workspaces with a dataset of the same
    name cannot overwrite one another's copy -- Track A never has two, Track B has one per
    session (C94). store.EXPORT_DIR is read at call time, so the scripts that redirect it
    (P13-O2) still decide the root.
    """
    root = Path(store.EXPORT_DIR)
    if workspace_id is None or workspace_id == DEFAULT_WORKSPACE_ID:
        return root
    return root / validate_workspace_id(workspace_id)


def confirm(
    con,
    contract_json: str,
    export_root: Path | str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Store a confirmed contract and write the version-controlled export.

    export_root, when given, is used exactly as given. Otherwise the root is the workspace's
    own, from `workspace_export_root()`.
    """
    try:
        contract = contract_from_json(contract_json)
    except ValueError as exc:
        return str(exc)
    # The engine's counts are the engine's: taken again from the table, never from the payload
    # (a model could edit them in transit, or the table could have changed since the proposal).
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                   [contract.dataset_name]).fetchone()[0]:
        contract.measured_caveats = measured_caveats.for_table(
            con, contract.dataset_name, contract.date_column)

    try:
        previous = store.current(con, contract.dataset_name)
        stored = store.confirm(con, contract)
    except ContractRefused as exc:
        return str(exc)

    export: Path | None = None
    warning = ""
    try:
        export = store.write_export(
            stored.contract,
            root=export_root if export_root is not None else workspace_export_root(workspace_id),
        )
    except Exception as exc:
        # The contract IS stored -- the database is authoritative. Losing the
        # export is a version-control inconvenience, not a reason to pretend
        # the confirmation did not happen.
        warning = (
            f"\nNOTE: the contract is stored, but the copy for version control "
            f"could not be written ({exc}). The database is authoritative; "
            f"re-confirming will try the export again."
        )

    return (
        store.summarise(
            stored,
            export=export,
            previous=previous.contract if previous else None,
        )
        + warning
    )


def workflow_state(con) -> str:
    """Where every dataset in the workspace has got to."""
    return describe_workflow_state(con)


def analyse(con, dataset_name: str, question: str | None = None) -> str:
    """
    The gate, and what is on the other side of it in Phase 4.

    Phase 8 puts real analyses here. Until then this passes the gate and
    reports what the gate found, which is not a placeholder: the refusal path
    is the whole of locked decision 12, and it has to be exercised by the
    agent rather than by a test harness.

    What comes back on success is deliberately NOT a second refusal. An agent
    that clears one gate and is immediately blocked again -- for a reason it
    cannot act on -- apologises, retries, and apologises again, which is the
    loop the Phase 4 Done-When exists to catch. So this returns a state
    report: the agreement the numbers would be computed under, everything that
    would have to be printed beside them, and a call that works today.
    """
    try:
        gate = require_contract(con, dataset_name)
    except ContractRefused as exc:
        return str(exc)

    rows, cols = db.table_shape(con, dataset_name)
    contract = gate.contract

    lines = [
        gate.header(),
        "",
        f"{dataset_name}: {rows:,} rows, {cols} columns, contract "
        f"v{gate.version}.",
    ]
    if question:
        lines.append(f"Question asked: {question}")

    lines.append("")
    lines.append("The contract in force says numbers may be built from:")
    if contract.measures:
        lines.append("| measure | agg | definition |")
        lines.append("| --- | --- | --- |")
        for m in contract.measures:
            lines.append(f"| {m.name} | {m.agg} | {m.definition} |")
    else:
        lines.append("  (no measures are defined in this contract)")
    if contract.dimensions:
        lines.append("")
        lines.append(f"grouped by: {', '.join(contract.dimensions)}")
    if contract.analysis_window:
        lines.append(
            f"over {contract.date_column} in "
            f"{contract.analysis_window.to_text()}"
        )

    if gate.caveats:
        lines.append("")
        lines.append("Every number produced here would have to carry:")
        lines += [f"  - {c}" for c in gate.caveats]

    lines.append("")
    lines.append("")
    lines.append("No analysis has been computed here. These can be:")
    for name, tier, summary in catalogue():
        lines.append(f"  {name} (tier {tier}) -- {summary}")
    lines.append(
        f'NEXT STEP: call compute_analysis(dataset_name="{dataset_name}", '
        f'analysis_type="summary_stats") for every declared measure at '
        f"once, or name another analysis from the list above."
    )
    return "\n".join(lines)


__all__ = ["analyse", "confirm", "propose", "workflow_state"]
