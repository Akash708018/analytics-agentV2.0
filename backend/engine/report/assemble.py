"""The report: nine sections the build guide requires, assembled from records other phases wrote.

Nothing here computes anything. Every number in a report was produced by a tool that recorded it
at the time, which is the point -- a report that recalculated its own figures could disagree with
the result files it cites, and a reader would have no way to tell which was wrong.

**A section with no record still appears, and says so.** A report that drops "cleaning ledger"
because nothing was cleaned reads as a report of a dataset that needed no cleaning, and those are
different claims. clean/ledger.describe() already works this way -- "Nothing has been cleaned.
Every table is as it was" -- and every section here follows it. Which sections were empty is
carried on the Report, so the caller is told rather than left to count headings.

**The reproduction appendix is the reason Phase 12 needed Step 2.** Until analysis/runs.py, a
result file was a table and a filename, and no record said which call produced it (P12-D3). The
appendix renders `AnalysisRun.call()` per run in the order they happened, so the section is a
list of invocations somebody can retype rather than a description of work they cannot repeat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import workspace
from ..analysis import runs as analysis_runs
from ..clean import ledger
from ..contract import store
from ..profile import runs as profile_runs
from ..util import db
from ..util.results import claim
from ..validate import runs as validation_runs

REPORTS_DIRNAME = "reports"

#: In the order the build guide lists them at line 949. The order is the contract: a reader
#: looking for the cleaning ledger should not have to search for it.
SECTIONS = (
    "The question asked",
    "Dataset and grain",
    "Data quality",
    "Cleaning ledger",
    "Validation results",
    "Findings",
    "Method notes",
    "Caveats and exclusions",
    "Reproduction appendix",
)

_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_SAFE = re.compile(r"[^A-Za-z0-9_]+")


def reports_dir(workspace_id: str) -> Path:
    """The workspace's reports directory, created on demand -- beside results/ and charts/."""
    path = workspace.workspace_dir(workspace_id) / REPORTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _anchor(heading: str) -> str:
    return heading.lower().replace(" ", "-")


def _bullets(items) -> list[str]:
    return [f"- {item}" for item in items] or ["- (none)"]


# --- the nine sections ---------------------------------------------------------------------

_TAG_OPEN = re.compile(r"<(?=[A-Za-z/!?])")
_JS_LINK = re.compile(r"\]\(\s*javascript:", re.IGNORECASE)


def defang(markdown: str) -> str:
    """The report with no markup that could run where it is opened (P14-D71).

    The question is the user's words, written as given (never paraphrased), and column names and
    values come from files anyone can make: '<script>' and '](javascript:' reached the .md a
    person downloads. A '<' that opens a tag becomes '&lt;' -- it reads the same rendered, and
    'p < 0.05' is untouched -- and a javascript: link loses its scheme.
    """
    return _JS_LINK.sub("](blocked-javascript:", _TAG_OPEN.sub("&lt;", markdown))


def _question(question: str) -> tuple[list[str], bool]:
    text = (question or "").strip()
    if not text:
        return (["No question was recorded for this report. Nothing in the workspace says why "
                 "this dataset was loaded, so it has to be given, and it was not."], True)
    return ([text], False)


def _dataset_and_grain(stored) -> tuple[list[str], bool]:
    if stored is None:
        return (["No contract has been confirmed for this dataset, so no grain has been agreed "
                 "and no analysis could have run under one."], True)
    c = stored.contract
    lines = [
        f"**Dataset:** `{c.dataset_name}`, contract v{stored.version}, "
        f"confirmed {stored.confirmed_at:%Y-%m-%d %H:%M}, bound to {stored.row_count:,} row(s).",
        "",
        f"**Grain:** {c.grain}",
        "",
        f"**Primary key:** {', '.join(f'`{k}`' for k in c.primary_key) or '(none declared)'}",
    ]
    if c.date_column:
        lines.append(f"**Date column:** `{c.date_column}`")
    window = getattr(c, "analysis_window", None)
    if window is not None:
        lines.append(f"**Analysis window:** {window.start} to {window.end}")
    lines += ["", "**Measures**"]
    lines += _bullets(
        f"`{m.name}` ({m.agg})" + (f", {m.unit}" if getattr(m, "unit", None) else "")
        + (f" -- {m.definition}" if getattr(m, "definition", None) else "")
        for m in c.measures
    )
    lines += ["", "**Dimensions**"]
    lines += _bullets(f"`{d}`" for d in c.dimensions)
    return (lines, False)


def _data_quality(run) -> tuple[list[str], bool]:
    if run is None:
        return (["This dataset has not been profiled, so nothing here describes its quality. "
                 "A report of unprofiled data is a report of numbers nobody has looked at."],
                True)
    lines = [
        f"Profiled {run.run_at:%Y-%m-%d %H:%M}: {run.row_count:,} row(s), "
        f"{run.column_count} column(s).",
        "",
    ]
    if run.duplicate_rows is not None:
        lines.append(f"- Duplicate rows: {run.duplicate_rows:,}")
    lines.append(f"- Columns holding missing values: {run.columns_missing}")
    return (lines, False)


