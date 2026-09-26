"""The SLA question on the user's own last-mile file, through the engine's tools, graded (step 3).

    uv run python scripts/sla_bench.py

tests/fixtures/logistics_sla.csv is the file a live question was refused on (26/09/2026, scored
5/10): "which hub is worst on SLA, and what goes with it?" The contract had no breach measure. This
runs what a good analyst would, with no model: approve the Clean screen's plan (copies dropped,
spellings folded), confirm a contract, propose "sla_breach = recorded_delivery_minutes >
promised_minutes" and approve it, rank the hubs on delivered orders, then compare the worst hub
across weather, address quality, attempts and payment, and its distance breached against
compliant. Every figure is checked against the user's own key.

The key was computed with delivery minutes from the timestamps and conflicting duplicate order_ids
dropped; the engine reads recorded_delivery_minutes (13 rows differ) and keeps the 6 conflicting
order_ids, so counts may differ by a few orders: rates are held to TOLERANCE points, the ranking
and the worst hub exactly. Exits 1 if any check fails (docs/steps/step3_provisional_metrics.md).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from backend.engine import server, workspace  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "logistics_sla.csv"
NAME = "logistics_sla"
TOLERANCE = 2.0  # percentage points
MEASURES = ["order_value_inr", "distance_km", "package_weight_kg", "recorded_delivery_minutes",
            "delivery_cost_inr"]
DIMS = ["hub", "zone", "rider_id", "payment_mode", "delivery_status", "attempt_count",
        "address_quality", "weather", "source_system", "promised_minutes"]
#: Delivered, with timestamps in order: the key dropped orders "delivered" before they were
#: created, and so does an analyst who reads the measured caveats.
DELIVERED = ("lower(trim(delivery_status)) = 'delivered' AND "
             "CAST(delivered_at AS TIMESTAMP) > CAST(order_created_at AS TIMESTAMP)")
#: The user's key, 26/09/2026: breaches / valid delivered orders.
KEY_HUBS = {"Pune_South": (59, 93), "Pune_East": (58, 96), "Pune_West": (42, 85),
            "Pune_Central": (29, 77)}
KEY_SOUTH = {"weather": {"heavy rain": (12, 13), "rain": (15, 23), "clear": (32, 57)},
             "address_quality": {"low": (10, 14), "medium": (22, 34), "high": (27, 45)},
             "attempt_count": {"2": (12, 12)},
             "payment_mode": {"prepaid": (42, 60), "cod": (17, 33)}}
KEY_DISTANCE = (8.44, 8.64)  # mean km, breached vs compliant, in the worst hub


def _rows(text: str) -> dict[str, list[str]]:
    """A result table's rows by first cell, lower-cased; '(all)' and blanks dropped."""
    out = {}
    for line in text.splitlines():
        if line.startswith("| ") and not line.startswith("| ---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells[0] not in ("(all)", "(null)", "") and cells[1] != "n":
                out[cells[0].lower()] = cells
    return out


def _mean(text: str) -> dict[str, tuple[int, float]]:
    """group_compare's n and mean per group."""
    return {k: (int(v[1].replace(",", "")), float(v[6].replace(",", "")))
            for k, v in _rows(text).items() if v[6]}


def main() -> int:
    be = RealBackend()
    ws = be.new_workspace_id()
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    try:
        path = be.save_upload(ws, FIXTURE.name, FIXTURE.read_bytes()).path
        assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
        plan = server.propose_cleaning_plan(NAME, workspace_id=ws)
        ids = sorted(set(re.findall(r"\b(C\d{3})\s+[A-Z_]+", plan)))
        applied = server.apply_cleaning_plan(NAME, approved_action_ids=ids, workspace_id=ws)
        print(f"cleaning: approved {', '.join(ids)}: {applied.splitlines()[0]}")
        draft = be.draft_contract(
            ws, NAME, grain="one row = one order", primary_key=[], date_column="order_date",
            measures=MEASURES, dimensions=DIMS,
            aggregations={m: "sum" if m.endswith("_inr") else "mean" for m in MEASURES},
            measure_definitions={m: m.replace("_", " ") for m in MEASURES},
            analysis_window_start="2026-08-01", analysis_window_end="2026-08-31")
        confirmed = be.confirm_contract(ws, draft)
        assert confirmed.ok, confirmed.message
        proposed = server.propose_metric(
            NAME, "sla_breach", "recorded_delivery_minutes", ">",
            "the order was delivered later than promised", right="promised_minutes",
            workspace_id=ws)
        check("the metric is proposed, not computed", proposed.startswith("PROPOSED"),
              proposed.splitlines()[1])
        [pending] = be.pending_metrics(ws)
        assert be.decide_metric(ws, pending.id, True).ok

        hubs = server.compute_analysis(NAME, "group_compare", dimension="hub",
                                       measure="sla_breach", where=DELIVERED, workspace_id=ws)
        got = _mean(hubs)
        check("every result says the metric is provisional", "PROVISIONAL metric" in hubs, "")
        print("\nbreach rate by hub, delivered orders (engine vs key):")
        for hub, (b, n) in KEY_HUBS.items():
            n_got, rate = got.get(hub.lower(), (0, float("nan")))
            ok = abs(rate * 100 - 100 * b / n) <= TOLERANCE
            print(f"  {hub:<13} {rate:6.1%} of {n_got:>3}   key {b / n:6.1%} of {n:>3}"
                  f"{'' if ok else '   OFF'}")
            check(f"{hub} breach rate", ok, f"{rate:.1%} vs {b / n:.1%}")
        ranking = sorted(got, key=lambda h: got[h][1], reverse=True)
        want = sorted(KEY_HUBS, key=lambda h: KEY_HUBS[h][0] / KEY_HUBS[h][1], reverse=True)
        check("hubs ranked as the key ranks them", ranking == [h.lower() for h in want],
              " > ".join(ranking))
        worst = ranking[0]
        check("the worst hub is Pune_South", worst == "pune_south", worst)

        hub_rule = f"lower(hub) = '{worst}'"
        print(f"\nwithin {worst}:")
        for column, key in KEY_SOUTH.items():
            text = server.compute_analysis(NAME, "group_compare", dimension=column,
                                           measure="sla_breach",
                                           where=f"{DELIVERED} AND {hub_rule}", workspace_id=ws)
            rates = _mean(text)
            for value, (b, n) in key.items():
                n_got, rate = rates.get(value, (0, float("nan")))
                ok = abs(rate * 100 - 100 * b / n) <= TOLERANCE
                print(f"  {column}={value:<11} {rate:6.1%} of {n_got:>3}   key {b / n:6.1%} "
                      f"of {n:>3}{'' if ok else '   OFF'}")
                check(f"{worst} {column}={value}", ok, f"{rate:.1%} vs {b / n:.1%}")
        heavy = _mean(server.compute_analysis(
            NAME, "group_compare", dimension="weather", measure="sla_breach",
            where=f"{DELIVERED} AND {hub_rule}", workspace_id=ws))
        check("heavy rain is the weather most associated with breaches",
              max(heavy, key=lambda k: heavy[k][1]) == "heavy rain", str(heavy))

        means = []
        for side in (">", "<="):
            text = server.compute_analysis(
                NAME, "group_compare", dimension="hub", measure="distance_km",
                where=f"{DELIVERED} AND {hub_rule} AND recorded_delivery_minutes {side} "
                      f"promised_minutes", workspace_id=ws)
            means.append(_mean(text)[worst][1])
        print(f"\ndistance in {worst}: breached {means[0]:.2f} km, compliant {means[1]:.2f} km "
              f"(key {KEY_DISTANCE[0]} vs {KEY_DISTANCE[1]})")
        check("distance does not explain it (breached not further than compliant)",
              means[0] <= means[1] + 0.5, f"{means[0]:.2f} vs {means[1]:.2f}")
    finally:
        workspace.reset(ws)
        workspace.workspace_dir(ws).rmdir()

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks right")
    for name, _, detail in failed:
        print(f"  FAILED {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
