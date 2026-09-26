"""The three cleaning tools, as text an agent can act on.

Phase 6, Step 7b. Everything below returns a string: either a rendered result or
a `Refusal.to_text()` carrying a reason code and the exact call to make next.

**Why `propose` opens its own connection.** Step 1 measured that a genuinely
read-only handle on the workspace is reachable only through
`ATTACH ... (READ_ONLY)` from a separate connection, and only when nothing else
holds the file. `util/db.connect` cannot be that opener: it runs
`CREATE TABLE IF NOT EXISTS _agent_datasets` on every open, and a read-only
attach refuses `CREATE` by statement type, `IF NOT EXISTS` included. So
`connect_read_only` is a separate function rather than a flag on `connect`, and
it now lives in `util/db.py` -- moved there in Phase 7, Step 7, which is the
moment this paragraph used to describe as "later": `validate/tools.py` needed
the same handle, and a second package importing it from `clean` would have been
the wrong shape.

**The proposal reads read-only and the plan is stored writable.** Two
connections, in that order, never open at once -- one handle per file per
process. The reading half cannot write even if it is wrong; the writing half
touches only `_agent_cleaning_plans`.

**Approval is by id and nothing else.** `apply_cleaning_plan` takes
`approved_action_ids`, resolves them against the latest stored plan, refuses on
staleness before refusing on anything else, and refuses again if the approved
set conflicts. An id that was not approved leaves no trace.

**Approved ids are applied in the order given**, not in plan order. Step 7's
live run found an agent unable to tell which it was, so it forced the order with
two calls rather than assume -- reasonable, and it should not have had to. The
docstrings say so now.

**The contract is read on a WRITABLE connection, before the read-only one is
opened.** `contract.store.current` calls `_ensure_table`, which is
`CREATE TABLE IF NOT EXISTS` -- refused by statement type on a read-only attach,
Step 1 measured that. So a proposal that honours a contract opens three
connections in sequence: writable to read the contract, read-only to detect,
writable to store the plan. Never two at once; one handle per file per process.
That is P6-D2 costing something concrete rather than in principle.

**The suggested call is one the tool would accept.** `NEXT STEP` used to slice
the first three ids, and Step 7's run watched that slice land on the one action
carrying a discard warning, one apply later, after the ids renumbered. It now
offers only actions that are neither lossy nor in conflict with each other, and
says that is what it is offering.
"""

from __future__ import annotations


from ..config import DEFAULT_NA_VALUES
from ..contract.dataset_contract import Binding
from ..contract.refusals import Reason, Refusal
from ..contract import store as contract_store
from ..util import db
from ..util.db import connect_read_only
from . import apply as apply_module
from . import detect, ledger, plan
from .plan import ActionKind


def _table_exists(con, dataset_name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = ?",
            [dataset_name],
        ).fetchone()
    )


def _fingerprint(con, dataset_name: str, row_count: int) -> str:
    """Phase 4's fingerprint, computed by Phase 4's code.

    The column list is read here; the hash is not computed here. Two
    fingerprints of one table that disagree would be worse than none.

    `Binding.from_pairs` requires a row count even though the fingerprint is
    taken over the columns alone -- structure is identity, volume is not, and
    the binding records both. Passing the count we already have is cheaper than
    reading the table twice.
    """
    pairs = [
        (r[0], r[1])
        for r in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [dataset_name],
        ).fetchall()
    ]
    return Binding.from_pairs(pairs, row_count).fingerprint


def _not_loaded(con, dataset_name: str) -> str:
    available = ", ".join(db.user_tables(con)) or "(none loaded)"
    return Refusal(
        reason=Reason.DATASET_NOT_LOADED,
        what=f"there is no dataset called '{dataset_name}' in this workspace.",
        why="cleaning rebuilds a loaded table, and nothing is read from disk here.",
        state=f"loaded: {available}",
        detail="list_datasets() shows what is already here.",
        next_call="list_datasets()",
    ).to_text()


def _not_loaded_text(dataset_name: str) -> str:
    """The not-loaded refusal for a workspace with no database yet -- opening one to say so
    would create it (P14-D21)."""
    return Refusal(
        reason=Reason.DATASET_NOT_LOADED,
        what=f"there is no dataset called '{dataset_name}' in this workspace.",
        why="cleaning rebuilds a loaded table, and nothing is read from disk here.",
        state="loaded: (none loaded)",
        detail="list_datasets() shows what is already here.",
        next_call="list_datasets()",
    ).to_text()


