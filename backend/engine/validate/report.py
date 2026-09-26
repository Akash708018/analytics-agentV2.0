"""The checks as a table somebody reads, and the sentences underneath it.

Phase 7, Step 6. The first half of the Phase 7 Done-When.

**P7-D10: the headline counts the three outcomes and never collapses them into
one word.** A dataset where five checks failed and one passed is not "FAIL", and
one where five passed and four could not run is very much not "PASS". Both of
those single words are available, both are shorter, and both throw away the
distinction P7-D7 was written to preserve -- the difference between a question
that was answered and one that was never asked. So the headline is a count, the
failures come first because that is what the reader is looking for, and NOT RUN
is named out loud rather than left as a blank row.

**Rows that could not be checked are NOT summed across checks.** On
broken_sales.csv the same six undated rows are `not_checked` for
`date.in_window` and again for `date.not_future`; adding them would report
twelve rows out of a hundred and eighty-six that do not exist. The per-check
column is the honest place for that number, and a roll-up would be a number
nothing measured -- P7-D8's rule arriving somewhere new.

**The NEXT STEP is not built here.** This module renders; it does not know what
workspace it is in, whether a result file was written, or which tools are
registered. A renderer that guesses a remedy from a failing check id would be
naming a call it cannot verify exists -- so `render()` takes the line it should
end with, and Step 7's tool supplies it.
"""

from __future__ import annotations

from ..util.formatting import format_table
from .rules import CheckResult, Outcome, Scope

DASH = "-"


def _counts(result: CheckResult) -> list[str]:
    """The four count columns, or dashes where counting rows is not the point."""
    if result.outcome is Outcome.NOT_RUN or result.scope is Scope.TABLE:
        return [DASH, DASH, DASH, DASH]
    return [
        f"{result.rows:,}",
        f"{result.passed:,}",
        f"{result.failed:,}",
        f"{result.not_checked:,}",
    ]


def tally(results: list[CheckResult]) -> dict[Outcome, int]:
    return {
        outcome: sum(1 for r in results if r.outcome is outcome)
        for outcome in Outcome
    }


def headline(dataset_name: str, results: list[CheckResult]) -> str:
    """One sentence that does not lie in either direction."""
    if not results:
        return f"{dataset_name}: nothing was checked."

    counted = tally(results)
    failed = counted[Outcome.FAIL]
    passed = counted[Outcome.PASS]
    not_run = counted[Outcome.NOT_RUN]
    total = len(results)

    if passed == total:
        return f"{dataset_name}: all {total} check(s) passed."
    if not_run == total:
        return (
            f"{dataset_name}: no check could run. All {total} say why below, "
            f"and none of them is a pass."
        )

    # "of N" attaches to whichever count is named first, so the reader gets
    # the denominator once rather than three times or not at all.
    parts = []
    for count, phrase in (
        (failed, "failed"),
        (passed, "passed"),
        (not_run, "could not run"),
    ):
        if not count:
            continue
        parts.append(
            f"{count} of {total} check(s) {phrase}" if not parts
            else f"{count} {phrase}"
        )

    line = f"{dataset_name}: {', '.join(parts)}."
    if not_run and not failed:
        line += (
            " Nothing failed, and nothing is claimed about what was not checked."
        )
    return line


def render(
    dataset_name: str,
    results: list[CheckResult],
    *,
    contract_version: int | None = None,
    note: str = "",
    next_call: str = "",
) -> str:
    """The report. Table, then the sentence behind every row of it."""
    if not results:
        return (
            f"{dataset_name}: nothing was checked. No check in this phase had "
            f"anything in the contract to run against."
        )

    lines = [headline(dataset_name, results)]
    if contract_version is not None:
        lines.append(f"Checked against contract v{contract_version}.")
    lines.append("")

    table_rows = [
        [r.title, r.subject, r.outcome.value] + _counts(r)
        for r in results
    ]
    lines.append(
        format_table(
            table_rows,
            ["check", "on", "result", "rows", "passed", "failed", "not checked"],
        )
    )

    if any(_counts(r)[0] == DASH for r in results):
        lines += [
            "",
            "A dash means the question is not about rows: a check that did not "
            "run examined none, and a table-level check has no per-row "
            "breakdown.",
        ]

    lines += ["", "What each check found:"]
    for r in results:
        lines.append(f"  {r.outcome.value}  {r.check_id} ({r.subject})")
        lines.append(f"    {r.sentence()}")
        for line in r.evidence:
            lines.append(f"      - {line}")
        if r.evidence_withheld:
            lines.append(
                f"      ({r.evidence_withheld:,} more not shown)"
            )

    unchecked = [r for r in results if r.scope is Scope.ROWS and r.not_checked]
    if unchecked:
        lines += [
            "",
            "Rows a check could not examine are counted per check and never "
            "added up: the same row can be uncheckable by more than one check, "
            "and a total would be a number nothing measured.",
        ]

    if note:
        lines += ["", note]
    if next_call:
        lines += ["", f"NEXT STEP: call {next_call}"]
    return "\n".join(lines)


__all__ = ["DASH", "headline", "render", "tally"]
