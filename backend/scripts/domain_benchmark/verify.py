"""Parent-side verification of a worker's records: correctness, charts, paging, plan quality.

Imports nothing from backend.engine: it reads the worker's JSONL, the result files the tools
wrote, and the generated CSV (through oracle.Data).
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from . import oracle

NO_ORACLE_STATUS = "SKIPPED_UNSUPPORTED"


def load(jsonl: Path) -> list[dict]:
    out = []
    if not jsonl.exists():
        return out
    for line in jsonl.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"kind": "note", "event": "corrupt_line", "line": line[:200]})
    return out


def result_rows(path: str | None) -> list[dict] | None:
    if not path or not Path(path).exists():
        return None
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows or None


def correctness(records: list[dict], data: "oracle.Data", domain, *, context: dict) -> list[dict]:
    """One correctness record per verified call, with every check inside it."""
    out = []
    for n in records:
        if n.get("kind") != "note" or n.get("event") != "verify":
            continue
        rows = result_rows(n.get("result_path"))
        checks, skipped = [], []
        status = "PASS"
        if rows is None and n.get("analysis") not in ("correlated_shift",):
            # A written result with no data rows: the oracle decides whether empty is right.
            rows = [] if n.get("result_path") and Path(n["result_path"]).exists() else None
        try:
            if rows is None and n.get("analysis") not in ("correlated_shift",):
                # the call wrote no result (it raised or was refused): nothing to verify, and
                # the call's own record carries why (H_concurrency: an ORACLE_ERROR of KeyError)
                checks, skipped = [], ["the call produced no result file"]
                status = "NOT_VERIFIED_NO_RESULT"
            elif rows == []:
                checks, skipped = [], ["result file has no data rows"]
                status = "PASS_EMPTY"
            else:
                checks, skipped = oracle.check(n["analysis"], n.get("params") or {}, rows,
                                               n.get("text", ""), data, domain.measures,
                                               domain.dimensions)
        except Exception as exc:  # noqa: BLE001 - an oracle fault is recorded as one, not hidden
            status = "ORACLE_ERROR"
            skipped = [f"oracle raised {type(exc).__name__}: {exc}"]
        failed = [c for c in checks if c["status"] == "FAIL"]
        if status == "PASS":
            status = "FAIL" if failed else ("PASS" if checks else NO_ORACLE_STATUS)
        out.append({**context, "analysis": n["analysis"], "variant": n.get("variant"),
                    "class": n.get("class_"), "parameters": n.get("params"),
                    "call_seq": n.get("seq"), "status": status, "checks_total": len(checks),
                    "checks_passed": len(checks) - len(failed), "checks_failed": len(failed),
                    "skipped": skipped, "checks": checks,
                    "failed_checks": failed[:50]})
    return out


def charts(records: list[dict], context: dict) -> list[dict]:
    """A chart is not 'successful' because a file exists: PNG signature, non-zero size and
    dimensions, the reply naming what it drew, and a drawn count no larger than the points."""
    out = []
    for r in records:
        if r.get("tool") != "render_chart" or r.get("kind") == "note":
            continue
        facts = r.get("chart") or {}
        reply = r.get("chart_reply") or ""
        checks = {}
        if r["status"] == "OK":
            checks["file_exists"] = bool(facts.get("exists"))
            checks["png_signature"] = bool(facts.get("png_signature"))
            checks["non_zero_bytes"] = (facts.get("bytes") or 0) > 0
            checks["has_dimensions"] = bool(facts.get("width")) and bool(facts.get("height"))
            m = re.search(r"([\d,]+) of ([\d,]+) point\(s\) drawn", reply)
            checks["drawn_le_points"] = (m is None or int(m.group(1).replace(",", "")) <=
                                         int(m.group(2).replace(",", "")))
            y = (r.get("parameters") or {}).get("y")
            want = y or (r.get("parameters") or {}).get("measure")
            checks["reply_names_the_measure"] = (want is None or want in reply
                                                 or "measure" in reply)
            checks["says_it_cannot_be_seen"] = "cannot see" in reply
        verdict = ("PASS" if r["status"] == "OK" and all(checks.values()) else
                   "FAIL" if r["status"] == "OK" else r["status"])
        out.append({**context, "seq": r["seq"], "analysis": r.get("analysis"),
                    "chart": r.get("chart_kind"), "edge": r.get("edge"),
                    "parameters": r.get("parameters"), "status": r["status"],
                    "verdict": verdict, "checks": checks, "wall_time_seconds":
                    r["wall_time_seconds"], "file": facts, "run_kind": r.get("run_kind"),
                    "why": next((ln for ln in (r.get("response_head") or "").splitlines()
                                 if ln.startswith("WHY")), None)})
    return out


def paging(records: list[dict], context: dict) -> list[dict]:
    out = []
    for n in records:
        if n.get("kind") != "note" or n.get("event") != "page":
            continue
        path, start, limit = n["path"], n["start"], n["limit"]
        lines = Path(path).read_text().splitlines() if Path(path).exists() else []
        data = lines[1:]
        # read_result_file serves at most 50 rows a page and says so, with the call for the next
        # page (run 2 asked for 200 and judged the stated cap a failure)
        want = data[start - 1: start - 1 + min(limit, 50)]
        text = n.get("text", "")
        present = sum(1 for w in want if w.split(",")[0].strip('"') in text)
        ok = present == len(want) if want else ("past the end" in text or "Nothing" in text)
        out.append({**context, "path": Path(path).name, "start": start, "limit": limit,
                    "expected_rows": len(want), "found_rows": present,
                    "status": "PASS" if ok else "FAIL"})
    return out


_ACTION = re.compile(r"^\s+(C\d{3})\s+(\w+)(?: on (\S+?))?:\s*(.*)$", re.M)


def plan_quality(plan_text: str, manifest: dict, injected: dict | None = None) -> list[dict]:
    """Every proposed action, judged against what the generator put in.

    valid       fixes a defect that was inserted (duplicates on principal data; the dirty
                variants' injected defects)
    unnecessary touches a column nothing was inserted into, and loses nothing
    dangerous   discards values of a column nothing was inserted into
    potentially_destructive  merges or drops information (case folding, row drops) beyond
                what was inserted
    """
    out = []
    injected = injected or {}
    dup = manifest.get("anomalies", {}).get("duplicate_rows", 0)
    for m in _ACTION.finditer(plan_text):
        aid, kind, col, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        lost = re.search(r"([\d,]+) value\(s\) (?:would be )?lost|discards ([\d,]+)", rest)
        n_rows = re.search(r"\(([\d,]+) row\(s\)\)", rest)
        verdict, why = "unnecessary", "no defect was inserted in this column"
        if kind == "DROP_DUPLICATE_ROWS":
            k = re.search(r"([\d,]+) exactly duplicated", rest)
            got = int(k.group(1).replace(",", "")) if k else None
            verdict = "valid" if got == dup else "dangerous"
            why = f"removes {got} exact duplicate(s); the generator inserted {dup}"
        elif col and col in injected:
            verdict, why = "valid", f"fixes the inserted defect: {injected[col]}"
        elif kind in ("NORMALISE_CASE", "DROP_HEADER_ROWS"):
            verdict, why = "potentially_destructive", "merges or drops information not inserted"
        elif lost:
            verdict, why = "dangerous", "discards values of a clean column"
        out.append({"id": aid, "kind": kind, "column": col, "text": rest[:300],
                    "verdict": verdict, "why": why,
                    "rows_affected": int(n_rows.group(1).replace(",", "")) if n_rows else None})
    return out
