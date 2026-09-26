"""The engine behind the Track B UI: webapp/contract.Backend, implemented in-process.

Why in-process rather than MCP over HTTP (Phase 14 Step 3): every MCP tool returns text, and the
screens need structure -- grid cells, columns, which fields are still a guess. Parsing text is what
the contract forbids the UI, so the backend calls the engine's own functions and hands back the
contract's dataclasses. Tools that only exist as text-returning calls (loading a confirmed spec,
storing a contract) are called exactly as the MCP layer calls them, and their refusals converted
once, here.

Concurrency, from Phase 14 Step 1's measurements: two workspaces on two threads are independent
(P14-D1, D5); two calls on ONE workspace must not overlap (P14-D2, D6). So every engine call runs
under that workspace's lock, and different workspaces never wait for each other.

Everything the UI sends is untrusted: file names keep only their base name, paths must lie inside
the workspace's own directories, and workspace ids are validated as the path segments they are.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path

from backend.engine import server, state, workspace
from backend.engine.charts.render import CHARTS_DIRNAME
from backend.engine.config import (
    DEFAULT_WORKSPACE_ID, SIZE_GATES, WORKSPACE_ROOT, describe_size_gates, validate_workspace_id,
)
from backend.engine.clean import plan as clean_plan
from backend.engine.clean import tools as clean_tools
from backend.engine.contract import ContractRefused
from backend.engine.contract import tools as contract_tools
from backend.engine.contract.propose import propose_contract
from backend.engine.contract.refusals import reason_of
from backend.engine.ingest import csv_loader, excel, merges, preview, sizegate
from backend.engine.ingest import draft as ingest_draft
from backend.engine.ingest.csv_loader import LoadRefused
from backend.engine.ingest.postgres import ROW_REFUSE
from backend.engine.report.assemble import REPORTS_DIRNAME
from backend.engine.util import db
from backend.engine.util.results import RESULTS_DIRNAME
from backend.engine.analysis import runs as analysis_runs

from . import autofill
from .contract import (
    ActionResult, AnalysisMenu, AnalysisParam, AnalysisRun, AnalysisSpec, Artifact, ChatTurn,
    CleaningProposal, CleaningStep, ColumnDraft, ContractColumn, ContractDraft, DatasetSummary,
    FieldSource, GridPreview, IngestDraft, Limits, MeasureSuggestion, MetricProposal, Refusal,
    UploadResult,
)

UPLOADS_DIRNAME = "uploads"
_ALLOWED = (".csv", ".xlsx")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,120}$")
_ARTIFACT_DIRS = {CHARTS_DIRNAME: "chart", REPORTS_DIRNAME: "report", RESULTS_DIRNAME: "result"}
_GRID_ROWS = 40  # rows of the sheet shown in the editor
DEFAULT_TTL_HOURS = 72  # a web workspace unused this long is removed (P14-O1)
SWEEP_EVERY = 3600  # seconds between sweeps triggered by new visitors


_FIELD = re.compile(r"^measures\[(.+)\](?:\.(agg|per|definition))?$")


def _value_at(answers: dict, path: str):
    """One field of a form's answers by its path ('measures[fee].per'), normalised so a fill and
    a person's answers compare equal when they say the same thing."""
    if path == "analysis_window":
        return [answers.get("analysis_window_start"), answers.get("analysis_window_end")]
    if path in ("grain", "date_column"):
        return answers.get(path) or None
    if path == "primary_key":
        return list(answers.get("primary_key") or [])
    m = _FIELD.match(path)
    if not m:
        return None
    name, part = m.group(1), m.group(2)
    if part is None:
        return name in (answers.get("measures") or [])
    if part == "agg":
        return (answers.get("aggregations") or {}).get(name)
    if part == "per":
        return list((answers.get("measure_per") or {}).get(name) or [])
    return ((answers.get("measure_definitions") or {}).get(name) or "").strip() or None


def _ready(filled, answers: dict) -> str:
    """"Nothing in it is a guess" is false after a model's fill: say how many fields are its
    alone (live walk, 25/09/2026)."""
    if filled is None:
        return "Ready to confirm: nothing in it is a guess."
    alone = sum(1 for chk in filled.checks if chk.status == "llm_only"
                and _value_at(answers, chk.path) == _value_at(
                    RealBackend._answers_from_fill(filled), chk.path))
    return (f"Ready to confirm. {alone} field(s) are the model's alone -- marked 'from the model' "
            f"above: read those before you confirm." if alone else
            "Ready to confirm: every field was checked by the data or given by you.")


def redact(text: str) -> str:
    """A web visitor never sees where the server keeps files: an engine message that names a
    path under the workspace root shows it relative to the workspace (P14-O9)."""
    for root in {str(WORKSPACE_ROOT.resolve()), str(WORKSPACE_ROOT)}:
        text = re.sub(re.escape(root) + r"/ws_[0-9a-f]{12}/(?:uploads/)?", "", text)
        text = text.replace(root, "workspace")
    return text


