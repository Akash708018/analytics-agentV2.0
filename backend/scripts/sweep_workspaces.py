"""Remove web workspaces nobody has used for ANALYTICS_WORKSPACE_TTL_HOURS (default 72).

    uv run python scripts/sweep_workspaces.py          # remove, and say what went
    uv run python scripts/sweep_workspaces.py --dry    # say what would go, remove nothing

The web backend also sweeps, at most hourly, when a new visitor arrives; this is for a cron on a
host that may go quiet. Only ws_ + 12 hex directories are ever touched -- never "local" (P14-O1).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.config import WORKSPACE_ROOT  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402


def main(argv: list[str]) -> int:
    ttl = RealBackend.ttl_seconds()
    if ttl <= 0:
        print("ANALYTICS_WORKSPACE_TTL_HOURS=0: expiry is off.")
        return 0
    if "--dry" in argv:
        now = time.time()
        idle = [d.name for d in sorted(WORKSPACE_ROOT.iterdir())
                if d.is_dir() and workspace.WEB_ID.match(d.name)
                and now - workspace.last_used(d) > ttl] if WORKSPACE_ROOT.is_dir() else []
        print(f"{len(idle)} idle web workspace(s) older than {ttl / 3600:g}h: {', '.join(idle)}")
        return 0
    removed = RealBackend().sweep()
    print(f"removed {len(removed)} idle web workspace(s) older than {ttl / 3600:g}h"
          + (f": {', '.join(removed)}" if removed else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
