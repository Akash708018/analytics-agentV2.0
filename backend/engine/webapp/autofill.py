"""The model fills the contract form, once per table; contract/llm_filter.py checks it (25/09/2026).

One request per dataset, when its contract is first drafted -- not per click, not per question --
and the model's JSON is kept in the workspace beside the table's fingerprint, so a reload, a
second visit or a person's edit reuses it. The filter runs again on every draft from the kept JSON:
a better filter applies without asking the model again. A changed table (a new fingerprint or row
count) is asked about afresh.

What the model is shown, per column: the name, the type, counts, the range, a few sampled values,
and what the data measured about repetition (the same on every row of each order_id). What it is
NOT shown: the engine's own reading of the column. The filter compares the two, and a model that
had been told the answer would agree with it by construction -- which would make "how often the
model is right" a number about nothing.

Waits are short (llm.QUICK_WAIT_S): the form is waiting. When every provider fails, the form falls
back to the data's own suggestions and says why.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from backend.engine import workspace
from backend.engine.clean import sql
from backend.engine.contract import llm_filter
from backend.engine.contract.compatibility import binding_for
from backend.engine.contract.evidence import gather, suggest_role

from . import llm

SAMPLE_ROWS = 400
SAMPLES_PER_COLUMN = 4
VALUE_CHARS = 24

SYSTEM = """You fill in the contract of a table for an analytics engine: what one row is, which \
column is its key and its date, and for every numeric column how it may be combined across rows.

Return ONE JSON object and nothing else:
{"grain": "one row = one ...",
 "primary_key": ["col", ...],
 "date_column": "col or null",
 "columns": [{"name": "col", "role": "measure|dimension|key|date|ignore",
              "agg": "sum|mean|median|min|max|count|count_distinct|none|null",
              "per": ["col"], "meaning": "under 15 words", "confidence": 0.0-1.0}]}

How to choose agg:
- sum only when adding rows gives a meaningful total: an amount of money, a quantity, a count.
- none for a value that is per row only: a price, a rate, a percentage, a score or rating, an age,
  a coordinate, a balance or stock level, a code or year stored as a number.
- mean for a value whose average is the usual question and whose total is not (a duration).
- per: when the column says it is the same on every row of some key (a fee repeated on every line
  of an order), name that key: the value is counted once per key.
- A column of 0 and 1 is a flag: role dimension (its rate is added separately).
- Identifiers are dimensions or keys, never measures to add.
- confidence: how sure you are of the agg, from the name and values shown.
Every column listed must appear once. Do not invent columns."""


@dataclass
class AutoFill:
    filled: llm_filter.Filled | None
    model: str = ""
    failures: list[str] | None = None
    cached: bool = False
    note: str = ""


def _cache_path(workspace_id: str, dataset: str) -> Path:
    return workspace.workspace_dir(workspace_id) / "contracts" / f"_autofill_{dataset}.json"


def packet(con, table: str, ev) -> str:
    """The columns as the model sees them: facts, never the engine's reading."""
    t = sql.ident(table)
    sample = con.execute(f"SELECT * FROM {t} USING SAMPLE {SAMPLE_ROWS} ROWS (reservoir, 7)")
    names = [d[0] for d in sample.description]
    rows = sample.fetchall()
    lines = []
    for idx, c in enumerate(ev.columns):
        seen: list[str] = []
        for r in rows:
            v = r[names.index(c.name)] if c.name in names else None
            if v is None:
                continue
            text = str(v)[:VALUE_CHARS]
            if text not in seen:
                seen.append(text)
            if len(seen) == SAMPLES_PER_COLUMN:
                break
        lines.append(json.dumps({
            "name": c.name, "type": c.dtype, "non_null": c.non_null, "distinct": c.distinct,
            "min": (c.min_value or "")[:VALUE_CHARS] or None,
            "max": (c.max_value or "")[:VALUE_CHARS] or None, "samples": seen},
            separators=(",", ":")))
    repeats = _repeat_facts(con, table, ev)
    return (f"Table {table}: {ev.row_count:,} rows, {len(ev.columns)} columns.\n"
            + "\n".join(lines)
            + ("\nMeasured repetition:\n" + "\n".join(repeats) if repeats else ""))


def _repeat_facts(con, table: str, ev) -> list[str]:
    """What the data measured about repetition -- a fact the model is shown, since no name can say
    it: 'order_shipping_fee is the same on every row of each order_id'."""
    from backend.engine.contract import suggest as sg
    numeric = [c.name for c in ev.columns
               if c.dtype.upper() not in ("VARCHAR", "BOOLEAN") and not c.dtype.upper().startswith(
                   ("DATE", "TIMESTAMP"))]
    units = sg._per_units(con, table, ev, numeric) if numeric else {}
    return [f"{m} is the same on every row of each {u}" for m, u in units.items()]


def enabled() -> bool:
    """On in the web app (ui/app.py sets ANALYTICS_AUTOFILL=on), off everywhere else by default:
    a test, a benchmark or a script drafting contracts never calls a model by accident."""
    return os.environ.get("ANALYTICS_AUTOFILL", "off").strip().lower() in ("on", "1", "true")


def fill(con, workspace_id: str, table: str, *, loaded_at=None, providers=None,
         refresh: bool = False) -> AutoFill:
    """The filtered fill for one table: from the kept answer when the table is unchanged."""
    if not enabled() and not kept(workspace_id, table):
        return AutoFill(None)
    ev = gather(con, table, probe_pairs=False)
    roles = {c.name: suggest_role(c)[0] for c in ev.columns}
    binding = binding_for(con, table, loaded_at)
    stamp = {"fingerprint": binding.fingerprint, "rows": ev.row_count}
    path = _cache_path(workspace_id, table)
    if not refresh and path.exists():
        try:
            saved = json.loads(path.read_text())
            if {k: saved.get(k) for k in stamp} == stamp:
                f = llm_filter.check(con, table, ev, saved["llm"], roles, model=saved["model"])
                return AutoFill(f, saved["model"], saved.get("failures") or [], cached=True,
                                note=f"Filled by {saved['model']} on {saved['at']}. {f.summary()}")
        except (OSError, ValueError, KeyError):
            pass
    providers = llm.configured() if providers is None else providers
    if not providers:
        return AutoFill(None, note="No model is configured (GEMINI_API_KEY or GROQ_API_KEY in "
                                   ".env), so the form shows the data's own suggestions.")
    try:
        answer, model, failures = llm.complete_json(providers, SYSTEM, packet(con, table, ev))
    except llm.ProviderError as exc:
        return AutoFill(None, failures=[exc.summary],
                        note=f"The model could not fill this ({exc.summary}); the form shows the "
                             f"data's own suggestions instead.")
    at = time.strftime("%Y-%m-%d %H:%M")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**stamp, "model": model, "at": at, "llm": answer,
                                "failures": failures}, indent=1, default=str))
    f = llm_filter.check(con, table, ev, answer, roles, model=model)
    return AutoFill(f, model, failures, note=f"Filled by {model} on {at}. {f.summary()}")


def kept(workspace_id: str, table: str) -> bool:
    """Whether a model's fill is kept for this table (a person's edits are compared with it)."""
    return _cache_path(workspace_id, table).exists()


def forget(workspace_id: str, table: str) -> None:
    """Drop the kept answer, so the next draft asks the model again."""
    _cache_path(workspace_id, table).unlink(missing_ok=True)


__all__ = ["AutoFill", "SYSTEM", "fill", "forget", "kept", "packet"]
