"""Workspace paths and reset.

The workspace id is a PARAMETER, never generated at import. Claude Desktop keeps
this process alive across chats, so a module-level uuid would leak state between
unrelated tasks. Track A passes a stable id; Track B will pass a session id.
"""
import re
import shutil
import time
from collections.abc import Callable
from typing import Any
from pathlib import Path

from .config import WORKSPACE_ROOT, DEFAULT_WORKSPACE_ID


def workspace_dir(workspace_id: str = DEFAULT_WORKSPACE_ID) -> Path:
    d = WORKSPACE_ROOT / workspace_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def duckdb_path(workspace_id: str = DEFAULT_WORKSPACE_ID) -> Path:
    return workspace_dir(workspace_id) / "session.duckdb"


def outputs_dir(workspace_id: str = DEFAULT_WORKSPACE_ID) -> Path:
    d = workspace_dir(workspace_id) / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reset(workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict:
    """Delete everything in this workspace. Returns what was removed."""
    d = WORKSPACE_ROOT / workspace_id
    if not d.exists():
        return {"existed": False, "removed_files": 0, "bytes": 0}
    files = [p for p in d.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    shutil.rmtree(d)
    workspace_dir(workspace_id)
    return {"existed": True, "removed_files": len(files), "bytes": total}

# --- expiry of web workspaces (Phase 14 Step 5, closes P14-O1) ----------------------------------

#: Written by the web backend on every call to a workspace that exists. Reads leave no other trace
#: on disk (measured, step 5 M2), so without it a person still reading looks like one who left.
LAST_USED = ".last_used"
WEB_ID = re.compile(r"^ws_[0-9a-f]{12}$")


def touch(workspace_id: str) -> None:
    """Record that this workspace was just used. Creates nothing that did not exist."""
    d = WORKSPACE_ROOT / workspace_id
    if d.is_dir():
        (d / LAST_USED).touch()


def last_used(d: Path) -> float:
    """The newest of the marker and every file's mtime, the directory's own included."""
    newest = d.stat().st_mtime
    for p in d.rglob("*"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except FileNotFoundError:  # removed while we looked
            continue
    return newest


def sweep_idle(max_age_seconds: float, *, now: float | None = None,
               lock_for: Callable[[str], Any] | None = None) -> list[str]:
    """Remove web workspaces (ws_ + 12 hex) unused for longer than `max_age_seconds`.

    Never anything else under the root: "local" is Claude Desktop's, and a name this code did
    not mint is not this code's to delete. `lock_for(wid)` gives that workspace's lock: it is
    taken WITHOUT waiting and held across the check and the removal, so a workspace in use is
    skipped and none can come into use while it is being removed. Returns the ids removed.
    """
    if max_age_seconds <= 0 or not WORKSPACE_ROOT.is_dir():
        return []
    now = time.time() if now is None else now
    removed = []
    for d in sorted(WORKSPACE_ROOT.iterdir()):
        if not d.is_dir() or not WEB_ID.match(d.name):
            continue
        lock = lock_for(d.name) if lock_for else None
        if lock is not None and not lock.acquire(blocking=False):
            continue  # in use right now
        try:
            if d.is_dir() and now - last_used(d) > max_age_seconds:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)
        finally:
            if lock is not None:
                lock.release()
    return removed
