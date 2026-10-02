"""Stored tool results: the typed result contract, evidence lineage and inspection (B9).

Every domain/core tool run is stored with what it was computed on -- a snapshot hash of the
table (order-independent, computed in DuckDB), the contract version and the metrics it read --
so a result can say later whether it is STALE (the data, the contract or a metric approval has
changed since). Up to ROWS_KEPT engine rows per step are kept so a person or the model can look
past the figure cap: inspection sorts and filters stored engine rows, it computes nothing new.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from backend.services.sessions import ServiceError

ROWS_KEPT = 500
INSPECT_MAX = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot(con, table: str) -> dict:
    """An order-independent fingerprint of a table's rows: the sum of every row's hash (a sum,
    not a xor, so a duplicated pair of rows does not cancel out), with the row count."""
    t = '"' + table.replace('"', '""') + '"'
    h, n = con.execute(f"SELECT coalesce(sum(hash(x)::HUGEINT), 0) % 18446744073709551616, "
                       f"count(*) FROM {t} x").fetchone()
    return {"hash": f"{int(h):016x}-{int(n)}", "rows": int(n)}


def status_of(result: dict) -> str:
    """ok | partial (a step was skipped or refused) | insufficient_data (nothing reportable)."""
    figs = result.get("figures", [])
    cav = " ".join(result.get("caveats", []))
    if not any(isinstance(f.get("value"), (int, float)) for f in figs) or \
            ("NOT ENOUGH DATA" in cav and not figs):
        return "insufficient_data"
    if ": skipped --" in cav or "the engine refused" in cav:
        return "partial"
    return "ok"


class ResultStore:
    def __init__(self, store):
        self.store = store
        with store._lock:
            store._con.execute(
                "CREATE TABLE IF NOT EXISTS results (result_id TEXT PRIMARY KEY, dataset_id "
                "TEXT, tool_id TEXT, run_id TEXT, params TEXT, snapshot TEXT, contract_version "
                "INTEGER, metrics TEXT, status TEXT, created_at TEXT, result TEXT, tables TEXT)")
            store._con.execute("CREATE INDEX IF NOT EXISTS results_by_ds ON results(dataset_id, "
                               "created_at)")
            store._con.commit()

    def save(self, d: dict, result: dict, tables: list[dict], *, params: dict, snap: dict,
             contract_version: int | None, run_id: str | None) -> dict:
        rid = f"r_{secrets.token_hex(8)}"
        used = {t: {"measure": d["metrics"].get(t)} for t in result.get("metrics_used", [])}
        result.update(result_id=rid, run_id=run_id or rid, status=status_of(result),
                      snapshot=snap, contract_version=contract_version, metrics_used=used)
        self.store._q("INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, d["dataset_id"], result["tool_id"], result["run_id"],
                       json.dumps(params, default=str), json.dumps(snap), contract_version,
                       json.dumps(used), result["status"], _now(), json.dumps(result),
                       json.dumps(tables, default=str)))
        return result

    def _row(self, rid: str):
        rows = self.store._q("SELECT * FROM results WHERE result_id=?", (rid,))
        if not rows:
            raise ServiceError(404, "not_found", f"result {rid} not found")
        return dict(rows[0])

    def get(self, rid: str, current) -> dict:
        r = self._row(rid)
        out = json.loads(r["result"])
        out.update(self._staleness(r, current))
        out["created_at"] = r["created_at"]
        return out

    def list(self, dataset_id: str, current) -> list[dict]:
        rows = self.store._q("SELECT * FROM results WHERE dataset_id=? ORDER BY created_at DESC",
                             (dataset_id,))
        return [{"result_id": r["result_id"], "tool_id": r["tool_id"], "run_id": r["run_id"],
                 "status": r["status"], "created_at": r["created_at"],
                 **self._staleness(dict(r), current)} for r in rows]

    @staticmethod
    def _staleness(r: dict, current) -> dict:
        """current(dataset_id) -> (snapshot, contract_version, metrics map), or None if gone."""
        now = current(r["dataset_id"])
        if now is None:
            return {"stale": True, "stale_reasons": ["the dataset is gone"]}
        snap, version, metrics = now
        why = []
        if json.loads(r["snapshot"])["hash"] != snap["hash"]:
            why.append("the data changed (cleaning or a new upload) since this result")
        if r["contract_version"] != version:
            why.append(f"the contract is now v{version} (was v{r['contract_version']})")
        for t, v in json.loads(r["metrics"]).items():
            if metrics.get(t) != v["measure"]:
                why.append(f"the metric '{t}' approval changed")
        return {"stale": bool(why), "stale_reasons": why}

    def inspect(self, rid: str, *, step: str | None = None, sort_by: str | None = None,
                descending: bool = True, limit: int = 20, offset: int = 0,
                group: str | None = None) -> dict:
        r = self._row(rid)
        tables = json.loads(r["tables"])
        if not tables:
            raise ServiceError(422, "nothing_to_inspect", "this result kept no rows")
        titles = [t["title"] for t in tables]
        t = tables[0] if step is None else next((x for x in tables if x["title"] == step), None)
        if t is None:
            raise ServiceError(422, "unknown_step", f"step {step!r}; steps: {titles}",
                               {"steps": titles})
        rows = t["rows"]
        if group is not None:
            g = group.lower().strip()
            rows = [x for x in rows if str(x[0]).lower().strip() == g] or \
                [x for x in rows if g in str(x[0]).lower()]
        if sort_by is not None:
            if sort_by not in t["headers"]:
                raise ServiceError(422, "unknown_column", f"sort_by {sort_by!r}; columns: "
                                   f"{t['headers']}", {"columns": t["headers"]})
            i = t["headers"].index(sort_by)
            from backend.tools.runner import num
            rows = sorted(rows, key=lambda x: (not isinstance(num(x[i]), (int, float)),
                                               -num(x[i]) if descending and isinstance(
                                                   num(x[i]), (int, float)) else
                                               num(x[i]) if isinstance(num(x[i]),
                                                                       (int, float)) else 0))
        limit = max(1, min(int(limit), INSPECT_MAX))
        return {"result_id": rid, "step": t["title"], "steps": titles,
                "headers": t["headers"], "rows": rows[offset:offset + limit],
                "total_rows": len(rows), "rows_kept": t.get("rows_kept", len(t["rows"])),
                "rows_in_engine_output": t.get("rows_total", len(t["rows"]))}
