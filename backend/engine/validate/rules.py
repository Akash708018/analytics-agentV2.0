"""One check, one row of the report, and four numbers that add up.

Phase 7, Step 4. The first code in `validate/`.

**P7-D4 is structural here rather than documented.** Every check reports
`passed`, `failed` and `not_checked`, and `CheckResult` refuses to construct
unless the three sum to `rows`. A predicate does not see NULLs -- Step 1
measured that on a window, a range and a domain -- so a check that reports only
what its predicate saw describes some of its rows and calls the rest a pass.
Making the sum a constructor invariant is the same move `Refusal` makes with
parentheses in `next_call` and `DatasetContract` makes with blanks not listed in
`unresolved`: the guarantee is not a line in a docstring that a later check can
forget.

**P7-D7: a check that could not run is not a check that passed.** A dataset with
no primary key declared has nothing to verify a key against, and reporting PASS
would be a lie told in the safest-looking direction. `not_run_because` carries
the sentence, the outcome renders as NOT RUN, and the counts are all zero --
because zero rows were examined, and pretending otherwise is how a validation
report becomes a thing people stop reading.

**Evidence is capped with the remainder counted**, P5-D3's rule again. Ten
offending values is enough to recognise the problem and short enough that
nobody mistakes it for the answer; the count of what is not shown is printed
beside it rather than left to be inferred from a truncated list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..contract.compatibility import verify_key

# What a check shows before it starts counting instead of listing.
EVIDENCE_LIMIT = 10


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT RUN"


class Scope(str, Enum):
    """What a check counts.

    P7-D8: not every check is about rows. "The contract was agreed at 51,290
    rows and the table holds 51,530" is true of the table and of no row in
    particular, and inventing a per-row breakdown for it -- calling every row
    failed, or every row passed -- would put a number in the report that
    nothing measured. A TABLE-scoped check leaves the four counts at zero and
    the renderer prints a dash, which is what "this question is not about
    rows" looks like when it is said out loud.
    """

    ROWS = "rows"
    TABLE = "table"


@dataclass(frozen=True)
class CheckResult:
    """One check against one dataset."""

    check_id: str
    title: str
    subject: str
    rows: int = 0
    passed: int = 0
    failed: int = 0
    not_checked: int = 0
    detail: str = ""
    evidence: tuple[str, ...] = ()
    evidence_total: int = 0
    not_run_because: str = ""
    scope: Scope = Scope.ROWS
    table_ok: bool | None = None

    def __post_init__(self) -> None:
        if self.not_run_because:
            if self.rows or self.passed or self.failed or self.not_checked:
                raise ValueError(
                    f"{self.check_id}: a check that did not run counts nothing. "
                    f"Zero rows were examined, and any other number here would "
                    f"be describing rows nobody looked at."
                )
            return
        if self.scope is Scope.TABLE:
            if self.rows or self.passed or self.failed or self.not_checked:
                raise ValueError(
                    f"{self.check_id}: a TABLE-scoped check counts no rows. "
                    f"P7-D8: the finding is true of the table and of no row in "
                    f"particular, and a per-row breakdown here would be a "
                    f"number nothing measured."
                )
            if self.table_ok is None:
                raise ValueError(
                    f"{self.check_id}: a TABLE-scoped check needs table_ok, "
                    f"because it has no failure count to derive an outcome from."
                )
            return
        total = self.passed + self.failed + self.not_checked
        if total != self.rows:
            raise ValueError(
                f"{self.check_id}: passed + failed + not_checked = {total:,}, "
                f"but the table has {self.rows:,} row(s). P7-D4: a predicate "
                f"does not see NULLs, so every row a check did not examine is "
                f"counted rather than left out of the arithmetic."
            )
        if len(self.evidence) > EVIDENCE_LIMIT:
            raise ValueError(
                f"{self.check_id}: {len(self.evidence)} evidence lines, cap is "
                f"{EVIDENCE_LIMIT}. Cap it and count the remainder."
            )
        if self.evidence_total < len(self.evidence):
            raise ValueError(
                f"{self.check_id}: evidence_total {self.evidence_total} is "
                f"below the {len(self.evidence)} line(s) shown."
            )

    @property
    def outcome(self) -> Outcome:
        if self.not_run_because:
            return Outcome.NOT_RUN
        if self.scope is Scope.TABLE:
            return Outcome.PASS if self.table_ok else Outcome.FAIL
        return Outcome.FAIL if self.failed else Outcome.PASS

    @property
    def evidence_withheld(self) -> int:
        return max(0, self.evidence_total - len(self.evidence))

    def sentence(self) -> str:
        """What this check found, in one line, counts included."""
        if self.not_run_because:
            return f"Not run: {self.not_run_because}"
        parts = [self.detail] if self.detail else []
        if self.not_checked:
            parts.append(
                f"{self.not_checked:,} row(s) could not be checked and are not "
                f"counted as passing"
            )
        return ". ".join(parts) + ("." if parts else "")

    @classmethod
    def about_the_table(cls, check_id: str, title: str, subject: str, *,
                        ok: bool, detail: str):
        return cls(check_id=check_id, title=title, subject=subject,
                   scope=Scope.TABLE, table_ok=ok, detail=detail)

    @classmethod
    def not_run(cls, check_id: str, title: str, subject: str, why: str):
        return cls(check_id=check_id, title=title, subject=subject,
                   not_run_because=why)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _duplicates(con, dataset_name: str, columns: list[str]):
    """The keys that repeat, how many rows they cover, and up to ten of them.

    P7-D1: asked with GROUP BY rather than with a count. The grouping form is
    null-safe for a one-column key as well as a composite one and -- the reason
    it wins outright -- it returns the offending values. A check that reports
    "3 keys repeat" sends somebody to write this query.

    The NULL guard is applied for a one-column key ONLY, which is P7-D6's
    denominator arriving in a second place: `count(DISTINCT x)` drops nulls and
    `count(DISTINCT (a, b))` does not, so a null-bearing tuple IS a comparable
    value for a composite key and is not one for a single column. Guarding both
    the same way would make this query disagree with the verdict it is
    evidence for.
    """
    quoted = ", ".join(_q(c) for c in columns)
    guard = (
        f"WHERE {_q(columns[0])} IS NOT NULL" if len(columns) == 1 else ""
    )
    # The totals subquery projects ONLY the count, never the key columns.
    # An earlier version aliased it `AS n` and selected the key columns
    # alongside; a table whose own key column is called `n` then had two
    # columns of that name in the subquery, `sum(n)` summed the grouping
    # column instead of the count, and every duplicate row silently became
    # zero. Nothing raised. Projecting one column makes the collision
    # impossible rather than unlikely.
    groups, rows = con.execute(
        f"SELECT count(*), coalesce(sum(cnt), 0) FROM ("
        f"  SELECT count(*) AS cnt FROM {_q(dataset_name)} {guard} "
        f"  GROUP BY {quoted} HAVING count(*) > 1)"
    ).fetchone()
    shown = con.execute(
        f"SELECT {quoted}, count(*) FROM {_q(dataset_name)} {guard} "
        f"GROUP BY {quoted} HAVING count(*) > 1 "
        f"ORDER BY count(*) DESC, {quoted} LIMIT {EVIDENCE_LIMIT}"
    ).fetchall()
    evidence = tuple(
        f"{' + '.join('(null)' if v is None else str(v) for v in r[:-1])} "
        f"appears {r[-1]:,} times"
        for r in shown
    )
    return evidence, int(groups), int(rows)


def key_checks(con, dataset_name: str, primary_key: list[str]) -> list[CheckResult]:
    """Uniqueness and completeness, from one pass over the table.

    Two checks and not one, because "the key repeats" and "the key is missing"
    have different fixes and P7-D6 showed what happens when one number tries to
    carry both. The verdict comes from `contract.compatibility.verify_key` --
    the same function the gate calls, so a dataset the gate refuses cannot be
    reported as valid here.
    """
    if not primary_key:
        why = (
            "the contract states no primary key, so there is nothing to verify "
            "a row against"
        )
        return [
            CheckResult.not_run("key.unique", "Primary key is unique", "-", why),
            CheckResult.not_run("key.complete", "Primary key is present", "-", why),
        ]

    v = verify_key(con, dataset_name, primary_key)
    label = " + ".join(primary_key)

    if v.missing:
        why = (
            f"{', '.join(v.missing)} is not a column of {dataset_name}, so the "
            f"contract names a key the table does not have"
        )
        return [
            CheckResult.not_run("key.unique", "Primary key is unique", label, why),
            CheckResult.not_run("key.complete", "Primary key is present", label, why),
        ]

    # P7-D6's denominator, reused: for a one-column key the null rows were
    # never comparable and are NOT counted as passing; for a composite key the
    # row constructor counts a null-bearing tuple, so every row was examined.
    not_comparable = v.row_count - v.keyed_rows
    evidence, groups, duplicate_rows = _duplicates(con, dataset_name, primary_key)

    missing_key_rows = 0
    if v.null_bearing:
        any_null = " OR ".join(f"{_q(c)} IS NULL" for c in primary_key)
        missing_key_rows = con.execute(
            f"SELECT count(*) FROM {_q(dataset_name)} WHERE {any_null}"
        ).fetchone()[0]

    unique = CheckResult(
        check_id="key.unique",
        title="Primary key is unique",
        subject=label,
        rows=v.row_count,
        passed=v.keyed_rows - duplicate_rows,
        failed=duplicate_rows,
        not_checked=not_comparable,
        detail=(
            f"{groups:,} key value(s) repeat, across {duplicate_rows:,} row(s)"
            if duplicate_rows
            else f"{v.distinct:,} distinct value(s), none repeated"
        ),
        evidence=evidence,
        evidence_total=groups,
    )

    complete = CheckResult(
        check_id="key.complete",
        title="Primary key is present",
        subject=label,
        rows=v.row_count,
        passed=v.row_count - missing_key_rows,
        failed=missing_key_rows,
        not_checked=0,
        detail=(
            f"{missing_key_rows:,} row(s) have no {label} and are not "
            f"identified by it"
            if missing_key_rows
            else f"no row is missing {label}"
        ),
    )
    return [unique, complete]


def _column_type(con, dataset_name: str, column: str) -> str | None:
    row = con.execute(
        """SELECT data_type FROM information_schema.columns
           WHERE table_schema = 'main' AND table_name = ? AND column_name = ?""",
        [dataset_name, column],
    ).fetchone()
    return row[0] if row else None


def date_checks(
    con,
    dataset_name: str,
    date_column: str | None,
    window: tuple[date, date] | None = None,
    *,
    today: date | None = None,
) -> list[CheckResult]:
    """Three questions about the column the contract dates a row by.

    `today` is injectable for the reason `store.confirm`'s `now` is: a test
    that asserts on "in the future" and reads the wall clock asserts something
    different every day it runs.

    The three run independently. A contract can name a date column and declare
    no window -- that is a legitimate state, not a gap -- so `date.in_window`
    reports NOT RUN while the other two still answer. Collapsing them into one
    check would make a missing window silence a question about nulls that has
    nothing to do with it.
    """
    if not date_column:
        why = "the contract names no date column, so no row can be placed in time"
        return [
            CheckResult.not_run("date.present", "Every row is dated", "-", why),
            CheckResult.not_run("date.in_window", "Dates fall in the analysis window", "-", why),
            CheckResult.not_run("date.not_future", "No row is dated in the future", "-", why),
        ]

    column_type = _column_type(con, dataset_name, date_column)
    if column_type is None:
        why = (
            f"{date_column} is not a column of {dataset_name}, so the contract "
            f"names a date column the table does not have"
        )
        return [
            CheckResult.not_run("date.present", "Every row is dated", date_column, why),
            CheckResult.not_run("date.in_window", "Dates fall in the analysis window", date_column, why),
            CheckResult.not_run("date.not_future", "No row is dated in the future", date_column, why),
        ]

    col = _q(date_column)
    table = _q(dataset_name)
    rows, dated = con.execute(
        f"SELECT count(*), count({col}) FROM {table}"
    ).fetchone()
    undated = rows - dated

    present = CheckResult(
        check_id="date.present",
        title="Every row is dated",
        subject=date_column,
        rows=rows,
        passed=dated,
        failed=undated,
        not_checked=0,
        detail=(
            f"{undated:,} row(s) have no {date_column} and cannot be placed in "
            f"time"
            if undated
            else f"no row is missing {date_column}"
        ),
    )

    if window is None:
        in_window = CheckResult.not_run(
            "date.in_window", "Dates fall in the analysis window", date_column,
            "the contract declares no analysis window, so there is no span to "
            "test a date against",
        )
    else:
        start, end = window
        # P7-D2. The window is inclusive at both ends and date_column may be a
        # TIMESTAMP, where `<= end` casts the bound to midnight and drops the
        # rest of that day. Step 1 measured it; broken_sales.csv holds the one
        # row at 23:59:59 that notices if this is ever written the other way.
        inside = con.execute(
            f"SELECT count(*) FROM {table} WHERE {col} >= ? "
            f"AND {col} < CAST(? AS DATE) + INTERVAL 1 DAY",
            [start, end],
        ).fetchone()[0]
        before = con.execute(
            f"SELECT count(*) FROM {table} WHERE {col} < ?", [start]
        ).fetchone()[0]
        after = con.execute(
            f"SELECT count(*) FROM {table} "
            f"WHERE {col} >= CAST(? AS DATE) + INTERVAL 1 DAY", [end]
        ).fetchone()[0]
        evidence = []
        if before:
            earliest = con.execute(
                f"SELECT min({col}) FROM {table} WHERE {col} < ?", [start]
            ).fetchone()[0]
            evidence.append(f"{before:,} row(s) before {start}, earliest {earliest}")
        if after:
            latest = con.execute(
                f"SELECT max({col}) FROM {table} "
                f"WHERE {col} >= CAST(? AS DATE) + INTERVAL 1 DAY", [end]
            ).fetchone()[0]
            evidence.append(f"{after:,} row(s) after {end}, latest {latest}")
        in_window = CheckResult(
            check_id="date.in_window",
            title="Dates fall in the analysis window",
            subject=f"{date_column} in {start} to {end}",
            rows=rows,
            passed=inside,
            failed=before + after,
            not_checked=undated,
            detail=(
                f"{before + after:,} row(s) fall outside {start} to {end}"
                if before + after
                else f"all {inside:,} dated row(s) fall inside {start} to {end}"
            ),
            evidence=tuple(evidence),
            evidence_total=len(evidence),
        )

    cutoff = today or date.today()
    ahead = con.execute(
        f"SELECT count(*) FROM {table} "
        f"WHERE {col} >= CAST(? AS DATE) + INTERVAL 1 DAY", [cutoff]
    ).fetchone()[0]
    not_future = CheckResult(
        check_id="date.not_future",
        title="No row is dated in the future",
        subject=date_column,
        rows=rows,
        passed=dated - ahead,
        failed=ahead,
        not_checked=undated,
        detail=(
            f"{ahead:,} row(s) are dated after {cutoff}"
            if ahead
            else f"nothing is dated after {cutoff}"
        ),
    )
    return [present, in_window, not_future]


def row_count_check(
    con, dataset_name: str, expected_rows: int, *, agreed_when: str = ""
) -> CheckResult:
    """The table against the row count the contract was confirmed at.

    **No new contract field.** `Binding.row_count` is what the table held when
    somebody agreed the definitions, stored beside the fingerprint precisely so
    a change is detectable. `classify_drift` already computes this difference
    as a caveat; this renders the same fact as a row of the report.

    **Growth passes with the difference stated; loss fails.** Rows appearing is
    a table being reloaded with more recent data, which `classify_drift` calls
    NEUTRAL -- proceed, and say so. Rows disappearing means data that was there
    when the agreement was made is gone, so every number computed under that
    contract describes rows that are no longer there. `runs.drift_phrase` makes
    exactly that distinction for a profile, in those words, and a validation
    report that treated the two the same would be less careful than the
    profiler.
    """
    rows = con.execute(f"SELECT count(*) FROM {_q(dataset_name)}").fetchone()[0]
    when = f" on {agreed_when}" if agreed_when else ""
    if rows == expected_rows:
        detail = f"{rows:,} row(s), the same count the contract was agreed at{when}"
    elif rows > expected_rows:
        detail = (
            f"the table has gained {rows - expected_rows:,} row(s) since the "
            f"contract was agreed{when} ({expected_rows:,} then, {rows:,} now). "
            f"Numbers computed here cover more data than the agreement was "
            f"written against"
        )
    else:
        detail = (
            f"the table has lost {expected_rows - rows:,} row(s) since the "
            f"contract was agreed{when} ({expected_rows:,} then, {rows:,} now), "
            f"so the agreement describes rows that are no longer there"
        )
    return CheckResult.about_the_table(
        "table.row_count", "Row count matches the contract",
        dataset_name, ok=rows >= expected_rows, detail=detail,
    )


def reference_checks(con, dataset_name: str, foreign_keys) -> list[CheckResult]:
    """dbt's `relationships`, one check per declared key.

    **P7-D3, and this is the check it was measured for.** One NULL anywhere in
    the parent column makes `NOT IN` return no rows at all, so an orphan check
    written that way reports a clean pass over a table full of orphans. Step 1
    measured it; `region_lookup.csv` carries a blank row so a regression is
    visible rather than theoretical -- `NOT EXISTS` finds 7, `NOT IN` finds 0.

    **An orphan and a null reference are counted apart.** Both are unmatched
    and only one is a broken reference: a NULL foreign key is a row that points
    at nothing on purpose, optional by design in most schemas. Counting them
    together produces a number nobody can act on, so nulls land in
    `not_checked` -- they were never comparable -- and orphans in `failed`.
    """
    out: list[CheckResult] = []
    for fk in foreign_keys or []:
        check_id = f"reference.{'+'.join(fk.columns)}"
        title = "Every reference points at a row"
        label = fk.label()

        if not _table_columns(con, fk.references):
            out.append(CheckResult.not_run(
                check_id, title, label,
                f"{fk.references} is not loaded in this workspace, so there is "
                f"nothing to check the reference against. Load it and run this "
                f"again -- the contract is not wrong, the workspace is thin",
            ))
            continue

        there = _table_columns(con, fk.references)
        missing = [c for c in fk.referenced_columns if c not in there]
        if missing:
            out.append(CheckResult.not_run(
                check_id, title, label,
                f"{fk.references} has no column called {', '.join(missing)}, "
                f"so the contract names a join that cannot be made",
            ))
            continue

        table, parent = _q(dataset_name), _q(fk.references)
        on = " AND ".join(
            f"p.{_q(b)} = c.{_q(a)}"
            for a, b in zip(fk.columns, fk.referenced_columns)
        )
        stated = " AND ".join(f"c.{_q(c)} IS NOT NULL" for c in fk.columns)

        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        unstated = con.execute(
            f"SELECT count(*) FROM {table} c WHERE NOT ({stated})"
        ).fetchone()[0]
        orphans = con.execute(
            f"SELECT count(*) FROM {table} c WHERE {stated} "
            f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {on})"
        ).fetchone()[0]

        evidence, distinct = (), 0
        if orphans:
            quoted = ", ".join(f"c.{_q(c)}" for c in fk.columns)
            distinct = con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT {quoted} FROM {table} c "
                f"WHERE {stated} AND NOT EXISTS "
                f"(SELECT 1 FROM {parent} p WHERE {on}))"
            ).fetchone()[0]
            shown = con.execute(
                f"SELECT {quoted}, count(*) FROM {table} c WHERE {stated} "
                f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {on}) "
                f"GROUP BY {quoted} ORDER BY count(*) DESC, {quoted} "
                f"LIMIT {EVIDENCE_LIMIT}"
            ).fetchall()
            evidence = tuple(
                f"{' + '.join(str(v) for v in r[:-1])} "
                f"({r[-1]:,} row(s)) is not in {fk.references}"
                for r in shown
            )

        out.append(CheckResult(
            check_id=check_id,
            title=title,
            subject=label,
            rows=rows,
            passed=rows - orphans - unstated,
            failed=orphans,
            not_checked=unstated,
            detail=(
                f"{orphans:,} row(s) point at {distinct:,} value(s) "
                f"{fk.references} does not have"
                if orphans
                else f"every stated reference matches a row of {fk.references}"
            ),
            evidence=evidence,
            evidence_total=distinct,
        ))
    return out


def domain_checks(con, dataset_name: str, domains) -> list[CheckResult]:
    """dbt's `accepted_values`, one check per declared column.

    Nulls are `not_checked` rather than failed, for the reason the reference
    check counts them apart: absence and a value outside the set are different
    findings with different fixes, and a predicate does not see a NULL anyway
    (Step 1 measured that on this exact shape).
    """
    out: list[CheckResult] = []
    known = _table_columns(con, dataset_name)
    for column, allowed in (domains or {}).items():
        check_id = f"value.{column}"
        title = "Values are inside the declared set"
        if column not in known:
            out.append(CheckResult.not_run(
                check_id, title, column,
                f"{column} is not a column of {dataset_name}, so the contract "
                f"constrains something the table does not have",
            ))
            continue
        if not allowed:
            out.append(CheckResult.not_run(
                check_id, title, column,
                f"the declared set for {column} is empty, which would fail "
                f"every row rather than testing anything",
            ))
            continue

        table, col = _q(dataset_name), _q(column)
        placeholders = ", ".join("?" for _ in allowed)
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        absent = con.execute(
            f"SELECT count(*) - count({col}) FROM {table}"
        ).fetchone()[0]
        outside = con.execute(
            f"SELECT count(*) FROM {table} WHERE {col} IS NOT NULL "
            f"AND {col} NOT IN ({placeholders})", list(allowed)
        ).fetchone()[0]

        evidence, distinct = (), 0
        if outside:
            distinct = con.execute(
                f"SELECT count(DISTINCT {col}) FROM {table} WHERE {col} IS NOT "
                f"NULL AND {col} NOT IN ({placeholders})", list(allowed)
            ).fetchone()[0]
            shown = con.execute(
                f"SELECT {col}, count(*) FROM {table} WHERE {col} IS NOT NULL "
                f"AND {col} NOT IN ({placeholders}) GROUP BY {col} "
                f"ORDER BY count(*) DESC, {col} LIMIT {EVIDENCE_LIMIT}",
                list(allowed)
            ).fetchall()
            evidence = tuple(f"{r[0]} ({r[1]:,} row(s))" for r in shown)

        out.append(CheckResult(
            check_id=check_id,
            title=title,
            subject=f"{column} in {', '.join(allowed)}",
            rows=rows,
            passed=rows - outside - absent,
            failed=outside,
            not_checked=absent,
            detail=(
                f"{outside:,} row(s) hold {distinct:,} value(s) the contract "
                f"does not list"
                if outside
                else f"every stated {column} is one of the {len(allowed)} "
                f"declared value(s)"
            ),
            evidence=evidence,
            evidence_total=distinct,
        ))
    return out


def reference_checks(
    con, dataset_name: str, foreign_keys, loaded: set[str] | None = None
) -> list[CheckResult]:
    """dbt's `relationships`, one check per declared key.

    **P7-D3, and this is the rule's whole reason for existing.** The join is
    written with NOT EXISTS. `NOT IN` returns zero rows the moment the parent
    column holds a single NULL -- Step 1 measured it, and `region_lookup.csv`
    carries a blank row so a regression here shows up as 7 orphans becoming 0
    rather than as nothing at all.

    **An orphan and a null reference are counted apart.** Both are unmatched
    and only one is a broken reference: a NULL foreign key is a row that points
    at nothing on purpose, optional by design in most schemas. So a null is
    `not_checked` -- it was never comparable -- and an orphan is `failed`.

    **A referenced dataset that is not loaded reports NOT RUN.** The contract
    names it rather than resolving it, because a contract has to be readable on
    a machine that never loaded anything. P7-D7: a check that could not run is
    not a check that passed.
    """
    if not foreign_keys:
        return []
    if loaded is None:
        loaded = {
            r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'"
            ).fetchall()
        }

    table = _q(dataset_name)
    rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    out: list[CheckResult] = []

    for i, fk in enumerate(foreign_keys, start=1):
        check_id = f"reference.integrity[{i}]"
        title = "Every reference points at a row"
        label = fk.label()

        if fk.references not in loaded:
            out.append(CheckResult.not_run(
                check_id, title, label,
                f"{fk.references} is not loaded in this workspace, so there "
                f"is nothing to resolve {' + '.join(fk.columns)} against. The "
                f"contract is not wrong; the workspace is thin",
            ))
            continue

        missing = [c for c in fk.columns if _column_type(con, dataset_name, c) is None]
        if missing:
            out.append(CheckResult.not_run(
                check_id, title, label,
                f"{', '.join(missing)} is not a column of {dataset_name}, so "
                f"the contract names a key the table does not have",
            ))
            continue

        # The far side too. Left to the engine this is a BinderException from
        # inside a check, which is a traceback where a sentence belongs.
        absent = [
            c for c in fk.referenced_columns
            if _column_type(con, fk.references, c) is None
        ]
        if absent:
            out.append(CheckResult.not_run(
                check_id, title, label,
                f"{fk.references} has no column called {', '.join(absent)}, so "
                f"the join the contract describes cannot be made",
            ))
            continue

        parent = _q(fk.references)
        on = " AND ".join(
            f"p.{_q(r)} = c.{_q(l)}"
            for l, r in zip(fk.columns, fk.referenced_columns)
        )
        any_null = " OR ".join(f"c.{_q(c)} IS NULL" for c in fk.columns)

        unset = con.execute(
            f"SELECT count(*) FROM {table} c WHERE {any_null}"
        ).fetchone()[0]
        orphans = con.execute(
            f"SELECT count(*) FROM {table} c WHERE NOT ({any_null}) "
            f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {on})"
        ).fetchone()[0]

        quoted = ", ".join(f"c.{_q(c)}" for c in fk.columns)
        shown = con.execute(
            f"SELECT {quoted}, count(*) FROM {table} c WHERE NOT ({any_null}) "
            f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {on}) "
            f"GROUP BY {quoted} ORDER BY count(*) DESC, {quoted} "
            f"LIMIT {EVIDENCE_LIMIT}"
        ).fetchall() if orphans else []
        distinct = con.execute(
            f"SELECT count(*) FROM (SELECT {quoted} FROM {table} c "
            f"WHERE NOT ({any_null}) AND NOT EXISTS "
            f"(SELECT 1 FROM {parent} p WHERE {on}) GROUP BY {quoted})"
        ).fetchone()[0] if orphans else 0

        out.append(CheckResult(
            check_id=check_id,
            title=title,
            subject=label,
            rows=rows,
            passed=rows - orphans - unset,
            failed=orphans,
            not_checked=unset,
            detail=(
                f"{orphans:,} row(s) point at {distinct:,} value(s) "
                f"{fk.references} does not have"
                if orphans
                else f"every set reference resolves in {fk.references}"
            ),
            evidence=tuple(
                f"{' + '.join(str(v) for v in r[:-1])} ({r[-1]:,} row(s)) "
                f"is not in {fk.references}"
                for r in shown
            ),
            evidence_total=distinct,
        ))
    return out


def domain_checks(con, dataset_name: str, domains) -> list[CheckResult]:
    """dbt's `accepted_values`, one check per declared column.

    A value outside the declared set and a value that is absent are different
    findings, and Step 1 measured that a `NOT IN` predicate reports only the
    first: a NULL comparison is UNKNOWN, so nulls are invisible to it. They are
    counted separately here, as `not_checked` -- a row with no value did not
    break the vocabulary, it simply has nothing to check against it.

    The declared set is never derived from the data. A domain read off the
    column it constrains validates the column against itself and passes by
    construction; `propose_dataset_contract` reports what it sees and leaves
    the declaring to somebody who knows whether a fourth value is legal and
    merely absent.
    """
    out: list[CheckResult] = []
    for column in sorted(domains):
        allowed = list(domains[column])
        check_id = f"value.domain[{column}]"
        title = "Values are inside the declared set"

        if _column_type(con, dataset_name, column) is None:
            out.append(CheckResult.not_run(
                check_id, title, column,
                f"{column} is not a column of {dataset_name}, so the contract "
                f"constrains a column the table does not have",
            ))
            continue
        if not allowed:
            out.append(CheckResult.not_run(
                check_id, title, column,
                f"the declared set for {column} is empty, which would fail "
                f"every row rather than constrain any",
            ))
            continue

        table, col = _q(dataset_name), _q(column)
        placeholders = ", ".join("?" for _ in allowed)
        rows, present = con.execute(
            f"SELECT count(*), count({col}) FROM {table}"
        ).fetchone()
        outside = con.execute(
            f"SELECT count(*) FROM {table} WHERE {col} IS NOT NULL "
            f"AND {col} NOT IN ({placeholders})", allowed
        ).fetchone()[0]
        shown = con.execute(
            f"SELECT {col}, count(*) FROM {table} WHERE {col} IS NOT NULL "
            f"AND {col} NOT IN ({placeholders}) GROUP BY {col} "
            f"ORDER BY count(*) DESC, {col} LIMIT {EVIDENCE_LIMIT}", allowed
        ).fetchall() if outside else []
        distinct = con.execute(
            f"SELECT count(DISTINCT {col}) FROM {table} WHERE {col} IS NOT NULL "
            f"AND {col} NOT IN ({placeholders})", allowed
        ).fetchone()[0] if outside else 0

        out.append(CheckResult(
            check_id=check_id,
            title=title,
            subject=f"{column} in {{{', '.join(allowed)}}}",
            rows=rows,
            passed=present - outside,
            failed=outside,
            not_checked=rows - present,
            detail=(
                f"{outside:,} row(s) hold {distinct:,} value(s) outside the "
                f"declared set"
                if outside
                else f"every value is one of the {len(allowed)} declared"
            ),
            evidence=tuple(
                f"{r[0]} ({r[1]:,} row(s))" for r in shown
            ),
            evidence_total=distinct,
        ))
    return out

def expectation_checks(con, dataset_name: str, expectations, primary_key) -> list[CheckResult]:
    """One check per declared rule every row must satisfy (Cleanup Step 13, RF-O7).

    failed where the rule is false; not_checked where it is NULL -- a rule over a blank column
    judged nothing about that row, and P7-D4 is that a predicate does not see NULLs, so they are
    counted rather than folded into passed. Evidence is the failing rows' primary-key values, the
    handle a person looks a row up by; without a key, the failing rows are counted and not named.
    The rule was bound at proposal; it is bound again here, because the table can have changed.
    """
    from ..util.sql_guard import UnsafeSQL, bind_predicate

    out: list[CheckResult] = []
    table = _q(dataset_name)
    for x in expectations or []:
        check_id, title = f"rule[{x.rule}]", "Rows satisfy a rule"
        try:
            bind_predicate(con, dataset_name, x.rule)
        except UnsafeSQL as exc:
            out.append(CheckResult.not_run(check_id, title, x.rule, str(exc)))
            continue
        rows, failed, unknown = con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE ({x.rule}) = false), "
            f"count(*) FILTER (WHERE ({x.rule}) IS NULL) FROM {table}"
        ).fetchone()
        shown: list[str] = []
        if failed and primary_key:
            key = " || ' + ' || ".join(f"CAST({_q(c)} AS VARCHAR)" for c in primary_key)
            shown = [r[0] for r in con.execute(
                f"SELECT {key} FROM {table} WHERE ({x.rule}) = false "
                f"ORDER BY 1 LIMIT {EVIDENCE_LIMIT}").fetchall()]
        out.append(CheckResult(
            check_id=check_id,
            title=title,
            subject=x.rule,
            rows=rows,
            passed=rows - failed - unknown,
            failed=failed,
            not_checked=unknown,
            detail=(f"{failed:,} row(s) break it -- {x.reason}" if failed
                    else f"every row it can judge keeps it -- {x.reason}")
            + (f"; {unknown:,} row(s) have a NULL where it looks, so it judged nothing about them"
               if unknown else ""),
            evidence=tuple(shown),
            evidence_total=failed if shown else 0,
        ))
    return out


__all__ = [
    "EVIDENCE_LIMIT",
    "CheckResult",
    "Outcome",
    "Scope",
    "date_checks",
    "domain_checks",
    "expectation_checks",
    "key_checks",
    "reference_checks",
    "row_count_check",
]