def _safe_suggestion(actions, limit: int | None = None):
    """Actions worth suggesting: lossless, compatible, and in the right order.

    **No cap by default.** The first version sliced `actions[:3]`, and when the
    slice was replaced by these filters the `3` came along with it -- a number
    that meant "keep the line short" back when the set was arbitrary, and means
    nothing now that the set is defined by a property. On the real fixture it
    silently dropped `C006`, a boolean conversion that discards nothing and
    conflicts with nothing, while the line above it said "these discard nothing
    and can run together" without mentioning it was a subset. A live agent
    noticed the omission and added it back by hand.

    `limit` stays for `_next_call_id`, which wants exactly one.

    Three filters, and the third was missed on the first attempt.

    1. Nothing lossy. A suggestion that includes the action the same message
       just warned about is worse than no suggestion.
    2. Nothing that conflicts with what is already picked, checked with the
       same `conflicts()` that would refuse it. A suggestion the tool would
       refuse is worse than no suggestion.
    3. **No conversion on a column that also has a normalisation waiting.**
       This is the one that slipped through, because `conflicts()` only fires
       when both are in the SAME call. Suggesting the conversion alone passes
       every check and still produces the worse outcome: the declared tokens
       are disposed of by the cast rather than by a step somebody named, which
       is precisely what this module's own refusal tells you to avoid. A
       suggestion that contradicts the refusal is not a smaller bug than a
       suggestion that gets refused.

    So the normalisation is offered first and the conversion comes round on the
    next proposal, which is the order the refusal recommends.
    """
    deferred = {
        a.column for a in actions
        if a.kind is ActionKind.NORMALISE_MISSING and a.column
    }
    # A column offered two readings is a question, not a suggestion (P14-D50).
    readings: dict[str, int] = {}
    for a in actions:
        if a.kind is ActionKind.CONVERT_TYPE and a.column:
            readings[a.column] = readings.get(a.column, 0) + 1
    deferred |= {c for c, n in readings.items() if n > 1}
    picked = []
    for a in actions:
        if a.is_lossy:
            continue
        if a.kind is ActionKind.CONVERT_TYPE and a.column in deferred:
            continue
        if apply_module.conflicts(picked + [a]):
            continue
        picked.append(a)
        if limit is not None and len(picked) == limit:
            break
    return picked


def _contract_settings(workspace_id: str, dataset_name: str):
    """The dataset's declared vocabulary and its excluded columns, or defaults.

    P6-D8, owed since Phase 5 Step 8. Three levels, and the order matters:

    1. an explicit `missing_values=` argument wins, because somebody typed it
       for this call;
    2. otherwise the contract's, because somebody confirmed it for this
       dataset;
    3. otherwise DEFAULT_NA_VALUES, the project vocabulary.

    `missing_values=None` on the contract means level 3, not "no tokens". `[]`
    means no tokens, the same switch Phase 5 gave `missing_values` one layer
    down. A field left unset is a default, NOT an unresolved gap -- if it went
    into `unresolved`, every contract in the workspace would be unconfirmable
    the day the field was added.
    """
    con = db.connect(workspace_id)
    try:
        stored = contract_store.current(con, dataset_name)
    except Exception:  # noqa: BLE001 - no contract table yet is not an error
        stored = None
    finally:
        con.close()
    if stored is None:
        return None, ()
    contract = stored.contract
    return (
        getattr(contract, "missing_values", None),
        tuple(getattr(contract, "excluded_columns", ()) or ()),
    )


def _next_call_id(actions) -> str:
    """An id safe to put in a NEXT STEP.

    Every `next_call` in this module is a call the reader is invited to make,
    so none of them may name an action the same message warns about. The
    nothing-approved refusal used `actions[0]`, which on a plan whose first
    action is lossy would recommend the discard.
    """
    safe = _safe_suggestion(actions, limit=1)
    return safe[0].action_id if safe else actions[0].action_id