def _cleaning(con, dataset_name: str) -> tuple[list[str], bool]:
    entries = ledger.entries(con, dataset_name)
    if not entries:
        return ([ledger.describe(con, dataset_name)], True)
    lines = [
        f"{len(entries)} action(s) applied. Every one is listed, with the statement it ran.",
        "",
    ]
    for e in entries:
        lines.append(f"- {e.line()}")
        lines.append(f"  - `{e.statement}`")
    return (lines, False)


def _validation(run) -> tuple[list[str], bool]:
    if run is None:
        return (["This dataset has not been validated against its contract. The numbers below "
                 "were computed under a contract nothing has checked the data still matches."],
                True)
    lines = [
        f"Validated {run.run_at:%Y-%m-%d %H:%M} against contract v{run.contract_version}, "
        f"over {run.row_count:,} row(s).",
        "",
        f"- Checks passed: {run.checks_passed} of {run.checks_total}",
        f"- Checks failed: {run.checks_failed}",
        f"- Checks not run: {run.checks_not_run}",
    ]
    if run.checks_failed:
        lines += ["", "**Checks failed.** Read the failures before trusting anything below."]
    return (lines, False)


#: Key findings in the reply. The report itself holds every run in section 6.
KEY_FINDINGS = 12


def _findings(runs_) -> tuple[list[str], bool, list[str]]:
    if not runs_:
        return (["No analysis has been run against this dataset, so there is nothing to "
                 "report. The sections above describe data nobody has asked a question of."],
                True, [])
    lines: list[str] = []
    # One key finding per distinct call, the latest run of it, in its first substantive line:
    # the method note is every run's first line and said only "N of M row(s) analysed", once
    # per run, repeats included -- 7,700 characters at 75 runs (P14-O23). The method notes have
    # their own section.
    latest: dict[str, str] = {}
    for i, r in enumerate(runs_, start=1):
        head = f"### {i}. `{r.analysis_type}`"
        lines.append(head)
        note = r.method_note()
        said = next((s for s in r.summary if s != note), None)
        summary = (f"{r.analysis_type}: {said}" if said
                   else f"{r.analysis_type}: {note}" if note
                   else f"{r.analysis_type}: {r.row_count:,} row(s)")
        latest.pop(r.call(), None)
        latest[r.call()] = summary
        lines.append("")
        for s in r.summary:
            lines.append(f"- {s}")
        if not r.summary:
            lines.append(f"- {r.row_count:,} row(s) in the result.")
        if r.result_path:
            lines.append(f"- Result: `{Path(r.result_path).name}`")
        if r.chart_path:
            lines.append(f"- Chart ({r.chart_kind}): `{Path(r.chart_path).name}`")
            lines.append("")
            lines.append(f"![{r.analysis_type} {r.chart_kind}]"
                         f"(../{'charts'}/{Path(r.chart_path).name})")
        lines.append("")
    key = list(latest.values())
    if len(key) > KEY_FINDINGS:
        rest = len(key) - KEY_FINDINGS
        key = key[-KEY_FINDINGS:] + [f"... and {rest:,} earlier finding(s): section 6 of the "
                                     f"report holds every run."]
    return (lines, False, key)


def _method_notes(runs_) -> tuple[list[str], bool]:
    notes = [(r.analysis_type, r.method_note()) for r in runs_ if r.method_note()]
    if not notes:
        return (["No analysis recorded a method note, which means either nothing ran or "
                 "something ran without saying what it was computed over."], True)
    lines = ["What each number was computed over, as the analysis reported it at the time.", ""]
    seen: set[tuple[str, str]] = set()
    for name, note in notes:
        if (name, note) in seen:
            continue
        seen.add((name, note))
        lines.append(f"- `{name}`: {note}")
    return (lines, False)


def _caveats(stored, narrowed: str | None = None) -> tuple[list[str], bool]:
    # A subset leads, contract or not: every caveat below qualifies numbers about the table,
    # and this one says the table is not the one the question was about (C95).
    lead = ["**Loaded as a subset**", f"- {narrowed}", ""] if narrowed else []
    if stored is None:
        return (lead + ["No contract, so no exclusions were declared and none were applied."],
                not lead)
    c = stored.contract
    caveats = list(getattr(c, "caveats", []) or [])
    exclusions = [getattr(e, "rule", str(e)) for e in (getattr(c, "known_exclusions", []) or [])]
    excluded = list(getattr(c, "excluded_columns", []) or [])
    missing = list(getattr(c, "missing_values", []) or [])
    if not (caveats or exclusions or excluded or missing):
        return (lead + ["The contract declares no caveats, no known exclusions, no excluded columns "
                 "and no missing-value markers. That is a claim, not an absence: somebody "
                 "confirmed a contract that says none apply."], not lead)
    lines: list[str] = list(lead)
    if caveats:
        lines += ["**Caveats**"] + _bullets(caveats) + [""]
    if exclusions:
        lines += (["**Rows excluded from every analysis**"]
                  + _bullets(f"`{r}`" for r in exclusions) + [""])
    if excluded:
        lines += (["**Columns excluded from the contract**"]
                  + _bullets(f"`{c_}`" for c_ in excluded) + [""])
    if missing:
        lines += ["**Read as missing**"] + _bullets(f"`{m}`" for m in missing) + [""]
    return (lines, False)