# How each analysis argument appears as a form field (Explore screen, P14-D65): which widget,
# and the one line under it. Arguments not named here are free text.
_DIMENSION_ARGS = ("dimension", "rows", "columns", "column", "second_dimension", "entity")
_MEASURE_ARGS = ("measure", "against")
_PARAM_HELP = {
    "measure": "the declared measure it is about", "against": "the second declared measure",
    "dimension": "the declared dimension to group by", "rows": "down the side",
    "columns": "across the top", "column": "the column whose values are counted",
    "second_dimension": "a second dimension, instead of a measure",
    "entity": "who comes back -- a customer id, say", "grain": "the calendar period",
    "period": "the period to compare (YYYY-MM by month, YYYY-Qn by quarter, YYYY by year)",
    "baseline": "the period it is compared with, written the same way",
    "before_start": "YYYY-MM-DD", "before_end": "YYYY-MM-DD", "after_start": "YYYY-MM-DD",
    "after_end": "YYYY-MM-DD", "n": "how many groups", "limit": "how many values",
    "bins": "how many bins", "threshold": "the share to reach, 0-1",
    "confidence": "0-1, e.g. 0.95", "alpha": "significance level, e.g. 0.05",
    "power": "0-1, e.g. 0.8", "method": "auto chooses the test from the data",
}
_NUMBER_ARGS = ("n", "limit", "bins", "threshold", "confidence", "alpha", "power")
_CHOICES = {"grain": ["day", "week", "month", "quarter", "year"],
            "method": ["auto", "parametric", "rank"]}
# The chart kind each analysis is drawn as (render_chart's own docstring pairs them).
CHART_FOR = {
    "calendar_coverage": "line", "trend": "line", "seasonality": "line", "period_compare": "bar",
    "frequency": "bar", "top_n": "bar", "group_compare": "bar", "ranking_shift": "bar",
    "pareto": "bar", "cross_tab": "heatmap", "correlation": "scatter", "bivariate": "scatter",
    "distribution": "histogram", "summary_stats": "box", "outlier_detection": "box",
    "cohort_retention": "heatmap", "mix_shift": "waterfall", "growth_decomposition": "waterfall",
}


def refusal_from_text(text: str) -> Refusal:
    """Any engine refusal as the contract's Refusal.

    Two shapes exist: contract.refusals.Refusal.to_text() (BLOCKED / WHY / ... / NEXT STEP: call
    X / reason: CODE) and the older LoadRefused messages (BLOCKED / WHY? / NEXT STEP: prose). The
    fields are read from their line prefixes; a missing one is left empty rather than invented.
    """
    text = redact(text)
    what = why = step = ""
    for line in text.splitlines():
        if line.startswith("BLOCKED:"):
            what = what or line[len("BLOCKED:"):].strip()
        elif line.startswith("WHY:"):
            why = why or line[len("WHY:"):].strip()
        elif line.startswith("NEXT STEP:"):
            step = step or line[len("NEXT STEP:"):].strip()
    if step.startswith("call "):
        step = step[len("call "):]
    if not what:
        what = text.strip().splitlines()[0] if text.strip() else "refused"
    reason = reason_of(text)
    return Refusal(reason=reason.value if reason else "LOAD_REFUSED", what=what, why=why,
                   next_step=step, text=text)


def _refused(text: str) -> bool:
    return reason_of(text) is not None or text.lstrip().startswith("BLOCKED")