def propose_cleaning_plan(
    workspace_id: str,
    dataset_name: str,
    missing_values: list[str] | None = None,
) -> str:
    """Detect on a read-only handle, then store the plan on a writable one."""
    declared, excluded = _contract_settings(workspace_id, dataset_name)
    if missing_values is not None:
        tokens = missing_values
    elif declared is not None:
        tokens = declared
    else:
        tokens = DEFAULT_NA_VALUES

    ro = connect_read_only(workspace_id)
    try:
        if not _table_exists(ro, dataset_name):
            return _not_loaded(ro, dataset_name)
        actions = [
            a for a in detect.detect(
                ro, source=dataset_name, target=dataset_name,
                missing_tokens=tokens,
            )
            if a.column not in excluded
        ]
        rows = ro.execute(
            f'SELECT count(*) FROM "{dataset_name}"'
        ).fetchone()[0]
        fingerprint = _fingerprint(ro, dataset_name, rows)
    finally:
        ro.close()

    if not actions:
        held = (
            f" {len(excluded)} column(s) are excluded by the contract and were "
            f"not looked at: {', '.join(excluded)}."
            if excluded
            else ""
        )
        return (
            f"Nothing to clean in {dataset_name}.{held} {rows:,} row(s), no exact "
            f"duplicates, no declared missing tokens, no padding, no case "
            f"variants, and every column already reads as the type it holds.\n\n"
            f"NEXT STEP: call profile_dataset(dataset_name=\"{dataset_name}\") "
            f"if you want the full counts anyway."
        )

    con = db.connect(workspace_id)
    try:
        stored = plan.record(
            con,
            dataset_name=dataset_name,
            row_count=rows,
            fingerprint=fingerprint,
            actions=actions,
        )
    finally:
        con.close()

    lines = "\n\n".join(f"  {a.line()}" for a in stored.actions)
    lossy = [a.action_id for a in stored.actions if a.is_lossy]
    warning = (
        f"\n{len(lossy)} of these discard something that is not declared "
        f"missing anywhere ({', '.join(lossy)}). Read those lines before "
        f"approving them.\n"
        if lossy
        else ""
    )
    suggested = _safe_suggestion(stored.actions)
    if suggested:
        ids = ", ".join(f'"{a.action_id}"' for a in suggested)
        held = [
            a.action_id for a in stored.actions
            if a.kind is ActionKind.CONVERT_TYPE and a.column in {
                x.column for x in stored.actions
                if x.kind is ActionKind.NORMALISE_MISSING
            }
        ]
        because = (
            f" {', '.join(held)} is left for the next round on purpose: "
            f"converting that column would dispose of its declared missing "
            f"tokens as part of the cast, and doing it in two steps records "
            f"which one disposed of them."
            if held
            else ""
        )
        nudge = (
            f"NEXT STEP: call apply_cleaning_plan(dataset_name=\"{dataset_name}\", "
            f"approved_action_ids=[{ids}]) — these discard nothing and can run "
            f"together.{because} Any of the others are yours to add by id."
        )
    else:
        nudge = (
            f"NEXT STEP: read the lines above and call "
            f"apply_cleaning_plan(dataset_name=\"{dataset_name}\", "
            f"approved_action_ids=[...]) with the ids you want. Nothing here "
            f"is free of loss, so there is no set worth suggesting."
        )
    excluded_note = (
        f"\n{len(excluded)} column(s) are excluded by the contract and were not "
        f"looked at: {', '.join(excluded)}.\n"
        if excluded
        else ""
    )
    return (
        f"{len(stored.actions)} change(s) proposed for {dataset_name} "
        f"({rows:,} row(s)). Nothing has been changed.{excluded_note}\n\n"
        f"{lines}\n{warning}\n"
        f"Approve by id, in the order you want them run. Anything you do not "
        f"name is not run.\n\n"
        f"{nudge}"
    )


