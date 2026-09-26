"""What an analysis was actually called with, kept so a report can say it.

The sixth of these. validate/runs.py, contract/store.py, clean/plan.py, clean/ledger.py and
profile/runs.py each keep their own runs in the workspace catalog, appended and never updated,
in a table the dataset list filters out by prefix. Until this one the analysis tier kept
nothing, which is P12-D4, and it is why the build guide's reproduction appendix could not be
written: measured on 21/09/2026, `write_result` puts the header row and the data rows on disk
and nothing else, so `top_n_20260921-124920.csv` says an analysis called top_n ran at a time and
does not say it was called with dimension="region", measure="revenue", n=3.

**The alternative was to let build_report take the calls as an argument, and it was rejected.**
The appendix would then record what the agent says it ran rather than what ran -- a claim and
the thing it describes, written separately, in the one section whose entire purpose is that
somebody else can repeat the work. The parameters are stored here because `call()` has to render
them rather than guess them.

**One table for analyses and charts.** A chart run is an analysis run that also drew something,
so the chart columns are nullable on the same row. That is also the only thing linking a PNG to
the analysis that produced it, which the report's "findings with charts" section needs as much
as the appendix needs the parameters.
"""

from __future__ import annotations

from backend.engine.util import db

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..profile.runs import BOOKKEEPING_PREFIX

ANALYSIS_TABLE = f"{BOOKKEEPING_PREFIX}analysis_runs"


def _literal(value: Any) -> str:
    """One parameter as a caller would type it.

    Strings are double-quoted because every example in server.py's docstrings is, and an
    appendix a reader retypes should match what they were told to write.
    """
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


@dataclass(frozen=True)
class AnalysisRun:
    """One analysis, as it was called and what it produced."""

    dataset_name: str
    analysis_type: str
    run_at: datetime
    params: dict[str, Any] = field(default_factory=dict)
    contract_version: int | None = None
    row_count: int = 0
    result_path: str | None = None
    chart_kind: str | None = None
    chart_path: str | None = None
    summary: list[str] = field(default_factory=list)

    @property
    def tool(self) -> str:
        """Which tool produced this. A chart run came through render_chart."""
        return "render_chart" if self.chart_kind else "compute_analysis"

    @property
    def drew(self) -> bool:
        return bool(self.chart_path)

    def call(self) -> str:
        """The exact invocation, as a caller would type it.

        This is the reproduction appendix's line, and the reason the parameters are stored
        rather than inferred from a filename.
        """
        parts = [
            f'dataset_name="{self.dataset_name}"',
            f'analysis_type="{self.analysis_type}"',
        ]
        if self.chart_kind:
            parts.append(f'chart="{self.chart_kind}"')
        parts += [f"{key}={_literal(self.params[key])}" for key in sorted(self.params)]
        return f"{self.tool}({', '.join(parts)})"

    def method_note(self) -> str | None:
        """The sentence saying what the numbers were computed over.

        Every analysis leads its summary with it and tools.py refuses a result that does not,
        so the first line is it when there is one at all.
        """
        return self.summary[0] if self.summary else None


def ensure_table(con) -> None:
    """Create the log if it is missing.

    Lazily rather than in `db.connect()`, for the reason contract/store.py records: a workspace
    that has never run an analysis should not carry an empty table, and every read path here
    calls this first anyway.
    """
    db.create_if_missing(
        con,
        f"""
        CREATE TABLE IF NOT EXISTS {ANALYSIS_TABLE} (
            dataset_name     VARCHAR NOT NULL,
            analysis_type    VARCHAR NOT NULL,
            run_at           TIMESTAMP NOT NULL,
            params_json      VARCHAR NOT NULL,
            contract_version INTEGER,
            row_count        BIGINT NOT NULL,
            result_path      VARCHAR,
            chart_kind       VARCHAR,
            chart_path       VARCHAR,
            summary_json     VARCHAR NOT NULL
        )
        """
    )


def record(
    con,
    *,
    dataset_name: str,
    analysis_type: str,
    params: dict[str, Any] | None = None,
    contract_version: int | None = None,
    row_count: int = 0,
    result_path: str | None = None,
    chart_kind: str | None = None,
    chart_path: str | None = None,
    summary: Sequence[str] = (),
    now: datetime | None = None,
) -> AnalysisRun:
    """Append one run. Never updates, never deletes.

    Called only after the analysis succeeded, so a refused call records nothing -- the appendix
    lists what ran, not what was attempted. A refusal is already a sentence the caller was
    given; it is not a step in reproducing the work.
    """
    ensure_table(con)
    run = AnalysisRun(
        dataset_name=dataset_name,
        analysis_type=analysis_type,
        run_at=now or datetime.now(),
        params=dict(params or {}),
        contract_version=contract_version,
        row_count=row_count,
        result_path=str(result_path) if result_path is not None else None,
        chart_kind=chart_kind,
        chart_path=str(chart_path) if chart_path is not None else None,
        summary=list(summary),
    )
    con.execute(
        f"INSERT INTO {ANALYSIS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            run.dataset_name, run.analysis_type, run.run_at,
            json.dumps(run.params, default=str), run.contract_version, run.row_count,
            run.result_path, run.chart_kind, run.chart_path,
            json.dumps(run.summary, default=str),
        ],
    )
    return run


def _row_to_run(row) -> AnalysisRun:
    return AnalysisRun(
        dataset_name=row[0],
        analysis_type=row[1],
        run_at=row[2],
        params=json.loads(row[3]),
        contract_version=row[4],
        row_count=row[5],
        result_path=row[6],
        chart_kind=row[7],
        chart_path=row[8],
        summary=json.loads(row[9]),
    )


_SELECT = (
    "SELECT dataset_name, analysis_type, run_at, params_json, contract_version, "
    "row_count, result_path, chart_kind, chart_path, summary_json "
    f"FROM {ANALYSIS_TABLE}"
)


def history(con, dataset_name: str) -> list[AnalysisRun]:
    """Every run against this dataset, oldest first -- the order they happened.

    Oldest first, unlike results.list_results which is newest first, because this is read as a
    sequence of steps somebody repeats rather than as a list of files to open.
    """
    ensure_table(con)
    rows = con.execute(
        f"{_SELECT} WHERE dataset_name = ? ORDER BY run_at, rowid", [dataset_name]
    ).fetchall()
    return [_row_to_run(r) for r in rows]


def latest(con, dataset_name: str) -> AnalysisRun | None:
    """The most recent run against this dataset, or None."""
    runs = history(con, dataset_name)
    return runs[-1] if runs else None


def all_runs(con) -> list[AnalysisRun]:
    """Every run in this workspace, oldest first, across datasets."""
    ensure_table(con)
    rows = con.execute(f"{_SELECT} ORDER BY run_at, rowid").fetchall()
    return [_row_to_run(r) for r in rows]


def count(con, dataset_name: str | None = None) -> int:
    ensure_table(con)
    if dataset_name is None:
        return con.execute(f"SELECT count(*) FROM {ANALYSIS_TABLE}").fetchone()[0]
    return con.execute(
        f"SELECT count(*) FROM {ANALYSIS_TABLE} WHERE dataset_name = ?", [dataset_name]
    ).fetchone()[0]


def charts(con, dataset_name: str) -> list[AnalysisRun]:
    """Only the runs that drew something, oldest first."""
    return [r for r in history(con, dataset_name) if r.drew]


__all__ = [
    "ANALYSIS_TABLE",
    "AnalysisRun",
    "ensure_table",
    "record",
    "history",
    "latest",
    "all_runs",
    "count",
    "charts",
]