def _cell(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


class RealBackend:
    """Implements backend.engine.webapp.contract.Backend on the engine."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._last_sweep = 0.0

    # --- expiry (P14-O1) ---------------------------------------------------------------------

    @staticmethod
    def ttl_seconds() -> float:
        """ANALYTICS_WORKSPACE_TTL_HOURS, default 72; 0 turns expiry off."""
        raw = os.environ.get("ANALYTICS_WORKSPACE_TTL_HOURS", "").strip()
        return float(raw or DEFAULT_TTL_HOURS) * 3600

    def _lock(self, workspace_id: str) -> threading.Lock:
        with self._guard:
            return self._locks[workspace_id]

    def sweep(self, *, now: float | None = None) -> list[str]:
        """Remove web workspaces unused for longer than the TTL. One in use is skipped."""
        removed = workspace.sweep_idle(self.ttl_seconds(), now=now, lock_for=self._lock)
        with self._guard:
            for wid in removed:
                self._locks.pop(wid, None)
        return removed

    def _maybe_sweep(self) -> None:
        """At most once per SWEEP_EVERY seconds: a new visitor is when the disk grows."""
        now = time.time()
        with self._guard:
            if now - self._last_sweep < SWEEP_EVERY:
                return
            self._last_sweep = now
        self.sweep(now=now)

    # --- plumbing -------------------------------------------------------------------------

    @contextmanager
    def _workspace(self, workspace_id: str):
        """This workspace's lock, held for the whole engine call (P14-D8)."""
        validate_workspace_id(workspace_id)
        with self._lock(workspace_id):
            try:
                yield
            finally:
                # Use, recorded: reads leave no other trace on disk (step 5, M2). Only a
                # workspace that exists is marked -- looking still creates nothing (P14-D21).
                workspace.touch(workspace_id)

    @staticmethod
    def _exists(workspace_id: str) -> bool:
        """Whether this workspace has ever been written to -- checked WITHOUT creating it.

        workspace.workspace_dir and db.connect both create on touch, so a sidebar listing the
        datasets of a first visit made a directory and a database for everyone who merely opened
        the page (seen: one per server start, P14-D21). Reads of a workspace that does not exist
        answer "nothing" instead.
        """
        return (WORKSPACE_ROOT / workspace_id / "session.duckdb").is_file()

    def _uploads(self, workspace_id: str) -> Path:
        path = workspace.workspace_dir(workspace_id) / UPLOADS_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _own_upload(self, workspace_id: str, path: str) -> Path | None:
        """The path, if and only if it is a file in this workspace's uploads directory."""
        root = self._uploads(workspace_id).resolve()
        candidate = Path(path).resolve()
        if candidate.parent != root or not candidate.is_file():
            return None
        return candidate

    @staticmethod
    def _not_an_upload(path: str) -> Refusal:
        return refusal_from_text(
            f"BLOCKED: {Path(path).name or path} is not a file uploaded to this workspace.\n"
            f"WHY: drafts and loads read only what was uploaded here.\n"
            f'NEXT STEP: call save_upload(filename="...") and use the path it returns.')

    # --- Backend ---------------------------------------------------------------------------

    def new_workspace_id(self) -> str:
        self._maybe_sweep()
        wid = f"ws_{secrets.token_hex(6)}"
        assert wid != DEFAULT_WORKSPACE_ID  # Claude Desktop's process holds "local" (P14-D4)
        return validate_workspace_id(wid)

    def limits(self) -> Limits:
        return Limits(csv_max_bytes=SIZE_GATES.csv_refuse_bytes,
                      excel_max_bytes=SIZE_GATES.excel_refuse_bytes,
                      postgres_max_rows=ROW_REFUSE,
                      description=describe_size_gates())

    @staticmethod
    def safe_name(filename: str) -> str:
        """The upload's base name with accents folded and anything else unsafe replaced:
        'Ventes 2024 (été).csv' -> 'Ventes 2024 _ete.csv'. It used to be refused (P14-D64)."""
        name = Path(filename).name
        folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        stem, dot, ext = folded.rpartition(".")
        stem = re.sub(r"[^A-Za-z0-9._ -]", "_", stem if dot else folded).strip(" ._") or "upload"
        return f"{stem[:110]}.{ext.lower()}" if dot else stem[:120]

    def save_upload(self, workspace_id: str, filename: str, data: bytes) -> UploadResult:
        name = self.safe_name(filename)
        if not _SAFE_NAME.match(name) or not name.lower().endswith(_ALLOWED):
            r = refusal_from_text(
                f"BLOCKED: {name!r} is not a CSV or .xlsx file name this server will store.\n"
                f"WHY: only .csv and .xlsx have a loader, and a name becomes a file on disk.\n"
                f'NEXT STEP: call save_upload(filename="sales.csv") with a plain name.')
            return UploadResult(verdict="REFUSE", message=r.what, refusal=r)
        cap = (SIZE_GATES.excel_refuse_bytes if name.lower().endswith(".xlsx")
               else SIZE_GATES.csv_refuse_bytes)
        if len(data) > cap:
            r = refusal_from_text(
                f"BLOCKED: {name} is {len(data):,} bytes, over this server's {cap:,}-byte limit.\n"
                f"WHY: a file that size would not fit in memory here.\n"
                f"NEXT STEP: call save_upload(...) with a smaller extract.")
            return UploadResult(verdict="REFUSE", message=r.what, refusal=r)
        with self._workspace(workspace_id):
            target = self._uploads(workspace_id) / name
            target.write_bytes(data)
            gate = sizegate.check_file(target)
            if not gate.allowed:
                target.unlink(missing_ok=True)
                r = refusal_from_text(gate.message)
                return UploadResult(verdict="REFUSE", message=r.what or gate.message, refusal=r)
        verdict = "WARN" if gate.verdict.value == "WARN" else "OK"
        message = gate.message if verdict == "WARN" else f"{name} saved."
        return UploadResult(verdict=verdict, message=message, path=str(target))

    def draft_ingest(self, workspace_id, path, *, dataset_name=None, sheet=None, header_rows=None,
                     header_join=None, authorised_fill=False) -> IngestDraft:
        empty = GridPreview(rows=[], first_row_number=1, sheet=None, sheet_names=[])
        with self._workspace(workspace_id):
            p = self._own_upload(workspace_id, path)
            if p is None:
                r = self._not_an_upload(path)
                return IngestDraft(spec={}, grid=empty, dataset_name="", header_rows=[],
                                   data_start_row=0, footer_skip_rows=0, header_join="space",
                                   columns=[], message=r.what, refusal=r)
            try:
                d = ingest_draft.draft_for_path(p, dataset_name=dataset_name, sheet=sheet,
                                         header_rows=header_rows, header_join=header_join,
                                         authorised_fill=authorised_fill)
                grid = self._grid(p, d)
            except Exception as exc:  # noqa: BLE001 - no file may crash the Upload screen
                # LoadRefused is the engine's refusal; anything else -- a ValueError from a file
                # the preview cannot read (P14-O7), a BadZipFile from a workbook that is not one
                # (P14-D70) -- is turned into one here.
                text = str(exc) if str(exc).startswith("BLOCKED") else (
                    f"BLOCKED: {Path(path).name} could not be read into a draft.\n"
                    f"WHY: {exc}\nNEXT STEP: call save_upload(...) with the file re-exported.")
                r = refusal_from_text(text)
                return IngestDraft(spec={}, grid=empty, dataset_name="", header_rows=[],
                                   data_start_row=0, footer_skip_rows=0, header_join="space",
                                   columns=[], message=r.what, refusal=r)
        spec = d.spec
        if spec is None:
            r = refusal_from_text(
                "BLOCKED: this file could not be read into a draft.\n"
                f"WHY: {' '.join(d.guess.questions or d.guess.reasons) or 'no header was found'}\n"
                f'NEXT STEP: call draft_ingest(path="{path}", header_rows=[...]) naming the header.')
            return IngestDraft(spec={}, grid=grid, dataset_name="", header_rows=[],
                               data_start_row=0, footer_skip_rows=0, header_join="space",
                               columns=[], questions=list(d.guess.questions), message=r.what,
                               refusal=r)
        notes = [d.pivot.message] if d.pivot.is_pivot_dump and d.pivot.message else []
        message = ("PROVISIONAL -- " + "; ".join(spec.unresolved) + " still a guess."
                   if spec.unresolved else
                   "Ready to load: every field was read from the file or answered by you.")
        return IngestDraft(
            spec=spec.model_dump(mode="json"), grid=grid, dataset_name=spec.dataset_name,
            header_rows=list(spec.header_rows), data_start_row=spec.data_start_row,
            footer_skip_rows=spec.footer_skip_rows, header_join=spec.header_join,
            columns=[ColumnDraft(c.source_name, c.target_name, c.dtype) for c in spec.columns],
            assumptions=list(spec.assumptions) + notes, questions=list(spec.questions),
            unresolved=list(spec.unresolved), message=message)

    @staticmethod
    def _grid(p: Path, d: ingest_draft.Draft) -> GridPreview:
        """The top of the sheet exactly as it sits there, before any interpretation."""
        if d.source_type == "excel":
            raw = excel.preview_rows(p, d.sheet, n=_GRID_ROWS)
            merged = merges.merged_ranges(str(p), d.sheet)
        else:
            raw = preview.parse_csv_preview(csv_loader.preview_lines(p, n=_GRID_ROWS))
            merged = []
        rows = [[_cell(v) for v in r] for r in raw]
        width = max((len(r) for r in rows), default=0)
        rows = [r + [None] * (width - len(r)) for r in rows]
        return GridPreview(rows=rows, first_row_number=1, sheet=d.sheet,
                           sheet_names=list(d.sheets), merged_ranges=list(merged))

    def confirm_ingest(self, workspace_id: str, spec: dict) -> ActionResult:
        with self._workspace(workspace_id):
            if self._own_upload(workspace_id, str(spec.get("path", ""))) is None:
                r = self._not_an_upload(str(spec.get("path", "")))
                return ActionResult(ok=False, message=r.what, refusal=r)
            text = server.confirm_ingest_spec(spec_json=json.dumps(spec),
                                              workspace_id=workspace_id)
        if _refused(text):
            r = refusal_from_text(text)
            return ActionResult(ok=False, message=r.what, refusal=r)
        return ActionResult(ok=True, message=text)

    def list_datasets(self, workspace_id: str) -> list[DatasetSummary]:
        with self._workspace(workspace_id):
            if not self._exists(workspace_id):
                return []
            con = db.connect(workspace_id)
            try:
                states = state.dataset_states(con)
            finally:
                con.close()
        out = []
        for s in states:
            notes = list(s.notes) + ([f"Blocked: {s.blocked_by}"] if s.blocked_by else [])
            out.append(DatasetSummary(
                name=s.dataset_name, rows=s.row_count, columns=s.column_count, source=s.source,
                loaded=s.age_phrase, stage=s.stage,
                next_step=s.next_call or f'run_analysis(dataset_name="{s.dataset_name}")',
                notes=notes))
        return out

    # --- contracts ---------------------------------------------------------------------

    @staticmethod
    def _window(start: str | None, end: str | None):
        if start and end:
            return (_dt.date.fromisoformat(start), _dt.date.fromisoformat(end))
        return None

    def _propose(self, con, dataset_name: str, **answers):
        record = db.get_dataset(con, dataset_name)
        return propose_contract(
            con, dataset_name,
            grain=answers.get("grain"), primary_key=answers.get("primary_key"),
            date_column=answers.get("date_column"), measures=answers.get("measures"),
            dimensions=answers.get("dimensions"),
            measure_definitions=answers.get("measure_definitions"),
            aggregations=answers.get("aggregations"),
            analysis_window=self._window(answers.get("analysis_window_start"),
                                         answers.get("analysis_window_end")),
            caveats=answers.get("caveats"),
            measure_per=answers.get("measure_per") or None,
            ratios=answers.get("ratios") or None,
            measure_columns=answers.get("measure_columns") or None,
            loaded_at=getattr(record, "loaded_at", None) if record else None)

    @staticmethod
    def _answers_from_fill(filled) -> dict:
        """contract/llm_filter.py's answers in the shape the form sends."""
        a = dict(filled.answers)
        window = a.pop("analysis_window", None)
        a["analysis_window_start"] = window[0].isoformat() if window else None
        a["analysis_window_end"] = window[1].isoformat() if window else None
        return a

    @staticmethod
    def _sources(filled, answers: dict) -> dict[str, FieldSource]:
        """Each filled field's verdict -- or `user`, where the answer now differs from the fill.
        The person's change is the authority; what the model and the data said is kept beside it."""
        if filled is None:
            return {}
        given = RealBackend._answers_from_fill(filled)
        out: dict[str, FieldSource] = {}
        for chk in filled.checks:
            now, then = _value_at(answers, chk.path), _value_at(given, chk.path)
            if now == then:
                out[chk.path] = FieldSource(chk.status, chk.reason, chk.llm, chk.confidence,
                                            chk.rule)
            else:
                out[chk.path] = FieldSource(
                    "user", f"you changed it from {then!r}" + (
                        f" (the model said {chk.llm!r}; {chk.status})" if chk.llm is not None
                        else ""), chk.llm, chk.confidence, chk.rule)
        return out

    def refill_contract(self, workspace_id: str, dataset_name: str) -> None:
        with self._workspace(workspace_id):
            autofill.forget(workspace_id, dataset_name)

    def draft_contract(self, workspace_id, dataset_name, *, grain=None, primary_key=None,
                       date_column=None, measures=None, dimensions=None, aggregations=None,
                       measure_definitions=None, analysis_window_start=None,
                       analysis_window_end=None, caveats=None, measure_per=None,
                       ratios=None, measure_columns=None) -> ContractDraft:
        answers = dict(grain=grain, primary_key=primary_key, date_column=date_column,
                       measures=measures, dimensions=dimensions, aggregations=aggregations,
                       measure_definitions=measure_definitions,
                       analysis_window_start=analysis_window_start,
                       analysis_window_end=analysis_window_end, caveats=caveats,
                       measure_per=measure_per, ratios=ratios, measure_columns=measure_columns)
        in_force = None
        fill = None
        try:
            with self._workspace(workspace_id):
                con = db.connect(workspace_id)
                try:
                    proposal = None
                    fresh = all(v is None for v in answers.values())
                    if fresh:
                        proposal, in_force = self._draft_in_force(con, dataset_name)
                    if proposal is None:
                        record = db.get_dataset(con, dataset_name)
                        loaded = getattr(record, "loaded_at", None) if record else None
                        if fresh:
                            # The model fills the form once per table; the data filters it
                            # (decided with the user, 25/09/2026). A failure falls back to the
                            # data's own suggestions, below.
                            fill = autofill.fill(con, workspace_id, dataset_name,
                                                 loaded_at=loaded)
                            if fill.filled is not None:
                                answers = self._answers_from_fill(fill.filled)
                        elif autofill.kept(workspace_id, dataset_name):
                            fill = autofill.fill(con, workspace_id, dataset_name,
                                                 loaded_at=loaded, providers=[])
                        proposal = self._propose(con, dataset_name, **answers)
                finally:
                    con.close()
        except (ContractRefused, ValueError) as exc:
            r = refusal_from_text(str(exc))
            return ContractDraft(dataset_name, None, [], None, [], [], {}, {}, None, None, [], [],
                                 message=r.what, refusal=r)
        c = proposal.contract
        open_ = set(c.unresolved)
        # A field still named in `unresolved` reaches the form BLANK. Measured: with grain
        # unresolved the contract holds the guess "one row = one order_id"; prefilled and sent
        # back, the engine's guess would be confirmed as the person's statement (P14-D18).
        roles = {name: "measure" for name in (m.name for m in c.measures)}
        roles.update({name: "dimension" for name in c.dimensions})
        if "primary_key" not in open_:
            roles.update({name: "key" for name in c.primary_key})
        if c.date_column and "date_column" not in open_:
            roles[c.date_column] = "date"
        columns = [ContractColumn(
            name=e.name, dtype=e.dtype, suggested_role=roles.get(e.name, "ignore"),
            distinct_count=e.distinct, null_count=e.row_count - e.non_null,
            sample_values=[v for v in (e.min_value, e.max_value) if v is not None])
            for e in proposal.evidence.columns]
        window = c.analysis_window if "analysis_window" not in open_ else None
        return ContractDraft(
            dataset_name=dataset_name,
            grain=None if "grain" in open_ else c.grain,
            primary_key=[] if "primary_key" in open_ else list(c.primary_key),
            date_column=None if "date_column" in open_ else c.date_column,
            measures=[m.name for m in c.measures], dimensions=list(c.dimensions),
            aggregations={m.name: m.agg for m in c.measures
                          if m.agg and f"measures[{m.name}].agg" not in open_},
            measure_definitions={m.name: m.definition for m in c.measures
                                 if m.definition and f"measures[{m.name}].definition" not in open_},
            analysis_window_start=window.start.isoformat() if window else None,
            analysis_window_end=window.end.isoformat() if window else None,
            caveats=list(c.caveats), columns=columns, provisional=list(c.unresolved),
            questions=list(c.questions), evidence=list(proposal.notes),
            suggestions={name: MeasureSuggestion(
                agg=sg.agg, strength=sg.strength, reason=sg.reason, rule=sg.rule,
                per=list(sg.per), question=sg.question(), ratio=sg.ratio)
                for name, sg in proposal.suggestions.items()},
            measure_per={m.name: list(m.per) for m in c.measures if m.per},
            ratios={m.name: {"numerator": list(m.numerator), "denominator": list(m.denominator),
                             "scale": m.scale} for m in c.measures if m.agg == "ratio"},
            measured_caveats=list(c.measured_caveats),
            measure_columns={m.name: m.column for m in c.measures
                             if m.column and m.column != m.name},
            sources=self._sources(fill.filled if fill else None, answers),
            filled_by=fill.model if fill and fill.filled else "",
            fill_note=fill.note if fill else "",
            message=("PROVISIONAL -- settle what is listed before confirming." if c.unresolved
                     else f"Contract v{in_force} is in force, as shown. Change a field and "
                          f"confirm to agree v{in_force + 1}." if in_force
                     else _ready(fill.filled if fill else None, answers)))

    def _draft_in_force(self, con, dataset_name: str):
        """The contract in force, drafted from its own fields: (proposal, version), or (None, None).

        A fresh session -- every page reload -- asks for a draft with no answers, and got one
        built from the evidence alone: a blank, PROVISIONAL form under a confirmed contract
        (P14-D68). None when there is no contract, or when the table has drifted so the stored
        fields no longer draft cleanly -- the evidence-only draft is then the honest one.
        """
        from backend.engine.contract import store as contract_store

        stored = contract_store.current(con, dataset_name)
        if stored is None:
            return None, None
        c = stored.contract
        window = getattr(c, "analysis_window", None)
        try:
            proposal = self._propose(
                con, dataset_name, grain=c.grain, primary_key=list(c.primary_key) or None,
                date_column=c.date_column, measures=[m.name for m in c.measures],
                dimensions=list(c.dimensions),
                aggregations={m.name: m.agg for m in c.measures if m.agg},
                measure_definitions={m.name: m.definition for m in c.measures if m.definition},
                analysis_window_start=window.start.isoformat() if window else None,
                analysis_window_end=window.end.isoformat() if window else None,
                caveats=list(c.caveats),
                measure_per={m.name: list(m.per) for m in c.measures if m.per},
                ratios={m.name: {"numerator": list(m.numerator),
                                 "denominator": list(m.denominator), "scale": m.scale}
                        for m in c.measures if m.agg == "ratio"},
                measure_columns={m.name: m.column for m in c.measures
                                 if m.column and m.column != m.name})
        except (ContractRefused, ValueError):
            return None, None
        if proposal.contract.unresolved:
            return None, None
        return proposal, stored.version

    def confirm_contract(self, workspace_id: str, draft: ContractDraft) -> ActionResult:
        if draft.provisional:
            r = refusal_from_text(
                "BLOCKED: this contract is still PROVISIONAL.\n"
                f"WHY: still a guess or a blank: {', '.join(draft.provisional)}.\n"
                f'NEXT STEP: call draft_contract(dataset_name="{draft.dataset_name}", ...) '
                f"with the answers.")
            return ActionResult(ok=False, message=r.what, refusal=r)
        # The engine decides, not the draft object: propose again from the draft's own fields
        # and store that proposal's contract only if it is confirmable.
        with self._workspace(workspace_id):
            con = db.connect(workspace_id)
            try:
                try:
                    proposal = self._propose(
                        con, draft.dataset_name, grain=draft.grain,
                        primary_key=draft.primary_key or None, date_column=draft.date_column,
                        measures=draft.measures, dimensions=draft.dimensions,
                        aggregations=draft.aggregations,
                        measure_definitions=draft.measure_definitions,
                        analysis_window_start=draft.analysis_window_start,
                        analysis_window_end=draft.analysis_window_end, caveats=draft.caveats,
                        measure_per=draft.measure_per, ratios=draft.ratios,
                        measure_columns=draft.measure_columns)
                except (ContractRefused, ValueError) as exc:
                    r = refusal_from_text(str(exc))
                    return ActionResult(ok=False, message=r.what, refusal=r)
                if proposal.needs_answer:
                    r = refusal_from_text(
                        "BLOCKED: the engine still finds this contract PROVISIONAL.\n"
                        f"WHY: {', '.join(proposal.contract.unresolved)}.\n"
                        f'NEXT STEP: call draft_contract(dataset_name="{draft.dataset_name}", '
                        f"...) with the answers.")
                    return ActionResult(ok=False, message=r.what, refusal=r)
                # The export goes into the session's own workspace, not docs/contracts: that copy
                # exists to version the canonical contract, and a browser session is not one --
                # exporting there would dirty the repository with every visitor (P14-D19).
                # Who decided each field travels with the contract: the model, the data, or the
                # person -- how the model chose, countable later (scripts/autofill_report.py).
                proposal.contract.provenance = {
                    path: {**asdict(src), "model": draft.filled_by}
                    for path, src in draft.sources.items()}
                text = contract_tools.confirm(
                    con, proposal.contract.model_dump_json(),
                    export_root=workspace.workspace_dir(workspace_id) / "contracts",
                    workspace_id=workspace_id)
            finally:
                con.close()
        if _refused(text):
            r = refusal_from_text(text)
            return ActionResult(ok=False, message=r.what, refusal=r)
        return ActionResult(ok=True, message=text)

    # --- cleaning (P14-O2) -----------------------------------------------------------------

    def propose_cleaning(self, workspace_id: str, dataset_name: str) -> CleaningProposal:
        """The engine's own proposal, then its stored plan read back as structure: the steps a
        screen shows are the steps the ids resolve against, never a parse of the text."""
        with self._workspace(workspace_id):
            if not self._exists(workspace_id):
                text = clean_tools._not_loaded_text(dataset_name)
            else:
                text = clean_tools.propose_cleaning_plan(workspace_id, dataset_name)
            if _refused(text):
                r = refusal_from_text(text)
                return CleaningProposal(dataset_name, 0, [], message=r.what, refusal=r)
            con = db.connect(workspace_id)
            try:
                rows = con.execute(f'SELECT count(*) FROM "{dataset_name}"').fetchone()[0]
                stored = clean_plan.latest(con, dataset_name)
            finally:
                con.close()
        if text.startswith("Nothing to clean") or stored is None:
            return CleaningProposal(dataset_name, rows, [], message=text)
        suggested = {a.action_id for a in clean_tools._safe_suggestion(stored.actions)}
        steps = [CleaningStep(
            action_id=a.action_id, kind=a.kind.value, column=a.column, intent=a.intent,
            sql=a.sql, rows_affected=a.rows_affected, values_lost=a.values_lost,
            loss_unit=a.loss_unit, sample=list(a.sample), lossy=a.is_lossy,
            suggested=a.action_id in suggested) for a in stored.actions]
        return CleaningProposal(dataset_name, rows, steps, message=text)

    def apply_cleaning(self, workspace_id: str, dataset_name: str,
                       approved_action_ids: list[str]) -> ActionResult:
        with self._workspace(workspace_id):
            text = clean_tools.apply_cleaning_plan(workspace_id, dataset_name,
                                                   list(approved_action_ids))
        if _refused(text):
            r = refusal_from_text(text)
            return ActionResult(ok=False, message=r.what, refusal=r)
        return ActionResult(ok=True, message=text)

    # --- Explore: analyses with no model in between (P14-D65) ------------------------------

    def analysis_menu(self, workspace_id: str, dataset_name: str) -> AnalysisMenu:
        import inspect

        from backend.engine.analysis import registry
        from backend.engine.contract import store as contract_store

        with self._workspace(workspace_id):
            stored = None
            if self._exists(workspace_id):
                con = db.connect(workspace_id)
                try:
                    stored = contract_store.current(con, dataset_name)
                    if stored is not None:
                        defaults = self._form_defaults(con, dataset_name, stored.contract)
                finally:
                    con.close()
        if stored is None:
            r = refusal_from_text(
                f"BLOCKED: {dataset_name} has no confirmed Dataset Contract.\n"
                f"WHY: no number is computed until what a row is and what each measure means "
                f"has been agreed.\n"
                f'NEXT STEP: call confirm_dataset_contract(dataset_name="{dataset_name}") '
                f"on the Contract screen.\n\nreason: NO_CONTRACT")
            return AnalysisMenu(dataset_name, [], refusal=r)
        c = stored.contract
        measures = [m.name for m in c.measures]
        # A measure declared agg='none' has no total per period: offered last as `against`.
        additive = [m.name for m in c.measures if (m.agg or "none") != "none"]
        dims = defaults["dims"]
        specs = []
        for name, a in registry.REGISTRY.items():
            fields = []
            for p in list(inspect.signature(a.run).parameters.values())[3:]:
                if p.kind is p.VAR_KEYWORD:
                    continue
                required = p.default is inspect.Parameter.empty
                kind, opts, default = "text", [], defaults.get(p.name)
                if p.name == "measure":
                    kind, opts = "measure", measures
                elif p.name == "against":
                    kind = "measure"
                    opts = [m for m in additive[1:] + additive[:1]] + \
                        [m for m in measures if m not in additive]
                elif p.name == "entity":
                    kind, opts = "dimension", list(reversed(dims))  # most distinct first
                elif p.name == "column":
                    kind, opts = "dimension", dims + measures
                elif p.name in ("columns", "second_dimension"):
                    kind, opts = "dimension", dims[1:] + dims[:1]
                elif p.name in _DIMENSION_ARGS:
                    kind, opts = "dimension", dims
                elif p.name == "period" and name == "cohort_retention":
                    # Here `period` is the cohort's grain, not a date (its signature says so).
                    kind, opts, default = "choice", ["week", "month", "quarter"], "month"
                elif p.name in _CHOICES:
                    kind, opts = "choice", _CHOICES[p.name]
                    default = "month" if p.name == "grain" else None
                elif p.name in _NUMBER_ARGS:
                    kind = "number"
                if kind in ("measure", "dimension") and opts:
                    default = opts[0]
                # An optional argument stays the engine's to default -- except grain, and measure,
                # which the tests of tier 6 take as one of "measure or second_dimension".
                if not required and p.name not in ("grain", "measure"):
                    default = None
                fields.append(AnalysisParam(p.name, kind, required, list(opts), default,
                                            _PARAM_HELP.get(p.name, "")))
            specs.append(AnalysisSpec(name, a.tier, a.summary, fields, CHART_FOR.get(name)))
        return AnalysisMenu(dataset_name, specs)

    @staticmethod
    def _form_defaults(con, dataset_name: str, contract) -> dict:
        """Values that make each analysis runnable as it first appears: the dimension with the
        fewest groups first (the group caps refuse a 60-customer one), the last two months as
        period and baseline, the analysis window's two halves as before and after."""
        q = lambda n: '"' + n.replace('"', '""') + '"'  # noqa: E731
        dims = list(contract.dimensions)
        if dims:
            counts = con.execute("SELECT " + ", ".join(f"count(DISTINCT {q(d)})" for d in dims)
                                 + f" FROM {q(dataset_name)}").fetchone()
            dims = [d for _, d in sorted(zip(counts, dims))]
        out: dict = {"dims": dims}
        date = contract.date_column
        if date:
            months = [r[0] for r in con.execute(
                f"SELECT DISTINCT strftime({q(date)}, '%Y-%m') AS m FROM {q(dataset_name)} "
                f"WHERE {q(date)} IS NOT NULL ORDER BY m").fetchall()]
            window = getattr(contract, "analysis_window", None)
            if window:
                # The calendar an analysis accepts is the window's; data past it made the
                # defaults '2025-01' and the form's own Run a refusal (P14-O14).
                lo, hi = window.start.strftime("%Y-%m"), window.end.strftime("%Y-%m")
                months = [m for m in months if lo <= m <= hi]
            if len(months) >= 2:
                out["period"], out["baseline"] = months[-1], months[-2]
        window = getattr(contract, "analysis_window", None)
        if window:
            mid = window.start + (window.end - window.start) / 2
            out.update(before_start=window.start.isoformat(), before_end=mid.isoformat(),
                       after_start=(mid + _dt.timedelta(days=1)).isoformat(),
                       after_end=window.end.isoformat())
        return out

    def run_analysis(self, workspace_id: str, dataset_name: str, analysis_type: str,
                     params: dict, chart: str | None = None) -> AnalysisRun:
        from backend.engine.analysis import tools as analysis_tools

        args = {k: v for k, v in params.items() if v not in (None, "")}
        # The boundary checks the names: compute_analysis's own signature refused 'colour' with a
        # TypeError that escaped to the screen (P14-D69).
        import inspect
        accepted = [p for p in inspect.signature(server.compute_analysis).parameters
                    if p not in ("dataset_name", "analysis_type", "workspace_id")]
        unknown = sorted(set(args) - set(accepted))
        if unknown:
            return AnalysisRun("", refusal=refusal_from_text(
                f"BLOCKED: {analysis_type} was called with {', '.join(unknown)}, which no "
                f"analysis takes.\nWHY: every argument is a column, a period or a number the "
                f"analysis names; accepted: {', '.join(accepted)}.\n"
                f'NEXT STEP: call run_analysis(dataset_name="{dataset_name}", '
                f'analysis_type="{analysis_type}", ...) with those names.'
                f"\n\nreason: ANALYSIS_PARAMS_INVALID"))
        before = {a.path for a in self.list_artifacts(workspace_id)}
        with self._workspace(workspace_id):
            text = server.compute_analysis(dataset_name=dataset_name,
                                           analysis_type=analysis_type,
                                           workspace_id=workspace_id, **args)
            if _refused(text):
                return AnalysisRun("", refusal=refusal_from_text(text))
            note = ""
            if chart:
                con = db.connect(workspace_id)
                try:
                    drawn = analysis_tools.render_chart(
                        con, workspace_id, dataset_name, analysis_type, chart,
                        pick_y=True, **args)
                finally:
                    con.close()
                if _refused(drawn):
                    note = ("\n\n*No chart: " + refusal_from_text(drawn).why
                            + " The table above is the answer.*")
        new = [a for a in self.list_artifacts(workspace_id) if a.path not in before]
        return AnalysisRun(redact(text) + note, new)

    def build_report(self, workspace_id: str, dataset_name: str, question: str) -> AnalysisRun:
        before = {a.path for a in self.list_artifacts(workspace_id)}
        with self._workspace(workspace_id):
            text = server.build_report(dataset_name=dataset_name, question=question,
                                       workspace_id=workspace_id)
        if _refused(text):
            return AnalysisRun("", refusal=refusal_from_text(text))
        new = [a for a in self.list_artifacts(workspace_id) if a.path not in before]
        return AnalysisRun(redact(text), new)

    # --- chat, files, reset --------------------------------------------------------------

    def chat(self, workspace_id: str, history: list[dict], message: str,
             progress=None) -> ChatTurn:
        """One turn of the agent loop (webapp/agent.py). The workspace lock is taken per tool
        call, not for the turn, so the model's thinking time never blocks the sidebar.
        `progress` hears what the turn is waiting on as it happens (step 2)."""
        validate_workspace_id(workspace_id)
        from . import agent
        return agent.answer(workspace_id, history, message,
                            lock=lambda: self._workspace(workspace_id),
                            list_artifacts=lambda: self.list_artifacts(workspace_id),
                            progress=progress)

    def list_artifacts(self, workspace_id: str) -> list[Artifact]:
        with self._workspace(workspace_id):
            if not self._exists(workspace_id):
                return []
            root = workspace.workspace_dir(workspace_id)
            described: dict[str, str] = {}
            con = db.connect(workspace_id)
            try:
                for d in db.list_datasets(con):
                    for run in analysis_runs.history(con, d.dataset_name):
                        text = " ".join(run.summary)
                        for p in (run.chart_path, run.result_path):
                            if p:
                                described[str(Path(p).resolve())] = text
            finally:
                con.close()
            found = []
            for dirname, kind in _ARTIFACT_DIRS.items():
                folder = root / dirname
                if not folder.is_dir():
                    continue
                for f in folder.iterdir():
                    if f.is_file() and not f.name.startswith("."):
                        found.append((f.stat().st_mtime, Artifact(
                            kind=kind, path=f"{dirname}/{f.name}",
                            title=f.stem.replace("_", " "),
                            created=_dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(
                                timespec="seconds"),
                            description=described.get(str(f.resolve()), ""))))
        return [a for _, a in sorted(found, key=lambda t: t[0], reverse=True)]

    def read_artifact(self, workspace_id: str, path: str) -> bytes:
        with self._workspace(workspace_id):
            root = workspace.workspace_dir(workspace_id).resolve()
            target = (root / path).resolve()
            if (target.parent.parent != root or target.parent.name not in _ARTIFACT_DIRS
                    or not target.is_file()):
                raise ValueError(f"{path} is not an artifact of this workspace")
            return target.read_bytes()

    def pending_metrics(self, workspace_id: str) -> list[MetricProposal]:
        from backend.engine.contract import provisional

        validate_workspace_id(workspace_id)
        with self._workspace(workspace_id):
            if not self._exists(workspace_id):
                return []
            return [MetricProposal(p.id, p.dataset_name, p.name, p.formula(), p.definition,
                                   provisional.describe(p)) for p in provisional.pending(workspace_id)]

    def decide_metric(self, workspace_id: str, proposal_id: str, approve: bool) -> ActionResult:
        from backend.engine.contract import provisional

        validate_workspace_id(workspace_id)
        with self._workspace(workspace_id):
            try:
                p = provisional.decide(workspace_id, proposal_id, approve)
            except provisional.ProposalError as exc:
                return ActionResult(ok=False, message=str(exc))
        if not approve:
            return ActionResult(ok=True, message=f"**{p.name}** was rejected; nothing uses it.")
        return ActionResult(ok=True, message=(
            f"**{p.name}** is approved: {p.formula()}. Ask again and the assistant computes with "
            f"it; every result says it is provisional, not in the contract."))

    def reset_workspace(self, workspace_id: str) -> ActionResult:
        with self._workspace(workspace_id):
            removed = workspace.reset(workspace_id)
        return ActionResult(ok=True, message=(
            f"The workspace is empty: {removed['removed_files']} file(s) removed."
            if removed["existed"] else "The workspace was already empty."))


__all__ = ["RealBackend", "refusal_from_text"]