def apply_cleaning_plan(
    workspace_id: str, dataset_name: str, approved_action_ids: list[str]
) -> str:
    """Run exactly the approved ids, in the order given, or change nothing."""
    con = db.connect(workspace_id)
    try:
        if not _table_exists(con, dataset_name):
            return _not_loaded(con, dataset_name)

        stored = plan.latest(con, dataset_name)
        if stored is None:
            return Refusal(
                reason=Reason.NO_CLEANING_PLAN,
                what=f"nothing has been proposed for {dataset_name}.",
                why="approval is by action id, and the ids come from a plan.",
                next_call=f'propose_cleaning_plan(dataset_name="{dataset_name}")',
            ).to_text()

        rows = con.execute(f'SELECT count(*) FROM "{dataset_name}"').fetchone()[0]
        stale = stored.staleness(rows, _fingerprint(con, dataset_name, rows))
        if stale:
            return Refusal(
                reason=Reason.CLEANING_PLAN_STALE,
                what=f"the plan for {dataset_name} is out of date.",
                why=stale,
                state=f"{stored.age_phrase()}, {len(stored.actions)} action(s)",
                next_call=f'propose_cleaning_plan(dataset_name="{dataset_name}")',
            ).to_text()

        if not approved_action_ids:
            return Refusal(
                reason=Reason.NOTHING_APPROVED,
                what="no action ids were approved.",
                why="nothing is run unless it is named. That is the whole gate.",
                state=", ".join(a.action_id for a in stored.actions),
                next_call=(
                    f'apply_cleaning_plan(dataset_name="{dataset_name}", '
                    f'approved_action_ids='
                    f'["{_next_call_id(stored.actions)}"])'
                ),
            ).to_text()

        found, unknown = stored.resolve(approved_action_ids)
        if unknown:
            return Refusal(
                reason=Reason.ACTION_NOT_IN_PLAN,
                what=f"{', '.join(unknown)} is not in the plan for {dataset_name}.",
                why="an id that is not in the plan cannot be run, and running "
                    "the rest without saying so would be worse.",
                state="in the plan: "
                      + ", ".join(a.action_id for a in stored.actions),
                next_call=f'propose_cleaning_plan(dataset_name="{dataset_name}")',
            ).to_text()

        problems = apply_module.conflicts(found)
        if problems:
            return Refusal(
                reason=Reason.ACTIONS_CONFLICT,
                what="these actions cannot be applied together.",
                why=" ".join(problems),
                state="approved: " + ", ".join(a.action_id for a in found),
                next_call=(
                    f'apply_cleaning_plan(dataset_name="{dataset_name}", '
                    f'approved_action_ids='
                    f'["{apply_module.first_step(found)}"])'
                ),
            ).to_text()

        result = apply_module.apply(
            con, dataset_name=dataset_name, actions=found,
            plan_id=stored.plan_id,
        )
        skipped = [
            a.action_id for a in stored.actions
            if a.action_id not in {x.action_id for x in found}
        ]
        applied_lines = "\n".join(
            f"  {a.action_id} {a.kind.value}"
            + (f" on {a.column}" if a.column else "")
            + (f": {a.rows_removed:,} row(s) removed"
               if a.rows_removed else ": row count unchanged")
            for a in result.applied
        )
        tail = (
            f"\n{len(skipped)} action(s) were not approved and did not run: "
            f"{', '.join(skipped)}.\n"
            if skipped
            else ""
        )
        return (
            f"{result.line()}\n\n{applied_lines}\n{tail}\n"
            f"NEXT STEP: call profile_dataset(dataset_name=\"{dataset_name}\") "
            f"to see the table as it is now."
        )
    finally:
        con.close()


def get_cleaning_ledger(workspace_id: str, dataset_name: str | None = None) -> str:
    """Every action that has actually run, newest first."""
    con = db.connect(workspace_id)
    try:
        # A name that is not loaded and never was cleaned is a mistake, not a clean table: it
        # read "Nothing has been cleaned for nope. Every table is as it was loaded." (P14-O19).
        if (dataset_name and dataset_name not in db.user_tables(con)
                and not ledger.count(con, dataset_name)):
            loaded = ", ".join(db.user_tables(con)) or "(none loaded)"
            return (f"BLOCKED: no dataset called {dataset_name!r} in this workspace.\n"
                    f"Loaded: {loaded}\n"
                    f"NEXT STEP: call list_datasets() and use one of those names.")
        return ledger.describe(con, dataset_name)
    finally:
        con.close()


__all__ = [
    "apply_cleaning_plan",
    "connect_read_only",
    "get_cleaning_ledger",
    "propose_cleaning_plan",
]