def _appendix(runs_) -> tuple[list[str], bool]:
    if not runs_:
        return (["Nothing ran, so there is nothing to reproduce."], True)
    lines = [
        "Every call that produced a number above, in the order it was made. Each line is the "
        "invocation as it was recorded, not a description of it.",
        "",
        "```python",
    ]
    lines += [r.call() for r in runs_]
    lines.append("```")
    return (lines, False)


# --- the report ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Report:
    """A report on disk, and enough to say what is in it without opening it.

    `to_text()` is what a tool returns. As with `util.results.Result` and `charts.render.Chart`,
    there is deliberately no accessor that produces just the path.
    """

    path: Path
    dataset_name: str
    question: str
    created_at: datetime
    headings: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    empty_sections: list[str] = field(default_factory=list)
    run_count: int = 0
    chart_count: int = 0

    def to_text(self) -> str:
        lines = [
            f"Report written: {self.path}",
            "",
            f"{self.dataset_name}, {len(self.headings)} section(s), "
            f"{self.run_count} analysis run(s), {self.chart_count} chart(s).",
            "",
            "Contents:",
        ]
        lines += [f"  {i}. {h}" for i, h in enumerate(self.headings, start=1)]
        lines += ["", "Key findings:"]
        lines += [f"  - {f}" for f in self.findings] or ["  - (no analysis has been run)"]
        if self.empty_sections:
            lines += [
                "",
                "Sections present with nothing to report, which is a finding of its own:",
            ]
            lines += [f"  - {s}" for s in self.empty_sections]
        return "\n".join(lines)


def assemble(
    con,
    workspace_id: str,
    *,
    dataset_name: str,
    question: str,
    now: datetime | None = None,
) -> Report:
    """Write the report and describe it. Returns a `Report`, never a path."""
    stamp = (now or datetime.now()).strftime(_STAMP_FORMAT)
    safe = _SAFE.sub("_", dataset_name).strip("_") or "dataset"
    directory = reports_dir(workspace_id)
    path = claim(directory, f"{safe}_{stamp}", ".md")    # atomic, as results (D16)

    stored = store.current(con, dataset_name)
    profile = profile_runs.latest(con, dataset_name)
    validation = validation_runs.latest(con, dataset_name)
    runs_ = analysis_runs.history(con, dataset_name)

    q_body, q_empty = _question(question)
    d_body, d_empty = _dataset_and_grain(stored)
    p_body, p_empty = _data_quality(profile)
    c_body, c_empty = _cleaning(con, dataset_name)
    v_body, v_empty = _validation(validation)
    f_body, f_empty, key = _findings(runs_)
    m_body, m_empty = _method_notes(runs_)
    record = db.get_dataset(con, dataset_name)
    x_body, x_empty = _caveats(stored, record.narrowing() if record else None)
    a_body, a_empty = _appendix(runs_)

    bodies = [q_body, d_body, p_body, c_body, v_body, f_body, m_body, x_body, a_body]
    empties = [q_empty, d_empty, p_empty, c_empty, v_empty, f_empty, m_empty, x_empty, a_empty]

    created = now or datetime.now()
    out = [
        f"# {dataset_name}",
        "",
        f"Written {created:%Y-%m-%d %H:%M}.",
        "",
        "## Contents",
        "",
    ]
    out += [f"{i}. [{h}](#{_anchor(h)})" for i, h in enumerate(SECTIONS, start=1)]
    out.append("")
    for i, (heading, body) in enumerate(zip(SECTIONS, bodies), start=1):
        out += [f"## {heading}", ""] + list(body) + [""]

    path.write_text(defang("\n".join(out) + "\n"), encoding="utf-8")

    return Report(
        path=path,
        dataset_name=dataset_name,
        question=question,
        created_at=created,
        headings=list(SECTIONS),
        findings=key,
        empty_sections=[h for h, e in zip(SECTIONS, empties) if e],
        run_count=len(runs_),
        chart_count=sum(1 for r in runs_ if r.drew),
    )


__all__ = ["REPORTS_DIRNAME", "SECTIONS", "Report", "assemble", "reports_dir"]
